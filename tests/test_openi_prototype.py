"""只验证原型计时和连接隔离，不以模拟结果替代 GPU 实测。"""

from __future__ import annotations

import hashlib
import io
import struct
import tarfile
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from experiments.openi_latency.client import wait_ready
from experiments.openi_latency.gateway import create_app
from experiments.openi_latency.model_paths import resolve_models, validate_model
from experiments.openi_latency.prepare_worker import image_requirements, prepare_source
from experiments.openi_latency.protocol import (
    PlaybackClock,
    signal_offset_ms,
    summarize,
    trim_silence,
)
from experiments.openi_latency.worker import ASRStream


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
        "torch==2.6.0\ntorchaudio==2.6.0\nnvidia-cudnn-cu12==9.1.0\ntriton==3.2.0\nfunasr==1.4.19\n"
    )
    assert image_requirements(lock) == "funasr==1.4.19\n"


def test_prepacked_source_is_verified_and_needs_no_git(tmp_path: Path) -> None:
    archive = tmp_path / "source.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        directory = tarfile.TarInfo("CosyVoice/third_party/Matcha-TTS/matcha")
        directory.type = tarfile.DIRTYPE
        bundle.addfile(directory)
        member = tarfile.TarInfo("CosyVoice/source-revisions.json")
        member.size = 2
        bundle.addfile(member, io.BytesIO(b"{}"))
    config = {
        "source_archive": archive.name,
        "source_archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
    }
    prepare_source(tmp_path, config)
    assert (tmp_path / "CosyVoice/source-revisions.json").read_text() == "{}"
    config["source_archive_sha256"] = "wrong"
    with pytest.raises(ValueError, match="checksum_mismatch"):
        prepare_source(tmp_path, config)


def test_offline_models_resolve_mounts_and_cached_asr_without_download(tmp_path: Path) -> None:
    mounts = tmp_path / "mounts"
    cache = tmp_path / "cache"
    for path in [mounts / "llm", mounts / "tts", cache / "iic" / "asr"]:
        path.mkdir(parents=True)
        (path / "weights.bin").write_bytes(b"weights")
    config: dict[str, Any] = {
        "mounted_models": {"llm": "llm", "tts": "tts"},
        "cached_models": {"asr": "**/asr"},
        "model_cache_roots": [str(cache)],
        "required_model_files": {role: ["weights.bin"] for role in ["asr", "llm", "tts"]},
        "allow_model_downloads": False,
    }
    paths = resolve_models(config, mounts)
    assert Path(paths["asr"]) == cache / "iic" / "asr"
    assert Path(paths["llm"]) == mounts / "llm"
    (mounts / "tts" / "weights.bin").unlink()
    with pytest.raises(FileNotFoundError, match="model_files_missing"):
        resolve_models(config, mounts)


def test_incomplete_shards_and_missing_cache_fail_before_model_loading(tmp_path: Path) -> None:
    (tmp_path / "model.safetensors.index.json").write_text(
        '{"weight_map":{"weight":"missing.safetensors"}}'
    )
    with pytest.raises(FileNotFoundError, match="model_shard_missing"):
        validate_model(tmp_path, [])
    with pytest.raises(ValueError, match="cached_model_expected_one"):
        resolve_models({"cached_models": {"asr": "asr"}, "model_cache_roots": []}, None)
    with pytest.raises(ValueError, match="require_all_three"):
        resolve_models({"allow_model_downloads": False}, None)


@pytest.mark.parametrize("streaming", [True, False])
def test_asr_modes_preserve_audio_and_reset_between_turns(streaming: bool) -> None:
    calls: list[dict[str, Any]] = []

    class Model:
        def generate(self, **kwargs: Any) -> list[dict[str, str]]:
            calls.append(kwargs)
            return [{"text": "<|zh|><|NEUTRAL|><|Speech|>订阅多少钱？"}]

    asr = ASRStream(
        Model(), [0, 8, 4], np, streaming=streaming, generate_options={"language": "zh"}
    )
    first = struct.pack("<h", 1000) * 7680
    tail = struct.pack("<h", 2000) * 320
    asr.feed(first)
    assert len(calls) == (1 if streaming else 0)
    text = asr.feed(tail, final=True)
    assert text == "订阅多少钱？" * (2 if streaming else 1)
    np.testing.assert_array_equal(
        np.concatenate([call["input"] for call in calls]),
        np.frombuffer(first + tail, dtype="<i2").astype("float32") / 32768,
    )
    assert all(call["language"] == "zh" for call in calls)
    if streaming:
        assert [call["is_final"] for call in calls] == [False, True]
        assert calls[0]["cache"] is calls[1]["cache"]
    else:
        assert "is_final" not in calls[0] and "chunk_size" not in calls[0]
    previous_cache = asr.cache
    asr.reset()
    calls.clear()
    assert not asr.pending and not asr.text and asr.cache is not previous_cache
    assert asr.feed(tail, final=True) == "订阅多少钱？"
    assert len(calls) == 1 and len(calls[0]["input"]) == 320
