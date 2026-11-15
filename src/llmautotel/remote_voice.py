"""服务器仅转发 PCM；原业务管线在常驻 GPU 上运行，不回退到旧模型。"""

from __future__ import annotations

import asyncio
import json
import struct
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from pipecat.frames.frames import (
    EndFrame,
    Frame,
    InputAudioRawFrame,
    InterruptionFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    TTSTextFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.processors.frameworks.rtvi import RTVIObserverParams
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.workers.runner import WorkerRunner
from websockets.asyncio.client import ClientConnection, connect

from llmautotel.models import AppSettings, TurnLatency
from llmautotel.voice import VoiceCallbacks


def remote_url(settings: AppSettings) -> str:
    parsed = urlsplit(settings.llm.base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("GPU 管线需要有效的文本模型 API 根地址")
    return urlunsplit(
        (
            "wss" if parsed.scheme == "https" else "ws",
            parsed.netloc,
            parsed.path.rstrip("/") + "/realtime",
            "",
            "",
        )
    )


def remote_snapshot(settings: AppSettings) -> dict[str, Any]:
    """传产品资料和业务规则；所有模型密钥、线路凭据及号码不离开业务服务器。"""
    result = settings.model_dump(
        mode="json",
        exclude={
            "asr": {"api_key"},
            "llm": {"api_key"},
            "tts": {"api_key"},
            "telephony": True,
        },
    )
    result["voice"]["pipeline"] = "local"
    return result


class _RelayInput(FrameProcessor):
    def __init__(self, owner: RemoteVoiceSession) -> None:
        super().__init__()
        self.owner = owner

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame):
            if self.owner.initialized:
                if frame.sample_rate != 16000 or frame.num_channels != 1:
                    await self.owner.fail("GPU 管线输入音频格式无效。")
                else:
                    await self.owner.send(frame.audio)
            return
        await self.push_frame(frame, direction)


class _RelayOutput(FrameProcessor):
    def __init__(self, owner: RemoteVoiceSession) -> None:
        super().__init__()
        self.owner = owner

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, TTSTextFrame) and "gpu_drain" in frame.metadata:
            generation = frame.metadata["gpu_generation"]
            if generation == self.owner.generation:
                await self.owner.send(
                    {"type": "played", "id": frame.metadata["gpu_drain"], "generation": generation}
                )
        await self.push_frame(frame, direction)


class RemoteVoiceSession:
    """一通会话一条鉴权 WebSocket，音频沿用原传输的排队和打断。"""

    def __init__(
        self,
        connection: SmallWebRTCConnection | None,
        settings: AppSettings,
        callbacks: VoiceCallbacks,
        *,
        transport: BaseTransport | None = None,
    ) -> None:
        self.connection, self.settings, self.callbacks = connection, settings, callbacks
        self.transport = transport
        self.worker: PipelineWorker | None = None
        self.socket: ClientConnection | None = None
        self.reader: asyncio.Task[None] | None = None
        self.sender = asyncio.Lock()
        self.initialized = False
        self.generation = 0
        self.reason: str | None = None
        self.input = _RelayInput(self)

    async def send(self, message: bytes | dict[str, Any]) -> None:
        if self.socket is not None:
            async with self.sender:
                await self.socket.send(
                    message if isinstance(message, bytes) else json.dumps(message)
                )

    async def initialize(self, source: Any) -> None:
        if self.initialized or self.reason is not None:
            return
        await self.send({"type": "start", "settings": remote_snapshot(self.settings)})
        self.initialized = True

    async def fail(self, message: str) -> None:
        if self.reason is not None:
            return
        await self.callbacks.on_error(message)
        if self.worker is not None:
            await self.worker.rtvi.send_server_message({"type": "error", "message": message})
        await self.stop("model_error")

    async def receive(self) -> None:
        assert self.socket is not None
        try:
            async for packet in self.socket:
                if isinstance(packet, bytes):
                    if len(packet) < 6 or len(packet) % 2:
                        raise ValueError("invalid_audio")
                    generation = struct.unpack("!I", packet[:4])[0]
                    if generation == self.generation:
                        await self.input.push_frame(
                            TTSAudioRawFrame(packet[4:], self.settings.tts.sample_rate, 1)
                        )
                    continue
                event = json.loads(packet)
                kind = event["type"]
                if kind == "flush":
                    self.generation = event["generation"]
                    await self.input.push_frame(InterruptionFrame())
                elif kind == "drain":
                    if event["generation"] == self.generation:
                        await self.input.push_frame(TTSStoppedFrame())
                        frame = TTSTextFrame("", aggregated_by="sentence")
                        frame.metadata.update(gpu_drain=event["id"], gpu_generation=self.generation)
                        await self.input.push_frame(frame)
                elif kind == "audio-start":
                    if event["generation"] == self.generation:
                        await self.input.push_frame(TTSStartedFrame())
                elif kind == "state":
                    await self.callbacks.on_state(event["state"])
                    if self.worker is not None:
                        await self.worker.rtvi.send_server_message(event)
                elif kind == "transcript":
                    entry = event["entry"]
                    await self.callbacks.on_message(
                        entry["role"], entry["text"], entry["interrupted"], entry["timestamp"]
                    )
                    if self.worker is not None:
                        await self.worker.rtvi.send_server_message(event)
                elif kind == "latency":
                    entry = TurnLatency.model_validate(event["entry"])
                    if self.callbacks.on_latency is not None:
                        await self.callbacks.on_latency(entry)
                    if self.worker is not None:
                        await self.worker.rtvi.send_server_message(event)
                elif kind == "ended":
                    self.reason = event["reason"]
                    if self.worker is not None:
                        await self.worker.rtvi.send_server_message(
                            {"type": "call-ended", "reason": self.reason}
                        )
                        await self.worker.queue_frame(EndFrame(reason=self.reason))
                    return
                elif kind == "error":
                    await self.fail(event["message"])
                    return
            if self.reason is None:
                await self.fail("GPU 长连接已断开，请重新开始通话。")
        except asyncio.CancelledError:
            raise
        except Exception:
            await self.fail("GPU 语音管线连接或协议失败，请检查服务。")

    async def run(self) -> str:
        try:
            key = self.settings.llm.api_key
            self.socket = await connect(
                remote_url(self.settings),
                additional_headers={
                    "Authorization": "Bearer " + (key.get_secret_value() if key else "")
                },
                open_timeout=self.settings.llm.timeout_seconds,
                max_size=1024 * 1024,
                max_queue=16,
                compression=None,
                proxy=None,
            )
            if self.transport is None:
                if self.connection is None:
                    raise ValueError("missing_transport")
                self.transport = SmallWebRTCTransport(
                    self.connection,
                    params=TransportParams(audio_in_enabled=True, audio_out_enabled=True),
                )
            pipeline = Pipeline(
                [self.transport.input(), self.input, self.transport.output(), _RelayOutput(self)]
            )
            self.worker = PipelineWorker(
                pipeline,
                params=PipelineParams(
                    audio_in_sample_rate=16000, audio_out_sample_rate=self.settings.tts.sample_rate
                ),
                rtvi_observer_params=RTVIObserverParams(
                    user_llm_enabled=False, system_logs_enabled=False, metrics_enabled=False
                ),
                idle_timeout_secs=None,
            )
            if self.connection is not None:
                self.worker.rtvi.add_event_handler("on_client_ready", self.initialize)
            else:
                self.transport.add_event_handler("on_client_connected", self.initialize)

            @self.transport.event_handler("on_client_disconnected")
            async def disconnected(source: Any, detail: Any = None) -> None:
                await self.stop("disconnected")

            @self.transport.event_handler("on_error")
            async def transport_error(source: Any, message: str) -> None:
                await self.fail(message)

            self.reader = asyncio.create_task(self.receive())
            runner = WorkerRunner(handle_sigint=False, handle_sigterm=False)
            await runner.add_workers(self.worker)
            await runner.run()
        except asyncio.CancelledError:
            raise
        except Exception:
            await self.fail("GPU 语音管线启动失败，请检查地址、令牌和服务状态。")
        finally:
            if self.socket is not None:
                await self.socket.close()
            if self.reader is not None:
                self.reader.cancel()
                await asyncio.gather(self.reader, return_exceptions=True)
            if self.transport is not None:
                await self.transport.cleanup()
        return self.reason or "disconnected"

    async def stop(self, reason: str = "user_hangup") -> None:
        if self.reason is None:
            self.reason = reason
        if self.socket is not None:
            await self.socket.close()
        if self.worker is not None:
            await self.worker.cancel(reason=self.reason)
