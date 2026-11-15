"""常驻接口协议与取消回归，不加载模型、不占用 GPU。"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import struct
import threading
import time
import wave
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from experiments.openi_latency.hybrid_api import (
    FirstAudioPriority,
    ModelAPI,
    ToolDecoder,
    bridge,
    create_app,
    wav_pcm,
)


def test_first_audio_priority_pauses_decode_and_releases_idempotently() -> None:
    priority = FirstAudioPriority(True)
    entered, done = threading.Event(), threading.Event()

    def decode() -> None:
        entered.set()
        assert not priority.wait(lambda: False)
        done.set()

    with priority.hold() as release:
        worker = threading.Thread(target=decode)
        worker.start()
        assert entered.wait(1)
        assert not done.wait(0.05)
        release()
        release()
        assert done.wait(1)
    worker.join(1)
    assert not worker.is_alive()
    assert not priority.wait(lambda: False)


def test_priority_cancellation_deadline_nested_holds_and_legacy_mode() -> None:
    priority = FirstAudioPriority(True)
    with priority.hold(), priority.hold():
        deadline = time.monotonic() + 0.05
        assert priority.wait(lambda: time.monotonic() >= deadline)
    assert not priority.wait(lambda: False)
    legacy = FirstAudioPriority(False)
    with legacy.hold():
        assert not legacy.wait(lambda: False)


@pytest.mark.parametrize("outcome", ["success", "error", "empty", "cancelled"])
def test_speech_releases_priority_before_delivery_and_on_all_exit_paths(
    monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    from types import SimpleNamespace

    from experiments.openi_latency import hybrid_worker

    cancelled = threading.Event()
    api = ModelAPI(SimpleNamespace(tts=None), {"service_tts_first_audio_priority": True})

    class FakePCM:
        def __init__(self, *args: Any) -> None:
            pass

        def generate(self, text: str) -> Iterator[bytes]:
            assert api.first_audio.wait(lambda: True)
            if outcome == "error":
                raise ValueError("fixture_error")
            if outcome == "empty":
                return
            if outcome == "cancelled":
                cancelled.set()
            yield b""
            yield b"\x01\x02"

    monkeypatch.setattr(hybrid_worker, "QwenPCM", FakePCM)
    stream = api.speech("测试首音。", cancelled)
    if outcome == "error":
        with pytest.raises(ValueError, match="fixture_error"):
            next(stream)
    elif outcome == "success":
        assert next(stream) == b"\x01\x02"
        # 下游尚未消费完流，LLM 已可继续；不能持有优先级等播放结束。
        assert not api.first_audio.wait(lambda: False)
    else:
        assert list(stream) == []
    stream.close()
    assert not api.first_audio.wait(lambda: False)


def config() -> dict[str, Any]:
    settings = json.loads(Path("experiments/openi_latency/settings.service.json").read_text())
    settings["token_sha256"] = hashlib.sha256(b"test-key").hexdigest()
    return settings


def wav() -> bytes:
    data = io.BytesIO()
    with wave.open(data, "wb") as target:
        target.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        target.writeframes(struct.pack("<h", 1000) * 320)
    return data.getvalue()


def test_tool_json_never_leaks_into_content_even_with_character_splits() -> None:
    decoder = ToolDecoder({"hang_up"})
    source = (
        '祝您生活愉快。<tool_call>{"name":"hang_up","arguments":{"confirmed":true}}</tool_call>'
    )
    deltas = [delta for character in source for delta in decoder.feed(character)]
    deltas.extend(decoder.feed("", final=True))
    assert "".join(d.get("content", "") for d in deltas) == "祝您生活愉快。"
    calls = [call for d in deltas for call in d.get("tool_calls", [])]
    assert len(calls) == 1
    assert json.loads(calls[0]["function"]["arguments"]) == {"confirmed": True}
    with pytest.raises(ValueError, match="invalid_tool_call"):
        list(ToolDecoder(set()).feed(source, final=True))
    with pytest.raises(ValueError, match="incomplete_tool_call"):
        list(ToolDecoder({"hang_up"}).feed("<tool_call>{", final=True))


async def test_bridge_completion_and_disconnect_cancel_real_producer() -> None:
    assert [item async for item in bridge(lambda stop: iter([1, 2]))] == [1, 2]
    stopped = threading.Event()

    def generate(cancelled: threading.Event) -> Iterator[int]:
        try:
            yield 1
            assert cancelled.wait(2)
            yield 2
        finally:
            stopped.set()

    stream = bridge(generate)
    assert await anext(stream) == 1
    await stream.aclose()
    assert await asyncio.to_thread(stopped.wait, 3)


def test_wav_limits_reject_wrong_rate_truncated_and_over_budget() -> None:
    assert len(wav_pcm(wav(), 1)) == 640
    with pytest.raises(ValueError):
        wav_pcm(wav()[:-2], 1)
    with pytest.raises(ValueError):
        wav_pcm(wav(), 0.001)
    with pytest.raises(ValueError):
        wav_pcm(b"not audio", 1)


async def test_authenticated_apis_preserve_prompt_tools_and_stream_real_pcm() -> None:
    cfg = config()
    received: list[dict[str, Any]] = []

    class Backend:
        def transcribe(self, pcm: bytes, stop: threading.Event) -> Iterator[str]:
            assert len(pcm) == 640
            yield "多少钱？"

        def speech(self, text: str, stop: threading.Event) -> Iterator[bytes]:
            assert text == "每月99元。"
            yield b"\x01\x02" * 10
            yield b"\x03\x04" * 10

        def chat(self, body: dict[str, Any], stop: threading.Event) -> Iterator[dict[str, Any]]:
            received.append(body)
            yield {"content": "每月99元。"}
            yield {"_finish": "stop"}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(cfg, Backend())), base_url="http://test"
    ) as client:
        assert (await client.get("/health")).status_code == 200
        assert (await client.get("/v1/models")).status_code == 401
        client.headers["Authorization"] = "Bearer test-key"
        assert len((await client.get("/v1/models")).json()["data"]) == 3
        transcription = await client.post(
            "/v1/audio/transcriptions",
            data={"model": cfg["asr_model"], "language": "zh"},
            files={"file": ("audio.wav", wav(), "audio/wav")},
        )
        assert transcription.json() == {"text": "多少钱？"}
        message = {"role": "system", "content": "使用原有产品资料与打断背景。"}
        tools = [{"type": "function", "function": {"name": "hang_up"}}]
        response = await client.post(
            "/v1/chat/completions",
            json={"model": cfg["llm_model"], "messages": [message], "tools": tools, "stream": True},
        )
        events = [
            json.loads(line[6:])
            for line in response.text.splitlines()
            if line.startswith("data: {")
        ]
        assert events[1]["choices"][0]["delta"]["content"] == "每月99元。"
        assert events[-1]["choices"][0]["finish_reason"] == "stop"
        assert received[0]["messages"] == [message] and received[0]["tools"] == tools
        assert "[DONE]" in response.text
        speech = await client.post(
            "/v1/audio/speech",
            json={
                "model": cfg["service_tts_model"],
                "input": "每月99元。",
                "voice": cfg["service_voice"],
                "response_format": "pcm",
            },
        )
        assert speech.content == b"\x01\x02" * 10 + b"\x03\x04" * 10
        assert speech.headers["content-type"] == "audio/pcm"
        bad = await client.post(
            "/v1/audio/speech",
            json={"model": cfg["service_tts_model"], "input": "test", "voice": "unsupported"},
        )
        assert bad.status_code == 422
        assert (
            await client.post(
                "/v1/chat/completions", json={"model": "wrong", "messages": [message]}
            )
        ).status_code == 404
        assert (
            await client.post(
                "/v1/chat/completions", content=b"x" * (cfg["service_max_request_bytes"] + 1)
            )
        ).status_code == 413
