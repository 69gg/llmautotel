"""真实 SDK 工具流、告别播放顺序、插话取消和结束原因竞态。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, cast
from unittest.mock import AsyncMock

import httpx2
import pytest
from pipecat.frames.frames import (
    Frame,
    FunctionCallResultProperties,
    InterruptionFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSSpeakFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.worker import PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.llm_service import FunctionCallParams
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.request_handler import SmallWebRTCRequestHandler
from test_interruption_context import SDKServices
from test_lifecycle import LocalHandler, connect
from test_providers import sse_text
from test_sessions import configured_settings
from test_voice import LocalTransport, Passthrough, Recorder

import llmautotel.voice as voice_module
from llmautotel.conversation import INTERRUPTED_BACKGROUND_LABEL
from llmautotel.hangup import HangupAfterPlaybackFrame, HangupController
from llmautotel.models import AppSettings, LLMSettings, TTSSettings
from llmautotel.providers import CompatibleLLMService, CompatibleTTSService
from llmautotel.sessions import SessionManager, VoiceRuntime
from llmautotel.store import Store
from llmautotel.voice import VoiceCallbacks, VoiceSession

USER_END = "不用了，谢谢。"
GOODBYE = "好的，祝您一切顺利，再见。"
FOLLOW_UP = "等一下，我还想了解它有什么功能？"
NEW_ANSWER = "它可以回答问题和整理文字。"


def tool_response(arguments: dict[str, Any], *, tool_id: str = "hangup-1") -> bytes:
    """通过实际 SDK 的 SSE 解析进入 Pipecat 工具执行器。"""
    chunk = {
        "id": "fixture-tool-reply",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "controlled-llm",
        "choices": [{
            "index": 0,
            "delta": {"tool_calls": [{
                "index": 0,
                "id": tool_id,
                "type": "function",
                "function": {
                    "name": "hang_up",
                    "arguments": json.dumps(arguments, ensure_ascii=False),
                },
            }]},
            "finish_reason": "tool_calls",
        }],
    }
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\ndata: [DONE]\n\n".encode()


class DelayedToolSSE(httpx2.AsyncByteStream):
    def __init__(self) -> None:
        self.waiting = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield tool_response({
            "confirmed": True, "evidence": USER_END, "goodbye": GOODBYE,
        }).removesuffix(b"data: [DONE]\n\n")
        self.waiting.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        yield b"data: [DONE]\n\n"

    async def aclose(self) -> None:
        self.closed = True


class GoodbyeTTS(CompatibleTTSService):
    """保留真实 TTS 队列，仅替换上游 PCM 生产以控制请求与播放窗口。"""

    def __init__(self, mode: Literal["complete", "request"] = "complete") -> None:
        super().__init__(
            TTSSettings(base_url="http://fixture.invalid/v1", model="fixture", voice="fixture")
        )
        self.mode = mode
        self.requests: list[str] = []
        self.waiting = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.generated = asyncio.Event()

    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame, None]:
        self.requests.append(text)
        try:
            if self.mode == "request" and len(self.requests) == 1:
                self.waiting.set()
                await asyncio.Event().wait()
            marker = 1 if text == GOODBYE else 2
            samples = 14400 if marker == 1 else 960
            yield TTSAudioRawFrame(
                audio=marker.to_bytes(2, "little") * samples,
                sample_rate=24000,
                num_channels=1,
                context_id=context_id,
            )
            if marker == 1:
                self.generated.set()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


@dataclass
class RunningHangup:
    session: VoiceSession
    task: asyncio.Task[str]
    transport: LocalTransport
    recorder: Recorder
    services: SDKServices
    requests: list[dict[str, Any]]

    async def send(self, frame: Frame) -> None:
        assert self.session._worker is not None
        await self.session._worker.queue_frame(frame)

    async def user(self, text: str) -> None:
        await self.send(VADUserStartedSpeakingFrame())
        await self.send(VADUserStoppedSpeakingFrame(stop_secs=0.6))
        await self.send(TranscriptionFrame(text, "user", "fixture", finalized=True))

    async def close(self) -> None:
        if not self.task.done():
            await self.session.stop()
            await asyncio.wait_for(self.task, timeout=3)
        await self.services.aclose()


async def running_hangup(
    monkeypatch: pytest.MonkeyPatch,
    *,
    mode: Literal["complete", "request"] = "complete",
    arguments: dict[str, Any] | None = None,
    delayed_tool: DelayedToolSSE | None = None,
) -> RunningHangup:
    requests: list[dict[str, Any]] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(json.loads(await request.aread()))
        if len(requests) == 1 and delayed_tool is not None:
            return httpx2.Response(
                200, headers={"content-type": "text/event-stream"}, stream=delayed_tool
            )
        content = (
            tool_response(arguments or {
                "confirmed": True, "evidence": USER_END, "goodbye": GOODBYE,
            })
            if len(requests) == 1
            else sse_text(NEW_ANSWER) + b"data: [DONE]\n\n"
        )
        return httpx2.Response(
            200, headers={"content-type": "text/event-stream"}, content=content
        )

    recorder = Recorder()
    transport = LocalTransport(recorder)
    service = CompatibleLLMService(
        LLMSettings(base_url="http://fixture.invalid/v1", model="controlled-llm"),
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )
    services = SDKServices(Passthrough(), service, cast(Any, GoodbyeTTS(mode)))
    monkeypatch.setattr(voice_module, "create_services", lambda settings: services)
    monkeypatch.setattr(voice_module, "SmallWebRTCTransport", lambda **kwargs: transport)
    settings = AppSettings()
    settings.sales.goal = "订阅测试产品"
    settings.sales.product_info = "测试产品可以回答问题。"
    session = VoiceSession(cast(SmallWebRTCConnection, object()), settings, recorder.callbacks())
    task = asyncio.create_task(session.run())
    await asyncio.wait_for(transport.outgoing.started.wait(), timeout=3)
    # 不发送 client-ready，避免另起主动开场请求干扰工具回合。
    return RunningHangup(session, task, transport, recorder, services, requests)


async def wait_new_answer(recorder: Recorder) -> None:
    """等待可观察的新回答，工具无正文时不虚构额外的助手文字行。"""
    async with asyncio.timeout(3):
        while not any(message.role == "assistant" and message.text == NEW_ANSWER
                      for message in recorder.messages):
            recorder.changed.clear()
            await recorder.changed.wait()


async def test_real_sdk_hangup_tool_waits_until_goodbye_audio_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    running = await running_hangup(monkeypatch)
    tts = cast(GoodbyeTTS, running.services.tts)
    try:
        await running.user(USER_END)
        await asyncio.wait_for(tts.generated.wait(), timeout=3)
        await asyncio.wait_for(running.transport.outgoing.audio_written.wait(), timeout=3)
        assert not running.task.done(), "合成完成但音频仍在输出时不能挂断"
        assert running.transport.outgoing.played.count(1) < 15
        request = running.requests[0]
        schema = next(tool for tool in request["tools"] if tool["function"]["name"] == "hang_up")
        assert schema["type"] == "function"
        parameters = schema["function"]["parameters"]
        assert set(parameters["required"]) == {"confirmed", "evidence", "goodbye"}
        assert parameters["properties"]["confirmed"]["type"] == "boolean"
        assert request["messages"][-1] == {"role": "user", "content": USER_END}
        assert await asyncio.wait_for(running.task, timeout=3) == "ai_hangup"
        # 24 kHz mono、16-bit PCM：14400 samples，输出 fixture 每块960 samples。
        # 框架 EndFrame 可追加静音，只统计实际告别PCM，不能要求完全没有静音。
        assert running.transport.outgoing.played.count(1) == 15
        assert set(running.transport.outgoing.played) <= {0, 1}
        assert tts.requests == [GOODBYE]
        assert len(running.requests) == 1, "工具成功后不应再请求 LLM 补生成告别"
        assert [message.text for message in running.recorder.messages] == [USER_END, GOODBYE]
        assert not running.recorder.messages[-1].interrupted
        assert not running.recorder.errors
        assert running.services.closed
    finally:
        await running.close()


async def test_late_tool_stream_cannot_hang_up_after_user_changes_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = DelayedToolSSE()
    running = await running_hangup(monkeypatch, delayed_tool=stream)
    try:
        await running.user(USER_END)
        await asyncio.wait_for(stream.waiting.wait(), timeout=3)
        await running.send(VADUserStartedSpeakingFrame())
        await asyncio.wait_for(stream.cancelled.wait(), timeout=3)
        assert stream.closed
        stream.release.set()
        await running.send(VADUserStoppedSpeakingFrame(stop_secs=0.6))
        await running.send(TranscriptionFrame(FOLLOW_UP, "user", "new", finalized=True))
        await wait_new_answer(running.recorder)
        assert running.services.tts.requests == [NEW_ANSWER]
        assert running.recorder.messages[-1].text == NEW_ANSWER
        assert len(running.requests) == 2
        assert not running.task.done()
        assert not running.recorder.errors
    finally:
        await running.close()


@pytest.mark.parametrize("stage", ["request", "playback"])
async def test_user_interrupts_pending_goodbye_and_continues_new_question(
    monkeypatch: pytest.MonkeyPatch, stage: Literal["request", "playback"]
) -> None:
    running = await running_hangup(
        monkeypatch, mode="request" if stage == "request" else "complete"
    )
    tts = cast(GoodbyeTTS, running.services.tts)
    try:
        await running.user(USER_END)
        if stage == "request":
            await asyncio.wait_for(tts.waiting.wait(), timeout=3)
        else:
            await asyncio.wait_for(tts.generated.wait(), timeout=3)
            await asyncio.wait_for(running.transport.outgoing.audio_written.wait(), timeout=3)
        previous_audio = running.transport.outgoing.played.count(1)
        await running.send(VADUserStartedSpeakingFrame())
        if stage == "request":
            await asyncio.wait_for(tts.cancelled.wait(), timeout=3)
        await running.send(VADUserStoppedSpeakingFrame(stop_secs=0.6))
        await running.send(TranscriptionFrame(FOLLOW_UP, "user", "new", finalized=True))
        await wait_new_answer(running.recorder)
        assert not running.task.done(), "插话取消挂断后应继续通话"
        assert tts.requests == [GOODBYE, NEW_ANSWER]
        assert len(running.requests) == 2
        next_context = running.requests[1]["messages"]
        assert next_context[-1]["role"] == "user"
        assert next_context[-1]["content"].endswith(FOLLOW_UP)
        assistant_texts = [message["content"] for message in next_context
                           if message["role"] == "assistant"
                           and isinstance(message.get("content"), str)]
        assert any(message["role"] == "system"
                   and str(message.get("content", "")).startswith(INTERRUPTED_BACKGROUND_LABEL)
                   and GOODBYE in message["content"] for message in next_context)
        assert all(GOODBYE not in text for text in assistant_texts)
        assert running.recorder.messages[-1].text == NEW_ANSWER
        assert all(message.interrupted for message in running.recorder.messages
                   if message.role == "assistant" and message.text != NEW_ANSWER)
        await asyncio.sleep(0.08)
        assert running.transport.outgoing.played.count(1) <= previous_audio + 1
        assert not running.task.done()
        assert not running.recorder.errors
    finally:
        await running.close()


def controller_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[HangupController, SimpleNamespace, LLMContext, FunctionCallParams, AsyncMock]:
    """边界测试只隔离框架启动；实际队列与音频顺序由上述管线验证。"""
    context = LLMContext([{"role": "user", "content": USER_END}])
    activity = SimpleNamespace(generation=0, request_generation=0, user_speaking=False)
    on_hangup = AsyncMock()
    controller = HangupController(context=context, activity=activity, on_hangup=on_hangup)
    monkeypatch.setattr(FrameProcessor, "process_frame", AsyncMock())
    monkeypatch.setattr(controller, "push_frame", AsyncMock())
    params = FunctionCallParams(
        function_name="hang_up",
        tool_call_id="hangup-unit",
        arguments={"confirmed": True, "evidence": USER_END, "goodbye": GOODBYE},
        llm=cast(Any, SimpleNamespace(push_frame=AsyncMock())),
        pipeline_worker=cast(PipelineWorker, object()),
        context=context,
        result_callback=AsyncMock(),
    )
    return controller, activity, context, params, on_hangup


@pytest.mark.parametrize(
    "arguments",
    [
        {"confirmed": False, "evidence": USER_END, "goodbye": GOODBYE},
        {"confirmed": "true", "evidence": USER_END, "goodbye": GOODBYE},
        {"evidence": USER_END, "goodbye": GOODBYE},
        {"confirmed": True, "evidence": "", "goodbye": GOODBYE},
        {"confirmed": True, "evidence": "用户之前说过再见。", "goodbye": GOODBYE},
        {"confirmed": True, "evidence": USER_END, "goodbye": ""},
        {"confirmed": True, "evidence": USER_END, "goodbye": None},
    ],
    ids=["not-confirmed", "string-confirmed", "missing-confirmation", "empty-evidence",
         "stale-evidence", "empty-goodbye", "invalid-goodbye"],
)
async def test_invalid_tool_arguments_do_not_play_goodbye_or_hang_up(
    monkeypatch: pytest.MonkeyPatch, arguments: dict[str, Any],
) -> None:
    controller, _, _, params, on_hangup = controller_fixture(monkeypatch)
    params.arguments = arguments
    await controller.handle(params)
    cast(AsyncMock, params.llm.push_frame).assert_not_awaited()
    on_hangup.assert_not_awaited()


@pytest.mark.parametrize("changed", ["speaking", "request-generation"])
async def test_old_tool_cannot_run_while_user_speaks_or_new_asr_is_pending(
    monkeypatch: pytest.MonkeyPatch, changed: str,
) -> None:
    controller, activity, _, params, on_hangup = controller_fixture(monkeypatch)
    if changed == "speaking":
        activity.user_speaking = True
    else:
        activity.generation = 1
    await controller.handle(params)
    cast(AsyncMock, params.llm.push_frame).assert_not_awaited()
    on_hangup.assert_not_awaited()


async def test_duplicate_tool_and_marker_do_not_repeat_goodbye_or_hangup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller, _, _, params, on_hangup = controller_fixture(monkeypatch)
    await controller.handle(params)
    await controller.handle(params)
    frames = [call.args[0] for call in cast(AsyncMock, params.llm.push_frame).await_args_list]
    assert len(frames) == 2
    assert isinstance(frames[0], TTSSpeakFrame)
    assert frames[0].text == GOODBYE and frames[0].append_to_context
    assert isinstance(frames[1], HangupAfterPlaybackFrame)
    properties = cast(AsyncMock, params.result_callback).await_args_list[0].kwargs["properties"]
    assert isinstance(properties, FunctionCallResultProperties) and properties.run_llm is False
    on_hangup.assert_not_awaited()
    await controller.process_frame(frames[1], FrameDirection.DOWNSTREAM)
    await controller.process_frame(frames[1], FrameDirection.DOWNSTREAM)
    on_hangup.assert_awaited_once()


@pytest.mark.parametrize("late_frame", [VADUserStartedSpeakingFrame, InterruptionFrame])
async def test_late_start_frame_from_same_user_turn_keeps_confirmed_hangup(
    monkeypatch: pytest.MonkeyPatch, late_frame: type[Frame],
) -> None:
    controller, activity, _, params, on_hangup = controller_fixture(monkeypatch)
    activity.generation = activity.request_generation = 1
    await controller.handle(params)
    frames = [call.args[0] for call in cast(AsyncMock, params.llm.push_frame).await_args_list]
    marker = next(frame for frame in frames if isinstance(frame, HangupAfterPlaybackFrame))
    # 本轮用户已经停口，工具候选有效；开始帧只是在输出端迟到。
    await controller.process_frame(late_frame(), FrameDirection.DOWNSTREAM)
    on_hangup.assert_not_awaited()
    await controller.process_frame(marker, FrameDirection.DOWNSTREAM)
    await controller.process_frame(marker, FrameDirection.DOWNSTREAM)
    on_hangup.assert_awaited_once()


@pytest.mark.parametrize("late_frame", [VADUserStartedSpeakingFrame, InterruptionFrame])
@pytest.mark.parametrize("changed", ["generation", "speaking"])
async def test_new_user_activity_still_cancels_hangup_on_start_frame(
    monkeypatch: pytest.MonkeyPatch, late_frame: type[Frame], changed: str,
) -> None:
    controller, activity, _, params, on_hangup = controller_fixture(monkeypatch)
    await controller.handle(params)
    frames = [call.args[0] for call in cast(AsyncMock, params.llm.push_frame).await_args_list]
    marker = next(frame for frame in frames if isinstance(frame, HangupAfterPlaybackFrame))
    if changed == "generation":
        activity.generation += 1
    else:
        activity.user_speaking = True
    await controller.process_frame(late_frame(), FrameDirection.DOWNSTREAM)
    # 新 VAD 插话已撤销候选；即使用户随后停口，旧音频尾标记不能复活挂断。
    activity.user_speaking = False
    await controller.process_frame(marker, FrameDirection.DOWNSTREAM)
    on_hangup.assert_not_awaited()


@pytest.mark.parametrize("changed", ["generation", "speaking", "latest-evidence", "marker"])
async def test_stale_playback_marker_does_not_end_current_conversation(
    monkeypatch: pytest.MonkeyPatch, changed: str,
) -> None:
    controller, activity, context, params, on_hangup = controller_fixture(monkeypatch)
    await controller.handle(params)
    frames = [call.args[0] for call in cast(AsyncMock, params.llm.push_frame).await_args_list]
    marker = next(frame for frame in frames if isinstance(frame, HangupAfterPlaybackFrame))
    if changed == "generation":
        activity.generation += 1
    elif changed == "speaking":
        activity.user_speaking = True
    elif changed == "latest-evidence":
        context.add_message({"role": "user", "content": FOLLOW_UP})
    else:
        marker = HangupAfterPlaybackFrame(tool_call_id="unknown-tool", generation=0)
    await controller.process_frame(marker, FrameDirection.DOWNSTREAM)
    on_hangup.assert_not_awaited()


class EndingVoice:
    """模拟告别已播放后发出 ending，再与 REST 断连收尾竞争。"""

    def __init__(self, callbacks: VoiceCallbacks) -> None:
        self.callbacks = callbacks
        self.ending = asyncio.Event()
        self.finish = asyncio.Event()

    async def run(self) -> str:
        await self.callbacks.on_state("ending")
        self.ending.set()
        await self.finish.wait()
        await self.callbacks.on_message("assistant", GOODBYE, False, "2026-10-04T09:00:00Z")
        return "ai_hangup"

    async def stop(self, reason: str = "user_hangup") -> None:
        self.finish.set()


async def test_manager_preserves_ai_hangup_when_disconnect_rest_races_final_save(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path)
    await store.save_settings(configured_settings())
    handler = LocalHandler()
    instances: list[EndingVoice] = []

    def factory(
        connection: SmallWebRTCConnection, settings: AppSettings, callbacks: VoiceCallbacks
    ) -> VoiceRuntime:
        instance = EndingVoice(callbacks)
        instances.append(instance)
        return instance

    manager = SessionManager(
        store,
        voice_factory=factory,
        handler_factory=lambda: cast(SmallWebRTCRequestHandler, handler),
    )
    call_id = await connect(manager)
    try:
        async with asyncio.timeout(3):
            while not instances:
                await asyncio.sleep(0)
            await instances[0].ending.wait()
        result = await manager.end(call_id, "connection_lost")
        assert result.end_reason == "ai_hangup"
        assert result.status == "ended"
        assert result.transcript[-1].text == GOODBYE
        assert not result.transcript[-1].interrupted
        assert handler.closed and handler.connection.disconnected
        assert await manager.active_record() is None
        assert await store.get_call(call_id) == result
        assert (await manager.start())["call"]["id"] != call_id
    finally:
        for instance in instances:
            instance.finish.set()
        await manager.close()
