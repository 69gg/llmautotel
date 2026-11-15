"""Omni 原型的输入边界和资产完整性；不替代 GPU 计时。"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any

import pytest

from experiments.openi_latency.omni_worker import AudioPrefill
from experiments.openi_latency.prepare_omni import download_asset


class PrefillRecorder:
    def __init__(self) -> None:
        self.sessions = 0
        self.received: list[tuple[bytes, bool]] = []

    def start_session(self) -> None:
        self.sessions += 1

    def prefill_audio(self, pcm: bytes, *, final: bool) -> None:
        self.received.append((pcm, final))


@pytest.mark.parametrize("frames", [0, 4000, 16000, 20000])
def test_omni_prefills_whole_seconds_and_finalizes_with_padding(frames: int) -> None:
    owner = PrefillRecorder()
    stream = AudioPrefill(owner)
    stream.reset()
    pcm = b"\x01\x00" * frames
    for offset in range(0, len(pcm), 640):
        stream.feed(pcm[offset : offset + 640])
    stream.feed(b"", final=True)
    assert owner.sessions == 1
    assert all(len(chunk) == 32000 for chunk, _ in owner.received)
    assert [last for _, last in owner.received] == [False] * (frames // 16000) + [True]
    combined = b"".join(chunk for chunk, _ in owner.received)
    assert combined.startswith(pcm)
    assert combined[len(pcm) :] == bytes(len(combined) - len(pcm))
    assert stream.padding_ms == pytest.approx((len(combined) - len(pcm)) / 32)
    stream.reset()
    assert stream.pending == b""
    assert stream.chunks == 0
    assert owner.sessions == 2
    with pytest.raises(ValueError, match="unaligned_input_pcm"):
        stream.feed(b"x")


def test_omni_asset_reuses_only_size_and_hash_verified_file(tmp_path: Path) -> None:
    data = b"verified-test-asset"
    path = tmp_path / "asset.bin"
    path.write_bytes(data)
    item: dict[str, Any] = {
        "path": path.name,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    download_asset(tmp_path, item, "https://unused.invalid", time.monotonic() + 1)
    path.write_bytes(b"x" * len(data))
    with pytest.raises(ValueError, match="asset_checksum_mismatch"):
        download_asset(tmp_path, item, "https://unused.invalid", time.monotonic() + 1)
    path.write_bytes(b"x")
    with pytest.raises(ValueError, match="asset_size_mismatch"):
        download_asset(tmp_path, item, "https://unused.invalid", time.monotonic() + 1)
    item["path"] = "../outside.bin"
    with pytest.raises(ValueError, match="asset_outside_model"):
        download_asset(tmp_path, item, "https://unused.invalid", time.monotonic() + 1)


def test_omni_asset_deadline_prevents_download_and_cleans_partial(tmp_path: Path) -> None:
    item = {"path": "asset.bin", "bytes": 5, "sha256": "unused", "revision": "locked"}
    partial = tmp_path / "asset.bin.partial"
    partial.write_bytes(b"old")
    with pytest.raises(TimeoutError, match="asset_prepare_deadline"):
        download_asset(tmp_path, item, "https://unused.invalid", time.monotonic() - 1)
    assert not partial.exists()
