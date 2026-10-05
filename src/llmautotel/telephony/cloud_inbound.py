"""供应商正式来电路由协议；此模块不发起外呼或创建本机会话。"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from llmautotel.models import AppSettings
from llmautotel.telephony.cloud import AliyunCallClient, CloudProvider
from llmautotel.telephony.settings import AliyunSettings, CloudSettings, TencentSettings

type GatewayProbeResponse = tuple[str, ...] | dict[str, Any]


class AliyunInboundRequest(BaseModel):
    """AICCS 应用的呼入开场变量回调，不是最终文字回执。"""

    model_config = ConfigDict(extra="ignore", strict=True)
    caller: str = Field(min_length=1, max_length=32, pattern=r"^\+?[0-9]+$")
    callee: str = Field(min_length=1, max_length=32, pattern=r"^\+?[0-9]+$")
    callId: str = Field(min_length=1, max_length=128)
    applicationCode: str = Field(min_length=1, max_length=128)


class TencentInboundDetails(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    AIAgentId: int = Field(default=0, ge=0)
    IvrId: int = Field(default=0, ge=0)
    Caller: str = Field(min_length=1, max_length=32, pattern=r"^\+?[0-9]+$")
    Callee: str = Field(min_length=1, max_length=32, pattern=r"^\+?[0-9]+$")


class TencentInboundRequest(BaseModel):
    """TCCC 振铃时发出的动态呼入路由请求。"""

    model_config = ConfigDict(extra="ignore", strict=True)
    Event: Literal["callInBound"]
    SessionId: str = Field(min_length=1, max_length=128)
    SdkAppId: int = Field(ge=1)
    CallInBound: TencentInboundDetails


@dataclass(frozen=True)
class CloudIncomingCall:
    provider: CloudProvider
    remote_id: str
    caller: str
    callee: str
    application: str | int


def parse_cloud_inbound(provider: CloudProvider, body: object) -> CloudIncomingCall:
    if provider == "aliyun":
        request = AliyunInboundRequest.model_validate(body)
        return CloudIncomingCall(
            provider, request.callId, request.caller, request.callee, request.applicationCode
        )
    request = TencentInboundRequest.model_validate(body)
    return CloudIncomingCall(
        provider,
        request.SessionId,
        request.CallInBound.Caller,
        request.CallInBound.Callee,
        request.SdkAppId,
    )


def _number(value: str) -> str:
    """允许大陆本地号码与平台 0086 / +86 号码格式互相匹配。"""
    if value.startswith("+86"):
        value = value[3:]
    elif value.startswith("0086"):
        value = value[4:]
    else:
        value = value.removeprefix("+")
    # E.164 里的大陆固话通常省略区号前 0，本地号码通常保留。
    return value.lstrip("0")


def authenticate_cloud_inbound(
    provider: CloudProvider,
    config: CloudSettings,
    body: object,
    token: str | None,
    *,
    timestamp: str | None = None,
    auth: str | None = None,
    now_ms: int | None = None,
    timestamp_tolerance_seconds: float = 300,
) -> bool:
    """URL 能力认证，阿里另校验官方签名；不臆造腾讯路由签名。"""
    if not config.enabled or not config.inbound_enabled:
        return False
    expected = config.webhook_token.get_secret_value() if config.webhook_token else ""
    if not expected or not token or not hmac.compare_digest(token.encode(), expected.encode()):
        return False
    try:
        call = parse_cloud_inbound(provider, body)
    except ValidationError:
        return False
    if not config.inbound_numbers or _number(call.callee) not in {
        _number(number) for number in config.inbound_numbers
    }:
        return False
    if provider == "tencent":
        return isinstance(config, TencentSettings) and call.application == config.sdk_app_id
    if not isinstance(config, AliyunSettings) or call.application != config.app_id:
        return False
    if not config.account_uid or not timestamp or not auth:
        return False
    try:
        millis = int(timestamp)
    except ValueError:
        return False
    current = now_ms if now_ms is not None else int(time.time() * 1000)
    if abs(current - millis) > timestamp_tolerance_seconds * 1000:
        return False
    # 供应商规定的 MD5 不是本机发明的安全签名；能力 token 提供独立秘密。
    signature = hashlib.md5(
        (call.caller + config.account_uid + timestamp).encode(), usedforsecurity=False
    ).hexdigest()
    return hmac.compare_digest(signature.encode(), auth.encode())


def cloud_inbound_response(
    provider: CloudProvider, config: CloudSettings, call_id: str
) -> dict[str, Any]:
    """仅已接纳来电可获得当前通话标识；拒绝响应由 HTTP 接入层决定。"""
    if provider == "aliyun":
        return {"code": "OK", "bizParam": json.dumps({"call_id": call_id})}
    if not isinstance(config, TencentSettings) or not config.inbound_ai_agent_id:
        raise ValueError("尚未配置来电智能体")
    return {
        "CallInBound": {
            "OverrideAIAgentId": config.inbound_ai_agent_id,
            "Variables": [{"Key": "llmautotel_call_id", "Value": call_id}],
        }
    }


async def reject_aliyun_inbound(config: AliyunSettings, remote_id: str) -> None:
    """阿里没有公开变量回调的拒接 JSON；已启用且已鉴权的忙线来电立即挂断。"""
    if not config.enabled or not config.inbound_enabled:
        raise ValueError("电话来电接入未启用")
    client = AliyunCallClient(config)
    try:
        await client.hangup(remote_id)
    finally:
        await client.close()


def cloud_gateway_call_id(provider: CloudProvider, body: dict[str, Any]) -> str | None:
    """腾讯来电用官方提示词变量绑定；只读取平台 system，不能从 user 猜 ID。"""
    if provider == "aliyun":
        biz = body.get("biz_params")
        direct = body.get("out_id") or (biz.get("call_id") if isinstance(biz, dict) else None)
        return direct if isinstance(direct, str) and direct else None
    direct = body.get("call_id")
    ids: set[str] = set()
    messages = body.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if (
                isinstance(message, dict)
                and message.get("role") == "system"
                and isinstance(message.get("content"), str)
            ):
                ids.update(
                    re.findall(r"\[llmautotel_call_id:([A-Za-z0-9_-]{1,128})\]", message["content"])
                )
    if isinstance(direct, str) and direct:
        ids.add(direct)
    return next(iter(ids)) if len(ids) == 1 else None


def authenticated_gateway_probe(
    settings: AppSettings,
    provider: CloudProvider,
    body: dict[str, Any],
    authorization: str | None,
) -> GatewayProbeResponse | None:
    """无通话时的纯协议探测；调用方必须先确认全局没有活动通话。"""
    config = getattr(settings.telephony, provider)
    if not config.enabled or not config.inbound_enabled:
        return None
    stream = body.get("stream")
    if provider == "aliyun" and stream is not True:
        return None
    if stream is not True and stream is not False and stream is not None:
        return None
    expected = config.gateway_token.get_secret_value() if config.gateway_token else ""
    if provider == "tencent":
        expected = "Bearer " + expected if expected else ""
    if (
        not expected
        or not authorization
        or not hmac.compare_digest(authorization.encode(), expected.encode())
    ):
        return None
    # 有空/错误标识仍是会话协议错误，不能降级成可用性探测。
    binding_fields = {"call_id", "session_id", "SessionId", "out_id", "biz_params"}
    if binding_fields.intersection(body) or body.get("tools"):
        return None
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return None
    for message in messages:
        if (
            not isinstance(message, dict)
            or message.get("role") not in {"system", "user", "assistant"}
            or not isinstance(message.get("content"), str)
            or "llmautotel_call_id" in message["content"]
        ):
            return None
    identity: dict[str, Any] = {
        "id": "chatcmpl-" + str(uuid4()),
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": settings.llm.model,
    }
    text = "模型网关连接正常；此回复仅验证协议，未调用模型或建立电话。"
    if stream is not True:
        return {
            **identity,
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text, "refusal": None},
                    "finish_reason": "stop",
                    "logprobs": None,
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }
    response = {
        **identity,
        "choices": [
            {
                "index": 0,
                "delta": {
                    "role": "assistant",
                    "content": text,
                },
                "finish_reason": None,
            }
        ],
    }
    finished = {
        **identity,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    return (
        "data: " + json.dumps(response, ensure_ascii=False) + "\n\n",
        "data: " + json.dumps(finished, ensure_ascii=False) + "\n\n",
        "data: [DONE]\n\n",
    )
