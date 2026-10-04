"""云平台托管语音的公开控制接口；模型请求经过本机网关。"""

from __future__ import annotations

import base64
import hmac
import json
import logging
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

from alibabacloud_aiccs20191015 import models as aliyun_models
from alibabacloud_aiccs20191015.client import Client as AliyunSDKClient
from alibabacloud_tea_openapi.models import Config as AliyunConfig
from pydantic import SecretStr
from tencentcloud.ccc.v20200210 import models as tencent_models
from tencentcloud.ccc.v20200210.ccc_client_async import CccClient
from tencentcloud.common.credential import Credential
from tencentcloud.common.profile.client_profile import ClientProfile
from tencentcloud.common.profile.http_profile import HttpProfile

from llmautotel.conversation import INTERRUPTED_BACKGROUND_LABEL
from llmautotel.models import AppSettings, TranscriptEntry
from llmautotel.telephony.base import TelephonyError
from llmautotel.telephony.settings import AliyunSettings, CloudSettings, TencentSettings

ALIYUN_HANGUP_TAG = "<hangup/>"
ALIYUN_INTERRUPT_TAG = "<user-interrupt/>"
TENCENT_END_FUNCTION = "call_end"
CloudProvider = Literal["aliyun", "tencent"]


class AliyunSDK(Protocol):
    async def llm_smart_call_async(
        self,
        request: aliyun_models.LlmSmartCallRequest,
    ) -> aliyun_models.LlmSmartCallResponse: ...

    async def hangup_operate_async(
        self,
        request: aliyun_models.HangupOperateRequest,
    ) -> aliyun_models.HangupOperateResponse: ...


class TencentSDK(Protocol):
    async def CreateAICall(
        self,
        request: tencent_models.CreateAICallRequest,
    ) -> tencent_models.CreateAICallResponse: ...

    async def HangUpCall(
        self,
        request: tencent_models.HangUpCallRequest,
    ) -> tencent_models.HangUpCallResponse: ...

    async def close(self) -> None: ...

    async def DescribeAICallInteractionRecords(
        self,
        request: tencent_models.DescribeAICallInteractionRecordsRequest,
    ) -> tencent_models.DescribeAICallInteractionRecordsResponse: ...


@dataclass(frozen=True)
class CloudReport:
    """仅抽取通话关联和平台确认的文字，录音/URL/其他字段不进入本机历史。"""

    local_id: str | None
    remote_id: str
    transcript: list[TranscriptEntry]
    end_reason: str


def _timestamp(value: object) -> str:
    if isinstance(value, (int, float)) and value > 0:
        seconds = value / 1000 if value > 100_000_000_000 else value
        try:
            return datetime.fromtimestamp(seconds, UTC).isoformat()
        except (OverflowError, OSError, ValueError):
            pass
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value).isoformat()
        except ValueError:
            pass
    return datetime.now(UTC).isoformat()


def _aliyun_transcript(value: object, timestamp: object) -> list[TranscriptEntry]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return []
    if not isinstance(value, list):
        return []
    result: list[TranscriptEntry] = []
    for message in value:
        if not isinstance(message, dict) or message.get("role") not in {"user", "assistant"}:
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        interrupted = ALIYUN_INTERRUPT_TAG in content
        text = content.replace(ALIYUN_INTERRUPT_TAG, "").replace(ALIYUN_HANGUP_TAG, "").strip()
        if text:
            result.append(
                TranscriptEntry(
                    role=message["role"],
                    text=text,
                    interrupted=interrupted,
                    timestamp=_timestamp(message.get("timestamp", timestamp)),
                )
            )
    return result


def tencent_interaction_transcript(payload: dict[str, Any]) -> list[TranscriptEntry]:
    """仅读取官方交互流 UserReply/AISpeak，不把生成稿猜作已播放文字。"""
    result: list[TranscriptEntry] = []
    rounds = payload.get("InteractionEventList", [])
    if not isinstance(rounds, list):
        return result
    for round_data in rounds:
        if not isinstance(round_data, dict) or not isinstance(round_data.get("Messages"), list):
            continue
        for message in round_data["Messages"]:
            if not isinstance(message, dict):
                continue
            user, assistant = message.get("UserReply"), message.get("AISpeak")
            role: Literal["user", "assistant"]
            if isinstance(user, dict):
                role, content = "user", user.get("ASRTranscript")
            elif isinstance(assistant, dict):
                role, content = "assistant", assistant.get("SpokenText")
            else:
                continue
            if isinstance(content, str) and content.strip():
                result.append(
                    TranscriptEntry(
                        role=role,
                        text=content.strip(),
                        interrupted=False,
                        timestamp=_timestamp(message.get("Timestamp")),
                    )
                )
    # 官方没有提供实际播放完成/被打断字段，不能从 CanBeInterrupted 推断发生了打断。
    return result


def _aliyun_end_reason(item: dict[str, Any]) -> str:
    smart = str(item.get("smart_status_code", "")).upper()
    reason = {
        "NO_ANSWER": "no_answer",
        "USER_BUSY": "busy",
        "NETWORK_BUSY": "busy",
        "CALL_REJECTED": "rejected",
        "CALLING_FAILED": "dial_failed",
        "INVALID_NUMBER": "unreachable",
        "POWERED_OFF": "unreachable",
        "SUSPEND": "unreachable",
        "UNAVAILABLE": "unreachable",
        "NO_USER_RESPONDING": "unreachable",
        "OPERATOR_BLOCK": "rejected",
        "INCOMING_CALL_BARRED": "rejected",
        "CANCEL": "caller_cancelled",
    }.get(smart)
    if reason is None:
        reason = {
            "200002": "busy",
            "200112": "busy",
            "200003": "no_answer",
            "200113": "no_answer",
            "200004": "unreachable",
            "200116": "unreachable",
            "200005": "unreachable",
            "200007": "unreachable",
            "200010": "unreachable",
            "200011": "unreachable",
            "200111": "rejected",
        }.get(str(item.get("status_code", "")))
    if reason is not None:
        return reason
    return (
        "remote_hangup"
        if str(item.get("hangup_direction", "")).lower() in {"用户", "user"}
        else "cloud_ended"
    )


def parse_cloud_reports(provider: CloudProvider, payload: object) -> list[CloudReport]:
    """支持阿里官方 HTTP 批量回执与腾讯单条电话 CDR；不接受虚构事件协议。"""
    items = payload if isinstance(payload, list) else [payload]
    result: list[CloudReport] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if provider == "aliyun":
            remote_id = item.get("call_id")
            if (
                not isinstance(remote_id, str)
                or not remote_id
                or not any(key in item for key in ("end_time", "status_code", "smart_status_code"))
            ):
                continue
            if str(item.get("status_code", "")) in {"200101", "200102", "200103", "300000"}:
                continue
            local = item.get("out_id")
            transcript = _aliyun_transcript(
                item.get("conversation_record"),
                item.get("end_time") or item.get("originate_time"),
            )
            reason = _aliyun_end_reason(item)
        else:
            remote_id = item.get("SessionId")
            if not isinstance(remote_id, str) or not remote_id or not item.get("EndedTimestamp"):
                continue
            local = None  # CreateAICall 不提供 UUI 入参，不猜 CDR 的 Uui 等于本机 ID。
            transcript = tencent_interaction_transcript(item)
            reason = "remote_hangup" if item.get("HungUpSide") == "user" else "cloud_ended"
        result.append(
            CloudReport(
                local_id=local if isinstance(local, str) and local else None,
                remote_id=remote_id,
                transcript=transcript,
                end_reason=reason,
            )
        )
    return result


def _secret(value: SecretStr | None) -> str:
    return value.get_secret_value() if value else ""


def authenticate_cloud_event(
    config: CloudSettings,
    token: str | None,
    authorization: str | None = None,
) -> bool:
    """电话数据推送的 URL 能力认证与腾讯官方 BasicAuth，供迟到回执共用。"""
    expected = _secret(config.webhook_token)
    if token and expected and hmac.compare_digest(token.encode(), expected.encode()):
        return True
    if isinstance(config, TencentSettings) and config.sdk_app_id and expected and authorization:
        basic = base64.b64encode(f"{config.sdk_app_id}:{expected}".encode()).decode()
        return hmac.compare_digest(authorization.encode(), ("Basic " + basic).encode())
    return False


def _check_start(config: CloudSettings, gateway_url: str) -> None:
    if not config.enabled:
        raise TelephonyError("电话服务未启用")
    missing = config.missing_fields()
    if missing:
        raise TelephonyError("电话配置不完整：" + "、".join(missing))
    url = urlsplit(gateway_url)
    if url.scheme not in {"http", "https"} or not url.hostname:
        raise TelephonyError("模型网关地址无效")
    if url.username or url.password or url.query or url.fragment:
        raise TelephonyError("模型网关地址不能包含凭据或查询参数")


def _endpoint(config: CloudSettings) -> tuple[str, str]:
    url = urlsplit(config.endpoint)
    if url.path not in {"", "/"}:
        raise TelephonyError("云 API 地址须为域名根地址")
    return url.scheme, url.netloc


def cloud_sales_prompt(settings: AppSettings, provider: CloudProvider) -> str:
    """复用现有销售规则，替换只适用于 Pipecat 的工具约定。"""
    from llmautotel.hangup import HANGUP_POLICY
    from llmautotel.voice import sales_prompt

    policy = (
        "区分拒绝目标行动和直接结束通话。首次明确拒绝目标行动时，"
        "只做一次简短、温和、依据产品资料的挽留；不得反复挽留，"
        "不得调查用途或询问是否要挂断。再次明确拒绝，或直接要求结束时，"
        "告别并结束通话。意思不明确、嫌贵、犹豫或仍提问题时继续回答和推销，"
        "不能猜测结束意愿。告别包含祝福，例如‘好的，祝您生活愉快，再见。’。\n"
    )
    if provider == "aliyun":
        policy += (
            "仅在明确可以结束时，在带祝福的告别正文末尾添加 <hangup/>，"
            "交由电话平台播放告别后挂断；其他回复不得添加此标记。\n"
        )
    else:
        policy += (
            "仅在明确可以结束时，使用请求中提供的 call_end 工具及其真实参数定义；"
            "保留带祝福的告别，不能编造工具参数。未提供该工具时不要假装挂断。\n"
        )
    return sales_prompt(settings).replace(HANGUP_POLICY, policy)


def cloud_context_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """平台明确标记被打断的助手消息时，保守地把整条移为未播放背景。"""
    result = deepcopy(messages)
    for message in result:
        content = message.get("content")
        if message.get("role") == "assistant" and isinstance(content, str):
            if ALIYUN_INTERRUPT_TAG in content:
                draft = content.replace(ALIYUN_INTERRUPT_TAG, "")
                draft = draft.replace(ALIYUN_HANGUP_TAG, "").strip()
                message["role"] = "system"
                message["content"] = INTERRUPTED_BACKGROUND_LABEL + "\n" + draft
                message.pop("tool_calls", None)
            else:
                message["content"] = content.replace(ALIYUN_HANGUP_TAG, "")
    return result


def native_tencent_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """只透传平台实际发送的 call_end 定义，不猜 schema 或开放其他工具。"""
    return [
        deepcopy(tool)
        for tool in tools
        if (
            tool.get("type") == "function"
            and isinstance(tool.get("function"), dict)
            and tool["function"].get("name") == TENCENT_END_FUNCTION
        )
    ]


class AliyunCallClient:
    """LlmSmartCall / HangupOperate，签名与序列化由锁定的官方 SDK 完成。"""

    def __init__(self, config: AliyunSettings, *, sdk: AliyunSDK | None = None) -> None:
        self.config = config.model_copy(deep=True)
        self._sdk = sdk

    def _client(self) -> AliyunSDK:
        # SDK 的 DEBUG=sdk 会打印签名请求，明确抑制此日志避免凭据外泄。
        logging.getLogger("darabonba-core").setLevel(logging.WARNING)
        if self._sdk is None:
            protocol, endpoint = _endpoint(self.config)
            self._sdk = AliyunSDKClient(
                AliyunConfig(
                    access_key_id=_secret(self.config.access_key_id),
                    access_key_secret=_secret(self.config.access_key_secret),
                    region_id=self.config.region,
                    protocol=protocol,
                    endpoint=endpoint,
                    read_timeout=int(self.config.timeout_seconds * 1000),
                    connect_timeout=int(self.config.timeout_seconds * 1000),
                )
            )
        return self._sdk

    async def start(
        self,
        number: str,
        call_id: str,
        settings: AppSettings,
        gateway_url: str,
    ) -> str:
        """阿里应用须在平台预先绑定 gateway_url；API 不支持单次覆盖。"""
        _check_start(self.config, gateway_url)
        request = aliyun_models.LlmSmartCallRequest(
            application_code=self.config.app_id,
            called_number=number,
            caller_number=self.config.caller_id,
            out_id=call_id,
            biz_param={"call_id": call_id},
            session_timeout=self.config.session_timeout,
            tts_voice_code=self.config.tts_voice or None,
        )
        try:
            response = await self._client().llm_smart_call_async(request)
        except Exception:
            raise TelephonyError("阿里云创建电话请求失败") from None
        if response.body is None or response.body.code != "OK" or not response.body.call_id:
            raise TelephonyError("阿里云未接受电话请求")
        return response.body.call_id

    async def hangup(self, remote_id: str) -> None:
        """用户挂断立即生效；AI 告别使用 MSML 播放屏障。"""
        if not self.config.enabled:
            raise TelephonyError("电话服务未启用")
        try:
            response = await self._client().hangup_operate_async(
                aliyun_models.HangupOperateRequest(call_id=remote_id, immediate_hangup=True)
            )
        except Exception:
            raise TelephonyError("阿里云挂断电话请求失败") from None
        if response.body is None or response.body.code != "OK" or response.body.result is False:
            raise TelephonyError("阿里云未接受挂断请求")

    async def close(self) -> None:
        """阿里 SDK 在每次请求的 finally 中关闭 HTTP 会话。"""


class TencentCallClient:
    """CreateAICall / HangUpCall，通过本机 OpenAI 兼容网关使用已配置模型。"""

    def __init__(self, config: TencentSettings, *, sdk: TencentSDK | None = None) -> None:
        self.config = config.model_copy(deep=True)
        self._sdk = sdk

    def _client(self) -> TencentSDK:
        logging.getLogger("tencentcloud_sdk_common").setLevel(logging.WARNING)
        if self._sdk is None:
            protocol, endpoint = _endpoint(self.config)
            profile = ClientProfile(
                httpProfile=HttpProfile(
                    protocol=protocol,
                    endpoint=endpoint,
                    reqTimeout=self.config.timeout_seconds,
                )
            )
            self._sdk = CccClient(
                Credential(
                    _secret(self.config.secret_id),
                    _secret(self.config.secret_key),
                ),
                self.config.region,
                profile,
            )
        return self._sdk

    async def start(
        self,
        number: str,
        call_id: str,
        settings: AppSettings,
        gateway_url: str,
    ) -> str:
        _check_start(self.config, gateway_url)
        parameters: dict[str, Any] = {
            "SdkAppId": self.config.sdk_app_id,
            "Callee": number,
            "Callers": [self.config.caller_id],
            "LLMType": "openai",
            "APIKey": _secret(self.config.gateway_token),
            "APIUrl": gateway_url.rstrip("/") + "/",
            "Model": settings.llm.model,
            "SystemPrompt": cloud_sales_prompt(settings, "tencent"),
            "LLMExtraBody": json.dumps({"call_id": call_id}),
            "WelcomeType": 0 if settings.sales.opening.strip() else 1,
            "WelcomeMessagePriority": 0,
            "InterruptMode": 0,
            "Languages": ["zh"],
            "EndFunctionEnable": True,
            "EndFunctionDesc": (
                "仅用户明确直接结束通话，或已温和挽留一次后再次明确拒绝目标行动时，"
                "保留带祝福的告别再结束。嫌贵、犹豫、含糊或仍提问题时不能调用。"
            ),
        }
        if settings.sales.opening.strip():
            parameters["WelcomeMessage"] = settings.sales.opening
        for field, value in (
            ("VoiceType", self.config.tts_voice),
            ("InterruptSpeechDuration", self.config.interrupt_speech_duration_ms),
            ("VadSilenceTime", self.config.vad_silence_ms),
        ):
            if value is not None and value != "":
                parameters[field] = value
        request = tencent_models.CreateAICallRequest()
        request.from_json_string(json.dumps(parameters, ensure_ascii=False))
        try:
            response = await self._client().CreateAICall(request)
        except Exception:
            raise TelephonyError("腾讯云创建电话请求失败") from None
        if not response.SessionId:
            raise TelephonyError("腾讯云未返回电话会话编号")
        return response.SessionId

    async def hangup(self, remote_id: str) -> None:
        if not self.config.enabled:
            raise TelephonyError("电话服务未启用")
        request = tencent_models.HangUpCallRequest()
        request.from_json_string(
            json.dumps(
                {
                    "SdkAppId": self.config.sdk_app_id,
                    "SessionId": remote_id,
                }
            )
        )
        try:
            await self._client().HangUpCall(request)
        except Exception:
            raise TelephonyError("腾讯云挂断电话请求失败") from None

    async def close(self) -> None:
        if self._sdk is not None:
            await self._sdk.close()
            self._sdk = None

    async def fetch_transcript(self, remote_id: str) -> list[TranscriptEntry]:
        """CDR 不含文字，结束后查询官方智能体交互流；不下载录音或录音转写 URL。"""
        if not self.config.enabled:
            raise TelephonyError("电话服务未启用")
        request = tencent_models.DescribeAICallInteractionRecordsRequest()
        request.from_json_string(
            json.dumps(
                {
                    "SdkAppId": self.config.sdk_app_id,
                    "SessionId": remote_id,
                }
            )
        )
        try:
            response = await self._client().DescribeAICallInteractionRecords(request)
            return tencent_interaction_transcript(json.loads(response.to_json_string()))
        except Exception:
            raise TelephonyError("腾讯云读取电话文字记录失败") from None
