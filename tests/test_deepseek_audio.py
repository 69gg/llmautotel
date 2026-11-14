"""DeepSeek 工具音频 API 的上传、PCM、独立配置和错误边界。"""

from __future__ import annotations

import io
import json
import wave
from pathlib import Path

import httpx
import httpx2
import pytest
from pipecat.frames.frames import ErrorFrame, TTSAudioRawFrame
from pydantic import SecretStr, ValidationError
from test_providers import ByteChunks, multipart_parts, wav_audio

from llmautotel.models import AppSettings, ASRSettings, TTSSettings
from llmautotel.providers import CompatibleSTTService, CompatibleTTSService, ProviderFailure
from llmautotel.store import Store


@pytest.mark.parametrize("text", ["支持导出。", " "])
async def test_deepseek_asr_only_sends_wav_file_and_authentication(text: str) -> None:
    async def handler(request: httpx2.Request) -> httpx2.Response:
        assert str(request.url) == "https://asr.test/custom/v1/tools/asr"
        assert request.headers["authorization"] == "Bearer asr-fixture"
        parts = multipart_parts(request.headers["content-type"], await request.aread())
        assert parts == {"file": wav_audio()}
        return httpx2.Response(200, json={"text": text, "duration_ms": 10, "request_id": "test"})

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    service = CompatibleSTTService(
        ASRSettings(
            protocol="deepseek",
            base_url="https://asr.test/custom/v1",
            model="ignored-old-model",
            language="auto",
            api_key=SecretStr("asr-fixture"),
        ),
        http_client=client,
    )
    try:
        frames = [frame async for frame in service.run_stt(wav_audio())]
        assert len(frames) == 1
        assert frames[0].text == text.strip()
        assert service._client.max_retries == 0
    finally:
        await service.aclose()
    assert client.is_closed


@pytest.mark.parametrize("failure", ["http", "size", "duration"])
async def test_deepseek_asr_failure_is_safe_and_limit_checks_precede_upload(failure: str) -> None:
    requests: list[httpx2.Request] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(402, text="upstream-fixture-secret")

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    service = CompatibleSTTService(
        ASRSettings(protocol="deepseek", base_url="https://asr.test/v1"),
        http_client=client,
    )
    audio = wav_audio()
    if failure == "size":
        audio = b"0" * (8 * 1024 * 1024 + 1)
    elif failure == "duration":
        output = io.BytesIO()
        with wave.open(output, "wb") as source:
            source.setnchannels(1)
            source.setsampwidth(2)
            source.setframerate(16000)
            source.writeframes(b"\0\0" * (16000 * 121))
        audio = output.getvalue()
    try:
        with pytest.raises(ProviderFailure) as caught:
            await service._transcribe(audio)
        assert "upstream-fixture-secret" not in str(caught.value)
        assert len(requests) == (1 if failure == "http" else 0)
        if failure == "http":
            assert "402" in str(caught.value)
    finally:
        await service.aclose()


@pytest.mark.parametrize("voice", ["", "stella", "tide"])
async def test_deepseek_http_tts_streams_pcm_and_omits_unsupported_fields(voice: str) -> None:
    stream = ByteChunks([b"\x01", b"\0\x02", b"\0"])

    async def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://tts.test/v1/tools/tts"
        assert request.headers["authorization"] == "Bearer tts-fixture"
        expected = {"text": "可以导出。", "format": "pcm"}
        if voice:
            expected["voice_id"] = voice
        assert json.loads(await request.aread()) == expected
        return httpx.Response(200, headers={"content-type": "audio/pcm"}, stream=stream)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = CompatibleTTSService(
        TTSSettings(
            protocol="deepseek",
            base_url="https://tts.test/v1",
            voice=voice,
            model="ignored-old-model",
            instructions="not-supported",
            api_key=SecretStr("tts-fixture"),
        ),
        http_client=client,
    )
    try:
        frames = [frame async for frame in service.run_tts("可以导出。", "test-context")]
        assert all(isinstance(frame, TTSAudioRawFrame) for frame in frames)
        assert b"".join(frame.audio for frame in frames) == b"\x01\0\x02\0"
        assert all(frame.sample_rate == 24000 and frame.num_channels == 1 for frame in frames)
        assert all(frame.context_id == "test-context" for frame in frames)
        assert stream.closed
    finally:
        await service.aclose()
    assert client.is_closed


@pytest.mark.parametrize("status", [400, 429, 503, 504])
async def test_deepseek_http_tts_errors_are_sanitized(status: int) -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status, text="fixture-secret-do-not-log")
        )
    )
    service = CompatibleTTSService(
        TTSSettings(protocol="deepseek", base_url="https://tts.test/v1"),
        http_client=client,
    )
    try:
        frames = [frame async for frame in service.run_tts("你好。", "test")]
        assert len(frames) == 1 and isinstance(frames[0], ErrorFrame)
        assert str(status) in frames[0].error
        assert "fixture-secret" not in frames[0].error
    finally:
        await service.aclose()


async def test_deepseek_configuration_needs_no_audio_model_and_persists_separately(
    tmp_path: Path,
) -> None:
    settings = AppSettings.model_validate(
        {
            "consultation": {"product_info": "支持导出。"},
            "asr": {
                "protocol": "deepseek",
                "base_url": "https://asr.test/v1",
                "api_key": "asr-key",
            },
            "tts": {
                "protocol": "deepseek",
                "base_url": "https://tts.test/v1",
                "api_key": "tts-key",
            },
            "llm": {"base_url": "https://llm.test", "model": "text-model", "thinking": "disabled"},
        }
    )
    assert settings.missing_call_fields() == []
    store = Store(tmp_path)
    await store.save_settings(settings)
    loaded = await Store(tmp_path).get_settings()
    assert loaded.asr.protocol == loaded.tts.protocol == "deepseek"
    assert loaded.llm.model == "text-model" and loaded.llm.thinking == "disabled"
    assert loaded.asr.api_key != loaded.tts.api_key
    public = json.dumps(loaded.public())
    assert "asr-key" not in public and "tts-key" not in public
    assert await store.list_calls() == []
    for stage in ("asr", "tts"):
        getattr(loaded, stage).protocol = "openai"
    assert "ASR 模型" in loaded.missing_call_fields()
    assert "TTS 模型" in loaded.missing_call_fields()
    assert "TTS 音色" in loaded.missing_call_fields()


def test_deepseek_audio_configuration_enforces_actual_output_format() -> None:
    with pytest.raises(ValidationError):
        TTSSettings(protocol="deepseek", sample_rate=16000)
    with pytest.raises(ValidationError):
        TTSSettings(protocol="deepseek", voice="unsupported-voice")
