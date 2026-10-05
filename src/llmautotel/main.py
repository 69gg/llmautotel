"""HTTP 服务和同源前端。"""

import asyncio
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal

import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger
from pydantic import BaseModel, Field

from llmautotel.config import RuntimeConfig
from llmautotel.models import AppSettings, CallRecord
from llmautotel.sessions import SessionManager
from llmautotel.store import Store
from llmautotel.telephony.catalog import provider_catalog
from llmautotel.telephony.service import IncomingService
from llmautotel.telephony.settings import TelephonyProviderName


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
        app.state.incoming = IncomingService(app.state.store, app.state.sessions)
        app.state.incoming.start()
        try:
            yield
        finally:
            app.state.incoming.pause()
            await app.state.sessions.close()
            await app.state.incoming.close()

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

    @app.get("/api/telephony/providers")
    async def telephone_providers(request: Request) -> list[dict[str, Any]]:
        settings = await request.app.state.store.get_settings()
        return provider_catalog(settings.telephony)

    @app.get("/api/telephony/inbound")
    async def incoming_status(request: Request) -> list[dict[str, Any]]:
        return await request.app.state.incoming.status()

    @app.post("/api/telephony/{provider}/inbound")
    async def incoming_cloud(
        provider: Literal["aliyun", "tencent"],
        request: Request,
        body: dict[str, Any],
        token: str | None = None,
    ) -> dict[str, Any]:
        from llmautotel.telephony.base import TelephonyError
        from llmautotel.telephony.cloud import AliyunCallClient, TencentCallClient
        from llmautotel.telephony.cloud_inbound import (
            authenticate_cloud_inbound,
            cloud_inbound_response,
            parse_cloud_inbound,
            reject_aliyun_inbound,
        )
        from llmautotel.telephony.incoming import IncomingCall

        settings = await request.app.state.store.get_settings()
        config = getattr(settings.telephony, provider)
        if not authenticate_cloud_inbound(
            provider,
            config,
            body,
            token,
            timestamp=request.headers.get("timestamp"),
            auth=request.headers.get("auth"),
        ):
            raise HTTPException(401, "来电接入未启用或鉴权失败")
        call = parse_cloud_inbound(provider, body)

        async def hangup(reason: str) -> None:
            client = AliyunCallClient(config) if provider == "aliyun" else TencentCallClient(config)
            try:
                await client.hangup(call.remote_id)
            finally:
                await client.close()

        incoming = IncomingCall(provider, call.remote_id, call.caller, call.callee, hangup=hangup)
        try:
            record = await request.app.state.sessions.accept_incoming(
                incoming, listener_settings=settings
            )
        except HTTPException:
            if provider == "tencent":
                return {"CallInBound": {}}
            try:
                await reject_aliyun_inbound(config, call.remote_id)
            except TelephonyError:
                raise HTTPException(503, "来电拒接失败，请检查云平台状态") from None
            raise HTTPException(409, "当前无法接听此来电，已请求挂断") from None
        response_config = config
        if provider == "tencent":
            response_config = config.model_copy(
                update={
                    "inbound_ai_agent_id": record.settings["telephony"]["tencent"][
                        "inbound_ai_agent_id"
                    ]
                }
            )
        return cloud_inbound_response(provider, response_config, record.id)

    @app.post("/api/calls", status_code=201)
    async def start_call(request: Request) -> dict[str, Any]:
        return await request.app.state.sessions.start()

    @app.post("/api/telephony/calls", status_code=201)
    async def start_phone(body: PhoneRequest, request: Request) -> dict[str, Any]:
        return await request.app.state.sessions.start_phone(body.provider, body.destination)

    async def cloud_completion(
        provider: Literal["aliyun", "tencent"],
        body: dict[str, Any],
        request: Request,
    ) -> Response:
        from llmautotel.telephony.base import TelephonyError

        try:
            voice = await request.app.state.sessions.phone_runtime(provider)
        except HTTPException as error:
            if (
                error.status_code != 409
                or await request.app.state.sessions.active_record() is not None
            ):
                raise
            from llmautotel.telephony.cloud_inbound import authenticated_gateway_probe

            settings = await request.app.state.store.get_settings()
            # 读取配置会让出执行权，期间可能已有浏览器或电话占用同一槽位。
            if await request.app.state.sessions.active_record() is not None:
                raise
            probe = authenticated_gateway_probe(
                settings, provider, body, request.headers.get("authorization")
            )
            if probe is None:
                raise
            if isinstance(probe, dict):
                return JSONResponse(probe, headers={"Cache-Control": "no-store"})
            return StreamingResponse(
                iter(probe),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
            )
        if not voice.authenticate(request.headers.get("authorization")):
            raise HTTPException(401, "模型网关鉴权失败")
        try:
            voice.validate_gateway(body)
        except TelephonyError as error:
            raise HTTPException(422, str(error)) from None
        return StreamingResponse(
            voice.cloud_gateway(body),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/telephony/aliyun/llm")
    async def aliyun_llm(body: dict[str, Any], request: Request) -> Response:
        return await cloud_completion("aliyun", body, request)

    @app.post("/api/telephony/tencent/llm/chat/completions")
    async def tencent_llm(body: dict[str, Any], request: Request) -> Response:
        return await cloud_completion("tencent", body, request)

    @app.post("/api/telephony/{provider}/events")
    async def phone_events(
        provider: Literal["aliyun", "tencent"],
        request: Request,
        body: dict[str, Any] | list[dict[str, Any]],
        token: str | None = None,
    ) -> dict[str, Any]:
        from dataclasses import replace

        from llmautotel.telephony.base import TelephonyError
        from llmautotel.telephony.cloud import (
            TencentCallClient,
            authenticate_cloud_event,
            parse_cloud_reports,
        )

        settings = await request.app.state.store.get_settings()
        provider_config = getattr(settings.telephony, provider)
        authorization = request.headers.get("authorization")
        try:
            voice = await request.app.state.sessions.phone_runtime(provider, allow_closing=True)
        except HTTPException:
            voice = None
        authorized = (
            voice.authenticate_event(token, authorization)
            if voice is not None
            else authenticate_cloud_event(provider_config, token, authorization)
        )
        if not authorized:
            raise HTTPException(401, "电话回执鉴权失败")
        consumed = await voice.handle_event(body) if voice is not None else False
        for report in parse_cloud_reports(provider, body):
            if consumed and report.remote_id == voice.remote_id:
                continue
            record = await request.app.state.store.get_phone_call(
                provider,
                report.local_id,
                report.remote_id,
            )
            if record is None or record.ended_at is None:
                continue
            if provider == "tencent":
                expected_app = record.settings["telephony"]["tencent"]["sdk_app_id"]
                if not isinstance(body, dict) or body.get("SdkAppId") != expected_app:
                    raise HTTPException(422, "电话回执应用标识不匹配")
            if provider == "tencent" and provider_config.enabled:
                client = TencentCallClient(provider_config)
                try:
                    report = replace(
                        report, transcript=await client.fetch_transcript(report.remote_id)
                    )
                except TelephonyError:
                    raise HTTPException(503, "云平台文字记录暂不可用，请重试回执") from None
                finally:
                    await client.close()
            await request.app.state.sessions.update_cloud_history(provider, report)
        return {"code": 0, "msg": "成功"} if provider == "aliyun" else {"ErrCode": 0, "ErrMsg": ""}

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


class PhoneRequest(BaseModel):
    provider: TelephonyProviderName
    destination: str = Field(min_length=7, max_length=16, pattern=r"^\+?[0-9]{7,15}$")


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
