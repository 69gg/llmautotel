"""实验用 CosyVoice 调度：逐轮分块、兼容的文本入口与首音路径计时。"""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any


def text_segments(pieces: Iterator[str], min_chars: int) -> Iterator[str]:
    """按中文语义边界聚合，保持金额小数和型号完整。"""
    pending = ""
    for piece in pieces:
        for character in piece:
            pending += character
            if character in "。！？" or (character in "，；" and len(pending.strip()) >= min_chars):
                if pending.strip():
                    yield pending
                pending = ""
    if pending.strip():
        yield pending


class CosySynthesis:
    """单工作线程实验；计时钩子只在本次合成期间存在，不改上游源码。"""

    def __init__(self, tts: Any, config: dict[str, Any]) -> None:
        self.tts = tts
        self.config = config
        self.initial_hop = tts.model.token_hop_len
        self.trace: dict[str, Any] = {}

    def generate(
        self, text: str | Iterator[str], *, reset_hop: bool | None = None
    ) -> Iterator[dict[str, Any]]:
        reset = self.config.get("tts_reset_hop", False) if reset_hop is None else reset_hop
        uses_vllm = hasattr(self.tts.model.llm, "vllm")
        mode = self.config.get("tts_text_mode", "auto")
        if mode not in {"auto", "bistream", "clauses"}:
            raise ValueError("invalid_tts_text_mode")
        # vLLM 当前接收完整文本。提前提交有意义的小句，音频仍流式返回。
        # 不删除上游断言：绕过它不会让 inference_bistream 获得 vLLM 的解码器。
        clauses = uses_vllm or mode == "clauses"
        inputs = (
            iter([text])
            if isinstance(text, str)
            else text_segments(text, self.config.get("tts_clause_min_chars", 8))
            if clauses
            else iter([text])
        )
        started = time.perf_counter()
        self.trace = {
            "reset_hop": reset,
            "text_mode": "complete"
            if isinstance(text, str)
            else "clauses"
            if clauses
            else "bistream",
        }
        first_token: float | None = None
        first_decode: float | None = None
        model = self.tts.model
        original_decode = model.token2wav
        token_method = "inference" if isinstance(text, str) or clauses else "inference_bistream"
        original_tokens = getattr(model.llm, token_method)

        def tokens(*args: Any, **kwargs: Any) -> Iterator[Any]:
            nonlocal first_token
            for value in original_tokens(*args, **kwargs):
                if first_token is None:
                    first_token = time.perf_counter()
                yield value

        def decode(*args: Any, **kwargs: Any) -> Any:
            nonlocal first_decode
            if first_decode is None:
                first_decode = time.perf_counter()
            return original_decode(*args, **kwargs)

        model.token2wav = decode
        setattr(model.llm, token_method, tokens)
        hops: list[int] = []
        try:
            for item in inputs:
                if reset:
                    model.token_hop_len = self.initial_hop
                hops.append(model.token_hop_len)
                yield from self.tts.inference_zero_shot(
                    item, "", "", zero_shot_spk_id="benchmark", stream=True, text_frontend=False
                )
        finally:
            model.token2wav = original_decode
            setattr(model.llm, token_method, original_tokens)
            self.trace.update(
                hop_lengths_at_start=hops,
                hop_length_at_end=model.token_hop_len,
                first_speech_token_ms=round((first_token - started) * 1000, 1)
                if first_token is not None
                else None,
                first_acoustic_decode_start_ms=round((first_decode - started) * 1000, 1)
                if first_decode is not None
                else None,
            )
