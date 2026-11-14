"""真实 loopback WebSocket 验证双向输入、完成确认和打断后的连接隔离。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

import pytest
from pipecat.frames.frames import (
    ErrorFrame,
    Frame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    StartFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSSpeakFrame,
    TTSStoppedFrame,
    TTSTextFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.workers.runner import WorkerRunner
from pydantic import SecretStr
from test_voice import ControlledLLM, LocalTransport, Passthrough, Recorder
from websockets.asyncio.client import ClientConnection
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

import llmautotel.voice as voice_module
from llmautotel.deepseek_tts import DeepSeekStreamingTTSService
from llmautotel.models import AppSettings, TTSSettings, VoiceSettings
from llmautotel.providers import CompatibleTTSService, create_tts_service
from llmautotel.voice import VoiceSession


@asynccontextmanager
async def server(handler: Callable[[ServerConnection], Awaitable[None]]) -> AsyncIterator[str]:
    async with serve(handler, "127.0.0.1", 0) as listener:
        port = listener.sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}/custom/v1"


class Collector(FrameProcessor):
    def __init__(self) -> None:
        super().__init__()
        self.frames: list[Frame] = []
        self.started = asyncio.Event()
        self.audio = asyncio.Event()

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        self.frames.append(frame)
        if isinstance(frame, StartFrame):
            self.started.set()
        if isinstance(frame, TTSAudioRawFrame):
            self.audio.set()
        await self.push_frame(frame, direction)

    async def wait_for(self, kind: type[Frame], count: int = 1) -> None:
        async with asyncio.timeout(3):
            while sum(isinstance(frame, kind) for frame in self.frames) < count:
                await asyncio.sleep(0.01)


@asynccontextmanager
async def pipeline(
    url: str,
    voice: VoiceSettings | None = None,
) -> AsyncIterator[tuple[DeepSeekStreamingTTSService, PipelineWorker, Collector]]:
    service = DeepSeekStreamingTTSService(
        TTSSettings(
            protocol="deepseek",
            base_url=url,
            voice="tide",
            api_key=SecretStr("ws-fixture-key"),
        ),
        voice,
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
        await asyncio.wait_for(collector.started.wait(), 3)
        yield service, worker, collector
    finally:
        await worker.cancel()
        await asyncio.wait_for(task, 3)
        await service.aclose()


async def test_audio_arrives_before_sentence_or_llm_end_and_text_follows_completed_audio() -> None:
    inputs: list[list[str]] = []

    async def handler(socket: ServerConnection) -> None:
        assert socket.request is not None
        assert socket.request.headers["authorization"] == "Bearer ws-fixture-key"
        parsed = urlsplit(socket.request.path)
        assert parsed.path == "/custom/v1/tools/tts"
        assert parse_qs(parsed.query) == {"format": ["pcm"], "voice_id": ["tide"]}
        chunks: list[str] = []
        inputs.append(chunks)
        async for message in socket:
            event = json.loads(message)
            if event["event"] == "text":
                chunks.append(event["text"])
                await socket.send(b"\x01")
                await socket.send(b"\0")
            else:
                assert event == {"event": "finish_text"}
                await socket.send(b"\x02\0")
                await socket.send(json.dumps({"event": "finish", "code": "success"}))
                return

    async with server(handler) as url, pipeline(url) as (_, worker, output):
        await worker.queue_frames([LLMFullResponseStartFrame(), LLMTextFrame("它可以")])
        await asyncio.wait_for(output.audio.wait(), 3)
        assert not any(isinstance(frame, TTSTextFrame) for frame in output.frames)
        assert inputs == [["它可以"]]
        await worker.queue_frame(LLMTextFrame("导出文件。"))
        await output.wait_for(TTSTextFrame)
        assert not any(isinstance(frame, LLMFullResponseEndFrame) for frame in output.frames)
        await worker.queue_frames([LLMTextFrame("也能汇总。"), LLMFullResponseEndFrame()])
        await output.wait_for(LLMFullResponseEndFrame)
        texts = [frame for frame in output.frames if isinstance(frame, TTSTextFrame)]
        assert [frame.text for frame in texts] == ["它可以导出文件。", "也能汇总。"]
        assert len({frame.context_id for frame in texts}) == 2
        assert inputs == [["它可以", "导出文件。"], ["也能汇总。"]]
        for text in texts:
            frames = [
                frame
                for frame in output.frames
                if getattr(frame, "context_id", None) == text.context_id
            ]
            assert any(
                isinstance(frame, TTSAudioRawFrame) for frame in frames[: frames.index(text)]
            )
            assert isinstance(frames[-1], TTSStoppedFrame)


@pytest.mark.parametrize("chunking,segments", [("first_clause", 2), ("sentence", 1)])
async def test_streamed_first_clause_preserves_price_decimals_and_later_commas(
    chunking: str,
    segments: int,
) -> None:
    inputs: list[str] = []

    async def handler(socket: ServerConnection) -> None:
        text = ""
        async for message in socket:
            event = json.loads(message)
            if event["event"] == "text":
                text += event["text"]
                await socket.send(b"\x01\0")
            else:
                inputs.append(text)
                await socket.send(json.dumps({"event": "finish", "code": "success"}))
                return

    voice = VoiceSettings(response_chunking=chunking)
    text = "目前价格是19.01万元，还有贷款优惠，具体见资料。"
    async with server(handler) as url, pipeline(url, voice) as (_, worker, output):
        await worker.queue_frames(
            [LLMFullResponseStartFrame(), LLMTextFrame(text), LLMFullResponseEndFrame()]
        )
        await output.wait_for(LLMFullResponseEndFrame)
        assert len(inputs) == segments
        assert "".join(inputs) == text
        assert "19.01" in inputs[0]


async def test_interruption_discards_late_audio_and_does_not_commit_unfinished_text() -> None:
    release_old = asyncio.Event()
    old_closed = asyncio.Event()

    async def handler(socket: ServerConnection) -> None:
        is_old = False
        try:
            async for message in socket:
                event = json.loads(message)
                if event["event"] == "text":
                    if event["text"] == "旧草稿":
                        is_old = True
                        await socket.send(b"\x01\0")
                        await release_old.wait()
                        await socket.send(b"\x7f\x7f")
                    else:
                        await socket.send(b"\x02\0")
                else:
                    await socket.send(json.dumps({"event": "finish", "code": "success"}))
                    return
        except ConnectionClosed:
            pass
        finally:
            if is_old:
                old_closed.set()

    async with server(handler) as url, pipeline(url) as (_, worker, output):
        await worker.queue_frames([LLMFullResponseStartFrame(), LLMTextFrame("旧草稿")])
        await asyncio.wait_for(output.audio.wait(), 3)
        await worker.queue_frame(InterruptionFrame())
        await output.wait_for(InterruptionFrame)
        await worker.queue_frames(
            [LLMFullResponseStartFrame(), LLMTextFrame("新回复。"), LLMFullResponseEndFrame()]
        )
        release_old.set()
        await output.wait_for(LLMFullResponseEndFrame)
        assert [frame.text for frame in output.frames if isinstance(frame, TTSTextFrame)] == [
            "新回复。"
        ]
        interruption = next(
            i for i, frame in enumerate(output.frames) if isinstance(frame, InterruptionFrame)
        )
        audio = [
            frame.audio
            for frame in output.frames[interruption:]
            if isinstance(frame, TTSAudioRawFrame)
        ]
        assert audio and all(b"\x7f" not in chunk for chunk in audio)
        await asyncio.wait_for(old_closed.wait(), 3)


@pytest.mark.parametrize("failure", ["code", "json", "close", "odd", "empty"])
async def test_websocket_requires_success_and_complete_nonempty_pcm(failure: str) -> None:
    async def handler(socket: ServerConnection) -> None:
        await socket.recv()
        await socket.recv()
        if failure != "empty":
            await socket.send(b"\x01" if failure == "odd" else b"\x01\0")
        if failure == "close":
            return
        if failure == "json":
            await socket.send("fixture-secret-invalid-json")
        else:
            await socket.send(
                json.dumps(
                    {
                        "event": "finish",
                        "code": "service_error" if failure == "code" else "success",
                        "message": "fixture-secret",
                    }
                )
            )

    async with server(handler) as url:
        service = DeepSeekStreamingTTSService(TTSSettings(protocol="deepseek", base_url=url))
        try:
            frames = [frame async for frame in service.run_tts("你好。", "test")]
            errors = [frame for frame in frames if isinstance(frame, ErrorFrame)]
            assert len(errors) == 1
            assert errors[0].error.startswith("tts:")
            assert "fixture-secret" not in errors[0].error
        finally:
            await service.aclose()


async def test_tts_factory_keeps_old_http_and_selects_websocket_only_when_enabled() -> None:
    settings = AppSettings.model_validate(
        {"tts": {"protocol": "deepseek", "base_url": "http://tts.test/v1"}}
    )
    streamed = create_tts_service(settings)
    assert isinstance(streamed, DeepSeekStreamingTTSService)
    await streamed.aclose()
    settings.tts.streaming_input = False
    segmented = create_tts_service(settings)
    assert type(segmented) is CompatibleTTSService
    await segmented.aclose()


async def test_fixed_goodbye_commits_once_after_all_pcm_and_finish() -> None:
    release = asyncio.Event()
    goodbye = "好的，那先不打扰了。祝您生活愉快，再见。"
    inputs: list[str] = []

    async def handler(socket: ServerConnection) -> None:
        event = json.loads(await socket.recv())
        inputs.append(event["text"])
        assert json.loads(await socket.recv()) == {"event": "finish_text"}
        await socket.send(b"\x01\0")
        await release.wait()
        await socket.send(b"\x02\0")
        await socket.send(json.dumps({"event": "finish", "code": "success"}))

    async with server(handler) as url, pipeline(url) as (_, worker, output):
        try:
            await worker.queue_frame(TTSSpeakFrame(goodbye, append_to_context=True))
            await asyncio.wait_for(output.audio.wait(), 3)
            assert not any(isinstance(f, (TTSTextFrame, TTSStoppedFrame)) for f in output.frames)
            release.set()
            await output.wait_for(TTSStoppedFrame)
            assert inputs == [goodbye]
            assert [f.text for f in output.frames if isinstance(f, TTSTextFrame)] == [goodbye]
            pcm = [f.audio for f in output.frames if isinstance(f, TTSAudioRawFrame)]
            assert b"".join(pcm) == b"\x01\0\x02\0"
            assert isinstance(output.frames[-1], TTSStoppedFrame)
        finally:
            release.set()


async def test_hangup_while_synthesizing_closes_socket_and_clears_context() -> None:
    closed = asyncio.Event()

    async def handler(socket: ServerConnection) -> None:
        await socket.recv()
        await socket.send(b"\x01\0")
        await socket.wait_closed()
        closed.set()

    async with server(handler) as url:
        async with pipeline(url) as (service, worker, output):
            await worker.queue_frames([LLMFullResponseStartFrame(), LLMTextFrame("尚未完成")])
            await asyncio.wait_for(output.audio.wait(), 3)
        await asyncio.wait_for(closed.wait(), 3)
        assert service.get_audio_contexts() == []
        assert not any(isinstance(f, TTSTextFrame) for f in output.frames)
        assert not service._closing


async def test_repeated_interruptions_use_fresh_connections_and_only_commit_new_answer() -> None:
    connections: list[str] = []

    async def handler(socket: ServerConnection) -> None:
        try:
            async for message in socket:
                event = json.loads(message)
                if event["event"] == "text":
                    connections.append(event["text"])
                    await socket.send(b"\x01\0")
                else:
                    await socket.send(json.dumps({"event": "finish", "code": "success"}))
                    return
        except ConnectionClosed:
            pass

    async with server(handler) as url, pipeline(url) as (_, worker, output):
        for count, text in enumerate(("旧话题", "又一个旧话题"), start=1):
            await worker.queue_frames([LLMFullResponseStartFrame(), LLMTextFrame(text)])
            await output.wait_for(TTSAudioRawFrame, count)
            await worker.queue_frame(InterruptionFrame())
            await output.wait_for(InterruptionFrame, count)
        await worker.queue_frames(
            [LLMFullResponseStartFrame(), LLMTextFrame("只回答新问题。"), LLMFullResponseEndFrame()]
        )
        await output.wait_for(LLMFullResponseEndFrame)
        assert connections == ["旧话题", "又一个旧话题", "只回答新问题。"]
        assert [f.text for f in output.frames if isinstance(f, TTSTextFrame)] == ["只回答新问题。"]
        assert len({f.context_id for f in output.frames if isinstance(f, TTSTextFrame)}) == 1


async def test_connection_that_finishes_after_interruption_is_never_claimed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connecting, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def handler(socket: ServerConnection) -> None:
        await socket.wait_closed()
        closed.set()

    async with server(handler) as url:
        service = DeepSeekStreamingTTSService(TTSSettings(protocol="deepseek", base_url=url))
        connect_original = service._connect

        async def delayed_connect() -> ClientConnection:
            socket = await connect_original()
            connecting.set()
            await release.wait()
            return socket

        monkeypatch.setattr(service, "_connect", delayed_connect)
        request = asyncio.create_task(service._feed("旧文字", True, False))
        try:
            await asyncio.wait_for(connecting.wait(), 3)
            service._abandon()
            release.set()
            await asyncio.wait_for(request, 3)
            await asyncio.wait_for(closed.wait(), 3)
            assert service._segment is None
            assert service.get_audio_contexts() == []
        finally:
            release.set()
            await service.aclose()


async def test_silent_websocket_times_out_without_committing_text() -> None:
    async def handler(socket: ServerConnection) -> None:
        await socket.wait_closed()

    async with server(handler) as url:
        settings = TTSSettings(protocol="deepseek", base_url=url, timeout_seconds=1)
        service = DeepSeekStreamingTTSService(settings)
        try:
            frames = [f async for f in service.run_tts("你好。", "timeout")]
            assert len(frames) == 1 and isinstance(frames[0], ErrorFrame)
            assert "tts:" in frames[0].error
        finally:
            await service.aclose()


@dataclass
class StreamingServices:
    stt: Passthrough
    llm: ControlledLLM
    tts: DeepSeekStreamingTTSService

    async def aclose(self) -> None:
        await self.tts.aclose()


@pytest.mark.parametrize("chunking", ["first_clause", "sentence"])
async def test_full_voice_pipeline_streams_unfinished_opening_and_answers_new_question(
    monkeypatch: pytest.MonkeyPatch, chunking: str
) -> None:
    async def handler(socket: ServerConnection) -> None:
        try:
            async for message in socket:
                event = json.loads(message)
                if event["event"] == "text":
                    await socket.send(b"\x01\0" * 960)
                else:
                    await socket.send(json.dumps({"event": "finish", "code": "success"}))
                    return
        except ConnectionClosed:
            pass

    async with server(handler) as url:
        settings = AppSettings.model_validate(
            {
                "tts": {"protocol": "deepseek", "base_url": url},
                "voice": {"response_chunking": chunking},
            }
        )
        recorder = Recorder()
        transport = LocalTransport(recorder)
        services = StreamingServices(
            Passthrough(),
            ControlledLLM(block_first=True, unspoken_first=True),
            DeepSeekStreamingTTSService(settings.tts, settings.voice),
        )
        monkeypatch.setattr(voice_module, "create_services", lambda settings: services)
        session = VoiceSession(None, settings, recorder.callbacks(), transport=transport)
        task = asyncio.create_task(session.run())
        try:
            await asyncio.wait_for(transport.outgoing.started.wait(), 3)
            assert session._worker is not None
            # 注入的原生传输通过连接事件开场；不使用浏览器 ready 事件。
            await session._open_conversation(transport)
            await asyncio.wait_for(transport.outgoing.audio_written.wait(), 3)
            assert recorder.messages == []  # 未完成的生成稿还不能作为已播放的文字。
            assert not services.llm.cancelled.is_set()
            await session._worker.queue_frame(VADUserStartedSpeakingFrame())
            await asyncio.wait_for(services.llm.cancelled.wait(), 3)
            await session._worker.queue_frames(
                [
                    VADUserStoppedSpeakingFrame(stop_secs=0.3),
                    TranscriptionFrame("现在多少钱？", "user", "latest", finalized=True),
                ]
            )
            async with asyncio.timeout(3):
                while not any(m.text == "新的回答。" for m in recorder.messages):
                    recorder.changed.clear()
                    await recorder.changed.wait()
            assert recorder.errors == []
            assert services.llm.inputs[-1][-1]["content"] == "现在多少钱？"
            assert any(
                m.get("role") == "system" and "尚未完整生成的开场" in m.get("content", "")
                for m in services.llm.inputs[-1]
            )
            assert not any(m.text == "这个尚未完整生成的开场" for m in recorder.messages)
        finally:
            await session.stop()
            await asyncio.wait_for(task, 3)
