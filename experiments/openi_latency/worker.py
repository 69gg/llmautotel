"""同 GPU 的三模型流式原型。全部录音与合成 PCM 只在内存中。"""

from __future__ import annotations

import base64
import importlib.metadata
import json
import queue
import sys
import threading
import time
import traceback
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from protocol import PlaybackClock, summarize, trim_silence

ROOT = Path(__file__).resolve().parent
PRINT_LOCK = threading.Lock()


def emit(event: dict[str, Any]) -> None:
    with PRINT_LOCK:
        print("OPENI_EVENT:" + json.dumps(event, ensure_ascii=False), flush=True)


class ASRStream:
    def __init__(self, model: Any, chunk: list[int], numpy: Any) -> None:
        self.model = model
        self.chunk = chunk
        self.numpy = numpy
        self.reset()

    def reset(self) -> None:
        self.cache: dict[str, Any] = {}
        self.pending = bytearray()
        self.text: list[str] = []
        self.compute_seconds = 0.0

    def feed(self, audio: bytes, *, final: bool = False) -> str:
        self.pending.extend(audio)
        size = self.chunk[1] * 960 * 2
        while len(self.pending) >= size or final:
            last = final and len(self.pending) <= size
            payload = bytes(self.pending[:size]) or b"\0\0"
            del self.pending[:size]
            waveform = self.numpy.frombuffer(payload, dtype="<i2").astype("float32") / 32768
            start = time.perf_counter()
            result = self.model.generate(
                input=waveform,
                cache=self.cache,
                is_final=last,
                chunk_size=self.chunk,
                encoder_chunk_look_back=4,
                decoder_chunk_look_back=1,
                batch_size=1,
                disable_pbar=True,
            )
            self.compute_seconds += time.perf_counter() - start
            self.text.extend(str(row.get("text", "")) for row in result)
            if last:
                break
        return "".join(self.text)


class Models:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        sys.path[:0] = [str(ROOT / "CosyVoice"), str(ROOT / "CosyVoice/third_party/Matcha-TTS")]
        import numpy as np
        import torch
        import torchaudio
        from cosyvoice.cli.cosyvoice import AutoModel as CosyModel
        from funasr import AutoModel as ASRModel
        from modelscope import snapshot_download
        from transformers import AutoModelForCausalLM, AutoTokenizer

        if not torch.cuda.is_available():
            raise RuntimeError("cuda_unavailable")
        self.np, self.torch, self.torchaudio = np, torch, torchaudio
        torch.set_num_threads(4)
        self.cancel = threading.Event()
        self.turns = 0
        self.generation = 0
        self.fixtures: list[bytes] = []

        emit({"type": "status", "phase": "loading_asr"})
        asr = ASRModel(
            model=config["asr_model"],
            model_revision=config["asr_revision"],
            device="cuda:0",
            disable_update=True,
            disable_pbar=True,
        )
        self.asr = ASRStream(asr, config["asr_chunk"], np)
        emit({"type": "status", "phase": "loading_llm"})
        llm_path = snapshot_download(config["llm_model"])
        self.tokenizer = AutoTokenizer.from_pretrained(llm_path)
        self.llm = (
            AutoModelForCausalLM.from_pretrained(
                llm_path, torch_dtype=torch.bfloat16, attn_implementation="sdpa"
            )
            .to("cuda:0")
            .eval()
        )
        emit({"type": "status", "phase": "loading_tts"})
        tts_path = snapshot_download(config["tts_model"])
        self.tts = CosyModel(model_dir=tts_path, fp16=True, load_vllm=False, load_trt=False)
        self.tts.add_zero_shot_spk(
            config["prompt_text"], str(ROOT / "CosyVoice" / config["prompt_audio"]), "benchmark"
        )
        self.sample_rate = int(self.tts.sample_rate)
        self.manifest = {
            "gpu": torch.cuda.get_device_name(0),
            "gpu_total_bytes": torch.cuda.get_device_properties(0).total_memory,
            "gpu_allocated_bytes": torch.cuda.memory_allocated(),
            "cuda": torch.version.cuda,
            "versions": {
                name: importlib.metadata.version(name)
                for name in ["torch", "torchaudio", "transformers", "funasr", "modelscope"]
            },
            "models": {key: config[key] for key in ["asr_model", "llm_model", "tts_model"]},
            "engine": "pytorch_sdpa_baseline_no_vllm_no_tensorrt",
            "sample_rate": self.sample_rate,
        }

    def synthesize(self, text: str | Iterator[str]) -> Iterator[bytes]:
        with self.torch.inference_mode():
            for result in self.tts.inference_zero_shot(
                text, "", "", zero_shot_spk_id="benchmark", stream=True, text_frontend=False
            ):
                if self.cancel.is_set():
                    break
                values = result["tts_speech"].detach().float().cpu().numpy().reshape(-1)
                yield (values.clip(-1, 1) * 32767).astype("<i2").tobytes()

    def make_fixtures(self) -> None:
        if not self.fixtures:
            self.cancel.clear()
            for question in self.config["questions"]:
                pcm = b"".join(self.synthesize(question))
                values = (
                    self.torch.from_numpy(self.np.frombuffer(pcm, "<i2").copy()).float() / 32768
                )
                resampled = self.torchaudio.functional.resample(values, self.sample_rate, 16000)
                audio = (resampled.numpy().clip(-1, 1) * 32767).astype("<i2").tobytes()
                self.fixtures.append(trim_silence(audio, 16000))

    def reply(self, callback: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        from transformers import StoppingCriteria, StoppingCriteriaList, TextIteratorStreamer

        if self.turns >= self.config["max_turns"]:
            raise RuntimeError("turn_budget_exhausted")
        self.turns += 1
        start = time.perf_counter()
        transcript = self.asr.feed(b"", final=True)
        asr_ms = (time.perf_counter() - start) * 1000
        if not transcript.strip():
            raise RuntimeError("empty_transcript")
        callback({"type": "transcript", "text": transcript, "generation": self.generation})
        prompt = self.tokenizer.apply_chat_template(
            [
                {"role": "system", "content": self.config["system_prompt"]},
                {"role": "user", "content": transcript},
            ],
            tokenize=False,
            add_generation_prompt=True,
        )
        inputs = self.tokenizer([prompt], return_tensors="pt").to("cuda:0")
        streamer = TextIteratorStreamer(
            self.tokenizer,
            skip_prompt=True,
            skip_special_tokens=True,
            timeout=self.config["generation_timeout_seconds"],
        )
        cancelled = self.cancel
        deadline = time.perf_counter() + self.config["generation_timeout_seconds"]

        class Stop(StoppingCriteria):
            def __call__(self, input_ids: Any, scores: Any, **kwargs: Any) -> bool:
                return cancelled.is_set() or time.perf_counter() >= deadline

        errors: list[BaseException] = []

        def generate() -> None:
            try:
                with self.torch.inference_mode():
                    self.llm.generate(
                        **inputs,
                        streamer=streamer,
                        do_sample=False,
                        stopping_criteria=StoppingCriteriaList([Stop()]),
                    )
            except BaseException as exc:
                errors.append(exc)
                streamer.end()

        generated: list[str] = []
        first_text_at: float | None = None

        def text_stream() -> Iterator[str]:
            nonlocal first_text_at
            for piece in streamer:
                if self.cancel.is_set():
                    break
                if piece:
                    if first_text_at is None:
                        first_text_at = time.perf_counter()
                    generated.append(piece)
                    callback({"type": "text", "text": piece, "generation": self.generation})
                    yield piece
            if errors:
                raise RuntimeError("llm_generation_failed") from errors[0]

        thread = threading.Thread(target=generate, daemon=True)
        thread.start()
        clock = PlaybackClock()
        first_packet_at: float | None = None
        try:
            for pcm in self.synthesize(text_stream()):
                now = time.perf_counter()
                first_packet_at = first_packet_at or now
                clock.feed(pcm, self.sample_rate, now)
                callback(
                    {
                        "type": "audio",
                        "pcm": base64.b64encode(pcm).decode(),
                        "sample_rate": self.sample_rate,
                        "generation": self.generation,
                    }
                )
        finally:
            self.cancel.set()
            thread.join(self.config["generation_timeout_seconds"] + 2)
            if thread.is_alive():
                raise RuntimeError("llm_did_not_stop")
        if clock.first_signal_at is None:
            raise RuntimeError("no_effective_reply_audio")
        result = {
            "ok": True,
            "generation": self.generation,
            "user_text": transcript,
            "assistant_text": "".join(generated),
            "asr_finalize_ms": round(asr_ms, 1),
            "asr_total_compute_ms": round(self.asr.compute_seconds * 1000, 1),
            "llm_first_text_after_end_ms": round((first_text_at - start) * 1000, 1)
            if first_text_at is not None
            else None,
            "first_pcm_after_end_ms": round((first_packet_at - start) * 1000, 1)
            if first_packet_at is not None
            else None,
            "effective_audio_ms": round((clock.first_signal_at - start) * 1000, 1),
        }
        callback({"type": "done", **result})
        return result

    def local_benchmark(self, repeats: int) -> dict[str, Any]:
        self.make_fixtures()
        samples: list[dict[str, Any]] = []
        for index, audio in enumerate(self.fixtures * repeats):
            self.cancel.clear()
            self.asr.reset()
            self.generation += 1
            start = time.perf_counter()
            size = 640
            for offset in range(0, len(audio), size):
                due = start + min(offset + size, len(audio)) / 32000
                time.sleep(max(0, due - time.perf_counter()))
                self.asr.feed(audio[offset : offset + size])
            stopped = start + len(audio) / 32000
            time.sleep(self.config["endpoint_wait_ms"] / 1000)
            first = PlaybackClock()

            def observe(event: dict[str, Any]) -> None:
                if event["type"] == "audio":
                    first.feed(
                        base64.b64decode(event["pcm"]), event["sample_rate"], time.perf_counter()
                    )

            try:
                result = self.reply(observe)
                if first.first_signal_at is None:
                    raise RuntimeError("no_signal")
                result["effective_audio_ms"] = round((first.first_signal_at - stopped) * 1000, 1)
                result["fixture"] = index % len(self.fixtures)
            except Exception as exc:
                result = {"ok": False, "error": type(exc).__name__, "detail": str(exc)[:300]}
            samples.append(result)
            emit({"type": "sample", "index": index, "result": result})
        return {
            "type": "report",
            "scope": "openi_local_memory_terminal",
            "endpointing": "simulated_wait_not_real_vad",
            "warmup": "fixture_tts_prepared_before_first_sample",
            "endpoint_wait_ms": self.config["endpoint_wait_ms"],
            "fixture_source": "synthetic_tts_not_real_caller",
            "manifest": self.manifest,
            "samples": samples,
            "summary": summarize(samples),
        }


def main() -> None:
    config = json.loads((ROOT / "settings.run.json").read_text())
    models = Models(config)
    commands: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=512)

    def receive() -> None:
        for line in sys.stdin:
            command = json.loads(line)
            if command.get("type") == "cancel":
                models.cancel.set()
            else:
                commands.put(command)
        models.cancel.set()

    threading.Thread(target=receive, daemon=True).start()
    emit({"type": "ready", "phase": "ready", "manifest": models.manifest})
    audio_bytes = 0
    while True:
        command = commands.get()
        kind = command["type"]
        try:
            if kind == "begin":
                models.generation = int(command["generation"])
                models.cancel.clear()
                models.asr.reset()
                audio_bytes = 0
                emit({"type": "begun", "generation": models.generation})
            elif kind == "audio" and command["generation"] == models.generation:
                audio = base64.b64decode(command["pcm"], validate=True)
                audio_bytes += len(audio)
                if audio_bytes > config["max_audio_seconds"] * 32000:
                    raise ValueError("audio_budget_exhausted")
                models.asr.feed(audio)
            elif kind == "end" and command["generation"] == models.generation:
                models.reply(emit)
            elif kind == "fixtures":
                models.make_fixtures()
                emit(
                    {
                        "type": "fixtures",
                        "sample_rate": 16000,
                        "audio": [base64.b64encode(pcm).decode() for pcm in models.fixtures],
                    }
                )
            elif kind == "local_benchmark":
                report = models.local_benchmark(min(3, max(1, int(command.get("repeats", 2)))))
                emit(report)
        except Exception as exc:
            traceback.print_exc()
            emit(
                {
                    "type": "turn_error",
                    "generation": models.generation,
                    "error": type(exc).__name__,
                    "detail": str(exc)[:400],
                }
            )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        traceback.print_exc()
        emit(
            {
                "type": "error",
                "phase": "model_initialization_failed",
                "error": type(exc).__name__,
                "detail": str(exc)[:400],
            }
        )
        raise
