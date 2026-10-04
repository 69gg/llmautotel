from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from fastapi.testclient import TestClient

from llmautotel.config import RuntimeConfig
from llmautotel.main import create_app
from llmautotel.models import AppSettings, CallRecord, TranscriptEntry
from llmautotel.sessions import SessionManager
from llmautotel.store import Store


def configured_settings() -> AppSettings:
    return AppSettings.model_validate(
        {
            "sales": {"goal": "订阅产品", "product_info": "每月10元", "opening": "你好"},
            "asr": {"base_url": "http://asr.test/v1", "model": "asr", "api_key": "ASR_SECRET"},
            "llm": {"base_url": "http://llm.test/v1", "model": "llm", "api_key": "LLM_SECRET"},
            "tts": {"base_url": "http://tts.test/v1", "model": "tts", "voice": "custom-voice"},
        }
    )


def test_call_configuration_snapshot_history_and_single_active(tmp_path: Path) -> None:
    runtime = RuntimeConfig(data_dir=tmp_path, frontend_dir=tmp_path / "no-ui")
    with TestClient(create_app(runtime)) as client:
        assert client.post("/api/calls").status_code == 422
        assert client.put("/api/settings", json=configured_settings().private()).status_code == 200
        first = client.post("/api/calls")
        assert first.status_code == 201
        assert "ASR_SECRET" not in first.text and "LLM_SECRET" not in first.text
        call_id = first.json()["call"]["id"]
        assert client.post("/api/calls").status_code == 409
        assert client.delete(f"/api/calls/{call_id}").status_code == 409
        updated = configured_settings()
        updated.sales.goal = "另一个目标"
        client.put("/api/settings", json=updated.private())
        assert client.get(f"/api/calls/{call_id}").json()["settings"]["sales"]["goal"] == "订阅产品"
        ended = client.post(f"/api/calls/{call_id}/end")
        assert ended.json()["status"] == "ended"
        assert ended.json()["end_reason"] == "user_hangup"
        assert client.get("/api/calls/active").json() is None
        assert client.post(f"/api/calls/{call_id}/end").json() == ended.json()
        second = client.post("/api/calls")
        assert second.status_code == 201
        assert second.json()["call"]["settings"]["sales"]["goal"] == "另一个目标"
        client.post(f"/api/calls/{second.json()['call']['id']}/end")
        assert len(client.get("/api/calls").json()) == 2
        assert client.delete(f"/api/calls/{call_id}").status_code == 204
        assert client.get(f"/api/calls/{call_id}").status_code == 404


async def test_simultaneous_starts_reserve_only_one_call(tmp_path: Path) -> None:
    store = Store(tmp_path)
    await store.save_settings(configured_settings())
    manager = SessionManager(store)
    results = await asyncio.gather(manager.start(), manager.start(), return_exceptions=True)
    assert sum(isinstance(result, dict) for result in results) == 1
    assert (
        sum(isinstance(result, HTTPException) and result.status_code == 409 for result in results)
        == 1
    )
    await manager.close()


async def test_abandoned_connection_expires_and_releases_slot(tmp_path: Path) -> None:
    store = Store(tmp_path)
    await store.save_settings(configured_settings())
    manager = SessionManager(store, connection_timeout_seconds=0.01)
    result = await manager.start()
    async with asyncio.timeout(2):
        while await manager.active_record():
            await asyncio.sleep(0.01)
    record = await store.get_call(result["call"]["id"])
    assert record and record.status == "failed" and record.ended_at is not None
    assert record.end_reason == "连接超时，请重新开始"
    assert (await manager.start())["call"]["id"] != record.id
    await manager.close()


def test_invalid_or_stale_signaling_is_rejected(tmp_path: Path) -> None:
    with TestClient(create_app(RuntimeConfig(data_dir=tmp_path))) as client:
        result = client.post("/api/offer?call_id=stale", json={"sdp": "bad", "type": "offer"})
        assert result.status_code == 409
        result = client.post("/api/offer?call_id=stale", json={"sdp": "bad", "type": "answer"})
        assert result.status_code == 422
        assert client.get("/api/calls").json() == []


def test_server_shutdown_finalizes_reservation(tmp_path: Path) -> None:
    runtime = RuntimeConfig(data_dir=tmp_path)
    with TestClient(create_app(runtime)) as client:
        client.put("/api/settings", json=configured_settings().private())
        result: dict[str, Any] = client.post("/api/calls").json()
        call_id = result["call"]["id"]
    with TestClient(create_app(runtime)) as client:
        record = client.get(f"/api/calls/{call_id}").json()
        assert record["status"] == "ended"
        assert record["end_reason"] == "server_shutdown"
        assert record["ended_at"] is not None


async def test_restart_finalizes_crashed_calls_and_preserves_history(tmp_path: Path) -> None:
    store = Store(tmp_path)
    original = CallRecord(
        id="crashed-call",
        started_at="2026-01-01T00:00:00+00:00",
        status="active",
        settings=configured_settings().public(),
        transcript=[
            TranscriptEntry(
                role="assistant",
                text="已经完整播放的介绍。",
                timestamp="2026-01-01T00:00:01+00:00",
            )
        ],
    )
    await store.save_call(original)
    with TestClient(create_app(RuntimeConfig(data_dir=tmp_path))) as client:
        recovered = client.get("/api/calls/crashed-call").json()
        assert recovered["status"] == "failed"
        assert recovered["end_reason"] == "server_restarted"
        assert recovered["ended_at"] is not None
        assert recovered["transcript"] == original.model_dump()["transcript"]
        assert recovered["settings"] == original.settings
        assert client.get("/api/calls/active").json() is None
    with TestClient(create_app(RuntimeConfig(data_dir=tmp_path))) as client:
        assert client.get("/api/calls/crashed-call").json() == recovered


def test_history_storage_never_contains_keys_and_delete_preserves_configuration(
    tmp_path: Path,
) -> None:
    runtime = RuntimeConfig(data_dir=tmp_path)
    settings = configured_settings()
    settings.tts.api_key = settings.asr.api_key
    with TestClient(create_app(runtime)) as client:
        client.put("/api/settings", json=settings.private())
        first = client.post("/api/calls").json()["call"]
        client.post(f"/api/calls/{first['id']}/end")
        second = client.post("/api/calls").json()["call"]
        client.post(f"/api/calls/{second['id']}/end")
        with sqlite3.connect(tmp_path / "app.sqlite3") as connection:
            rows = connection.execute("SELECT body FROM calls").fetchall()
        assert len(rows) == 2
        assert all("SECRET" not in row[0] and '"api_key":' not in row[0] for row in rows)
        summaries = client.get("/api/calls").json()
        assert [record["id"] for record in summaries] == [second["id"], first["id"]]
        assert all("settings" not in record and "transcript" not in record for record in summaries)
        assert client.delete(f"/api/calls/{first['id']}").status_code == 204
        assert client.delete(f"/api/calls/{first['id']}").status_code == 404
        assert client.get(f"/api/calls/{second['id']}").status_code == 200
        assert all(client.get("/api/settings").json()[stage]["api_key_set"] for stage in (
            "asr", "llm", "tts"
        ))
