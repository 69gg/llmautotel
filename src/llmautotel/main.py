"""HTTP 服务和同源前端。"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from llmautotel.config import RuntimeConfig
from llmautotel.models import AppSettings
from llmautotel.store import Store


def create_app(runtime: RuntimeConfig | None = None) -> FastAPI:
    config = runtime or RuntimeConfig.from_environment()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.store = Store(config.data_dir)
        app.state.settings_lock = asyncio.Lock()
        yield

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

    if config.frontend_dir.exists():
        app.mount("/", StaticFiles(directory=config.frontend_dir, html=True), name="frontend")
    return app


def run() -> None:
    config = RuntimeConfig.from_environment()
    uvicorn.run(create_app(config), host=config.host, port=config.port, log_level="warning")
