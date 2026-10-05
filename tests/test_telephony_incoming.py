"""来电占槽前后的单通回收及配置边界。"""

from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from llmautotel.telephony.incoming import IncomingCall, IncomingEvents
from llmautotel.telephony.settings import (
    AsteriskSettings,
    FreeswitchSettings,
    TelephonySettings,
)


async def test_cancelled_prestart_hangup_waits_for_remote_cleanup_and_unsubscribes() -> None:
    requested = asyncio.Event()
    finished = asyncio.Event()
    released: list[IncomingEvents] = []
    reasons: list[str] = []

    async def hangup(reason: str) -> None:
        reasons.append(reason)
        requested.set()
        await finished.wait()

    events = IncomingEvents("remote.1", released.append)
    call = IncomingCall("asterisk", "remote.1", "13800138000", "4001234567", events, hangup)
    task = asyncio.create_task(call.close())
    await requested.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done() and released == []
    finished.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await call.close()
    assert reasons == ["normal"] and released == [events]


async def test_event_overload_disconnects_only_its_subscription() -> None:
    released: list[IncomingEvents] = []
    first = IncomingEvents("one", released.append)
    second = IncomingEvents("two", released.append)
    for _ in range(257):
        first.put({"type": "noise"})
    second.put({"type": "kept"})
    with pytest.raises(StopAsyncIteration):
        await first.__anext__()
    assert await second.__anext__() == {"type": "kept"}
    first.close()
    assert released == [first]
    second.close()


def test_inbound_settings_independent_defaults_and_public_secrets() -> None:
    settings = TelephonySettings()
    for provider in (settings.asterisk, settings.freeswitch, settings.aliyun, settings.tencent):
        assert not provider.enabled and not provider.inbound_enabled
        assert provider.inbound_numbers == []
    settings.asterisk = AsteriskSettings(
        password="inbound-secret", inbound_numbers=["4001234567", "4001234567"]
    )
    assert settings.asterisk.inbound_numbers == ["4001234567"]
    public = settings.public()["asterisk"]
    assert "password" not in public and public["password_set"]
    assert "inbound-secret" not in str(public)
    for invalid in (["number"], ["1\napi kill all"], ["12"], ["400 123 4567"]):
        with pytest.raises(ValidationError):
            AsteriskSettings(inbound_numbers=invalid)
    with pytest.raises(ValidationError):
        FreeswitchSettings(inbound_marker="marker\napi uuid_kill all")
