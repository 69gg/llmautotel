"""验证基准不把静音计为回复、不写配置，并始终清理会话。"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest
from openai.types.audio import Transcription
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import ErrorFrame, Frame
from test_voice import ControlledLLM, ControlledTTS, FixtureServices

import llmautotel.benchmark as benchmark_module
import llmautotel.voice as voice_module
from llmautotel.benchmark import (
    BenchmarkFailure,
    SampleDriver,
    measure_sample,
    pcm_rms,
    read_settings,
    synthesize_question,
    trim_trailing_silence,
)
from llmautotel.models import AppSettings
from llmautotel.providers import CompatibleSTTService
from llmautotel.store import Store
from llmautotel.telephony.base import MediaCallbacks


def test_silence_and_incomplete_samples_cannot_be_reported_as_effective_audio() -> None:
    assert pcm_rms(b"\0" * 960) == 0
    assert pcm_rms((300).to_bytes(2, "little") * 480) == 300
    with pytest.raises(BenchmarkFailure, match="invalid_pcm"):
        pcm_rms(b"\0")
    with pytest.raises(BenchmarkFailure, match="silent_question_audio"):
        trim_trailing_silence(b"\0" * 960, 24000, 300)


def test_question_anchor_uses_last_active_block_instead_of_tts_trailing_silence() -> None:
    quiet = (100).to_bytes(2, "little") * 480
    speech = (3000).to_bytes(2, "little") * 480
    assert trim_trailing_silence(quiet + speech + quiet * 20, 24000, 300) == quiet + speech
    # 尾部不足一帧时，补零不丢失最后的语音采样。
    partial = speech[:200]
    assert trim_trailing_silence(partial, 24000, 300) == partial.ljust(960, b"\0")


async def test_driver_excludes_greeting_and_leading_silence_and_records_first_audio_once() -> None:
    async def noop(*args: Any) -> None:
        pass

    driver = SampleDriver(MediaCallbacks(noop, noop, noop, noop), 24000, 300)
    speech = (3000).to_bytes(2, "little") * 480
    await driver.send_audio(speech)
    assert driver.first_audio_at is None
    driver.armed = True
    await driver.send_audio(b"\0" * 960)
    assert driver.first_audio_at is None
    await driver.send_audio(speech)
    first = driver.first_audio_at
    assert first is not None
    await driver.send_audio(speech)
    assert driver.first_audio_at == first


async def test_configuration_and_history_are_read_without_mutation(tmp_path: Path) -> None:
    store = Store(tmp_path)
    settings = AppSettings.model_validate({"asr": {"api_key": "fixture-secret"}})
    await store.save_settings(settings)
    before = hashlib.sha256(store.path.read_bytes()).digest()
    loaded = read_settings(tmp_path)
    assert loaded.asr.api_key is not None
    assert loaded.asr.api_key.get_secret_value() == "fixture-secret"
    loaded.voice.vad_stop_seconds = 0.25
    assert hashlib.sha256(store.path.read_bytes()).digest() == before
    assert (await store.get_settings()).voice.vad_stop_seconds == 0.3
    assert await store.list_calls() == []


async def test_preparation_failure_is_sanitized_and_closes_the_model_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[bool] = []

    class FailedTTS:
        def __init__(self, settings: object) -> None:
            pass

        async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame, None]:
            yield ErrorFrame(error="upstream body contains fixture-secret")

        async def aclose(self) -> None:
            closed.append(True)

    monkeypatch.setattr(benchmark_module, "create_tts_service", FailedTTS)
    with pytest.raises(BenchmarkFailure, match="^question_synthesis_failed$"):
        await synthesize_question(AppSettings(), "功能是什么？", 300)
    assert closed == [True]


async def test_timed_out_sample_cancels_and_joins_its_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stopped = asyncio.Event()

    class SilentSession:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def run(self) -> str:
            await stopped.wait()
            return "benchmark_complete"

        async def stop(self, reason: str) -> None:
            stopped.set()

    monkeypatch.setattr(benchmark_module, "VoiceSession", SilentSession)
    with pytest.raises(TimeoutError):
        await measure_sample(AppSettings(), b"\0" * 960, sequence=1, threshold=300, timeout=0.02)
    assert stopped.is_set()


async def test_benchmark_runs_real_media_queues_and_counts_endpointing_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FixtureVAD(SileroVADAnalyzer):
        def __init__(self, *, params: VADParams) -> None:
            params.min_volume = 0
            super().__init__(params=params)

        def voice_confidence(self, buffer: bytes) -> float:
            return 1.0 if pcm_rms(buffer) > 300 else 0.0

    class FixtureSTT(CompatibleSTTService):
        async def _transcribe(self, audio: bytes) -> Transcription:
            return Transcription(text="支持导出吗？")

    settings = AppSettings.model_validate({"consultation": {"opening": "您好，请讲。"}})
    services = FixtureServices(
        FixtureSTT(settings.asr), ControlledLLM(block_first=False), ControlledTTS()
    )
    monkeypatch.setattr(voice_module, "SileroVADAnalyzer", FixtureVAD)
    monkeypatch.setattr(voice_module, "create_services", lambda settings: services)
    result = await measure_sample(
        settings,
        (3000).to_bytes(2, "little") * 24000,
        sequence=1,
        threshold=1,
        timeout=5,
    )
    assert result.user_text == "支持导出吗？"
    assert result.assistant_text == "新的回答。"
    assert 250 <= result.effective_audio_ms < 1000
    assert result.within_target
    assert result.server_latency is not None
    assert result.server_latency["kind"] == "reply"
    assert any(part["stage"] == "endpointing_wait" for part in result.server_latency["parts"])
    assert services.closed
