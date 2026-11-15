"""GPU 本地模型适配、鉴权和实际播放提交回归；不运行模型。"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import threading
import wave
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pipecat.processors.aggregators.llm_context import LLMContext
from starlette.websockets import WebSocketDisconnect

from experiments.openi_latency.realtime_service import GPUDriver, attach_realtime, local_services
from llmautotel.models import AppSettings
from llmautotel.remote_voice import remote_snapshot, remote_url
from llmautotel.telephony.base import MediaCallbacks


def configuration() -> tuple[AppSettings, dict[str, Any]]:
    cfg = json.loads(Path("experiments/openi_latency/settings.service.json").read_text())
    cfg["token_sha256"] = hashlib.sha256(b"test-key").hexdigest()
    settings = AppSettings()
    for stage, model in [("asr", "asr_model"), ("llm", "llm_model"), ("tts", "service_tts_model")]:
        provider = getattr(settings, stage)
        provider.base_url = "https://gpu.example/v1"
        provider.model = cfg[model]
    settings.tts.voice = cfg["service_voice"]
    settings.tts.instructions = ""
    return settings, cfg


def test_snapshot_removes_all_keys_and_telephony_and_prevents_relay_recursion() -> None:
    settings, _ = configuration()
    from pydantic import SecretStr

    for stage in ("asr", "llm", "tts"):
        getattr(settings, stage).api_key = SecretStr("private-test-credential")
    settings.voice.pipeline = "gpu"
    snapshot = remote_snapshot(settings)
    assert "private-test-credential" not in json.dumps(snapshot)
    assert "telephony" not in snapshot
    assert snapshot["voice"]["pipeline"] == "local"
    assert remote_url(settings) == "wss://gpu.example/v1/realtime"
    assert AppSettings.model_validate(snapshot).consultation == settings.consultation


async def test_memory_services_preserve_real_llm_tools_and_wav_pcm() -> None:
    settings, _ = configuration()
    received: list[dict[str, Any]] = []

    class Backend:
        def transcribe(self, audio: bytes, stop: threading.Event) -> Iterator[str]:
            assert audio == b"\x01\x02" * 320
            yield "价格多少？"

        def chat(self, body: dict[str, Any], stop: threading.Event) -> Iterator[dict[str, Any]]:
            received.append(body)
            yield {"content": "每月99元。"}
            yield {"_finish": "stop"}

        def speech(self, text: str, stop: threading.Event) -> Iterator[bytes]:
            assert text == "每月99元。"
            yield b"\x03\x04" * 200

    services = local_services(settings, Backend())
    try:
        data = io.BytesIO()
        with wave.open(data, "wb") as target:
            target.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            target.writeframes(b"\x01\x02" * 320)
        assert (await services.stt._transcribe(data.getvalue())).text == "价格多少？"
        context = LLMContext([{"role": "user", "content": "价格多少？"}])
        stream = await services.llm.get_chat_completions(context)
        assert [c.choices[0].delta.content async for c in stream] == ["每月99元。", None]
        await stream.close()
        assert received[0]["messages"] == [{"role": "user", "content": "价格多少？"}]
        assert (
            b"".join([pcm async for pcm in services.tts._audio_chunks("每月99元。")])
            == b"\x03\x04" * 200
        )
    finally:
        await services.aclose()


class Socket:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def send_json(self, event: Any) -> None:
        self.events.append(event)

    async def send_bytes(self, data: bytes) -> None:
        self.events.append(data)


async def test_actual_playback_ack_and_old_generation_cannot_commit() -> None:
    async def noop(*args: Any) -> None:
        pass

    socket = Socket()
    driver = GPUDriver(MediaCallbacks(noop, noop, noop, noop), socket, 24000)  # type: ignore[arg-type]
    await driver.send_audio(b"\x01\x02")
    assert socket.events[1][:4] == b"\x00\x00\x00\x00"
    first = asyncio.create_task(driver.wait_played())
    await asyncio.sleep(0)
    checkpoint = socket.events[-1]
    driver.played({**checkpoint, "generation": 5})
    assert not first.done()
    await driver.flush()
    with pytest.raises(asyncio.CancelledError):
        await first
    second = asyncio.create_task(driver.wait_played())
    await asyncio.sleep(0)
    driver.played(checkpoint)
    assert not second.done()
    driver.played(socket.events[-1])
    await asyncio.wait_for(second, 1)
    await driver.close()


def test_websocket_rejects_missing_auth_credentials_and_wrong_model() -> None:
    settings, cfg = configuration()
    app = FastAPI()
    attach_realtime(app, cfg, object())
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/v1/realtime"):
                pass
        for mutation in ({"llm": {"model": "wrong"}}, {"asr": {"api_key": "must-not-send"}}):
            snapshot = remote_snapshot(settings)
            for stage, fields in mutation.items():
                snapshot[stage].update(fields)
            with client.websocket_connect(
                "/v1/realtime", headers={"Authorization": "Bearer test-key"}
            ) as ws:
                ws.send_json({"type": "start", "settings": snapshot})
                assert ws.receive_json()["type"] == "error"


def test_real_pipeline_opening_waits_for_playback_and_releases_slot() -> None:
    settings, cfg = configuration()
    settings.consultation.opening = "您好，请问有什么可以帮您？"
    synthesized: list[str] = []

    class Backend:
        def speech(self, text: str, stop: threading.Event) -> Iterator[bytes]:
            synthesized.append(text)
            yield b"\x01\x10" * 2400

    app = FastAPI()
    attach_realtime(app, cfg, Backend())
    with TestClient(app) as client:
        for _ in range(2):
            with client.websocket_connect(
                "/v1/realtime", headers={"Authorization": "Bearer test-key"}
            ) as ws:
                ws.send_json({"type": "start", "settings": remote_snapshot(settings)})
                heard = False
                while True:
                    packet = ws.receive()
                    if packet.get("bytes"):
                        assert packet["bytes"][:4] == b"\x00\x00\x00\x00"
                        heard = True
                        continue
                    event = json.loads(packet["text"])
                    assert event["type"] != "error", event
                    if event["type"] == "drain":
                        assert heard
                        ws.send_json({**event, "type": "played"})
                    elif event["type"] == "transcript":
                        assert event["entry"]["text"] == settings.consultation.opening
                        break
    assert len(synthesized) == 2
