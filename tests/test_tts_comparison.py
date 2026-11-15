"""实验回归：分块复位、加速器入口和错误去敏，不替代 GPU 延迟结果。"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from experiments.openi_latency.deepseek_benchmark import billing_upper_bound, run
from experiments.openi_latency.synthesis import CosySynthesis, text_segments


class FakeTTS:
    def __init__(self, *, vllm: bool = False) -> None:
        def tokens(*args: Any, **kwargs: Any) -> Iterator[int]:
            yield 1

        llm = SimpleNamespace(inference=tokens, inference_bistream=tokens)
        if vllm:
            llm.vllm = object()
        self.model = SimpleNamespace(
            token_hop_len=25, token_max_hop_len=100, llm=llm, token2wav=lambda: None
        )
        self.inputs: list[str | Iterator[str]] = []
        self.hops: list[int] = []

    def inference_zero_shot(self, text: Any, *args: Any, **kwargs: Any) -> Iterator[Any]:
        self.inputs.append(text)
        self.hops.append(self.model.token_hop_len)
        name = "inference" if isinstance(text, str) else "inference_bistream"
        list(getattr(self.model.llm, name)())
        self.model.token2wav()
        self.model.token_hop_len = 100
        yield {"tts_speech": b"audio"}


def test_hop_reset_uses_original_model_value_on_every_synthesis() -> None:
    tts = FakeTTS()
    runner = CosySynthesis(tts, {})
    list(runner.generate("预热"))
    list(runner.generate("旧路径"))
    list(runner.generate("新路径", reset_hop=True))
    list(runner.generate("下一轮", reset_hop=True))
    assert tts.hops == [25, 100, 25, 25]
    assert runner.trace["hop_lengths_at_start"] == [25]
    assert runner.trace["first_speech_token_ms"] is not None
    assert runner.trace["first_acoustic_decode_start_ms"] is not None


def test_vllm_receives_complete_clauses_while_native_keeps_text_stream() -> None:
    pieces = ["价格是19.", "01万，", "还可以置换。"]
    assert list(text_segments(iter(pieces), 8)) == ["价格是19.01万，", "还可以置换。"]
    accelerated = FakeTTS(vllm=True)
    runner = CosySynthesis(accelerated, {"tts_text_mode": "bistream", "tts_reset_hop": True})
    list(runner.generate(iter(pieces)))
    assert accelerated.inputs == ["价格是19.01万，", "还可以置换。"]
    assert accelerated.hops == [25, 25]
    assert runner.trace["text_mode"] == "clauses"
    native = FakeTTS()
    stream = iter(pieces)
    list(CosySynthesis(native, {}).generate(stream))
    assert native.inputs == [stream]


def test_trace_hooks_restore_when_generation_fails() -> None:
    tts = FakeTTS()
    original_decode = tts.model.token2wav
    original_tokens = tts.model.llm.inference

    def fail(*args: Any, **kwargs: Any) -> Iterator[Any]:
        raise RuntimeError("test")
        yield

    tts.inference_zero_shot = fail  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="test"):
        list(CosySynthesis(tts, {}).generate("测试"))
    assert tts.model.token2wav is original_decode
    assert tts.model.llm.inference is original_tokens


@pytest.mark.asyncio
async def test_account_error_stops_paid_requests_without_returning_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instances: list[Any] = []

    class Service:
        def __init__(self) -> None:
            self.closed = False
            instances.append(self)

        async def _audio_chunks(self, text: str) -> Any:
            request = httpx.Request("POST", "https://test.invalid", headers={"secret": "hidden"})
            response = httpx.Response(402, request=request, text="secret upstream text")
            raise httpx.HTTPStatusError("secret upstream text", request=request, response=response)
            yield b""

        async def aclose(self) -> None:
            self.closed = True

    monkeypatch.setattr(
        "experiments.openi_latency.deepseek_benchmark.create_tts_service",
        lambda settings: Service(),
    )
    report = await run(
        ["测试"], "hidden", base_url="https://test.invalid/v1", voice="stella", repeats=1, timeout=2
    )
    assert len(report["samples"]) == 1
    assert report["samples"][0]["http_status"] == 402
    assert "secret" not in str(report) and "hidden" not in str(report)
    assert all(service.closed for service in instances)
    assert billing_upper_bound(["测试abc"], 1) == 14


@pytest.mark.asyncio
async def test_http_chunks_align_samples_before_effective_audio_measurement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Service:
        async def _audio_chunks(self, text: str) -> Any:
            audio = b"\xe8\x03" * 480
            for chunk in [audio[:1], audio[1:319], audio[319:]]:
                yield chunk

        async def aclose(self) -> None:
            pass

    monkeypatch.setattr(
        "experiments.openi_latency.deepseek_benchmark.create_tts_service",
        lambda settings: Service(),
    )
    report = await run(
        ["测试"],
        "hidden",
        base_url="https://test.invalid/v1",
        voice="stella",
        repeats=1,
        timeout=2,
        protocol="http",
    )
    assert len(report["samples"]) == 1
    sample = report["samples"][0]
    assert sample["ok"] and sample["audio_ms"] == 20 and sample["chunks"] == 3
