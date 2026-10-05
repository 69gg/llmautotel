"""云平台托管语音：本机模型网关、公开回执及外呼资源生命周期。"""

from __future__ import annotations

import asyncio
import hmac
import json
from collections.abc import AsyncIterator
from typing import Any, Protocol

from fastapi import HTTPException
from openai import AsyncStream
from openai.types.chat import ChatCompletionChunk
from pipecat.adapters.schemas.tools_schema import AdapterType, ToolsSchema
from pipecat.processors.aggregators.llm_context import LLMContext

from llmautotel.models import AppSettings, TranscriptEntry
from llmautotel.providers import CompatibleLLMService
from llmautotel.speech import _filter_chunk, _speech_aggregator
from llmautotel.telephony.base import TelephonyError
from llmautotel.telephony.cloud import (
    AliyunCallClient,
    CloudProvider,
    TencentCallClient,
    authenticate_cloud_event,
    cloud_context_messages,
    cloud_sales_prompt,
    native_tencent_tools,
    parse_cloud_reports,
)
from llmautotel.telephony.cloud_inbound import cloud_gateway_call_id
from llmautotel.voice import VoiceCallbacks


class CloudClient(Protocol):
    async def start(
        self,
        number: str,
        call_id: str,
        settings: AppSettings,
        gateway_url: str,
    ) -> str: ...

    async def hangup(self, remote_id: str) -> None: ...

    async def close(self) -> None: ...


def _same_token(actual: str | None, expected: str) -> bool:
    return bool(actual and expected) and hmac.compare_digest(
        actual.encode("utf-8"),
        expected.encode("utf-8"),
    )


def _sse(payload: dict[str, Any]) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


class CloudVoiceSession:
    """每通使用配置快照，生成内容只给平台，不冒充已经播放的历史。"""

    def __init__(
        self,
        provider: CloudProvider,
        number: str,
        call_id: str,
        settings: AppSettings,
        callbacks: VoiceCallbacks,
        *,
        client: CloudClient | None = None,
        llm: CompatibleLLMService | None = None,
        incoming_remote_id: str | None = None,
    ) -> None:
        if incoming_remote_id is not None and not incoming_remote_id:
            raise ValueError("来电平台会话标识不能为空")
        self.provider = provider
        self.number = number
        self.call_id = call_id
        self.settings = settings.model_copy(deep=True)
        self.callbacks = callbacks
        self.config = getattr(self.settings.telephony, provider)
        self._client: CloudClient = client or (
            AliyunCallClient(self.config)
            if provider == "aliyun"
            else TencentCallClient(self.config)
        )
        self._llm = llm
        self.incoming = incoming_remote_id is not None
        self._remote_id: str | None = incoming_remote_id
        self._created = asyncio.Event()
        self._finished = asyncio.Event()
        self._stop_reason: str | None = None
        self._reason = "cloud_ended"
        self._start_task: asyncio.Task[str] | None = None
        self._hangup_lock = asyncio.Lock()
        self._hung_up = False
        self._closed = False
        self._generation = 0
        self._request_task: asyncio.Task[Any] | None = None
        self._report_received = False
        self._report_lock = asyncio.Lock()

    @property
    def remote_id(self) -> str | None:
        return self._remote_id

    def authenticate(self, authorization: str | None) -> bool:
        if not self.config.enabled or (self.incoming and not self.config.inbound_enabled):
            return False
        expected = self.config.gateway_token
        token = expected.get_secret_value() if expected else ""
        if self.provider == "tencent":
            return _same_token(authorization, "Bearer " + token if token else "")
        return _same_token(authorization, token)

    def authenticate_event(
        self,
        token: str | None,
        authorization: str | None = None,
    ) -> bool:
        """URL 能力 token；腾讯另支持其官方电话 BasicAuth，不假造 HMAC。"""
        return authenticate_cloud_event(self.config, token, authorization)

    def validate_gateway(self, body: dict[str, Any]) -> None:
        if (
            not self.config.enabled
            or (self.incoming and not self.config.inbound_enabled)
            or self._finished.is_set()
            or self._stop_reason
        ):
            raise HTTPException(409, "此通电话已结束或已失效")
        if body.get("stream") is not True:
            raise HTTPException(422, "模型网关仅支持流式请求")
        if self.provider == "aliyun":
            local_id = cloud_gateway_call_id(self.provider, body)
            session_id = body.get("session_id")
            if local_id != self.call_id or (self.remote_id and session_id != self.remote_id):
                raise HTTPException(409, "电话会话标识不匹配")
            if not isinstance(session_id, str) or not session_id:
                raise HTTPException(422, "缺少电话平台会话标识")
        elif cloud_gateway_call_id(self.provider, body) != self.call_id:
            raise HTTPException(409, "电话会话标识不匹配")
        elif self.incoming:
            # OpenAI 协议没有规定电话 SessionId；若平台提供则必须精确匹配。
            for field in ("session_id", "SessionId"):
                if field in body and body[field] != self.remote_id:
                    raise HTTPException(409, "电话平台会话标识不匹配")
        messages = body.get("messages")
        if not isinstance(messages, list) or any(
            not isinstance(message, dict)
            or message.get("role") not in {"system", "user", "assistant"}
            or not isinstance(message.get("content"), str)
            for message in messages
        ):
            raise HTTPException(422, "电话对话消息格式无效")
        if "tools" in body and not isinstance(body["tools"], list):
            raise HTTPException(422, "电话工具定义格式无效")
        if any(not isinstance(tool, dict) for tool in body.get("tools", [])):
            raise HTTPException(422, "电话工具定义格式无效")

    def cloud_gateway(self, body: dict[str, Any]) -> AsyncIterator[str]:
        """同步检查先于 HTTP 流；新请求使旧流与迟到模型结果失效。"""
        self.validate_gateway(body)
        self._generation += 1
        generation = self._generation
        previous = self._request_task
        if previous is not None and previous is not asyncio.current_task() and not previous.done():
            previous.cancel()
        return self._generate(body, generation)

    def _current(self, generation: int) -> bool:
        return generation == self._generation and not self._finished.is_set()

    def _context(self, body: dict[str, Any]) -> LLMContext:
        # 平台 system/model/采样参数不覆盖销售快照或本机模型设置。
        messages = [
            {
                "role": "system",
                "content": cloud_sales_prompt(
                    self.settings,
                    self.provider,
                ),
            }
        ]
        messages.extend(
            cloud_context_messages(
                [
                    {"role": message["role"], "content": message["content"]}
                    for message in body["messages"]
                    if message["role"] != "system"
                ]
            )
        )
        tools = native_tencent_tools(body.get("tools", [])) if self.provider == "tencent" else []
        if tools:
            return LLMContext(
                messages=messages,
                tools=ToolsSchema(
                    standard_tools=[],
                    custom_tools={AdapterType.OPENAI: tools},
                ),
            )
        return LLMContext(messages=messages)

    async def _generate(self, body: dict[str, Any], generation: int) -> AsyncIterator[str]:
        # 延迟开始消费的旧 HTTP iterator 不得接管或取消较新的请求。
        if not self._current(generation):
            return
        task = asyncio.current_task()
        previous = self._request_task
        self._request_task = task
        if previous is not None and previous is not task and not previous.done():
            previous.cancel()
        stream: AsyncStream[ChatCompletionChunk] | None = None
        aggregator = _speech_aggregator()
        allowed_tools: set[int] = set()
        allow_native = bool(native_tencent_tools(body.get("tools", [])))
        try:
            if not self._current(generation):
                return
            await self.callbacks.on_state("thinking")
            if not self._current(generation):
                return
            if self._llm is None:
                self._llm = CompatibleLLMService(self.settings.llm)
            stream = await self._llm.get_chat_completions(self._context(body))
            if not self._current(generation):
                return
            async for chunk in stream:
                if not self._current(generation):
                    return
                payload = chunk.model_dump(mode="json", exclude_none=True)
                for choice in payload.get("choices", []):
                    delta = choice.get("delta", {})
                    # 思考由本机设置控制；只把回复正文/原生工具发给平台。
                    delta.pop("reasoning_content", None)
                    if isinstance(delta.get("content"), str):
                        delta["content"] = await _filter_chunk(aggregator, delta["content"])
                    if "tool_calls" in delta:
                        filtered: list[dict[str, Any]] = []
                        for tool in delta["tool_calls"]:
                            index = tool.get("index", 0)
                            name = tool.get("function", {}).get("name")
                            if name == "call_end" and self.provider == "tencent" and allow_native:
                                allowed_tools.add(index)
                            if index in allowed_tools:
                                filtered.append(tool)
                        if filtered:
                            delta["tool_calls"] = filtered
                        else:
                            delta.pop("tool_calls", None)
                    if choice.get("finish_reason"):
                        remaining = await aggregator.flush()
                        if not self._current(generation):
                            return
                        if remaining is not None and remaining.text:
                            yield _sse(
                                {
                                    "choices": [
                                        {
                                            "index": 0,
                                            "delta": {
                                                "content": remaining.text,
                                            },
                                        }
                                    ]
                                }
                            )
                if not self._current(generation):
                    return
                await self.callbacks.on_state("speaking")
                if not self._current(generation):
                    return
                yield _sse(payload)
            remaining = await aggregator.flush()
            if self._current(generation):
                if remaining is not None and remaining.text:
                    yield _sse({"choices": [{"index": 0, "delta": {"content": remaining.text}}]})
            if self._current(generation):
                yield "data: [DONE]\n\n"
        except asyncio.CancelledError:
            # 平台打断或 HTTP 断开只取消本轮请求，不结束整通电话。
            raise
        except Exception:
            if self._current(generation):
                await self.callbacks.on_error("文本模型请求失败，请检查模型配置")
                if not self._current(generation):
                    return
                await self.stop("model_error")
        finally:
            await aggregator.reset()
            if stream is not None:
                await stream.close()
            if self._request_task is task:
                self._request_task = None

    async def _cancel_request(self) -> None:
        self._generation += 1
        task = self._request_task
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _hangup(self) -> None:
        async with self._hangup_lock:
            if (
                self.remote_id is None
                or self._hung_up
                or not self.config.enabled
                or (self.incoming and not self.config.inbound_enabled)
            ):
                return
            await self._client.hangup(self.remote_id)
            self._hung_up = True

    async def run(self) -> str:
        try:
            if self._finished.is_set():
                return self._stop_reason or self._reason
            if self.incoming:
                if not self.config.enabled or not self.config.inbound_enabled:
                    raise TelephonyError("电话来电接入未启用")
                self._created.set()
                # 回调发生在振铃阶段，尚无官方接听/实际播放确认。
                await self.callbacks.on_state("ringing")
            else:
                await self.callbacks.on_state("dialing")
                gateway_url = (
                    self.settings.telephony.public_base_url + f"/api/telephony/{self.provider}/llm"
                )
                self._start_task = asyncio.create_task(
                    self._client.start(
                        self.number,
                        self.call_id,
                        self.settings,
                        gateway_url,
                    )
                )
                # 不能取消外呼创建后忘掉远端 callId；迟到成功也要收到并挂断。
                self._remote_id = await asyncio.shield(self._start_task)
                self._created.set()
                await self.callbacks.on_state("ringing")
            if self._stop_reason:
                await self._hangup()
            try:
                await asyncio.wait_for(
                    self._finished.wait(),
                    timeout=self.config.call_timeout_seconds,
                )
            except TimeoutError:
                self._stop_reason = "call_timeout"
                await self.callbacks.on_error("电话通话或等待回执超时，已请求挂断")
                self._finished.set()
        except asyncio.CancelledError:
            self._stop_reason = self._stop_reason or "server_shutdown"
            self._finished.set()
        except Exception:
            self._reason = "provider_error"
            # 远端已绑定后，本地状态/记录异常也必须回收原电话。
            self._stop_reason = self._stop_reason or self._reason
            await self.callbacks.on_error("云电话请求失败，请检查电话配置")
            self._finished.set()
        finally:
            if self._start_task is not None and not self._start_task.done():
                try:
                    self._remote_id = await asyncio.shield(self._start_task)
                    await self.callbacks.on_state("ringing")
                except Exception:
                    pass
            self._created.set()
            await self._cancel_request()
            if self._stop_reason:
                try:
                    await self._hangup()
                except TelephonyError:
                    await self.callbacks.on_error("云电话挂断失败，请检查电话平台状态")
            try:
                if self._llm is not None:
                    await self._llm.aclose()
            except Exception:
                await self.callbacks.on_error("模型连接释放失败")
            finally:
                try:
                    await self._client.close()
                except Exception:
                    await self.callbacks.on_error("电话连接释放失败")
            self._closed = True
        return self._stop_reason or self._reason

    async def stop(self, reason: str = "user_hangup") -> None:
        self._stop_reason = self._stop_reason or reason
        self._finished.set()
        await self._cancel_request()
        if self._start_task is not None:
            await self._created.wait()
        if not self._closed:
            try:
                await self._hangup()
            except TelephonyError:
                await self.callbacks.on_error("云电话挂断失败，请检查电话平台状态")

    async def handle_event(self, payload: dict[str, Any] | list[Any]) -> bool:
        async with self._report_lock:
            return await self._handle_report(payload)

    async def _handle_report(self, payload: dict[str, Any] | list[Any]) -> bool:
        reports = parse_cloud_reports(self.provider, payload)
        report = next(
            (
                report
                for report in reports
                if (
                    (
                        report.remote_id == self.remote_id
                        or (
                            self.provider == "aliyun"
                            and self.remote_id is None
                            and report.local_id == self.call_id
                        )
                    )
                    and (report.local_id is None or report.local_id == self.call_id)
                )
            ),
            None,
        )
        if report is None:
            return False
        if self._report_received:
            return True
        if self.provider == "tencent":
            if not isinstance(payload, dict) or payload.get("SdkAppId") != self.config.sdk_app_id:
                return False
            transcript: list[TranscriptEntry] = []
            if isinstance(self._client, TencentCallClient):
                try:
                    transcript = await self._client.fetch_transcript(report.remote_id)
                except TelephonyError:
                    self._reason = report.end_reason
                    self._finished.set()
                    await self._cancel_request()
                    # 官方推送遇非成功应答会重试；不能 ACK 空记录后永久丢失文字。
                    raise HTTPException(503, "云平台文字记录暂不可用，请重试回执") from None
            else:
                transcript = report.transcript
        else:
            transcript = report.transcript
        for entry in transcript:
            await self.callbacks.on_message(
                entry.role,
                entry.text,
                entry.interrupted,
                entry.timestamp,
            )
        self._report_received = True
        self._reason = report.end_reason
        self._finished.set()
        await self._cancel_request()
        return True
