"""只验证计时与缓存边界，不在本机运行模型。"""

from __future__ import annotations

import functools
import struct
import time
from collections.abc import Iterator

import pytest

from experiments.openi_latency.optimization import (
    LeadingSilenceFilter,
    ReplyAudioCache,
    bind_cosy_poll_interval,
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


class CosyPoll:
    def tts(self) -> Iterator[float]:
        time.sleep(0.1)
        yield 25


def test_cosy_poll_only_changes_current_instance_and_preserves_chunk_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delays: list[float] = []
    monkeypatch.setattr(time, "sleep", delays.append)
    model = CosyPoll()
    bind_cosy_poll_interval(model, 0.005)
    assert list(model.tts()) == [25]
    assert list(CosyPoll().tts()) == [25]
    assert delays == [0.005, 0.1]
    with pytest.raises(ValueError, match="invalid_cosy_poll_interval"):
        bind_cosy_poll_interval(model, 0)


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


def test_compile_failure_restores_both_models_and_preserves_each_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from experiments.openi_latency.omni_optimizations import OptimizedMiniCPMModels

    model = object.__new__(OptimizedMiniCPMModels)

    def old_llm() -> None:
        pass

    def old_estimator() -> None:
        pass

    estimator = SimpleNamespace(forward_chunk=old_estimator)
    model.model = SimpleNamespace(
        llm=SimpleNamespace(forward=old_llm),
        tts=SimpleNamespace(
            audio_tokenizer=SimpleNamespace(
                flow=SimpleNamespace(decoder=SimpleNamespace(estimator=estimator)),
            )
        ),
    )
    model.original_llm_forward = old_llm
    model.original_estimator_forward = old_estimator
    model.compilation = {"enabled": False}
    model.manifest = {}
    model.config = {
        "generation_timeout_seconds": 30,
        "optimization_compile": {
            "timeout_seconds": 1800,
            "llm": {},
            "estimator": {},
            "warmups": 1,
        },
    }
    attempts = 0

    def fake_compile(function: object, **kwargs: object) -> object:
        nonlocal attempts
        attempts += 1
        if attempts % 2 == 0:
            raise ValueError(f"attempt-{attempts}")
        return lambda: None

    model.torch = SimpleNamespace(compile=fake_compile)
    monkeypatch.setattr("experiments.openi_latency.omni_optimizations.emit", lambda event: None)
    for _ in range(2):
        with pytest.raises(ValueError, match="attempt"):
            model.compile_models()
        assert model.model.llm.forward is old_llm
        assert estimator.forward_chunk is old_estimator
        assert model.config["generation_timeout_seconds"] == 30
    recorded = model.manifest["compilation_attempts"]
    assert [row["detail"] for row in recorded] == ["attempt-2", "attempt-4"]
    assert all(row["enabled"] is False for row in recorded)
