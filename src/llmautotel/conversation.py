"""把被打断的生成内容保留为背景，完整播放的内容仍交给官方聚合器。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from pipecat.frames.frames import (
    AggregatedTextFrame,
    Frame,
    InterruptionFrame,
    LLMAssistantPushAggregationFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TTSSpeakFrame,
    TTSStartedFrame,
    TTSTextFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.utils.string import TextPartForConcatenation, concatenate_aggregated_text

_RESPONSE_ID_KEY = "llmautotel_response_id"
INTERRUPTED_BACKGROUND_LABEL = (
    "【AI 生成背景：上轮回复被打断，以下内容尚未完整播放给用户。"
    "仅供理解最新发言，不要自动续播，不要假定用户已经听到。】"
)


@dataclass(frozen=True)
class InterruptedResponse:
    """已生成内容和已完整输出内容分开保存，不承诺逐字播放对齐。"""

    generated_text: str
    played_text: str

    @property
    def background_text(self) -> str:
        """通常为已播放句子之后的草稿；无法匹配时保留有状态标签的全文。"""
        generated = self.generated_text.strip()
        played = self.played_text.strip()
        # 官方句聚合器可能在相邻句间补空格，原始 LLM chunk 未必有这些空格。
        # 顺序对齐非空白字符，只移除已完成输出的前缀，不猜测部分音频的进度。
        generated_index = 0
        for character in played:
            if character.isspace():
                continue
            while generated_index < len(generated) and generated[generated_index].isspace():
                generated_index += 1
            if generated_index == len(generated) or generated[generated_index] != character:
                return generated
            generated_index += 1
        return generated[generated_index:].strip()


class _ContextStage(FrameProcessor):
    """在管线队列内同步更新状态，避免异步观察事件晚于新请求。"""

    def __init__(self, on_frame: Callable[[Frame], None]) -> None:
        super().__init__()
        self._on_frame = on_frame

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM:
            self._on_frame(frame)
        await self.push_frame(frame, direction)


class InterruptedResponseContext:
    """共享三个薄处理器的会话状态，分别放在 LLM 前、TTS 前和输出后。"""

    def __init__(self, context: LLMContext) -> None:
        self.context = context
        # 打断代次可同时用于取消依赖旧用户回合的工具操作。
        self.generation = 0
        self.request_generation = 0
        self.user_speaking = False
        self.last_interrupted: InterruptedResponse | None = None
        self._response_id = 0
        self._active = False
        self._accept_text = False
        self._fixed_speech = False
        self._played_response_id: int | None = None
        self._generated_parts: list[str] = []
        self._played_parts: list[TextPartForConcatenation] = []
        self._pending: list[InterruptedResponse] = []
        self._input = _ContextStage(self._before_llm)
        self._generated = _ContextStage(self._after_llm)
        self._output = _ContextStage(self._after_output)

    def input(self) -> FrameProcessor:
        """放在用户聚合器之后、LLM 之前。"""
        return self._input

    def generated(self) -> FrameProcessor:
        """放在 LLM 之后、TTS 之前，保留生成稿直到实际输出完成。"""
        return self._generated

    def output(self) -> FrameProcessor:
        """放在传输输出之后、助手聚合器之前。"""
        return self._output

    def _before_llm(self, frame: Frame) -> None:
        if isinstance(frame, VADUserStartedSpeakingFrame):
            self.user_speaking = True
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            self.user_speaking = False
        elif isinstance(frame, InterruptionFrame):
            self.generation += 1
            if self._active:
                interrupted = InterruptedResponse(
                    generated_text="".join(self._generated_parts),
                    played_text=concatenate_aggregated_text(self._played_parts),
                )
                self.last_interrupted = interrupted
                if interrupted.background_text:
                    self._pending.append(interrupted)
            self._clear_response()
        elif isinstance(frame, LLMContextFrame):
            self.request_generation = self.generation
            self._commit_background(frame.context)

    def _start_response(self, *, fixed_speech: bool = False) -> None:
        self._response_id += 1
        self._active = True
        self._accept_text = not fixed_speech
        self._fixed_speech = fixed_speech
        self._played_response_id = None
        self._generated_parts = []
        self._played_parts = []

    def _after_llm(self, frame: Frame) -> None:
        if isinstance(frame, LLMFullResponseStartFrame):
            self._start_response()
            frame.metadata[_RESPONSE_ID_KEY] = self._response_id
        elif isinstance(frame, LLMTextFrame) and self._active and self._accept_text:
            self._generated_parts.append(frame.text)
        elif isinstance(frame, LLMFullResponseEndFrame) and self._active:
            # LLM 已生成完毕不代表 TTS 或播放完成；草稿仍保留到输出端结束。
            frame.metadata[_RESPONSE_ID_KEY] = self._response_id
            self._accept_text = False
        elif isinstance(frame, TTSSpeakFrame) and frame.append_to_context:
            self._start_response(fixed_speech=True)
            self._generated_parts.append(frame.text)

    def _after_output(self, frame: Frame) -> None:
        if isinstance(frame, LLMFullResponseStartFrame):
            self._played_response_id = frame.metadata.get(_RESPONSE_ID_KEY)
        elif isinstance(frame, TTSStartedFrame) and self._active and self._fixed_speech:
            self._played_response_id = self._response_id
        elif isinstance(frame, TTSTextFrame) and frame.append_to_context:
            if self._active and self._played_response_id == self._response_id:
                text = (
                    frame.raw_text
                    if isinstance(frame, AggregatedTextFrame) and frame.raw_text
                    else frame.text
                )
                self._played_parts.append(
                    TextPartForConcatenation(
                        text, includes_inter_part_spaces=frame.includes_inter_frame_spaces
                    )
                )
        elif isinstance(frame, LLMFullResponseEndFrame):
            if frame.metadata.get(_RESPONSE_ID_KEY) == self._response_id:
                self._clear_response()
        elif isinstance(frame, LLMAssistantPushAggregationFrame):
            if self._fixed_speech and self._played_response_id == self._response_id:
                self._clear_response()

    def _clear_response(self) -> None:
        self._active = False
        self._accept_text = False
        self._fixed_speech = False
        self._played_response_id = None
        self._generated_parts = []
        self._played_parts = []

    def _commit_background(self, context: LLMContext) -> None:
        if not self._pending:
            return
        messages = list(context.get_messages())
        # 当前用户发言保留为最后的请求；工具消息及已经播放的助手消息不改写。
        insertion = len(messages)
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            if isinstance(message, dict) and message.get("role") == "user":
                insertion = index
                break
        backgrounds = [
            {
                # 状态说明是内部管理信息，不能让模型当作助手说话风格模仿。
                "role": "system",
                "content": f"{INTERRUPTED_BACKGROUND_LABEL}\n{response.background_text}",
            }
            for response in self._pending
        ]
        context.set_messages(messages[:insertion] + backgrounds + messages[insertion:])
        self._pending.clear()
