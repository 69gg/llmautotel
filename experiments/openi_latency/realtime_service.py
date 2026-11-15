"""将已有业务管线部署到模型进程；所有模型调用在内存中完成。"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import struct
import uuid
import wave
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any, cast

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from openai import AsyncStream
from openai.types.audio import Transcription
from openai.types.chat import ChatCompletionChunk
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.utils.types import assert_given

from llmautotel.models import AppSettings
from llmautotel.providers import (
    CompatibleLLMService,
    CompatibleSTTService,
    CompatibleTTSService,
    ProviderServices,
)
from llmautotel.telephony.base import MediaCallbacks
from llmautotel.telephony.transport import PhoneTransport
from llmautotel.voice import VoiceCallbacks, VoiceSession

try:
    from .hybrid_api import bridge
except ImportError:
    from hybrid_api import bridge


class LocalStream:
    def __init__(self, iterator: AsyncIterator[ChatCompletionChunk]) -> None:
        self.iterator = iterator

    def __aiter__(self) -> AsyncIterator[ChatCompletionChunk]:
        return self.iterator

    async def close(self) -> None:
        await self.iterator.aclose()  # type: ignore[attr-defined]


def local_services(settings: AppSettings, backend: Any) -> ProviderServices:
    """保留现有上下文投影、工具处理及句级播放提交，只替换模型IO。"""

    class LocalSTT(CompatibleSTTService):
        async def _transcribe(self, audio: bytes) -> Transcription:
            with wave.open(io.BytesIO(audio), "rb") as source:
                if source.getframerate() != 16000 or source.getnchannels() != 1:
                    raise ValueError("invalid_asr_format")
                pcm = source.readframes(source.getnframes())
            text = ""
            async for item in bridge(lambda stop: backend.transcribe(pcm, stop)):
                text = item
            return Transcription(text=text)

    class LocalLLM(CompatibleLLMService):
        async def get_chat_completions(
            self, context: LLMContext
        ) -> AsyncStream[ChatCompletionChunk]:
            adapter = self.get_llm_adapter()
            params = self.build_chat_completion_params(
                adapter.get_llm_invocation_params(
                    context,
                    system_instruction=assert_given(self._settings.system_instruction),
                    convert_developer_to_user=not self.supports_developer_role,
                )
            )
            body = {"messages": params["messages"]}
            if isinstance(params.get("tools"), list):
                body["tools"] = params["tools"]
            if isinstance(params.get("tool_choice"), (dict, str)):
                body["tool_choice"] = params["tool_choice"]

            async def chunks() -> AsyncIterator[ChatCompletionChunk]:
                async for delta in bridge(lambda stop: backend.chat(body, stop)):
                    finish = delta.get("_finish")
                    yield ChatCompletionChunk(
                        id="chatcmpl-local",
                        created=0,
                        model=settings.llm.model,
                        object="chat.completion.chunk",
                        choices=[
                            {"index": 0, "delta": {} if finish else delta, "finish_reason": finish}
                        ],
                    )

            return cast(AsyncStream[ChatCompletionChunk], LocalStream(chunks()))

    class LocalTTS(CompatibleTTSService):
        async def _audio_chunks(self, text: str) -> AsyncGenerator[bytes, None]:
            async for pcm in bridge(lambda stop: backend.speech(text, stop)):
                yield pcm

    return ProviderServices(LocalSTT(settings.asr), LocalLLM(settings.llm), LocalTTS(settings.tts))


class GPUDriver:
    """输出按代次标记；句子只有在服务器实际输出确认后才提交上下文。"""

    input_sample_rate = 16000

    def __init__(self, callbacks: MediaCallbacks, websocket: WebSocket, sample_rate: int) -> None:
        self.callbacks, self.websocket, self.sample_rate = callbacks, websocket, sample_rate
        self.generation = 0
        self.pending: dict[str, tuple[int, asyncio.Future[None]]] = {}
        self.lock = asyncio.Lock()
        self.started = False
        self.closed = False

    async def send(self, event: dict[str, Any] | bytes) -> None:
        async with self.lock:
            if isinstance(event, bytes):
                await self.websocket.send_bytes(event)
            else:
                await self.websocket.send_json(event)

    async def start(self, number: str, call_id: str) -> None:
        await self.callbacks.on_ready()

    async def send_audio(self, audio: bytes) -> None:
        if not self.started:
            await self.send({"type": "audio-start", "generation": self.generation})
            self.started = True
        await self.send(struct.pack("!I", self.generation) + audio)

    async def wait_played(self) -> None:
        identity = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[identity] = self.generation, future
        try:
            await self.send({"type": "drain", "generation": self.generation, "id": identity})
            await asyncio.wait_for(future, 30)
        finally:
            self.pending.pop(identity, None)
            self.started = False

    def played(self, event: dict[str, Any]) -> None:
        pending = self.pending.get(event.get("id"))
        if pending is not None:
            generation, future = pending
            if generation == event.get("generation") == self.generation and not future.done():
                future.set_result(None)

    async def flush(self) -> None:
        self.generation += 1
        self.started = False
        for _, future in list(self.pending.values()):
            if not future.done():
                future.cancel()
        if not self.closed:
            await self.send({"type": "flush", "generation": self.generation})

    async def hangup(self) -> None:
        pass

    async def close(self) -> None:
        self.closed = True
        for _, future in list(self.pending.values()):
            if not future.done():
                future.cancel()


def attach_realtime(app: FastAPI, config: dict[str, Any], backend: Any) -> None:
    active = asyncio.Lock()

    @app.websocket("/v1/realtime")
    async def realtime(websocket: WebSocket) -> None:
        supplied = websocket.headers.get("authorization", "").removeprefix("Bearer ")
        if not config.get("token_sha256") or not hmac.compare_digest(
            hashlib.sha256(supplied.encode()).hexdigest(), config["token_sha256"]
        ):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        if active.locked():
            await websocket.send_json({"type": "error", "message": "GPU 已有一通会话。"})
            await websocket.close(code=1013)
            return
        async with active:
            session: VoiceSession | None = None
            task: asyncio.Task[str] | None = None
            driver: GPUDriver | None = None
            try:
                event = await asyncio.wait_for(websocket.receive_json(), 15)
                if event.get("type") != "start":
                    raise ValueError("requires_start")
                settings = AppSettings.model_validate(event["settings"])
                if settings.voice.pipeline != "local" or settings.tts.sample_rate != 24000:
                    raise ValueError("invalid_remote_configuration")
                for stage in ("asr", "llm", "tts"):
                    if getattr(settings, stage).api_key is not None:
                        raise ValueError("unexpected_provider_credential")
                # 独立服务的模型音色由镜像确定，不能静默接受不匹配模型。
                expected = {
                    "asr": config["asr_model"],
                    "llm": config["llm_model"],
                    "tts": config["service_tts_model"],
                }
                if any(
                    getattr(settings, stage).model != model for stage, model in expected.items()
                ):
                    raise ValueError("model_mismatch")
                if settings.tts.voice != config["service_voice"] or settings.tts.instructions:
                    raise ValueError("unsupported_voice")

                def factory(callbacks: MediaCallbacks) -> GPUDriver:
                    nonlocal driver
                    driver = GPUDriver(callbacks, websocket, settings.tts.sample_rate)
                    return driver

                transport = PhoneTransport(factory, number="", call_id=uuid.uuid4().hex)
                assert driver is not None

                async def state(value: str) -> None:
                    await driver.send({"type": "state", "state": value})

                async def message(role: str, text: str, interrupted: bool, timestamp: str) -> None:
                    await driver.send(
                        {
                            "type": "transcript",
                            "entry": {
                                "role": role,
                                "text": text,
                                "interrupted": interrupted,
                                "timestamp": timestamp,
                            },
                        }
                    )

                async def error(value: str) -> None:
                    await driver.send({"type": "error", "message": value})

                async def latency(entry: Any) -> None:
                    await driver.send({"type": "latency", "entry": entry.model_dump(mode="json")})

                session = VoiceSession(
                    None,
                    settings,
                    VoiceCallbacks(state, message, error, latency),
                    transport=transport,
                    services_factory=lambda current: local_services(current, backend),
                )

                async def run() -> str:
                    reason = await session.run()
                    await driver.send({"type": "ended", "reason": reason})
                    await websocket.close()
                    return reason

                task = asyncio.create_task(run())
                while True:
                    packet = await websocket.receive()
                    if packet["type"] == "websocket.disconnect":
                        break
                    if packet.get("bytes") is not None:
                        audio = packet["bytes"]
                        if len(audio) > 32000 or len(audio) % 2:
                            raise ValueError("invalid_audio_packet")
                        await driver.callbacks.on_audio(audio, driver.input_sample_rate)
                    elif packet.get("text") is not None:
                        import json

                        event = json.loads(packet["text"])
                        if event.get("type") != "played":
                            raise ValueError("unexpected_event")
                        driver.played(event)
            except (WebSocketDisconnect, RuntimeError):
                pass
            except Exception:
                try:
                    await websocket.send_json(
                        {"type": "error", "message": "GPU 语音管线请求无效或失败。"}
                    )
                except Exception:
                    pass
            finally:
                if driver is not None:
                    driver.closed = True
                if session is not None:
                    await session.stop("disconnected")
                if task is not None:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
