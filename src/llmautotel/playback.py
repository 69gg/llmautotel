"""在业务服务器流式调整播放语速，保持音调与原始采样率。"""

from fractions import Fraction

import av
from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    Frame,
    InterruptionFrame,
    TTSAudioRawFrame,
    TTSStoppedFrame,
    TTSTextFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


class SpeechPlaybackSpeed(FrameProcessor):
    def __init__(self, speed: float) -> None:
        super().__init__()
        self._speed = speed
        self._graph: av.filter.Graph | None = None
        self._rate = 0
        self._channels = 0
        self._samples = 0

    def reset(self) -> None:
        """打断时丢弃滤镜内部余音，避免旧回复复活。"""
        self._graph = None
        self._samples = 0

    def convert(self, frame: TTSAudioRawFrame | None) -> list[TTSAudioRawFrame]:
        if self._speed == 1:
            return [frame] if frame is not None else []
        if frame is not None:
            if self._graph is None:
                self._rate, self._channels = frame.sample_rate, frame.num_channels
                layout = "mono" if self._channels == 1 else "stereo"
                self._graph = av.filter.Graph()
                self._graph.link_nodes(
                    self._graph.add_abuffer(
                        sample_rate=self._rate,
                        format="s16",
                        layout=layout,
                        time_base=Fraction(1, self._rate),
                    ),
                    self._graph.add("atempo", str(self._speed)),
                    self._graph.add("aformat", "sample_fmts=s16"),
                    self._graph.add("abuffersink"),
                )
                self._graph.configure()
            audio = av.AudioFrame(
                format="s16",
                layout="mono" if self._channels == 1 else "stereo",
                samples=len(frame.audio) // (2 * self._channels),
            )
            audio.planes[0].update(frame.audio)
            audio.sample_rate = self._rate
            audio.time_base = Fraction(1, self._rate)
            audio.pts = self._samples
            self._samples += audio.samples
        else:
            audio = None
        if self._graph is None:
            return []
        self._graph.push(audio)
        result: list[TTSAudioRawFrame] = []
        while True:
            try:
                converted = self._graph.pull()
            except (av.BlockingIOError, av.EOFError):
                break
            size = converted.samples * self._channels * 2
            result.append(
                TTSAudioRawFrame(
                    bytes(converted.planes[0])[:size],
                    self._rate,
                    self._channels,
                )
            )
        if frame is None:
            self.reset()
        return result

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if direction != FrameDirection.DOWNSTREAM:
            await self.push_frame(frame, direction)
            return
        if isinstance(frame, (InterruptionFrame, CancelFrame)):
            self.reset()
        elif isinstance(frame, TTSAudioRawFrame):
            for converted in self.convert(frame):
                await self.push_frame(converted, direction)
            return
        elif isinstance(frame, (TTSStoppedFrame, TTSTextFrame, EndFrame)):
            # 必须先排完滤镜尾音，再让播放确认或自动挂断通过。
            for converted in self.convert(None):
                await self.push_frame(converted, direction)
        await self.push_frame(frame, direction)
