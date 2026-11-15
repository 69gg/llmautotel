"""验证提前合成、官方首音计时、历史持久化与旧记录兼容。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from pipecat.frames.frames import (
    Frame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from test_voice import ControlledLLM, ControlledTTS, FixtureServices, LocalTransport, Passthrough

import llmautotel.voice as voice_module
from llmautotel.models import AppSettings, CallRecord, TurnLatency
from llmautotel.speech import FirstClauseTextAggregator
from llmautotel.store import Store
from llmautotel.voice import VoiceCallbacks, VoiceSession


async def aggregate(aggregator: FirstClauseTextAggregator, text: str) -> list[str]:
    return [item.text async for item in aggregator.aggregate(text)]


async def test_first_clause_emits_before_next_token_without_splitting_amounts() -> None:
    aggregator = FirstClauseTextAggregator(min_chars=8)
    assert await aggregate(aggregator, "您好，") == []
    assert await aggregate(aggregator, "当前价格是19.") == []
    assert await aggregate(aggregator, "01万元，") == ["您好，当前价格是19.01万元，"]
    assert await aggregate(aggregator, "可减32,000元，") == []
    assert await aggregate(aggregator, "条件以资料为准。") == ["可减32,000元，条件以资料为准。"]
    assert await aggregator.flush() is None


async def test_long_unpunctuated_reply_is_not_cut_arbitrarily_and_english_uses_tokenizer() -> None:
    aggregator = FirstClauseTextAggregator(min_chars=8)
    text = "这是一段没有标点也不能按任意字数切开型号或者价格的长回复"
    assert await aggregate(aggregator, text) == []
    result = await aggregator.flush()
    assert result is not None and result.text == text
    assert await aggregate(aggregator, "The price is 19.01, paid monthly.") == []
    result = await aggregator.flush()
    assert result is not None and result.text == "The price is 19.01, paid monthly."


async def test_first_list_clause_emits_early_but_preserves_numeric_model_lists() -> None:
    aggregator = FirstClauseTextAggregator(min_chars=8)
    assert await aggregate(aggregator, "它可以帮您整理文档、") == ["它可以帮您整理文档、"]
    assert await aggregate(aggregator, "总结会议、辅助写作。") == ["总结会议、辅助写作。"]
    assert await aggregator.flush() is None
    assert await aggregate(aggregator, "可选车型包括B8、B9、") == []
    assert await aggregate(aggregator, "还有其他型号。") == ["可选车型包括B8、B9、还有其他型号。"]
    assert await aggregator.flush() is None
    assert await aggregate(aggregator, "价格可以选择19.01、32.00、") == []
    assert await aggregate(aggregator, "具体以资料为准。") == [
        "价格可以选择19.01、32.00、具体以资料为准。"
    ]


async def test_interruption_discards_buffer_and_rearms_first_clause_for_next_response() -> None:
    aggregator = FirstClauseTextAggregator(min_chars=8)
    assert await aggregate(aggregator, "首个回复有足够长度，") == ["首个回复有足够长度，"]
    assert await aggregate(aggregator, "这些旧内容没有播放") == []
    await aggregator.handle_interruption()
    assert await aggregate(aggregator, "现在直接回答新问题，") == ["现在直接回答新问题，"]
    assert await aggregate(aggregator, "这是当前结论。") == ["这是当前结论。"]
    assert await aggregator.flush() is None
    assert await aggregate(aggregator, "再下一轮也提前分句，") == ["再下一轮也提前分句，"]


class PausedClauseLLM(ControlledLLM):
    def __init__(self) -> None:
        super().__init__(block_first=False)
        self.prefix_ready = asyncio.Event()
        self.release = asyncio.Event()

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        if not isinstance(frame, LLMContextFrame):
            await super().process_frame(frame, direction)
            return
        self.inputs.append([dict(message) for message in frame.context.get_messages()])
        await self.push_frame(LLMFullResponseStartFrame())
        await self.push_frame(LLMTextFrame("可以导出并整理资料，"))
        self.prefix_ready.set()
        await self.release.wait()
        await self.push_frame(LLMTextFrame("也可以处理其他内容。"))
        await self.push_frame(LLMFullResponseEndFrame())


@pytest.mark.parametrize("mode", ["first_clause", "sentence"])
async def test_real_pipeline_starts_first_clause_before_generation_finishes_and_times_reply(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    from test_voice import Recorder

    recorder = Recorder()
    latencies: list[TurnLatency] = []

    async def on_latency(entry: TurnLatency) -> None:
        latencies.append(entry)

    transport = LocalTransport(recorder)
    llm = PausedClauseLLM()
    tts = ControlledTTS()
    services = FixtureServices(Passthrough(), llm, tts)
    monkeypatch.setattr(voice_module, "create_services", lambda settings: services)
    settings = AppSettings.model_validate({"voice": {"response_chunking": mode}})
    session = VoiceSession(
        None,
        settings,
        VoiceCallbacks(recorder.on_state, recorder.on_message, recorder.on_error, on_latency),
        transport=transport,
    )
    task = asyncio.create_task(session.run())
    try:
        await asyncio.wait_for(transport.outgoing.started.wait(), 3)
        assert session._worker is not None
        await session._worker.queue_frame(VADUserStartedSpeakingFrame())
        # 模拟一段实际讲话，让开始事件的打断广播先完成；停口不能与开口同一时刻。
        await asyncio.sleep(0.1)
        await session._worker.queue_frames(
            [
                VADUserStoppedSpeakingFrame(stop_secs=0.3),
                TranscriptionFrame("支持导出吗？", "user", "test", finalized=True),
            ]
        )
        await asyncio.wait_for(llm.prefix_ready.wait(), 3)
        if mode == "first_clause":
            await asyncio.wait_for(transport.outgoing.audio_written.wait(), 1)
            assert tts.requests == ["可以导出并整理资料，"]
            assert not llm.release.is_set()
        else:
            await asyncio.sleep(0.05)
            assert tts.requests == []
            assert transport.outgoing.played == []
        llm.release.set()
        async with asyncio.timeout(3):
            while not latencies:
                await asyncio.sleep(0.01)
        assert len(latencies) == 1
        entry = latencies[0]
        assert entry.kind == "reply" and entry.sequence == 1 and entry.target_ms == 1000
        assert entry.total_ms >= 300
        assert any(
            part.stage == "endpointing_wait" and part.duration_ms >= 299 for part in entry.parts
        )
        assert abs(sum(part.duration_ms for part in entry.parts) - entry.total_ms) < 1
        async with asyncio.timeout(3):
            while not any(
                message.get("type") == "latency" for message in transport.outgoing.server_messages
            ):
                await asyncio.sleep(0.01)
        assert recorder.errors == []
    finally:
        llm.release.set()
        await session.stop()
        await asyncio.wait_for(task, 3)


async def test_legacy_record_loads_empty_metrics_and_new_metrics_survive_restart(
    tmp_path: Path,
) -> None:
    settings = AppSettings()
    original = {
        "id": "fixture",
        "started_at": "2026-11-14T00:00:00Z",
        "settings": settings.public(),
    }
    record = CallRecord.model_validate(original)
    assert record.latencies == []
    record.latencies.append(
        TurnLatency(
            sequence=1,
            kind="reply",
            timestamp="2026-11-14T00:00:01Z",
            total_ms=1400,
            target_ms=1000,
            parts=[{"stage": "speech_synthesis", "duration_ms": 1400}],
        )
    )
    store = Store(tmp_path)
    await store.save_call(record)
    reloaded = await Store(tmp_path).get_call(record.id)
    assert reloaded is not None and reloaded.latencies == record.latencies
    assert (await store.list_calls())[0]["latencies"] == record.model_dump()["latencies"]


@pytest.mark.parametrize(
    "voice",
    [
        {"response_chunking": "invalid"},
        {"first_clause_min_chars": 3},
        {"response_target_ms": 0},
        {"vad_stop_seconds": 0.09},
    ],
)
def test_invalid_latency_configuration_is_rejected(voice: dict[str, Any]) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        AppSettings.model_validate({"voice": voice})
