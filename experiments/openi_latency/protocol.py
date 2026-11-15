"""延迟原型的纯 Python 音频计时；不加载模型、不修改正式应用。"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from typing import Any


def signal_offset_ms(pcm: bytes, sample_rate: int, threshold: int = 100) -> float | None:
    if sample_rate <= 0 or len(pcm) % 2:
        raise ValueError("invalid_pcm")
    window = max(1, sample_rate // 50) * 2
    for offset in range(0, len(pcm), window):
        values = [value for (value,) in struct.iter_unpack("<h", pcm[offset : offset + window])]
        if values and math.sqrt(sum(value * value for value in values) / len(values)) >= threshold:
            return offset / (sample_rate * 2) * 1000
    return None


def trim_silence(pcm: bytes, sample_rate: int, threshold: int = 100) -> bytes:
    window = max(1, sample_rate // 50) * 2
    end = 0
    for offset in range(0, len(pcm), window):
        if signal_offset_ms(pcm[offset : offset + window], sample_rate, threshold) is not None:
            end = offset + window
    if not end:
        raise ValueError("silent_fixture")
    return pcm[:end]


@dataclass
class PlaybackClock:
    """内存终端按音频时长播放，前导静音和包间等待均计入首音。"""

    queued_until: float = 0.0
    first_signal_at: float | None = None

    def feed(self, pcm: bytes, sample_rate: int, received_at: float) -> None:
        offset = signal_offset_ms(pcm, sample_rate)
        starts = max(received_at, self.queued_until)
        if self.first_signal_at is None and offset is not None:
            self.first_signal_at = starts + offset / 1000
        self.queued_until = starts + len(pcm) / (sample_rate * 2)


def summarize(samples: list[dict[str, Any]]) -> dict[str, Any]:
    values = sorted(float(row["effective_audio_ms"]) for row in samples if row.get("ok"))
    return {
        "attempted": len(samples),
        "successful": len(values),
        "failed": len(samples) - len(values),
        "p50_ms": values[math.ceil(len(values) * 0.5) - 1] if values else None,
        "p95_ms": values[math.ceil(len(values) * 0.95) - 1] if values else None,
        "sample_count_warning": "small_sample_not_stability_acceptance",
    }
