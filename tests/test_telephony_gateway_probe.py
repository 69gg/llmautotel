"""无活动通话的已认证 SSE 协议探测，不执行模型或创建电话。"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest
from test_telephony_gateway import configured_settings

from llmautotel.telephony.cloud_inbound import authenticated_gateway_probe


@pytest.mark.parametrize("provider", ["aliyun", "tencent"])
def test_authentication_only_probe_returns_complete_sse_without_model_or_call(
    provider: str,
) -> None:
    settings = configured_settings()
    getattr(settings.telephony, provider).inbound_enabled = True
    body = {
        "model": "platform-model",
        "messages": [
            {"role": "assistant", "content": "这是一个测试开场白"},
            {"role": "user", "content": "你是谁？"},
        ],
        "stream": True,
    }
    authorization = "Bearer TEST-GATEWAY-TOKEN" if provider == "tencent" else "TEST-GATEWAY-TOKEN"
    with (
        patch(
            "llmautotel.providers.CompatibleLLMService", side_effect=AssertionError("模型不得创建")
        ),
        patch(
            "llmautotel.telephony.cloud.AliyunCallClient.start",
            side_effect=AssertionError("不得拨号"),
        ),
        patch(
            "llmautotel.telephony.cloud.TencentCallClient.start",
            side_effect=AssertionError("不得拨号"),
        ),
    ):
        response = authenticated_gateway_probe(settings, provider, body, authorization)
    assert response is not None
    assert response[-1] == "data: [DONE]\n\n"
    first, last = [json.loads(chunk.removeprefix("data: ")) for chunk in response[:-1]]
    assert first["object"] == "chat.completion.chunk"
    assert first["model"] == settings.llm.model and first["id"] == last["id"]
    assert first["choices"][0]["delta"]["role"] == "assistant"
    assert "未调用模型或建立电话" in first["choices"][0]["delta"]["content"]
    assert last["choices"][0]["finish_reason"] == "stop"
    assert "PRIVATE" not in str(response) and "TOKEN" not in str(response)


@pytest.mark.parametrize("provider", ["aliyun", "tencent"])
def test_probe_is_disabled_without_both_switches_and_exact_gateway_authorization(
    provider: str,
) -> None:
    settings = configured_settings()
    config = getattr(settings.telephony, provider)
    body = {"stream": True, "messages": [{"role": "user", "content": "测试"}]}
    authorization = "Bearer TEST-GATEWAY-TOKEN" if provider == "tencent" else "TEST-GATEWAY-TOKEN"
    assert authenticated_gateway_probe(settings, provider, body, authorization) is None
    config.inbound_enabled = True
    assert authenticated_gateway_probe(settings, provider, body, "wrong") is None
    assert authenticated_gateway_probe(settings, provider, body, None) is None
    config.enabled = False
    assert authenticated_gateway_probe(settings, provider, body, authorization) is None


@pytest.mark.parametrize(
    "change",
    [
        {"call_id": "actual-call"},
        {"session_id": ""},
        {"SessionId": None},
        {"out_id": "old-call"},
        {"biz_params": {}},
        {"stream": False},
        {"messages": []},
        {"messages": [{"role": "tool", "content": "test"}]},
        {"messages": [{"role": "user", "content": "[llmautotel_call_id:local-id]"}]},
        {"messages": [{"role": "system", "content": "[llmautotel_call_id:${llmautotel_call_id}]"}]},
        {"tools": [{"type": "function", "function": {"name": "call_end"}}]},
    ],
)
def test_bound_malformed_nonstream_or_tool_requests_cannot_be_silently_treated_as_probe(
    change: dict[str, Any],
) -> None:
    settings = configured_settings()
    settings.telephony.tencent.inbound_enabled = True
    body = {"stream": True, "messages": [{"role": "user", "content": "测试"}]} | change
    assert (
        authenticated_gateway_probe(settings, "tencent", body, "Bearer TEST-GATEWAY-TOKEN") is None
    )
