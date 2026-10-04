"""HTTP 服务和同源前端。"""

import asyncio
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal

import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger
from pydantic import BaseModel, Field

from llmautotel.config import RuntimeConfig
from llmautotel.models import AppSettings, CallRecord
from llmautotel.sessions import SessionManager
from llmautotel.store import Store


def create_app(runtime: RuntimeConfig | None = None) -> FastAPI:
    config = runtime or RuntimeConfig.from_environment()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.store = Store(config.data_dir)
        await app.state.store.finalize_unfinished_calls()
        app.state.settings_lock = asyncio.Lock()
        app.state.sessions = SessionManager(
            app.state.store, connection_timeout_seconds=config.connection_timeout_seconds
        )
        try:
            yield
        finally:
            await app.state.sessions.close()

    app = FastAPI(title="LLMAutoTel", lifespan=lifespan)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, error: RequestValidationError) -> JSONResponse:
        # FastAPI's default errors echo input values, which may include API keys.
        issues = [
            {"loc": item["loc"], "msg": item["msg"], "type": item["type"]}
            for item in error.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": issues})

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/settings")
    async def get_settings(request: Request) -> dict[str, Any]:
        return (await request.app.state.store.get_settings()).public()

    @app.put("/api/settings")
    async def put_settings(settings: AppSettings, request: Request) -> dict[str, Any]:
        async with request.app.state.settings_lock:
            saved = await request.app.state.store.save_settings(settings)
        return saved.public()

    @app.post("/api/calls", status_code=201)
    async def start_call(request: Request) -> dict[str, Any]:
        return await request.app.state.sessions.start()

    @app.get("/api/calls/active")
    async def active_call(request: Request) -> CallRecord | None:
        return await request.app.state.sessions.active_record()

    @app.get("/api/calls")
    async def list_calls(request: Request) -> list[dict[str, Any]]:
        return await request.app.state.store.list_calls()

    @app.get("/api/calls/{call_id}")
    async def get_call(call_id: str, request: Request) -> CallRecord:
        record = await request.app.state.store.get_call(call_id)
        if record is None:
            raise HTTPException(404, "通话记录不存在")
        return record

    @app.post("/api/calls/{call_id}/end")
    async def end_call(
        call_id: str, request: Request, body: EndRequest | None = None
    ) -> CallRecord:
        return await request.app.state.sessions.end(call_id, body.reason if body else "user_hangup")

    @app.delete("/api/calls/{call_id}", status_code=204)
    async def delete_call(call_id: str, request: Request) -> Response:
        await request.app.state.sessions.delete(call_id)
        return Response(status_code=204)

    @app.post("/api/offer")
    async def offer(body: OfferRequest, call_id: str, request: Request) -> dict[str, str]:
        return await request.app.state.sessions.offer(call_id, body.model_dump(exclude_none=True))

    @app.patch("/api/offer")
    async def patch(body: PatchRequest, call_id: str, request: Request) -> dict[str, str]:
        await request.app.state.sessions.patch(call_id, body.model_dump())
        return {"status": "ok"}

    if config.frontend_dir.exists():
        app.mount("/", StaticFiles(directory=config.frontend_dir, html=True), name="frontend")
    return app


def run() -> None:
    config = RuntimeConfig.from_environment()
    logger.remove()
    logger.add(sys.stderr, level="WARNING")
    uvicorn.run(create_app(config), host=config.host, port=config.port, log_level="warning")


class EndRequest(BaseModel):
    reason: Literal["user_hangup", "connection_lost"] = "user_hangup"


class OfferRequest(BaseModel):
    sdp: str = Field(min_length=1, max_length=100000)
    type: Literal["offer"]
    pc_id: str | None = None
    restart_pc: bool = False
    request_data: dict[str, Any] | None = None


class CandidateRequest(BaseModel):
    candidate: str
    sdp_mid: str
    sdp_mline_index: int


class PatchRequest(BaseModel):
    pc_id: str
    candidates: list[CandidateRequest]
