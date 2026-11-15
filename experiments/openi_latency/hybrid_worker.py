"""低延迟三模型实验：隔离 ASR，复用小 LLM 与 CUDA Graph TTS。"""

from __future__ import annotations

import base64
import importlib.metadata
import json
import queue
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

if __package__:
    from .optimization import LeadingSilenceFilter
    from .synthesis import text_segments
    from .worker import Models, emit
else:
    from optimization import LeadingSilenceFilter
    from synthesis import text_segments
    from worker import Models, emit


class ASRProcess:
    """分离 Transformers 版本；每个请求有期限，模型常驻而非逐轮加载。"""

    def __init__(self, root: Path, config: dict[str, Any]) -> None:
        self.timeout = float(config["generation_timeout_seconds"])
        self.process = subprocess.Popen(
            [
                str(Path(config["asr_worker_environment"]) / "bin/python"),
                "-u",
                str(root / "hybrid_asr.py"),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self.events: queue.Queue[dict[str, Any]] = queue.Queue()

        def read() -> None:
            assert self.process.stdout is not None
            for line in self.process.stdout:
                if line.startswith("ASR_EVENT:"):
                    self.events.put(json.loads(line.removeprefix("ASR_EVENT:")))
            self.events.put({"error": "asr_process_exited"})

        threading.Thread(target=read, daemon=True).start()
        self.manifest = self.receive(float(config["model_prepare_timeout_seconds"]))
        self.compute_seconds = 0.0

    def receive(self, timeout: float) -> dict[str, Any]:
        try:
            result = self.events.get(timeout=timeout)
        except queue.Empty as exc:
            self.process.terminate()
            raise TimeoutError("asr_request_deadline") from exc
        if result.get("error"):
            raise RuntimeError(str(result["error"]))
        return result

    def request(self, message: dict[str, Any]) -> dict[str, Any]:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()
        result = self.receive(self.timeout)
        self.compute_seconds = float(result.get("compute_seconds", 0))
        return result

    def reset(self) -> None:
        self.request({"type": "reset"})

    def feed(self, audio: bytes, *, final: bool = False) -> str:
        return str(
            self.request(
                {"type": "audio", "pcm": base64.b64encode(audio).decode(), "final": final}
            ).get("text", "")
        )


class QwenPCM:
    """先交付有意义的小句；每句真正输出 PCM，不填充、不伪造首音。"""

    def __init__(self, model: Any, config: dict[str, Any], cancel: threading.Event) -> None:
        self.model, self.config, self.cancel = model, config, cancel
        self.trace: dict[str, Any] = {}

    def generate(self, text: str | Iterator[str]) -> Iterator[bytes]:
        import numpy as np

        clauses = (
            iter([text])
            if isinstance(text, str)
            else text_segments(text, self.config["tts_clause_min_chars"])
        )
        self.trace = {
            "text_mode": "complete" if isinstance(text, str) else "clauses",
            "clauses": [],
            "chunk_size": self.config["tts_chunk_size"],
        }
        for clause in clauses:
            if self.cancel.is_set():
                return
            self.trace["clauses"].append(clause)
            cutter = LeadingSilenceFilter(24000)
            generator = self.model.generate_voice_clone_streaming(
                text=clause,
                language="Chinese",
                ref_audio=self.config["qwen3_reference_audio"],
                xvec_only=True,
                append_silence=False,
                do_sample=False,
                chunk_size=self.config["tts_chunk_size"],
                parity_mode=False,
                non_streaming_mode=False,
                max_new_tokens=self.config["qwen3_max_audio_tokens"],
            )
            try:
                for audio, sample_rate, _timing in generator:
                    if self.cancel.is_set():
                        return
                    if sample_rate != 24000:
                        raise ValueError("unexpected_sample_rate")
                    values = np.asarray(audio).reshape(-1)
                    pcm = (values.clip(-1, 1) * 32767).astype("<i2").tobytes()
                    if self.config["tts_trim_leading_silence"]:
                        pcm = cutter.feed(pcm)
                    if pcm:
                        yield pcm
                if self.config["tts_trim_leading_silence"]:
                    if tail := cutter.finish():
                        yield tail
            finally:
                generator.close()


class HybridModels(Models):
    """复用现有逐轮管线、合成问题、协议和取消，不引入第二套计时。"""

    def __init__(self, config: dict[str, Any]) -> None:
        import numpy as np
        import torch
        import torchaudio
        from faster_qwen3_tts import FasterQwen3TTS
        from transformers import AutoModelForCausalLM, AutoTokenizer

        root = Path(__file__).resolve().parent
        self.config = config
        self.np, self.torch, self.torchaudio = np, torch, torchaudio
        torch.set_num_threads(4)
        self.cancel = threading.Event()
        self.turns = self.generation = 0
        self.fixtures: list[bytes] = []
        paths = json.loads((root / "model-paths.run.json").read_text())
        started = time.perf_counter()
        emit({"type": "status", "phase": "loading_isolated_asr"})
        self.asr = ASRProcess(root, config)
        emit({"type": "status", "phase": "loading_hybrid_llm"})
        self.tokenizer = AutoTokenizer.from_pretrained(paths["llm"], local_files_only=True)
        self.llm = (
            AutoModelForCausalLM.from_pretrained(
                paths["llm"],
                dtype=torch.bfloat16,
                attn_implementation="sdpa",
                local_files_only=True,
            )
            .to("cuda:0")
            .eval()
        )
        emit({"type": "status", "phase": "loading_hybrid_tts"})
        self.tts = FasterQwen3TTS.from_pretrained(
            config["qwen3_model_directory"],
            device="cuda",
            dtype=torch.bfloat16,
            attn_implementation="sdpa",
            max_seq_len=config["qwen3_max_seq_len"],
        )
        self.tts.warmup()
        self.sample_rate = 24000
        self.synthesis = QwenPCM(self.tts, config, self.cancel)
        for _ in self.synthesize(config["qwen3_warmup_text"]):
            pass
        warmup = self.tokenizer.apply_chat_template(
            [{"role": "user", "content": config["llm_warmup_text"]}],
            tokenize=False,
            add_generation_prompt=True,
            enable_thinking=False,
        )
        with torch.inference_mode():
            self.llm.generate(
                **self.tokenizer([warmup], return_tensors="pt").to("cuda:0"),
                do_sample=False,
                max_new_tokens=config["llm_warmup_tokens"],
            )
        self.manifest = {
            "gpu": torch.cuda.get_device_name(0),
            "backend": "hybrid_qwen3_graph",
            "models": {
                "asr": config["asr_model"],
                "llm": config["llm_model"],
                "tts": "Qwen3-TTS-12Hz-0.6B-Base",
            },
            "asr_streaming": config["asr_streaming"],
            "asr_manifest": self.asr.manifest,
            "versions": {
                p: importlib.metadata.version(p)
                for p in ["torch", "transformers", "faster-qwen3-tts"]
            },
            "load_and_warmup_ms": round((time.perf_counter() - started) * 1000, 1),
            "tts_input": "complete_clauses_not_unknown_text_bistream",
            "sample_rate": self.sample_rate,
        }

    def synthesize(
        self, text: str | Iterator[str], *, reset_hop: bool | None = None
    ) -> Iterator[bytes]:
        yield from self.synthesis.generate(text)

    def make_fixtures(self) -> None:
        prepared = bool(self.fixtures)
        super().make_fixtures()
        if not prepared:
            # 首次算子初始化单列为预热，不污染首轮测量；问题音频仍不落盘。
            self.asr.reset()
            self.asr.feed(self.fixtures[0], final=True)
            self.asr.reset()
