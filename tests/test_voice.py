"""使用真实 Pipecat 管线验证开场、打断、上下文及资源释放。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any, Literal, cast

import pytest
from pipecat.frames.frames import (
    ErrorFrame,
    Frame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    OutputAudioRawFrame,
    OutputTransportMessageFrame,
    OutputTransportMessageUrgentFrame,
    StartFrame,
    STTMetadataFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection

import llmautotel.voice as voice_module
from llmautotel.conversation import INTERRUPTED_BACKGROUND_LABEL
from llmautotel.models import AppSettings, TranscriptEntry, TTSSettings
from llmautotel.providers import CompatibleTTSService
from llmautotel.voice import TranscriptRole, VoiceCallbacks, VoiceSession


@dataclass
class Recorder:
    states: list[str] = field(default_factory=list)
    messages: list[TranscriptEntry] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    ordering: list[str] = field(default_factory=list)
    changed: asyncio.Event = field(default_factory=asyncio.Event)

    async def on_state(self, state: str) -> None:
        self.states.append(state)
        self.changed.set()

    async def on_message(
        self, role: TranscriptRole, text: str, interrupted: bool, timestamp: str
    ) -> None:
        self.messages.append(
            TranscriptEntry(role=role, text=text, interrupted=interrupted, timestamp=timestamp)
        )
        self.ordering.append(f"persist:{role}:{text}")
        self.changed.set()

    async def on_error(self, message: str) -> None:
        self.errors.append(message)
        self.changed.set()

    def callbacks(self) -> VoiceCallbacks:
        return VoiceCallbacks(self.on_state, self.on_message, self.on_error)


class Passthrough(FrameProcessor):
    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)


class ControlledLLM(FrameProcessor):
    def __init__(self, *, block_first: bool, unspoken_first: bool = False) -> None:
        super().__init__()
        self.block_first = block_first
        self.unspoken_first = unspoken_first
        self.inputs: list[list[dict[str, Any]]] = []
        self.first_blocked = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if not isinstance(frame, LLMContextFrame):
            await self.push_frame(frame, direction)
            return
        self.inputs.append([dict(message) for message in frame.context.get_messages()])
        await self.push_frame(LLMFullResponseStartFrame())
        if self.block_first and len(self.inputs) == 1:
            if self.unspoken_first:
                await self.push_frame(LLMTextFrame("这个尚未完整生成的开场"))
            else:
                await self.push_frame(LLMTextFrame("第一句。第二句。"))
                await self.push_frame(LLMTextFrame("后续"))
            self.first_blocked.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
        else:
            await self.push_frame(LLMTextFrame("新的回答。"))
            await self.push_frame(LLMFullResponseEndFrame())


class ControlledTTS(CompatibleTTSService):
    def __init__(
        self, *, block_first: Literal["before_audio", "during_audio"] | None = None
    ) -> None:
        super().__init__(
            TTSSettings(base_url="http://fixture.invalid/v1", model="fixture", voice="fixture")
        )
        self.requests: list[str] = []
        self.block_first = block_first
        self.first_blocked = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame, None]:
        self.requests.append(text)
        marker = 2 if "第二句" in text else 1
        chunks = 50 if marker == 2 else 1
        try:
            if self.block_first is not None and len(self.requests) == 1:
                if self.block_first == "during_audio":
                    yield TTSAudioRawFrame(
                        audio=(3).to_bytes(2, "little") * 960,
                        sample_rate=24000,
                        num_channels=1,
                        context_id=context_id,
                    )
                self.first_blocked.set()
                await asyncio.Event().wait()
            for _ in range(chunks):
                yield TTSAudioRawFrame(
                    audio=marker.to_bytes(2, "little") * 960,
                    sample_rate=24000,
                    num_channels=1,
                    context_id=context_id,
                )
            if marker == 2:
                await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


class PlayedOutput(BaseOutputTransport):
    def __init__(self, params: TransportParams, recorder: Recorder) -> None:
        super().__init__(params)
        self.recorder = recorder
        self.started = asyncio.Event()
        self.second_audio = asyncio.Event()
        self.audio_written = asyncio.Event()
        self.transcript_sent = asyncio.Event()
        self.played: list[int] = []
        self.server_messages: list[dict[str, Any]] = []

    async def start(self, frame: StartFrame) -> None:
        await super().start(frame)
        await self.set_transport_ready(frame)
        self.started.set()

    async def write_audio_frame(self, frame: OutputAudioRawFrame) -> bool:
        # 真正使用 BaseOutputTransport 的排队/清队列；仅替换最后的设备写入。
        await asyncio.sleep(0.02)
        marker = int.from_bytes(frame.audio[:2], "little")
        self.played.append(marker)
        self.audio_written.set()
        if marker == 2:
            self.second_audio.set()
        return True

    async def send_message(
        self, frame: OutputTransportMessageFrame | OutputTransportMessageUrgentFrame
    ) -> None:
        message = frame.message
        if isinstance(message, dict) and message.get("type") == "server-message":
            data = message["data"]
            self.server_messages.append(data)
            if data.get("type") == "transcript":
                entry = data["entry"]
                self.recorder.ordering.append(f"send:{entry['role']}:{entry['text']}")
                self.transcript_sent.set()


class LocalTransport(BaseTransport):
    def __init__(self, recorder: Recorder) -> None:
        super().__init__()
        self._register_event_handler("on_client_disconnected")
        self.incoming = Passthrough()
        self.outgoing = PlayedOutput(
            TransportParams(audio_in_enabled=True, audio_out_enabled=True), recorder
        )

    def input(self) -> FrameProcessor:
        return self.incoming

    def output(self) -> FrameProcessor:
        return self.outgoing


@dataclass
class FixtureServices:
    stt: Passthrough
    llm: ControlledLLM
    tts: ControlledTTS
    closed: bool = False

    async def aclose(self) -> None:
        await self.tts.aclose()
        self.closed = True


@dataclass
class RunningSession:
    session: VoiceSession
    task: asyncio.Task[str]
    recorder: Recorder
    transport: LocalTransport
    services: FixtureServices

    async def ready(self) -> None:
        assert self.session._worker is not None
        await self.session._worker.rtvi.set_client_ready()

    async def frame(self, frame: Frame) -> None:
        assert self.session._worker is not None
        await self.session._worker.queue_frame(frame)

    async def close(self) -> None:
        await self.session.stop()
        assert await asyncio.wait_for(self.task, timeout=3) == "hangup"
        assert self.services.closed


async def start_session(
    monkeypatch: pytest.MonkeyPatch,
    *,
    block_first: bool = False,
    unspoken_first: bool = False,
    tts_block_first: Literal["before_audio", "during_audio"] | None = None,
    opening: str = "",
    asr_timeout: float = 30.0,
) -> RunningSession:
    recorder = Recorder()
    transport = LocalTransport(recorder)
    services = FixtureServices(
        Passthrough(),
        ControlledLLM(block_first=block_first, unspoken_first=unspoken_first),
        ControlledTTS(block_first=tts_block_first),
    )
    monkeypatch.setattr(voice_module, "create_services", lambda settings: services)
    monkeypatch.setattr(voice_module, "SmallWebRTCTransport", lambda **kwargs: transport)
    settings = AppSettings()
    settings.sales.goal = "让用户订阅测试计划"
    settings.sales.product_info = "测试计划价格每月 10 元"
    settings.sales.opening = opening
    settings.asr.timeout_seconds = asr_timeout
    session = VoiceSession(cast(SmallWebRTCConnection, object()), settings, recorder.callbacks())
    task = asyncio.create_task(session.run())
    await asyncio.wait_for(transport.outgoing.started.wait(), timeout=3)
    return RunningSession(session, task, recorder, transport, services)


async def wait_messages(recorder: Recorder, count: int) -> None:
    async with asyncio.timeout(3):
        while len(recorder.messages) < count:
            recorder.changed.clear()
            await recorder.changed.wait()


async def wait_state(running: RunningSession, state: str) -> None:
    async with asyncio.timeout(3):
        while running.session._state != state:
            running.recorder.changed.clear()
            await running.recorder.changed.wait()


async def test_ready_generates_opening_once_and_persists_before_web_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    running = await start_session(monkeypatch)
    try:
        await running.ready()
        await running.ready()
        await wait_messages(running.recorder, 1)
        assert len(running.services.llm.inputs) == 1
        prompt = running.services.llm.inputs[0][0]["content"]
        assert "让用户订阅测试计划" in prompt
        assert "价格每月 10 元" in prompt
        assert running.recorder.messages[0].text == "新的回答。"
        await asyncio.wait_for(running.transport.outgoing.transcript_sent.wait(), timeout=3)
        assert running.recorder.ordering[:2] == [
            "persist:assistant:新的回答。", "send:assistant:新的回答。"
        ]
    finally:
        await running.close()


async def test_fixed_opening_uses_tts_and_same_spoken_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    running = await start_session(monkeypatch, opening="您好，我是 AI 助手。")
    try:
        await running.ready()
        await running.ready()
        await wait_messages(running.recorder, 1)
        assert running.services.llm.inputs == []
        assert running.services.tts.requests == ["您好，我是 AI 助手。"]
        assert running.recorder.messages[0].text == "您好，我是 AI 助手。"
        await running.frame(VADUserStartedSpeakingFrame())
        await running.frame(VADUserStoppedSpeakingFrame(stop_secs=0.6))
        await running.frame(TranscriptionFrame("价格多少？", "user", "now", finalized=True))
        await wait_messages(running.recorder, 3)
        assert {"role": "assistant", "content": "您好，我是 AI 助手。"} in (
            running.services.llm.inputs[0]
        )
    finally:
        await running.close()


async def test_user_speaking_before_ready_suppresses_late_opening(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    running = await start_session(monkeypatch)
    try:
        await running.frame(VADUserStartedSpeakingFrame())
        async with asyncio.timeout(3):
            while not running.session._user_spoken:
                await asyncio.sleep(0)
        await running.ready()
        assert running.services.llm.inputs == []
        assert running.services.tts.requests == []
        await running.frame(VADUserStoppedSpeakingFrame(stop_secs=0.6))
        await running.frame(TranscriptionFrame("你好？", "user", "now", finalized=True))
        await wait_messages(running.recorder, 2)
        assert len(running.services.llm.inputs) == 1
        assert running.services.llm.inputs[0][-1] == {"role": "user", "content": "你好？"}
    finally:
        await running.close()


async def test_interrupt_keeps_completed_sentence_and_labels_unfinished_background(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    running = await start_session(monkeypatch, block_first=True)
    try:
        await running.ready()
        await asyncio.wait_for(running.transport.outgoing.second_audio.wait(), timeout=3)
        await running.frame(VADUserStartedSpeakingFrame())
        await asyncio.wait_for(running.services.llm.cancelled.wait(), timeout=3)
        await asyncio.wait_for(running.services.tts.cancelled.wait(), timeout=3)
        await wait_messages(running.recorder, 1)
        interrupted = running.recorder.messages[0]
        assert interrupted.text == "第一句。"
        assert interrupted.interrupted
        old_audio_count = running.transport.outgoing.played.count(2)
        await asyncio.sleep(0.06)
        assert running.transport.outgoing.played.count(2) == old_audio_count
        assert old_audio_count < 50

        await running.frame(VADUserStoppedSpeakingFrame(stop_secs=0.6))
        await running.frame(TranscriptionFrame("先说价格。", "user", "now", finalized=True))
        await wait_messages(running.recorder, 3)
        new_context = running.services.llm.inputs[1]
        assert {"role": "assistant", "content": "第一句。"} in new_context
        background = [
            message["content"] for message in new_context
            if str(message.get("content", "")).startswith(INTERRUPTED_BACKGROUND_LABEL)
        ]
        assert len(background) == 1
        assert "第二句" in background[0]
        assert "第一句" not in background[0]
        assert new_context[-1] == {"role": "user", "content": "先说价格。"}
        assert running.recorder.messages[-1].text == "新的回答。"
    finally:
        await running.close()


async def test_provider_error_is_sanitized_and_ends_real_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    running = await start_session(monkeypatch)
    await running.frame(ErrorFrame(error="tts: upstream response with secret-key"))
    assert await asyncio.wait_for(running.task, timeout=3) == "model_error"
    assert running.services.closed
    assert len(running.recorder.errors) == 1
    assert "语音合成" in running.recorder.errors[0]
    assert "secret-key" not in running.recorder.errors[0]


async def test_empty_final_transcript_returns_to_listening_without_new_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    running = await start_session(monkeypatch, block_first=True)
    try:
        await running.frame(STTMetadataFrame(service_name="fixture", ttfs_p99_latency=2.0))
        await running.ready()
        await asyncio.wait_for(running.transport.outgoing.second_audio.wait(), timeout=3)
        await running.frame(VADUserStartedSpeakingFrame())
        await asyncio.wait_for(running.services.llm.cancelled.wait(), timeout=3)
        await asyncio.wait_for(running.services.tts.cancelled.wait(), timeout=3)
        await wait_messages(running.recorder, 1)
        await running.frame(VADUserStoppedSpeakingFrame(stop_secs=0.6))
        await wait_state(running, "recognizing")
        await running.frame(TranscriptionFrame("", "user", "now", finalized=True))
        await wait_state(running, "listening")
        assert len(running.services.llm.inputs) == 1
        assert len(running.recorder.messages) == 1
        assert running.recorder.messages[0].interrupted

        await running.frame(VADUserStartedSpeakingFrame())
        await running.frame(VADUserStoppedSpeakingFrame(stop_secs=0.6))
        await running.frame(TranscriptionFrame("我想了解价格。", "user", "now", finalized=True))
        await wait_messages(running.recorder, 3)
        assert len(running.services.llm.inputs) == 2
        assert running.services.llm.inputs[-1][-1] == {
            "role": "user", "content": "我想了解价格。"
        }
        assert running.recorder.messages[-1].text == "新的回答。"
    finally:
        await running.close()


@pytest.mark.parametrize("text", ["我想了解价格。", ""])
async def test_delayed_final_waits_past_stt_safety_timer(
    monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    running = await start_session(monkeypatch)
    try:
        await running.frame(STTMetadataFrame(service_name="fixture", ttfs_p99_latency=0.65))
        await running.frame(VADUserStartedSpeakingFrame())
        await running.frame(VADUserStoppedSpeakingFrame(stop_secs=0.6))
        await wait_state(running, "recognizing")
        # 超过 P99 安全计时器，不代表分段 HTTP 转写已经完成。
        await asyncio.sleep(0.15)
        assert running.session._state == "recognizing"
        assert running.services.llm.inputs == []
        assert running.recorder.messages == []

        await running.frame(TranscriptionFrame(text, "user", "now", finalized=True))
        if text:
            await wait_messages(running.recorder, 2)
            assert running.services.llm.inputs[0][-1] == {"role": "user", "content": text}
            assert "thinking" in running.recorder.states
        else:
            await wait_state(running, "listening")
            assert running.services.llm.inputs == []
            assert running.recorder.messages == []
    finally:
        await running.close()


async def test_asr_configured_timeout_keeps_turn_open_past_default_watchdog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    running = await start_session(monkeypatch, asr_timeout=6.0)
    try:
        await running.frame(STTMetadataFrame(service_name="fixture", ttfs_p99_latency=0.65))
        await running.frame(VADUserStartedSpeakingFrame())
        await running.frame(VADUserStoppedSpeakingFrame(stop_secs=0.6))
        await wait_state(running, "recognizing")
        # Pipecat 默认回合 watchdog 为 5 秒；6 秒 ASR 超时仍容许迟到结果。
        await asyncio.sleep(5.1)
        assert running.session._state == "recognizing"
        assert running.services.llm.inputs == []
        await running.frame(TranscriptionFrame("现在说价格。", "user", "now", finalized=True))
        await wait_messages(running.recorder, 2)
        assert running.recorder.messages[0].text == "现在说价格。"
    finally:
        await running.close()


async def test_previous_final_cannot_finish_resumed_speech(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    running = await start_session(monkeypatch)
    try:
        await running.frame(STTMetadataFrame(service_name="fixture", ttfs_p99_latency=0.65))
        await running.frame(VADUserStartedSpeakingFrame())
        await running.frame(VADUserStoppedSpeakingFrame(stop_secs=0.6))
        await wait_state(running, "recognizing")
        await running.frame(VADUserStartedSpeakingFrame())
        await running.frame(TranscriptionFrame("我想了解价格。", "user", "first", finalized=True))
        await asyncio.sleep(0.1)
        assert running.services.llm.inputs == []
        assert "thinking" not in running.recorder.states

        await running.frame(VADUserStoppedSpeakingFrame(stop_secs=0.6))
        await asyncio.sleep(0.1)
        assert running.services.llm.inputs == []
        await running.frame(
            TranscriptionFrame("还有使用限制。", "user", "second", finalized=True)
        )
        await wait_messages(running.recorder, 2)
        assert len(running.services.llm.inputs) == 1
        assert "我想了解价格。" in running.recorder.messages[0].text
        assert "还有使用限制。" in running.recorder.messages[0].text
    finally:
        await running.close()


async def test_segmented_asr_watchdog_fails_without_reply_to_incomplete_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    running = await start_session(monkeypatch, asr_timeout=1.0)
    try:
        await running.frame(STTMetadataFrame(service_name="fixture", ttfs_p99_latency=0.65))
        await running.frame(VADUserStartedSpeakingFrame())
        await running.frame(VADUserStoppedSpeakingFrame(stop_secs=0.6))
        await running.frame(VADUserStartedSpeakingFrame())
        await running.frame(VADUserStoppedSpeakingFrame(stop_secs=0.6))
        # 第二段一直没有 final；第一段有有效内容，可检测 watchdog 是否误触发 LLM。
        await running.frame(
            TranscriptionFrame("我想了解价格。", "user", "first", finalized=True)
        )
        assert await asyncio.wait_for(running.task, timeout=3) == "model_error"
        assert running.services.closed
        assert running.recorder.errors == [
            "语音识别等待超时，请检查识别服务和超时设置后重试。"
        ]
        assert running.services.llm.inputs == []
        assert running.services.tts.requests == []
    finally:
        if not running.task.done():
            await running.close()


async def test_hangup_discards_pending_user_turn_without_new_model_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    running = await start_session(monkeypatch)
    try:
        await running.frame(STTMetadataFrame(service_name="fixture", ttfs_p99_latency=0.65))
        await running.frame(VADUserStartedSpeakingFrame())
        await running.frame(VADUserStoppedSpeakingFrame(stop_secs=0.6))
        await running.frame(VADUserStartedSpeakingFrame())
        await running.frame(VADUserStoppedSpeakingFrame(stop_secs=0.6))
        await running.frame(
            TranscriptionFrame("我想了解价格。", "user", "first", finalized=True)
        )
        user = running.session._user_aggregator
        assert user is not None
        async with asyncio.timeout(3):
            while user.aggregation_string() != "我想了解价格。":
                await asyncio.sleep(0)
        assert running.services.llm.inputs == []
        await running.close()
        assert running.services.llm.inputs == []
        assert running.services.tts.requests == []
        assert running.recorder.messages == []
    finally:
        if not running.task.done():
            await running.close()


@pytest.mark.parametrize("tts_stage", ["before_audio", "during_audio"])
async def test_interrupt_fixed_opening_before_complete_sentence_never_resumes(
    monkeypatch: pytest.MonkeyPatch,
    tts_stage: Literal["before_audio", "during_audio"],
) -> None:
    opening = "您好，我是 AI 助手，想和您介绍订阅计划。"
    running = await start_session(monkeypatch, opening=opening, tts_block_first=tts_stage)
    try:
        await running.ready()
        await asyncio.wait_for(running.services.tts.first_blocked.wait(), timeout=3)
        if tts_stage == "before_audio":
            assert running.transport.outgoing.played == []
        else:
            await asyncio.wait_for(running.transport.outgoing.audio_written.wait(), timeout=3)
            assert running.transport.outgoing.played
            assert set(running.transport.outgoing.played) == {3}
        assert running.services.tts.requests == [opening]
        assert running.services.llm.inputs == []

        await running.frame(VADUserStartedSpeakingFrame())
        await asyncio.wait_for(running.services.tts.cancelled.wait(), timeout=3)
        await wait_messages(running.recorder, 1)
        assert running.recorder.messages[0].text == ""
        assert running.recorder.messages[0].interrupted
        old_audio_count = len(running.transport.outgoing.played)
        await running.frame(VADUserStoppedSpeakingFrame(stop_secs=0.6))
        await running.frame(TranscriptionFrame("", "user", "empty", finalized=True))
        await wait_state(running, "listening")
        await running.ready()
        await asyncio.sleep(0.1)
        assert len(running.transport.outgoing.played) == old_audio_count
        assert running.services.tts.requests == [opening]
        assert running.services.llm.inputs == []
        if tts_stage == "before_audio":
            assert old_audio_count == 0

        await running.frame(VADUserStartedSpeakingFrame())
        await running.frame(VADUserStoppedSpeakingFrame(stop_secs=0.6))
        await running.frame(TranscriptionFrame("先说价格。", "user", "valid", finalized=True))
        await wait_messages(running.recorder, 3)
        assert len(running.services.llm.inputs) == 1
        next_context = running.services.llm.inputs[0]
        assert next_context[-2] == {
            "role": "assistant", "content": f"{INTERRUPTED_BACKGROUND_LABEL}\n{opening}"
        }
        assert next_context[-1] == {"role": "user", "content": "先说价格。"}
        assert running.services.tts.requests == [opening, "新的回答。"]
        assert running.transport.outgoing.played[old_audio_count:]
        assert set(running.transport.outgoing.played[old_audio_count:]) == {1}
    finally:
        await running.close()


async def test_interrupt_llm_text_before_any_tts_never_enters_spoken_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    running = await start_session(monkeypatch, block_first=True, unspoken_first=True)
    try:
        await running.ready()
        await asyncio.wait_for(running.services.llm.first_blocked.wait(), timeout=3)
        assert len(running.services.llm.inputs) == 1
        assert running.services.tts.requests == []
        assert running.transport.outgoing.played == []

        await running.frame(VADUserStartedSpeakingFrame())
        await asyncio.wait_for(running.services.llm.cancelled.wait(), timeout=3)
        await wait_messages(running.recorder, 1)
        assert running.recorder.messages[0].text == ""
        assert running.recorder.messages[0].interrupted
        await running.frame(VADUserStoppedSpeakingFrame(stop_secs=0.6))
        await running.frame(TranscriptionFrame("", "user", "empty", finalized=True))
        await wait_state(running, "listening")
        await running.ready()
        await asyncio.sleep(0.1)
        assert len(running.services.llm.inputs) == 1
        assert running.services.tts.requests == []
        assert running.transport.outgoing.played == []

        await running.frame(VADUserStartedSpeakingFrame())
        await running.frame(VADUserStoppedSpeakingFrame(stop_secs=0.6))
        await running.frame(TranscriptionFrame("先说价格。", "user", "valid", finalized=True))
        await wait_messages(running.recorder, 3)
        assert len(running.services.llm.inputs) == 2
        next_context = running.services.llm.inputs[1]
        assert next_context[-2] == {
            "role": "assistant",
            "content": f"{INTERRUPTED_BACKGROUND_LABEL}\n这个尚未完整生成的开场",
        }
        assert next_context[-1] == {"role": "user", "content": "先说价格。"}
        assert running.services.tts.requests == ["新的回答。"]
        assert running.transport.outgoing.played
        assert set(running.transport.outgoing.played) == {1}
    finally:
        await running.close()
