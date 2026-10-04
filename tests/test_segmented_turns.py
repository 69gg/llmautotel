"""慢分段 ASR 的 FIFO 结果须覆盖最新停顿后，才可触发一次回复。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, cast

import httpx2
import pytest
from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.aggregators.llm_response_universal import LLMUserAggregator
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.turns.types import ProcessFrameResult
from test_voice import ControlledLLM, ControlledTTS, LocalTransport, Recorder, wait_messages

import llmautotel.voice as voice_module
from llmautotel.models import AppSettings, ASRSettings
from llmautotel.providers import CompatibleSTTService
from llmautotel.voice import VoiceSession


@dataclass
class SegmentServices:
    stt: CompatibleSTTService
    llm: ControlledLLM
    tts: ControlledTTS
    closed: bool = False

    async def aclose(self) -> None:
        await self.stt.aclose()
        await self.tts.aclose()
        self.closed = True


def find_user(processor: FrameProcessor) -> LLMUserAggregator | None:
    if isinstance(processor, LLMUserAggregator):
        return processor
    for child in processor.processors:
        found = find_user(child)
        if found is not None:
            return found
    return None


@pytest.mark.parametrize("first_text", ["我想了解价格。", ""])
@pytest.mark.parametrize("second_text", ["还有退款规则。", ""])
async def test_slow_fifo_asr_waits_for_latest_segment_before_one_llm_response(
    monkeypatch: pytest.MonkeyPatch,
    first_text: str,
    second_text: str,
) -> None:
    started = [asyncio.Event(), asyncio.Event()]
    release = [asyncio.Event(), asyncio.Event()]
    request_count = 0
    uploads: list[bytes] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal request_count
        index = request_count
        request_count += 1
        uploads.append(await request.aread())
        assert index < 2, "仅两个 VAD 段，应只有两个分段识别请求"
        started[index].set()
        await release[index].wait()
        text = first_text if index == 0 else second_text
        return httpx2.Response(200, json={"text": text})

    recorder = Recorder()
    transport = LocalTransport(recorder)
    stt = CompatibleSTTService(
        ASRSettings(base_url="http://fixture.invalid/v1", model="controlled-asr"),
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )
    services = SegmentServices(stt, ControlledLLM(block_first=False), ControlledTTS())
    monkeypatch.setattr(voice_module, "create_services", lambda settings: services)
    monkeypatch.setattr(voice_module, "SmallWebRTCTransport", lambda **kwargs: transport)
    settings = AppSettings()
    settings.sales.goal = "介绍订阅"
    settings.sales.product_info = "每月十元，支持退款"
    session = VoiceSession(cast(SmallWebRTCConnection, object()), settings, recorder.callbacks())
    task = asyncio.create_task(session.run())

    async def send_segment() -> None:
        assert session._worker is not None
        await session._worker.queue_frame(VADUserStartedSpeakingFrame())
        await session._worker.queue_frame(InputAudioRawFrame(b"\x01\x00" * 320, 16000, 1))
        await session._worker.queue_frame(VADUserStoppedSpeakingFrame(stop_secs=0.6))

    try:
        await asyncio.wait_for(transport.outgoing.started.wait(), timeout=3)
        assert session._worker is not None
        user = find_user(session._worker.pipeline)
        assert user is not None
        strategy = user._user_turn_controller.user_turn_strategies.stop[0]
        original_process = strategy.process_frame
        second_stop_processed = asyncio.Event()
        both_finals_processed = asyncio.Event()
        stops = 0
        finals = 0

        async def traced_process(frame: Frame) -> ProcessFrameResult:
            nonlocal stops, finals
            result = await original_process(frame)
            if isinstance(frame, VADUserStoppedSpeakingFrame):
                stops += 1
                if stops == 2:
                    second_stop_processed.set()
            if isinstance(frame, TranscriptionFrame) and frame.finalized:
                finals += 1
                if finals == 2:
                    both_finals_processed.set()
            return result

        # 仅记录官方策略完成处理的时刻，决策仍由真实策略执行。
        monkeypatch.setattr(strategy, "process_frame", traced_process)
        await send_segment()
        await asyncio.wait_for(started[0].wait(), timeout=3)
        assert session._worker is not None
        await session._worker.rtvi.set_client_ready()
        # 第一句仍在识别，用户已完成第二段补充；第二段必须排在第一段之后。
        await send_segment()
        await asyncio.wait_for(second_stop_processed.wait(), timeout=3)
        async with asyncio.timeout(3):
            while stt._segment_queue.qsize() != 1:
                await asyncio.sleep(0)
        assert not started[1].is_set()
        assert services.llm.inputs == []
        release[0].set()
        await asyncio.wait_for(started[1].wait(), timeout=3)
        # 第一段 final 已经过真实 STT 分段任务进入管线，而最新段仍未返回。
        await asyncio.sleep(0.05)
        assert services.llm.inputs == [], "旧段 finalized 不得提前触发只有第一段的回复"
        assert recorder.messages == [], "完整用户回合应等待最新已停顿段识别完成"
        assert "speaking" not in recorder.states
        release[1].set()
        await asyncio.wait_for(both_finals_processed.wait(), timeout=3)
        assert request_count == 2 and all(b"RIFF" in upload for upload in uploads)
        if not first_text and not second_text:
            assert services.llm.inputs == []
            assert recorder.messages == []
            assert session._state == "listening"
            return
        await wait_messages(recorder, 2)
        assert len(services.llm.inputs) == 1
        last_input: dict[str, Any] = services.llm.inputs[0][-1]
        assert last_input["role"] == "user"
        if first_text:
            assert first_text in last_input["content"]
        if second_text:
            assert second_text in last_input["content"]
        assert [message.role for message in recorder.messages] == ["user", "assistant"]
        assert recorder.messages[0].text == last_input["content"]
    finally:
        for event in release:
            event.set()
        await session.stop()
        await asyncio.wait_for(task, timeout=3)
        assert services.closed
