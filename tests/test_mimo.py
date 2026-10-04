"""MiMo 协议通过真实 SDK、分段 STT 和可控 SSE 音频验证。"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import wave
from collections.abc import AsyncIterator
from typing import Any

import httpx2
import pytest
from pipecat.frames.frames import (
    EndFrame,
    ErrorFrame,
    Frame,
    InputAudioRawFrame,
    StartFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.workers.runner import WorkerRunner
from pydantic import SecretStr, ValidationError
from test_providers import wav_audio

from llmautotel.models import ASRSettings, TTSSettings
from llmautotel.providers import CompatibleSTTService, CompatibleTTSService


def asr_response(text: str) -> dict[str, Any]:
    return {
        "id": "fixture-asr",
        "object": "chat.completion",
        "created": 0,
        "model": "mimo-v2.5-asr",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}}],
    }


def sse_delta(delta: dict[str, Any] | None) -> bytes:
    chunk = {
        "id": "fixture-tts",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "mimo-v2.5-tts",
        "choices": (
            [{"index": 0, "delta": delta, "finish_reason": None}]
            if delta is not None
            else []
        ),
    }
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()


def sse_audio(audio: bytes) -> bytes:
    return sse_delta({"audio": {"data": base64.b64encode(audio).decode("ascii")}})


class SSEChunks(httpx2.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


class DelayedAudio(httpx2.AsyncByteStream):
    def __init__(self) -> None:
        self.waiting = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield sse_audio(b"\x01\x00")
        self.waiting.set()
        await self.release.wait()
        yield sse_audio(b"\x02\x00")
        yield b"data: [DONE]\n\n"

    async def aclose(self) -> None:
        self.closed = True


class TranscriptionCollector(FrameProcessor):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.received = asyncio.Event()
        self.transcripts: list[TranscriptionFrame] = []

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, StartFrame):
            self.started.set()
        elif isinstance(frame, TranscriptionFrame):
            self.transcripts.append(frame)
            self.received.set()
        await self.push_frame(frame, direction)


@pytest.mark.parametrize("language", ["auto", "zh", "en"])
async def test_mimo_asr_sdk_sends_chat_wav_data_url_and_configured_language(
    language: str,
) -> None:
    requests: list[httpx2.Request] = []
    bodies: list[dict[str, Any]] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        bodies.append(json.loads(await request.aread()))
        return httpx2.Response(200, json=asr_response(" 先说价格。 "))

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    service = CompatibleSTTService(
        ASRSettings(
            protocol="mimo",
            base_url="https://fixture.invalid/custom/v1",
            model="mimo-v2.5-asr",
            language=language,
            api_key=SecretStr("fixture-asr-key"),
            timeout_seconds=8,
        ),
        http_client=client,
    )
    try:
        frames = [frame async for frame in service.run_stt(wav_audio())]
        assert len(requests) == 1
        assert str(requests[0].url) == "https://fixture.invalid/custom/v1/chat/completions"
        assert requests[0].headers["authorization"] == "Bearer fixture-asr-key"
        assert requests[0].headers["content-type"] == "application/json"
        body = bodies[0]
        assert body["model"] == "mimo-v2.5-asr"
        assert body["stream"] is False
        assert body["asr_options"] == {"language": language}
        assert len(body["messages"]) == 1 and body["messages"][0]["role"] == "user"
        parts = body["messages"][0]["content"]
        assert len(parts) == 1 and parts[0]["type"] == "input_audio"
        prefix, encoded = parts[0]["input_audio"]["data"].split(",", 1)
        assert prefix == "data:audio/wav;base64"
        assert base64.b64decode(encoded, validate=True) == wav_audio()
        assert len(frames) == 1 and isinstance(frames[0], TranscriptionFrame)
        assert frames[0].text == "先说价格。"
        assert service._client.max_retries == 0
        assert service._client.timeout == 8
    finally:
        await service.aclose()
    assert client.is_closed


@pytest.mark.parametrize("text", ["我想了解计划。", " "])
async def test_mimo_real_segmented_stt_emits_final_even_when_empty(text: str) -> None:
    uploads: list[bytes] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(await request.aread())
        encoded = body["messages"][0]["content"][0]["input_audio"]["data"].split(",", 1)[1]
        uploads.append(base64.b64decode(encoded, validate=True))
        return httpx2.Response(200, json=asr_response(text))

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    service = CompatibleSTTService(
        ASRSettings(
            protocol="mimo", base_url="http://fixture.invalid/v1",
            model="mimo-v2.5-asr", language="auto",
        ),
        http_client=client,
    )
    collector = TranscriptionCollector()
    worker = PipelineWorker(
        Pipeline([service, collector]),
        enable_rtvi=False,
        enable_turn_tracking=False,
        params=PipelineParams(audio_in_sample_rate=16000),
        idle_timeout_secs=None,
    )
    runner = WorkerRunner(handle_sigint=False, handle_sigterm=False)
    await runner.add_workers(worker)
    task = asyncio.create_task(runner.run())
    try:
        await asyncio.wait_for(collector.started.wait(), timeout=3)
        await worker.queue_frames([
            VADUserStartedSpeakingFrame(),
            InputAudioRawFrame(b"\x01\x00" * 160, 16000, 1),
            VADUserStoppedSpeakingFrame(stop_secs=0.6),
        ])
        await asyncio.wait_for(collector.received.wait(), timeout=3)
        assert len(collector.transcripts) == len(uploads) == 1
        assert collector.transcripts[0].text == text.strip()
        assert collector.transcripts[0].finalized is True
        with wave.open(io.BytesIO(uploads[0]), "rb") as audio:
            assert (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) == (
                1, 2, 16000,
            )
            assert audio.readframes(160) == b"\x01\x00" * 160
        await worker.queue_frame(EndFrame())
        await asyncio.wait_for(task, timeout=3)
    finally:
        if not task.done():
            await worker.cancel()
            await asyncio.wait_for(task, timeout=3)
        await service.aclose()
    assert client.is_closed


@pytest.mark.parametrize("failure", ["http", "timeout", "malformed"])
async def test_mimo_asr_failure_is_safe_and_not_retried(failure: str) -> None:
    requests = 0
    secret = "fixture-upstream-secret"

    async def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal requests
        requests += 1
        if failure == "timeout":
            raise httpx2.ReadTimeout(secret, request=request)
        if failure == "malformed":
            return httpx2.Response(200, json={"choices": []})
        return httpx2.Response(401, json={"error": {"message": secret}})

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    service = CompatibleSTTService(
        ASRSettings(protocol="mimo", base_url="http://fixture.invalid/v1", model="mimo-v2.5-asr"),
        http_client=client,
    )
    try:
        frames = [frame async for frame in service.run_stt(wav_audio())]
        assert len(frames) == 1 and isinstance(frames[0], ErrorFrame)
        assert frames[0].error.startswith("asr:")
        assert secret not in frames[0].error
        assert requests == 1
        if failure == "http":
            assert "401" in frames[0].error
        elif failure == "timeout":
            assert "超时" in frames[0].error
    finally:
        await service.aclose()


async def test_mimo_tts_sdk_request_and_sse_pcm_alignment() -> None:
    requests: list[httpx2.Request] = []
    bodies: list[dict[str, Any]] = []
    stream = SSEChunks([
        sse_delta(None),
        sse_delta({"role": "assistant", "content": ""}),
        sse_delta({"audio": None}),
        sse_delta({"audio": {"id": "metadata-only"}}),
        sse_audio(b"\x01"),
        sse_audio(b"\x00\x02"),
        sse_audio(b"\x00"),
        b"data: [DONE]\n\n",
    ])

    async def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        bodies.append(json.loads(await request.aread()))
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    service = CompatibleTTSService(
        TTSSettings(
            protocol="mimo", base_url="https://fixture.invalid/custom/v1",
            model="mimo-v2.5-tts", voice="苏打", api_key=SecretStr("fixture-tts-key"),
        ),
        mimo_http_client=client,
    )
    try:
        frames = [frame async for frame in service.run_tts("您好，先说价格。", "sentence-1")]
        assert len(requests) == 1
        assert str(requests[0].url) == "https://fixture.invalid/custom/v1/chat/completions"
        assert requests[0].headers["authorization"] == "Bearer fixture-tts-key"
        assert requests[0].headers["content-type"] == "application/json"
        assert bodies[0] == {
            "model": "mimo-v2.5-tts",
            "messages": [{"role": "assistant", "content": "您好，先说价格。"}],
            "audio": {"format": "pcm16", "voice": "苏打"},
            "stream": True,
        }
        assert len(frames) == 2 and all(isinstance(frame, TTSAudioRawFrame) for frame in frames)
        assert [frame.audio for frame in frames] == [b"\x01\x00", b"\x02\x00"]
        assert all(frame.sample_rate == 24000 and frame.num_channels == 1 for frame in frames)
        assert all(frame.context_id == "sentence-1" for frame in frames)
        assert stream.closed
        assert service._mimo_client is not None and service._mimo_client.max_retries == 0
    finally:
        await service.aclose()
    assert client.is_closed


@pytest.mark.parametrize(
    "delta",
    [
        {"audio": {"data": "%%%fixture-secret%%%"}},
        {"audio": {"data": 42}},
        {"audio": "fixture-secret"},
        {"audio": {"data": ""}},
        {"audio": {"id": "metadata-only"}},
        {"audio": {"data": "AQ=="}},
    ],
)
async def test_mimo_tts_rejects_invalid_or_missing_pcm(delta: dict[str, Any]) -> None:
    stream = SSEChunks([sse_delta(delta), b"data: [DONE]\n\n"])
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(
        lambda request: httpx2.Response(
            200, headers={"content-type": "text/event-stream"}, stream=stream,
        )
    ))
    service = CompatibleTTSService(
        TTSSettings(protocol="mimo", base_url="http://fixture.invalid/v1", model="mimo-v2.5-tts"),
        mimo_http_client=client,
    )
    try:
        frames = [frame async for frame in service.run_tts("您好。", "invalid")]
        assert len(frames) == 1 and isinstance(frames[0], ErrorFrame)
        assert frames[0].error.startswith("tts:")
        assert "fixture-secret" not in frames[0].error
        assert stream.closed
    finally:
        await service.aclose()


@pytest.mark.parametrize("failure", ["http", "timeout", "sse"])
async def test_mimo_tts_failure_is_safe_closes_stream_and_not_retried(failure: str) -> None:
    requests = 0
    secret = "fixture-upstream-secret"
    stream = SSEChunks([f'data: {{"error": {{"message": "{secret}"}}}}\n\n'.encode()])

    async def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal requests
        requests += 1
        if failure == "timeout":
            raise httpx2.ReadTimeout(secret, request=request)
        if failure == "http":
            return httpx2.Response(401, json={"error": {"message": secret}})
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    service = CompatibleTTSService(
        TTSSettings(protocol="mimo", base_url="http://fixture.invalid/v1", model="mimo-v2.5-tts"),
        mimo_http_client=client,
    )
    try:
        frames = [frame async for frame in service.run_tts("您好。", "failure")]
        assert len(frames) == 1 and isinstance(frames[0], ErrorFrame)
        assert frames[0].error.startswith("tts:")
        assert secret not in frames[0].error
        assert requests == 1
        if failure == "sse":
            assert stream.closed
        elif failure == "http":
            assert "401" in frames[0].error
        else:
            assert "超时" in frames[0].error
    finally:
        await service.aclose()


async def test_mimo_tts_cancel_closes_sdk_stream_and_next_request_has_no_old_audio() -> None:
    stream = DelayedAudio()
    requests = 0

    async def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            return httpx2.Response(
                200, headers={"content-type": "text/event-stream"}, stream=stream,
            )
        return httpx2.Response(
            200, headers={"content-type": "text/event-stream"},
            content=sse_audio(b"\x03\x00") + b"data: [DONE]\n\n",
        )

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    service = CompatibleTTSService(
        TTSSettings(protocol="mimo", base_url="http://fixture.invalid/v1", model="mimo-v2.5-tts"),
        mimo_http_client=client,
    )
    old_frames: list[Frame] = []

    async def collect() -> None:
        async for frame in service.run_tts("旧句子。", "old"):
            old_frames.append(frame)

    task = asyncio.create_task(collect())
    try:
        await asyncio.wait_for(stream.waiting.wait(), timeout=3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        stream.release.set()
        assert stream.closed
        assert len(old_frames) == 1 and isinstance(old_frames[0], TTSAudioRawFrame)
        assert old_frames[0].audio == b"\x01\x00"
        new_frames = [frame async for frame in service.run_tts("新句子。", "new")]
        assert len(new_frames) == 1 and isinstance(new_frames[0], TTSAudioRawFrame)
        assert new_frames[0].audio == b"\x03\x00"
        assert new_frames[0].context_id == "new"
        assert len(old_frames) == 1 and requests == 2
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await service.aclose()
    assert client.is_closed


async def test_mimo_tts_consumer_close_at_audio_yield_closes_sdk_stream() -> None:
    stream = DelayedAudio()
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(
        lambda request: httpx2.Response(
            200, headers={"content-type": "text/event-stream"}, stream=stream,
        )
    ))
    service = CompatibleTTSService(
        TTSSettings(protocol="mimo", base_url="http://fixture.invalid/v1", model="mimo-v2.5-tts"),
        mimo_http_client=client,
    )
    generator = service.run_tts("旧句子。", "old")
    try:
        first = await anext(generator)
        assert isinstance(first, TTSAudioRawFrame) and first.audio == b"\x01\x00"
        await generator.aclose()
        assert stream.closed
        stream.release.set()
        with pytest.raises(StopAsyncIteration):
            await anext(generator)
    finally:
        await generator.aclose()
        await service.aclose()


@pytest.mark.parametrize("language", ["auto", "zh", "en"])
def test_mimo_asr_accepts_only_supported_languages(language: str) -> None:
    assert ASRSettings(protocol="mimo", language=language).language == language


@pytest.mark.parametrize("language", ["fr", "zh-CN", "unknown"])
def test_mimo_asr_rejects_unsupported_language(language: str) -> None:
    with pytest.raises(ValidationError, match="auto、zh 或 en"):
        ASRSettings(protocol="mimo", language=language)


def test_openai_asr_does_not_accept_mimo_auto_language() -> None:
    with pytest.raises(ValidationError, match="支持的语言代码"):
        ASRSettings(protocol="openai", language="auto")


@pytest.mark.parametrize("sample_rate", [8000, 16000, 48000])
def test_mimo_tts_rejects_rates_other_than_24khz(sample_rate: int) -> None:
    with pytest.raises(ValidationError, match="24000"):
        TTSSettings(protocol="mimo", sample_rate=sample_rate)


def test_mimo_24khz_default_preserves_openai_configurable_rate() -> None:
    assert TTSSettings(protocol="mimo").sample_rate == 24000
    assert TTSSettings(protocol="openai", sample_rate=16000).sample_rate == 16000
