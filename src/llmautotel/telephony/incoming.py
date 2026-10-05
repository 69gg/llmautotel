"""来电接管接口；仅管理明确路由给本应用的电话。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from llmautotel.telephony.settings import TelephonyProviderName


class IncomingEvents:
    """单通 ARI 事件订阅，避免入呼驱动抢占监听器的应用 WebSocket。"""

    def __init__(self, remote_id: str, release: Callable[[IncomingEvents], None]) -> None:
        self.channels = {remote_id}
        self._queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=256)
        self._release = release
        self._closed = False

    def add_channel(self, channel_id: str) -> None:
        self.channels.add(channel_id)

    def put(self, event: dict[str, Any]) -> None:
        if self._closed:
            return
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            # 不能静默丢掉结束事件；队列过载只结束对应单通。
            self.disconnect()

    def disconnect(self) -> None:
        while not self._queue.empty():
            self._queue.get_nowait()
        self._queue.put_nowait(None)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self.disconnect()
            self._release(self)

    def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        return self

    async def __anext__(self) -> dict[str, Any]:
        event = await self._queue.get()
        if event is None:
            raise StopAsyncIteration
        return event


@dataclass
class IncomingCall:
    provider: TelephonyProviderName
    remote_id: str
    caller: str
    destination: str
    events: IncomingEvents | None = field(default=None, repr=False)
    hangup: Callable[[str], Awaitable[None]] | None = field(default=None, repr=False)
    _end_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _close_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    def release(self) -> None:
        if self.events is not None:
            self.events.close()

    async def _end(self, reason: str) -> None:
        async with self._close_lock:
            if self._end_task is None:
                self._end_task = asyncio.create_task(self._finish(reason))
            task = self._end_task
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # 挂断等待者取消时，远端清理仍须结束，避免已接纳来电遗留。
            await asyncio.shield(task)
            raise

    async def _finish(self, reason: str) -> None:
        try:
            if self.hangup is not None:
                await self.hangup(reason)
        finally:
            self.release()

    async def reject(self) -> None:
        await self._end("busy")

    async def close(self) -> None:
        """接纳后任务尚未启动时，也能清理原来电而不会重新拨号。"""
        await self._end("normal")


@dataclass
class IncomingCallbacks:
    on_call: Callable[[IncomingCall], Awaitable[bool]]
    on_error: Callable[[str], Awaitable[None]]


class IncomingListener(Protocol):
    connected: bool
    last_error: str | None

    async def start(self) -> None: ...

    async def close(self) -> None: ...
