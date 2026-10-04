"""单活跃会话管理、WebRTC 协商和记录生命周期。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol
from uuid import uuid4

from fastapi import HTTPException

from llmautotel.models import AppSettings, CallRecord, TranscriptEntry
from llmautotel.store import Store

if TYPE_CHECKING:
    from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
    from pipecat.transports.smallwebrtc.request_handler import SmallWebRTCRequestHandler

    from llmautotel.voice import VoiceCallbacks


def now() -> str:
    return datetime.now(UTC).isoformat()


class VoiceRuntime(Protocol):
    async def run(self) -> str: ...

    async def stop(self, reason: str = "user_hangup") -> None: ...


VoiceFactory = Callable[["SmallWebRTCConnection", AppSettings, "VoiceCallbacks"], VoiceRuntime]


def default_voice_factory(
    connection: SmallWebRTCConnection, settings: AppSettings, callbacks: VoiceCallbacks
) -> VoiceRuntime:
    from llmautotel.voice import VoiceSession

    return VoiceSession(connection, settings, callbacks)


def default_handler_factory() -> SmallWebRTCRequestHandler:
    from pipecat.transports.smallwebrtc.request_handler import (
        ConnectionMode,
        SmallWebRTCRequestHandler,
    )

    return SmallWebRTCRequestHandler(connection_mode=ConnectionMode.SINGLE, ice_servers=[])


@dataclass
class ActiveCall:
    record: CallRecord
    settings: AppSettings
    handler: SmallWebRTCRequestHandler
    connection: SmallWebRTCConnection | None = None
    voice: VoiceRuntime | None = None
    task: asyncio.Task[None] | None = None
    watchdog: asyncio.Task[None] | None = None
    negotiating: bool = False
    closing: bool = False
    requested_reason: str | None = None
    error: str | None = None


class SessionManager:
    def __init__(
        self,
        store: Store,
        *,
        connection_timeout_seconds: float = 30,
        voice_factory: VoiceFactory = default_voice_factory,
        handler_factory: Callable[[], SmallWebRTCRequestHandler] = default_handler_factory,
    ) -> None:
        self.store = store
        self._active: ActiveCall | None = None
        self._lock = asyncio.Lock()
        self._voice_factory = voice_factory
        self._handler_factory = handler_factory
        self._connection_timeout = connection_timeout_seconds

    async def start(self) -> dict[str, Any]:
        async with self._lock:
            if self._active is not None:
                raise HTTPException(409, "已有一通对话进行中，请先挂断")
            settings = await self.store.get_settings()
            missing = settings.missing_call_fields()
            if missing:
                raise HTTPException(422, "请先配置：" + "、".join(missing))
            record = CallRecord(id=str(uuid4()), started_at=now(), settings=settings.public())
            active = ActiveCall(record, settings.model_copy(deep=True), self._handler_factory())
            await self.store.save_call(record)
            self._active = active
            active.watchdog = asyncio.create_task(self._watch_connection(active))
            return {
                "call": record.model_dump(mode="json"),
                "connection": {
                    "webrtcRequestParams": {"endpoint": f"/api/offer?call_id={record.id}"},
                    "iceConfig": {"iceServers": []},
                },
            }

    async def active_record(self) -> CallRecord | None:
        async with self._lock:
            return self._active.record.model_copy(deep=True) if self._active else None

    async def _watch_connection(self, active: ActiveCall) -> None:
        await asyncio.sleep(self._connection_timeout)
        async with self._lock:
            expired = self._active is active and active.record.status == "connecting"
            if expired:
                active.error = "连接超时，请重新开始"
        if expired:
            await self.end(active.record.id, "connection_timeout")

    def _require_active(self, call_id: str) -> ActiveCall:
        if self._active is None or self._active.record.id != call_id or self._active.closing:
            raise HTTPException(409, "此通对话已结束或已失效，请重新开始")
        return self._active

    async def offer(self, call_id: str, payload: dict[str, Any]) -> dict[str, str]:
        from pipecat.transports.smallwebrtc.request_handler import SmallWebRTCRequest

        async with self._lock:
            active = self._require_active(call_id)
            if active.negotiating:
                raise HTTPException(409, "连接正在协商，请稍候")
            if active.connection and payload.get("pc_id") != active.connection.pc_id:
                raise HTTPException(409, "已有连接，请勿重复开始")
            if not active.connection and payload.get("pc_id"):
                raise HTTPException(400, "连接标识已失效")
            active.negotiating = True

        async def connected(connection: SmallWebRTCConnection) -> None:
            async with self._lock:
                if self._active is not active or active.closing:
                    await connection.disconnect()
                    return
                active.connection = connection
                active.task = asyncio.create_task(self._run_voice(active))

        try:
            answer = await active.handler.handle_web_request(
                SmallWebRTCRequest.from_dict(payload), connected
            )
            async with self._lock:
                valid = self._active is active and not active.closing
            if not valid:
                await active.handler.close()
                raise HTTPException(409, "对话已结束")
            if answer is None:
                raise RuntimeError("Missing WebRTC answer")
            return answer
        except HTTPException:
            raise
        except Exception:
            active.error = "连接协商失败，请重新开始"
            await self.end(call_id, "connection_failed")
            raise HTTPException(400, "连接协商失败，请重新开始") from None
        finally:
            active.negotiating = False

    async def patch(self, call_id: str, payload: dict[str, Any]) -> None:
        from pipecat.transports.smallwebrtc.request_handler import (
            IceCandidate,
            SmallWebRTCPatchRequest,
        )

        async with self._lock:
            active = self._require_active(call_id)
            if active.connection is None or payload["pc_id"] != active.connection.pc_id:
                raise HTTPException(404, "连接不存在")
        try:
            await active.handler.handle_patch_request(
                SmallWebRTCPatchRequest(
                    pc_id=payload["pc_id"],
                    candidates=[IceCandidate(**candidate) for candidate in payload["candidates"]],
                )
            )
        except HTTPException:
            raise
        except Exception:
            active.error = "连接候选地址无效，请重新开始"
            await self.end(call_id, "connection_failed")
            raise HTTPException(400, "连接候选地址无效") from None

    async def _run_voice(self, active: ActiveCall) -> None:
        from llmautotel.voice import VoiceCallbacks

        async def on_state(state: str) -> None:
            async with self._lock:
                if self._active is active and not active.closing and state != "ended":
                    active.record.status = "active"
                    await self.store.save_call(active.record)

        async def on_message(
            role: Literal["user", "assistant"], text: str, interrupted: bool, timestamp: str
        ) -> None:
            async with self._lock:
                if self._active is not active:
                    return
                active.record.transcript.append(
                    TranscriptEntry(
                        role=role, text=text, interrupted=interrupted, timestamp=timestamp
                    )
                )
                await self.store.save_call(active.record)

        async def on_error(message: str) -> None:
            active.error = message

        reason = "disconnected"
        try:
            assert active.connection is not None
            active.voice = self._voice_factory(
                active.connection, active.settings, VoiceCallbacks(on_state, on_message, on_error)
            )
            reason = await active.voice.run()
        except asyncio.CancelledError:
            reason = active.requested_reason or "server_shutdown"
        except Exception:
            active.error = active.error or "语音会话异常，请重新开始"
            reason = "internal_error"
        finally:
            await self._finish(active, active.requested_reason or reason)

    async def _finish(self, active: ActiveCall, reason: str) -> None:
        async with self._lock:
            if self._active is not active:
                return
            active.closing = True
        try:
            await active.handler.close()
        finally:
            async with self._lock:
                if self._active is not active:
                    return
                active.record.ended_at = now()
                active.record.status = "failed" if active.error else "ended"
                active.record.end_reason = active.error or reason
                await self.store.save_call(active.record)
                self._active = None
                if active.watchdog and active.watchdog is not asyncio.current_task():
                    active.watchdog.cancel()

    async def end(self, call_id: str, reason: str = "user_hangup") -> CallRecord:
        async with self._lock:
            active = self._active
            if active is None or active.record.id != call_id:
                existing = await self.store.get_call(call_id)
                if existing is None:
                    raise HTTPException(404, "通话记录不存在")
                return existing
            active.closing = True
            active.requested_reason = active.requested_reason or reason
        if active.voice:
            await active.voice.stop(reason)
        elif active.task:
            # 挂断可能发生在 SDP 返回后、语音任务首次运行之前。
            active.task.cancel()
            await asyncio.gather(active.task, return_exceptions=True)
        await active.handler.close()
        if active.task and not active.task.done() and active.task is not asyncio.current_task():
            try:
                await asyncio.wait_for(asyncio.shield(active.task), timeout=5)
            except TimeoutError:
                active.task.cancel()
                await asyncio.gather(active.task, return_exceptions=True)
        await self._finish(active, reason)
        return active.record.model_copy(deep=True)

    async def delete(self, call_id: str) -> None:
        async with self._lock:
            if self._active and self._active.record.id == call_id:
                raise HTTPException(409, "请先结束通话再删除记录")
            if not await self.store.delete_call(call_id):
                raise HTTPException(404, "通话记录不存在")

    async def close(self) -> None:
        record = await self.active_record()
        if record:
            await self.end(record.id, "server_shutdown")
