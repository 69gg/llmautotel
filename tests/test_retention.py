"""首次购买拒绝只挽留一次，随后明确拒绝或直接要求结束时告别。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import Any, Literal, cast
from unittest.mock import AsyncMock

import httpx2
import pytest
from pipecat.frames.frames import (
    Frame,
    InterruptionFrame,
    LLMAssistantPushAggregationFrame,
    TTSAudioRawFrame,
    TTSSpeakFrame,
    VADUserStartedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from test_hangup import (
    FOLLOW_UP,
    GOODBYE,
    NEW_ANSWER,
    GoodbyeTTS,
    RunningHangup,
    controller_fixture,
    tool_response,
    wait_new_answer,
)
from test_interruption_context import SDKServices
from test_providers import sse_text
from test_voice import LocalTransport, Passthrough, Recorder

import llmautotel.voice as voice_module
from llmautotel.conversation import INTERRUPTED_BACKGROUND_LABEL
from llmautotel.hangup import HangupAfterPlaybackFrame
from llmautotel.models import AppSettings, LLMSettings
from llmautotel.providers import CompatibleLLMService
from llmautotel.voice import VoiceSession

REFUSAL = "不需要，谢谢。"
RETAIN = "理解，订阅可以节省整理资料的时间，您可以先看看是否适合自己的使用习惯。"


class RetentionTTS(GoodbyeTTS):
    def __init__(self, mode: Literal["complete", "request", "playback"]) -> None:
        super().__init__("request" if mode == "request" else "complete")
        self.retention_mode = mode

    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame, None]:
        if text == RETAIN and self.retention_mode == "playback":
            self.requests.append(text)
            yield TTSAudioRawFrame(
                audio=(2).to_bytes(2, "little") * 14400,
                sample_rate=24000,
                num_channels=1,
                context_id=context_id,
            )
            self.generated.set()
            return
        async for frame in super().run_tts(text, context_id):
            yield frame


def refusal_hangup(evidence: str = REFUSAL) -> dict[str, Any]:
    return {
        "intent": "purchase_refusal",
        "confirmed": True,
        "evidence": evidence,
        "goodbye": GOODBYE,
    }


async def running_retention(
    monkeypatch: pytest.MonkeyPatch,
    replies: list[bytes],
    *,
    mode: Literal["complete", "request", "playback"] = "complete",
) -> RunningHangup:
    requests: list[dict[str, Any]] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(json.loads(await request.aread()))
        assert len(requests) <= len(replies), "出现未计划的额外模型请求"
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=replies[len(requests) - 1],
        )

    recorder = Recorder()
    transport = LocalTransport(recorder)
    llm = CompatibleLLMService(
        LLMSettings(base_url="http://fixture.invalid/v1", model="controlled-llm"),
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )
    services = SDKServices(Passthrough(), llm, cast(Any, RetentionTTS(mode)))
    monkeypatch.setattr(voice_module, "create_services", lambda settings: services)
    monkeypatch.setattr(voice_module, "SmallWebRTCTransport", lambda **kwargs: transport)
    settings = AppSettings()
    settings.conversation.mode = "sales"
    settings.sales.goal = "订阅测试产品"
    settings.sales.product_info = "测试产品可以整理资料。"
    session = VoiceSession(cast(SmallWebRTCConnection, object()), settings, recorder.callbacks())
    task = asyncio.create_task(session.run())
    await asyncio.wait_for(transport.outgoing.started.wait(), timeout=3)
    return RunningHangup(session, task, transport, recorder, services, requests)


async def wait_retention(recorder: Recorder) -> None:
    async with asyncio.timeout(3):
        while not any(message.text == RETAIN for message in recorder.messages):
            recorder.changed.clear()
            await recorder.changed.wait()


async def test_first_refusal_recovers_from_premature_hangup_then_second_refusal_ends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    running = await running_retention(
        monkeypatch,
        [
            tool_response(refusal_hangup(), tool_id="premature"),
            tool_response(
                {"evidence": REFUSAL, "reply": RETAIN},
                tool_id="retention",
                function_name="retain_once",
            ),
            tool_response(refusal_hangup(), tool_id="confirmed-second"),
        ],
    )
    try:
        await running.user(REFUSAL)
        await wait_retention(running.recorder)
        assert not running.task.done()
        assert running.services.tts.requests == [RETAIN]
        assert running.transport.outgoing.played.count(2) == 1
        assert running.transport.outgoing.played.count(1) == 0
        assert len(running.requests) == 2
        assert running.requests[1]["messages"][-1]["role"] == "tool"
        result = json.loads(running.requests[1]["messages"][-1]["content"])
        assert result["accepted"] is False and result["retention_used"] is False
        # 即使第二次原话完全相同，也必须是新的用户回合。
        await running.user(REFUSAL)
        assert await asyncio.wait_for(running.task, timeout=3) == "ai_hangup"
        assert running.services.tts.requests == [RETAIN, GOODBYE]
        assert running.transport.outgoing.played.count(1) == 15
        assert [message.text for message in running.recorder.messages] == [
            REFUSAL,
            RETAIN,
            REFUSAL,
            GOODBYE,
        ]
        schema = running.requests[2]["tools"]
        assert all("已使用一次挽留" in tool["function"]["description"] for tool in schema)
        assert not running.recorder.errors
    finally:
        await running.close()


async def test_retention_keeps_conversation_open_for_new_question_before_later_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    running = await running_retention(
        monkeypatch,
        [
            tool_response(
                {"evidence": REFUSAL, "reply": RETAIN},
                tool_id="retention",
                function_name="retain_once",
            ),
            sse_text(NEW_ANSWER) + b"data: [DONE]\n\n",
            tool_response(refusal_hangup(), tool_id="confirmed-second"),
        ],
    )
    try:
        await running.user(REFUSAL)
        await wait_retention(running.recorder)
        await running.user(FOLLOW_UP)
        await wait_new_answer(running.recorder)
        assert not running.task.done()
        assert running.services.tts.requests == [RETAIN, NEW_ANSWER]
        assert running.requests[1]["messages"][-1] == {"role": "user", "content": FOLLOW_UP}
        await running.user(REFUSAL)
        assert await asyncio.wait_for(running.task, timeout=3) == "ai_hangup"
        assert running.services.tts.requests == [RETAIN, NEW_ANSWER, GOODBYE]
        assert not running.recorder.errors
    finally:
        await running.close()


@pytest.mark.parametrize("stage", ["request", "playback"])
async def test_interrupted_retention_is_not_replayed_even_if_model_calls_tool_again(
    monkeypatch: pytest.MonkeyPatch,
    stage: Literal["request", "playback"],
) -> None:
    running = await running_retention(
        monkeypatch,
        [
            tool_response(
                {"evidence": REFUSAL, "reply": RETAIN},
                tool_id="retention",
                function_name="retain_once",
            ),
            tool_response(
                {"evidence": FOLLOW_UP, "reply": RETAIN},
                tool_id="illegal-repeat",
                function_name="retain_once",
            ),
            sse_text(NEW_ANSWER) + b"data: [DONE]\n\n",
            tool_response(refusal_hangup(), tool_id="confirmed-second"),
        ],
        mode=stage,
    )
    tts = cast(RetentionTTS, running.services.tts)
    try:
        await running.user(REFUSAL)
        if stage == "request":
            await asyncio.wait_for(tts.waiting.wait(), timeout=3)
        else:
            await asyncio.wait_for(tts.generated.wait(), timeout=3)
            await asyncio.wait_for(running.transport.outgoing.audio_written.wait(), timeout=3)
        previous_audio = running.transport.outgoing.played.count(2)
        await running.user(FOLLOW_UP)
        if stage == "request":
            await asyncio.wait_for(tts.cancelled.wait(), timeout=3)
        await wait_new_answer(running.recorder)
        assert tts.requests == [RETAIN, NEW_ANSWER], "已取消挽留不能再次请求 TTS"
        assert running.transport.outgoing.played.count(2) <= previous_audio + 2
        assert all(message.text != RETAIN for message in running.recorder.messages)
        result = json.loads(running.requests[2]["messages"][-1]["content"])
        assert result["accepted"] is False and result["retention_used"] is True
        assert not running.task.done()
        await running.user(REFUSAL)
        assert await asyncio.wait_for(running.task, timeout=3) == "ai_hangup"
        assert tts.requests == [RETAIN, NEW_ANSWER, GOODBYE]
        assert not running.recorder.errors
    finally:
        await running.close()


async def test_same_user_turn_cannot_spend_retention_then_claim_second_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller, _, context, params, on_hangup = controller_fixture(monkeypatch)
    context.set_messages([{"role": "user", "content": REFUSAL}])
    params.function_name = "retain_once"
    params.tool_call_id = "retention"
    params.arguments = {"evidence": REFUSAL, "reply": RETAIN}
    await controller.retain(params)
    params.function_name = "hang_up"
    params.tool_call_id = "same-turn-hangup"
    params.arguments = refusal_hangup()
    await controller.handle(params)
    frames = [call.args[0] for call in cast(AsyncMock, params.llm.push_frame).await_args_list]
    assert len(frames) == 2 and isinstance(frames[0], TTSSpeakFrame)
    assert isinstance(frames[1], LLMAssistantPushAggregationFrame)
    assert frames[0].text == RETAIN
    results = cast(AsyncMock, params.result_callback).await_args_list
    assert results[-1].args[0]["accepted"] is False
    assert results[-1].kwargs["properties"].run_llm is False
    on_hangup.assert_not_awaited()


@pytest.mark.parametrize("frame_type", [InterruptionFrame, VADUserStartedSpeakingFrame])
async def test_interruptions_do_not_reset_once_budget_and_new_call_starts_fresh(
    monkeypatch: pytest.MonkeyPatch,
    frame_type: type[Frame],
) -> None:
    controller, activity, context, params, _ = controller_fixture(monkeypatch)
    context.set_messages([{"role": "user", "content": REFUSAL}])
    params.function_name = "retain_once"
    params.tool_call_id = "retention"
    params.arguments = {"evidence": REFUSAL, "reply": RETAIN}
    await controller.retain(params)
    activity.generation += 1
    await controller.process_frame(frame_type(), FrameDirection.DOWNSTREAM)
    activity.request_generation = activity.generation
    context.add_message({"role": "user", "content": REFUSAL})
    params.tool_call_id = "repeat"
    await controller.retain(params)
    assert cast(AsyncMock, params.llm.push_frame).await_count == 2
    result = cast(AsyncMock, params.result_callback).await_args_list[-1].args[0]
    assert result["accepted"] is False and result["retention_used"] is True
    # 新会话有独立 controller，绝不把旧会话的挽留次数带入。
    fresh, _, fresh_context, fresh_params, _ = controller_fixture(monkeypatch)
    fresh_context.set_messages([{"role": "user", "content": REFUSAL}])
    fresh_params.arguments = {"evidence": REFUSAL, "reply": RETAIN}
    await fresh.retain(fresh_params)
    assert cast(AsyncMock, fresh_params.llm.push_frame).await_count == 2


@pytest.mark.parametrize(
    "arguments",
    [
        {"evidence": "旧原话", "reply": RETAIN},
        {"evidence": REFUSAL, "reply": ""},
        {"evidence": REFUSAL, "reply": None},
        {"evidence": "", "reply": RETAIN},
    ],
)
async def test_invalid_retention_does_not_consume_budget(
    monkeypatch: pytest.MonkeyPatch,
    arguments: dict[str, Any],
) -> None:
    controller, _, context, params, _ = controller_fixture(monkeypatch)
    context.set_messages([{"role": "user", "content": REFUSAL}])
    params.arguments = arguments
    await controller.retain(params)
    cast(AsyncMock, params.llm.push_frame).assert_not_awaited()
    params.arguments = {"evidence": REFUSAL, "reply": RETAIN}
    await controller.retain(params)
    assert cast(AsyncMock, params.llm.push_frame).await_count == 2


async def test_direct_exit_after_retention_still_uses_farewell_playback_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller, _, context, params, on_hangup = controller_fixture(monkeypatch)
    context.set_messages([{"role": "user", "content": REFUSAL}])
    params.arguments = {"evidence": REFUSAL, "reply": RETAIN}
    await controller.retain(params)
    direct_exit = "别再打扰，请结束通话。"
    context.add_message({"role": "user", "content": direct_exit})
    params.tool_call_id = "direct-exit"
    params.arguments = {
        "intent": "direct_exit",
        "confirmed": True,
        "evidence": direct_exit,
        "goodbye": GOODBYE,
    }
    await controller.handle(params)
    frames = [call.args[0] for call in cast(AsyncMock, params.llm.push_frame).await_args_list]
    assert [frame.text for frame in frames if isinstance(frame, TTSSpeakFrame)] == [RETAIN, GOODBYE]
    on_hangup.assert_not_awaited()
    marker = next(frame for frame in frames if isinstance(frame, HangupAfterPlaybackFrame))
    await controller.process_frame(marker, FrameDirection.DOWNSTREAM)
    on_hangup.assert_awaited_once()


@pytest.mark.parametrize("intent", [None, "uncertain", "purchase_refusal"])
async def test_missing_unknown_or_unretained_purchase_intent_cannot_hang_up(
    monkeypatch: pytest.MonkeyPatch,
    intent: str | None,
) -> None:
    controller, _, context, params, on_hangup = controller_fixture(monkeypatch)
    context.set_messages([{"role": "user", "content": REFUSAL}])
    params.arguments = {
        "confirmed": True,
        "evidence": REFUSAL,
        "goodbye": GOODBYE,
        "intent": intent,
    }
    await controller.handle(params)
    cast(AsyncMock, params.llm.push_frame).assert_not_awaited()
    on_hangup.assert_not_awaited()


@pytest.mark.parametrize("changed", ["speaking", "new-generation"])
async def test_outdated_retention_cannot_consume_the_new_user_turn_budget(
    monkeypatch: pytest.MonkeyPatch,
    changed: str,
) -> None:
    controller, activity, context, params, _ = controller_fixture(monkeypatch)
    context.set_messages([{"role": "user", "content": REFUSAL}])
    params.arguments = {"evidence": REFUSAL, "reply": RETAIN}
    if changed == "speaking":
        activity.user_speaking = True
    else:
        activity.generation += 1
    await controller.retain(params)
    cast(AsyncMock, params.llm.push_frame).assert_not_awaited()
    activity.user_speaking = False
    activity.request_generation = activity.generation
    await controller.retain(params)
    assert cast(AsyncMock, params.llm.push_frame).await_count == 2


@pytest.mark.parametrize("same_id", [True, False])
async def test_duplicate_retention_calls_play_exactly_one_reply(
    monkeypatch: pytest.MonkeyPatch,
    same_id: bool,
) -> None:
    controller, _, context, params, _ = controller_fixture(monkeypatch)
    context.set_messages([{"role": "user", "content": REFUSAL}])
    params.arguments = {"evidence": REFUSAL, "reply": RETAIN}
    await controller.retain(params)
    if not same_id:
        params.tool_call_id = "parallel-second-retention"
    await controller.retain(params)
    assert cast(AsyncMock, params.llm.push_frame).await_count == 2
    last_result = cast(AsyncMock, params.result_callback).await_args_list[-1]
    assert last_result.args[0]["accepted"] is False
    assert last_result.kwargs["properties"].run_llm is False


async def test_internal_label_only_retention_does_not_spend_once_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller, _, context, params, _ = controller_fixture(monkeypatch)
    context.set_messages([{"role": "user", "content": REFUSAL}])
    params.arguments = {"evidence": REFUSAL, "reply": INTERRUPTED_BACKGROUND_LABEL}
    await controller.retain(params)
    cast(AsyncMock, params.llm.push_frame).assert_not_awaited()
    params.arguments["reply"] = f"{INTERRUPTED_BACKGROUND_LABEL}\n{RETAIN}"
    await controller.retain(params)
    frames = [call.args[0] for call in cast(AsyncMock, params.llm.push_frame).await_args_list]
    assert isinstance(frames[0], TTSSpeakFrame) and frames[0].text == RETAIN


async def test_internal_label_only_goodbye_does_not_hang_up_without_farewell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller, _, _, params, on_hangup = controller_fixture(monkeypatch)
    params.arguments["goodbye"] = INTERRUPTED_BACKGROUND_LABEL
    await controller.handle(params)
    cast(AsyncMock, params.llm.push_frame).assert_not_awaited()
    on_hangup.assert_not_awaited()
