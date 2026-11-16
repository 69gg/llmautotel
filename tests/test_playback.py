"""播放变速验证：时长、音调、尾音和打断余音。"""

import numpy as np
import pytest
from pipecat.frames.frames import InterruptionFrame, TTSAudioRawFrame, TTSTextFrame
from pipecat.processors.frame_processor import FrameDirection

from llmautotel.models import AppSettings
from llmautotel.playback import SpeechPlaybackSpeed
from llmautotel.remote_voice import remote_snapshot


def tone(seconds: float = 1.0) -> bytes:
    samples = np.arange(int(24000 * seconds))
    return (np.sin(samples * (2 * np.pi * 440 / 24000)) * 12000).astype("<i2").tobytes()


def test_speed_duration_pitch_and_passthrough() -> None:
    pcm = tone()
    processor = SpeechPlaybackSpeed(1.15)
    output: list[TTSAudioRawFrame] = []
    for offset in range(0, len(pcm), 960):
        output.extend(processor.convert(TTSAudioRawFrame(pcm[offset : offset + 960], 24000, 1)))
    output.extend(processor.convert(None))
    samples = np.frombuffer(b"".join(frame.audio for frame in output), dtype="<i2")
    assert len(samples) / 24000 == pytest.approx(1 / 1.15, abs=0.04)
    frequency = np.fft.rfftfreq(len(samples), 1 / 24000)[np.argmax(abs(np.fft.rfft(samples)))]
    assert frequency == pytest.approx(440, abs=4)
    assert all(frame.sample_rate == 24000 for frame in output)
    frame = TTSAudioRawFrame(pcm, 24000, 1)
    assert SpeechPlaybackSpeed(1).convert(frame) == [frame]


async def test_text_flushes_tail_and_interruption_discards_buffer() -> None:
    processor = SpeechPlaybackSpeed(1.15)
    output: list[object] = []

    async def collect(frame: object, direction: FrameDirection) -> None:
        output.append(frame)

    processor.push_frame = collect  # type: ignore[method-assign]
    processor.convert(TTSAudioRawFrame(tone(0.2), 24000, 1))
    marker = TTSTextFrame("完成", aggregated_by="sentence")
    await processor.process_frame(marker, FrameDirection.DOWNSTREAM)
    assert output[-1] is marker
    assert any(isinstance(frame, TTSAudioRawFrame) for frame in output[:-1])
    processor.convert(TTSAudioRawFrame(tone(0.2), 24000, 1))
    await processor.process_frame(InterruptionFrame(), FrameDirection.DOWNSTREAM)
    assert processor.convert(None) == []


def test_speed_not_applied_twice_or_sent_to_legacy_gpu() -> None:
    settings = AppSettings()
    settings.voice.playback_speed = 1.15
    assert "playback_speed" not in remote_snapshot(settings)["voice"]
    assert settings.public()["voice"]["playback_speed"] == 1.15
