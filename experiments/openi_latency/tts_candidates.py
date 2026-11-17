"""同一 GPU、文本和参考音下比较 TTS；加载、预热不计入首音。"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import statistics
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

if __package__:
    from .optimization import LeadingSilenceFilter, bind_cosy_poll_interval
    from .protocol import PlaybackClock
else:
    from optimization import LeadingSilenceFilter, bind_cosy_poll_interval
    from protocol import PlaybackClock


def benchmark(
    synthesize: Callable[[str], Iterator[bytes]],
    texts: list[str],
    *,
    repeats: int,
    sample_rate: int = 24000,
) -> dict[str, Any]:
    """按真实 PCM 计时，并暴露首块之后的供音断档。"""
    if not texts or not 1 <= repeats <= 10:
        raise ValueError("invalid_benchmark_budget")
    rows: list[dict[str, Any]] = []
    for repeat in range(repeats):
        for case, text in enumerate(texts):
            clock = PlaybackClock()
            started = time.perf_counter()
            first_pcm: float | None = None
            total_bytes = chunks = 0
            iterator = synthesize(text)
            try:
                for pcm in iterator:
                    if not pcm:
                        continue
                    now = time.perf_counter()
                    first_pcm = first_pcm or now
                    clock.feed(pcm, sample_rate, now)
                    total_bytes += len(pcm)
                    chunks += 1
            finally:
                iterator.close()
            elapsed = time.perf_counter() - started
            duration = total_bytes / (sample_rate * 2)
            rows.append(
                {
                    "repeat": repeat,
                    "case": case,
                    "text": text,
                    "ok": clock.first_signal_at is not None,
                    "first_pcm_ms": round((first_pcm - started) * 1000, 1)
                    if first_pcm is not None
                    else None,
                    "effective_audio_ms": round((clock.first_signal_at - started) * 1000, 1)
                    if clock.first_signal_at is not None
                    else None,
                    "total_ms": round(elapsed * 1000, 1),
                    "audio_ms": round(duration * 1000, 1),
                    "rtf": round(elapsed / duration, 4) if duration else None,
                    "chunks": chunks,
                    "starvation_ms": round(clock.starvation_seconds * 1000, 1),
                    "starvation_events": clock.starvation_events,
                }
            )
    return {"scope": "fixed_text_effective_audio_in_memory_not_device", "samples": rows}


def choose_fastest(reports: dict[str, dict[str, Any]], *, baseline: str | None = None) -> str:
    """只选全部配对文本成功、持续供音通过的候选，不选单个幸运首包。"""
    if baseline is not None:
        control = reports.get(baseline, {})
        if control.get("error") or not control.get("samples") or any(
            not row.get("ok") for row in control.get("samples", [])
        ):
            raise ValueError("unverified_tts_baseline")
    expected: set[tuple[int, int, str]] | None = None
    scores: dict[str, float] = {}
    for name, report in reports.items():
        rows = report.get("samples", [])
        if not rows or report.get("error"):
            continue
        cases = {(row["repeat"], row["case"], row["text"]) for row in rows}
        if expected is None:
            expected = cases
        if cases != expected or len(cases) != len(rows):
            raise ValueError("unpaired_tts_benchmark")
        if any(
            not row.get("ok")
            or row.get("effective_audio_ms") is None
            or row.get("rtf") is None
            or row["rtf"] >= 1
            or row.get("starvation_ms", 0) > report.get("max_starvation_ms", 40)
            for row in rows
        ):
            continue
        scores[name] = statistics.median(float(row["effective_audio_ms"]) for row in rows)
    if not scores:
        raise ValueError("no_continuous_tts_candidate")
    return min(scores, key=lambda name: scores[name])


class FloatPCM:
    """有状态重采样，避免将 44.1kHz 改个标签当成 24kHz。"""

    def __init__(self, source_rate: int, target_rate: int = 24000) -> None:
        self.target_rate = target_rate
        self.resampler: Any = None
        if source_rate != target_rate:
            import soxr

            self.resampler = soxr.ResampleStream(source_rate, target_rate, 1, dtype="float32")

    def feed(self, samples: Any, *, final: bool = False) -> bytes:
        import numpy as np

        values = np.asarray(samples, dtype=np.float32).reshape(-1)
        if self.resampler is not None:
            values = self.resampler.resample_chunk(values, last=final)
        return (values.clip(-1, 1) * 32767).astype("<i2").tobytes()


class CandidatePCM:
    """复用厂商流式推理和参考缓存；每次生成有独立音频状态。"""

    def __init__(self, engine: str, config: dict[str, Any]) -> None:
        self.engine, self.config = engine, config
        self.cancel = threading.Event()
        import torch

        torch.set_num_threads(config.get("cpu_threads", 4))
        self.torch = torch
        self.native_rate = 24000
        if engine == "cosyvoice3":
            from cosyvoice.cli.cosyvoice import AutoModel

            self.model = AutoModel(
                model_dir=config["model_path"],
                fp16=config.get("fp16", True),
                load_vllm=config.get("load_vllm", False),
                load_trt=config.get("load_trt", False),
            )
            self.model.add_zero_shot_spk(
                config["reference_text"], config["reference_audio"], "reference"
            )
            if interval := config.get("poll_interval_seconds"):
                self.poll_manifest = bind_cosy_poll_interval(self.model.model, float(interval))
            self.initial_hop = self.model.model.token_hop_len
            self.native_rate = int(self.model.sample_rate)
        elif engine == "voxcpm15":
            from voxcpm import VoxCPM

            self.model = VoxCPM.from_pretrained(
                config["model_path"], load_denoiser=False, optimize=config.get("optimize", True)
            )
            self.native_rate = int(self.model.tts_model.sample_rate)
            self.prompt = self.model.tts_model.build_prompt_cache(
                prompt_wav_path=config["reference_audio"], prompt_text=config["reference_text"]
            )
        elif engine == "qwen3":
            from faster_qwen3_tts import FasterQwen3TTS

            self.model = FasterQwen3TTS.from_pretrained(
                config["model_path"],
                device="cuda",
                dtype=torch.bfloat16,
                attn_implementation="sdpa",
                max_seq_len=config.get("max_seq_len", 1024),
            )
            self.model.warmup()
        else:
            raise ValueError("unsupported_tts_candidate")

    def raw(self, text: str) -> Iterator[Any]:
        if self.engine == "cosyvoice3":
            self.model.model.token_hop_len = self.initial_hop
            with self.torch.inference_mode():
                for result in self.model.inference_zero_shot(
                    text, "", "", zero_shot_spk_id="reference", stream=True, text_frontend=False
                ):
                    yield result["tts_speech"].detach().float().cpu().numpy().reshape(-1)
        elif self.engine == "voxcpm15":
            iterator = self.model.tts_model._generate_with_prompt_cache(
                target_text=text,
                prompt_cache=self.prompt,
                inference_timesteps=self.config.get("inference_timesteps", 10),
                cfg_value=self.config.get("cfg_value", 2.0),
                retry_badcase=False,
                streaming=True,
            )
            try:
                for audio, _, _ in iterator:
                    yield audio.detach().float().cpu().numpy().reshape(-1)
            finally:
                iterator.close()
        else:
            for audio, rate, _ in self.model.generate_voice_clone_streaming(
                text=text,
                language="Chinese",
                ref_audio=self.config["reference_audio"],
                xvec_only=True,
                append_silence=False,
                do_sample=False,
                chunk_size=self.config.get("chunk_size", 4),
                parity_mode=False,
                non_streaming_mode=False,
                max_new_tokens=self.config.get("max_audio_tokens", 512),
            ):
                if rate != self.native_rate:
                    raise ValueError("unexpected_candidate_sample_rate")
                yield audio

    def generate(self, text: str) -> Iterator[bytes]:
        converter = FloatPCM(self.native_rate)
        cutter = LeadingSilenceFilter(24000)
        iterator = self.raw(text)
        try:
            for samples in iterator:
                if self.cancel.is_set():
                    return
                pcm = converter.feed(samples)
                if pcm and (chunk := cutter.feed(pcm)):
                    yield chunk
            if self.cancel.is_set():
                return
            if tail := converter.feed([], final=True):
                if chunk := cutter.feed(tail):
                    yield chunk
            if tail := cutter.finish():
                yield tail
        finally:
            iterator.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--engine", choices=["cosyvoice3", "voxcpm15", "qwen3"], required=True)
    arguments = parser.parse_args()
    config = json.loads(arguments.config.read_text())
    started = time.perf_counter()
    model = CandidatePCM(arguments.engine, config)
    for text in config["warmup_texts"]:
        for _ in model.generate(text):
            pass
    preparation_ms = round((time.perf_counter() - started) * 1000, 1)
    report = benchmark(model.generate, config["texts"], repeats=config["repeats"])
    report.update(
        engine=arguments.engine,
        preparation_ms=preparation_ms,
        config=config,
        gpu=model.torch.cuda.get_device_name(0),
        native_sample_rate=model.native_rate,
        output_sample_rate=24000,
        versions={"torch": importlib.metadata.version("torch")},
    )
    print("TTS_REPORT:" + json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
