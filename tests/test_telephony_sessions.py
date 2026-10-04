"""电话与网页共用槽位、启动校验、快照和收尾的可观察行为。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from test_sessions import configured_settings

from llmautotel.config import RuntimeConfig
from llmautotel.main import create_app
from llmautotel.models import AppSettings
from llmautotel.sessions import SessionManager
from llmautotel.store import Store
from llmautotel.telephony.factory import missing_phone_fields
from llmautotel.telephony.settings import TelephonyProviderName
from llmautotel.voice import VoiceCallbacks


def phone_settings() -> AppSettings:
    settings = configured_settings()
    settings.telephony.asterisk = settings.telephony.asterisk.model_validate(
        {
            "enabled": True,
            "ari_url": "http://pbx.test/ari",
            "username": "agent",
            "password": "PBX_SECRET",
            "endpoint_template": "PJSIP/{number}@configured-trunk",
            "caller_id": "01012345678",
        }
    )
    return AppSettings.model_validate(settings.private())


class FakePhone:
    def __init__(self, settings: AppSettings, callbacks: VoiceCallbacks) -> None:
        self.settings = settings
        self.callbacks = callbacks
        self.started = asyncio.Event()
        self.answered = asyncio.Event()
        self.finished = asyncio.Event()
        self.reason = "disconnected"

    async def run(self) -> str:
        await self.callbacks.on_state("dialing")
        self.started.set()
        await self.answered.wait()
        if not self.finished.is_set():
            await self.callbacks.on_state("listening")
            await self.callbacks.on_message("assistant", "接听后的开场", False, "2026-10-04")
        await self.finished.wait()
        return self.reason

    async def stop(self, reason: str = "user_hangup") -> None:
        self.reason = reason
        self.finished.set()
        self.answered.set()


async def test_disabled_and_incomplete_providers_never_construct_runtime(tmp_path: Path) -> None:
    store = Store(tmp_path)
    instances: list[FakePhone] = []

    def factory(
        provider: TelephonyProviderName,
        number: str,
        call_id: str,
        settings: AppSettings,
        callbacks: VoiceCallbacks,
    ) -> FakePhone:
        instance = FakePhone(settings, callbacks)
        instances.append(instance)
        return instance

    manager = SessionManager(store, phone_factory=factory)
    await store.save_settings(configured_settings())
    for provider in ("asterisk", "freeswitch", "aliyun", "tencent"):
        with pytest.raises(HTTPException) as error:
            await manager.start_phone(provider, "13800000000")
        assert error.value.status_code == 422
    settings = configured_settings()
    settings.telephony.asterisk.enabled = True
    await store.save_settings(AppSettings.model_validate(settings.private()))
    with pytest.raises(HTTPException) as error:
        await manager.start_phone("asterisk", "13800000000")
    assert error.value.status_code == 422
    assert instances == [] and await manager.active_record() is None
    assert await store.list_calls() == []


async def test_browser_and_phone_starts_compete_for_same_slot(tmp_path: Path) -> None:
    store = Store(tmp_path)
    await store.save_settings(phone_settings())
    manager = SessionManager(store, phone_factory=lambda p, n, c, s, cb: FakePhone(s, cb))
    results = await asyncio.gather(
        manager.start(),
        manager.start_phone("asterisk", "13800000000"),
        return_exceptions=True,
    )
    assert sum(isinstance(result, dict) for result in results) == 1
    assert (
        sum(isinstance(result, HTTPException) and result.status_code == 409 for result in results)
        == 1
    )
    await manager.close()
    assert await manager.active_record() is None


async def test_dialing_answer_snapshot_and_hangup_then_restart(tmp_path: Path) -> None:
    store = Store(tmp_path)
    settings = phone_settings()
    await store.save_settings(settings)
    phones: list[FakePhone] = []

    def factory(
        provider: TelephonyProviderName,
        number: str,
        call_id: str,
        snapshot: AppSettings,
        callbacks: VoiceCallbacks,
    ) -> FakePhone:
        assert provider == "asterisk" and number == "13800000000" and call_id
        phone = FakePhone(snapshot, callbacks)
        phones.append(phone)
        return phone

    manager = SessionManager(store, phone_factory=factory)
    first = await manager.start_phone("asterisk", "13800000000")
    async with asyncio.timeout(2):
        while not phones:
            await asyncio.sleep(0)
        await phones[0].started.wait()
    record = await manager.active_record()
    assert record and record.status == "connecting" and record.state == "dialing"
    assert record.channel == "telephone" and record.provider == "asterisk"
    assert record.transcript == [] and "SECRET" not in record.model_dump_json()
    settings.sales.goal = "新目标"
    settings.telephony.asterisk.enabled = False
    await store.save_settings(settings)
    assert phones[0].settings.sales.goal == "订阅产品"
    assert phones[0].settings.telephony.asterisk.enabled
    phones[0].answered.set()
    async with asyncio.timeout(2):
        while not (await manager.active_record()).transcript:
            await asyncio.sleep(0)
    ended = await manager.end(first["call"]["id"])
    assert ended.status == "ended" and ended.state == "ended"
    assert ended.end_reason == "user_hangup" and len(ended.transcript) == 1
    assert (await manager.end(ended.id)).model_dump() == ended.model_dump()
    assert await manager.active_record() is None
    assert (await manager.start())["call"]["id"] != ended.id
    await manager.close()
    summary = await store.list_calls()
    assert any(
        row["provider"] == "asterisk" and row["destination"] == "13800000000" for row in summary
    )


async def test_hangup_before_task_starts_does_not_construct_provider(tmp_path: Path) -> None:
    store = Store(tmp_path)
    await store.save_settings(phone_settings())
    phones: list[FakePhone] = []

    def factory(
        provider: TelephonyProviderName,
        number: str,
        call_id: str,
        settings: AppSettings,
        callbacks: VoiceCallbacks,
    ) -> FakePhone:
        phone = FakePhone(settings, callbacks)
        phones.append(phone)
        return phone

    manager = SessionManager(store, phone_factory=factory)
    call = await manager.start_phone("asterisk", "13800000000")
    await manager.end(call["call"]["id"])
    assert phones == []
    assert await manager.active_record() is None


async def test_stop_failure_is_safe_and_still_releases_slot(tmp_path: Path) -> None:
    store = Store(tmp_path)
    await store.save_settings(phone_settings())

    class FailingStop(FakePhone):
        async def stop(self, reason: str = "user_hangup") -> None:
            raise RuntimeError("PBX_SECRET unsafe upstream error")

    phone: FailingStop | None = None

    def factory(
        provider: TelephonyProviderName,
        number: str,
        call_id: str,
        settings: AppSettings,
        callbacks: VoiceCallbacks,
    ) -> FailingStop:
        nonlocal phone
        phone = FailingStop(settings, callbacks)
        return phone

    manager = SessionManager(store, phone_factory=factory)
    call = await manager.start_phone("asterisk", "13800000000")
    async with asyncio.timeout(2):
        while phone is None:
            await asyncio.sleep(0)
        await phone.started.wait()
    ended = await manager.end(call["call"]["id"])
    assert ended.status == "failed" and "PBX_SECRET" not in ended.model_dump_json()
    assert "停止通话失败" in ended.end_reason
    assert await manager.active_record() is None


def test_cloud_requires_llm_and_sales_but_not_local_asr_tts() -> None:
    settings = AppSettings.model_validate(
        {
            "sales": {"goal": "预约演示", "product_info": "产品功能"},
            "llm": {"base_url": "http://llm.test/v1", "model": "configured-model"},
            "telephony": {
                "public_base_url": "https://configured-gateway.test",
                "aliyun": {
                    "enabled": True,
                    "endpoint": "https://configured-cloud.test",
                    "region": "configured-region",
                    "access_key_id": "ID",
                    "access_key_secret": "KEY",
                    "app_id": "configured-app",
                    "caller_id": "01012345678",
                    "gateway_token": "GATEWAY",
                    "webhook_token": "EVENT",
                },
            },
        }
    )
    assert missing_phone_fields(settings, "aliyun") == []
    assert missing_phone_fields(settings, "asterisk")


def test_start_endpoint_rejects_disabled_unknown_provider_and_command_injection(
    tmp_path: Path,
) -> None:
    with TestClient(create_app(RuntimeConfig(data_dir=tmp_path))) as client:
        for provider, destination in (
            ("asterisk", "13800000000"),
            ("unknown", "13800000000"),
            ("freeswitch", "13800000000\napi shutdown"),
        ):
            response = client.post(
                "/api/telephony/calls",
                json={
                    "provider": provider,
                    "destination": destination,
                },
            )
            assert response.status_code == 422
        assert client.get("/api/calls/active").json() is None
        assert client.get("/api/calls").json() == []
