"""按已保存配置维护原生入呼监听；所有连接默认关闭。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from fastapi import HTTPException

from llmautotel.models import AppSettings
from llmautotel.sessions import SessionManager
from llmautotel.store import Store
from llmautotel.telephony.factory import missing_phone_fields
from llmautotel.telephony.incoming import IncomingCall, IncomingCallbacks, IncomingListener
from llmautotel.telephony.settings import PROVIDER_NAMES, TelephonyProviderName

ListenerFactory = Callable[
    [TelephonyProviderName, AppSettings, IncomingCallbacks], IncomingListener
]


def default_listener_factory(
    provider: TelephonyProviderName, settings: AppSettings, callbacks: IncomingCallbacks
) -> IncomingListener:
    if provider == "asterisk":
        from llmautotel.telephony.asterisk import AsteriskIncomingListener

        return AsteriskIncomingListener(settings.telephony.asterisk, callbacks)
    if provider == "freeswitch":
        from llmautotel.telephony.freeswitch import FreeSwitchIncomingListener

        return FreeSwitchIncomingListener(settings.telephony.freeswitch, callbacks)
    raise ValueError("云来电通过正式 HTTP 回调接入")


class IncomingService:
    """监听参数变更延后至原有入呼结束，对话配置在每次来电时取快照。"""

    def __init__(
        self,
        store: Store,
        sessions: SessionManager,
        *,
        listener_factory: ListenerFactory = default_listener_factory,
        refresh_seconds: float = 1,
    ) -> None:
        self.store = store
        self.sessions = sessions
        self._factory = listener_factory
        self._refresh_seconds = refresh_seconds
        self._tasks: list[asyncio.Task[None]] = []
        self._states: dict[str, dict[str, str | None]] = {}
        self._closing = False

    def start(self) -> None:
        if self._tasks or self._closing:
            return
        for provider in ("asterisk", "freeswitch"):
            self._tasks.append(asyncio.create_task(self._watch(provider)))

    def pause(self) -> None:
        self._closing = True

    async def status(self) -> list[dict[str, Any]]:
        settings = await self.store.get_settings()
        result: list[dict[str, Any]] = []
        for provider in PROVIDER_NAMES:
            config = getattr(settings.telephony, provider)
            state = dict(self._states.get(provider, {"state": "connecting", "error": None}))
            missing = missing_phone_fields(settings, provider, direction="inbound")
            if not config.enabled or not config.inbound_enabled:
                state = {"state": "disabled", "error": None}
            elif missing:
                state = {"state": "incomplete", "error": "请先配置：" + "、".join(missing)}
            elif provider in {"aliyun", "tencent"}:
                state = {"state": "awaiting_callback", "error": None}
            result.append({"provider": provider, **state})
        return result

    async def _watch(self, provider: TelephonyProviderName) -> None:
        listener: IncomingListener | None = None
        snapshot: AppSettings | None = None
        retry_at = 0.0
        try:
            while not self._closing:
                settings = await self.store.get_settings()
                config = getattr(settings.telephony, provider)
                enabled = config.enabled and config.inbound_enabled
                missing = missing_phone_fields(settings, provider, direction="inbound")
                changed = snapshot is not None and config != getattr(snapshot.telephony, provider)
                active = await self.sessions.active_record()
                in_use = (
                    active is not None
                    and active.provider == provider
                    and active.direction == "inbound"
                )
                if listener is not None and (changed or not enabled or missing):
                    if in_use:
                        self._states[provider] = {
                            "state": "pending",
                            "error": "线路更新将在当前来电结束后生效",
                        }
                    else:
                        closed = await self._close_listener(provider, listener)
                        listener = None
                        snapshot = None
                        if not closed:
                            retry_at = (
                                asyncio.get_running_loop().time() + config.inbound_reconnect_seconds
                            )
                if not enabled:
                    self._states[provider] = {"state": "disabled", "error": None}
                elif missing:
                    self._states[provider] = {
                        "state": "incomplete",
                        "error": "请先配置：" + "、".join(missing),
                    }
                elif listener is not None and not changed:
                    # 监听器自行重连，不能另开同 ARI 应用连接抢走旧通事件。
                    self._states[provider] = (
                        {"state": "listening", "error": None}
                        if listener.connected
                        else {"state": "failed", "error": "来电监听连接中断，正在重连"}
                    )
                elif listener is None and asyncio.get_running_loop().time() >= retry_at:
                    snapshot = settings.model_copy(deep=True)

                    async def on_call(
                        incoming: IncomingCall, source: AppSettings = snapshot
                    ) -> bool:
                        if self._closing:
                            return False
                        try:
                            record = await self.sessions.accept_incoming(
                                incoming, listener_settings=source
                            )
                        except HTTPException:
                            return False
                        return record.ended_at is None

                    async def on_error(message: str) -> None:
                        # 不透传供应商异常或连接 URL；更详细状态由监听器自行脱敏。
                        self._states[provider] = {
                            "state": "failed",
                            "error": "来电监听连接失败，请检查线路配置",
                        }

                    self._states[provider] = {"state": "connecting", "error": None}
                    try:
                        listener = self._factory(
                            provider, snapshot, IncomingCallbacks(on_call, on_error)
                        )
                        await listener.start()
                    except Exception:
                        if listener is not None:
                            await self._close_listener(provider, listener)
                        listener = None
                        snapshot = None
                        retry_at = (
                            asyncio.get_running_loop().time() + config.inbound_reconnect_seconds
                        )
                        self._states[provider] = {
                            "state": "failed",
                            "error": "来电监听连接失败，请检查线路配置",
                        }
                    else:
                        self._states[provider] = {"state": "listening", "error": None}
                await asyncio.sleep(self._refresh_seconds)
        finally:
            if listener is not None:
                await self._close_listener(provider, listener)

    async def _close_listener(
        self, provider: TelephonyProviderName, listener: IncomingListener
    ) -> bool:
        try:
            await listener.close()
        except Exception:
            self._states[provider] = {
                "state": "failed",
                "error": "来电监听连接清理失败，请检查线路状态",
            }
            return False
        return True

    async def close(self) -> None:
        self.pause()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
