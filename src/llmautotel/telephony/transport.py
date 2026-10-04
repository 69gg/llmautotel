"""电话媒体接入现有 Pipecat 管线，复用官方排队、重采样与打断。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from pipecat.audio.utils import create_stream_resampler
from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    Frame,
    InputAudioRawFrame,
    InterruptionFrame,
    LLMAssistantPushAggregationFrame,
    LLMFullResponseEndFrame,
    OutputAudioRawFrame,
    StartFrame,
    TTSTextFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.transports.base_input import BaseInputTransport
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.transports.base_transport import BaseTransport, TransportParams

from llmautotel.hangup import HangupAfterPlaybackFrame
from llmautotel.telephony.base import MediaCallbacks, MediaDriver, TelephonyError

_PLAYBACK_CHECKPOINTS = (
    TTSTextFrame,
    LLMFullResponseEndFrame,
    LLMAssistantPushAggregationFrame,
    HangupAfterPlaybackFrame,
)
_PLAYBACK_GENERATION = "llmautotel.phone.playback_generation"


class PhoneInputTransport(BaseInputTransport):
    def __init__(self, owner: PhoneTransport, params: TransportParams) -> None:
        super().__init__(params)
        self._owner = owner
        # 输入优先低延迟：默认VHQ会缓冲上百毫秒，影响电话插话检测。
        self._resampler = create_stream_resampler(quality="QQ", clear_after_secs=None)

    async def start(self, frame: StartFrame) -> None:
        await super().start(frame)
        await self.set_transport_ready(frame)
        self._owner.start()

    async def receive(self, audio: bytes, sample_rate: int) -> None:
        if self._owner.closed or not self._owner.ready or not audio:
            return
        if len(audio) % 2 or sample_rate != self._owner.driver.sample_rate:
            await self._owner.fail("电话输入音频格式或采样率无效。")
            return
        pcm = await self._resampler.resample(audio, sample_rate, self.sample_rate)
        if pcm and not self._owner.closed:
            await self.push_audio_frame(InputAudioRawFrame(pcm, self.sample_rate, 1))

    async def cancel(self, frame: CancelFrame) -> None:
        await super().cancel(frame)
        await self._owner.close()


class PhoneOutputTransport(BaseOutputTransport):
    def __init__(self, owner: PhoneTransport, params: TransportParams) -> None:
        super().__init__(params)
        self._owner = owner
        self._next_send_at = 0.0
        self._flush_done = asyncio.Event()
        self._flush_done.set()

    async def start(self, frame: StartFrame) -> None:
        await super().start(frame)
        await self.set_transport_ready(frame)
        self._owner.output_ready.set()

    async def write_audio_frame(self, frame: OutputAudioRawFrame) -> bool:
        generation = self._owner.generation
        await self._flush_done.wait()
        if self._owner.closed or generation != self._owner.generation:
            return False
        if not self._owner.ready:
            return False
        try:
            now = asyncio.get_running_loop().time()
            delay = self._next_send_at - now
            if delay > 0:
                await asyncio.sleep(delay)
            if self._owner.closed or generation != self._owner.generation:
                return False
            await self._owner.driver.send_audio(frame.audio)
            self._next_send_at = asyncio.get_running_loop().time() + (
                len(frame.audio) / (self.sample_rate * 2)
            )
            return not self._owner.closed and generation == self._owner.generation
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._owner.fail("电话音频发送失败，请检查线路和媒体连接。")
            return False

    async def _handle_frame(self, frame: Frame) -> None:
        if isinstance(frame, _PLAYBACK_CHECKPOINTS):
            frame.metadata[_PLAYBACK_GENERATION] = self._owner.generation
            sender = self._media_senders.get(frame.transport_destination)
            if sender is not None:
                # Pipecat 1.12.0：TTSText 可早于 TTSStopped 到达。复用框架的尾部
                # flush，先把不足20ms的PCM/重采样滤波尾部排在句子提交之前。
                await sender._enqueue_flushed_audio_buffer()
        await super()._handle_frame(frame)

    async def write_transport_frame(self, frame: Frame) -> None:
        if not isinstance(frame, _PLAYBACK_CHECKPOINTS):
            return
        generation = frame.metadata.get(_PLAYBACK_GENERATION)
        if self._owner.closed or not self._owner.ready or generation != self._owner.generation:
            return
        try:
            await self._owner.driver.wait_played()
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise
            if generation != self._owner.generation:
                # 工具控制帧可能使 Pipecat 保留音频任务。flush 取消的是旧确认，
                # 不能连同保留的任务一起取消，否则下一轮音频也无法继续发送。
                return
            raise
        except Exception:
            await self._owner.fail("电话未确认语音播放完成，请检查媒体连接。")

    async def push_frame(
        self, frame: Frame, direction: FrameDirection = FrameDirection.DOWNSTREAM
    ) -> None:
        if (
            direction == FrameDirection.DOWNSTREAM
            and isinstance(frame, _PLAYBACK_CHECKPOINTS)
            and (
                self._owner.closed
                or not self._owner.ready
                or frame.metadata.get(_PLAYBACK_GENERATION) != self._owner.generation
            )
        ):
            # 对端迟到的旧播放确认不能提交被打断的句子或执行旧挂断工具。
            return
        await super().push_frame(frame, direction)

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        if isinstance(frame, InterruptionFrame):
            self._owner.generation += 1
            self._next_send_at = 0
            self._flush_done.clear()
            try:
                # 先取消正在写入/等确认的官方音频任务，释放 provider 发送锁。
                await super().process_frame(frame, direction)
                for sender in self._media_senders.values():
                    # 尚不足一块、未触发BotStartedSpeaking的音频也必须丢弃。
                    sender._clear_audio_buffer()
                    await sender._resampler.reset()
                await self._owner.driver.flush()
            except asyncio.CancelledError:
                raise
            except Exception:
                await self._owner.fail("电话音频打断失败，请检查媒体连接。")
            finally:
                self._flush_done.set()
            return
        await super().process_frame(frame, direction)

    async def stop(self, frame: EndFrame) -> None:
        try:
            await super().stop(frame)
        finally:
            await self._owner.close()

    async def cancel(self, frame: CancelFrame) -> None:
        try:
            await super().cancel(frame)
        finally:
            await self._owner.close()


class PhoneTransport(BaseTransport):
    """driver_factory 在构造时只创建配置快照，StartFrame 才建立外呼。"""

    def __init__(
        self,
        driver_factory: Callable[[MediaCallbacks], MediaDriver],
        *,
        number: str,
        call_id: str,
    ) -> None:
        super().__init__()
        for event in ("on_client_connected", "on_client_disconnected", "on_error"):
            self._register_event_handler(event, sync=True)
        self.number = number
        self.call_id = call_id
        self.closed = False
        self.ready = False
        self.generation = 0
        self.output_ready = asyncio.Event()
        self._dial_task: asyncio.Task[None] | None = None
        self._close_lock = asyncio.Lock()
        self._error_reported = False
        self._ended = False
        self.driver = driver_factory(
            MediaCallbacks(self._connected, self._receive, self._disconnected, self.fail)
        )
        params = TransportParams(
            audio_in_enabled=True,
            audio_in_sample_rate=16000,
            audio_out_enabled=True,
            audio_out_sample_rate=self.driver.sample_rate,
            audio_out_10ms_chunks=2,
            audio_out_end_silence_secs=0,
            audio_out_auto_silence=False,
        )
        self._input = PhoneInputTransport(self, params)
        self._output = PhoneOutputTransport(self, params)

    def input(self) -> PhoneInputTransport:
        return self._input

    def output(self) -> PhoneOutputTransport:
        return self._output

    def start(self) -> None:
        if self._dial_task is None and not self.closed:
            self._dial_task = asyncio.create_task(self._dial())

    async def _dial(self) -> None:
        try:
            await self.output_ready.wait()
            if not self.closed:
                await self.driver.start(self.number, self.call_id)
        except asyncio.CancelledError:
            raise
        except TelephonyError as exc:
            await self.fail(str(exc))
        except Exception:
            await self.fail("电话连接失败，请检查 provider 配置和线路。")

    async def _connected(self) -> None:
        if self.closed or self.ready or self._ended or self._error_reported:
            return
        self.ready = True
        await self._call_event_handler("on_client_connected")

    async def _receive(self, audio: bytes, sample_rate: int) -> None:
        await self._input.receive(audio, sample_rate)

    async def _disconnected(self, reason: str) -> None:
        if self.closed or self._ended:
            return
        self._ended = True
        self.ready = False
        self.generation += 1
        await self._call_event_handler("on_client_disconnected", reason)

    async def fail(self, message: str) -> None:
        if self.closed or self._error_reported:
            return
        self._error_reported = True
        self.ready = False
        self.generation += 1
        await self._call_event_handler("on_error", message)

    async def close(self) -> None:
        async with self._close_lock:
            if self.closed:
                return
            self.closed = True
            self.ready = False
            self.generation += 1
            self._output._flush_done.set()
            if self._dial_task is not None and self._dial_task is not asyncio.current_task():
                self._dial_task.cancel()
                await asyncio.gather(self._dial_task, return_exceptions=True)
            cleanup_failed = False
            try:
                try:
                    await self.driver.flush()
                except Exception:
                    cleanup_failed = True
                try:
                    await self.driver.hangup()
                except Exception:
                    cleanup_failed = True
            finally:
                try:
                    await self.driver.close()
                except Exception:
                    cleanup_failed = True
            if cleanup_failed:
                await self._call_event_handler(
                    "on_error", "电话清理未获确认，请检查电话服务器是否存在遗留通道。"
                )

    async def cleanup(self) -> None:
        await self.close()
        await super().cleanup()
