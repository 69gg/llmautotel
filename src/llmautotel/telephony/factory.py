"""配置校验与显式创建；导入或列出 provider 不连接电话系统。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from llmautotel.models import AppSettings
from llmautotel.telephony.settings import TelephonyProviderName

if TYPE_CHECKING:
    from llmautotel.sessions import VoiceRuntime
    from llmautotel.voice import VoiceCallbacks


def missing_phone_fields(settings: AppSettings, provider: TelephonyProviderName) -> list[str]:
    config = getattr(settings.telephony, provider)
    missing: list[str] = config.missing_fields()
    if provider in {"asterisk", "freeswitch"}:
        missing += settings.missing_call_fields()
    else:
        for label, value in (
            ("销售目标", settings.sales.goal),
            ("产品资料", settings.sales.product_info),
            ("LLM 地址", settings.llm.base_url),
            ("LLM 模型", settings.llm.model),
            ("公网网关根地址", settings.telephony.public_base_url),
        ):
            if not value.strip():
                missing.append(label)
    return missing


def create_phone_session(
    provider: TelephonyProviderName,
    number: str,
    call_id: str,
    settings: AppSettings,
    callbacks: VoiceCallbacks,
) -> VoiceRuntime:
    if provider in {"aliyun", "tencent"}:
        from llmautotel.telephony.gateway import CloudVoiceSession

        return CloudVoiceSession(provider, number, call_id, settings, callbacks)
    from llmautotel.telephony.asterisk import AsteriskDriver
    from llmautotel.telephony.freeswitch import FreeSwitchDriver
    from llmautotel.telephony.transport import PhoneTransport
    from llmautotel.voice import VoiceSession

    config = settings.telephony
    transport = PhoneTransport(
        lambda events: (
            AsteriskDriver(config.asterisk, events)
            if provider == "asterisk"
            else FreeSwitchDriver(config.freeswitch, events)
        ),
        number=number,
        call_id=call_id,
    )
    return VoiceSession(None, settings, callbacks, transport=transport)
