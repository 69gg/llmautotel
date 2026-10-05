"""云来电正式 HTTP 路由的鉴权、幂等、忙线与回执，不连接真实线路。"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from test_telephony_gateway import CloudControl, configured_settings
from test_telephony_gateway_api import wait_cloud_ready, wait_runtime

from llmautotel.config import RuntimeConfig
from llmautotel.main import create_app
from llmautotel.models import AppSettings
from llmautotel.telephony.gateway import CloudVoiceSession
from llmautotel.telephony.incoming import IncomingCall
from llmautotel.voice import VoiceCallbacks


def cloud_settings() -> AppSettings:
    settings = configured_settings()
    settings.conversation.mode = "consultation"
    settings.consultation.product_info = "产品可以整理资料和协作。"
    for provider in ("aliyun", "tencent"):
        config = getattr(settings.telephony, provider)
        config.inbound_enabled = True
        config.inbound_numbers = ["01012345678"]
    settings.telephony.aliyun.account_uid = "123456"
    settings.telephony.tencent.inbound_ai_agent_id = 321
    return AppSettings.model_validate(settings.private())


def body_for(settings: AppSettings, provider: str, remote_id: str) -> dict[str, Any]:
    if provider == "aliyun":
        return {
            "caller": "13800000000",
            "callee": "01012345678",
            "callId": remote_id,
            "applicationCode": settings.telephony.aliyun.app_id,
        }
    return {
        "Event": "callInBound",
        "SessionId": remote_id,
        "SdkAppId": settings.telephony.tencent.sdk_app_id,
        "CallInBound": {"Caller": "13800000000", "Callee": "01012345678", "AIAgentId": 321},
    }


def headers_for(settings: AppSettings, provider: str) -> dict[str, str]:
    if provider == "tencent":
        return {}
    stamp = str(int(time.time() * 1000))
    signature = hashlib.md5(
        ("13800000000" + settings.telephony.aliyun.account_uid + stamp).encode(),
        usedforsecurity=False,
    ).hexdigest()
    return {"timestamp": stamp, "auth": signature}


@pytest.mark.parametrize("provider", ["aliyun", "tencent"])
def test_inbound_callback_never_originates_and_retries_preserve_one_snapshot(
    tmp_path: Path,
    provider: str,
) -> None:
    sessions: list[CloudVoiceSession] = []
    controls: list[CloudControl] = []

    def factory(
        incoming: IncomingCall,
        call_id: str,
        settings: AppSettings,
        callbacks: VoiceCallbacks,
    ) -> CloudVoiceSession:
        control = CloudControl()
        voice = CloudVoiceSession(
            incoming.provider,
            incoming.caller,
            call_id,
            settings,
            callbacks,
            client=control,
            incoming_remote_id=incoming.remote_id,
        )
        controls.append(control)
        sessions.append(voice)
        return voice

    settings = cloud_settings()
    body = body_for(settings, provider, "remote-inbound")
    headers = headers_for(settings, provider)
    endpoint = f"/api/telephony/{provider}/inbound?token=TEST-WEBHOOK-TOKEN"
    with TestClient(create_app(RuntimeConfig(data_dir=tmp_path))) as client:
        client.app.state.sessions._incoming_factory = factory
        assert client.put("/api/settings", json=settings.private()).status_code == 200
        first = client.post(endpoint, json=body, headers=headers)
        assert first.status_code == 200
        assert client.portal is not None
        client.portal.call(wait_cloud_ready, sessions)
        call = client.get("/api/calls/active").json()
        local_id = call["id"]
        assert call["direction"] == "inbound" and call["caller"] == "13800000000"
        assert call["destination"] == "01012345678" and call["remote_id"] == "remote-inbound"
        assert call["settings"]["conversation"]["mode"] == "consultation"
        assert controls[0].requests == []
        assert client.post(endpoint, json=body, headers=headers).json() == first.json()
        assert len(sessions) == 1 and len(client.get("/api/calls").json()) == 1
        if provider == "aliyun":
            assert json.loads(first.json()["bizParam"]) == {"call_id": local_id}
        else:
            assert first.json()["CallInBound"]["Variables"] == [
                {"Key": "llmautotel_call_id", "Value": local_id}
            ]
        # 下一通模式配置不会改变当前云模型网关快照。
        settings.conversation.mode = "sales"
        if provider == "tencent":
            settings.telephony.tencent.inbound_ai_agent_id = 987
        assert client.put("/api/settings", json=settings.private()).status_code == 200
        assert sessions[0].settings.conversation.mode == "consultation"
        assert client.post(endpoint, json=body, headers=headers).json() == first.json()
        watch = client.app.state.sessions._active.task
        ended = client.post(f"/api/calls/{local_id}/end")
        assert ended.status_code == 200 and controls[0].hangups == ["remote-inbound"]
        client.portal.call(wait_runtime, watch)
        assert client.post(endpoint, json=body, headers=headers).json() == first.json()
        assert client.get("/api/calls/active").json() is None
        assert len(sessions) == 1


def test_tencent_busy_returns_official_empty_route_and_wrong_call_cannot_enter(
    tmp_path: Path,
) -> None:
    settings = cloud_settings()
    with TestClient(create_app(RuntimeConfig(data_dir=tmp_path))) as client:
        client.app.state.sessions._incoming_factory = lambda i, c, s, cb: CloudVoiceSession(
            i.provider, i.caller, c, s, cb, client=CloudControl(), incoming_remote_id=i.remote_id
        )
        client.put("/api/settings", json=settings.private())
        endpoint = "/api/telephony/tencent/inbound?token=TEST-WEBHOOK-TOKEN"
        assert client.post(endpoint, json=body_for(settings, "tencent", "first")).status_code == 200
        assert client.post(endpoint, json=body_for(settings, "tencent", "busy")).json() == {
            "CallInBound": {}
        }
        body = body_for(settings, "tencent", "wrong")
        body["SdkAppId"] = 999999
        assert client.post(endpoint, json=body).status_code == 401
        body = body_for(settings, "tencent", "wrong")
        body["CallInBound"]["Callee"] = "01000000000"
        assert client.post(endpoint, json=body).status_code == 401
        assert len(client.get("/api/calls").json()) == 1


def test_aliyun_busy_requests_real_hangup_but_disabled_callback_has_no_side_effect(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    hangups: list[str] = []

    async def fake_reject(config: object, remote_id: str) -> None:
        hangups.append(remote_id)

    monkeypatch.setattr("llmautotel.telephony.cloud_inbound.reject_aliyun_inbound", fake_reject)
    settings = cloud_settings()
    with TestClient(create_app(RuntimeConfig(data_dir=tmp_path))) as client:
        client.app.state.sessions._incoming_factory = lambda i, c, s, cb: CloudVoiceSession(
            i.provider, i.caller, c, s, cb, client=CloudControl(), incoming_remote_id=i.remote_id
        )
        client.put("/api/settings", json=settings.private())
        endpoint = "/api/telephony/aliyun/inbound?token=TEST-WEBHOOK-TOKEN"
        headers = headers_for(settings, "aliyun")
        assert (
            client.post(
                endpoint, json=body_for(settings, "aliyun", "first"), headers=headers
            ).status_code
            == 200
        )
        assert (
            client.post(
                endpoint, json=body_for(settings, "aliyun", "busy"), headers=headers
            ).status_code
            == 409
        )
        assert hangups == ["busy"]
        settings.telephony.aliyun.inbound_enabled = False
        client.put("/api/settings", json=settings.private())
        assert (
            client.post(
                endpoint, json=body_for(settings, "aliyun", "disabled"), headers=headers
            ).status_code
            == 401
        )
        assert hangups == ["busy"]
        assert len(client.get("/api/calls").json()) == 1


def test_new_app_inbound_status_is_default_disabled_and_contains_no_credentials(
    tmp_path: Path,
) -> None:
    with TestClient(create_app(RuntimeConfig(data_dir=tmp_path))) as client:
        settings = client.get("/api/settings").json()
        assert settings["conversation"]["mode"] == "consultation"
        status = client.get("/api/telephony/inbound")
        assert status.status_code == 200 and len(status.json()) == 4
        assert all(row["state"] == "disabled" for row in status.json())
        assert all(
            not settings["telephony"][row["provider"]]["inbound_enabled"] for row in status.json()
        )
        assert client.get("/api/calls").json() == []


def test_cloud_authentication_snapshot_cannot_be_replaced_during_admission(
    tmp_path: Path,
) -> None:
    settings = cloud_settings()
    with TestClient(create_app(RuntimeConfig(data_dir=tmp_path))) as client:
        client.put("/api/settings", json=settings.private())
        manager = client.app.state.sessions
        original = manager.accept_incoming

        async def changed_before_admission(incoming: IncomingCall, **kwargs: Any) -> Any:
            changed = settings.model_copy(deep=True)
            changed.telephony.tencent.inbound_ai_agent_id = 987
            await manager.store.save_settings(changed)
            return await original(incoming, **kwargs)

        manager.accept_incoming = changed_before_admission
        result = client.post(
            "/api/telephony/tencent/inbound?token=TEST-WEBHOOK-TOKEN",
            json=body_for(settings, "tencent", "changed"),
        )
        assert result.json() == {"CallInBound": {}}
        assert client.get("/api/calls").json() == []


@pytest.mark.parametrize("provider", ["aliyun", "tencent"])
def test_gateway_probe_is_authenticated_has_no_call_and_active_requests_cannot_bypass_ids(
    tmp_path: Path,
    provider: str,
) -> None:
    settings = cloud_settings()
    with TestClient(create_app(RuntimeConfig(data_dir=tmp_path))) as client:
        client.app.state.sessions._incoming_factory = lambda i, c, s, cb: CloudVoiceSession(
            i.provider, i.caller, c, s, cb, client=CloudControl(), incoming_remote_id=i.remote_id
        )
        client.put("/api/settings", json=settings.private())
        gateway = f"/api/telephony/{provider}/llm" + (
            "/chat/completions" if provider == "tencent" else ""
        )
        body = {"stream": True, "messages": [{"role": "user", "content": "检验网关是否可用"}]}
        auth = {
            "Authorization": ("Bearer " if provider == "tencent" else "") + "TEST-GATEWAY-TOKEN"
        }
        assert client.post(gateway, json=body).status_code == 409
        probe = client.post(gateway, json=body, headers=auth)
        assert probe.status_code == 200 and "网关连接正常" in probe.text and "[DONE]" in probe.text
        assert client.get("/api/calls").json() == []
        assert (
            client.post(gateway, json=body | {"call_id": "stale"}, headers=auth).status_code == 409
        )
        callback = client.post(
            f"/api/telephony/{provider}/inbound?token=TEST-WEBHOOK-TOKEN",
            json=body_for(settings, provider, "actual-call"),
            headers=headers_for(settings, provider),
        )
        assert callback.status_code == 200
        # 有任何活动通话时，缺标识的请求只能拒绝，不能变成探测并发往电话平台。
        invalid = client.post(gateway, json=body, headers=auth)
        assert invalid.status_code in {409, 503}
        assert "网关连接正常" not in invalid.text


@pytest.mark.parametrize("stream", [False, None, "omitted"])
def test_tencent_nonstream_probe_returns_standard_json_without_model_or_call(
    tmp_path: Path,
    stream: bool | str | None,
) -> None:
    with TestClient(create_app(RuntimeConfig(data_dir=tmp_path))) as client:
        client.put("/api/settings", json=cloud_settings().private())
        body: dict[str, Any] = {"messages": [{"role": "user", "content": "校验接口"}]}
        if stream != "omitted":
            body["stream"] = stream
        response = client.post(
            "/api/telephony/tencent/llm/chat/completions",
            json=body,
            headers={"Authorization": "Bearer TEST-GATEWAY-TOKEN"},
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/json")
        assert response.json()["object"] == "chat.completion"
        content = response.json()["choices"][0]["message"]["content"]
        assert "网关连接正常" in content and "未调用模型" in content
        assert client.get("/api/calls").json() == []


@pytest.mark.parametrize("provider", ["aliyun", "tencent"])
def test_gateway_probe_rechecks_slot_after_settings_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
) -> None:
    settings = cloud_settings()
    settings.asr.base_url = "http://fixture.invalid/v1"
    settings.asr.model = "fixture-asr"
    settings.tts.base_url = "http://fixture.invalid/v1"
    settings.tts.model = "fixture-tts"
    settings.tts.voice = "fixture-voice"
    with TestClient(create_app(RuntimeConfig(data_dir=tmp_path))) as client:
        assert client.put("/api/settings", json=settings.private()).status_code == 200
        assert client.portal is not None
        # 隔离后台配置轮询，让下面的介入只发生在真实模型网关路由的读取中。
        client.portal.call(client.app.state.incoming.close)
        manager = client.app.state.sessions
        original = manager.store.get_settings
        entered = False
        started: dict[str, Any] | None = None

        async def occupy_during_settings_read() -> AppSettings:
            nonlocal entered, started
            snapshot = await original()
            if not entered:
                entered = True
                # start 再读配置时走原逻辑，不递归介入；不建立实际媒体连接。
                started = await manager.start()
            return snapshot

        monkeypatch.setattr(manager.store, "get_settings", occupy_during_settings_read)
        gateway = f"/api/telephony/{provider}/llm" + (
            "/chat/completions" if provider == "tencent" else ""
        )
        response = client.post(
            gateway,
            json={"stream": True, "messages": [{"role": "user", "content": "检查网关"}]},
            headers={
                "Authorization": ("Bearer " if provider == "tencent" else "") + "TEST-GATEWAY-TOKEN"
            },
        )
        assert entered and started is not None
        assert response.status_code == 409
        assert "网关连接正常" not in response.text and "[DONE]" not in response.text
        active = client.get("/api/calls/active").json()
        assert active["id"] == started["call"]["id"]
        assert active["channel"] == "browser"
        assert len(client.get("/api/calls").json()) == 1
