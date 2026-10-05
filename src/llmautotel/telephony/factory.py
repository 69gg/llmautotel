"""配置校验与显式创建；导入或列出 provider 不连接电话系统。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from llmautotel.models import AppSettings
from llmautotel.telephony.settings import TelephonyProviderName

if TYPE_CHECKING:
    from llmautotel.sessions import VoiceRuntime
    from llmautotel.telephony.incoming import IncomingCall
    from llmautotel.voice import VoiceCallbacks


def missing_phone_fields(
    settings: AppSettings,
    provider: TelephonyProviderName,
    *,
    direction: str = "outbound",
) -> list[str]:
    config = getattr(settings.telephony, provider)
    missing: list[str] = (
        config.missing_inbound_fields() if direction == "inbound" else config.missing_fields()
    )
    if provider in {"asterisk", "freeswitch"}:
        missing += settings.missing_call_fields()
    else:
        missing += settings.missing_conversation_fields()
        for label, value in (
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


def create_incoming_session(
    incoming: IncomingCall,
    call_id: str,
    settings: AppSettings,
    callbacks: VoiceCallbacks,
) -> VoiceRuntime:
    """接管已有来电，不调用任何 originate 或云外呼创建接口。"""
    if incoming.provider in {"aliyun", "tencent"}:
        from llmautotel.telephony.gateway import CloudVoiceSession

        return CloudVoiceSession(
            incoming.provider,
            incoming.caller,
            call_id,
            settings,
            callbacks,
            incoming_remote_id=incoming.remote_id,
        )
    from llmautotel.telephony.asterisk import AsteriskDriver
    from llmautotel.telephony.freeswitch import FreeSwitchDriver
    from llmautotel.telephony.transport import PhoneTransport
    from llmautotel.voice import VoiceSession

    config = settings.telephony
    transport = PhoneTransport(
        lambda events: (
            AsteriskDriver(config.asterisk, events, incoming=incoming)
            if incoming.provider == "asterisk"
            else FreeSwitchDriver(config.freeswitch, events, incoming=incoming)
        ),
        number=incoming.caller,
        call_id=call_id,
    )
    return VoiceSession(None, settings, callbacks, transport=transport)
