"""轻量验证 TTS 实测计时和异常，不导入 Torch、不加载权重。"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from experiments.openi_latency.qwen3_worker import benchmark


class FakeSpeech:
    def __init__(self, failure: bool = False) -> None:
        self.failure = failure

    def generate_voice_clone_streaming(self, **kwargs: Any) -> Any:
        assert kwargs["do_sample"] is False
        assert kwargs["xvec_only"] is True
        if self.failure:
            raise RuntimeError("test provider failure")
        yield np.zeros(7200), 24000, {}
        yield np.ones(2400) * 0.2, 24000, {}


def settings() -> dict[str, Any]:
    return {
        "qwen3_variants": [
            {"name": "control", "chunk_size": 8, "parity_mode": True, "trim": False},
            {"name": "trim", "chunk_size": 4, "parity_mode": False, "trim": True},
        ],
        "tts_texts": ["测试产品价格99元。"],
        "qwen3_reference_audio": "/not-loaded-test.wav",
        "qwen3_max_audio_tokens": 64,
        "generation_timeout_seconds": 10,
    }


def test_first_effective_audio_counts_queued_silence_and_actual_trim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(
        sys.modules,
        "torch",
        SimpleNamespace(cuda=SimpleNamespace(max_memory_allocated=lambda: 123)),
    )
    monkeypatch.setattr("experiments.openi_latency.qwen3_worker.emit", lambda event: None)
    report = benchmark(FakeSpeech(), settings())
    control, trimmed = [row["samples"][0] for row in report["variants"]]
    assert control["ok"] and trimmed["ok"]
    assert control["effective_audio_ms"] >= 300
    assert trimmed["effective_audio_ms"] < control["effective_audio_ms"] - 200
    assert trimmed["trimmed_ms"] == 260
    assert trimmed["audio_bytes"] < control["audio_bytes"]
    assert report["scope"] == "fixed_text_tts_not_end_to_end"


def test_stream_failure_is_reported_and_next_variant_still_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace())
    monkeypatch.setattr("experiments.openi_latency.qwen3_worker.emit", lambda event: None)
    report = benchmark(FakeSpeech(failure=True), settings())
    assert len(report["variants"]) == 2
    for row in report["variants"]:
        assert row["summary"]["successful"] == 0
        assert row["samples"][0]["error"] == "RuntimeError"
        assert "effective_audio_ms" not in row["samples"][0]
