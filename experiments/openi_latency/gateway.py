"""单端口、单会话的实验网关。模型进程通过本机管道连接，无端口穿透。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import struct
import sys
import time
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect

ROOT = Path(__file__).resolve().parent
PREFIX = "OPENI_EVENT:"


class Runner:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.phase = "proxy_ready"
        self.process: asyncio.subprocess.Process | None = None
        self.setup_task: asyncio.Task[None] | None = None
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=64)
        self.logs: deque[str] = deque(maxlen=45)
        self.report: dict[str, Any] | None = None
        self.busy = False
        self.generation = 0

    async def publish(self, event: dict[str, Any]) -> None:
        if event["type"] in {"status", "ready", "error"}:
            self.phase = event.get("phase", event["type"])
        if event["type"] == "report":
            self.report = event
        if self.busy:
            await self.events.put(event)

    async def start(self) -> None:
        if self.setup_task is None:
            self.setup_task = asyncio.create_task(self._prepare())

    async def _prepare(self) -> None:
        self.phase = "installing_worker"
        self.process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-u",
            str(ROOT / "prepare_worker.py"),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
            limit=4 * 1024 * 1024,
        )
        assert self.process.stdout is not None
        while line := await self.process.stdout.readline():
            text = line.decode(errors="replace").rstrip()
            if text.startswith(PREFIX):
                await self.publish(json.loads(text[len(PREFIX) :]))
            else:
                self.logs.append(text[-1000:])
        code = await self.process.wait()
        await self.publish({"type": "error", "phase": "worker_exited", "exit_code": code})

    async def send(self, message: dict[str, Any]) -> None:
        if self.process is None or self.process.stdin is None or self.phase != "ready":
            raise ValueError("worker_not_ready")
        self.process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode())
        await self.process.stdin.drain()

    async def close(self) -> None:
        if self.process is not None and self.process.returncode is None:
            import signal

            os.killpg(self.process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(self.process.wait(), 10)
            except TimeoutError:
                os.killpg(self.process.pid, signal.SIGKILL)
                await self.process.wait()
        if self.setup_task is not None:
            self.setup_task.cancel()
            await asyncio.gather(self.setup_task, return_exceptions=True)


def create_app(config: dict[str, Any], runner: Runner | None = None) -> FastAPI:
    worker = runner or Runner(config)
    started = time.monotonic()

    def authorized(token: str) -> bool:
        expected = config.get("token_sha256", "")
        return bool(expected) and hmac.compare_digest(
            hashlib.sha256(token.encode()).hexdigest(), expected
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        await worker.close()

    app = FastAPI(lifespan=lifespan)
    app.state.runner = worker

    @app.get("/")
    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"service": "openi-latency-prototype", "phase": worker.phase}

    @app.get("/status")
    async def status(authorization: str = Header(default="")) -> dict[str, Any]:
        if not authorized(authorization.removeprefix("Bearer ")):
            raise HTTPException(401, "unauthorized")
        return {"phase": worker.phase, "logs": list(worker.logs), "report": worker.report}

    @app.websocket("/ws")
    async def websocket(ws: WebSocket) -> None:
        await ws.accept()
        try:
            authentication = await asyncio.wait_for(ws.receive_json(), 5)
            if not authorized(str(authentication.get("token", ""))):
                await ws.close(1008, "unauthorized")
                return
            if worker.busy or time.monotonic() - started > config["lifetime_seconds"]:
                await ws.close(1013, "busy_or_expired")
                return
            worker.busy = True
            while not worker.events.empty():
                worker.events.get_nowait()
            await ws.send_json({"type": "hello", "phase": worker.phase})

            async def outgoing() -> None:
                while True:
                    event = await worker.events.get()
                    if event["type"] == "audio":
                        if event["generation"] != worker.generation:
                            continue
                        packet = struct.pack("<II", event["generation"], event["sample_rate"])
                        await ws.send_bytes(packet + base64.b64decode(event["pcm"]))
                    else:
                        await ws.send_json(event)

            sender = asyncio.create_task(outgoing())
            try:
                while True:
                    message = await ws.receive_json()
                    kind = message.get("type")
                    if kind == "ping":
                        await ws.send_json({"type": "pong", "sequence": message.get("sequence")})
                    elif kind == "load":
                        await worker.start()
                        await ws.send_json({"type": "status", "phase": worker.phase})
                    elif kind in {"begin", "audio", "end", "fixtures", "local_benchmark", "cancel"}:
                        if kind in {"begin", "cancel"}:
                            worker.generation = int(message["generation"])
                        if kind == "audio":
                            if len(message.get("pcm", "")) > 128000:
                                raise ValueError("audio_packet_too_large")
                            if message["generation"] != worker.generation:
                                continue
                        await worker.send(message)
                        if kind == "cancel":
                            await ws.send_json(
                                {"type": "cancelled", "generation": worker.generation}
                            )
                    else:
                        raise ValueError("unsupported_command")
            finally:
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)
                worker.busy = False
                if worker.phase == "ready":
                    worker.generation += 1
                    await worker.send({"type": "cancel", "generation": worker.generation})
        except (WebSocketDisconnect, TimeoutError):
            pass
        except (ValueError, KeyError):
            await ws.close(1008, "invalid_or_unready_request")

    return app


if __name__ == "__main__":
    import uvicorn

    settings = json.loads((ROOT / "settings.run.json").read_text())
    uvicorn.run(
        create_app(settings),
        host="0.0.0.0",
        port=int(os.environ["OPENI_SELF_PORT"]),
        access_log=False,
    )
