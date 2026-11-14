"""DeepSeek 双向流式合成：按中文句段独立连接，保留可靠的播放检查点。"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit, urlunsplit

from pipecat.frames.frames import (
    AggregatedTextFrame,
    CancelFrame,
    EndFrame,
    ErrorFrame,
    InterruptionFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    TTSTextFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.tts_service import TextAggregationMode, TTSContext
from pipecat.utils.text.base_text_aggregator import AggregationType
from websockets.asyncio.client import ClientConnection, connect

from llmautotel.models import TTSSettings, VoiceSettings
from llmautotel.providers import CompatibleTTSService, ProviderFailure, _key
from llmautotel.speech import chinese_speech_boundary

# WebSocket 的 DEBUG 握手日志会带 Authorization，音频也不应写入 wire 日志。
_wire_logger = logging.getLogger(__name__ + ".wire")
_wire_logger.disabled = True


@dataclass
class _Segment:
    context_id: str
    socket: ClientConnection
    text: str = ""
    receiver: asyncio.Task[None] | None = None
    finish_sent: bool = False
    completed: bool = False
    closing: bool = False


class DeepSeekStreamingTTSService(CompatibleTTSService):
    """复用 Pipecat 的序列队列；仅在音频成功结束后提交对应句段文字。"""

    def __init__(self, settings: TTSSettings, voice: VoiceSettings | None = None) -> None:
        super().__init__(settings, text_aggregation_mode=TextAggregationMode.TOKEN)
        self._voice = voice or VoiceSettings()
        self._segment: _Segment | None = None
        self._first_segment = True
        self._failed = False
        self._epoch = 0
        self._closing: set[asyncio.Task[None]] = set()

    def _url(self) -> str:
        parsed = urlsplit(self._provider_settings.base_url)
        query = {"format": "pcm"}
        if self._provider_settings.voice:
            query["voice_id"] = self._provider_settings.voice
        return urlunsplit(
            (
                "wss" if parsed.scheme == "https" else "ws",
                parsed.netloc,
                parsed.path.rstrip("/") + "/tools/tts",
                urlencode(query),
                "",
            )
        )

    async def _connect(self) -> ClientConnection:
        key = _key(self._provider_settings)
        return await connect(
            self._url(),
            additional_headers={"Authorization": f"Bearer {key}"} if key else {},
            open_timeout=self._provider_settings.timeout_seconds,
            close_timeout=1,
            max_size=2**20,
            logger=_wire_logger,
        )

    async def _send(self, socket: ClientConnection, event: dict[str, str]) -> None:
        async with asyncio.timeout(self._provider_settings.timeout_seconds):
            await socket.send(json.dumps(event, ensure_ascii=False))

    async def _pcm(self, socket: ClientConnection) -> AsyncGenerator[bytes, None]:
        remainder = b""
        emitted = False
        while True:
            message = await asyncio.wait_for(socket.recv(), self._provider_settings.timeout_seconds)
            if isinstance(message, bytes):
                remainder += message
                aligned = len(remainder) & ~1
                if aligned:
                    emitted = True
                    yield remainder[:aligned]
                    remainder = remainder[aligned:]
                continue
            event = json.loads(message)
            if not isinstance(event, dict) or event.get("event") != "finish":
                raise ValueError("Invalid TTS event")
            if event.get("code") != "success" or remainder or not emitted:
                raise ValueError("Incomplete TTS synthesis")
            return

    async def _audio_chunks(self, text: str) -> AsyncGenerator[bytes, None]:
        """基准和独立合成也走 WebSocket，避免与实际通话使用不同协议。"""
        self._validate_text(text)
        socket = await self._connect()
        try:
            await self._send(socket, {"event": "text", "text": text})
            await self._send(socket, {"event": "finish_text"})
            async for chunk in self._pcm(socket):
                yield chunk
        finally:
            await socket.close()

    @staticmethod
    def _validate_text(text: str) -> None:
        # 按全部字符保守限制，避免超过服务的 10,000 计费字符或 64 KiB 上限。
        if len(text) > 10000 or len(text.encode("utf-8")) > 64 * 1024:
            raise ValueError("TTS input exceeds the protocol limit")

    def _owns(self, segment: _Segment) -> bool:
        return self._segment is segment and not self._failed

    async def _report_failure(self, error: Exception) -> None:
        if not self._failed:
            self._failed = True
            await self.push_error_frame(ErrorFrame(error=str(ProviderFailure("tts", error))))

    async def _receive(self, segment: _Segment) -> None:
        try:
            async for chunk in self._pcm(segment.socket):
                if self._segment is not segment or self._failed:
                    return
                if not self.audio_context_available(segment.context_id):
                    raise ValueError("TTS audio context expired")
                await self.append_to_audio_context(
                    segment.context_id,
                    TTSAudioRawFrame(
                        chunk,
                        24000,
                        1,
                        context_id=segment.context_id,
                    ),
                )
            if self._segment is segment:
                if not segment.finish_sent:
                    raise ValueError("TTS completed before text input ended")
                segment.completed = True
        except Exception as error:
            if self._segment is segment:
                await self._report_failure(error)

    async def _feed(
        self,
        text: str,
        append_to_context: bool,
        push_assistant_aggregation: bool | None,
    ) -> None:
        if self._failed or not text:
            return
        if self._segment is None:
            text = text.lstrip()
            if not text:
                return
            epoch = self._epoch
            socket = await self._connect()
            segment = _Segment(str(uuid.uuid4()), socket)
            if epoch != self._epoch or self._failed:
                self._schedule_close(segment)
                return
            self._segment = segment
            self._tts_contexts[segment.context_id] = TTSContext(
                append_to_context=append_to_context,
                push_assistant_aggregation=push_assistant_aggregation,
            )
            await self.create_audio_context(segment.context_id)
            if not self._owns(segment):
                return
            await self.start_ttfb_metrics()
            if not self._owns(segment):
                return
            await self.append_to_audio_context(
                segment.context_id, TTSStartedFrame(context_id=segment.context_id)
            )
            if not self._owns(segment):
                return
            segment.receiver = self.create_task(self._receive(segment))
        segment = self._segment
        if segment is None or self._failed:
            return
        segment.text += text
        self._validate_text(segment.text)
        await self._send(segment.socket, {"event": "text", "text": text})
        if self._segment is segment:
            self._refresh_audio_context(segment.context_id)

    async def _close(self, segment: _Segment) -> None:
        if segment.receiver is not None:
            if not segment.receiver.done():
                segment.receiver.cancel()
            await asyncio.gather(segment.receiver, return_exceptions=True)
        await segment.socket.close()

    def _schedule_close(self, segment: _Segment) -> None:
        if segment.closing:
            return
        segment.closing = True
        task = asyncio.create_task(self._close(segment))
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)

    def _abandon(self) -> None:
        # 先撤销所有权，再做网络清理；旧二进制消息没有回合 ID，不能复用连接。
        segment, self._segment = self._segment, None
        self._epoch += 1
        self._first_segment = True
        if segment is not None:
            if segment.receiver is not None:
                segment.receiver.cancel()
            self._schedule_close(segment)

    async def _finish_segment(self) -> None:
        segment = self._segment
        if segment is None:
            return
        try:
            segment.finish_sent = True
            await self._send(segment.socket, {"event": "finish_text"})
            if segment.receiver is not None:
                await segment.receiver
            if self._segment is not segment or self._failed or not segment.completed:
                return
            metadata = self._tts_contexts[segment.context_id]
            text = TTSTextFrame(segment.text, aggregated_by=AggregationType.SENTENCE)
            text.context_id = segment.context_id
            text.append_to_context = metadata.append_to_context
            text.will_be_spoken = True
            text.includes_inter_frame_spaces = False
            await self.append_to_audio_context(segment.context_id, text)
            await self.append_to_audio_context(
                segment.context_id, TTSStoppedFrame(context_id=segment.context_id)
            )
            await self.remove_audio_context(segment.context_id)
        finally:
            if self._segment is segment:
                self._segment = None
                self._first_segment = False
            self._schedule_close(segment)

    async def _push_tts_frames(
        self,
        src_frame: AggregatedTextFrame,
        includes_inter_frame_spaces: bool = False,
        append_tts_text_to_context: bool = True,
        push_assistant_aggregation: bool | None = False,
    ) -> None:
        if self._failed:
            return
        epoch = self._epoch
        self._processing_text = True
        try:
            if src_frame.aggregated_by != AggregationType.TOKEN:
                # 固定开场和工具告别使用一个检查点，不在中途提交或触发挂断。
                await self._feed(
                    src_frame.text, append_tts_text_to_context, push_assistant_aggregation
                )
                if epoch != self._epoch:
                    return
                await self._finish_segment()
                return
            pending = ""
            for character in src_frame.text:
                pending += character
                text = (self._segment.text if self._segment else "") + pending
                minimum = (
                    self._voice.first_clause_min_chars
                    if self._first_segment and self._voice.response_chunking == "first_clause"
                    else None
                )
                if chinese_speech_boundary(text, character, minimum):
                    await self._feed(
                        pending, append_tts_text_to_context, push_assistant_aggregation
                    )
                    if epoch != self._epoch:
                        return
                    pending = ""
                    await self._finish_segment()
                    if epoch != self._epoch:
                        return
            await self._feed(pending, append_tts_text_to_context, push_assistant_aggregation)
        except Exception as error:
            self._abandon()
            await self._report_failure(error)

    async def on_turn_context_completed(self) -> None:
        try:
            await self._finish_segment()
        except Exception as error:
            self._abandon()
            await self._report_failure(error)
        self._first_segment = True
        await super().on_turn_context_completed()

    async def _handle_interruption(
        self,
        frame: InterruptionFrame,
        direction: FrameDirection,
    ) -> None:
        self._abandon()
        await super()._handle_interruption(frame, direction)

    async def on_audio_context_interrupted(self, context_id: str) -> None:
        self._tts_contexts.pop(context_id, None)
        await super().on_audio_context_interrupted(context_id)

    async def stop(self, frame: EndFrame) -> None:
        await self.on_turn_context_completed()
        await super().stop(frame)

    async def cancel(self, frame: CancelFrame) -> None:
        self._abandon()
        await super().cancel(frame)
        # 基类停止队列消费者但保留其字典；取消后不再持有未播放 PCM 和文字。
        self._audio_contexts.clear()
        self._tts_contexts.clear()
        self._serialization_queue.reset()
        self.reset_active_audio_context()
        self._turn_context_id = None

    async def aclose(self) -> None:
        self._abandon()
        if self._closing:
            await asyncio.gather(*tuple(self._closing), return_exceptions=True)
        await super().aclose()
