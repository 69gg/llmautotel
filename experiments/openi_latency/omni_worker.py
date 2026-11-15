"""MiniCPM-o 原生半双工流式实验，复用现有网关和播放计时。"""

from __future__ import annotations

import base64
import importlib.metadata
import json
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

if __package__:
    from .protocol import PlaybackClock
    from .worker import Models, emit
else:
    from protocol import PlaybackClock
    from worker import Models, emit


class AudioPrefill:
    """每个完整秒在讲话期间预填，尾块在停口后补齐并封口。"""

    def __init__(self, owner: Any, chunk_bytes: int = 32000) -> None:
        self.owner = owner
        self.chunk_bytes = chunk_bytes
        self.pending = bytearray()
        self.compute_seconds = 0.0
        self.padding_ms = 0.0
        self.chunks = 0

    def reset(self) -> None:
        self.pending.clear()
        self.compute_seconds = 0.0
        self.padding_ms = 0.0
        self.chunks = 0
        self.owner.start_session()

    def feed(self, pcm: bytes, *, final: bool = False) -> str:
        if len(pcm) % 2:
            raise ValueError("unaligned_input_pcm")
        self.pending.extend(pcm)
        while len(self.pending) >= self.chunk_bytes:
            self._prefill(bytes(self.pending[: self.chunk_bytes]), False)
            del self.pending[: self.chunk_bytes]
        if final:
            self.padding_ms = (self.chunk_bytes - len(self.pending)) / 32
            tail = bytes(self.pending) + bytes(self.chunk_bytes - len(self.pending))
            self.pending.clear()
            self._prefill(tail, True)
        return ""

    def _prefill(self, pcm: bytes, final: bool) -> None:
        started = time.perf_counter()
        self.owner.prefill_audio(pcm, final=final)
        self.compute_seconds += time.perf_counter() - started
        self.chunks += 1


class MiniCPMModels(Models):
    def __init__(self, config: dict[str, Any]) -> None:
        import numpy as np
        import soundfile as sf
        import torch
        import torchaudio
        from transformers import AutoModel

        self.config, self.np, self.torch, self.torchaudio = config, np, torch, torchaudio
        self.cancel = threading.Event()
        self.turns = self.generation = self.session_counter = 0
        self.fixtures: list[bytes] = []
        self.sample_rate = 24000
        self.model_path = Path(
            json.loads((Path(__file__).parent / "model-paths.run.json").read_text())["omni"]
        )
        torch.set_num_threads(4)
        started = time.perf_counter()
        emit({"type": "status", "phase": "loading_omni"})
        self.model = (
            AutoModel.from_pretrained(
                str(self.model_path),
                trust_remote_code=True,
                local_files_only=True,
                attn_implementation="sdpa",
                torch_dtype=torch.bfloat16,
                init_vision=False,
                init_audio=True,
                init_tts=True,
            )
            .eval()
            .cuda()
        )
        emit({"type": "status", "phase": "loading_omni_token2wav"})
        self.model.init_tts(
            model_dir=str(self.model_path / "assets/token2wav"),
            enable_float16=config["omni_tts_fp16"],
        )
        reference, rate = sf.read(self.model_path / config["omni_reference_audio"], dtype="float32")
        if rate != 16000 or reference.ndim != 1:
            raise ValueError("reference_must_be_mono_16khz")
        self.reference = reference
        self.model.init_token2wav_cache(prompt_speech_16k=reference)
        self.asr = AudioPrefill(self)
        self.manifest = {
            "backend": "minicpm_o45",
            "mode": "native_half_duplex_streaming_audio",
            "gpu": torch.cuda.get_device_name(0),
            "gpu_total_bytes": torch.cuda.get_device_properties(0).total_memory,
            "gpu_allocated_bytes": torch.cuda.memory_allocated(),
            "cuda": torch.version.cuda,
            "versions": {
                name: importlib.metadata.version(name)
                for name in ["torch", "torchaudio", "transformers", "minicpmo-utils", "onnxruntime"]
            },
            "source_revision": config["omni_source_revision"],
            "model_source": config["omni_mounted_model"],
            "weight_provenance": "openi_mount_not_full_hash_verified",
            "system_prompt": config["system_prompt"],
            "generation_options": config["omni_generation_options"],
            "tts_fp16": config["omni_tts_fp16"],
            "vision_loaded": False,
            "sample_rate": self.sample_rate,
            "model_load_ms": round((time.perf_counter() - started) * 1000, 1),
        }

    def start_session(self) -> None:
        self.session_counter += 1
        self.session_id = f"benchmark-{self.session_counter}"
        self.model.reset_session(reset_token2wav_cache=False)
        system = {
            "role": "system",
            "content": [
                self.config["omni_voice_instruction"],
                self.reference,
                self.config["system_prompt"],
            ],
        }
        with self.torch.inference_mode():
            self.model.streaming_prefill(
                session_id=self.session_id,
                msgs=[system],
                omni_mode=False,
                is_last_chunk=True,
                enable_thinking=False,
            )

    def prefill_audio(self, pcm: bytes, *, final: bool) -> None:
        audio = self.np.frombuffer(pcm, "<i2").astype("float32") / 32768
        with self.torch.inference_mode():
            self.model.streaming_prefill(
                session_id=self.session_id,
                msgs=[{"role": "user", "content": [audio]}],
                omni_mode=False,
                is_last_chunk=final,
                enable_thinking=False,
            )

    def generate(self, *, forced_text: str | None = None) -> Iterator[tuple[bytes, str]]:
        deadline = time.monotonic() + self.config["generation_timeout_seconds"]
        generation = self.generation
        with self.torch.inference_mode():
            iterator = self.model.streaming_generate(
                session_id=self.session_id,
                generate_audio=True,
                use_tts_template=True,
                enable_thinking=False,
                teacher_forcing=forced_text is not None,
                teacher_forcing_text=forced_text or "",
                **self.config["omni_generation_options"],
            )
            try:
                for waveform, text in iterator:
                    if self.cancel.is_set() or generation != self.generation:
                        return
                    if time.monotonic() >= deadline:
                        raise TimeoutError("omni_generation_deadline")
                    values = waveform.detach().float().cpu().numpy().reshape(-1)
                    pcm = (values.clip(-1, 1) * 32767).astype("<i2").tobytes()
                    yield pcm, text
            finally:
                iterator.close()

    def synthesize(
        self, text: str | Iterator[str], *, reset_hop: bool | None = None
    ) -> Iterator[bytes]:
        if not isinstance(text, str):
            raise ValueError("fixture_text_must_be_complete")
        self.start_session()
        with self.torch.inference_mode():
            self.model.streaming_prefill(
                session_id=self.session_id,
                msgs=[{"role": "user", "content": [self.config["fixture_instruction"]]}],
                omni_mode=False,
                is_last_chunk=True,
            )
        for pcm, _ in self.generate(forced_text=text):
            yield pcm

    def reply(self, callback: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        if self.turns >= self.config["max_turns"]:
            raise RuntimeError("turn_budget_exhausted")
        self.turns += 1
        generation = self.generation
        started = time.perf_counter()
        self.asr.feed(b"", final=True)
        final_prefill_ms = (time.perf_counter() - started) * 1000
        clock = PlaybackClock()
        first_pcm: float | None = None
        first_text: float | None = None
        text_parts: list[str] = []
        audio_bytes = chunks = 0
        for pcm, text in self.generate():
            if self.cancel.is_set() or self.generation != generation:
                raise RuntimeError("omni_generation_cancelled")
            now = time.perf_counter()
            if text:
                first_text = first_text or now
                text_parts.append(text)
                callback({"type": "text", "text": text, "generation": generation})
            if pcm:
                first_pcm = first_pcm or now
                clock.feed(pcm, self.sample_rate, now)
                chunks += 1
                audio_bytes += len(pcm)
                callback(
                    {
                        "type": "audio",
                        "pcm": base64.b64encode(pcm).decode(),
                        "sample_rate": self.sample_rate,
                        "generation": generation,
                    }
                )
        if clock.first_signal_at is None:
            raise RuntimeError("no_effective_reply_audio")
        result = {
            "ok": True,
            "generation": generation,
            "user_text": None,
            "user_text_source": "direct_audio_no_separate_transcript",
            "assistant_text": "".join(text_parts),
            "omni_final_prefill_ms": round(final_prefill_ms, 1),
            "omni_audio_prefill_compute_ms": round(self.asr.compute_seconds * 1000, 1),
            "omni_input_chunks": self.asr.chunks,
            "omni_tail_padding_ms": self.asr.padding_ms,
            "first_delivered_text_after_end_ms": round((first_text - started) * 1000, 1)
            if first_text is not None
            else None,
            "first_pcm_after_end_ms": round((first_pcm - started) * 1000, 1)
            if first_pcm is not None
            else None,
            "effective_audio_ms": round((clock.first_signal_at - started) * 1000, 1),
            "generation_total_ms": round((time.perf_counter() - started) * 1000, 1),
            "audio_ms": round(audio_bytes / (self.sample_rate * 2) * 1000, 1),
            "audio_chunks": chunks,
            "gpu_peak_allocated_bytes": self.torch.cuda.max_memory_allocated(),
        }
        callback({"type": "done", **result})
        return result

    def local_benchmark(self, repeats: int, reset_hop: bool | None = None) -> dict[str, Any]:
        report = super().local_benchmark(repeats)
        report.pop("tts_reset_hop", None)
        report["fixture_source"] = "omni_teacher_forced_synthetic_questions"
        for row in report["samples"]:
            if "fixture" in row:
                row["expected_question"] = self.config["questions"][row["fixture"]]
        return report
