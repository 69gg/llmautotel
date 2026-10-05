"""内部草稿标记不能进入实际 TTS、SQLite 文字或 RTVI 通话记录。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import httpx
import httpx2
import pytest
from pipecat.frames.frames import (
    Frame,
    TranscriptionFrame,
    TTSSpeakFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from test_providers import sse_text
from test_voice import LocalTransport, Passthrough, Recorder, wait_messages

import llmautotel.voice as voice_module
from llmautotel.conversation import INTERRUPTED_BACKGROUND_LABEL
from llmautotel.models import AppSettings, CallRecord, LLMSettings, TTSSettings
from llmautotel.providers import CompatibleLLMService, CompatibleTTSService
from llmautotel.store import Store
from llmautotel.voice import TranscriptRole, VoiceCallbacks, VoiceSession

OPENING = "您好，想了解订阅吗？"
ANSWER = "它可以解释知识、整理资料。"


class ScriptedSSE(httpx2.AsyncByteStream):
    """按提供的 token 边界输出；可暂停一次以制造真实 SDK 取消。"""

    def __init__(self, chunks: list[str], *, pause: bool = False) -> None:
        self.chunks = chunks
        self.pause = pause
        self.waiting = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield sse_text(chunk)
        if self.pause:
            self.waiting.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
            yield sse_text("】迟到的旧回复。")
        yield b"data: [DONE]\n\n"

    async def aclose(self) -> None:
        self.closed = True


@dataclass
class SpeechServices:
    stt: Passthrough
    llm: CompatibleLLMService
    tts: CompatibleTTSService

    async def aclose(self) -> None:
        await self.llm.aclose()
        await self.tts.aclose()


@dataclass
class SpeechHarness:
    session: VoiceSession
    task: asyncio.Task[str]
    recorder: Recorder
    transport: LocalTransport
    services: SpeechServices
    store: Store
    record: CallRecord
    llm_requests: list[dict[str, Any]]
    tts_requests: list[dict[str, Any]]
    opening_audio_frames: int = 0

    async def send(self, frame: Frame) -> None:
        assert self.session._worker is not None
        await self.session._worker.queue_frame(frame)

    async def question(self, text: str, timestamp: str) -> None:
        await self.send(VADUserStartedSpeakingFrame())
        await self.send(VADUserStoppedSpeakingFrame(stop_secs=0.6))
        await self.send(TranscriptionFrame(text, "user", timestamp, finalized=True))

    async def close(self) -> None:
        if not self.task.done():
            await self.session.stop()
            assert await asyncio.wait_for(self.task, timeout=3) == "hangup"
        await self.services.aclose()

    async def assert_spoken_answer(self, expected: str) -> None:
        """检查真正发给语音服务的文本，并等到相同文字已落库和发送网页。"""
        async with asyncio.timeout(3):
            while not any(
                data.get("type") == "transcript"
                and data["entry"]["role"] == "assistant"
                and not data["entry"]["interrupted"]
                and data["entry"]["text"] != OPENING
                for data in self.transport.outgoing.server_messages
            ):
                await asyncio.sleep(0.01)
        tts_text = "".join(body["input"] for body in self.tts_requests[1:])
        assert compact(tts_text) == compact(expected)
        persisted = await self.store.get_call(self.record.id)
        assert persisted is not None
        assert compact(persisted.transcript[-1].text) == compact(expected)
        assert persisted.transcript[-1].role == "assistant"
        assert not persisted.transcript[-1].interrupted
        public_text = "".join(entry.text for entry in persisted.transcript)
        rtvi_text = "".join(
            data["entry"]["text"]
            for data in self.transport.outgoing.server_messages
            if data.get("type") == "transcript"
        )
        for text in (tts_text, public_text, rtvi_text):
            assert "AI 生成背景" not in text
            assert "上轮回复被打断" not in text
            assert "不要假定用户" not in text
            assert "迟到的旧回复" not in text
        assert len(self.transport.outgoing.played) > self.opening_audio_frames, (
            "开场之后确实输出了回复音频"
        )
        assert not self.recorder.errors


def compact(text: str) -> str:
    return "".join(text.split())


async def speech_harness(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, streams: list[ScriptedSSE]
) -> SpeechHarness:
    llm_requests: list[dict[str, Any]] = []
    tts_requests: list[dict[str, Any]] = []

    async def llm_handler(request: httpx2.Request) -> httpx2.Response:
        assert str(request.url).endswith("/chat/completions")
        llm_requests.append(json.loads(await request.aread()))
        assert len(llm_requests) <= len(streams), "不能额外续播或重新请求旧回复"
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=streams[len(llm_requests) - 1],
        )

    async def tts_handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).endswith("/audio/speech")
        tts_requests.append(json.loads(await request.aread()))
        return httpx.Response(
            200, headers={"content-type": "audio/pcm"}, content=b"\x01\x00" * 960
        )

    recorder = Recorder()
    transport = LocalTransport(recorder)
    services = SpeechServices(
        Passthrough(),
        CompatibleLLMService(
            LLMSettings(base_url="http://fixture.invalid/v1", model="fixture-llm"),
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(llm_handler)),
        ),
        CompatibleTTSService(
            TTSSettings(
                base_url="http://fixture.invalid/v1", model="fixture-tts", voice="fixture"
            ),
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(tts_handler)),
        ),
    )
    settings = AppSettings()
    settings.conversation.mode = "sales"
    settings.sales.goal = "让用户了解订阅"
    settings.sales.product_info = "可以解释知识和整理资料。"
    settings.sales.opening = OPENING
    store = Store(tmp_path / "speech")
    record = CallRecord(id="speech", started_at="now", settings=settings.public())

    async def persist(
        role: TranscriptRole, text: str, interrupted: bool, timestamp: str
    ) -> None:
        await recorder.on_message(role, text, interrupted, timestamp)
        record.transcript = list(recorder.messages)
        await store.save_call(record)

    monkeypatch.setattr(voice_module, "create_services", lambda settings: services)
    monkeypatch.setattr(voice_module, "SmallWebRTCTransport", lambda **kwargs: transport)
    session = VoiceSession(
        cast(SmallWebRTCConnection, object()),
        settings,
        VoiceCallbacks(recorder.on_state, persist, recorder.on_error),
    )
    task = asyncio.create_task(session.run())
    harness = SpeechHarness(
        session, task, recorder, transport, services, store, record, llm_requests, tts_requests
    )
    try:
        await asyncio.wait_for(transport.outgoing.started.wait(), timeout=3)
        assert session._worker is not None
        await session._worker.rtvi.set_client_ready()
        await wait_messages(recorder, 1)
        assert recorder.messages[0].text == OPENING
        assert len(tts_requests) == 1
        harness.opening_audio_frames = len(transport.outgoing.played)
        return harness
    except BaseException:
        await harness.close()
        raise


@pytest.mark.parametrize(
    ("chunks", "expected"),
    [
        ([INTERRUPTED_BACKGROUND_LABEL, ANSWER], ANSWER),
        (list(INTERRUPTED_BACKGROUND_LABEL) + [ANSWER], ANSWER),
        (
            list(
                INTERRUPTED_BACKGROUND_LABEL.replace("【", "[")
                .replace("】", "]")
                .replace("AI ", "AI")
            ) + [ANSWER],
            ANSWER,
        ),
        ([INTERRUPTED_BACKGROUND_LABEL.replace("。", "。 ").replace("】", " 】"), ANSWER], ANSWER),
        (
            [ANSWER, INTERRUPTED_BACKGROUND_LABEL, "您主要用来学习吗？"],
            ANSWER + "您主要用来学习吗？",
        ),
        ([ANSWER, INTERRUPTED_BACKGROUND_LABEL[:28]], ANSWER),
        ([ANSWER, "【AI 生"], ANSWER),
        (
            ["【产品】可以写作。【AI 生成背景：内部说明", "】现在回答。"],
            "【产品】可以写作。现在回答。",
        ),
        (
            ["[产品]可以写作。[AI生成背景：内部说明", "]现在回答。"],
            "[产品]可以写作。现在回答。",
        ),
    ],
    ids=[
        "whole", "each-token", "ascii-tokens", "history-spacing", "middle",
        "unclosed", "partial-prefix", "normal-bracket-before-control",
        "ascii-bracket-before-control",
    ],
)
async def test_echoed_internal_background_never_reaches_speech_or_history(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, chunks: list[str], expected: str
) -> None:
    harness = await speech_harness(monkeypatch, tmp_path, [ScriptedSSE(chunks)])
    try:
        await harness.question("可以做什么？", "first")
        await harness.assert_spoken_answer(expected)
        assert len(harness.llm_requests) == 1
    finally:
        await harness.close()


async def test_ordinary_chinese_brackets_and_background_words_are_spoken(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    expected = "它可以帮助您理解 AI 的背景【学习和写作】。"
    harness = await speech_harness(monkeypatch, tmp_path, [ScriptedSSE(list(expected))])
    try:
        await harness.question("可以做什么？", "first")
        await harness.assert_spoken_answer(expected)
    finally:
        await harness.close()


async def test_fixed_tool_utterance_drops_internal_label_before_tts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    expected = "好的，祝您生活愉快，再见。"
    harness = await speech_harness(monkeypatch, tmp_path, [])
    try:
        # 挽留和告别工具都会使用同一个 TTSSpeakFrame 通道。
        await harness.send(
            TTSSpeakFrame(text=INTERRUPTED_BACKGROUND_LABEL + expected, append_to_context=True)
        )
        await harness.assert_spoken_answer(expected)
        assert harness.llm_requests == []
    finally:
        await harness.close()


async def test_interruption_inside_internal_label_does_not_poison_next_reply(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    old_stream = ScriptedSSE(list(INTERRUPTED_BACKGROUND_LABEL[:28]), pause=True)
    harness = await speech_harness(
        monkeypatch, tmp_path, [old_stream, ScriptedSSE([ANSWER])]
    )
    try:
        await harness.question("多少钱？", "first")
        await asyncio.wait_for(old_stream.waiting.wait(), timeout=3)
        await harness.send(VADUserStartedSpeakingFrame())
        await asyncio.wait_for(old_stream.cancelled.wait(), timeout=3)
        old_stream.release.set()
        await harness.send(VADUserStoppedSpeakingFrame(stop_secs=0.6))
        await harness.send(TranscriptionFrame("可以干啥？", "user", "second", finalized=True))
        await harness.assert_spoken_answer(ANSWER)
        assert len(harness.llm_requests) == 2
        assert old_stream.closed
        next_request = json.dumps(harness.llm_requests[-1], ensure_ascii=False)
        assert "上轮回复被打断" not in next_request
        assert "迟到的旧回复" not in next_request
        assert "可以干啥？" in next_request
        await asyncio.sleep(0.1)
        assert len(harness.tts_requests) == 2
    finally:
        old_stream.release.set()
        await harness.close()
