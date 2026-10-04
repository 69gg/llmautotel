"""公开网关先认证/校验再流式输出，最终回执与迟到历史的整条 HTTP 路径。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import httpx2
import pytest
from fastapi.testclient import TestClient
from test_providers import sse_text
from test_telephony_gateway import CloudControl, configured_settings, platform_body
from test_telephony_routes import seed_ended_call

from llmautotel.config import RuntimeConfig
from llmautotel.main import create_app
from llmautotel.models import AppSettings
from llmautotel.providers import CompatibleLLMService
from llmautotel.telephony.cloud import TencentCallClient
from llmautotel.telephony.gateway import CloudVoiceSession
from llmautotel.voice import VoiceCallbacks


async def wait_runtime(task: asyncio.Task[Any]) -> None:
    await asyncio.wait_for(task, 2)


async def wait_cloud_ready(sessions: list[CloudVoiceSession]) -> None:
    async with asyncio.timeout(2):
        while not sessions:
            await asyncio.sleep(0)
        await sessions[0]._created.wait()


def test_gateway_http_checks_call_auth_and_final_report_replaces_no_generated_history(
    tmp_path: Path,
) -> None:
    requests: list[dict[str, Any]] = []
    sessions: list[CloudVoiceSession] = []
    controls: list[CloudControl] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(json.loads(await request.aread()))
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse_text("生成稿10元。") + b"data: [DONE]\n\n",
        )

    def factory(
        provider: str,
        number: str,
        call_id: str,
        settings: AppSettings,
        callbacks: VoiceCallbacks,
    ) -> CloudVoiceSession:
        llm = CompatibleLLMService(
            settings.llm,
            http_client=httpx2.AsyncClient(
                transport=httpx2.MockTransport(handler),
            ),
        )
        control = CloudControl()
        session = CloudVoiceSession(
            provider, number, call_id, settings, callbacks, client=control, llm=llm
        )
        sessions.append(session)
        controls.append(control)
        return session

    with TestClient(create_app(RuntimeConfig(data_dir=tmp_path))) as client:
        client.app.state.sessions._phone_factory = factory
        assert client.put("/api/settings", json=configured_settings().private()).status_code == 200
        start = client.post(
            "/api/telephony/calls",
            json={
                "provider": "aliyun",
                "destination": "12300001",
            },
        )
        assert start.status_code == 201
        call_id = start.json()["call"]["id"]
        assert client.portal is not None
        client.portal.call(wait_cloud_ready, sessions)
        body = platform_body("aliyun") | {"out_id": call_id, "biz_params": {"call_id": call_id}}
        endpoint = "/api/telephony/aliyun/llm"
        assert client.post(endpoint, json=body).status_code == 401
        assert (
            client.post(endpoint, json=body, headers={"Authorization": "wrong"}).status_code == 401
        )
        auth = {"Authorization": "TEST-GATEWAY-TOKEN"}
        assert client.post(endpoint, json=body | {"stream": False}, headers=auth).status_code == 422
        wrong_call = client.post(endpoint, json=body | {"out_id": "old-call"}, headers=auth)
        assert wrong_call.status_code == 409
        assert not requests
        response = client.post(endpoint, json=body, headers=auth)
        assert response.status_code == 200 and "生成稿10元。" in response.text
        assert len(requests) == 1
        record = client.get(f"/api/calls/{call_id}").json()
        assert record["transcript"] == [] and record["remote_id"] == "remote-id"
        assert record["settings"]["llm"]["model"] == "configured-model"
        report = [
            {
                "call_id": "remote-id",
                "out_id": call_id,
                "status_code": "200001",
                "end_time": "2026-10-05 00:00:00",
                "hangup_direction": "用户",
                "conversation_record": json.dumps(
                    [
                        {"role": "user", "content": "请挂断。"},
                        {"role": "assistant", "content": "平台实际播放。<user-interrupt/>"},
                    ]
                ),
            }
        ]
        watch = client.app.state.sessions._active.task
        invalid_event = client.post("/api/telephony/aliyun/events?token=wrong", json=report)
        assert invalid_event.status_code == 401
        final = client.post("/api/telephony/aliyun/events?token=TEST-WEBHOOK-TOKEN", json=report)
        assert final.status_code == 200 and final.json() == {"code": 0, "msg": "成功"}
        client.portal.call(wait_runtime, watch)
        saved = client.get(f"/api/calls/{call_id}").json()
        assert [entry["text"] for entry in saved["transcript"]] == ["请挂断。", "平台实际播放。"]
        assert saved["transcript"][1]["interrupted"]
        assert saved["end_reason"] == "remote_hangup"
        assert "生成稿10元" not in str(saved) and "LLM-PRIVATE-KEY" not in str(saved)
        assert controls[0].closed and not controls[0].hangups
        assert client.get("/api/calls/active").json() is None
        assert client.post(endpoint, json=body, headers=auth).status_code == 409


def test_batch_callback_saves_late_history_even_when_current_call_is_consumed(
    tmp_path: Path,
) -> None:
    old_id = seed_ended_call(tmp_path, "aliyun")
    sessions: list[CloudVoiceSession] = []

    def factory(
        provider: str,
        number: str,
        call_id: str,
        settings: AppSettings,
        callbacks: VoiceCallbacks,
    ) -> CloudVoiceSession:
        session = CloudVoiceSession(
            provider, number, call_id, settings, callbacks, client=CloudControl()
        )
        sessions.append(session)
        return session

    with TestClient(create_app(RuntimeConfig(data_dir=tmp_path))) as client:
        client.app.state.sessions._phone_factory = factory
        assert client.put("/api/settings", json=configured_settings().private()).status_code == 200
        original = client.get(f"/api/calls/{old_id}").json()
        start = client.post(
            "/api/telephony/calls",
            json={
                "provider": "aliyun",
                "destination": "12300001",
            },
        )
        assert start.status_code == 201
        current_id = start.json()["call"]["id"]
        assert client.portal is not None
        client.portal.call(wait_cloud_ready, sessions)
        watch = client.app.state.sessions._active.task
        batch = [
            {
                "out_id": old_id,
                "call_id": "remote-final-id",
                "status_code": "200001",
                "end_time": "2026-10-05 00:00:00",
                "conversation_record": json.dumps(
                    [
                        {"role": "assistant", "content": "上一通实际告别。"},
                    ]
                ),
            },
            {
                "out_id": current_id,
                "call_id": "remote-id",
                "status_code": "200002",
                "smart_status_code": "USER_BUSY",
                "end_time": "",
                "duration": 0,
            },
        ]
        result = client.post("/api/telephony/aliyun/events?token=TEST-WEBHOOK-TOKEN", json=batch)
        assert result.status_code == 200
        client.portal.call(wait_runtime, watch)
        old = client.get(f"/api/calls/{old_id}").json()
        assert old["transcript"][0]["text"] == "上一通实际告别。"
        assert old["ended_at"] == original["ended_at"] and old["end_reason"] == "user_hangup"
        current = client.get(f"/api/calls/{current_id}").json()
        assert current["end_reason"] == "busy" and current["transcript"] == []
        assert client.get("/api/calls/active").json() is None


def test_tencent_callback_temporary_query_failure_retries_into_existing_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_client_type = TencentCallClient
    queries: list[httpx.Request] = []
    sessions: list[CloudVoiceSession] = []
    http_clients: list[httpx.AsyncClient] = []

    def capture(request: httpx.Request) -> httpx.Response:
        action = request.headers["X-TC-Action"]
        if action == "CreateAICall":
            return httpx.Response(
                200,
                json={
                    "Response": {
                        "SessionId": "remote-id",
                        "RequestId": "1",
                    }
                },
            )
        assert action == "DescribeAICallInteractionRecords"
        assert request.headers["Authorization"].startswith("TC3-HMAC-SHA256 ")
        assert json.loads(request.content)["SessionId"] == "remote-id"
        queries.append(request)
        if len(queries) == 1:
            return httpx.Response(
                200,
                json={
                    "Response": {
                        "Error": {
                            "Code": "FailedOperation.SessionNotExists",
                            "Message": "TEST-SECRET-KEY",
                        },
                        "RequestId": "2",
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "Response": {
                    "InteractionEventList": [
                        {
                            "Messages": [
                                {
                                    "Timestamp": 1784166669089,
                                    "AISpeak": {"SpokenText": "祝您生活愉快。"},
                                },
                            ]
                        }
                    ],
                    "RequestId": "3",
                }
            },
        )

    def factory(
        provider: str,
        number: str,
        call_id: str,
        settings: AppSettings,
        callbacks: VoiceCallbacks,
    ) -> CloudVoiceSession:
        # factory 在服务端事件循环里执行，避免在已运行循环里使用 asyncio.run。
        cloud = original_client_type(settings.telephony.tencent)
        sdk = cloud._client()
        unused_http = sdk.http_client
        asyncio.create_task(unused_http.aclose())
        sdk.http_client = httpx.AsyncClient(transport=httpx.MockTransport(capture))
        http_clients.append(sdk.http_client)
        session = CloudVoiceSession(provider, number, call_id, settings, callbacks, client=cloud)
        sessions.append(session)
        return session

    async def patched_fetch(self: TencentCallClient, remote_id: str) -> list[Any]:
        # 迟到报告的新 client 仍用真实 SDK；替换 HTTP 传输后调用原公开方法。
        sdk = self._client()
        if sdk.http_client not in http_clients:
            await sdk.http_client.aclose()
            sdk.http_client = httpx.AsyncClient(transport=httpx.MockTransport(capture))
            http_clients.append(sdk.http_client)
        return await original_fetch(self, remote_id)

    original_fetch = original_client_type.fetch_transcript
    monkeypatch.setattr(original_client_type, "fetch_transcript", patched_fetch)
    with TestClient(create_app(RuntimeConfig(data_dir=tmp_path))) as client:
        client.app.state.sessions._phone_factory = factory
        assert client.put("/api/settings", json=configured_settings().private()).status_code == 200
        start = client.post(
            "/api/telephony/calls",
            json={
                "provider": "tencent",
                "destination": "12300001",
            },
        )
        assert start.status_code == 201
        call_id = start.json()["call"]["id"]
        assert client.portal is not None
        client.portal.call(wait_cloud_ready, sessions)
        watch = client.app.state.sessions._active.task
        report = {
            "SdkAppId": 1400000000,
            "SessionId": "remote-id",
            "EndedTimestamp": 1791100800,
            "HungUpSide": "user",
        }
        url = "/api/telephony/tencent/events?token=TEST-WEBHOOK-TOKEN&action=cdr&version=1"
        failed = client.post(url, json=report)
        assert failed.status_code == 503 and "TEST-SECRET-KEY" not in failed.text
        client.portal.call(wait_runtime, watch)
        ended = client.get(f"/api/calls/{call_id}").json()
        assert ended["transcript"] == [] and ended["end_reason"] == "remote_hangup"
        assert client.get("/api/calls/active").json() is None
        retry = client.post(url, json=report)
        assert retry.status_code == 200 and retry.json() == {"ErrCode": 0, "ErrMsg": ""}
        saved = client.get(f"/api/calls/{call_id}").json()
        assert saved["transcript"][0]["text"] == "祝您生活愉快。"
        assert saved["end_reason"] == ended["end_reason"] and saved["ended_at"] == ended["ended_at"]
        assert len(queries) == 2 and all(http.is_closed for http in http_clients)
