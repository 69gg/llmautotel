"""在语音与文字共同的出口去掉模型误复述的内部控制标记。"""

from __future__ import annotations

from collections.abc import AsyncIterator

from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    Frame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TTSSpeakFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.utils.text.base_text_aggregator import Aggregation, AggregationType
from pipecat.utils.text.pattern_pair_aggregator import MatchAction, PatternPairAggregator
from pipecat.utils.text.simple_text_aggregator import SimpleTextAggregator

from llmautotel.conversation import INTERRUPTED_BACKGROUND_LABEL


def chinese_speech_boundary(text: str, character: str, min_chars: int | None) -> bool:
    """中文句末立即释放；首段可选足够长的中文分句，避免拆开金额。"""
    return character in "。！？" or (
        min_chars is not None and character in "，；" and len(text.strip()) >= min_chars
    )


class FirstClauseTextAggregator(SimpleTextAggregator):
    """首段在足够长的中文分句处提前合成，其后沿用句级聚合。

    不按任意字数截断，也不把英文逗号、小数点或型号内部当作短句边界。
    完整中文句末无需等待下一词；其余语言复用 Pipecat 的分句器。
    """

    def __init__(self, min_chars: int) -> None:
        super().__init__(language="zh")
        self._min_chars = min_chars
        self._first_pending = True

    async def aggregate(self, text: str) -> AsyncIterator[Aggregation]:
        for character in text:
            async for result in super().aggregate(character):
                self._first_pending = False
                yield result
            if chinese_speech_boundary(
                self.text.text, character, self._min_chars if self._first_pending else None
            ):
                result = await self.flush()
                if result is not None:
                    self._first_pending = False
                    yield result

    async def flush(self) -> Aggregation | None:
        result = await super().flush()
        self._first_pending = True
        return result

    async def reset(self) -> None:
        await super().reset()
        self._first_pending = True

    async def handle_interruption(self) -> None:
        await super().handle_interruption()
        self._first_pending = True


def _speech_aggregator() -> PatternPairAggregator:
    """只移除应用保留的控制标签，普通括号仍然是可朗读正文。"""
    aggregator = PatternPairAggregator(aggregation_type=AggregationType.TOKEN)
    prefix = INTERRUPTED_BACKGROUND_LABEL.split("：", 1)[0]
    prefixes = (prefix, prefix.replace(" ", ""))
    for index, start in enumerate(prefixes):
        for opening in ("【", "["):
            for closing in ("】", "]"):
                aggregator.add_pattern(
                    type=f"internal-background-{index}-{opening}-{closing}",
                    start_pattern=opening + start[1:],
                    end_pattern=closing,
                    action=MatchAction.REMOVE,
                )
    return aggregator


async def _filter_chunk(aggregator: PatternPairAggregator, text: str) -> str:
    """逐字符释放安全正文，防止普通右括号干扰 1.12.0 的模式计数。"""
    parts: list[str] = []
    for character in text:
        parts.extend([part.text async for part in aggregator.aggregate(character)])
    return "".join(parts)


async def clean_speech_text(text: str) -> str:
    """完整文本（开场或工具回复）使用独立缓冲，不影响正在生成的流。"""
    aggregator = _speech_aggregator()
    parts = [await _filter_chunk(aggregator, text)]
    remaining = await aggregator.flush()
    if remaining is not None:
        parts.append(remaining.text)
    return "".join(parts)


class SpeechTextGuard(FrameProcessor):
    """在草稿捕捉和 TTS 前过滤，声音、记录和后续背景共享净化后的文字。"""

    def __init__(self) -> None:
        super().__init__()
        self._aggregator = _speech_aggregator()

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM:
            if isinstance(
                frame, (LLMFullResponseStartFrame, InterruptionFrame, EndFrame, CancelFrame)
            ):
                # 打断和收尾不把半截控制标签重新排回音频队列。
                await self._aggregator.reset()
            elif isinstance(frame, LLMTextFrame):
                frame.text = await _filter_chunk(self._aggregator, frame.text)
                if not frame.text:
                    return
            elif isinstance(frame, LLMFullResponseEndFrame):
                remaining = await self._aggregator.flush()
                if remaining is not None:
                    text_frame = LLMTextFrame(remaining.text)
                    text_frame.skip_tts = frame.skip_tts
                    await self.push_frame(text_frame, direction)
            elif isinstance(frame, TTSSpeakFrame):
                frame.text = await clean_speech_text(frame.text)
                if not frame.text.strip():
                    return
        await self.push_frame(frame, direction)
