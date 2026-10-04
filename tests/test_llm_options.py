"""可选思考参数使用真实 SDK 编码；SSE 推理字段不会进入 TTS。"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import httpx2
import pytest
from pipecat.frames.frames import (
    EndFrame,
    Frame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    TTSAudioRawFrame,
    TTSTextFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.workers.runner import WorkerRunner

from llmautotel.models import LLMSettings, TTSSettings
from llmautotel.providers import CompatibleLLMService, CompatibleTTSService


def streaming_response(deltas: list[dict[str, Any]]) -> httpx2.Response:
    events = []
    for delta in deltas:
        chunk = {
            "id": "controlled-reply",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "deepseek-flash",
            "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
        }
        events.append(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n")
    return httpx2.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=("".join(events) + "data: [DONE]\n\n").encode(),
    )


@pytest.mark.parametrize(
    ("thinking", "reasoning_effort"),
    [
        (None, None),
        ("enabled", "high"),
        ("disabled", None),
        (None, "none"),
        (None, "low"),
        (None, "medium"),
        (None, "max"),
    ],
)
async def test_llm_optional_thinking_params_and_both_token_limits_omitted(
    thinking: str | None,
    reasoning_effort: str | None,
) -> None:
    bodies: list[dict[str, Any]] = []
    endpoints: list[str] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        endpoints.append(str(request.url))
        bodies.append(json.loads(await request.aread()))
        return streaming_response([{"content": "您好。"}])

    settings = LLMSettings.model_validate(
        {
            "base_url": "https://api.deepseek.com",
            "model": "deepseek-flash",
            "thinking": thinking,
            "reasoning_effort": reasoning_effort,
        }
    )
    service = CompatibleLLMService(
        settings,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )
    try:
        stream = await service.get_chat_completions(
            LLMContext(messages=[{"role": "user", "content": "介绍一下订阅。"}])
        )
        try:
            assert [chunk.choices[0].delta.content async for chunk in stream] == ["您好。"]
        finally:
            await stream.close()
        assert endpoints == ["https://api.deepseek.com/chat/completions"]
        assert len(bodies) == 1
        body = bodies[0]
        assert body["model"] == "deepseek-flash"
        assert body["stream"] is True
        assert "max_tokens" not in body
        assert "max_completion_tokens" not in body
        assert "extra_body" not in body
        if thinking is None:
            assert "thinking" not in body
        else:
            assert body["thinking"] == {"type": thinking}
        if reasoning_effort is None:
            assert "reasoning_effort" not in body
        else:
            assert body["reasoning_effort"] == reasoning_effort
    finally:
        await service.aclose()


async def test_reasoning_sse_reuses_pipecat_and_sends_only_answer_to_actual_tts_request() -> None:
    llm_bodies: list[dict[str, Any]] = []
    tts_bodies: list[dict[str, Any]] = []
    private_reasoning = "内部推理测试字段，不能朗读。"

    async def llm_handler(request: httpx2.Request) -> httpx2.Response:
        llm_bodies.append(json.loads(await request.aread()))
        return streaming_response(
            [
                {"role": "assistant", "reasoning_content": private_reasoning},
                {"reasoning_content": "继续内部测试推理。", "content": "您好，"},
                {"content": "欢迎了解订阅。"},
            ]
        )

    async def tts_handler(request: httpx.Request) -> httpx.Response:
        tts_bodies.append(json.loads(await request.aread()))
        return httpx.Response(200, content=b"\x01\x00" * 960)

    class Collector(FrameProcessor):
        def __init__(self) -> None:
            super().__init__()
            self.frames: list[Frame] = []
            self.completed = asyncio.Event()

        async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
            await super().process_frame(frame, direction)
            self.frames.append(frame)
            if isinstance(frame, LLMFullResponseEndFrame):
                self.completed.set()
            await self.push_frame(frame, direction)

    llm = CompatibleLLMService(
        LLMSettings.model_validate(
            {
                "base_url": "https://api.deepseek.com",
                "model": "deepseek-flash",
                "thinking": "enabled",
                "reasoning_effort": "high",
            }
        ),
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(llm_handler)),
    )
    tts = CompatibleTTSService(
        TTSSettings(base_url="http://fixture.invalid/v1", model="fixture", voice="fixture"),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(tts_handler)),
    )
    collector = Collector()
    worker = PipelineWorker(
        Pipeline([llm, tts, collector]),
        enable_rtvi=False,
        enable_turn_tracking=False,
        idle_timeout_secs=None,
        params=PipelineParams(audio_out_sample_rate=24000),
    )
    runner = WorkerRunner(handle_sigint=False, handle_sigterm=False)
    await runner.add_workers(worker)
    task = asyncio.create_task(runner.run())
    try:
        await worker.queue_frame(
            LLMContextFrame(
                LLMContext(
                    messages=[
                        {"role": "system", "content": "用一句话礼貌介绍订阅。"},
                        {"role": "user", "content": "你好。"},
                    ]
                )
            )
        )
        await asyncio.wait_for(collector.completed.wait(), timeout=3)
        assert len(llm_bodies) == 1
        assert llm_bodies[0]["thinking"] == {"type": "enabled"}
        assert llm_bodies[0]["reasoning_effort"] == "high"
        assert "max_tokens" not in llm_bodies[0]
        assert "max_completion_tokens" not in llm_bodies[0]
        assert [body["input"] for body in tts_bodies] == ["您好，欢迎了解订阅。"]
        text_frames = [frame for frame in collector.frames if isinstance(frame, TTSTextFrame)]
        assert [frame.text for frame in text_frames] == ["您好，欢迎了解订阅。"]
        assert any(isinstance(frame, TTSAudioRawFrame) for frame in collector.frames)
        assert all(private_reasoning not in str(frame) for frame in collector.frames)
        await worker.queue_frame(EndFrame())
        await asyncio.wait_for(task, timeout=3)
    finally:
        if not task.done():
            await worker.cancel()
            await asyncio.wait_for(task, timeout=3)
        await llm.aclose()
        await tts.aclose()
