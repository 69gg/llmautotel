"""只验证原型计时和连接隔离，不以模拟结果替代 GPU 实测。"""

from __future__ import annotations

import hashlib
import struct
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from experiments.openi_latency.client import wait_ready
from experiments.openi_latency.gateway import create_app
from experiments.openi_latency.prepare_worker import image_requirements
from experiments.openi_latency.protocol import (
    PlaybackClock,
    signal_offset_ms,
    summarize,
    trim_silence,
)


def test_effective_audio_includes_leading_silence_and_playback_queue() -> None:
    silence = b"\0\0" * 320
    speech = struct.pack("<h", 1000) * 320
    assert signal_offset_ms(silence + speech, 16000) == 20
    assert trim_silence(speech + silence, 16000) == speech
    clock = PlaybackClock()
    clock.feed(silence * 2, 16000, 1)
    clock.feed(silence + speech, 16000, 1.01)
    assert clock.first_signal_at == pytest.approx(1.06)
    assert clock.queued_until == pytest.approx(1.08)


def test_summary_keeps_failure_count() -> None:
    result = summarize([{"ok": True, "effective_audio_ms": 800}, {"ok": False}])
    assert result["attempted"] == 2
    assert result["successful"] == 1
    assert result["failed"] == 1
    assert result["p50_ms"] == 800
    with pytest.raises(ValueError, match="silent_fixture"):
        trim_silence(b"\0\0" * 100, 16000)


def test_gateway_requires_token_and_allows_only_one_client() -> None:
    config: dict[str, Any] = {
        "token_sha256": hashlib.sha256(b"test-only-token").hexdigest(),
        "lifetime_seconds": 300,
    }
    with TestClient(create_app(config)) as client:
        assert client.get("/health").json()["phase"] == "proxy_ready"
        assert client.get("/status").status_code == 401
        with client.websocket_connect("/ws") as wrong:
            wrong.send_json({"token": "wrong"})
            with pytest.raises(WebSocketDisconnect) as error:
                wrong.receive_json()
            assert error.value.code == 1008
        with client.websocket_connect("/ws") as first:
            first.send_json({"token": "test-only-token"})
            assert first.receive_json()["type"] == "hello"
            first.send_json({"type": "ping", "sequence": 3})
            assert first.receive_json() == {"type": "pong", "sequence": 3}
            with client.websocket_connect("/ws") as second:
                second.send_json({"token": "test-only-token"})
                with pytest.raises(WebSocketDisconnect) as error:
                    second.receive_json()
                assert error.value.code == 1013


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["ready", "worker_exited", "model_initialization_failed"])
async def test_initialization_poll_uses_authenticated_http_and_surfaces_failure(phase: str) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/status"
        assert request.headers["Authorization"] == "Bearer test-only-token"
        return httpx.Response(200, json={"phase": phase, "logs": []})

    async with httpx.AsyncClient(
        base_url="https://test.invalid",
        headers={"Authorization": "Bearer test-only-token"},
        transport=httpx.MockTransport(handle),
    ) as client:
        if phase == "ready":
            assert (await wait_ready(client))["phase"] == "ready"
        else:
            with pytest.raises(RuntimeError, match=phase):
                await wait_ready(client)


def test_verified_image_stack_is_not_downloaded_again() -> None:
    lock = (
        "torch==2.6.0\ntorchaudio==2.6.0\nnvidia-cudnn-cu12==9.1.0\n"
        "triton==3.2.0\nfunasr==1.4.19\n"
    )
    assert image_requirements(lock) == "funasr==1.4.19\n"
