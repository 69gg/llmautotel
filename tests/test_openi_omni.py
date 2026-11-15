"""Omni 原型的输入边界和资产完整性；不替代 GPU 计时。"""

from __future__ import annotations

import hashlib
import re
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from experiments.openi_latency.omni_worker import AudioPrefill
from experiments.openi_latency.prepare_omni import download_asset, link_model_shards

SOURCES = [{"url_template": "https://unused.invalid/{path}", "revision": "locked"}]


@pytest.mark.parametrize("previous", ["absent", "broken", "old_mount", "current_mount"])
def test_omni_cached_image_refreshes_private_weight_links(tmp_path: Path, previous: str) -> None:
    mounted = tmp_path / "new_mount"
    model = tmp_path / "private_model"
    mounted.mkdir()
    model.mkdir()
    shard = mounted / "model-00001.safetensors"
    shard.write_bytes(b"new-mount-weights")
    link = model / shard.name
    old = tmp_path / "old-mount-weights"
    if previous == "old_mount":
        old.write_bytes(b"old-mount-weights")
    if previous != "absent":
        link.symlink_to(shard if previous == "current_mount" else old)
    link_model_shards(mounted, model)
    assert link.is_symlink()
    assert link.resolve() == shard
    assert link.read_bytes() == b"new-mount-weights"
    if old.exists():
        assert old.read_bytes() == b"old-mount-weights"


def test_omni_cached_image_does_not_replace_regular_weight_files(tmp_path: Path) -> None:
    mounted, model = tmp_path / "mount", tmp_path / "private"
    mounted.mkdir()
    model.mkdir()
    (mounted / "model.safetensors").write_bytes(b"mounted")
    target = model / "model.safetensors"
    target.write_bytes(b"preserve")
    with pytest.raises(ValueError, match="private_model_shard_not_symlink"):
        link_model_shards(mounted, model)
    assert target.read_bytes() == b"preserve"


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
    download_asset(tmp_path, item, SOURCES, time.monotonic() + 1)
    path.write_bytes(b"x" * len(data))
    with pytest.raises(ValueError, match="asset_checksum_mismatch"):
        download_asset(tmp_path, item, SOURCES, time.monotonic() + 1)
    path.write_bytes(b"x")
    with pytest.raises(ValueError, match="asset_size_mismatch"):
        download_asset(tmp_path, item, SOURCES, time.monotonic() + 1)
    item["path"] = "../outside.bin"
    with pytest.raises(ValueError, match="asset_outside_model"):
        download_asset(tmp_path, item, SOURCES, time.monotonic() + 1)


def test_omni_asset_deadline_prevents_download_and_keeps_partial(tmp_path: Path) -> None:
    item = {"path": "asset.bin", "bytes": 5, "sha256": "unused", "revision": "locked"}
    partial = tmp_path / "asset.bin.partial"
    partial.write_bytes(b"old")
    with pytest.raises(TimeoutError, match="asset_prepare_deadline"):
        download_asset(tmp_path, item, SOURCES, time.monotonic() - 1)
    assert partial.read_bytes() == b"old"


@pytest.fixture
def asset_server() -> Iterator[tuple[str, bytes, list[str], list[tuple[str, str]]]]:
    data = bytes(range(100))
    modes: list[str] = []
    requests: list[tuple[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_: Any) -> None:
            pass

        def do_GET(self) -> None:
            requests.append((self.path, self.headers["Range"]))
            mode = modes.pop(0) if modes else "range"
            if mode == "unavailable":
                self.send_error(503)
                return
            match = re.fullmatch(r"bytes=(\d+)-(\d+)", self.headers["Range"])
            assert match
            start, end = map(int, match.groups())
            if mode == "ignore_range":
                self.send_response(200)
                body = data
            else:
                self.send_response(206)
                body = data[start : end + 1]
                self.send_header(
                    "Content-Range",
                    f"bytes {start + (1 if mode == 'bad_range' else 0)}-{end}/{len(data)}",
                )
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if mode == "truncate":
                body = body[:7]
            self.wfile.write(body)
            self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", data, modes, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("mode", ["truncate", "bad_range", "ignore_range", "unavailable"])
def test_omni_asset_resumes_and_falls_back_without_appending_wrong_ranges(
    tmp_path: Path,
    asset_server: tuple[str, bytes, list[str], list[tuple[str, str]]],
    mode: str,
) -> None:
    url, data, modes, requests = asset_server
    item = {
        "path": "nested/asset.bin",
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    partial = tmp_path / "nested/asset.bin.partial"
    partial.parent.mkdir()
    partial.write_bytes(data[:11])
    modes.append(mode)
    download_asset(
        tmp_path,
        item,
        [
            {"url_template": url + "/primary/{path}", "revision": "locked"},
            {"url_template": url + "/fallback/{path}", "revision": "locked"},
        ],
        time.monotonic() + 5,
        chunk_bytes=32,
        request_timeout=1,
        retry_delay=0,
    )
    assert (tmp_path / item["path"]).read_bytes() == data
    assert not partial.exists()
    assert requests[0] == ("/primary/nested/asset.bin", "bytes=11-42")
    if mode != "ignore_range":
        offset = 18 if mode == "truncate" else 11
        assert requests[1] == ("/fallback/nested/asset.bin", f"bytes={offset}-{offset + 31}")


def test_omni_asset_rechecks_completed_partial_before_reusing_it(
    tmp_path: Path, asset_server: tuple[str, bytes, list[str], list[tuple[str, str]]]
) -> None:
    url, data, _, requests = asset_server
    partial = tmp_path / "asset.bin.partial"
    partial.write_bytes(b"x" * len(data))
    item = {"path": "asset.bin", "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    download_asset(
        tmp_path,
        item,
        [{"url_template": url + "/{path}", "revision": "locked"}],
        time.monotonic() + 5,
        chunk_bytes=32,
        retry_delay=0,
    )
    assert requests[0][1] == "bytes=0-31"
    assert (tmp_path / "asset.bin").read_bytes() == data
