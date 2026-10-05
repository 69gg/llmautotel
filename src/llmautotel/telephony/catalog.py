"""电话接入目录与表单字段，前端无需复制供应商配置结构。"""

from typing import Any

from llmautotel.telephony.settings import PROVIDER_NAMES, PhoneProviderSettings, TelephonySettings

PROVIDERS: dict[str, tuple[str, str, str]] = {
    "asterisk": ("Asterisk", "media", "沿用本机配置的 ASR、LLM、TTS，接入企业 SIP 线路。"),
    "freeswitch": (
        "FreeSWITCH",
        "media",
        "沿用本机三组模型；原生 ESL 与 UDP 媒体，适用于可信网络。",
    ),
    "aliyun": (
        "阿里云 AICCS",
        "cloud",
        "平台托管识别、合成及打断，通过本机模型网关调用已配置 LLM。",
    ),
    "tencent": ("腾讯云 TCCC", "cloud", "平台托管语音，通过本机模型网关调用已配置 LLM。"),
}

LABELS: dict[str, str] = {
    "inbound_enabled": "启用来电接听",
    "inbound_numbers": "接听号码白名单（留空接受专属路由上的来电）",
    "incoming_app": "入呼 ARI 应用名（与外呼不同）",
    "inbound_marker": "入呼专属路由标识",
    "inbound_reconnect_seconds": "来电监听重连间隔（秒）",
    "account_uid": "阿里云账号 UID",
    "inbound_ai_agent_id": "入呼 AI 智能体 ID",
    "call_timeout_seconds": "回执缺失时的通话上限（秒）",
    "ari_url": "ARI 地址",
    "username": "ARI 用户名",
    "password": "连接密码",
    "app": "ARI 应用名",
    "endpoint_template": "SIP 拨号模板",
    "caller_id": "主叫号码",
    "ring_timeout_seconds": "振铃超时（秒）",
    "media_timeout_seconds": "媒体就绪超时（秒）",
    "cleanup_timeout_seconds": "远端资源清理总超时（秒）",
    "host": "ESL 主机",
    "port": "ESL 端口",
    "gateway": "SIP 网关名称",
    "fs_media_host": "FreeSWITCH 媒体 IPv4",
    "fs_media_port": "FreeSWITCH 媒体 UDP 端口",
    "audio_bind_host": "本机媒体绑定 IPv4",
    "audio_bind_port": "本机媒体 UDP 端口（0 为自动）",
    "audio_advertised_host": "FreeSWITCH 可访问的本机媒体 IPv4",
    "playback_tail_seconds": "媒体发送尾部等待（秒）",
    "endpoint": "云 API 地址",
    "region": "云服务地域",
    "gateway_token": "模型网关鉴权码",
    "webhook_token": "回执鉴权码",
    "tts_voice": "平台音色标识（留空使用平台默认）",
    "timeout_seconds": "请求超时（秒）",
    "access_key_id": "AccessKey ID",
    "access_key_secret": "AccessKey Secret",
    "app_id": "ApplicationCode 应用编码",
    "session_timeout": "平台会话超时（秒，可留空）",
    "secret_id": "SecretId",
    "secret_key": "SecretKey",
    "sdk_app_id": "SdkAppId",
    "interrupt_speech_duration_ms": "平台打断阈值（毫秒，可留空）",
    "vad_silence_ms": "平台停顿阈值（毫秒，可留空）",
}


def provider_catalog(settings: TelephonySettings) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for name in PROVIDER_NAMES:
        provider: PhoneProviderSettings = getattr(settings, name)
        fields: list[dict[str, Any]] = []
        for key, schema in provider.model_json_schema()["properties"].items():
            if key == "enabled":
                continue
            nullable = "anyOf" in schema
            actual = next(
                (item for item in schema.get("anyOf", []) if item.get("type") != "null"), schema
            )
            fields.append(
                {
                    "name": key,
                    "label": LABELS[key],
                    "type": "secret"
                    if key in provider.secret_fields
                    else actual.get("type", "string"),
                    "nullable": nullable,
                    "minimum": actual.get("minimum"),
                    "maximum": actual.get("maximum"),
                }
            )
        label, mode, description = PROVIDERS[name]
        result.append(
            {
                "id": name,
                "label": label,
                "mode": mode,
                "description": description,
                "enabled": provider.enabled,
                "inbound_enabled": provider.inbound_enabled,
                "fields": fields,
            }
        )
    return result
