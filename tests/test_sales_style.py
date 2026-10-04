"""通过真实 SDK 请求检查普通回复和挽留都收到直接推销规则。"""

from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx2
import pytest
from pipecat.processors.aggregators.llm_context import LLMContext
from test_providers import sse_text

from llmautotel.hangup import HangupController
from llmautotel.models import AppSettings, LLMSettings
from llmautotel.providers import CompatibleLLMService
from llmautotel.voice import sales_prompt


@pytest.mark.parametrize("fixed_opening", [False, True])
@pytest.mark.parametrize(
    ("goal", "product_info", "answer"),
    [
        ("购买示例笔记工具", "笔记可导出 PDF。", "它可以导出 PDF，考虑购买吗？"),
        ("订阅示例资料服务", "每周提供资料汇总。", "它可以每周汇总资料，考虑订阅吗？"),
        ("预约示例产品演示", "可现场演示订单管理。", "您可以现场看订单管理流程，考虑预约演示吗？"),
    ],
)
async def test_sdk_receives_sales_style_for_configured_goal_and_retention(
    fixed_opening: bool, goal: str, product_info: str, answer: str,
) -> None:
    bodies: list[dict[str, Any]] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        bodies.append(json.loads(await request.aread()))
        return httpx2.Response(
            200, headers={"content-type": "text/event-stream"},
            content=sse_text(answer) + b"data: [DONE]\n\n",
        )

    settings = AppSettings()
    settings.sales.goal = goal
    settings.sales.product_info = product_info
    settings.sales.instructions = "语气自然，每轮一句话。"
    settings.sales.opening = f"您好，考虑{goal}吗？{product_info}" if fixed_opening else ""
    context = LLMContext([
        {"role": "system", "content": sales_prompt(settings)},
        {"role": "user", "content": "有什么功能？"},
    ])
    controller = HangupController(
        context, SimpleNamespace(generation=0, request_generation=0, user_speaking=False),
        AsyncMock(),
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
            text = "".join([chunk.choices[0].delta.content or "" async for chunk in stream])
            assert text == answer
        assert context.get_messages() == original
        request = bodies[0]
        assert request["messages"][-1] == {"role": "user", "content": "有什么功能？"}
        prompt = request["messages"][0]["content"]
        assert settings.sales.goal in prompt and settings.sales.product_info in prompt
        assert settings.sales.instructions in prompt
        assert "直接推销" in prompt and "不要调查" in prompt
        assert all(word in prompt for word in ("用途", "使用场景", "频率", "职业", "痛点"))
        assert "行动与配置目标一致" in prompt
        assert "考虑 + 目标行动 + 吗" in prompt
        assert "没有实际工具支持" in prompt
        assert "ChatGPT" not in prompt and "Plus" not in prompt
        tools = {tool["function"]["name"]: tool["function"] for tool in request["tools"]}
        retention = tools["retain_once"]["parameters"]["properties"]["reply"]["description"]
        assert "产品实际价值" in retention and "配置目标" in retention
        assert "考虑 + 配置目标行动 + 吗" in retention
        assert "不调查" in retention and "不询问是否挂断" in retention
        assert "没有实际工具支持" in retention
        assert tools["hang_up"]["parameters"]["properties"]["intent"]["enum"] == [
            "direct_exit", "purchase_refusal",
        ]
    finally:
        await service.aclose()
