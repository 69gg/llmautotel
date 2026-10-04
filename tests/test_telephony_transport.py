"""真实 Pipecat 队列和 PCM 重采样，电话对端通过可控 driver 观测。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import httpx2
import pytest
from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    InterruptionFrame,
    LLMAssistantPushAggregationFrame,
    LLMFullResponseEndFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSTextFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.workers.runner import WorkerRunner
from test_hangup import FOLLOW_UP, GOODBYE, NEW_ANSWER, USER_END, GoodbyeTTS, tool_response
from test_interruption_context import SDKServices
from test_providers import sse_text
from test_voice import ControlledLLM, ControlledTTS, FixtureServices, Passthrough, Recorder

import llmautotel.voice as voice_module
from llmautotel.hangup import HangupAfterPlaybackFrame
from llmautotel.models import AppSettings, LLMSettings
from llmautotel.providers import CompatibleLLMService
from llmautotel.telephony.base import MediaCallbacks
from llmautotel.telephony.transport import PhoneTransport
from llmautotel.voice import VoiceSession


class ControlledDriver:
    sample_rate = 8000

    def __init__(self, callbacks: MediaCallbacks, *, auto_answer: bool = True) -> None:
        self.callbacks = callbacks
        self.auto_answer = auto_answer
        self.started = asyncio.Event()
        self.connected = asyncio.Event()
        self.waiting = asyncio.Event()
        self.played = asyncio.Event()
        self.played.set()
        self.closed = False
        self.flush_count = 0
        self.hangup_count = 0
        self.close_count = 0
        self.wait_cancelled = 0
        self._confirmed_count = 0
        self._pending_waits: set[asyncio.Task[bool]] = set()
        self.audio: list[bytes] = []
        self.times: list[float] = []
        self.target: tuple[str, str] | None = None
        self.start_error: Exception | None = None
        self.hangup_error: Exception | None = None
        self.block_start = False
        self.start_cancelled = asyncio.Event()

    async def start(self, number: str, call_id: str) -> None:
        self.target = number, call_id
        self.started.set()
        try:
            if self.block_start:
                await asyncio.Event().wait()
            if self.start_error is not None:
                raise self.start_error
            if self.auto_answer:
                await self.answer()
        except asyncio.CancelledError:
            self.start_cancelled.set()
            raise

    async def answer(self) -> None:
        await self.callbacks.on_ready()
        self.connected.set()

    async def send_audio(self, audio: bytes) -> None:
        assert not self.closed
        self.audio.append(audio)
        self.times.append(asyncio.get_running_loop().time())

    async def wait_played(self) -> None:
        count = len(self.audio)
        if count <= self._confirmed_count:
            return
        self.waiting.set()
        wait = asyncio.create_task(self.played.wait())
        self._pending_waits.add(wait)
        try:
            await wait
            self._confirmed_count = count
        except asyncio.CancelledError:
            self.wait_cancelled += 1
            raise
        finally:
            self._pending_waits.discard(wait)

    async def flush(self) -> None:
        self.flush_count += 1
        self._confirmed_count = len(self.audio)
        for wait in self._pending_waits:
            wait.cancel()

    async def hangup(self) -> None:
        self.hangup_count += 1
        if self.hangup_error is not None:
            raise self.hangup_error

    async def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.close_count += 1


class FrameLog(FrameProcessor):
    def __init__(self) -> None:
        super().__init__()
        self.frames: list[Frame] = []
        self.changed = asyncio.Event()

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM:
            self.frames.append(frame)
            self.changed.set()
        await self.push_frame(frame, direction)

    async def wait_for(self, kind: type[Frame]) -> Frame:
        async with asyncio.timeout(3):
            while True:
                if frame := next((item for item in self.frames if isinstance(item, kind)), None):
                    return frame
                self.changed.clear()
                await self.changed.wait()


@dataclass
class RunningTransport:
    transport: PhoneTransport
    driver: ControlledDriver
    worker: PipelineWorker
    task: asyncio.Task[None]
    incoming: FrameLog
    outgoing: FrameLog

    async def close(self) -> None:
        if not self.task.done():
            await self.worker.cancel()
            await asyncio.wait_for(self.task, timeout=3)
        await self.transport.cleanup()


@pytest.fixture
async def running_transport() -> AsyncGenerator[RunningTransport, None]:
    drivers: list[ControlledDriver] = []

    def factory(callbacks: MediaCallbacks) -> ControlledDriver:
        driver = ControlledDriver(callbacks)
        drivers.append(driver)
        return driver

    transport = PhoneTransport(factory, number="13800138000", call_id=str(uuid4()))
    incoming, outgoing = FrameLog(), FrameLog()
    worker = PipelineWorker(
        Pipeline([transport.input(), incoming, transport.output(), outgoing]),
        enable_rtvi=False,
        enable_turn_tracking=False,
        idle_timeout_secs=None,
        params=PipelineParams(audio_in_sample_rate=16000, audio_out_sample_rate=24000),
    )
    runner = WorkerRunner(handle_sigint=False, handle_sigterm=False)
    await runner.add_workers(worker)
    task = asyncio.create_task(runner.run())
    running = RunningTransport(transport, drivers[0], worker, task, incoming, outgoing)
    await asyncio.wait_for(running.driver.connected.wait(), timeout=3)
    try:
        yield running
    finally:
        await running.close()


async def test_input_resamples_8k_pcm_to_existing_16k_asr(
    running_transport: RunningTransport,
) -> None:
    running = running_transport
    assert running.driver.target == ("13800138000", running.transport.call_id)
    await running.driver.callbacks.on_audio((1200).to_bytes(2, "little") * 800, 8000)
    frame = await running.incoming.wait_for(InputAudioRawFrame)
    assert isinstance(frame, InputAudioRawFrame)
    assert frame.sample_rate == 16000
    assert frame.num_channels == 1
    assert len(frame.audio) > 1600


async def test_output_resamples_paces_and_flushes_partial_sentence_before_text(
    running_transport: RunningTransport,
) -> None:
    running = running_transport
    running.driver.played.clear()
    audio = (2000).to_bytes(2, "little") * 1800  # 24k、75ms，末尾不足20ms。
    text = TTSTextFrame("完整句子。", aggregated_by="sentence")
    await running.worker.queue_frames([TTSAudioRawFrame(audio, 24000, 1), text])
    await asyncio.wait_for(running.driver.waiting.wait(), timeout=3)
    assert not any(isinstance(frame, TTSTextFrame) for frame in running.outgoing.frames)
    assert len(running.driver.audio) == 4
    assert all(len(chunk) == 320 for chunk in running.driver.audio)
    assert running.driver.times[-1] - running.driver.times[0] >= 0.045
    running.driver.played.set()
    assert await running.outgoing.wait_for(TTSTextFrame) is text


@pytest.mark.parametrize(
    "checkpoint",
    [
        LLMFullResponseEndFrame(),
        LLMAssistantPushAggregationFrame(),
        HangupAfterPlaybackFrame(tool_call_id="test", generation=0),
    ],
)
async def test_response_end_and_hangup_markers_wait_for_playback(
    running_transport: RunningTransport, checkpoint: Frame
) -> None:
    running = running_transport
    running.driver.played.clear()
    await running.worker.queue_frames(
        [TTSAudioRawFrame((2000).to_bytes(2, "little") * 960, 24000, 1), checkpoint]
    )
    await asyncio.wait_for(running.driver.waiting.wait(), timeout=3)
    assert checkpoint not in running.outgoing.frames
    running.driver.played.set()
    assert await running.outgoing.wait_for(type(checkpoint)) is checkpoint


async def test_interrupt_cancels_ack_old_pcm_and_old_text(
    running_transport: RunningTransport,
) -> None:
    running = running_transport
    running.driver.played.clear()
    await running.worker.queue_frames(
        [
            TTSAudioRawFrame((1000).to_bytes(2, "little") * 960, 24000, 1),
            TTSTextFrame("旧回复。", aggregated_by="sentence"),
            TTSAudioRawFrame((1000).to_bytes(2, "little") * 24000, 24000, 1),
        ]
    )
    await asyncio.wait_for(running.driver.waiting.wait(), timeout=3)
    before = len(running.driver.audio)
    await running.worker.queue_frame(InterruptionFrame())
    async with asyncio.timeout(3):
        while not running.driver.flush_count:
            await asyncio.sleep(0.005)
    running.driver.played.set()  # 对端迟到的旧确认不能复活旧文字。
    await running.worker.queue_frames(
        [
            TTSAudioRawFrame((3000).to_bytes(2, "little") * 960, 24000, 1),
            TTSTextFrame("最新回复。", aggregated_by="sentence"),
        ]
    )
    text = await running.outgoing.wait_for(TTSTextFrame)
    assert isinstance(text, TTSTextFrame)
    assert text.text == "最新回复。"
    assert running.driver.wait_cancelled >= 1
    assert not any(
        isinstance(frame, TTSTextFrame) and frame.text == "旧回复。"
        for frame in running.outgoing.frames
    )
    newer = b"".join(running.driver.audio[before:])
    samples = [
        int.from_bytes(newer[i : i + 2], "little", signed=True) for i in range(0, len(newer), 2)
    ]
    assert samples and sum(samples) / len(samples) > 2000


async def test_stop_during_playback_cancels_queued_pcm_and_closes_once(
    running_transport: RunningTransport,
) -> None:
    running = running_transport
    await running.worker.queue_frame(
        TTSAudioRawFrame((1000).to_bytes(2, "little") * 24000, 24000, 1)
    )
    async with asyncio.timeout(3):
        while not running.driver.audio:
            await asyncio.sleep(0.005)
    await running.close()
    sent = len(running.driver.audio)
    await asyncio.sleep(0.05)
    assert len(running.driver.audio) == sent < 50
    assert running.driver.hangup_count == 1
    assert running.driver.close_count == 1


async def test_interrupt_preserves_required_control_frame_and_next_audio_task(
    running_transport: RunningTransport,
) -> None:
    running = running_transport
    running.driver.played.clear()
    control = Frame()
    control.interruptible = False
    await running.worker.queue_frames(
        [
            TTSAudioRawFrame((1000).to_bytes(2, "little") * 320, 8000, 1),
            TTSTextFrame("未完成的旧句子。", aggregated_by="sentence"),
            control,
        ]
    )
    await asyncio.wait_for(running.driver.waiting.wait(), timeout=3)
    sender = running.transport.output()._media_senders[None]
    async with asyncio.timeout(3):
        while not sender._audio_queue.has_uninterruptible:
            await asyncio.sleep(0.005)
    audio_task = sender._audio_task
    await running.worker.queue_frame(InterruptionFrame())
    async with asyncio.timeout(3):
        while control not in running.outgoing.frames:
            running.outgoing.changed.clear()
            await running.outgoing.changed.wait()
    assert sender._audio_task is audio_task and not audio_task.done()
    assert running.driver.wait_cancelled == 1
    assert not any(isinstance(frame, TTSTextFrame) for frame in running.outgoing.frames)
    running.driver.played.set()
    await running.worker.queue_frames(
        [
            TTSAudioRawFrame((3000).to_bytes(2, "little") * 320, 8000, 1),
            TTSTextFrame("当前新句子。", aggregated_by="sentence"),
        ]
    )
    text = await running.outgoing.wait_for(TTSTextFrame)
    assert isinstance(text, TTSTextFrame) and text.text == "当前新句子。"


async def test_interrupt_discards_sub_chunk_before_bot_speaking(
    running_transport: RunningTransport,
) -> None:
    running = running_transport
    await running.worker.queue_frame(TTSAudioRawFrame((1000).to_bytes(2, "little") * 80, 8000, 1))
    await asyncio.sleep(0.02)
    assert running.driver.audio == []
    await running.worker.queue_frame(InterruptionFrame())
    async with asyncio.timeout(3):
        while not running.driver.flush_count:
            await asyncio.sleep(0.005)
    await running.worker.queue_frames(
        [
            TTSAudioRawFrame((3000).to_bytes(2, "little") * 320, 8000, 1),
            TTSTextFrame("新句子。", aggregated_by="sentence"),
        ]
    )
    await running.outgoing.wait_for(TTSTextFrame)
    assert running.driver.audio == [(3000).to_bytes(2, "little") * 160] * 2


@dataclass
class RunningPhoneVoice:
    session: VoiceSession
    task: asyncio.Task[str]
    transport: PhoneTransport
    driver: ControlledDriver
    recorder: Recorder

    async def user(self, text: str) -> None:
        assert self.session._worker is not None
        await self.session._worker.queue_frames(
            [
                VADUserStartedSpeakingFrame(),
                VADUserStoppedSpeakingFrame(stop_secs=0.6),
                TranscriptionFrame(text, "user", "fixture", finalized=True),
            ]
        )

    async def close(self) -> None:
        if not self.task.done():
            await self.session.stop("user_hangup")
            await asyncio.wait_for(self.task, timeout=3)
        await self.transport.cleanup()


async def phone_voice(
    monkeypatch: pytest.MonkeyPatch,
    services: Any,
    *,
    auto_answer: bool = True,
    opening: str = "您好，考虑了解我们的产品吗？",
) -> RunningPhoneVoice:
    drivers: list[ControlledDriver] = []

    def factory(callbacks: MediaCallbacks) -> ControlledDriver:
        driver = ControlledDriver(callbacks, auto_answer=auto_answer)
        drivers.append(driver)
        return driver

    transport = PhoneTransport(factory, number="13800138000", call_id=str(uuid4()))
    monkeypatch.setattr(voice_module, "create_services", lambda _: services)
    recorder = Recorder()
    settings = AppSettings()
    settings.sales.goal = "让用户考虑预约演示"
    settings.sales.product_info = "产品可以整理客户资料。"
    settings.sales.opening = opening
    session = VoiceSession(None, settings, recorder.callbacks(), transport=transport)
    task = asyncio.create_task(session.run())
    await asyncio.wait_for(drivers[0].started.wait(), timeout=3)
    return RunningPhoneVoice(session, task, transport, drivers[0], recorder)


async def wait_messages(recorder: Recorder, count: int) -> None:
    async with asyncio.timeout(3):
        while len(recorder.messages) < count:
            recorder.changed.clear()
            await recorder.changed.wait()


async def test_phone_answer_automatically_opens_once_without_rtvi_client_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = FixtureServices(Passthrough(), ControlledLLM(block_first=False), ControlledTTS())
    running = await phone_voice(monkeypatch, services, auto_answer=False)
    try:
        await asyncio.sleep(0.03)
        assert services.tts.requests == []
        await running.driver.answer()
        await running.driver.answer()
        await wait_messages(running.recorder, 1)
        assert services.tts.requests == ["您好，考虑了解我们的产品吗？"]
        assert running.recorder.messages[0].text == services.tts.requests[0]
        assert running.driver.audio
        assert services.llm.inputs == []
    finally:
        await running.close()
    assert services.closed


async def test_hangup_while_driver_is_connecting_cancels_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = FixtureServices(Passthrough(), ControlledLLM(block_first=False), ControlledTTS())
    drivers: list[ControlledDriver] = []

    def factory(callbacks: MediaCallbacks) -> ControlledDriver:
        driver = ControlledDriver(callbacks)
        driver.block_start = True
        drivers.append(driver)
        return driver

    transport = PhoneTransport(factory, number="13800138000", call_id=str(uuid4()))
    monkeypatch.setattr(voice_module, "create_services", lambda _: services)
    session = VoiceSession(None, AppSettings(), Recorder().callbacks(), transport=transport)
    task = asyncio.create_task(session.run())
    await asyncio.wait_for(drivers[0].started.wait(), timeout=3)
    await session.stop("user_hangup")
    assert await asyncio.wait_for(task, timeout=3) == "user_hangup"
    assert drivers[0].start_cancelled.is_set()
    assert drivers[0].close_count == 1
    assert services.closed


async def test_remote_busy_reason_and_transport_error_are_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = FixtureServices(Passthrough(), ControlledLLM(block_first=False), ControlledTTS())
    running = await phone_voice(monkeypatch, services, auto_answer=False)
    try:
        await running.driver.callbacks.on_ended("busy")
        assert await asyncio.wait_for(running.task, timeout=3) == "busy"
        assert services.tts.requests == []
        assert not running.recorder.errors
    finally:
        await running.close()

    services = FixtureServices(Passthrough(), ControlledLLM(block_first=False), ControlledTTS())
    running = await phone_voice(monkeypatch, services, auto_answer=False)
    try:
        await running.driver.callbacks.on_error("线路鉴权失败，请检查凭据。")
        assert await asyncio.wait_for(running.task, timeout=3) == "transport_error"
        assert running.recorder.errors == ["线路鉴权失败，请检查凭据。"]
        assert services.tts.requests == []
    finally:
        await running.close()


async def test_phone_goodbye_tool_waits_for_remote_playback_before_hangup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[dict[str, Any]] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(json.loads(await request.aread()))
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=tool_response(
                {
                    "intent": "direct_exit",
                    "confirmed": True,
                    "evidence": USER_END,
                    "goodbye": GOODBYE,
                }
            ),
        )

    llm = CompatibleLLMService(
        LLMSettings(base_url="http://fixture.invalid/v1", model="fixture"),
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )
    services = SDKServices(Passthrough(), llm, GoodbyeTTS())
    running = await phone_voice(monkeypatch, services)
    try:
        await wait_messages(running.recorder, 1)
        running.driver.waiting.clear()
        running.driver.played.clear()
        await running.user(USER_END)
        await asyncio.wait_for(running.driver.waiting.wait(), timeout=3)
        assert not running.task.done()
        assert running.driver.hangup_count == 0
        assert not any(message.text == GOODBYE for message in running.recorder.messages)
        running.driver.played.set()
        assert await asyncio.wait_for(running.task, timeout=3) == "ai_hangup"
        assert [message.text for message in running.recorder.messages] == [
            "您好，考虑了解我们的产品吗？",
            USER_END,
            GOODBYE,
        ]
        assert not running.recorder.messages[-1].interrupted
        assert running.driver.hangup_count == 1
        assert running.driver.close_count == 1
        assert len(requests) == 1
    finally:
        await running.close()


async def test_phone_interrupting_goodbye_cancels_hangup_and_answers_latest_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[dict[str, Any]] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(json.loads(await request.aread()))
        content = (
            tool_response(
                {
                    "intent": "direct_exit",
                    "confirmed": True,
                    "evidence": USER_END,
                    "goodbye": GOODBYE,
                }
            )
            if len(requests) == 1
            else sse_text(NEW_ANSWER) + b"data: [DONE]\n\n"
        )
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=content)

    llm = CompatibleLLMService(
        LLMSettings(base_url="http://fixture.invalid/v1", model="fixture"),
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )
    services = SDKServices(Passthrough(), llm, GoodbyeTTS())
    running = await phone_voice(monkeypatch, services)
    try:
        await wait_messages(running.recorder, 1)
        running.driver.waiting.clear()
        running.driver.played.clear()
        await running.user(USER_END)
        await asyncio.wait_for(running.driver.waiting.wait(), timeout=3)
        await running.user(FOLLOW_UP)
        async with asyncio.timeout(3):
            while len(requests) < 2:
                await asyncio.sleep(0.005)
        assert requests[-1]["messages"][-1]["role"] == "user"
        assert requests[-1]["messages"][-1]["content"].endswith(FOLLOW_UP)
        async with asyncio.timeout(3):
            # 模型输入先于输出端打断到达；等媒体 flush 后模拟迟到的旧确认。
            while running.driver.flush_count < 2:
                await asyncio.sleep(0.005)
        running.driver.played.set()  # 迟到的告别确认不能导致旧工具挂断。
        async with asyncio.timeout(3):
            while not any(message.text == NEW_ANSWER for message in running.recorder.messages):
                running.recorder.changed.clear()
                await running.recorder.changed.wait()
        assert not running.task.done()
        assert running.driver.hangup_count == 0
        assert running.driver.wait_cancelled >= 1
        assert not any(
            message.text == GOODBYE and not message.interrupted
            for message in running.recorder.messages
        )
    finally:
        await running.close()


async def test_session_stopped_before_run_does_not_dial(monkeypatch: pytest.MonkeyPatch) -> None:
    drivers: list[ControlledDriver] = []

    def factory(callbacks: MediaCallbacks) -> ControlledDriver:
        driver = ControlledDriver(callbacks)
        drivers.append(driver)
        return driver

    def must_not_create(settings: AppSettings) -> None:
        raise AssertionError("pre-cancelled voice session must not create models")

    monkeypatch.setattr(voice_module, "create_services", must_not_create)
    transport = PhoneTransport(factory, number="13800138000", call_id=str(uuid4()))
    session = VoiceSession(None, AppSettings(), Recorder().callbacks(), transport=transport)
    await session.stop("user_hangup")
    assert await session.run() == "user_hangup"
    assert not drivers[0].started.is_set()
    assert drivers[0].close_count == 1


async def test_cleanup_failure_is_visible_and_still_closes_driver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = FixtureServices(Passthrough(), ControlledLLM(block_first=False), ControlledTTS())
    running = await phone_voice(monkeypatch, services, auto_answer=False)
    running.driver.hangup_error = RuntimeError("upstream credentials must never be shown")
    try:
        await running.session.stop("user_hangup")
        assert await asyncio.wait_for(running.task, timeout=3) == "user_hangup"
        assert running.driver.close_count == 1
        assert services.closed
        assert running.recorder.errors == ["电话清理未获确认，请检查电话服务器是否存在遗留通道。"]
    finally:
        await running.close()
