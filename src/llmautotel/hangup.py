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
    LLMAssistantPushAggregationFrame,
    TTSSpeakFrame,
    VADUserStartedSpeakingFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.llm_service import FunctionCallParams

from llmautotel.speech import clean_speech_text

HANGUP_POLICY = (
    "区分拒绝配置的销售目标行动和直接结束通话，不要把行动拒绝猜测成要求挂断。"
    "首次明确拒绝目标行动（如‘不需要’、‘不买了’、‘不用了，谢谢’）调用 retain_once，"
    "引用最新用户原文为 evidence，reply 填一次简短、温和且与对方顾虑相关的挽留："
    "直接说明一项产品实际价值，结尾用‘考虑 + 配置目标行动 + 吗？’询问意向，"
    "不调查用途、场景或使用频率。"
    "每通最多一次挽留尝试，挽留被打断也不能重复；不要问是否要挂断。"
    "工具说明会告知本通是否已挽留；已挽留后的新一轮仍明确拒绝目标行动，"
    "才调用 hang_up 并设置 intent=purchase_refusal。\n"
    "用户直接要求结束（如‘挂了吧’、‘别再打扰’、‘结束通话’、‘请挂断’），"
    "立即调用 hang_up 并设置 intent=direct_exit，不挽留。"
    "必须引用最新用户原文为 evidence，确认意图明确后设置 confirmed=true，"
    "goodbye 填带祝福的简短礼貌告别，例如‘好的，祝您生活愉快，再见。’。"
    "工具会完整朗读 goodbye 后挂断，不要另外生成重复告别。\n"
    "用户意思不明确时继续回应问题、介绍相关价值并推进销售，不要问是否要挂断。"
    "价格抱怨、犹豫、沉默，都不能推断为明确拒绝目标行动或结束通话。"
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
class _ConfirmedUserTurn:
    tool_call_id: str
    generation: int
    evidence: str
    user_turn: int


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
        self._pending: _ConfirmedUserTurn | None = None
        self._accepted: set[str] = set()
        self._retention: _ConfirmedUserTurn | None = None

    def tools(self) -> ToolsSchema:
        retention_state = (
            "本通已使用一次挽留尝试，不能再挽留；根据最新发言回答问题，"
            "若新一轮仍明确拒绝目标行动则告别挂断。"
            if self._retention is not None else
            "本通尚未挽留；首次拒绝目标行动必须先调用 retain_once，不能直接挂断。"
        )
        return ToolsSchema(standard_tools=[FunctionSchema(
            name="hang_up",
            description=(
                "最新发言直接要求结束，或已挽留一次后新一轮仍明确拒绝目标行动时，"
                "完整播放带祝福的告别再挂断。"
                "含糊、嫌贵、犹豫或仍有问题时继续销售对话，不能猜测或询问是否挂断。"
                f"{retention_state}"
            ),
            properties={
                "intent": {
                    "type": "string", "enum": ["direct_exit", "purchase_refusal"],
                    "description": (
                        "direct_exit=直接要求结束；purchase_refusal=拒绝当前销售目标行动，"
                        "可为购买、订阅或其他配置目标。"
                    ),
                },
                "confirmed": {
                    "type": "boolean", "enum": [True],
                    "description": "最新用户意图明确，不能凭猜测确认。",
                },
                "evidence": {
                    "type": "string",
                    "description": (
                        "完整引用最新用户原文；若有‘当前用户发言’标记，"
                        "只引用标记下的用户文字。"
                    ),
                },
                "goodbye": {
                    "type": "string",
                    "description": "完整的简短结束回复，须包含‘祝您生活愉快’或类似祝福后道别。",
                },
            },
            required=["intent", "confirmed", "evidence", "goodbye"],
            handler=self.handle,
        ), FunctionSchema(
            name="retain_once",
            description=(
                "仅在首次明确拒绝目标行动时，播放一次简短温和的挽留，然后等待用户回应。"
                "直接要求结束通话、含糊犹豫、嫌贵或提出问题时不调用。"
                f"{retention_state}"
            ),
            properties={
                "evidence": {
                    "type": "string",
                    "description": (
                        "完整引用当前明确拒绝目标行动的原文，"
                        "不包含‘当前用户发言’管理标记。"
                    ),
                },
                "reply": {
                    "type": "string",
                    "description": (
                        "一次简短温和的挽留：介绍一项产品实际价值，"
                        "结尾用‘考虑 + 配置目标行动 + 吗？’询问意向。"
                        "不调查用途、场景或使用频率，不询问是否挂断。"
                        "只询问意向，不承诺代办没有实际工具支持的操作。"
                    ),
                },
            },
            required=["evidence", "reply"],
            handler=self.retain,
        )])

    def _user_turn(self) -> int:
        return sum(message.get("role") == "user" for message in self._context.get_messages())

    def _candidate(self, params: FunctionCallParams) -> _ConfirmedUserTurn:
        evidence = params.arguments.get("evidence")
        return _ConfirmedUserTurn(
            params.tool_call_id, self._activity.request_generation,
            evidence if isinstance(evidence, str) else "", self._user_turn(),
        )

    def _latest_user_text(self) -> str | None:
        for message in reversed(self._context.get_messages()):
            if message.get("role") == "user":
                content = message.get("content")
                return content if isinstance(content, str) else None
        return None

    def _current(self, pending: _ConfirmedUserTurn) -> bool:
        return (
            not self._activity.user_speaking
            and pending.generation == self._activity.generation
            and pending.generation == self._activity.request_generation
            and pending.evidence == self._latest_user_text()
            and pending.user_turn == self._user_turn()
        )

    async def retain(self, params: FunctionCallParams) -> None:
        candidate = self._candidate(params)
        reply = params.arguments.get("reply")
        reply = await clean_speech_text(reply) if isinstance(reply, str) else None
        valid = (
            bool(candidate.evidence.strip()) and self._current(candidate)
            and isinstance(reply, str) and bool(reply.strip())
        )
        pending_current = self._pending is not None and self._current(self._pending)
        duplicate = params.tool_call_id in self._accepted or (
            self._retention is not None and candidate.user_turn == self._retention.user_turn
        ) or pending_current
        if not valid or self._retention is not None or pending_current:
            await params.result_callback(
                {"accepted": False, "retention_used": self._retention is not None,
                 "message": "本通最多挽留一次；已挽留后按最新发言回应，明确再次拒绝才告别挂断。"},
                properties=FunctionCallResultProperties(run_llm=not duplicate),
            )
            return
        # 接受时就消耗唯一次数，取消 TTS 或插话都不会恢复挽留资格。
        self._retention = candidate
        self._accepted.add(params.tool_call_id)
        self._context.set_tools(self.tools())
        await params.result_callback(
            {"accepted": True, "retention_used": True,
             "status": "播放唯一一次挽留，然后等待用户；被打断也不能再次挽留。"},
            properties=FunctionCallResultProperties(run_llm=False),
        )
        await params.llm.push_frame(TTSSpeakFrame(text=reply.strip(), append_to_context=True))
        # 工具与 LLMFullResponseEndFrame 并发到达时，TTS 不一定补自动提交帧。
        # 显式使用官方聚合帧，沿相同播放队列在挽留音频完成后提交文字。
        await params.llm.push_frame(LLMAssistantPushAggregationFrame())

    async def handle(self, params: FunctionCallParams) -> None:
        goodbye = params.arguments.get("goodbye")
        goodbye = await clean_speech_text(goodbye) if isinstance(goodbye, str) else None
        candidate = self._candidate(params)
        intent = params.arguments.get("intent")
        purchase_refusal_allowed = (
            self._retention is not None and candidate.user_turn > self._retention.user_turn
        )
        valid = (
            params.arguments.get("confirmed") is True
            and (intent == "direct_exit" or (
                intent == "purchase_refusal" and purchase_refusal_allowed
            ))
            and bool(candidate.evidence.strip())
            and isinstance(goodbye, str) and bool(goodbye.strip())
            and self._current(candidate)
        )
        duplicate = params.tool_call_id in self._accepted or (
            self._pending is not None and self._current(self._pending)
        )
        if not valid or duplicate:
            same_retention_turn = (
                self._retention is not None and candidate.user_turn == self._retention.user_turn
            )
            await params.result_callback(
                {"accepted": False, "retention_used": self._retention is not None,
                 "message": (
                     "首次拒绝目标行动先调用 retain_once 挽留一次，不得挂断；"
                     "挽留后等待用户新一轮回应，再次明确拒绝才告别。"
                     if intent == "purchase_refusal" and not purchase_refusal_allowed else
                     "未确认当前用户明确结束，或挂断已在处理。"
                 )},
                properties=FunctionCallResultProperties(
                    run_llm=not duplicate and not same_retention_turn,
                ),
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
