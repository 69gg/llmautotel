"""挂断收尾、迟到回调和单活跃会话槽位的调度回归。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import cast

import pytest
from fastapi import HTTPException
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.request_handler import (
    SmallWebRTCRequest,
    SmallWebRTCRequestHandler,
)
from test_sessions import configured_settings
from test_voice import ControlledLLM, ControlledTTS, FixtureServices, LocalTransport, Passthrough
from test_voice import Recorder as VoiceRecorder

import llmautotel.voice as voice_module
from llmautotel.models import AppSettings
from llmautotel.sessions import SessionManager, VoiceRuntime
from llmautotel.store import Store
from llmautotel.voice import VoiceCallbacks, VoiceSession


class LocalConnection:
    def __init__(self) -> None:
        self.pc_id = "local-peer"
        self.disconnected = False

    async def disconnect(self) -> None:
        self.disconnected = True


class LocalHandler:
    def __init__(self) -> None:
        self.connection = LocalConnection()
        self.closed = False

    async def handle_web_request(
        self,
        request: SmallWebRTCRequest,
        callback: Callable[[SmallWebRTCConnection], Awaitable[None]],
    ) -> dict[str, str]:
        await callback(cast(SmallWebRTCConnection, self.connection))
        return {"sdp": "local-answer", "type": "answer", "pc_id": self.connection.pc_id}

    async def close(self) -> None:
        self.closed = True
        await self.connection.disconnect()


class ControlledVoice:
    """stop 立即请求取消，run 在最终文字提交后才真正结束。"""

    def __init__(self, callbacks: VoiceCallbacks) -> None:
        self.callbacks = callbacks
        self.started = asyncio.Event()
        self.stop_requested = asyncio.Event()
        self.finish_allowed = asyncio.Event()
        self.reason = "disconnected"

    async def run(self) -> str:
        await self.callbacks.on_state("speaking")
        self.started.set()
        await self.stop_requested.wait()
        await self.finish_allowed.wait()
        await self.callbacks.on_message(
            "assistant", "已经播放完的句子。", True, "2026-10-04T09:00:00Z"
        )
        return self.reason

    async def stop(self, reason: str = "user_hangup") -> None:
        self.reason = reason
        self.stop_requested.set()


class VoiceFactory:
    def __init__(self) -> None:
        self.instances: list[ControlledVoice] = []

    def __call__(
        self,
        connection: SmallWebRTCConnection,
        settings: AppSettings,
        callbacks: VoiceCallbacks,
    ) -> VoiceRuntime:
        voice = ControlledVoice(callbacks)
        self.instances.append(voice)
        return voice


async def manager_with_fixture(
    tmp_path: Path, factory: VoiceFactory
) -> tuple[SessionManager, Store, list[LocalHandler]]:
    store = Store(tmp_path)
    await store.save_settings(configured_settings())
    handlers: list[LocalHandler] = []

    def handler_factory() -> SmallWebRTCRequestHandler:
        handler = LocalHandler()
        handlers.append(handler)
        return cast(SmallWebRTCRequestHandler, handler)

    manager = SessionManager(store, voice_factory=factory, handler_factory=handler_factory)
    return manager, store, handlers


async def connect(manager: SessionManager) -> str:
    call = await manager.start()
    call_id = call["call"]["id"]
    await manager.offer(call_id, {"sdp": "local-offer", "type": "offer"})
    return call_id


async def wait_instance(factory: VoiceFactory) -> ControlledVoice:
    async with asyncio.timeout(3):
        while not factory.instances:
            await asyncio.sleep(0)
    voice = factory.instances[-1]
    await asyncio.wait_for(voice.started.wait(), timeout=3)
    return voice


async def test_manager_hangup_commits_final_played_sentence_from_real_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path)
    settings = configured_settings()
    settings.sales.opening = ""
    await store.save_settings(settings)
    recorder = VoiceRecorder()
    transport = LocalTransport(recorder)
    services = FixtureServices(Passthrough(), ControlledLLM(block_first=True), ControlledTTS())
    monkeypatch.setattr(voice_module, "create_services", lambda settings: services)
    monkeypatch.setattr(voice_module, "SmallWebRTCTransport", lambda **kwargs: transport)
    handler = LocalHandler()
    manager = SessionManager(
        store, handler_factory=lambda: cast(SmallWebRTCRequestHandler, handler)
    )
    call_id = await connect(manager)
    try:
        await asyncio.wait_for(transport.outgoing.started.wait(), timeout=3)
        assert manager._active is not None
        voice = cast(VoiceSession, manager._active.voice)
        assert voice._worker is not None
        await voice._worker.rtvi.set_client_ready()
        await asyncio.wait_for(transport.outgoing.second_audio.wait(), timeout=3)

        ended = await manager.end(call_id)
        assert ended.status == "ended"
        assert ended.end_reason == "user_hangup"
        assert len(ended.transcript) == 1
        assert ended.transcript[0].text == "第一句。"
        assert ended.transcript[0].interrupted
        saved = await store.get_call(call_id)
        assert saved is not None and saved.transcript == ended.transcript
        assert services.llm.cancelled.is_set() and services.tts.cancelled.is_set()
        assert services.closed and handler.connection.disconnected
        assert await manager.active_record() is None
    finally:
        await manager.close()


async def test_slot_is_reserved_until_final_commit_and_late_callbacks_are_ignored(
    tmp_path: Path,
) -> None:
    factory = VoiceFactory()
    manager, store, handlers = await manager_with_fixture(tmp_path, factory)
    call_id = await connect(manager)
    voice = await wait_instance(factory)
    ending = asyncio.create_task(manager.end(call_id))
    await asyncio.wait_for(voice.stop_requested.wait(), timeout=3)
    try:
        with pytest.raises(HTTPException) as busy:
            await manager.start()
        assert busy.value.status_code == 409
        assert await manager.active_record() is not None
        voice.finish_allowed.set()
        ended = await asyncio.wait_for(ending, timeout=3)
        assert ended.transcript[0].text == "已经播放完的句子。"
        assert ended.transcript[0].interrupted
        assert handlers[0].closed
        assert await manager.active_record() is None

        await voice.callbacks.on_message(
            "assistant", "结束后的迟到内容。", False, "2026-10-04T09:01:00Z"
        )
        saved = await store.get_call(call_id)
        assert saved is not None and saved.transcript == ended.transcript
    finally:
        voice.finish_allowed.set()
        await manager.close()
        await asyncio.gather(ending, return_exceptions=True)


async def test_new_call_is_not_overwritten_by_previous_hangup_or_callbacks(tmp_path: Path) -> None:
    factory = VoiceFactory()
    manager, store, handlers = await manager_with_fixture(tmp_path, factory)
    first_id = await connect(manager)
    old_voice = await wait_instance(factory)
    old_voice.finish_allowed.set()
    first_record = await manager.end(first_id)
    second = await manager.start()
    second_id = second["call"]["id"]
    try:
        assert second_id != first_id
        await old_voice.callbacks.on_state("speaking")
        await old_voice.callbacks.on_message(
            "assistant", "旧通话的迟到内容。", True, "2026-10-04T09:02:00Z"
        )
        repeated = await manager.end(first_id)
        active = await manager.active_record()
        assert active is not None and active.id == second_id
        assert active.status == "connecting" and active.transcript == []
        assert repeated == first_record
        assert await store.get_call(first_id) == first_record
        assert handlers[0].closed and not handlers[1].closed
    finally:
        await manager.close()


async def test_hangup_before_voice_task_starts_never_constructs_voice(tmp_path: Path) -> None:
    factory = VoiceFactory()
    manager, store, handlers = await manager_with_fixture(tmp_path, factory)
    call_id = await connect(manager)
    # 本机 handler 在 callback 创建任务后立即返回，没有给新任务运行的机会。
    assert manager._active is not None
    assert manager._active.task is not None
    assert manager._active.voice is None
    ended = await manager.end(call_id)
    assert ended.end_reason == "user_hangup" and ended.status == "ended"
    assert factory.instances == []
    assert handlers[0].closed and handlers[0].connection.disconnected
    assert await manager.active_record() is None
    saved = await store.get_call(call_id)
    assert saved == ended
    second = await manager.start()
    assert second["call"]["id"] != call_id
    await manager.close()
