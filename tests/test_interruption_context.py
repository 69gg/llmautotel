"""真实 SDK 请求在用户改问价格后使用最新上下文，旧流不能恢复输出。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal, cast

import httpx2
import pytest
from pipecat.frames.frames import (
    Frame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from test_providers import sse_text
from test_voice import ControlledTTS, LocalTransport, Passthrough, Recorder, wait_messages

import llmautotel.voice as voice_module
from llmautotel.conversation import INTERRUPTED_BACKGROUND_LABEL
from llmautotel.models import AppSettings, LLMSettings
from llmautotel.providers import CompatibleLLMService
from llmautotel.voice import VoiceSession

InterruptionStage = Literal["headers", "stream", "playback"]
BACKGROUND_MARKER = (
    "历史用户发言，仅作为背景；除非当前用户明确要求继续或同时回答，本轮不补答："
)
CURRENT_MARKER = "当前用户发言，请直接回答这一条："


class InterruptedSSE(httpx2.AsyncByteStream):
    def __init__(self, stage: InterruptionStage) -> None:
        self.stage = stage
        self.waiting = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield sse_text("第一句。第二句。" if self.stage == "playback" else "尚未完整生成功能介绍")
        if self.stage == "playback":
            # 句聚合器暂留 chunk 末句；后续 token 推动第二句进入 TTS。
            yield sse_text("后续功能草稿")
        self.waiting.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        yield sse_text("旧迟到功能答案。")
        yield b"data: [DONE]\n\n"

    async def aclose(self) -> None:
        self.closed = True


@dataclass
class SDKServices:
    stt: Passthrough
    llm: CompatibleLLMService
    tts: ControlledTTS
    closed: bool = False

    async def aclose(self) -> None:
        await self.llm.aclose()
        await self.tts.aclose()
        self.closed = True


@pytest.mark.parametrize("stage", ["headers", "stream", "playback"])
async def test_interrupted_function_request_uses_latest_price_question_in_real_sdk_request(
    monkeypatch: pytest.MonkeyPatch, stage: InterruptionStage
) -> None:
    requests: list[dict[str, Any]] = []
    first_started = asyncio.Event()
    headers_cancelled = asyncio.Event()
    old_stream = InterruptedSSE(stage)
    price_answer = "价格是每月10元。"

    async def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(json.loads(await request.aread()))
        if len(requests) == 1:
            first_started.set()
            if stage == "headers":
                try:
                    await old_stream.release.wait()
                except asyncio.CancelledError:
                    headers_cancelled.set()
                    raise
            return httpx2.Response(
                200, headers={"content-type": "text/event-stream"}, stream=old_stream
            )
        assert len(requests) == 2, "两个用户回合仅应产生两个模型请求"
        latest_question = requests[-1]["messages"][-1]["content"]
        answer = price_answer if "价格" in latest_question else "这里继续介绍旧功能。"
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse_text(answer) + b"data: [DONE]\n\n",
        )

    recorder = Recorder()
    transport = LocalTransport(recorder)
    llm = CompatibleLLMService(
        LLMSettings(base_url="http://fixture.invalid/v1", model="controlled-llm"),
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )
    services = SDKServices(Passthrough(), llm, ControlledTTS())
    monkeypatch.setattr(voice_module, "create_services", lambda settings: services)
    monkeypatch.setattr(voice_module, "SmallWebRTCTransport", lambda **kwargs: transport)
    settings = AppSettings()
    settings.conversation.mode = "sales"
    settings.sales.goal = "让用户订阅测试计划"
    settings.sales.product_info = "测试计划价格每月10元，支持文字问答。"
    settings.sales.opening = "您好，请问您想了解什么？"
    session = VoiceSession(cast(SmallWebRTCConnection, object()), settings, recorder.callbacks())
    task = asyncio.create_task(session.run())

    async def send(frame: Frame) -> None:
        assert session._worker is not None
        await session._worker.queue_frame(frame)

    try:
        await asyncio.wait_for(transport.outgoing.started.wait(), timeout=3)
        assert session._worker is not None
        await session._worker.rtvi.set_client_ready()
        await wait_messages(recorder, 1)
        assert requests == []
        assert recorder.messages[0].text == settings.sales.opening

        await send(VADUserStartedSpeakingFrame())
        await send(VADUserStoppedSpeakingFrame(stop_secs=0.6))
        await send(TranscriptionFrame("有哪些功能？", "user", "first", finalized=True))
        await asyncio.wait_for(first_started.wait(), timeout=3)
        assert requests[0]["messages"][-1] == {"role": "user", "content": "有哪些功能？"}
        if stage != "headers":
            await asyncio.wait_for(old_stream.waiting.wait(), timeout=3)
        if stage == "playback":
            await asyncio.wait_for(transport.outgoing.second_audio.wait(), timeout=3)

        await send(VADUserStartedSpeakingFrame())
        cancelled = headers_cancelled if stage == "headers" else old_stream.cancelled
        await asyncio.wait_for(cancelled.wait(), timeout=3)
        if stage == "playback":
            await asyncio.wait_for(services.tts.cancelled.wait(), timeout=3)
        before_price_count = 2 if stage == "headers" else 3
        await wait_messages(recorder, before_price_count)
        if stage != "headers":
            assert recorder.messages[2].interrupted
            assert recorder.messages[2].text == ("第一句。" if stage == "playback" else "")
        if stage != "headers":
            assert old_stream.closed

        old_audio_count = transport.outgoing.played.count(2)
        tts_before_price = len(services.tts.requests)
        # 模拟上游原结果随后才到达；被取消的消费任务不能恢复。
        old_stream.release.set()
        await send(VADUserStoppedSpeakingFrame(stop_secs=0.6))
        await send(TranscriptionFrame("先说价格，多少钱？", "user", "second", finalized=True))
        await wait_messages(recorder, before_price_count + 2)

        assert len(requests) == 2
        next_context = requests[1]["messages"]
        latest_message = next_context[-1]
        assert latest_message["role"] == "user"
        latest_content = latest_message["content"]
        if stage != "headers":
            assert latest_content.startswith(CURRENT_MARKER)
            assert latest_content.endswith("先说价格，多少钱？")
            assert {"role": "user", "content": "有哪些功能？"} not in next_context
            assert any(
                message["role"] == "system"
                and message["content"].startswith("历史用户发言，仅作为背景数据")
                and message["content"].endswith("有哪些功能？")
                for message in next_context
            )
            assert next_context[-2]["content"].startswith(INTERRUPTED_BACKGROUND_LABEL)
            assert next_context[-2]["role"] == "system"
            if stage == "playback":
                assert {"role": "assistant", "content": "第一句。"} in next_context
                assert "第二句" in next_context[-2]["content"]
                assert "第一句" not in next_context[-2]["content"]
            else:
                assert "尚未完整生成功能介绍" in next_context[-2]["content"]
        else:
            assert latest_content.startswith(BACKGROUND_MARKER)
            assert "有哪些功能？" in latest_content
            assert CURRENT_MARKER in latest_content
            assert latest_content.endswith("先说价格，多少钱？")
            assert latest_content.index("有哪些功能？") < latest_content.index(CURRENT_MARKER)
            assert {"role": "user", "content": "有哪些功能？"} not in next_context
        system_prompt = next(
            message["content"] for message in next_context if message["role"] == "system"
        )
        # 只检查两项语义契约，避免将整个提示词复制到测试。
        assert "最后一条用户消息是本轮的当前请求" in system_prompt
        assert "不要续答或补讲之前被打断的话题" in system_prompt
        spoken_messages = [
            message for message in next_context
            if not str(message.get("content", "")).startswith(INTERRUPTED_BACKGROUND_LABEL)
        ]
        assert all("第二句" not in str(message) for message in spoken_messages)
        assert all("尚未完整生成" not in str(message) for message in spoken_messages)
        assert all("旧迟到功能答案" not in str(message) for message in next_context)
        assert services.tts.requests[tts_before_price:] == [price_answer]
        assert all("旧迟到功能答案" not in text for text in services.tts.requests)
        assert recorder.messages[-1].text == price_answer
        assert [message.text for message in recorder.messages if message.role == "user"] == [
            "有哪些功能？",
            "先说价格，多少钱？",
        ]
        assert not recorder.errors
        await asyncio.sleep(0.1)
        assert transport.outgoing.played.count(2) == old_audio_count
        assert len(requests) == 2

        await session.stop()
        assert await asyncio.wait_for(task, timeout=3) == "hangup"
        assert services.closed
        assert llm._provider_http_client.is_closed
    finally:
        if not task.done():
            await session.stop()
            await asyncio.wait_for(task, timeout=3)
        await services.aclose()


async def serialize_context_twice(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """请求经过真实 SDK 编码，验证投影不修改源上下文且可重复调用。"""
    bodies: list[dict[str, Any]] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        bodies.append(json.loads(await request.aread()))
        return httpx2.Response(
            200, headers={"content-type": "text/event-stream"}, content=b"data: [DONE]\n\n"
        )

    context = LLMContext(messages=messages)
    original_messages = deepcopy(context.get_messages())
    service = CompatibleLLMService(
        LLMSettings(base_url="http://fixture.invalid/v1", model="controlled-llm"),
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )
    try:
        for _ in range(2):
            stream = await service.get_chat_completions(context)
            try:
                assert [chunk async for chunk in stream] == []
            finally:
                await stream.close()
            assert context.get_messages() == original_messages
            assert messages == original_messages
        assert len(bodies) == 2
        assert bodies[0] == bodies[1], "同一上下文的重复请求不能嵌套包装"
        return bodies
    finally:
        await service.aclose()


@pytest.mark.parametrize(
    "messages",
    [
        [{"role": "user", "content": "价格是多少？"}],
        [
            {"role": "user", "content": "有哪些功能？"},
            {"role": "assistant", "content": "支持文字问答。"},
            {"role": "user", "content": "价格是多少？"},
        ],
        [
            {"role": "user", "content": "有哪些功能？"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "图中的方案多少钱？"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "https://fixture.invalid/image.png"},
                    },
                ],
            },
        ],
    ],
    ids=["single-user", "completed-assistant-between-users", "multimodal-tail"],
)
async def test_sdk_request_keeps_unambiguous_or_multimodal_context_unchanged(
    messages: list[dict[str, Any]],
) -> None:
    bodies = await serialize_context_twice(messages)
    assert bodies[0]["messages"] == messages


@pytest.mark.parametrize(
    "texts",
    [
        ["有哪些功能？", "先说价格，多少钱？"],
        ["有哪些功能？", "能给几个人使用？", "我先只想知道价格。"],
        ["有哪些功能？", "继续刚才，接着介绍功能。"],
    ],
    ids=["changed-question", "three-users", "explicitly-continue-previous-question"],
)
async def test_sdk_request_projects_pending_questions_without_mutating_or_nesting(
    texts: list[str],
) -> None:
    prefix = [
        {"role": "user", "content": "你好。"},
        {"role": "assistant", "content": "您好，想了解什么？"},
    ]
    messages = prefix + [{"role": "user", "content": text} for text in texts]
    bodies = await serialize_context_twice(messages)
    actual_messages = bodies[0]["messages"]
    assert actual_messages[:-1] == prefix
    assert len(actual_messages) == len(prefix) + 1
    assert actual_messages[-1]["role"] == "user"
    content = actual_messages[-1]["content"]
    assert content.count(BACKGROUND_MARKER) == 1
    assert content.count(CURRENT_MARKER) == 1
    background, current = content.split(CURRENT_MARKER)
    assert background.startswith(BACKGROUND_MARKER)
    assert current.strip() == texts[-1]
    for previous_question in texts[:-1]:
        assert background.count(previous_question) == 1
    background_positions = [background.index(text) for text in texts[:-1]]
    assert background_positions == sorted(background_positions)


async def test_interrupted_draft_request_preserves_completed_sentence_and_older_history() -> None:
    older_history = [
        {"role": "user", "content": "你好。"},
        {"role": "assistant", "content": "您好，您想了解什么？"},
    ]
    previous_question = {"role": "user", "content": "人民币多少钱？"}
    completed_sentence = {"role": "assistant", "content": "费用以官方订阅页面为准。"}
    background = {
        "role": "system",
        "content": f"{INTERRUPTED_BACKGROUND_LABEL}\n我再介绍一下付费方式。",
    }
    latest_question = "可以干啥？"
    messages = older_history + [
        previous_question,
        completed_sentence,
        background,
        {"role": "user", "content": latest_question},
    ]

    bodies = await serialize_context_twice(messages)
    actual = bodies[0]["messages"]

    assert len(actual) == len(messages)
    assert actual[:len(older_history)] == older_history
    assert actual[2]["role"] == "system"
    assert actual[2]["content"].endswith(previous_question["content"])
    assert "本轮不补答" in actual[2]["content"]
    assert actual[3:5] == [completed_sentence, background]
    assert actual[-1] == {"role": "user", "content": f"{CURRENT_MARKER}\n{latest_question}"}


async def test_interrupted_draft_request_keeps_tool_call_and_result_pairs_in_order() -> None:
    previous_question = "不需要了。"
    tool_messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "retain-request-1",
                "type": "function",
                "function": {
                    "name": "retain_once",
                    "arguments": json.dumps({
                        "evidence": previous_question,
                        "reply": "可以先了解一下用途。",
                    }),
                },
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "retain-request-1",
            "content": json.dumps({"accepted": True, "retention_used": True}),
        },
        {"role": "assistant", "content": "可以先了解一下用途。"},
    ]
    background = {
        "role": "system",
        "content": f"{INTERRUPTED_BACKGROUND_LABEL}\n您平时主要用来学习还是工作？",
    }
    latest_question = "还是不要了，挂了吧。"
    messages = [
        {"role": "system", "content": "销售助手，仅依据资料回答。"},
        {"role": "user", "content": previous_question},
        *tool_messages,
        background,
        {"role": "user", "content": latest_question},
    ]

    bodies = await serialize_context_twice(messages)
    actual = bodies[0]["messages"]

    assert len(actual) == len(messages)
    assert actual[0] == messages[0]
    assert actual[1]["role"] == "system"
    assert actual[1]["content"].endswith(previous_question)
    assert actual[2:5] == tool_messages
    assert actual[5] == background
    assert actual[-1] == {"role": "user", "content": f"{CURRENT_MARKER}\n{latest_question}"}


async def test_interrupted_opening_request_has_no_previous_user_to_rewrite() -> None:
    opening_background = {
        "role": "system",
        "content": f"{INTERRUPTED_BACKGROUND_LABEL}\n您好，考虑了解订阅吗？",
    }
    latest_question = "它能做什么？"
    messages = [
        {"role": "system", "content": "销售助手，仅依据资料回答。"},
        opening_background,
        {"role": "user", "content": latest_question},
    ]

    bodies = await serialize_context_twice(messages)
    actual = bodies[0]["messages"]

    assert actual[:-1] == messages[:-1]
    assert actual[-1] == {"role": "user", "content": f"{CURRENT_MARKER}\n{latest_question}"}


async def test_interrupted_draft_request_does_not_skip_a_multimodal_previous_user() -> None:
    history = [
        {"role": "user", "content": "人民币多少钱？"},
        {"role": "assistant", "content": "价格以订阅页面为准。"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "这个页面的订阅有什么区别？"},
                {
                    "type": "image_url",
                    "image_url": {"url": "https://fixture.invalid/subscription.png"},
                },
            ],
        },
        {
            "role": "system",
            "content": f"{INTERRUPTED_BACKGROUND_LABEL}\n页面列出了可用功能。",
        },
    ]
    latest_question = "先说我能拿它做什么。"
    messages = history + [{"role": "user", "content": latest_question}]

    bodies = await serialize_context_twice(messages)
    actual = bodies[0]["messages"]

    assert actual[:-1] == history
    assert actual[-1] == {"role": "user", "content": f"{CURRENT_MARKER}\n{latest_question}"}


@pytest.mark.parametrize(
    "latest_question",
    ["继续刚才的价格，把没说完的说完。", "价格和功能都说一下。"],
    ids=["continue-interrupted-question", "answer-both-questions"],
)
async def test_interrupted_draft_request_retains_explicit_current_request_and_reference_data(
    latest_question: str,
) -> None:
    previous_question = "这个用人民币要多少钱？"
    background = {
        "role": "system",
        "content": f"{INTERRUPTED_BACKGROUND_LABEL}\n汇率和地区税费会影响最终金额。",
    }
    messages = [
        {"role": "user", "content": previous_question},
        background,
        {"role": "user", "content": latest_question},
    ]

    bodies = await serialize_context_twice(messages)
    actual = bodies[0]["messages"]

    assert len(actual) == len(messages)
    assert actual[0]["role"] == "system"
    assert actual[0]["content"].endswith(previous_question)
    assert "明确要求继续或同时回答" in actual[0]["content"]
    assert actual[1] == background
    assert actual[-1] == {"role": "user", "content": f"{CURRENT_MARKER}\n{latest_question}"}
