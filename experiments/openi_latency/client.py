"""从本机或媒体服务器测 OpenI 代理往返和有效首音，不写录音或凭据。"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import statistics
import struct
import time
from pathlib import Path
from typing import Any

import httpx
import websockets

if __package__:
    from .protocol import PlaybackClock, summarize
else:
    from protocol import PlaybackClock, summarize


async def event(ws: Any, timeout: float = 1800) -> dict[str, Any]:
    raw = await asyncio.wait_for(ws.recv(), timeout)
    if isinstance(raw, bytes):
        generation, sample_rate = struct.unpack("<II", raw[:8])
        return {
            "type": "audio",
            "generation": generation,
            "sample_rate": sample_rate,
            "pcm": raw[8:],
        }
    return json.loads(raw)


async def until(ws: Any, kind: str) -> dict[str, Any]:
    while True:
        item = await event(ws)
        if item["type"] in {"error", "turn_error"}:
            raise RuntimeError(json.dumps(item, ensure_ascii=False))
        if item["type"] == kind:
            return item
        if item["type"] in {"status", "sample"}:
            print(json.dumps(item, ensure_ascii=False), flush=True)


async def wait_ready(client: httpx.AsyncClient, timeout: float = 1800) -> dict[str, Any]:
    """长时间初始化使用 HTTP，避免代理关闭闲置的 WebSocket。"""
    deadline = time.monotonic() + timeout
    previous = ""
    while time.monotonic() < deadline:
        response = await client.get("/status")
        response.raise_for_status()
        status = response.json()
        phase = status["phase"]
        if phase != previous:
            print(json.dumps({"phase": phase}, ensure_ascii=False), flush=True)
            previous = phase
        if phase == "ready":
            return status
        if phase in {"worker_exited", "model_initialization_failed"}:
            raise RuntimeError(json.dumps(status, ensure_ascii=False))
        await asyncio.sleep(min(5, max(0, deadline - time.monotonic())))
    raise TimeoutError("worker_initialization_timeout")


async def remote_sample(ws: Any, audio: bytes, generation: int, wait_ms: int) -> dict[str, Any]:
    await ws.send(json.dumps({"type": "begin", "generation": generation}))
    await until(ws, "begun")
    start = time.perf_counter()
    stopped = start + len(audio) / 32000

    async def send_audio() -> None:
        size = 640
        for offset in range(0, len(audio), size):
            due = start + min(offset + size, len(audio)) / 32000
            await asyncio.sleep(max(0, due - time.perf_counter()))
            await ws.send(
                json.dumps(
                    {
                        "type": "audio",
                        "generation": generation,
                        "pcm": base64.b64encode(audio[offset : offset + size]).decode(),
                    }
                )
            )
        await asyncio.sleep(max(0, stopped + wait_ms / 1000 - time.perf_counter()))
        await ws.send(json.dumps({"type": "end", "generation": generation}))

    sender = asyncio.create_task(send_audio())
    clock = PlaybackClock()
    try:
        while True:
            item = await event(ws, 90)
            if item.get("generation") != generation:
                continue
            if item["type"] == "audio":
                clock.feed(item["pcm"], item["sample_rate"], time.perf_counter())
            elif item["type"] in {"turn_error", "error"}:
                return {"ok": False, "generation": generation, "error": item}
            elif item["type"] == "done":
                result = {**item, "server_after_end_effective_ms": item["effective_audio_ms"]}
                result["ok"] = clock.first_signal_at is not None
                result["effective_audio_ms"] = (
                    round((clock.first_signal_at - stopped) * 1000, 1)
                    if clock.first_signal_at is not None
                    else None
                )
                return result
    finally:
        await asyncio.gather(sender)


async def run(args: argparse.Namespace) -> dict[str, Any]:
    token = Path(args.token_file).read_text().strip()
    if args.mode in {"status", "wait"}:
        async with httpx.AsyncClient(
            base_url=args.url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        ) as client:
            if args.mode == "wait":
                return await wait_ready(client, args.prepare_timeout_seconds)
            response = await client.get("/status")
            response.raise_for_status()
            return response.json()
    endpoint = args.url.rstrip("/").replace("https://", "wss://").replace("http://", "ws://")
    async with websockets.connect(
        endpoint + "/ws", max_size=8 * 1024 * 1024, ping_interval=20, close_timeout=5
    ) as ws:
        await ws.send(json.dumps({"token": token}))
        hello = await until(ws, "hello")
        if args.mode == "load":
            await ws.send(json.dumps({"type": "load"}))
            return await until(ws, "status")
        pings: list[float] = []
        for index in range(5):
            start = time.perf_counter()
            await ws.send(json.dumps({"type": "ping", "sequence": index}))
            await until(ws, "pong")
            pings.append(round((time.perf_counter() - start) * 1000, 1))
        report: dict[str, Any] = {
            "proxy_rtt_ms": pings,
            "proxy_rtt_median_ms": statistics.median(pings),
        }
        if args.mode == "probe":
            return {**report, **hello}
        if hello["phase"] != "ready":
            raise RuntimeError("worker_not_ready")
        if args.mode == "tts":
            await ws.send(json.dumps({"type": "tts_benchmark"}))
            return {**report, **await until(ws, "report")}
        if getattr(args, "variant", None):
            await ws.send(json.dumps({"type": "select_variant", "variant": args.variant}))
            await until(ws, "configured")
        if args.mode == "optimize":
            await ws.send(json.dumps({"type": "optimization_benchmark", "repeats": args.repeats}))
            return {**report, **await until(ws, "report")}
        if args.mode == "local":
            await ws.send(
                json.dumps(
                    {
                        "type": "local_benchmark",
                        "repeats": args.repeats,
                        "reset_hop": args.reset_hop,
                    }
                )
            )
            return {**report, **await until(ws, "report")}
        await ws.send(json.dumps({"type": "fixtures"}))
        fixtures = await until(ws, "fixtures")
        samples: list[dict[str, Any]] = []
        for index, encoded in enumerate(fixtures["audio"] * args.repeats):
            sample = await remote_sample(
                ws, base64.b64decode(encoded), index + 100, args.endpoint_wait_ms
            )
            sample["fixture"] = index % len(fixtures["audio"])
            samples.append(sample)
            print(json.dumps({"index": index, **sample}, ensure_ascii=False), flush=True)
        return {
            **report,
            "scope": args.label,
            "samples": samples,
            "summary": summarize(samples),
            "endpointing": "simulated_wait_not_real_vad",
            "endpoint_wait_ms": args.endpoint_wait_ms,
            "warmup": "fixture_tts_prepared_before_first_sample",
            "fixture_source": "synthetic_tts_not_real_caller",
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--token-file", required=True)
    parser.add_argument(
        "--mode",
        choices=["probe", "load", "wait", "status", "local", "remote", "tts", "optimize"],
        default="probe",
    )
    parser.add_argument("--reset-hop", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--variant", help="选择已配置的远端实验方案")
    parser.add_argument("--repeats", type=int, choices=[1, 2, 3], default=2)
    parser.add_argument("--endpoint-wait-ms", type=int, default=200)
    parser.add_argument("--prepare-timeout-seconds", type=float, default=10800)
    parser.add_argument("--label", default="remote_memory_terminal")
    parser.add_argument("--output")
    args = parser.parse_args()
    result = asyncio.run(run(args))
    content = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.touch(mode=0o600, exist_ok=True)
        output.write_text(content + "\n")
    print(content, flush=True)


if __name__ == "__main__":
    main()
