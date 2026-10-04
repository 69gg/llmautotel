"""电话媒体驱动的最小公共接口。"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol


class TelephonyError(RuntimeError):
    """可向界面展示的阶段错误，不包含上游响应或凭据。"""


@dataclass
class MediaCallbacks:
    on_ready: Callable[[], Awaitable[None]]
    on_audio: Callable[[bytes, int], Awaitable[None]]
    on_ended: Callable[[str], Awaitable[None]]
    on_error: Callable[[str], Awaitable[None]]


class MediaDriver(Protocol):
    """单声道 PCM16LE；驱动只在 start 时建立外部连接。"""

    sample_rate: int

    async def start(self, number: str, call_id: str) -> None: ...

    async def send_audio(self, audio: bytes) -> None: ...

    async def flush(self) -> None: ...

    async def wait_played(self) -> None: ...

    async def hangup(self) -> None: ...

    async def close(self) -> None: ...
