"""来电接纳、模式迁移、单会话竞争与提前挂断的真实管理路径。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi import HTTPException
from pydantic import SecretStr
from test_sessions import configured_settings

from llmautotel.models import AppSettings, CallRecord
from llmautotel.sessions import SessionManager
from llmautotel.store import Store
from llmautotel.telephony.incoming import IncomingCall, IncomingCallbacks
from llmautotel.telephony.service import IncomingService
from llmautotel.voice import VoiceCallbacks


def incoming_settings() -> AppSettings:
    settings = configured_settings()
    settings.conversation.mode = "consultation"
    settings.consultation.product_info = "产品支持资料整理和协作。"
    settings.telephony.asterisk.enabled = True
    settings.telephony.asterisk.inbound_enabled = True
    settings.telephony.asterisk.ari_url = "http://pbx.test/ari"
    settings.telephony.asterisk.username = "test-user"
    settings.telephony.asterisk.password = SecretStr("TEST-PBX-SECRET")
    return AppSettings.model_validate(settings.private())


class IncomingRuntime:
    def __init__(self, callbacks: VoiceCallbacks) -> None:
        self.callbacks = callbacks
        self.started = asyncio.Event()
        self.finished = asyncio.Event()
        self.reason = "remote_hangup"

    async def run(self) -> str:
        await self.callbacks.on_state("listening")
        self.started.set()
        await self.finished.wait()
        return self.reason

    async def stop(self, reason: str = "user_hangup") -> None:
        self.reason = reason
        self.finished.set()


async def test_upgrade_keeps_sales_and_uses_product_copy_for_consultation(tmp_path: Path) -> None:
    store = Store(tmp_path)
    legacy = configured_settings().private()
    legacy.pop("conversation")
    legacy.pop("consultation")
    legacy["sales"]["instructions"] = "只推销不调查"
    with sqlite3.connect(store.path) as connection:
        connection.execute("INSERT INTO settings VALUES(1,?)", (json.dumps(legacy),))
    upgraded = await store.get_settings()
    assert upgraded.conversation.mode == "consultation"
    assert upgraded.consultation.product_info == legacy["sales"]["product_info"]
    assert upgraded.consultation.instructions == upgraded.consultation.opening == ""
    assert upgraded.sales.model_dump() == legacy["sales"]
    await store.save_settings(upgraded)
    assert (await Store(tmp_path).get_settings()).conversation.mode == "consultation"
    # 旧客户端省略新分组也不抹掉已保存咨询资料或模式。
    old_update = AppSettings.model_validate({"sales": {"goal": "保留的销售目标"}})
    saved = await store.save_settings(old_update)
    assert saved.consultation == upgraded.consultation
    assert saved.conversation == upgraded.conversation


async def test_inbound_competes_with_browser_and_retries_are_idempotent(tmp_path: Path) -> None:
    store = Store(tmp_path)
    await store.save_settings(incoming_settings())
    runtimes: list[IncomingRuntime] = []

    def factory(
        incoming: IncomingCall, local_id: str, settings: AppSettings, callbacks: VoiceCallbacks
    ) -> IncomingRuntime:
        assert incoming.remote_id in {"remote-1", "remote-after"} and local_id
        assert settings.conversation.mode == "consultation"
        runtime = IncomingRuntime(callbacks)
        runtimes.append(runtime)
        return runtime

    manager = SessionManager(store, incoming_factory=factory)
    incoming = IncomingCall("asterisk", "remote-1", "13800000000", "01012345678")
    results = await asyncio.gather(
        manager.accept_incoming(incoming), manager.start(), return_exceptions=True
    )
    assert sum(isinstance(result, (CallRecord, dict)) for result in results) == 1
    assert (
        sum(isinstance(result, HTTPException) and result.status_code == 409 for result in results)
        == 1
    )
    await manager.close()
    incoming = IncomingCall("asterisk", "remote-after", "13800000000", "01012345678")
    record = await manager.accept_incoming(incoming)
    duplicate = await manager.accept_incoming(incoming)
    assert duplicate.id == record.id
    assert duplicate.direction == "inbound" and duplicate.caller == "13800000000"
    assert duplicate.destination == "01012345678"
    assert "SECRET" not in duplicate.model_dump_json()
    await manager.close()
    # 已结束远端ID的重试不能重新创建或接听旧通话。
    ended_duplicate = await manager.accept_incoming(incoming)
    assert ended_duplicate.id == record.id and ended_duplicate.ended_at is not None
    assert await manager.active_record() is None


async def test_incoming_snapshot_and_hangup_before_runtime_start(tmp_path: Path) -> None:
    store = Store(tmp_path)
    settings = incoming_settings()
    await store.save_settings(settings)
    hangups: list[str] = []
    runtimes: list[IncomingRuntime] = []

    async def hangup(reason: str) -> None:
        hangups.append(reason)

    def factory(
        incoming: IncomingCall, local_id: str, snapshot: AppSettings, callbacks: VoiceCallbacks
    ) -> IncomingRuntime:
        runtime = IncomingRuntime(callbacks)
        runtimes.append(runtime)
        return runtime

    manager = SessionManager(store, incoming_factory=factory)
    call = IncomingCall("asterisk", "remote-early", "13800000000", "01012345678", hangup=hangup)
    accepted = await manager.accept_incoming(call)
    await manager.end(accepted.id)
    assert runtimes == [] and hangups == ["normal"]
    assert await manager.active_record() is None
    second = await manager.accept_incoming(
        IncomingCall("asterisk", "remote-next", "13900000000", "01012345678")
    )
    settings.consultation.product_info = "下次的新资料"
    await store.save_settings(settings)
    assert second.settings["consultation"]["product_info"] == "产品支持资料整理和协作。"
    await manager.close()


async def test_disabled_incomplete_and_stale_listener_reject_without_history(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path)
    manager = SessionManager(store)
    call = IncomingCall("asterisk", "remote", "13800000000", "01012345678")
    with pytest.raises(HTTPException) as disabled:
        await manager.accept_incoming(call)
    assert disabled.value.status_code == 403
    settings = incoming_settings()
    settings.consultation.product_info = ""
    await store.save_settings(settings)
    with pytest.raises(HTTPException) as missing:
        await manager.accept_incoming(call)
    assert missing.value.status_code == 422
    settings.consultation.product_info = "产品资料"
    await store.save_settings(settings)
    stale = settings.model_copy(deep=True)
    stale.telephony.asterisk.ari_url = "http://old.test/ari"
    with pytest.raises(HTTPException) as conflict:
        await manager.accept_incoming(call, listener_settings=stale)
    assert conflict.value.status_code == 409
    assert await store.list_calls() == []


class FakeListener:
    def __init__(self, callbacks: IncomingCallbacks) -> None:
        self.callbacks = callbacks
        self.connected = False
        self.last_error: str | None = None
        self.closed = False

    async def start(self) -> None:
        self.connected = True

    async def close(self) -> None:
        self.connected = False
        self.closed = True


async def test_service_is_default_off_and_reload_waits_for_current_inbound(tmp_path: Path) -> None:
    store = Store(tmp_path)
    manager = SessionManager(store, incoming_factory=lambda i, c, s, cb: IncomingRuntime(cb))
    listeners: list[FakeListener] = []

    def factory(provider: str, settings: AppSettings, callbacks: IncomingCallbacks) -> FakeListener:
        listener = FakeListener(callbacks)
        listeners.append(listener)
        return listener

    service = IncomingService(store, manager, listener_factory=factory, refresh_seconds=0.005)
    service.start()
    await asyncio.sleep(0.02)
    assert listeners == []
    assert all(row["state"] == "disabled" for row in await service.status())
    settings = incoming_settings()
    await store.save_settings(settings)
    async with asyncio.timeout(2):
        while not listeners:
            await asyncio.sleep(0.005)
    incoming = IncomingCall("asterisk", "live-remote", "13800000000", "01012345678")
    assert await listeners[0].callbacks.on_call(incoming)
    settings.telephony.asterisk.username = "new-user"
    await store.save_settings(settings)
    async with asyncio.timeout(2):
        while (await service.status())[0]["state"] != "pending":
            await asyncio.sleep(0.005)
    assert not listeners[0].closed and len(listeners) == 1
    assert not await listeners[0].callbacks.on_call(
        IncomingCall("asterisk", "new-remote", "13900000000", "01012345678")
    )
    current = await manager.active_record()
    assert current is not None
    await manager.end(current.id)
    async with asyncio.timeout(2):
        while len(listeners) != 2:
            await asyncio.sleep(0.005)
    assert listeners[0].closed
    service.pause()
    assert not await listeners[1].callbacks.on_call(incoming)
    await service.close()
    assert listeners[1].closed


async def test_cancelled_hangup_before_runtime_start_finishes_cleanup_and_releases_slot(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path)
    await store.save_settings(incoming_settings())
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()
    cleanup_finished = False
    created: list[str] = []

    async def hangup(reason: str) -> None:
        nonlocal cleanup_finished
        cleanup_started.set()
        await cleanup_release.wait()
        cleanup_finished = True

    def factory(
        incoming: IncomingCall, call_id: str, settings: AppSettings, callbacks: VoiceCallbacks
    ) -> IncomingRuntime:
        created.append(call_id)
        return IncomingRuntime(callbacks)

    manager = SessionManager(store, incoming_factory=factory)
    accepted = await manager.accept_incoming(
        IncomingCall("asterisk", "cancel-before-run", "13800000000", "01012345678", hangup=hangup)
    )
    parent = asyncio.current_task()
    assert parent is not None

    async def cancel_waiter() -> None:
        await cleanup_started.wait()
        parent.cancel()
        await asyncio.sleep(0)
        assert await manager.active_record() is not None and not cleanup_finished
        cleanup_release.set()

    canceller = asyncio.create_task(cancel_waiter())
    with pytest.raises(asyncio.CancelledError):
        await manager.end(accepted.id)
    await canceller
    assert created == [] and cleanup_finished
    assert await manager.active_record() is None
    record = await store.get_call(accepted.id)
    assert record is not None and record.ended_at is not None and record.end_reason == "user_hangup"
    assert (await manager.start())["call"]["id"] != accepted.id
    await manager.close()


async def test_listener_close_failure_remains_visible_and_does_not_kill_reload(
    tmp_path: Path,
) -> None:
    from llmautotel.telephony.base import TelephonyError

    store = Store(tmp_path)
    settings = incoming_settings()
    settings.telephony.asterisk.inbound_reconnect_seconds = 1
    await store.save_settings(settings)
    manager = SessionManager(store)
    listeners: list[FakeListener] = []

    class BrokenClose(FakeListener):
        async def close(self) -> None:
            await super().close()
            raise TelephonyError("upstream TEST-PBX-SECRET response")

    def factory(provider: str, snapshot: AppSettings, callbacks: IncomingCallbacks) -> FakeListener:
        listener = BrokenClose(callbacks) if not listeners else FakeListener(callbacks)
        listeners.append(listener)
        return listener

    service = IncomingService(store, manager, listener_factory=factory, refresh_seconds=0.005)
    service.start()
    async with asyncio.timeout(2):
        while not listeners:
            await asyncio.sleep(0.005)
    settings.telephony.asterisk.username = "replacement-user"
    await store.save_settings(settings)
    async with asyncio.timeout(2):
        while (await service.status())[0]["state"] != "failed":
            await asyncio.sleep(0.005)
    assert all(not task.done() for task in service._tasks)
    assert "SECRET" not in str(await service.status())
    async with asyncio.timeout(2):
        while len(listeners) != 2:
            await asyncio.sleep(0.005)
    assert listeners[1].connected
    # 监听器自己重连，协调器不新建第三个相同 ARI 应用连接。
    listeners[1].connected = False
    await asyncio.sleep(0.02)
    assert (await service.status())[0]["state"] == "failed"
    listeners[1].connected = True
    await asyncio.sleep(0.02)
    assert (await service.status())[0]["state"] == "listening" and len(listeners) == 2
    await service.close()
