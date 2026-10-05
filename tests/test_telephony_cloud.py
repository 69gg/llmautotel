"""真正官方 SDK 的签名/序列化受控验证，不连接供应商或拨打电话。"""

from __future__ import annotations

import json
import logging
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from alibabacloud_aiccs20191015 import models as aliyun_models
from darabonba.core import DaraCore
from darabonba.request import DaraRequest
from darabonba.response import DaraResponse
from pydantic import SecretStr

from llmautotel.conversation import INTERRUPTED_BACKGROUND_LABEL
from llmautotel.models import AppSettings, LLMSettings, SalesSettings
from llmautotel.telephony.base import TelephonyError
from llmautotel.telephony.cloud import (
    ALIYUN_HANGUP_TAG,
    AliyunCallClient,
    TencentCallClient,
    cloud_context_messages,
    cloud_sales_prompt,
    native_tencent_tools,
)
from llmautotel.telephony.settings import AliyunSettings, TencentSettings


@pytest.fixture
def settings() -> AppSettings:
    return AppSettings(
        conversation={"mode": "sales"},
        sales=SalesSettings(
            goal="订阅测试产品",
            product_info="每月10元，可整理资料。",
            instructions="直接进入主题",
            opening="您好，考虑订阅吗？",
        ),
        llm=LLMSettings(
            base_url="https://private-model.example/v1",
            model="private-model",
            api_key=SecretStr("PRIVATE-MODEL-KEY"),
            thinking="disabled",
            reasoning_effort="high",
        ),
    )


def aliyun_config(**changes: Any) -> AliyunSettings:
    fields: dict[str, Any] = {
        "enabled": True,
        "endpoint": "https://aiccs.test.example",
        "region": "cn-test",
        "app_id": "test-app",
        "caller_id": "12300000",
        "access_key_id": "TEST-ACCESS-ID",
        "access_key_secret": "TEST-ACCESS-SECRET",
        "gateway_token": "TEST-GATEWAY-TOKEN",
        "webhook_token": "TEST-WEBHOOK-TOKEN",
        "tts_voice": "custom-voice",
        "session_timeout": 600,
    }
    return AliyunSettings(**(fields | changes))


def tencent_config(**changes: Any) -> TencentSettings:
    fields: dict[str, Any] = {
        "enabled": True,
        "endpoint": "https://ccc.test.example",
        "region": "ap-test",
        "sdk_app_id": 1400000000,
        "caller_id": "008612300000",
        "secret_id": "TEST-SECRET-ID",
        "secret_key": "TEST-SECRET-KEY",
        "gateway_token": "TEST-GATEWAY-TOKEN",
        "webhook_token": "TEST-WEBHOOK-TOKEN",
        "tts_voice": "custom-voice",
        "vad_silence_ms": 500,
        "interrupt_speech_duration_ms": 300,
    }
    return TencentSettings(**(fields | changes))


@pytest.mark.asyncio
async def test_aliyun_signed_sdk_request_and_immediate_hangup(
    monkeypatch: pytest.MonkeyPatch,
    settings: AppSettings,
) -> None:
    requests: list[DaraRequest] = []

    async def capture(request: DaraRequest, runtime_option: dict[str, Any]) -> DaraResponse:
        requests.append(request)
        assert runtime_option["readTimeout"] == 30000
        assert request.protocol == "https"
        assert request.headers["host"] == "aiccs.test.example"
        assert request.headers["x-acs-version"] == "2019-10-15"
        assert request.headers["Authorization"].startswith("ACS3-HMAC-SHA256 ")
        response = DaraResponse()
        response.status_code = 200
        response.headers = {"content-type": "application/json"}
        response.body = json.dumps(
            {
                "Code": "OK",
                "CallId": "remote-ali",
                "RequestId": "request-1",
                "Result": True,
            }
        ).encode()
        return response

    monkeypatch.setattr(DaraCore, "async_do_action", capture)
    client = AliyunCallClient(aliyun_config())
    assert (
        await client.start("12300001", "local-id", settings, "https://our.example/aliyun/v1")
        == "remote-ali"
    )
    await client.hangup("remote-ali")
    assert len(requests) == 2
    start, stop = requests
    assert start.headers["x-acs-action"] == "LlmSmartCall"
    assert start.query["ApplicationCode"] == "test-app"
    assert start.query["CalledNumber"] == "12300001"
    assert start.query["CallerNumber"] == "12300000"
    assert start.query["OutId"] == "local-id"
    assert json.loads(start.query["BizParam"]) == {"call_id": "local-id"}
    assert start.query["TtsVoiceCode"] == "custom-voice"
    assert start.query["SessionTimeout"] == "600"
    assert "PRIVATE-MODEL-KEY" not in str(start.query)
    assert "TEST-GATEWAY-TOKEN" not in str(start.query)
    assert stop.headers["x-acs-action"] == "HangupOperate"
    assert stop.query["CallId"] == "remote-ali"
    assert stop.query["ImmediateHangup"].lower() == "true"
    await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("opening", ["您好，考虑订阅吗？", ""])
async def test_tencent_actual_sdk_signed_http_gateway_privacy_and_close(
    settings: AppSettings,
    opening: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings.sales.opening = opening
    client = TencentCallClient(tencent_config())
    sdk = client._client()
    await sdk.http_client.aclose()
    requests: list[httpx.Request] = []

    def capture(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.scheme == "https"
        assert request.url.host == "ccc.test.example"
        assert request.url.path == "/" and not request.url.query
        assert request.headers["X-TC-Version"] == "2020-02-10"
        assert request.headers["X-TC-Region"] == "ap-test"
        assert request.headers["Authorization"].startswith("TC3-HMAC-SHA256 ")
        return httpx.Response(
            200,
            json={
                "Response": {
                    "SessionId": "remote-tencent",
                    "RequestId": "request-1",
                }
            },
        )

    transport = httpx.AsyncClient(transport=httpx.MockTransport(capture))
    sdk.http_client = transport
    with caplog.at_level(logging.DEBUG):
        assert (
            await client.start(
                "008612300001", "local-id", settings, "https://our.example/tencent/v1"
            )
            == "remote-tencent"
        )
        await client.hangup("remote-tencent")
    await client.close()
    assert transport.is_closed
    assert client._sdk is None
    assert len(requests) == 2
    start, stop = [json.loads(request.content) for request in requests]
    assert requests[0].headers["X-TC-Action"] == "CreateAICall"
    assert start["SdkAppId"] == 1400000000
    assert start["Callee"] == "008612300001"
    assert start["Callers"] == ["008612300000"]
    assert start["APIUrl"] == "https://our.example/tencent/v1/"
    assert start["APIKey"] == "TEST-GATEWAY-TOKEN"
    assert start["Model"] == "private-model"
    assert json.loads(start["LLMExtraBody"]) == {"call_id": "local-id"}
    assert start["WelcomeType"] == (0 if opening else 1)
    assert start["WelcomeMessagePriority"] == 0
    assert start["InterruptMode"] == 0
    assert start["InterruptSpeechDuration"] == 300
    assert start["VadSilenceTime"] == 500
    assert start["VoiceType"] == "custom-voice"
    assert start["EndFunctionEnable"] is True
    assert "EnableComplianceAudio" not in start
    assert "MaxDuration" not in start
    assert "MaxCallDurationMs" not in start
    assert "max_tokens" not in start
    assert "thinking" not in start
    assert "PRIVATE-MODEL-KEY" not in requests[0].content.decode()
    assert "TEST-SECRET-KEY" not in requests[0].content.decode()
    assert "TEST-WEBHOOK-TOKEN" not in requests[0].content.decode()
    assert requests[1].headers["X-TC-Action"] == "HangUpCall"
    assert stop == {"SdkAppId": 1400000000, "SessionId": "remote-tencent"}
    assert "TEST-GATEWAY-TOKEN" not in caplog.text
    assert "TEST-SECRET-KEY" not in caplog.text
    await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["aliyun", "tencent"])
@pytest.mark.parametrize("change", [{"enabled": False}, {"gateway_token": None}])
async def test_disabled_or_incomplete_provider_never_initializes_sdk(
    provider: str,
    change: dict[str, Any],
    settings: AppSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = (
        AliyunCallClient(aliyun_config(**change))
        if provider == "aliyun"
        else TencentCallClient(tencent_config(**change))
    )
    initialization = AsyncMock(side_effect=AssertionError("must not connect"))
    monkeypatch.setattr(client, "_client", initialization)
    with pytest.raises(TelephonyError, match="未启用|不完整"):
        await client.start("12300001", "local-id", settings, "https://our.example/v1")
    initialization.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["aliyun", "tencent"])
async def test_sdk_failure_never_exposes_response_or_key(
    provider: str,
    settings: AppSettings,
) -> None:
    sdk = AsyncMock()
    failure = RuntimeError("PRIVATE-MODEL-KEY TEST-SECRET-KEY response body")
    sdk.llm_smart_call_async.side_effect = failure
    sdk.CreateAICall.side_effect = failure
    sdk.hangup_operate_async.side_effect = failure
    sdk.HangUpCall.side_effect = failure
    client = (
        AliyunCallClient(aliyun_config(), sdk=sdk)
        if provider == "aliyun"
        else TencentCallClient(tencent_config(), sdk=sdk)
    )
    for action in (
        client.start("12300001", "local-id", settings, "https://our.example/v1"),
        client.hangup("remote-id"),
    ):
        with pytest.raises(TelephonyError, match="请求失败") as error:
            await action
        assert "KEY" not in str(error.value)
        assert error.value.__suppress_context__ is True
    await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [None, {"code": "Denied"}, {"code": "OK"}])
async def test_aliyun_unsuccessful_or_missing_call_id_is_failure(
    settings: AppSettings,
    body: dict[str, Any] | None,
) -> None:
    sdk = AsyncMock()
    sdk.llm_smart_call_async.return_value = aliyun_models.LlmSmartCallResponse(
        body=aliyun_models.LlmSmartCallResponseBody(**body) if body else None,
    )
    client = AliyunCallClient(aliyun_config(), sdk=sdk)
    with pytest.raises(TelephonyError, match="未接受"):
        await client.start("12300001", "local-id", settings, "https://our.example/v1")


@pytest.mark.asyncio
async def test_tencent_actual_sdk_queries_official_interaction_text_only() -> None:
    client = TencentCallClient(tencent_config())
    sdk = client._client()
    await sdk.http_client.aclose()
    calls: list[httpx.Request] = []

    def capture(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.headers["X-TC-Action"] == "DescribeAICallInteractionRecords"
        assert json.loads(request.content) == {"SdkAppId": 1400000000, "SessionId": "remote-id"}
        return httpx.Response(
            200,
            json={
                "Response": {
                    "RequestId": "request-1",
                    "InteractionEventList": [
                        {
                            "Messages": [
                                {
                                    "Timestamp": 1784166669089,
                                    "AISpeak": {
                                        "SpokenText": "您好。",
                                        "CanBeInterrupted": True,
                                    },
                                },
                                {
                                    "Timestamp": 1784166685178,
                                    "UserReply": {"ASRTranscript": "不用了。"},
                                },
                            ]
                        }
                    ],
                }
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(capture))
    sdk.http_client = http
    try:
        entries = await client.fetch_transcript("remote-id")
        assert len(calls) == 1
        assert [entry.text for entry in entries] == ["您好。", "不用了。"]
        assert not entries[0].interrupted
        assert all("+00:00" in entry.timestamp for entry in entries)
    finally:
        await client.close()
    assert http.is_closed


def test_cloud_interrupted_draft_is_system_background_without_mutating_original() -> None:
    messages = [
        {"role": "assistant", "content": "完整听完的回答。"},
        {"role": "user", "content": "可以干啥？"},
        {"role": "assistant", "content": "可整理资料。<user-interrupt/><hangup/>"},
        {"role": "user", "content": "多少钱？"},
    ]
    converted = cloud_context_messages(messages)
    assert messages[2]["role"] == "assistant"
    assert "<user-interrupt/>" in messages[2]["content"]
    assert converted[0] == messages[0]
    assert converted[2] == {
        "role": "system",
        "content": INTERRUPTED_BACKGROUND_LABEL + "\n可整理资料。",
    }
    assert converted[-1] == {"role": "user", "content": "多少钱？"}
    assert cloud_context_messages(converted) == converted


def test_native_call_end_schema_is_preserved_and_other_tools_are_removed() -> None:
    tools = [
        {
            "type": "function",
            "function": {
                "name": "call_end",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "provider_goodbye": {"type": "string"},
                    },
                    "required": ["provider_goodbye"],
                },
            },
        },
        {"type": "function", "function": {"name": "arbitrary_remote_action"}},
        {"type": "web_search"},
    ]
    selected = native_tencent_tools(tools)
    assert selected == tools[:1]
    selected[0]["function"]["parameters"]["required"].append("fake")
    assert tools[0]["function"]["parameters"]["required"] == ["provider_goodbye"]
    assert native_tencent_tools([]) == []


@pytest.mark.parametrize("provider", ["aliyun", "tencent"])
def test_cloud_prompt_reuses_sales_without_pipecat_tool_calls(
    settings: AppSettings,
    provider: str,
) -> None:
    prompt = cloud_sales_prompt(settings, provider)
    assert settings.sales.goal in prompt
    assert settings.sales.product_info in prompt
    assert "retain_once" not in prompt
    assert "hang_up" not in prompt
    assert "首次明确拒绝" in prompt and "一次" in prompt
    assert "祝您生活愉快" in prompt
    assert "不要自动续讲" in prompt
    assert (ALIYUN_HANGUP_TAG in prompt) == (provider == "aliyun")
    assert ("call_end" in prompt) == (provider == "tencent")
