"""电话配置，所有接入默认关闭；凭据仅保存在服务器。"""

from typing import Any, ClassVar, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


def http_url(value: str) -> str:
    value = value.strip().rstrip("/")
    if not value:
        return value
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("服务地址须使用 http 或 https")
    try:
        parsed.port
    except ValueError:
        raise ValueError("服务端口无效") from None
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("服务地址不能包含凭据、查询参数或片段")
    return value


class PhoneProviderSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    secret_fields: ClassVar[tuple[str, ...]] = ()
    enabled: bool = False

    @field_validator("caller_id", check_fields=False)
    @classmethod
    def validate_caller_id(cls, value: str) -> str:
        import re

        if value and not re.fullmatch(r"\+?[0-9]{3,20}", value):
            raise ValueError("主叫号码须仅包含数字及可选的开头加号")
        return value

    def missing_fields(self) -> list[str]:
        return []


class AsteriskSettings(PhoneProviderSettings):
    secret_fields: ClassVar[tuple[str, ...]] = ("password",)
    ari_url: str = ""
    username: str = ""
    password: SecretStr | None = None
    app: str = "llmautotel"
    endpoint_template: str = ""
    caller_id: str = ""
    ring_timeout_seconds: float = Field(default=45, ge=5, le=180)
    media_timeout_seconds: float = Field(default=10, ge=1, le=60)

    _url = field_validator("ari_url")(http_url)

    @field_validator("endpoint_template")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        from string import Formatter

        if not value:
            return value
        try:
            parsed = list(Formatter().parse(value))
            fields = [field for _, field, _, _ in parsed if field is not None]
        except ValueError:
            raise ValueError("拨号模板格式无效") from None
        if (
            fields != ["number"]
            or not value.startswith("PJSIP/")
            or any(spec or conversion for _, field, spec, conversion in parsed if field)
        ):
            raise ValueError("拨号模板须为 PJSIP/...，且只包含一个 {number}")
        if any(character in value for character in "\r\n\x00"):
            raise ValueError("拨号模板不能包含控制字符")
        return value

    def missing_fields(self) -> list[str]:
        return [
            label
            for field, label in (
                ("ari_url", "ARI 地址"),
                ("username", "ARI 用户名"),
                ("password", "ARI 密码"),
                ("app", "ARI 应用名"),
                ("endpoint_template", "SIP 拨号模板"),
                ("caller_id", "主叫号码"),
            )
            if not getattr(self, field)
        ]


class FreeswitchSettings(PhoneProviderSettings):
    secret_fields: ClassVar[tuple[str, ...]] = ("password",)
    host: str = ""
    port: int = Field(default=8021, ge=1, le=65535)
    password: SecretStr | None = None
    gateway: str = ""
    caller_id: str = ""
    ring_timeout_seconds: float = Field(default=45, ge=5, le=180)
    media_timeout_seconds: float = Field(default=10, ge=1, le=60)
    fs_media_host: str = ""
    fs_media_port: int = Field(default=0, ge=0, le=65535)
    audio_bind_host: str = "127.0.0.1"
    audio_bind_port: int = Field(default=0, ge=0, le=65535)
    audio_advertised_host: str = ""
    playback_tail_seconds: float = Field(default=0.1, ge=0, le=2)

    @field_validator("gateway")
    @classmethod
    def validate_gateway(cls, value: str) -> str:
        import re

        if value and not re.fullmatch(r"[A-Za-z0-9_.-]+", value):
            raise ValueError("网关名称仅允许字母、数字、点、下划线和连字符")
        return value

    @field_validator("fs_media_host", "audio_bind_host", "audio_advertised_host")
    @classmethod
    def validate_media_host(cls, value: str) -> str:
        from ipaddress import IPv4Address

        if value:
            try:
                IPv4Address(value)
            except ValueError:
                raise ValueError("媒体地址须为 IPv4 地址") from None
        return value

    def missing_fields(self) -> list[str]:
        return [
            label
            for field, label in (
                ("host", "ESL 主机"),
                ("password", "ESL 密码"),
                ("gateway", "SIP 网关名"),
                ("caller_id", "主叫号码"),
                ("fs_media_host", "FreeSWITCH 媒体地址"),
                ("fs_media_port", "FreeSWITCH 媒体端口"),
                ("audio_advertised_host", "应用媒体可达地址"),
            )
            if not getattr(self, field)
        ]


class CloudSettings(PhoneProviderSettings):
    endpoint: str = ""
    region: str = ""
    caller_id: str = ""
    gateway_token: SecretStr | None = None
    webhook_token: SecretStr | None = None
    tts_voice: str = ""
    timeout_seconds: float = Field(default=30, ge=1, le=120)

    _url = field_validator("endpoint")(http_url)


class AliyunSettings(CloudSettings):
    secret_fields: ClassVar[tuple[str, ...]] = (
        "access_key_id",
        "access_key_secret",
        "gateway_token",
        "webhook_token",
    )
    access_key_id: SecretStr | None = None
    access_key_secret: SecretStr | None = None
    app_id: str = ""
    session_timeout: int | None = Field(default=None, ge=600, le=3600)

    def missing_fields(self) -> list[str]:
        return [
            label
            for field, label in (
                ("endpoint", "API 地址"),
                ("region", "地域"),
                ("access_key_id", "AccessKey ID"),
                ("access_key_secret", "AccessKey Secret"),
                ("app_id", "应用编码"),
                ("caller_id", "主叫号码"),
                ("gateway_token", "模型网关鉴权码"),
                ("webhook_token", "回执鉴权码"),
            )
            if not getattr(self, field)
        ]


class TencentSettings(CloudSettings):
    secret_fields: ClassVar[tuple[str, ...]] = (
        "secret_id",
        "secret_key",
        "gateway_token",
        "webhook_token",
    )
    secret_id: SecretStr | None = None
    secret_key: SecretStr | None = None
    sdk_app_id: int | None = Field(default=None, ge=1)
    interrupt_speech_duration_ms: int | None = Field(default=None, ge=100, le=3000)
    vad_silence_ms: int | None = Field(default=None, ge=240, le=2000)

    def missing_fields(self) -> list[str]:
        return [
            label
            for field, label in (
                ("endpoint", "API 地址"),
                ("region", "地域"),
                ("secret_id", "SecretId"),
                ("secret_key", "SecretKey"),
                ("sdk_app_id", "SdkAppId"),
                ("caller_id", "主叫号码"),
                ("gateway_token", "模型网关鉴权码"),
                ("webhook_token", "回执鉴权码"),
            )
            if not getattr(self, field)
        ]


TelephonyProviderName = Literal["asterisk", "freeswitch", "aliyun", "tencent"]
PROVIDER_NAMES: tuple[TelephonyProviderName, ...] = (
    "asterisk",
    "freeswitch",
    "aliyun",
    "tencent",
)


class TelephonySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    public_base_url: str = ""
    asterisk: AsteriskSettings = Field(default_factory=AsteriskSettings)
    freeswitch: FreeswitchSettings = Field(default_factory=FreeswitchSettings)
    aliyun: AliyunSettings = Field(default_factory=AliyunSettings)
    tencent: TencentSettings = Field(default_factory=TencentSettings)

    _url = field_validator("public_base_url")(http_url)

    def public(self) -> dict[str, Any]:
        result: dict[str, Any] = {"public_base_url": self.public_base_url}
        for name in PROVIDER_NAMES:
            provider: PhoneProviderSettings = getattr(self, name)
            public = provider.model_dump(mode="json", exclude=set(provider.secret_fields))
            for field in provider.secret_fields:
                secret: SecretStr | None = getattr(provider, field)
                public[f"{field}_set"] = bool(secret and secret.get_secret_value())
            result[name] = public
        return result

    def private(self) -> dict[str, Any]:
        result = self.model_dump(mode="json")
        for name in PROVIDER_NAMES:
            provider: PhoneProviderSettings = getattr(self, name)
            for field in provider.secret_fields:
                secret: SecretStr | None = getattr(provider, field)
                result[name][field] = secret.get_secret_value() if secret else None
        return result

    def preserve_secrets(self, current: "TelephonySettings") -> None:
        for name in PROVIDER_NAMES:
            old: PhoneProviderSettings = getattr(current, name)
            if name not in self.model_fields_set:
                setattr(self, name, old.model_copy(deep=True))
                continue
            incoming: PhoneProviderSettings = getattr(self, name)
            for field in incoming.secret_fields:
                if field not in incoming.model_fields_set:
                    setattr(incoming, field, getattr(old, field))
