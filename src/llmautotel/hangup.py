"""基于明确用户意愿的挂断工具，告别完成前允许插话撤销。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.frames.frames import (
    CancelFrame,
    DataFrame,
    EndFrame,
    Frame,
    FunctionCallResultProperties,
    InterruptionFrame,
    TTSSpeakFrame,
    VADUserStartedSpeakingFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.llm_service import FunctionCallParams

HANGUP_POLICY = (
    "只有用户最新发言明确要求结束通话，或明确表示不需要继续介绍时，才调用 hang_up。"
    "例如‘不用了，谢谢’、‘不需要了，再见’、‘请挂断’。必须引用最新用户原文为 evidence，"
    "确认其确实要结束后设置 confirmed=true，goodbye 填带祝福的简短礼貌告别，"
    "例如‘好的，祝您生活愉快，再见。’。工具会完整朗读 goodbye 后挂断，"
    "不要另外生成重复告别。\n"
    "用户意思不明确时继续回应问题、介绍相关价值并推进销售，不要问是否要挂断。"
    "价格抱怨、犹豫、沉默、单纯拒绝购买，都不能推断为结束通话。"
    "用户仍提出问题、要求继续或说不要挂断时不得调用工具。\n"
)


class ConversationActivity(Protocol):
    generation: int
    request_generation: int
    user_speaking: bool


@dataclass
class HangupAfterPlaybackFrame(DataFrame):
    """与告别音频顺序排队的可打断标记，不是不可撤销的结束帧。"""

    tool_call_id: str
    generation: int


@dataclass(frozen=True)
class _PendingHangup:
    tool_call_id: str
    generation: int
    evidence: str


class HangupController(FrameProcessor):
    """放在传输输出之后，验证告别完成和用户回合后才提交挂断。"""

    def __init__(
        self,
        context: LLMContext,
        activity: ConversationActivity,
        on_hangup: Callable[[], Awaitable[None]],
    ) -> None:
        super().__init__()
        self._context = context
        self._activity = activity
        self._on_hangup = on_hangup
        self._pending: _PendingHangup | None = None
        self._accepted: set[str] = set()

    def tools(self) -> ToolsSchema:
        return ToolsSchema(standard_tools=[FunctionSchema(
            name="hang_up",
            description=(
                "用户最新发言明确结束或明确不需要继续介绍时，完整播放带祝福的告别再挂断。"
                "含糊、嫌贵、犹豫或仍有问题时继续销售对话，不能猜测或询问是否挂断。"
            ),
            properties={
                "confirmed": {
                    "type": "boolean", "enum": [True],
                    "description": "最新用户发言明确要结束或不需要继续，不能凭猜测确认。",
                },
                "evidence": {
                    "type": "string", "description": "完整引用最新一条用户发言原文。",
                },
                "goodbye": {
                    "type": "string",
                    "description": "完整的简短结束回复，须包含‘祝您生活愉快’或类似祝福后道别。",
                },
            },
            required=["confirmed", "evidence", "goodbye"],
            handler=self.handle,
        )])

    def _latest_user_text(self) -> str | None:
        for message in reversed(self._context.get_messages()):
            if message.get("role") == "user":
                content = message.get("content")
                return content if isinstance(content, str) else None
        return None

    def _current(self, pending: _PendingHangup) -> bool:
        return (
            not self._activity.user_speaking
            and pending.generation == self._activity.generation
            and pending.generation == self._activity.request_generation
            and pending.evidence == self._latest_user_text()
        )

    async def handle(self, params: FunctionCallParams) -> None:
        evidence = params.arguments.get("evidence")
        goodbye = params.arguments.get("goodbye")
        candidate = _PendingHangup(
            params.tool_call_id, self._activity.request_generation,
            evidence if isinstance(evidence, str) else "",
        )
        valid = (
            params.arguments.get("confirmed") is True
            and bool(candidate.evidence.strip())
            and isinstance(goodbye, str) and bool(goodbye.strip())
            and self._current(candidate)
        )
        duplicate = params.tool_call_id in self._accepted or (
            self._pending is not None and self._current(self._pending)
        )
        if not valid or duplicate:
            await params.result_callback(
                {"accepted": False, "message": "未确认当前用户明确结束，或挂断已在处理。"},
                properties=FunctionCallResultProperties(run_llm=not duplicate),
            )
            return
        self._pending = candidate
        self._accepted.add(params.tool_call_id)
        await params.result_callback(
            {"accepted": True, "status": "告别播放后结束；用户再次讲话则撤销。"},
            properties=FunctionCallResultProperties(run_llm=False),
        )
        # 普通 DataFrame 经过官方 TTS 串行队列和音频输出队列，不能越过告别音频。
        await params.llm.push_frame(TTSSpeakFrame(text=goodbye.strip(), append_to_context=True))
        await params.llm.push_frame(HangupAfterPlaybackFrame(
            tool_call_id=candidate.tool_call_id, generation=candidate.generation,
        ))

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM:
            if isinstance(frame, (EndFrame, CancelFrame)):
                self._pending = None
            elif isinstance(frame, (InterruptionFrame, VADUserStartedSpeakingFrame)):
                # 导致本轮工具调用的 VAD 帧可能晚于 handler 到达输出端。
                # 共享输入状态判定是否有更新回合，不能让旧开始帧撤销新候选。
                if self._pending is not None and not self._current(self._pending):
                    self._pending = None
            elif isinstance(frame, HangupAfterPlaybackFrame):
                pending = self._pending
                if (
                    pending is not None and frame.tool_call_id == pending.tool_call_id
                    and frame.generation == pending.generation and self._current(pending)
                ):
                    self._pending = None
                    await self._on_hangup()
                return
        await self.push_frame(frame, direction)
