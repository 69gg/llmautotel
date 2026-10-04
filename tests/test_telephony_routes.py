"""公网模型网关与最终回执仅认证关联的已有会话。"""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from llmautotel.config import RuntimeConfig
from llmautotel.main import create_app
from llmautotel.models import AppSettings, CallRecord
from llmautotel.store import Store


def seed_ended_call(path: Path, provider: str) -> str:
    call_id = f"{provider}-ended-local"
    settings = AppSettings.model_validate(
        {
            "telephony": {
                provider: {
                    "webhook_token": "TEST_WEBHOOK",
                    **({"sdk_app_id": 123} if provider == "tencent" else {}),
                },
            }
        }
    )
    store = Store(path)
    asyncio.run(store.save_settings(settings))
    asyncio.run(
        store.save_call(
            CallRecord(
                id=call_id,
                started_at=datetime.now(UTC).isoformat(),
                ended_at=datetime.now(UTC).isoformat(),
                status="ended",
                state="ended",
                channel="telephone",
                provider=provider,
                destination="13800000000",
                remote_id="remote-final-id",
                settings=settings.public(),
                end_reason="user_hangup",
            )
        )
    )
    return call_id


def test_late_aliyun_report_updates_existing_history_and_is_idempotent(tmp_path: Path) -> None:
    call_id = seed_ended_call(tmp_path, "aliyun")
    body = [
        {
            "out_id": call_id,
            "call_id": "remote-final-id",
            "end_time": "2026-10-05 00:00:00",
            "conversation_record": json.dumps(
                [
                    {"role": "user", "content": "不需要"},
                    {"role": "assistant", "content": "祝您生活愉快<hangup/>"},
                ]
            ),
        }
    ]
    with TestClient(create_app(RuntimeConfig(data_dir=tmp_path))) as client:
        assert client.post("/api/telephony/aliyun/events", json=body).status_code == 401
        assert client.post("/api/telephony/aliyun/events?token=wrong", json=body).status_code == 401
        for _ in range(2):
            response = client.post("/api/telephony/aliyun/events?token=TEST_WEBHOOK", json=body)
            assert response.status_code == 200 and response.json()["code"] == 0
        record = client.get(f"/api/calls/{call_id}").json()
        assert record["end_reason"] == "user_hangup" and len(record["transcript"]) == 2
        assert record["transcript"][1]["text"] == "祝您生活愉快"
        assert "TEST_WEBHOOK" not in str(record)
        assert client.get("/api/calls/active").json() is None
        # 正确 token 也不能把错误 remote_id 覆盖到已有记录，或新建不存在的电话。
        body[0]["call_id"] = "another-remote"
        assert (
            client.post("/api/telephony/aliyun/events?token=TEST_WEBHOOK", json=body).status_code
            == 200
        )
        assert client.get(f"/api/calls/{call_id}").json() == record
        body[0]["out_id"] = "unknown-call"
        assert (
            client.post("/api/telephony/aliyun/events?token=TEST_WEBHOOK", json=body).status_code
            == 200
        )
        assert len(client.get("/api/calls").json()) == 1


def test_disabled_tencent_callback_basic_auth_does_not_connect_cloud(
    tmp_path: Path, monkeypatch: Any
) -> None:
    from llmautotel.telephony.cloud import TencentCallClient

    call_id = seed_ended_call(tmp_path, "tencent")
    requests: list[str] = []

    async def forbidden_fetch(self: TencentCallClient, remote_id: str) -> list[Any]:
        requests.append(remote_id)
        raise AssertionError("disabled provider must not open SDK client")

    monkeypatch.setattr(TencentCallClient, "fetch_transcript", forbidden_fetch)
    body = {
        "SdkAppId": 123,
        "SessionId": "remote-final-id",
        "EndedTimestamp": 1791100800,
        "HungUpSide": "user",
    }
    auth = "Basic " + base64.b64encode(b"123:TEST_WEBHOOK").decode()
    with TestClient(create_app(RuntimeConfig(data_dir=tmp_path))) as client:
        response = client.post(
            "/api/telephony/tencent/events?action=cdr&version=1",
            json=body,
            headers={"Authorization": auth},
        )
        assert response.status_code == 200 and response.json() == {"ErrCode": 0, "ErrMsg": ""}
        assert requests == []
        assert client.get(f"/api/calls/{call_id}").json()["end_reason"] == "user_hangup"
        assert client.get("/api/calls/active").json() is None


def test_no_active_cloud_session_cannot_open_model_gateway(tmp_path: Path) -> None:
    with TestClient(create_app(RuntimeConfig(data_dir=tmp_path))) as client:
        for path in ("/api/telephony/aliyun/llm", "/api/telephony/tencent/llm/chat/completions"):
            assert client.post(path, json={"stream": True, "messages": []}).status_code == 409
        assert client.get("/api/calls").json() == []


def test_shared_start_api_checks_phone_number_before_reserving_call(tmp_path: Path) -> None:
    with TestClient(create_app(RuntimeConfig(data_dir=tmp_path))) as client:
        for number in ("123", "1" * 16, "13800000000;shutdown"):
            assert (
                client.post(
                    "/api/telephony/calls", json={"provider": "asterisk", "destination": number}
                ).status_code
                == 422
            )
        assert client.get("/api/calls").json() == []
