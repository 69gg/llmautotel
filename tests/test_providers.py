"""Provider protocol tests exercise actual SDK encoding and streamed HTTP bytes."""

from __future__ import annotations

import asyncio
import io
import json
import wave
from collections.abc import AsyncIterator
from email import policy
from email.parser import BytesParser
from typing import Any

import httpx
import httpx2
import pytest
from pipecat.frames.frames import (
    AggregatedTextFrame,
    EndFrame,
    ErrorFrame,
    Frame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSStoppedFrame,
    TTSTextFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.utils.text.base_text_aggregator import AggregationType
from pipecat.workers.runner import WorkerRunner
from pydantic import SecretStr

from llmautotel.models import AppSettings, ASRSettings, LLMSettings, TTSSettings
from llmautotel.providers import (
    CompatibleLLMService,
    CompatibleSTTService,
    CompatibleTTSService,
    ProviderFailure,
    create_services,
)


def wav_audio() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\x01\x00" * 160)
    return buffer.getvalue()


def multipart_parts(content_type: str, body: bytes) -> dict[str, bytes]:
    message = BytesParser(policy=policy.default).parsebytes(
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + body
    )
    return {
        str(part.get_param("name", header="content-disposition")): part.get_payload(decode=True)
        for part in message.iter_parts()
    }


class ByteChunks(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


class DelayedPCM(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.waiting = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"\x01\x00"
        self.waiting.set()
        await self.release.wait()
        yield b"\x02\x00"

    async def aclose(self) -> None:
        self.closed = True


def sse_text(text: str) -> bytes:
    chunk = {
        "id": "reply-1",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "custom",
        "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
    }
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()


class DelayedSSE(httpx2.AsyncByteStream):
    def __init__(self, *, fail: bool = False) -> None:
        self.waiting = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = False
        self.fail = fail

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield sse_text("已生成。")
        self.waiting.set()
        await self.release.wait()
        if self.fail:
            raise httpx2.ReadTimeout("secret-must-not-leak")
        yield sse_text("迟到的旧文本。")

    async def aclose(self) -> None:
        self.closed = True


async def test_asr_sends_real_wav_multipart_to_independent_endpoint() -> None:
    requests: list[httpx2.Request] = []
    uploaded: dict[str, bytes] = {}

    async def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        uploaded.update(multipart_parts(request.headers["content-type"], await request.aread()))
        return httpx2.Response(200, json={"text": " 我想了解订阅。 "})

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    service = CompatibleSTTService(
        ASRSettings(
            base_url="https://asr.invalid/custom/v1",
            model="local-asr",
            api_key=SecretStr("asr-secret"),
            language="zh",
            timeout_seconds=9,
        ),
        http_client=client,
    )
    try:
        frames = [frame async for frame in service.run_stt(wav_audio())]
        assert len(requests) == 1
        assert str(requests[0].url) == "https://asr.invalid/custom/v1/audio/transcriptions"
        assert requests[0].headers["authorization"] == "Bearer asr-secret"
        assert uploaded["model"] == b"local-asr"
        assert uploaded["language"] == b"zh"
        assert uploaded["file"] == wav_audio()
        with wave.open(io.BytesIO(uploaded["file"]), "rb") as audio:
            assert (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) == (
                1,
                2,
                16000,
            )
        assert len(frames) == 1
        assert isinstance(frames[0], TranscriptionFrame)
        assert frames[0].text == "我想了解订阅。"
        assert service._client.max_retries == 0
        assert service._client.timeout == 9
    finally:
        await service.aclose()


async def test_empty_asr_text_is_forwarded_for_resume_listening() -> None:
    client = httpx2.AsyncClient(
        transport=httpx2.MockTransport(lambda request: httpx2.Response(200, json={"text": " "}))
    )
    service = CompatibleSTTService(
        ASRSettings(base_url="http://localhost:9001/v1", model="empty-asr"), http_client=client
    )
    try:
        frames = [frame async for frame in service.run_stt(wav_audio())]
        assert len(frames) == 1
        assert isinstance(frames[0], TranscriptionFrame)
        assert frames[0].text == ""
    finally:
        await service.aclose()


async def test_llm_consumes_actual_sse_with_correct_model_messages_and_key() -> None:
    requests: list[httpx2.Request] = []
    bodies: list[dict[str, Any]] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        bodies.append(json.loads(await request.aread()))
        events = []
        for text in ("您好，", "我们来聊聊。"):
            chunk = {
                "id": "reply-1",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "custom-text",
                "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
            }
            events.append(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n")
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=("".join(events) + "data: [DONE]\n\n").encode(),
        )

    service = CompatibleLLMService(
        LLMSettings(
            base_url="https://llm.invalid/compatible/v1",
            model="custom-text",
            api_key=SecretStr("text-secret"),
            timeout_seconds=12,
        ),
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )
    context = LLMContext(messages=[{"role": "user", "content": "有什么功能？"}])
    try:
        stream = await service.get_chat_completions(context)
        try:
            text = "".join([chunk.choices[0].delta.content or "" async for chunk in stream])
        finally:
            await stream.close()
        assert text == "您好，我们来聊聊。"
        assert str(requests[0].url) == "https://llm.invalid/compatible/v1/chat/completions"
        assert requests[0].headers["authorization"] == "Bearer text-secret"
        assert bodies[0]["model"] == "custom-text"
        assert bodies[0]["stream"] is True
        assert bodies[0]["messages"] == [{"role": "user", "content": "有什么功能？"}]
        assert service._client.max_retries == 0
        assert service._client.timeout == 12
    finally:
        await service.aclose()


async def test_tts_custom_voice_pcm_alignment_and_configured_sample_rate() -> None:
    bodies: list[dict[str, Any]] = []
    requests: list[httpx.Request] = []
    audio_stream = ByteChunks([b"\x01", b"\x00\x02", b"\x00\x03\x00"])

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        bodies.append(json.loads(await request.aread()))
        return httpx.Response(200, stream=audio_stream)

    service = CompatibleTTSService(
        TTSSettings(
            base_url="https://tts.invalid/vendor/v1",
            model="vendor-tts",
            voice="my-chinese-voice",
            sample_rate=22050,
            api_key=SecretStr("tts-secret"),
        ),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    try:
        frames = [frame async for frame in service.run_tts("你好。", "sentence-1")]
        assert str(requests[0].url) == "https://tts.invalid/vendor/v1/audio/speech"
        assert requests[0].headers["authorization"] == "Bearer tts-secret"
        assert bodies == [
            {
                "input": "你好。",
                "model": "vendor-tts",
                "voice": "my-chinese-voice",
                "response_format": "pcm",
            }
        ]
        assert all(isinstance(frame, TTSAudioRawFrame) for frame in frames)
        assert b"".join(frame.audio for frame in frames) == b"\x01\x00\x02\x00\x03\x00"
        assert all(frame.sample_rate == 22050 and frame.num_channels == 1 for frame in frames)
        assert all(frame.context_id == "sentence-1" for frame in frames)
        assert audio_stream.closed
    finally:
        await service.aclose()


async def test_two_tts_sentences_finish_promptly_with_audio_text_then_stop() -> None:
    class Collector(FrameProcessor):
        def __init__(self) -> None:
            super().__init__()
            self.frames: list[Frame] = []
            self.completed = asyncio.Event()

        async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
            await super().process_frame(frame, direction)
            self.frames.append(frame)
            if sum(isinstance(item, TTSStoppedFrame) for item in self.frames) == 2:
                self.completed.set()
            await self.push_frame(frame, direction)

    service = CompatibleTTSService(
        TTSSettings(base_url="http://localhost:9003/v1", model="tts", voice="custom"),
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=b"\x01\x00" * 960)
            )
        ),
    )
    collector = Collector()
    worker = PipelineWorker(
        Pipeline([service, collector]),
        enable_rtvi=False,
        enable_turn_tracking=False,
        params=PipelineParams(audio_out_sample_rate=24000),
        idle_timeout_secs=None,
    )
    runner = WorkerRunner(handle_sigint=False, handle_sigterm=False)
    await runner.add_workers(worker)
    task = asyncio.create_task(runner.run())
    try:
        await worker.queue_frames(
            [
                LLMFullResponseStartFrame(),
                AggregatedTextFrame("第一句。", AggregationType.SENTENCE),
                AggregatedTextFrame("第二句。", AggregationType.SENTENCE),
                LLMFullResponseEndFrame(),
            ]
        )
        # The stock per-context watchdog is 3 s. A finished HTTP sentence
        # must not hold up the next sentence or its playback completion.
        await asyncio.wait_for(collector.completed.wait(), timeout=1)
        meaningful = [
            frame
            for frame in collector.frames
            if isinstance(frame, (TTSAudioRawFrame, TTSTextFrame, TTSStoppedFrame))
        ]
        assert [type(frame) for frame in meaningful] == [
            TTSAudioRawFrame,
            TTSTextFrame,
            TTSStoppedFrame,
            TTSAudioRawFrame,
            TTSTextFrame,
            TTSStoppedFrame,
        ]
        contexts = [frame.context_id for frame in meaningful]
        assert contexts[0] == contexts[1] == contexts[2]
        assert contexts[3] == contexts[4] == contexts[5]
        assert contexts[0] != contexts[3]
        await worker.queue_frame(EndFrame())
        await asyncio.wait_for(task, timeout=2)
    finally:
        if not task.done():
            await worker.cancel()
            await asyncio.wait_for(task, timeout=2)
        await service.aclose()


@pytest.mark.parametrize("stage", ["asr", "llm", "tts"])
async def test_provider_errors_do_not_expose_response_body_or_key_and_do_not_retry(
    stage: str,
) -> None:
    requests: list[Any] = []
    secret = "do-not-leak-secret"

    async def sdk_handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(401, json={"error": {"message": secret}})

    async def tts_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(401, json={"error": {"message": secret}})

    base: dict[str, Any] = {
        "base_url": "https://error.invalid/v1",
        "model": "error-model",
        "api_key": SecretStr(secret),
    }
    if stage == "asr":
        asr = CompatibleSTTService(
            ASRSettings(**base),
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(sdk_handler)),
        )
        try:
            frames = [frame async for frame in asr.run_stt(wav_audio())]
            assert len(frames) == 1 and isinstance(frames[0], ErrorFrame)
            message = frames[0].error
        finally:
            await asr.aclose()
    elif stage == "llm":
        llm = CompatibleLLMService(
            LLMSettings(**base),
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(sdk_handler)),
        )
        try:
            with pytest.raises(ProviderFailure) as failure:
                await llm.get_chat_completions(LLMContext(messages=[]))
            message = str(failure.value)
        finally:
            await llm.aclose()
    else:
        tts = CompatibleTTSService(
            TTSSettings(**base, voice="custom"),
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(tts_handler)),
        )
        try:
            frames = [frame async for frame in tts.run_tts("您好。", "error-sentence")]
            assert len(frames) == 1 and isinstance(frames[0], ErrorFrame)
            message = frames[0].error
        finally:
            await tts.aclose()
    assert len(requests) == 1
    assert message.startswith(f"{stage}:")
    assert "401" in message
    assert secret not in message


async def test_tts_cancel_closes_stream_and_new_turn_has_no_old_audio() -> None:
    stream = DelayedPCM()
    requests = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return (
            httpx.Response(200, stream=stream)
            if requests == 1
            else httpx.Response(200, content=b"\x03\x00")
        )

    service = CompatibleTTSService(
        TTSSettings(base_url="http://localhost:9002/v1", model="tts", voice="custom"),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    old_audio: list[Frame] = []

    async def collect() -> None:
        async for frame in service.run_tts("旧回复。", "old-turn"):
            old_audio.append(frame)

    task = asyncio.create_task(collect())
    try:
        await asyncio.wait_for(stream.waiting.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        stream.release.set()
        assert stream.closed
        assert len(old_audio) == 1
        assert old_audio[0].audio == b"\x01\x00"
        new_audio = [frame async for frame in service.run_tts("新回复。", "new-turn")]
        assert len(new_audio) == 1
        assert new_audio[0].audio == b"\x03\x00"
        assert new_audio[0].context_id == "new-turn"
    finally:
        await service.aclose()


async def test_llm_pipeline_cancellation_closes_sse_and_discards_late_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = DelayedSSE()
    responses = 0

    async def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal responses
        responses += 1
        if responses == 1:
            return httpx2.Response(
                200, headers={"content-type": "text/event-stream"}, stream=stream
            )
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse_text("新回答。") + b"data: [DONE]\n\n",
        )

    service = CompatibleLLMService(
        LLMSettings(base_url="http://localhost:9002/v1", model="custom"),
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )
    output: list[Frame] = []

    async def capture(frame: Frame, direction: FrameDirection = FrameDirection.DOWNSTREAM) -> None:
        output.append(frame)

    monkeypatch.setattr(service, "push_frame", capture)
    task = asyncio.create_task(service._process_context(LLMContext(messages=[])))
    try:
        await asyncio.wait_for(stream.waiting.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        stream.release.set()
        assert stream.closed
        await service._process_context(
            LLMContext(messages=[{"role": "user", "content": "换个问题"}])
        )
        assert [frame.text for frame in output if isinstance(frame, LLMTextFrame)] == [
            "已生成。",
            "新回答。",
        ]
    finally:
        await service.aclose()


async def test_llm_pipeline_stream_failure_is_sanitized_and_closes_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = DelayedSSE(fail=True)
    stream.release.set()
    service = CompatibleLLMService(
        LLMSettings(base_url="http://localhost:9002/v1", model="custom"),
        http_client=httpx2.AsyncClient(
            transport=httpx2.MockTransport(
                lambda request: httpx2.Response(
                    200, headers={"content-type": "text/event-stream"}, stream=stream
                )
            )
        ),
    )

    async def capture(frame: Frame, direction: FrameDirection = FrameDirection.DOWNSTREAM) -> None:
        pass

    monkeypatch.setattr(service, "push_frame", capture)
    try:
        with pytest.raises(ProviderFailure) as failure:
            await service._process_context(LLMContext(messages=[]))
        assert str(failure.value).startswith("llm:")
        assert "secret-must-not-leak" not in str(failure.value)
        assert stream.closed
    finally:
        await service.aclose()


async def test_tts_timeout_has_safe_stage_and_no_retry() -> None:
    requests = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        raise httpx.ReadTimeout("secret-must-not-leak", request=request)

    service = CompatibleTTSService(
        TTSSettings(base_url="http://localhost:9003/v1", model="tts", voice="custom"),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    try:
        frames = [frame async for frame in service.run_tts("您好。", "timeout")]
        assert len(frames) == 1 and isinstance(frames[0], ErrorFrame)
        assert frames[0].error == "tts: 模型请求失败（超时）"
        assert requests == 1
    finally:
        await service.aclose()


async def test_tts_rejects_incomplete_pcm_sample() -> None:
    service = CompatibleTTSService(
        TTSSettings(base_url="http://localhost:9003/v1", model="tts", voice="custom"),
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"\x01"))
        ),
    )
    try:
        frames = [frame async for frame in service.run_tts("您好。", "invalid-pcm")]
        assert len(frames) == 1 and isinstance(frames[0], ErrorFrame)
        assert frames[0].error.startswith("tts:")
    finally:
        await service.aclose()


@pytest.mark.parametrize("media_type", ["audio/mpeg", "audio/wav", "application/json"])
async def test_tts_rejects_non_pcm_response(media_type: str) -> None:
    stream = ByteChunks([b"non-pcm-content"])
    service = CompatibleTTSService(
        TTSSettings(base_url="http://localhost:9003/v1", model="tts", voice="custom"),
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, headers={"content-type": media_type}, stream=stream
                )
            )
        ),
    )
    try:
        frames = [frame async for frame in service.run_tts("您好。", "wrong-format")]
        assert len(frames) == 1 and isinstance(frames[0], ErrorFrame)
        assert frames[0].error.startswith("tts:")
        assert stream.closed
    finally:
        await service.aclose()


@pytest.mark.parametrize("stage", ["asr", "llm", "tts"])
async def test_self_hosted_endpoints_do_not_inherit_environment_key(
    stage: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "environment-secret")
    captured_headers: list[Any] = []

    async def sdk_handler(request: httpx2.Request) -> httpx2.Response:
        captured_headers.append(request.headers)
        if stage == "asr":
            return httpx2.Response(200, json={"text": "你好"})
        return httpx2.Response(
            200, headers={"content-type": "text/event-stream"}, content=b"data: [DONE]\n\n"
        )

    async def tts_handler(request: httpx.Request) -> httpx.Response:
        captured_headers.append(request.headers)
        return httpx.Response(200, content=b"\x00\x00")

    if stage == "asr":
        asr = CompatibleSTTService(
            ASRSettings(base_url="http://localhost:9001/v1", model="local"),
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(sdk_handler)),
        )
        try:
            assert [frame async for frame in asr.run_stt(wav_audio())]
        finally:
            await asr.aclose()
    elif stage == "llm":
        llm = CompatibleLLMService(
            LLMSettings(base_url="http://localhost:9002/v1", model="local"),
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(sdk_handler)),
        )
        try:
            stream = await llm.get_chat_completions(LLMContext(messages=[]))
            assert [chunk async for chunk in stream] == []
            await stream.close()
        finally:
            await llm.aclose()
    else:
        tts = CompatibleTTSService(
            TTSSettings(base_url="http://localhost:9003/v1", model="local", voice="custom"),
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(tts_handler)),
        )
        try:
            assert [frame async for frame in tts.run_tts("您好。", "local")]
        finally:
            await tts.aclose()
    assert len(captured_headers) == 1
    assert "authorization" not in captured_headers[0]


async def test_factory_builds_three_independent_clients() -> None:
    services = create_services(
        AppSettings(
            asr=ASRSettings(base_url="http://localhost:9001/v1", model="a"),
            llm=LLMSettings(base_url="http://localhost:9002/v1", model="l"),
            tts=TTSSettings(base_url="http://localhost:9003/v1", model="t", voice="v"),
        )
    )
    try:
        assert str(services.stt._client.base_url) == "http://localhost:9001/v1/"
        assert str(services.llm._client.base_url) == "http://localhost:9002/v1/"
        assert services.tts._endpoint == "http://localhost:9003/v1/audio/speech"
        assert services.stt._client._client is not services.llm._client._client
    finally:
        await services.aclose()
    assert services.stt._client.is_closed()
    assert services.llm._client.is_closed()
    assert services.tts._http_client.is_closed
