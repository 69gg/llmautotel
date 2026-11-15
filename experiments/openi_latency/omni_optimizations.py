"""OpenI 上的 Omni 低延迟逐项对照；GPU 仅在远端进程使用。"""

from __future__ import annotations

import base64
import re
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

if __package__:
    from .omni_worker import MiniCPMModels
    from .optimization import LeadingSilenceFilter, ReplyAudioCache, bind_streaming_chunk_options
    from .protocol import PlaybackClock, summarize
    from .worker import emit
else:
    from omni_worker import MiniCPMModels
    from optimization import LeadingSilenceFilter, ReplyAudioCache, bind_streaming_chunk_options
    from protocol import PlaybackClock, summarize
    from worker import emit


class OptimizedMiniCPMModels(MiniCPMModels):
    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self.manifest["chunk_patch"] = bind_streaming_chunk_options(self.model)
        self.variant: dict[str, Any] = {}
        self.variant_name = "baseline"
        self.trimmed_ms = 0.0
        self.compilation: dict[str, Any] = {"enabled": False}
        self.original_llm_forward = self.model.llm.forward
        self.original_estimator_forward = (
            self.model.tts.audio_tokenizer.flow.decoder.estimator.forward_chunk
        )
        self.cache: ReplyAudioCache | None = None
        self.select_variant("baseline")

    def snapshot(self) -> dict[str, Any]:
        return {
            "prompt": self.config["system_prompt"],
            "voice": self.config["omni_reference_audio"],
            "source": self.config["omni_source_revision"],
            "sample_rate": self.sample_rate,
            "approved_replies": self.config["optimization_cached_replies"],
        }

    def select_variant(self, name: str) -> dict[str, Any]:
        variants = self.config["optimization_variants"]
        if name not in variants:
            raise ValueError("unknown_optimization_variant")
        self.variant_name, self.variant = name, dict(variants[name])
        audio_tokens = int(self.variant.get("audio_tokens", 25))
        text_tokens = int(self.variant.get("text_tokens", 10))
        if not 5 <= audio_tokens <= 25 or not 1 <= text_tokens <= 10:
            raise ValueError("invalid_optimization_chunk_size")
        self.model._latency_audio_tokens = audio_tokens
        self.model._latency_text_tokens = text_tokens
        self.config["omni_generation_options"]["audio_token_chunk_size"] = audio_tokens
        self.config["endpoint_wait_ms"] = int(self.variant.get("endpoint_wait_ms", 200))
        return {"type": "configured", "variant": name, "options": self.variant}

    def generate(self, *, forced_text: str | None = None) -> Iterator[tuple[bytes, str]]:
        cutter = LeadingSilenceFilter(
            self.sample_rate, **self.config["optimization_silence_filter"]
        )
        trim = bool(self.variant.get("trim", False))
        for pcm, text in super().generate(forced_text=forced_text):
            yield cutter.feed(pcm) if trim else pcm, text
        tail = cutter.finish() if trim else b""
        if tail:
            yield tail, ""
        self.trimmed_ms = cutter.removed_ms if trim else 0.0

    def prepare_cache(self) -> None:
        if self.cache is not None and self.cache.fingerprint == ReplyAudioCache.snapshot_key(
            self.snapshot()
        ):
            return
        previous = self.variant_name
        self.select_variant("trim")
        started = time.perf_counter()
        self.cache = ReplyAudioCache(self.snapshot())
        for text in self.config["optimization_cached_replies"]:
            self.cache.put(text, b"".join(self.synthesize(text)))
        self.manifest["reply_cache"] = {
            "preparation_ms": round((time.perf_counter() - started) * 1000, 1),
            "entries": len(self.cache.entries),
            "pcm_bytes": sum(map(len, self.cache.entries.values())),
            "matching": "exact_complete_model_reply_not_fixture_oracle",
        }
        self.select_variant(previous)

    def cached_reply(self, callback: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        if self.turns >= self.config["max_turns"]:
            raise RuntimeError("turn_budget_exhausted")
        self.turns += 1
        started = time.perf_counter()
        self.asr.feed(b"", final=True)
        with self.torch.inference_mode():
            iterator = self.model.streaming_generate(
                session_id=self.session_id,
                generate_audio=False,
                use_tts_template=False,
                enable_thinking=False,
                do_sample=False,
            )
            parts = [text for text, _ in iterator]
        text = re.sub(r"<\|[^<>]+\|>", "", "".join(parts)).strip()
        pcm = self.cache.get(text, self.snapshot()) if self.cache else None
        if pcm is None:
            raise RuntimeError("approved_reply_cache_miss")
        now = time.perf_counter()
        clock = PlaybackClock()
        clock.feed(pcm, self.sample_rate, now)
        callback({"type": "text", "text": text, "generation": self.generation})
        callback(
            {
                "type": "audio",
                "pcm": base64.b64encode(pcm).decode(),
                "sample_rate": self.sample_rate,
                "generation": self.generation,
            }
        )
        result = {
            "ok": clock.first_signal_at is not None,
            "generation": self.generation,
            "user_text": None,
            "assistant_text": text,
            "cache_hit": True,
            "cache_match_ms": round((now - started) * 1000, 1),
            "effective_audio_ms": round((clock.first_signal_at - started) * 1000, 1)
            if clock.first_signal_at is not None
            else None,
        }
        callback({"type": "done", **result})
        return result

    def reply(self, callback: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        if self.variant.get("cache"):
            result = self.cached_reply(callback)
        else:
            result = super().reply(callback)
        result.update(variant=self.variant_name, trimmed_leading_ms=round(self.trimmed_ms, 1))
        return result

    def compile_models(self) -> None:
        """编译和两次预热独立计时；失败还原 eager，不伪装成编译成功。"""
        if self.compilation.get("enabled"):
            return
        options = self.config["optimization_compile"]
        started = time.perf_counter()
        emit({"type": "status", "phase": "compiling_omni"})
        old_timeout = self.config["generation_timeout_seconds"]
        self.config["generation_timeout_seconds"] = options["timeout_seconds"]
        try:
            self.model.llm.forward = self.torch.compile(self.original_llm_forward, **options["llm"])
            estimator = self.model.tts.audio_tokenizer.flow.decoder.estimator
            estimator.forward_chunk = self.torch.compile(
                self.original_estimator_forward, **options["estimator"]
            )
            for _ in range(options["warmups"]):
                for _pcm in self.synthesize(self.config["optimization_warmup_text"]):
                    pass
            self.compilation = {
                "enabled": True,
                "preparation_ms": round((time.perf_counter() - started) * 1000, 1),
                "options": options,
            }
        except Exception as exc:
            self.model.llm.forward = self.original_llm_forward
            self.model.tts.audio_tokenizer.flow.decoder.estimator.forward_chunk = (
                self.original_estimator_forward
            )
            self.compilation = {
                "enabled": False,
                "error": type(exc).__name__,
                "detail": str(exc)[:500],
                "preparation_ms": round((time.perf_counter() - started) * 1000, 1),
            }
            raise
        finally:
            self.config["generation_timeout_seconds"] = old_timeout
            emit({"type": "status", "phase": "ready"})
            self.manifest["compilation"] = self.compilation

    def benchmark_variant(self, name: str, repeats: int) -> dict[str, Any]:
        self.select_variant(name)
        if self.variant.get("cache"):
            self.prepare_cache()
        if self.variant.get("compile"):
            self.compile_models()
        samples: list[dict[str, Any]] = []
        for index, audio in enumerate(self.fixtures * repeats):
            self.cancel.clear()
            self.asr.reset()
            self.generation += 1
            started = time.perf_counter()
            for offset in range(0, len(audio), 640):
                due = started + min(offset + 640, len(audio)) / 32000
                time.sleep(max(0, due - time.perf_counter()))
                self.asr.feed(audio[offset : offset + 640])
            stopped = started + len(audio) / 32000
            confirm_at = stopped + self.config["endpoint_wait_ms"] / 1000
            clock = PlaybackClock()
            confirmed = threading.Event()
            outcome: list[dict[str, Any]] = []

            def observe(event: dict[str, Any]) -> None:
                if event["type"] == "audio":
                    confirmed.wait()
                    clock.feed(
                        base64.b64decode(event["pcm"]), event["sample_rate"], time.perf_counter()
                    )

            def respond() -> None:
                try:
                    outcome.append(self.reply(observe))
                except Exception as exc:
                    outcome.append(
                        {"ok": False, "error": type(exc).__name__, "detail": str(exc)[:400]}
                    )

            speculation = bool(self.variant.get("speculate"))
            if not speculation:
                time.sleep(max(0, confirm_at - time.perf_counter()))
                confirmed.set()
                respond()
            else:
                thread = threading.Thread(target=respond)
                thread.start()
                time.sleep(max(0, confirm_at - time.perf_counter()))
                confirmed.set()
                thread.join()
            result = outcome[0]
            result.update(
                variant=name,
                fixture=index % len(self.fixtures),
                expected_question=self.config["questions"][index % len(self.fixtures)],
                speculation_start="actual_input_end_oracle" if speculation else None,
                confirmation_wait_ms=self.config["endpoint_wait_ms"],
            )
            if result.get("ok") and clock.first_signal_at is not None:
                result["effective_audio_ms"] = round((clock.first_signal_at - stopped) * 1000, 1)
            else:
                result["ok"] = False
            samples.append(result)
            emit({"type": "sample", "index": len(samples) - 1, "result": result})
        return {
            "variant": name,
            "options": self.variant,
            "samples": samples,
            "summary": summarize(samples),
        }

    def rollback_benchmark(self) -> dict[str, Any]:
        """候选音频不发送；关闭生成器、恢复 KV，再输入实际新问题。"""
        self.select_variant("trim")
        self.cancel.clear()
        self.asr.reset()
        self.asr.feed(self.fixtures[0], final=True)
        try:
            snapshot = self.model.save_speculative_snapshot()
            iterator = self.generate()
            candidate, candidate_text = next(iterator)
            iterator.close()
            restored = self.model.restore_speculative_snapshot(snapshot)
            cache_length = self.model._get_kv_cache_length()
            if not restored or cache_length != snapshot.llm_cache_length:
                raise RuntimeError("rollback_cache_not_restored")
            # 新问题为真实输入音频，不以预期问题或 fixture 编号选择回答。
            self.asr.feed(self.fixtures[1], final=True)
            delivered: list[dict[str, Any]] = []
            result = self.reply(delivered.append)
            return {
                "ok": result["ok"],
                "restored": restored,
                "cache_length_restored": cache_length == snapshot.llm_cache_length,
                "candidate_pcm_bytes_discarded": len(candidate),
                "candidate_text_discarded": candidate_text,
                "candidate_audio_delivered": False,
                "new_expected_question": self.config["questions"][1],
                "new_reply": result,
            }
        except Exception as exc:
            return {"ok": False, "error": type(exc).__name__, "detail": str(exc)[:400]}

    def optimization_benchmark(self, repeats: int = 1) -> dict[str, Any]:
        self.select_variant("baseline")
        self.make_fixtures()
        variants: list[dict[str, Any]] = []
        for name in self.config["optimization_variants"]:
            try:
                variants.append(self.benchmark_variant(name, repeats))
            except Exception as exc:
                variants.append(
                    {
                        "variant": name,
                        "ok": False,
                        "error": type(exc).__name__,
                        "detail": str(exc)[:500],
                    }
                )
            emit({"type": "variant_report", **variants[-1]})
        rollback = self.rollback_benchmark()
        self.select_variant("baseline")
        return {
            "type": "report",
            "scope": "openi_optimization_matrix_memory_terminal",
            "manifest": self.manifest,
            "variants": variants,
            "rollback": rollback,
            "endpointing": "simulated_confirmation_not_real_vad",
            "fixture_source": "same_omni_teacher_forced_audio_for_all_variants",
            "cache_scope": "approved_complete_replies_only_cache_miss_is_failure",
            "speculation_scope": "candidate_at_known_input_end_rollback_separate",
        }
