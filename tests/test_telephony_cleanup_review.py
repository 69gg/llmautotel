"""电话远端清理完成前，结束请求和取消均不能释放单通话槽位。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from pathlib import Path
from typing import TypeVar

import pytest
from fastapi import HTTPException
from test_telephony_sessions import phone_settings

from llmautotel.models import AppSettings
from llmautotel.sessions import SessionManager
from llmautotel.store import Store
from llmautotel.telephony.settings import TelephonyProviderName
from llmautotel.voice import VoiceCallbacks

T = TypeVar("T")


class DelayedPhoneCleanup:
    """远端收尾独立运行，模拟取消等待不能撤销已接受的资源创建。"""

    def __init__(self, callbacks: VoiceCallbacks) -> None:
        self.callbacks = callbacks
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()
        self.cleanup_started = asyncio.Event()
        self.cleanup_allowed = asyncio.Event()
        self.cleanup_finished = asyncio.Event()
        self.cleanup_task: asyncio.Task[None] | None = None
        self.reason = "disconnected"

    async def _cleanup(self) -> None:
        self.cleanup_started.set()
        await self.cleanup_allowed.wait()
        self.cleanup_finished.set()

    async def run(self) -> str:
        await self.callbacks.on_state("listening")
        self.started.set()
        try:
            await self.stopped.wait()
        finally:
            self.cleanup_task = asyncio.create_task(self._cleanup())
            await asyncio.shield(self.cleanup_task)
        return self.reason

    async def stop(self, reason: str = "user_hangup") -> None:
        self.reason = reason
        self.stopped.set()


async def configured_manager(
    tmp_path: Path,
) -> tuple[SessionManager, list[DelayedPhoneCleanup], str]:
    store = Store(tmp_path)
    await store.save_settings(phone_settings())
    voices: list[DelayedPhoneCleanup] = []

    def factory(
        provider: TelephonyProviderName,
        number: str,
        call_id: str,
        settings: AppSettings,
        callbacks: VoiceCallbacks,
    ) -> DelayedPhoneCleanup:
        voice = DelayedPhoneCleanup(callbacks)
        if voices:
            voice.cleanup_allowed.set()
        voices.append(voice)
        return voice

    manager = SessionManager(store, phone_factory=factory)
    call = await manager.start_phone("asterisk", "13800000000")
    async with asyncio.timeout(1):
        while not voices:
            await asyncio.sleep(0)
        await voices[0].started.wait()
    return manager, voices, call["call"]["id"]


async def test_phone_end_reserves_slot_until_remote_cleanup_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, voices, call_id = await configured_manager(tmp_path)
    first = voices[0]
    original_wait_for = asyncio.wait_for
    timeout_attempts: list[float] = []

    async def immediate_browser_fallback_timeout(future: Awaitable[T], timeout: float | None) -> T:
        if timeout == 5:
            timeout_attempts.append(timeout)
            await first.cleanup_started.wait()
            raise TimeoutError("模拟旧的五秒挂断兜底，无需真实等待")
        return await original_wait_for(future, timeout)

    monkeypatch.setattr(asyncio, "wait_for", immediate_browser_fallback_timeout)
    ending = asyncio.create_task(manager.end(call_id))
    try:
        await original_wait_for(first.cleanup_started.wait(), 1)
        # 让旧兜底、取消、落库和最终收尾均有机会完成，再检查资源约束。
        for _ in range(20):
            await asyncio.sleep(0)
        assert not timeout_attempts, "电话收尾不能复用浏览器的五秒强制取消"
        assert not ending.done()
        assert not first.cleanup_finished.is_set()
        record = await manager.active_record()
        assert record is not None and record.id == call_id and record.state == "ending"
        with pytest.raises(HTTPException) as busy:
            await manager.start_phone("asterisk", "13800000001")
        assert busy.value.status_code == 409
        first.cleanup_allowed.set()
        ended = await original_wait_for(ending, 1)
        assert first.cleanup_finished.is_set()
        assert ended.end_reason == "user_hangup" and ended.state == "ended"
        assert await manager.active_record() is None
        next_call = await manager.start_phone("asterisk", "13800000001")
        assert next_call["call"]["id"] != call_id
    finally:
        for voice in voices:
            voice.cleanup_allowed.set()
        await asyncio.gather(ending, return_exceptions=True)
        await manager.close()
        await asyncio.gather(
            *(voice.cleanup_task for voice in voices if voice.cleanup_task is not None),
            return_exceptions=True,
        )


async def test_cancelled_end_request_keeps_phone_slot_until_cleanup_finishes(
    tmp_path: Path,
) -> None:
    manager, voices, call_id = await configured_manager(tmp_path)
    first = voices[0]
    ending = asyncio.create_task(manager.end(call_id))
    try:
        await asyncio.wait_for(first.cleanup_started.wait(), 1)
        ending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await ending
        assert not first.cleanup_finished.is_set()
        assert first.cleanup_task is not None and not first.cleanup_task.done()
        with pytest.raises(HTTPException) as busy:
            await manager.start_phone("asterisk", "13800000001")
        assert busy.value.status_code == 409
        first.cleanup_allowed.set()
        async with asyncio.timeout(1):
            while await manager.active_record() is not None:
                await asyncio.sleep(0)
        assert first.cleanup_finished.is_set()
        record = await manager.store.get_call(call_id)
        assert record is not None and record.ended_at is not None
    finally:
        first.cleanup_allowed.set()
        await asyncio.gather(ending, return_exceptions=True)
        await manager.close()
        await asyncio.gather(
            *(voice.cleanup_task for voice in voices if voice.cleanup_task is not None),
            return_exceptions=True,
        )
