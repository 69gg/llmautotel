"""Qwen3-TTS 0.6B 的真实流式 PCM 与 CUDA Graph 对照，GPU 仅在远端。"""

from __future__ import annotations

import importlib.metadata
import json
import sys
import time
from pathlib import Path
from typing import Any

if __package__:
    from .optimization import LeadingSilenceFilter
    from .protocol import PlaybackClock, summarize
    from .worker import emit
else:
    from optimization import LeadingSilenceFilter
    from protocol import PlaybackClock, summarize
    from worker import emit


def benchmark(model: Any, config: dict[str, Any]) -> dict[str, Any]:
    import numpy as np
    import torch

    reports: list[dict[str, Any]] = []
    for variant in config["qwen3_variants"]:
        samples: list[dict[str, Any]] = []
        for text in config["tts_texts"]:
            started = time.perf_counter()
            clock = PlaybackClock()
            first_pcm: float | None = None
            audio_bytes = chunks = 0
            cutter = LeadingSilenceFilter(24000)
            try:
                generator = model.generate_voice_clone_streaming(
                    text=text,
                    language="Chinese",
                    ref_audio=config["qwen3_reference_audio"],
                    xvec_only=True,
                    append_silence=False,
                    do_sample=False,
                    chunk_size=variant["chunk_size"],
                    parity_mode=variant["parity_mode"],
                    non_streaming_mode=False,
                    max_new_tokens=config["qwen3_max_audio_tokens"],
                )
                for audio, sample_rate, _timing in generator:
                    if time.perf_counter() - started > config["generation_timeout_seconds"]:
                        generator.close()
                        raise TimeoutError("qwen3_generation_deadline")
                    values = np.asarray(audio).reshape(-1)
                    pcm = (values.clip(-1, 1) * 32767).astype("<i2").tobytes()
                    if variant["trim"]:
                        if sample_rate != cutter.sample_rate:
                            raise ValueError("unexpected_sample_rate")
                        pcm = cutter.feed(pcm)
                    if pcm:
                        now = time.perf_counter()
                        first_pcm = first_pcm or now
                        clock.feed(pcm, sample_rate, now)
                        audio_bytes += len(pcm)
                        chunks += 1
                tail = cutter.finish() if variant["trim"] else b""
                if tail:
                    clock.feed(tail, sample_rate, time.perf_counter())
                    audio_bytes += len(tail)
                result = {
                    "ok": clock.first_signal_at is not None,
                    "text": text,
                    "effective_audio_ms": round((clock.first_signal_at - started) * 1000, 1)
                    if clock.first_signal_at is not None
                    else None,
                    "first_pcm_ms": round((first_pcm - started) * 1000, 1) if first_pcm else None,
                    "total_ms": round((time.perf_counter() - started) * 1000, 1),
                    "audio_bytes": audio_bytes,
                    "chunks": chunks,
                    "trimmed_ms": cutter.removed_ms,
                    "gpu_peak_bytes": torch.cuda.max_memory_allocated(),
                }
            except Exception as exc:
                result = {
                    "ok": False,
                    "text": text,
                    "error": type(exc).__name__,
                    "detail": str(exc)[:500],
                }
            samples.append(result)
            emit({"type": "sample", "variant": variant["name"], "result": result})
        reports.append({"variant": variant, "samples": samples, "summary": summarize(samples)})
    return {"type": "report", "scope": "fixed_text_tts_not_end_to_end", "variants": reports}


def main() -> None:
    import torch
    from faster_qwen3_tts import FasterQwen3TTS

    root = Path(__file__).resolve().parent
    config = json.loads((root / "settings.run.json").read_text())
    torch.set_num_threads(4)
    emit({"type": "status", "phase": "loading_qwen3"})
    started = time.perf_counter()
    model = FasterQwen3TTS.from_pretrained(
        config["qwen3_model_directory"],
        device="cuda",
        dtype=torch.bfloat16,
        attn_implementation="sdpa",
        max_seq_len=config["qwen3_max_seq_len"],
    )
    emit({"type": "status", "phase": "warming_qwen3_cuda_graphs"})
    model.warmup()
    # 完整生成一次以预热参考音色与解码器；不把它作为延迟样本。
    for _ in model.generate_voice_clone_streaming(
        text=config["qwen3_warmup_text"],
        language="Chinese",
        ref_audio=config["qwen3_reference_audio"],
        xvec_only=True,
        append_silence=False,
        do_sample=False,
        chunk_size=8,
        non_streaming_mode=False,
        max_new_tokens=config["qwen3_max_audio_tokens"],
    ):
        pass
    manifest = {
        "gpu": torch.cuda.get_device_name(0),
        "load_and_warmup_ms": round((time.perf_counter() - started) * 1000, 1),
        "model_revision": config["qwen3_revision"],
        "versions": {
            p: importlib.metadata.version(p)
            for p in ["torch", "transformers", "qwen-tts-hf", "faster-qwen3-tts"]
        },
        "engine": "third_party_faster_qwen3_tts",
        "streaming_input": "complete_text_internal_streaming_not_text_iterator",
    }
    emit({"type": "ready", "phase": "ready", "manifest": manifest})
    for line in sys.stdin:
        command = json.loads(line)
        if command["type"] in {"optimization_benchmark", "tts_benchmark"}:
            emit({**benchmark(model, config), "manifest": manifest})


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        import traceback

        traceback.print_exc()
        emit(
            {
                "type": "error",
                "phase": "model_initialization_failed",
                "error": type(error).__name__,
                "detail": str(error)[:500],
            }
        )
        raise
