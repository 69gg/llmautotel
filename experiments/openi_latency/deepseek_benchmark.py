"""复用正式 TTS provider，对同一组完整文本做小额协议对照。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx
from pydantic import SecretStr
from websockets.exceptions import InvalidStatus

from experiments.openi_latency.protocol import PlaybackClock, summarize
from llmautotel.models import AppSettings, TTSSettings
from llmautotel.providers import create_tts_service


def billing_upper_bound(texts: list[str], repeats: int) -> int:
    """汉字计两字符；保守把其余非 ASCII 也计两字符。两种协议各合成一次。"""
    return (
        sum(sum(1 if character.isascii() else 2 for character in text) for text in texts)
        * 2
        * repeats
    )


async def run(
    texts: list[str],
    key: str,
    *,
    base_url: str,
    voice: str,
    repeats: int,
    timeout: float,
    protocol: str = "both",
) -> dict[str, Any]:
    samples: list[dict[str, Any]] = []
    settings = AppSettings()
    protocols: dict[bool, Any] = {}
    try:
        for streaming in (False, True):
            if protocol != "both" and protocol != ("websocket" if streaming else "http"):
                continue
            settings.tts = TTSSettings(
                protocol="deepseek",
                base_url=base_url,
                api_key=SecretStr(key),
                voice=voice,
                streaming_input=streaming,
                timeout_seconds=timeout,
            )
            protocols[streaming] = create_tts_service(settings)
        # 按文本交替协议；首个 HTTP 请求包含连接建立，WS 每次均建立新连接。
        for repeat in range(repeats):
            for index, text in enumerate(texts):
                for streaming, service in protocols.items():
                    started = time.perf_counter()
                    clock = PlaybackClock()
                    first_packet: float | None = None
                    audio_bytes = 0
                    chunks = 0
                    remainder = b""
                    sample: dict[str, Any] = {
                        "case": index,
                        "repeat": repeat,
                        "text": text,
                        "protocol": "websocket" if streaming else "http",
                        "ok": False,
                    }
                    try:
                        async with asyncio.timeout(timeout):
                            async for pcm in service._audio_chunks(text):
                                now = time.perf_counter()
                                if first_packet is None:
                                    first_packet = now
                                audio_bytes += len(pcm)
                                chunks += 1
                                remainder += pcm
                                aligned = len(remainder) & ~1
                                if aligned:
                                    clock.feed(remainder[:aligned], 24000, now)
                                    remainder = remainder[aligned:]
                        if remainder:
                            raise ValueError("incomplete_pcm_sample")
                        sample.update(
                            ok=clock.first_signal_at is not None,
                            first_pcm_ms=round((first_packet - started) * 1000, 1)
                            if first_packet is not None
                            else None,
                            effective_audio_ms=round((clock.first_signal_at - started) * 1000, 1)
                            if clock.first_signal_at is not None
                            else None,
                            total_ms=round((time.perf_counter() - started) * 1000, 1),
                            audio_ms=round(audio_bytes / 48000 * 1000, 1),
                            chunks=chunks,
                        )
                    except (httpx.HTTPStatusError, InvalidStatus) as exc:
                        sample["error"] = type(exc).__name__
                        response = exc.response
                        sample["http_status"] = (
                            response.status_code if response is not None else None
                        )
                    except Exception as exc:
                        # 不能打印可能含握手头、URL 或上游正文的异常字符串。
                        sample["error"] = type(exc).__name__
                        if isinstance(exc, ValueError) and str(exc) in {
                            "invalid_pcm",
                            "incomplete_pcm_sample",
                            "服务返回的音频格式不是原始 PCM",
                        }:
                            sample["error_code"] = str(exc)
                    samples.append(sample)
                    print(json.dumps(sample, ensure_ascii=False), flush=True)
                    if sample.get("http_status") in {401, 402, 403, 429}:
                        return {
                            "scope": "tts_only_fixed_text",
                            "samples": samples,
                            "stopped": "account_or_rate_limit_response",
                        }
    finally:
        for service in protocols.values():
            await service.aclose()
    return {
        "scope": "tts_only_fixed_text",
        "sample_rate": 24000,
        "input": "complete_text_available_at_request_start",
        "samples": samples,
        "summary": {
            protocol: summarize([row for row in samples if row["protocol"] == protocol])
            for protocol in ("http", "websocket")
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--base-url", default="https://api.deepseek.com/v1")
    parser.add_argument("--voice", choices=["stella", "tide"], default="stella")
    parser.add_argument("--protocol", choices=["both", "http", "websocket"], default="both")
    parser.add_argument("--repeats", type=int, choices=[1, 2, 3], default=1)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--max-billable-chars", type=int, default=1000)
    parser.add_argument("--key-stdin", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    texts = json.loads(args.cases.read_text())["tts_benchmark_texts"]
    if (
        not texts
        or len(texts) > 6
        or not all(isinstance(text, str) and text.strip() for text in texts)
    ):
        parser.error("cases must contain 1 to 6 non-empty texts")
    characters = billing_upper_bound(texts, args.repeats)
    if args.protocol != "both":
        characters //= 2
    if characters > args.max_billable_chars:
        parser.error("billable character budget exceeded before requesting")
    key = sys.stdin.readline().strip() if args.key_stdin else os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        parser.error("provide DEEPSEEK_API_KEY or --key-stdin")
    report = asyncio.run(
        run(
            texts,
            key,
            base_url=args.base_url,
            voice=args.voice,
            repeats=args.repeats,
            timeout=args.timeout,
            protocol=args.protocol,
        )
    )
    report["billable_characters_upper_bound"] = characters
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.touch(mode=0o600, exist_ok=True)
    args.output.chmod(0o600)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {key: value for key, value in report.items() if key != "samples"}, ensure_ascii=False
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
