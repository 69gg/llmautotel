"""咨询模式与销售配置隔离，实际 SDK 工具编码、欢迎和告别播放验证。"""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import httpx2
import pytest
from pipecat.frames.frames import (
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from test_hangup import (
    FOLLOW_UP,
    GOODBYE,
    USER_END,
    GoodbyeTTS,
    controller_fixture,
    running_hangup,
    wait_new_answer,
)
from test_providers import sse_text
from test_voice import start_session, wait_messages

from llmautotel.hangup import HangupController
from llmautotel.models import AppSettings, LLMSettings
from llmautotel.providers import CompatibleLLMService
from llmautotel.telephony.cloud import cloud_sales_prompt
from llmautotel.voice import conversation_prompt


def configured_modes() -> AppSettings:
    settings = AppSettings()
    settings.consultation.product_info = "示例文档工具支持导出 PDF，导出入口在文件菜单。"
    settings.consultation.instructions = "遇到未说明的价格时明确说暂无法确认。"
    settings.consultation.opening = "您好，欢迎咨询文档工具，请问有什么可以帮您？"
    settings.sales.goal = "预约旧演示"
    settings.sales.product_info = "旧销售资料"
    settings.sales.instructions = "旧销售要求"
    settings.sales.opening = "旧销售开场"
    return settings


def test_mode_switch_preserves_both_configurations_and_checks_only_active_fields() -> None:
    settings = configured_modes()
    assert settings.conversation.mode == "consultation"
    assert settings.active_conversation is settings.consultation
    assert settings.missing_conversation_fields() == []
    serialized = settings.private()
    assert "active_conversation" not in serialized
    restored = AppSettings.model_validate(serialized)
    assert restored.public() == settings.public()
    restored.conversation.mode = "sales"
    assert restored.active_conversation is restored.sales
    assert restored.consultation == settings.consultation
    restored.sales.goal = ""
    restored.sales.product_info = ""
    assert restored.missing_conversation_fields() == ["销售目标", "产品资料"]
    restored.conversation.mode = "consultation"
    assert restored.missing_conversation_fields() == []
    restored.consultation.product_info = ""
    assert restored.missing_conversation_fields() == ["产品资料"]


async def test_sdk_consultation_uses_facts_and_only_confirmed_end_tool() -> None:
    settings = configured_modes()
    bodies: list[dict[str, Any]] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        bodies.append(json.loads(await request.aread()))
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse_text("在文件菜单选择导出 PDF。") + b"data: [DONE]\n\n",
        )

    context = LLMContext(
        [
            {"role": "system", "content": conversation_prompt(settings)},
            {"role": "user", "content": "怎么导出文件？"},
        ]
    )
    controller = HangupController(
        context,
        SimpleNamespace(generation=0, request_generation=0, user_speaking=False),
        AsyncMock(),
        mode=settings.conversation.mode,
    )
    context.set_tools(controller.tools())
    original = deepcopy(context.get_messages())
    service = CompatibleLLMService(
        LLMSettings(base_url="http://fixture.invalid/v1", model="controlled-llm"),
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )
    try:
        stream = await service.get_chat_completions(context)
        async with stream:
            assert (
                "".join([chunk.choices[0].delta.content or "" async for chunk in stream])
                == "在文件菜单选择导出 PDF。"
            )
        assert context.get_messages() == original
        prompt = bodies[0]["messages"][0]["content"]
        assert settings.consultation.product_info in prompt
        assert settings.consultation.instructions in prompt
        assert all(value not in prompt for value in settings.sales.model_dump().values())
        assert "不主动推销" in prompt and "不做销售挽留" in prompt
        assert "澄清问题" in prompt and "资料未说明" in prompt
        assert "不要自动续讲" in prompt and "最新消息" in prompt
        tools = bodies[0]["tools"]
        assert [tool["function"]["name"] for tool in tools] == ["hang_up"]
        assert tools[0]["function"]["parameters"]["properties"]["intent"]["enum"] == ["direct_exit"]
        assert "max_tokens" not in bodies[0] and "max_completion_tokens" not in bodies[0]
    finally:
        await service.aclose()


@pytest.mark.parametrize("provider", ["aliyun", "tencent"])
def test_cloud_consultation_prompt_uses_platform_end_protocol_without_sales_retention(
    provider: str,
) -> None:
    settings = configured_modes()
    prompt = cloud_sales_prompt(settings, cast(Any, provider))
    assert settings.consultation.product_info in prompt
    assert settings.sales.goal not in prompt and settings.sales.instructions not in prompt
    assert "拒绝购买" in prompt and "不能据此推断咨询已结束" in prompt
    assert "retain_once" not in prompt and "hang_up" not in prompt
    assert ("<hangup/>" in prompt) == (provider == "aliyun")
    assert ("call_end" in prompt) == (provider == "tencent")
    settings.conversation.mode = "sales"
    sales = cloud_sales_prompt(settings, cast(Any, provider))
    assert settings.sales.goal in sales and "首次明确拒绝" in sales
    assert settings.consultation.product_info not in sales


@pytest.mark.parametrize("fixed", [False, True])
async def test_consultation_welcome_once_then_answers_current_question(
    monkeypatch: pytest.MonkeyPatch, fixed: bool
) -> None:
    welcome = "您好，欢迎咨询文档工具。" if fixed else ""
    running = await start_session(monkeypatch, opening=welcome, conversation_mode="consultation")
    try:
        await running.ready()
        await running.ready()
        await wait_messages(running.recorder, 1)
        assert len(running.services.llm.inputs) == (0 if fixed else 1)
        if fixed:
            assert running.services.tts.requests == [welcome]
        await running.frame(VADUserStartedSpeakingFrame())
        await running.frame(VADUserStoppedSpeakingFrame(stop_secs=0.6))
        await running.frame(TranscriptionFrame("怎么导出 PDF？", "user", "fixture", finalized=True))
        await wait_messages(running.recorder, 3)
        current = running.services.llm.inputs[-1]
        assert "咨询产品支持导出 PDF" in current[0]["content"]
        assert "让用户订阅测试计划" not in current[0]["content"]
        assert current[-1]["content"] == "怎么导出 PDF？"
        if fixed:
            assert running.services.tts.requests.count(welcome) == 1
    finally:
        await running.close()


async def test_consultation_rejects_purchase_refusal_and_retention_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller, _, _, params, on_hangup = controller_fixture(monkeypatch, mode="consultation")
    params.arguments["intent"] = "purchase_refusal"
    await controller.handle(params)
    assert params.result_callback.await_args.args[0]["accepted"] is False
    params.arguments = {"evidence": USER_END, "reply": "考虑购买吗？"}
    await controller.retain(params)
    assert params.result_callback.await_args.args[0]["accepted"] is False
    params.llm.push_frame.assert_not_awaited()
    on_hangup.assert_not_awaited()
    assert controller._retention is None


async def test_consultation_goodbye_plays_completely_before_hangup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    running = await running_hangup(monkeypatch, conversation_mode="consultation")
    try:
        await running.user(USER_END)
        tts = cast(GoodbyeTTS, running.services.tts)
        await asyncio.wait_for(tts.generated.wait(), timeout=3)
        await asyncio.wait_for(running.transport.outgoing.audio_written.wait(), timeout=3)
        assert not running.task.done()
        assert [tool["function"]["name"] for tool in running.requests[0]["tools"]] == ["hang_up"]
        assert await asyncio.wait_for(running.task, timeout=3) == "ai_hangup"
        assert running.transport.outgoing.played.count(1) == 15
        assert tts.requests == [GOODBYE]
        assert len(running.requests) == 1
    finally:
        await running.close()


async def test_consultation_interrupting_goodbye_resumes_new_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    running = await running_hangup(monkeypatch, mode="request", conversation_mode="consultation")
    try:
        await running.user(USER_END)
        tts = cast(GoodbyeTTS, running.services.tts)
        await asyncio.wait_for(tts.waiting.wait(), timeout=3)
        await running.user(FOLLOW_UP)
        await wait_new_answer(running.recorder)
        assert tts.cancelled.is_set()
        assert not running.task.done()
        latest_user = next(
            message
            for message in reversed(running.requests[-1]["messages"])
            if message["role"] == "user"
        )
        assert latest_user["content"].endswith(FOLLOW_UP)
        assert running.transport.outgoing.played.count(1) == 0
        assert not running.recorder.errors
    finally:
        await running.close()
