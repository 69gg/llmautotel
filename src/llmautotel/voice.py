"""每通会话独立的 Pipecat 语音管线和已播放文字提交。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    EndFrame,
    Frame,
    LLMRunFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.observers.error_observer import ErrorEvent, ErrorObserver
from pipecat.observers.speaking_observer import SpeakingObserver, SpeechEvent, SpeechEventKind
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    AssistantTurnStoppedMessage,
    LLMContextAggregatorPair,
    LLMUserAggregator,
    LLMUserAggregatorParams,
    UserTurnStoppedMessage,
)
from pipecat.processors.frameworks.rtvi import RTVIObserverParams, RTVIProcessor
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.turns.types import ProcessFrameResult
from pipecat.turns.user_start import BaseUserTurnStartStrategy
from pipecat.turns.user_stop import BaseUserTurnStopStrategy, SpeechTimeoutUserTurnStopStrategy
from pipecat.turns.user_turn_controller import UserTurnController
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.workers.runner import WorkerRunner

from llmautotel.conversation import InterruptedResponseContext
from llmautotel.hangup import HANGUP_POLICY, HangupController
from llmautotel.models import AppSettings
from llmautotel.providers import ProviderServices, create_services
from llmautotel.speech import SpeechTextGuard

TranscriptRole = Literal["user", "assistant"]


@dataclass
class VoiceCallbacks:
    """存储和会话管理由调用方负责，语音管线只提交事件。"""

    on_state: Callable[[str], Awaitable[None]]
    on_message: Callable[[TranscriptRole, str, bool, str], Awaitable[None]]
    on_error: Callable[[str], Awaitable[None]]


class FinalTranscriptUserTurnStopStrategy(SpeechTimeoutUserTurnStopStrategy):
    """保留官方停顿计时，等待所有分段 HTTP ASR 最终结果（包括空结果）。"""

    def __init__(self) -> None:
        # VAD 已等待配置停顿，不再叠加官方默认 0.6 秒策略等待。
        super().__init__(user_speech_timeout=0.0, wait_for_transcript=True)
        self._pending_segments = 0

    async def process_frame(self, frame: Frame) -> ProcessFrameResult:
        # CompatibleSTTService 每个 VAD stop 恰有一个 final，且按 FIFO 返回。
        # 续说时保留计数，避免上一段迟到 final 提前结束尚在识别的后一段。
        if isinstance(frame, VADUserStoppedSpeakingFrame):
            self._pending_segments += 1
        elif isinstance(frame, TranscriptionFrame) and frame.finalized:
            self._pending_segments = max(0, self._pending_segments - 1)
        return await super().process_frame(frame)

    async def _maybe_trigger_user_turn_stopped(self) -> None:
        # Pipecat 1.12.0 私有状态：P99 safety timer 不等于 HTTP final 到达。
        if self._pending_segments or not self._transcript_finalized:
            return
        if self._text:
            await super()._maybe_trigger_user_turn_stopped()
        elif (
            not self._vad_user_speaking
            and self._user_speech_wait_done
            and self._stt_wait_done
        ):
            # 官方 wait_for_transcript 会阻止空文本收尾；真实 empty final 可结束。
            await self.trigger_user_turn_stopped()


def sales_prompt(settings: AppSettings) -> str:
    """让销售目标建立在已配置事实之上，并约束语音回复长度。"""
    sales = settings.sales
    opening_rule = (
        "系统会按配置尝试播放开场；优先回应最新用户发言，只以对话上下文确认已说内容，"
        "不要假定开场已经完整播放。"
        if sales.opening.strip()
        else "仅在对话中尚无用户发言时主动做一个简短开场，"
        "直接介绍一项产品价值并围绕配置目标询问用户意向；"
        "已有用户发言时直接回应用户，不补开场。"
    )
    return (
        "你是一名通过语音交流的 AI 销售助手。使用简体中文，自然、礼貌、简洁地对话。\n"
        "一次只说一到两句，优先回应用户最新的话。不要输出 Markdown、列表或舞台指示。\n"
        "对话采用直接推销：先回答当前问题，再介绍一项资料支持的产品价值，"
        "最后以‘考虑 + 目标行动 + 吗？’的意向问句收尾，"
        "例如‘考虑购买吗？’、‘考虑订阅吗？’、‘考虑预约演示吗？’。"
        "行动与配置目标一致，例如购买、订阅或预约演示，不预设具体产品或行动。"
        "只询问用户意向，下一步以配置资料为准；"
        "不承诺代用户执行没有实际工具支持的开通、购买、预约等操作。"
        "不要调查用户的用途、使用场景、频率、职业或痛点；"
        "不要问‘您平时用它干什么’、‘主要用来学习还是工作’或类似需求调查问题。"
        "用户主动提供需求时可以据此介绍相关价值，不再追问用途。"
        "拒绝目标行动与直接结束按下方工具规则处理，告别不追加推销。\n"
        "最后一条用户消息是本轮的当前请求，前文只用于理解指代和必要背景。"
        "用户插话、换问题或修正需求后，必须依据最新消息重新回答，"
        "不要续答或补讲之前被打断的话题。\n"
        "连续出现多条用户消息时，不要按顺序补答旧问题；直接回答最后一条，"
        "除非用户明确要求继续旧话题或同时回答。先给当前问题的直接答案，"
        "再补充必要信息，不重复开场或无关卖点。\n"
        "上下文中标注的 AI 生成背景是被打断、尚未完整播放的草稿，"
        "可用于理解用户指代，但不能假定用户已听到，也不要自动续讲。"
        "背景标签及管理说明仅供内部使用，绝不能朗读、复述或出现在回复正文中。\n"
        "只依据提供的产品资料介绍事实，不编造价格、优惠、保障或购买结果。\n"
        "首次明确拒绝目标行动时最多温和挽留一次，不持续施压；"
        "再次明确拒绝目标行动或直接要求结束时尊重其意愿，告别后挂断。\n"
        f"{HANGUP_POLICY}"
        f"{opening_rule}\n\n"
        f"销售目标：\n{sales.goal}\n\n"
        f"产品资料：\n{sales.product_info}\n\n"
        f"话术要求：\n{sales.instructions}"
    )


class VoiceSession:
    """一通会话对应一份上下文、一组模型和一个管线，传输可注入。"""

    def __init__(
        self,
        connection: SmallWebRTCConnection | None,
        settings: AppSettings,
        callbacks: VoiceCallbacks,
        *,
        transport: BaseTransport | None = None,
    ) -> None:
        self._connection = connection
        self._transport = transport
        self._settings = settings.model_copy(deep=True)
        self._callbacks = callbacks
        self._worker: PipelineWorker | None = None
        self._user_aggregator: LLMUserAggregator | None = None
        self._services: ProviderServices | None = None
        self._conversation: InterruptedResponseContext | None = None
        self._reason: str | None = None
        self._state: str | None = None
        self._opened = False
        self._user_spoken = False
        self._error_reported = False

    async def _state_changed(self, state: str) -> None:
        if self._state == state or self._reason is not None:
            return
        self._state = state
        await self._callbacks.on_state(state)
        if self._worker is not None:
            await self._worker.rtvi.send_server_message({"type": "state", "state": state})

    async def _commit_message(
        self, role: TranscriptRole, text: str, interrupted: bool, timestamp: str
    ) -> None:
        if not text.strip() and not interrupted:
            return
        # 在向网页发送提交事件之前落库。空文本中断也保留，方便解释半句被舍弃。
        await self._callbacks.on_message(role, text, interrupted, timestamp)
        if self._worker is not None:
            await self._worker.rtvi.send_server_message(
                {
                    "type": "transcript",
                    "entry": {
                        "role": role,
                        "text": text,
                        "timestamp": timestamp,
                        "interrupted": interrupted,
                    },
                }
            )

    async def _fail(self, message: str, *, reason: str = "model_error") -> None:
        if self._error_reported or self._reason is not None:
            return
        self._error_reported = True
        self._reason = reason
        if self._user_aggregator is not None:
            # Pipecat 1.12.0 cancel 会运行用户缓存；故障时丢弃未完整识别回合。
            await self._user_aggregator.reset()
        await self._callbacks.on_error(message)
        if self._worker is not None:
            await self._worker.rtvi.send_server_message({"type": "error", "message": message})
            await self._worker.cancel(reason=reason)

    async def _open_conversation(self, source: RTVIProcessor | BaseTransport) -> None:
        if self._opened or self._reason is not None:
            return
        self._opened = True
        # ready 和音频事件可能交错；用户已经说话时直接响应用户，避免随后插入开场。
        if self._user_spoken or self._reason is not None or self._worker is None:
            return
        await self._state_changed("listening")
        if self._user_spoken or self._reason is not None:
            return
        opening = self._settings.sales.opening.strip()
        if opening:
            await self._worker.queue_frame(TTSSpeakFrame(text=opening, append_to_context=True))
        else:
            await self._worker.queue_frame(LLMRunFrame())

    async def _hang_up_after_goodbye(self) -> None:
        if self._reason is not None or self._worker is None:
            return
        self._reason = "ai_hangup"
        # 先让会话管理器锁定原因，避免客户端断连的 REST 收尾覆盖 AI 正常结束。
        await self._callbacks.on_state("ending")
        await self._worker.rtvi.send_server_message({"type": "call-ended", "reason": "ai_hangup"})
        await self._worker.queue_frame(EndFrame(reason="ai_hangup"))

    async def run(self) -> str:
        """运行到挂断、断开或模型失败，始终释放模型客户端。"""
        if self._reason is not None:
            return self._reason
        try:
            self._services = create_services(self._settings)
            if self._transport is not None:
                transport = self._transport
            else:
                if self._connection is None:
                    raise ValueError("语音会话缺少传输连接")
                transport = SmallWebRTCTransport(
                    webrtc_connection=self._connection,
                    params=TransportParams(audio_in_enabled=True, audio_out_enabled=True),
                )
            context = LLMContext([{"role": "system", "content": sales_prompt(self._settings)}])
            conversation = InterruptedResponseContext(context)
            self._conversation = conversation
            hangup = HangupController(context, conversation, self._hang_up_after_goodbye)
            context.set_tools(hangup.tools())
            user, assistant = LLMContextAggregatorPair(
                context,
                user_params=LLMUserAggregatorParams(
                    vad_analyzer=SileroVADAnalyzer(
                        params=VADParams(
                            start_secs=self._settings.voice.vad_start_seconds,
                            stop_secs=self._settings.voice.vad_stop_seconds,
                            confidence=self._settings.voice.vad_confidence,
                        )
                    ),
                    user_turn_strategies=UserTurnStrategies(
                        stop=[FinalTranscriptUserTurnStopStrategy()]
                    ),
                    # 分段 ASR 可比官方默认 5 秒回合 watchdog 更慢。
                    user_turn_stop_timeout=(
                        self._settings.asr.timeout_seconds
                        + self._settings.voice.vad_stop_seconds
                    ),
                    # emptyfinal 同样结束识别回合，噪声 / 空转写不触发新模型回复。
                    empty_user_turn=None,
                ),
            )
            self._user_aggregator = user
            errors = ErrorObserver()
            speaking = SpeakingObserver()
            pipeline = Pipeline(
                [
                    transport.input(),
                    self._services.stt,
                    user,
                    conversation.input(),
                    self._services.llm,
                    SpeechTextGuard(),
                    conversation.generated(),
                    self._services.tts,
                    transport.output(),
                    conversation.output(),
                    hangup,
                    assistant,
                ]
            )
            worker = PipelineWorker(
                pipeline,
                params=PipelineParams(
                    audio_in_sample_rate=16000,
                    audio_out_sample_rate=self._settings.tts.sample_rate,
                ),
                observers=[errors, speaking],
                # 不向客户端输出模型收到的上下文或内部日志。
                rtvi_observer_params=RTVIObserverParams(
                    user_llm_enabled=False, system_logs_enabled=False, metrics_enabled=False
                ),
                idle_timeout_secs=None,
            )
            self._worker = worker
            if self._transport is None:
                worker.rtvi.add_event_handler("on_client_ready", self._open_conversation)
            else:
                transport.add_event_handler("on_client_connected", self._open_conversation)

                @transport.event_handler("on_error")
                async def on_transport_error(transport: BaseTransport, message: str) -> None:
                    if self._reason is not None:
                        # 清理失败仍需可见，但不覆盖已确认的用户/AI挂断原因。
                        await self._callbacks.on_error(message)
                    else:
                        await self._fail(message, reason="transport_error")

            @user.event_handler("on_user_turn_started")
            async def on_user_started(
                aggregator: LLMUserAggregator, strategy: BaseUserTurnStartStrategy
            ) -> None:
                self._user_spoken = True

            @user.event_handler("on_user_turn_stopped")
            async def on_user_stopped(
                aggregator: LLMUserAggregator,
                strategy: BaseUserTurnStopStrategy,
                message: UserTurnStoppedMessage,
            ) -> None:
                if message.content:
                    await self._commit_message("user", message.content, False, message.timestamp)
                    await self._state_changed("thinking")
                else:
                    await self._state_changed("listening")

            # aggregator 的同名公开事件异步排队，会晚于 watchdog 强制提交。
            # 直接使用 1.12.0 controller 同步事件，先取消再允许兜底路径继续。
            @user._user_turn_controller.event_handler("on_user_turn_stop_timeout")
            async def on_user_turn_timeout(controller: UserTurnController) -> None:
                await self._fail("语音识别等待超时，请检查识别服务和超时设置后重试。")

            @assistant.event_handler("on_assistant_turn_started")
            async def on_assistant_started(aggregator: object) -> None:
                await self._state_changed("thinking")

            @assistant.event_handler("on_assistant_turn_stopped")
            async def on_assistant_stopped(
                aggregator: object, message: AssistantTurnStoppedMessage
            ) -> None:
                await self._commit_message(
                    "assistant", message.content, message.interrupted, message.timestamp
                )

            @speaking.event_handler("on_speech_event")
            async def on_speech_event(observer: SpeakingObserver, event: SpeechEvent) -> None:
                if event.kind == SpeechEventKind.USER_SPEECH_STARTED:
                    self._user_spoken = True
                    await self._state_changed("listening")
                elif event.kind == SpeechEventKind.BOT_SPEECH_STARTED:
                    await self._state_changed("speaking")
                elif event.kind == SpeechEventKind.USER_SPEECH_STOPPED:
                    await self._state_changed("recognizing")
                elif (
                    event.kind == SpeechEventKind.BOT_SPEECH_STOPPED
                    and self._state == "speaking"
                ):
                    # 已发生新的用户停口 / 模型回复时，不让迟到的旧音频结束覆盖状态。
                    await self._state_changed("listening")

            @errors.event_handler("on_error")
            async def on_pipeline_error(observer: ErrorObserver, event: ErrorEvent) -> None:
                stage_labels = {"asr": "语音识别", "llm": "文本模型", "tts": "语音合成"}
                stage = next(
                    (label for code, label in stage_labels.items() if f"{code}:" in event.message),
                    "语音模型",
                )
                await self._fail(f"{stage}请求失败，请检查服务地址、模型、密钥和超时设置。")

            @transport.event_handler("on_client_disconnected")
            async def on_disconnected(
                transport: BaseTransport, connection: SmallWebRTCConnection | str | None = None
            ) -> None:
                if self._reason is None:
                    reason = connection if isinstance(connection, str) else "disconnected"
                    await self.stop(reason)

            runner = WorkerRunner(handle_sigint=False, handle_sigterm=False)
            await runner.add_workers(worker)
            if self._reason is not None:
                await worker.cancel(reason=self._reason)
            await runner.run()
        except Exception:
            # 不将异常对象、上游响应或 traceback 交给日志 / 网页。
            await self._fail("语音会话启动或运行失败，请检查模型配置后重试。")
        finally:
            try:
                if self._transport is not None:
                    await self._transport.cleanup()
            finally:
                if self._services is not None:
                    await self._services.aclose()
        return self._reason or "disconnected"

    async def stop(self, reason: str = "hangup") -> None:
        """取消而非等待排队语音播完，结束原因保持第一次请求的值。"""
        if self._reason is None:
            self._reason = reason
        if self._user_aggregator is not None:
            # cancel() 前清掉尚未提交的用户文本，避免挂断反而触发新模型请求。
            await self._user_aggregator.reset()
        if self._worker is not None:
            await self._worker.cancel(reason=self._reason)
        elif self._transport is not None:
            await self._transport.cleanup()
        elif self._connection is not None:
            await self._connection.disconnect()
