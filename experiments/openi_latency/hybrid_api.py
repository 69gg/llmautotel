"""常驻 GPU 的兼容模型接口；复用实验模型，不承接或改写业务对话。"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import hmac
import io
import json
import os
import queue
import threading
import time
import uuid
import wave
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse


class ToolDecoder:
    """增量分离 Qwen 工具标签，绝不将工具 JSON 送入 TTS。"""

    opening = "<tool_call>"
    closing = "</tool_call>"

    def __init__(self, allowed: set[str]) -> None:
        self.allowed = allowed
        self.pending = ""
        self.in_tool = False
        self.calls = 0

    def feed(self, text: str, *, final: bool = False) -> Iterator[dict[str, Any]]:
        self.pending += text
        while self.pending:
            if self.in_tool:
                end = self.pending.find(self.closing)
                if end < 0:
                    if len(self.pending) > 16384 or final:
                        raise ValueError("incomplete_tool_call")
                    return
                call = json.loads(self.pending[:end])
                if call.get("name") not in self.allowed or not isinstance(
                    call.get("arguments"), dict
                ):
                    raise ValueError("invalid_tool_call")
                yield {
                    "tool_calls": [
                        {
                            "index": self.calls,
                            "id": "call_" + uuid.uuid4().hex,
                            "type": "function",
                            "function": {
                                "name": call["name"],
                                "arguments": json.dumps(call["arguments"], ensure_ascii=False),
                            },
                        }
                    ]
                }
                self.calls += 1
                self.pending = self.pending[end + len(self.closing) :]
                self.in_tool = False
                continue
            start = self.pending.find(self.opening)
            if start >= 0:
                if start:
                    yield {"content": self.pending[:start]}
                self.pending = self.pending[start + len(self.opening) :]
                self.in_tool = True
                continue
            keep = 0
            for size in range(1, min(len(self.opening), len(self.pending)) + 1):
                if self.opening.startswith(self.pending[-size:]):
                    keep = size
            if final and keep:
                raise ValueError("incomplete_tool_marker")
            count = len(self.pending) - keep
            if count:
                yield {"content": self.pending[:count]}
                self.pending = self.pending[count:]
            return
        if final and self.in_tool:
            raise ValueError("incomplete_tool_call")


@contextmanager
def acquired(lock: threading.Lock, cancelled: threading.Event) -> Iterator[None]:
    while not lock.acquire(timeout=0.1):
        if cancelled.is_set():
            raise InterruptedError("cancelled_before_inference")
    try:
        if cancelled.is_set():
            raise InterruptedError("cancelled_before_inference")
        yield
    finally:
        lock.release()


async def bridge(
    factory: Callable[[threading.Event], Iterator[Any]],
) -> AsyncIterator[Any]:
    """推理只在拥有生成器的线程结束；断连设置独立取消标记并丢弃迟到结果。"""
    cancelled = threading.Event()
    events: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=8)

    def put(kind: str, value: Any) -> None:
        while not cancelled.is_set():
            try:
                events.put((kind, value), timeout=0.1)
                return
            except queue.Full:
                pass

    def run() -> None:
        try:
            for value in factory(cancelled):
                if cancelled.is_set():
                    break
                put("item", value)
        except Exception as exc:
            put("error", exc)
        finally:
            put("done", None)

    threading.Thread(target=run, daemon=True).start()
    try:
        while True:
            try:
                kind, value = await asyncio.to_thread(events.get, True, 0.2)
            except queue.Empty:
                continue
            if kind == "done":
                return
            if kind == "error":
                raise RuntimeError("model_inference_failed") from value
            yield value
    finally:
        cancelled.set()


class ModelAPI:
    def __init__(self, models: Any, config: dict[str, Any]) -> None:
        self.models, self.config = models, config
        self.locks = {stage: threading.Lock() for stage in ("asr", "llm", "tts")}

    def transcribe(self, pcm: bytes, cancelled: threading.Event) -> Iterator[str]:
        with acquired(self.locks["asr"], cancelled):
            self.models.asr.reset()
            yield self.models.asr.feed(pcm, final=True)

    def speech(self, text: str, cancelled: threading.Event) -> Iterator[bytes]:
        if __package__:
            from .hybrid_worker import QwenPCM
        else:
            from hybrid_worker import QwenPCM
        with acquired(self.locks["tts"], cancelled):
            yield from QwenPCM(self.models.tts, self.config, cancelled).generate(text)

    def chat(self, body: dict[str, Any], cancelled: threading.Event) -> Iterator[dict[str, Any]]:
        from transformers import StoppingCriteria, StoppingCriteriaList, TextIteratorStreamer

        messages = copy.deepcopy(body["messages"])
        tools = body.get("tools", []) if body.get("tool_choice") != "none" else []
        for message in messages:
            for call in message.get("tool_calls", []):
                arguments = call.get("function", {}).get("arguments")
                if isinstance(arguments, str):
                    call["function"]["arguments"] = json.loads(arguments)
        decoder = ToolDecoder({tool["function"]["name"] for tool in tools})
        with acquired(self.locks["llm"], cancelled):
            prompt = self.models.tokenizer.apply_chat_template(
                messages,
                tools=tools or None,
                tokenize=False,
                add_generation_prompt=True,
                enable_thinking=False,
            )
            inputs = self.models.tokenizer([prompt], return_tensors="pt").to("cuda:0")
            if inputs["input_ids"].shape[-1] > self.config["service_max_input_tokens"]:
                raise ValueError("context_budget_exceeded")
            deadline = time.monotonic() + self.config["generation_timeout_seconds"]
            generation_stop = threading.Event()

            class Stop(StoppingCriteria):
                def __call__(self, input_ids: Any, scores: Any, **kwargs: Any) -> bool:
                    return (
                        cancelled.is_set()
                        or generation_stop.is_set()
                        or time.monotonic() >= deadline
                    )

            streamer = TextIteratorStreamer(
                self.models.tokenizer,
                skip_prompt=True,
                skip_special_tokens=True,
                timeout=self.config["generation_timeout_seconds"],
            )
            errors: list[Exception] = []

            def generate() -> None:
                try:
                    with self.models.torch.inference_mode():
                        self.models.llm.generate(
                            **inputs,
                            streamer=streamer,
                            do_sample=False,
                            max_new_tokens=self.config["service_max_output_tokens"],
                            stopping_criteria=StoppingCriteriaList([Stop()]),
                        )
                except Exception as exc:
                    errors.append(exc)
                    streamer.end()

            worker = threading.Thread(target=generate, daemon=True)
            worker.start()
            try:
                for text in streamer:
                    if cancelled.is_set():
                        return
                    yield from decoder.feed(text)
                if errors:
                    raise RuntimeError("llm_generation_failed") from errors[0]
                if time.monotonic() >= deadline:
                    raise TimeoutError("llm_generation_deadline")
                yield from decoder.feed("", final=True)
                yield {"_finish": "tool_calls" if decoder.calls else "stop"}
            finally:
                generation_stop.set()
                worker.join(self.config["generation_timeout_seconds"] + 2)
                if worker.is_alive():
                    # 不释放模型锁供新请求复用仍在生成的同一模型。
                    os._exit(70)


def wav_pcm(data: bytes, max_seconds: float) -> bytes:
    try:
        with wave.open(io.BytesIO(data), "rb") as source:
            if (source.getnchannels(), source.getsampwidth(), source.getframerate()) != (
                1,
                2,
                16000,
            ):
                raise ValueError("requires_mono_16bit_16000hz_wav")
            frames = source.getnframes()
            if not 0 < frames <= max_seconds * 16000:
                raise ValueError("invalid_audio_duration")
            pcm = source.readframes(frames)
            if len(pcm) != frames * 2:
                raise ValueError("truncated_wav")
            return pcm
    except (wave.Error, EOFError) as exc:
        raise ValueError("invalid_wav") from exc


def create_app(config: dict[str, Any], backend: Any) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        process = getattr(getattr(getattr(backend, "models", None), "asr", None), "process", None)
        if process is not None and process.poll() is None:
            process.terminate()
            await asyncio.to_thread(process.wait, 10)

    app = FastAPI(title="LLMAutoTel GPU models", lifespan=lifespan, docs_url=None, redoc_url=None)
    ids = {
        "asr": config["asr_model"],
        "llm": config["llm_model"],
        "tts": config["service_tts_model"],
    }

    @app.middleware("http")
    async def authenticate(request: Request, call_next: Any) -> Any:
        if request.url.path != "/health":
            supplied = request.headers.get("authorization", "").removeprefix("Bearer ")
            digest = hashlib.sha256(supplied.encode()).hexdigest()
            if not config.get("token_sha256") or not hmac.compare_digest(
                digest, config["token_sha256"]
            ):
                return JSONResponse({"error": {"message": "unauthorized"}}, status_code=401)
        return await call_next(request)

    async def payload(request: Request) -> bytes:
        result = bytearray()
        async for chunk in request.stream():
            result.extend(chunk)
            if len(result) > config["service_max_request_bytes"]:
                raise HTTPException(413, "request_too_large")
        return bytes(result)

    async def json_body(request: Request) -> dict[str, Any]:
        try:
            body = json.loads(await payload(request))
            if not isinstance(body, dict):
                raise ValueError
            return body
        except (ValueError, TypeError):
            raise HTTPException(422, "invalid_json_body") from None

    def model_check(value: Any, stage: str) -> None:
        if value != ids[stage]:
            raise HTTPException(404, "model_not_available")

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ready", "service": "llmautotel-gpu-models", "models": ids}

    @app.get("/v1/models")
    async def catalog() -> dict[str, Any]:
        return {
            "object": "list",
            "data": [
                {"id": value, "object": "model", "owned_by": "local"} for value in ids.values()
            ],
        }

    @app.post("/v1/audio/transcriptions")
    async def transcribe(request: Request) -> dict[str, str]:
        content_type = request.headers.get("content-type", "")
        if not content_type.startswith("multipart/form-data"):
            raise HTTPException(415, "requires_multipart_wav")
        # 完全在内存解析，不使用 UploadFile 的磁盘溢出缓存。
        form = BytesParser(policy=policy.default).parsebytes(
            b"Content-Type: "
            + content_type.encode()
            + b"\r\nMIME-Version: 1.0\r\n\r\n"
            + await payload(request)
        )
        fields = {
            part.get_param("name", header="content-disposition"): part.get_payload(decode=True)
            for part in form.iter_parts()
        }
        model_check(fields.get("model", b"").decode(errors="replace"), "asr")
        try:
            pcm = wav_pcm(fields.get("file", b""), config["max_audio_seconds"])
        except ValueError:
            raise HTTPException(422, "requires_valid_mono_16bit_16000hz_wav") from None
        result = ""
        async for text in bridge(lambda stop: backend.transcribe(pcm, stop)):
            result = text
        return {"text": result}

    @app.post("/v1/audio/speech")
    async def speech(request: Request) -> StreamingResponse:
        body = await json_body(request)
        model_check(body.get("model"), "tts")
        text = body.get("input")
        if (
            not isinstance(text, str)
            or not text.strip()
            or len(text) > config["service_max_tts_chars"]
            or body.get("response_format", "pcm") != "pcm"
            or body.get("voice") != config["service_voice"]
            or body.get("instructions")
        ):
            raise HTTPException(422, "requires_pcm_reference_voice_and_nonempty_text")
        return StreamingResponse(
            bridge(lambda stop: backend.speech(text, stop)),
            media_type="audio/pcm",
            headers={"X-Accel-Buffering": "no"},
        )

    @app.post("/v1/chat/completions")
    async def chat(request: Request) -> Any:
        body = await json_body(request)
        model_check(body.get("model"), "llm")
        messages = body.get("messages")
        if (
            not isinstance(messages, list)
            or not messages
            or not all(
                isinstance(m, dict) and m.get("role") in {"system", "user", "assistant", "tool"}
                for m in messages
            )
            or len(messages) > config["service_max_messages"]
        ):
            raise HTTPException(422, "invalid_messages")
        tools = body.get("tools", [])
        if not isinstance(tools, list) or any(
            not isinstance(t, dict)
            or t.get("type") != "function"
            or not isinstance(t.get("function"), dict)
            or not isinstance(t["function"].get("name"), str)
            for t in tools
        ):
            raise HTTPException(422, "invalid_tools")
        completion_id, created = "chatcmpl-" + uuid.uuid4().hex, int(time.time())

        def chunk(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
            return {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": ids["llm"],
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }

        async def stream() -> AsyncIterator[str]:
            yield "data: " + json.dumps(chunk({"role": "assistant"})) + "\n\n"
            async for delta in bridge(lambda stop: backend.chat(body, stop)):
                finish = delta.get("_finish")
                item = chunk({} if finish else delta, finish)
                yield "data: " + json.dumps(item, ensure_ascii=False) + "\n\n"
            yield "data: [DONE]\n\n"

        if body.get("stream", False):
            return StreamingResponse(
                stream(),
                media_type="text/event-stream",
                headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
            )
        message: dict[str, Any] = {"role": "assistant", "content": ""}
        finish = "stop"
        async for delta in bridge(lambda stop: backend.chat(body, stop)):
            message["content"] += delta.get("content", "")
            if "tool_calls" in delta:
                calls = message.setdefault("tool_calls", [])
                calls.extend(
                    {k: v for k, v in call.items() if k != "index"} for call in delta["tool_calls"]
                )
            finish = delta.get("_finish", finish)
        return {
            "id": completion_id,
            "object": "chat.completion",
            "created": created,
            "model": ids["llm"],
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        }

    return app


def main() -> None:
    import uvicorn
    from hybrid_worker import HybridModels

    config = json.loads(Path(__file__).with_name("settings.run.json").read_text())
    models = HybridModels(config)
    uvicorn.run(
        create_app(config, ModelAPI(models, config)),
        host="0.0.0.0",
        port=int(os.environ["OPENI_SELF_PORT"]),
        access_log=False,
    )


if __name__ == "__main__":
    main()
