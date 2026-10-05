"""应用配置和公开响应；密钥从不进入公开模型。"""

from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationInfo, field_validator

from llmautotel.telephony.settings import TelephonySettings


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SalesSettings(StrictModel):
    goal: str = ""
    product_info: str = ""
    instructions: str = ""
    opening: str = ""


ConversationMode = Literal["consultation", "sales"]


class ConversationSettings(StrictModel):
    mode: ConversationMode = "consultation"


class ConsultationSettings(StrictModel):
    product_info: str = ""
    instructions: str = ""
    opening: str = ""


class ProviderSettings(StrictModel):
    base_url: str = ""
    model: str = ""
    api_key: SecretStr | None = None
    timeout_seconds: float = Field(default=30, ge=1, le=300)

    @field_validator("base_url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value:
            return value
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("API 地址须使用 http 或 https")
        try:
            parsed.port
        except ValueError:
            raise ValueError("API 地址中的端口须为有效数字，范围为 0 至 65535") from None
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("API 地址不能包含凭据、查询参数或片段；请单独填写密钥")
        return value


class ASRSettings(ProviderSettings):
    protocol: Literal["openai", "mimo"] = "openai"
    language: str = "zh"

    @field_validator("language")
    @classmethod
    def validate_language(cls, value: str, info: ValidationInfo) -> str:
        from pipecat.transcriptions.language import Language

        value = value.strip()
        if info.data.get("protocol") == "mimo":
            if value not in {"auto", "zh", "en"}:
                raise ValueError("MiMo 识别语言须为 auto、zh 或 en")
            return value
        try:
            Language(value)
        except ValueError:
            raise ValueError("识别语言须为支持的语言代码，例如 zh 或 en") from None
        return value


class LLMSettings(ProviderSettings):
    thinking: Literal["enabled", "disabled"] | None = None
    reasoning_effort: Literal["none", "low", "medium", "high", "max"] | None = None


class TTSSettings(ProviderSettings):
    protocol: Literal["openai", "mimo"] = "openai"
    voice: str = ""
    sample_rate: int = Field(default=24000, ge=8000, le=96000)

    @field_validator("sample_rate")
    @classmethod
    def validate_sample_rate(cls, value: int, info: ValidationInfo) -> int:
        if info.data.get("protocol") == "mimo" and value != 24000:
            raise ValueError("MiMo PCM 输出采样率须为 24000 Hz")
        return value


class VoiceSettings(StrictModel):
    vad_start_seconds: float = Field(default=0.1, ge=0.05, le=1)
    vad_stop_seconds: float = Field(default=0.6, ge=0.2, le=3)
    vad_confidence: float = Field(default=0.7, gt=0, lt=1)


class AppSettings(StrictModel):
    conversation: ConversationSettings = Field(default_factory=ConversationSettings)
    consultation: ConsultationSettings = Field(default_factory=ConsultationSettings)
    sales: SalesSettings = Field(default_factory=SalesSettings)
    asr: ASRSettings = Field(default_factory=ASRSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    tts: TTSSettings = Field(default_factory=TTSSettings)
    voice: VoiceSettings = Field(default_factory=VoiceSettings)
    telephony: TelephonySettings = Field(default_factory=TelephonySettings)

    @property
    def active_conversation(self) -> SalesSettings | ConsultationSettings:
        return self.sales if self.conversation.mode == "sales" else self.consultation

    def public(self) -> dict[str, Any]:
        result = self.model_dump(
            mode="json", exclude={"asr": {"api_key"}, "llm": {"api_key"}, "tts": {"api_key"}}
        )
        for stage in ("asr", "llm", "tts"):
            provider = getattr(self, stage)
            result[stage]["api_key_set"] = bool(
                provider.api_key and provider.api_key.get_secret_value()
            )
        result["telephony"] = self.telephony.public()
        return result

    def private(self) -> dict[str, Any]:
        result = self.model_dump(mode="json")
        for stage in ("asr", "llm", "tts"):
            provider = getattr(self, stage)
            result[stage]["api_key"] = (
                provider.api_key.get_secret_value() if provider.api_key else None
            )
        result["telephony"] = self.telephony.private()
        return result

    def missing_conversation_fields(self) -> list[str]:
        missing: list[str] = []
        if self.conversation.mode == "sales" and not self.sales.goal.strip():
            missing.append("销售目标")
        if not self.active_conversation.product_info.strip():
            missing.append("产品资料")
        return missing

    def missing_call_fields(self) -> list[str]:
        missing = self.missing_conversation_fields()
        for stage, label in (("asr", "ASR"), ("llm", "LLM"), ("tts", "TTS")):
            provider = getattr(self, stage)
            if not provider.base_url:
                missing.append(f"{label} API 地址")
            if not provider.model.strip():
                missing.append(f"{label} 模型")
        if not self.tts.voice.strip():
            missing.append("TTS 音色")
        return missing


class TranscriptEntry(StrictModel):
    role: Literal["user", "assistant"]
    text: str
    timestamp: str
    interrupted: bool = False


class CallRecord(StrictModel):
    id: str
    started_at: str
    ended_at: str | None = None
    status: Literal["connecting", "active", "ended", "failed"] = "connecting"
    end_reason: str | None = None
    settings: dict[str, Any]
    transcript: list[TranscriptEntry] = Field(default_factory=list)
    channel: Literal["browser", "telephone"] = "browser"
    direction: Literal["inbound", "outbound"] | None = None
    caller: str | None = None
    provider: Literal["asterisk", "freeswitch", "aliyun", "tencent"] | None = None
    destination: str | None = None
    remote_id: str | None = None
    state: str = "connecting"
