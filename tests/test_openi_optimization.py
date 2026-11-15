"""只验证计时与缓存边界，不在本机运行模型。"""

from __future__ import annotations

import functools
import struct
from collections.abc import Iterator

import pytest

from experiments.openi_latency.optimization import (
    LeadingSilenceFilter,
    ReplyAudioCache,
    bind_streaming_chunk_options,
)
from experiments.openi_latency.protocol import PlaybackClock


def test_trim_changes_real_pcm_and_keeps_leading_margin_across_packets() -> None:
    silence = bytes(24000 * 2 // 10)
    speech = struct.pack("<h", 1000) * 2400
    cutter = LeadingSilenceFilter(24000, keep_ms=40)
    assert cutter.feed(silence) == b""
    actual = cutter.feed(silence + silence + speech)
    assert actual == bytes(1920) + speech
    assert cutter.removed_ms == 260
    clock = PlaybackClock()
    clock.feed(actual, 24000, 10)
    assert clock.first_signal_at == pytest.approx(10.04)
    assert cutter.feed(speech) == speech


def test_silence_cap_and_unfinished_short_input_preserve_pcm() -> None:
    cutter = LeadingSilenceFilter(1000, keep_ms=40, max_trim_ms=400)
    assert cutter.feed(bytes(1000)) == bytes(200)
    assert cutter.removed_ms == 400
    short = LeadingSilenceFilter(1000)
    assert short.feed(bytes(100)) == b""
    assert short.finish() == bytes(100)
    with pytest.raises(ValueError, match="unaligned_output_pcm"):
        short.feed(b"x")


def test_cache_requires_exact_reply_and_same_product_and_voice() -> None:
    snapshot = {"price": 99, "voice": "test", "prompt": "v1"}
    cache = ReplyAudioCache(snapshot)
    cache.put("订阅99元。", b"\x01\x00")
    assert cache.get(" 订阅99元。 ", dict(snapshot)) == b"\x01\x00"
    assert cache.get("订阅19元。", snapshot) is None
    assert cache.get("订阅99元。", {**snapshot, "price": 199}) is None
    assert cache.get("订阅99元。", {**snapshot, "voice": "new"}) is None


class Upstream:
    def streaming_generate(self) -> Iterator[tuple[int, int]]:
        generate_chunk_size = 10
        CHUNK_SIZE = 25
        yield generate_chunk_size, CHUNK_SIZE


def test_patch_changes_actual_inner_sizes_without_changing_other_instances() -> None:
    model = Upstream()
    manifest = bind_streaming_chunk_options(model)
    model._latency_text_tokens = 4
    model._latency_audio_tokens = 12
    assert list(model.streaming_generate()) == [(4, 12)]
    assert list(Upstream().streaming_generate()) == [(10, 25)]
    assert manifest["modified_assignments"] == {"CHUNK_SIZE": 1, "generate_chunk_size": 1}


class ChangedUpstream:
    def streaming_generate(self) -> Iterator[int]:
        CHUNK_SIZE = 24
        yield CHUNK_SIZE


def test_upstream_change_fails_instead_of_silently_mismeasuring() -> None:
    with pytest.raises(ValueError, match="upstream_chunk_assignment_changed"):
        bind_streaming_chunk_options(ChangedUpstream())


def test_patch_uses_source_globals_through_foreign_decorator_namespace() -> None:
    namespace: dict[str, object] = {"wraps": functools.wraps}
    exec(
        "def decorate(f):\n @wraps(f)\n def wrapped(*a, **kw):\n"
        "  return f(*a, **kw)\n return wrapped",
        namespace,
    )
    decorate = namespace["decorate"]

    class Wrapped(Upstream):
        streaming_generate = decorate(Upstream.streaming_generate)

    model = Wrapped()
    bind_streaming_chunk_options(model)
    model._latency_audio_tokens = 12
    assert list(model.streaming_generate()) == [(10, 12)]
