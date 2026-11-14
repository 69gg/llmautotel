"""只读配置的真实语音链路基准；音频只在内存中，不连接电话或设备。"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import sqlite3
import struct
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import median
from typing import Any

from loguru import logger
from pipecat.frames.frames import ErrorFrame, TTSAudioRawFrame

from llmautotel.config import RuntimeConfig
from llmautotel.models import AppSettings, TurnLatency
from llmautotel.providers import create_tts_service
from llmautotel.telephony.base import MediaCallbacks
from llmautotel.telephony.transport import PhoneTransport
from llmautotel.voice import TranscriptRole, VoiceCallbacks, VoiceSession


class BenchmarkFailure(RuntimeError):
    """仅允许固定的错误代码进入基准输出。"""


def pcm_rms(audio: bytes) -> float:
    """PCM16LE 的实际幅度，零音频不构成有效首音。"""
    if len(audio) % 2:
        raise BenchmarkFailure("invalid_pcm")
    if not audio:
        return 0
    return math.sqrt(
        sum(value * value for (value,) in struct.iter_unpack("<h", audio)) / (len(audio) // 2)
    )


def trim_trailing_silence(audio: bytes, sample_rate: int, threshold: int) -> bytes:
    """保留至最后一个有效的 20 ms 块；与实时输入块保持同一计时精度。"""
    size = sample_rate // 50 * 2
    if not audio or len(audio) % 2 or size <= 0:
        raise BenchmarkFailure("invalid_question_audio")
    active_end = 0
    for offset in range(0, len(audio), size):
        if pcm_rms(audio[offset : offset + size]) >= threshold:
            active_end = offset + size
    if not active_end:
        raise BenchmarkFailure("silent_question_audio")
    return audio[:active_end].ljust(active_end, b"\0")


class SampleDriver:
    """复用电话媒体的真实排队和播放屏障，仅把线路替换成内存终端。"""

    def __init__(self, callbacks: MediaCallbacks, sample_rate: int, threshold: int) -> None:
        self.callbacks = callbacks
        self.sample_rate = sample_rate
        self.threshold = threshold
        self.first_audio_at: float | None = None
        self.armed = False
        self.audio_end = 0.0

    async def start(self, number: str, call_id: str) -> None:
        await self.callbacks.on_ready()

    async def send_audio(self, audio: bytes) -> None:
        now = asyncio.get_running_loop().time()
        self.audio_end = now + len(audio) / (self.sample_rate * 2)
        if self.armed and self.first_audio_at is None and pcm_rms(audio) >= self.threshold:
            self.first_audio_at = now

    async def wait_played(self) -> None:
        await asyncio.sleep(max(0, self.audio_end - asyncio.get_running_loop().time()))

    async def flush(self) -> None:
        self.audio_end = 0

    async def hangup(self) -> None:
        pass

    async def close(self) -> None:
        pass


@dataclass
class SampleResult:
    sequence: int
    effective_audio_ms: float
    target_ms: int
    within_target: bool
    user_text: str
    assistant_text: str
    server_latency: dict[str, Any] | None


async def synthesize_question(settings: AppSettings, question: str, threshold: int) -> bytes:
    service = create_tts_service(settings)
    chunks: list[bytes] = []
    try:
        async for frame in service.run_tts(question, "benchmark-question"):
            if isinstance(frame, ErrorFrame):
                raise BenchmarkFailure("question_synthesis_failed")
            if isinstance(frame, TTSAudioRawFrame):
                chunks.append(frame.audio)
    finally:
        await service.aclose()
    return trim_trailing_silence(b"".join(chunks), settings.tts.sample_rate, threshold)


async def measure_sample(
    settings: AppSettings, audio: bytes, *, sequence: int, threshold: int, timeout: float
) -> SampleResult:
    """停口至内存终端有效 PCM；包含 VAD、真实 ASR/LLM/TTS 和输出节奏。"""
    opening_done = asyncio.Event()
    reply_done = asyncio.Event()
    errors: list[str] = []
    user_texts: list[str] = []
    assistant_texts: list[str] = []
    latencies: list[TurnLatency] = []

    async def on_state(state: str) -> None:
        pass

    async def on_message(
        role: TranscriptRole, text: str, interrupted: bool, timestamp: str
    ) -> None:
        if role == "user":
            user_texts.append(text)
        elif not opening_done.is_set():
            opening_done.set()
        else:
            if not interrupted and text.strip():
                assistant_texts.append(text)
            reply_done.set()

    async def on_error(message: str) -> None:
        errors.append("pipeline_error")
        opening_done.set()
        reply_done.set()

    async def on_latency(entry: TurnLatency) -> None:
        latencies.append(entry)

    transport = PhoneTransport(
        lambda callbacks: SampleDriver(callbacks, settings.tts.sample_rate, threshold),
        number="",
        call_id="benchmark",
    )
    driver = transport.driver
    assert isinstance(driver, SampleDriver)
    session = VoiceSession(
        None,
        settings.model_copy(deep=True),
        VoiceCallbacks(on_state, on_message, on_error, on_latency),
        transport=transport,
    )
    task = asyncio.create_task(session.run())
    try:
        async with asyncio.timeout(timeout):
            await opening_done.wait()
            if errors:
                raise BenchmarkFailure("opening_failed")
            await driver.wait_played()
            driver.armed = True
            size = settings.tts.sample_rate // 50 * 2
            speech = trim_trailing_silence(audio, settings.tts.sample_rate, threshold)
            # 先送 200 ms 静音，再按实时 20 ms 采集节奏提交，末尾足够触发 VAD。
            prefix = b"\0" * (size * 10)
            tail_frames = math.ceil((settings.voice.vad_stop_seconds + 0.5) * 50)
            payload = prefix + speech + b"\0" * (size * tail_frames)
            stopped_at: float | None = None
            start = asyncio.get_running_loop().time()
            for offset in range(0, len(payload), size):
                due = start + (offset // size + 1) * 0.02
                await asyncio.sleep(max(0, due - asyncio.get_running_loop().time()))
                if offset + size == len(prefix) + len(speech):
                    stopped_at = asyncio.get_running_loop().time()
                await driver.callbacks.on_audio(payload[offset : offset + size], driver.sample_rate)
                if errors:
                    raise BenchmarkFailure("pipeline_failed")
            await reply_done.wait()
            if errors:
                raise BenchmarkFailure("pipeline_failed")
            if len(user_texts) != 1 or not assistant_texts:
                raise BenchmarkFailure("incomplete_or_split_turn")
            if stopped_at is None or driver.first_audio_at is None:
                raise BenchmarkFailure("no_effective_reply_audio")
            elapsed_ms = round((driver.first_audio_at - stopped_at) * 1000, 1)
            if elapsed_ms < 0:
                raise BenchmarkFailure("reply_before_question_ended")
            replies = [entry for entry in latencies if entry.kind == "reply"]
            target = settings.voice.response_target_ms
            return SampleResult(
                sequence,
                elapsed_ms,
                target,
                elapsed_ms <= target,
                user_texts[0],
                " ".join(assistant_texts),
                replies[-1].model_dump(mode="json") if replies else None,
            )
    finally:
        await session.stop("benchmark_complete")
        try:
            await asyncio.wait_for(task, 10)
        except TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def read_settings(data_dir: Path) -> AppSettings:
    """不初始化或写入数据库，也不创建通话历史。"""
    database = (data_dir / "app.sqlite3").resolve()
    with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as connection:
        row = connection.execute("SELECT body FROM settings WHERE id = 1").fetchone()
    if row is None:
        raise BenchmarkFailure("missing_configuration")
    return AppSettings.model_validate_json(row[0])


async def benchmark(args: argparse.Namespace) -> int:
    settings = read_settings(args.data_dir)
    voice = settings.voice.model_dump()
    if args.chunking is not None:
        voice["response_chunking"] = args.chunking
    if args.vad_stop is not None:
        voice["vad_stop_seconds"] = args.vad_stop
    settings.voice = type(settings.voice).model_validate(voice)
    if (
        not 1 <= args.samples <= 20
        or not 1 <= args.threshold <= 32767
        or not args.question.strip()
        or not math.isfinite(args.timeout)
        or args.timeout <= 0
    ):
        raise BenchmarkFailure("invalid_benchmark_parameters")
    audio = await synthesize_question(settings, args.question, args.threshold)
    results: list[SampleResult] = []
    for sequence in range(1, args.samples + 1):
        result = await measure_sample(
            settings,
            audio,
            sequence=sequence,
            threshold=args.threshold,
            timeout=args.timeout,
        )
        results.append(result)
        print(json.dumps({"sample": asdict(result)}, ensure_ascii=False), flush=True)
    times = [result.effective_audio_ms for result in results]
    print(
        json.dumps(
            {
                "summary": {
                    "scope": "in_memory_terminal_not_browser_or_phone",
                    "semantic_check": "review_assistant_text_manually",
                    "models": {
                        stage: getattr(settings, stage).model for stage in ("asr", "llm", "tts")
                    },
                    "chunking": settings.voice.response_chunking,
                    "vad_stop_seconds": settings.voice.vad_stop_seconds,
                    "rms_threshold": args.threshold,
                    "samples": len(results),
                    "min_ms": min(times),
                    "median_ms": median(times),
                    "max_ms": max(times),
                    "within_target": sum(result.within_target for result in results),
                },
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0 if all(result.within_target for result in results) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=RuntimeConfig.from_environment().data_dir)
    parser.add_argument("--question", required=True, help="合成的测试问题，不包含私人资料")
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--chunking", choices=("first_clause", "sentence"))
    parser.add_argument("--vad-stop", type=float)
    parser.add_argument("--threshold", type=int, default=300, help="有效音频块的 PCM16 RMS 下限")
    parser.add_argument("--timeout", type=float, default=90, help="每通测量的超时秒数")
    args = parser.parse_args()
    logger.remove()
    logging.disable(logging.CRITICAL)
    try:
        return asyncio.run(benchmark(args))
    except BenchmarkFailure as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), flush=True)
        return 2
    except (Exception, KeyboardInterrupt):
        # 不输出模型异常、请求体、数据库内容或 traceback。
        print(
            json.dumps(
                {"error": "测量未完成，请检查模型配置、网络和测试参数。"}, ensure_ascii=False
            ),
            flush=True,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
