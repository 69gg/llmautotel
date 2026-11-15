"""只用小数组检查分句、取消和真实 PCM，不导入模型或占用 GPU。"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from typing import Any

import numpy as np
import pytest

from experiments.openi_latency.hybrid_worker import QwenPCM


def settings() -> dict[str, Any]:
    return {
        "tts_clause_min_chars": 8,
        "tts_chunk_size": 4,
        "tts_trim_leading_silence": True,
        "qwen3_reference_audio": "/not-loaded.wav",
        "qwen3_max_audio_tokens": 256,
    }


def test_clauses_start_before_llm_finishes_and_keep_decimal_prices() -> None:
    consumed: list[str] = []
    requests: list[str] = []

    def text() -> Iterator[str]:
        for piece in ["当前售价19.", "01万元，", "另外支持置换优惠。"]:
            consumed.append(piece)
            yield piece

    class Model:
        def generate_voice_clone_streaming(self, **kwargs: Any) -> Any:
            requests.append(kwargs["text"])
            assert kwargs["chunk_size"] == 4
            assert kwargs["parity_mode"] is False
            if len(requests) == 1:
                assert len(consumed) == 2
            yield np.ones(480) * 0.2, 24000, {}

    pcm = list(QwenPCM(Model(), settings(), threading.Event()).generate(text()))
    assert requests == ["当前售价19.01万元，", "另外支持置换优惠。"]
    assert len(pcm) == 2 and all(len(chunk) == 960 for chunk in pcm)


def test_cancel_discards_late_audio_and_never_starts_next_clause() -> None:
    cancel = threading.Event()
    requests: list[str] = []
    closed: list[bool] = []

    class Model:
        def generate_voice_clone_streaming(self, **kwargs: Any) -> Any:
            requests.append(kwargs["text"])
            try:
                yield np.ones(480) * 0.2, 24000, {}
                cancel.set()
                yield np.ones(480) * 0.3, 24000, {}
            finally:
                closed.append(True)

    result = list(QwenPCM(Model(), settings(), cancel).generate(iter(["第一段有效回答。第二段。"])))
    assert len(result) == 1 and requests == ["第一段有效回答。"]
    assert closed == [True]


def test_tts_rejects_wrong_sample_rate_and_closes_stream() -> None:
    closed: list[bool] = []

    class Model:
        def generate_voice_clone_streaming(self, **kwargs: Any) -> Any:
            try:
                yield np.ones(480), 16000, {}
            finally:
                closed.append(True)

    with pytest.raises(ValueError, match="unexpected_sample_rate"):
        list(QwenPCM(Model(), settings(), threading.Event()).generate("测试。"))
    assert closed == [True]
