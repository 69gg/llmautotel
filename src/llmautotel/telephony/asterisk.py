"""Asterisk 22.8+ ARI 外呼与原生 chan_websocket 双向媒体。"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import re
from collections.abc import Coroutine
from typing import Any
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

import httpx
from websockets.asyncio.client import ClientConnection, connect

from llmautotel.telephony.base import MediaCallbacks, TelephonyError
from llmautotel.telephony.settings import AsteriskSettings


class AsteriskDriver:
    """仅显式 start 发起一通电话；ARI 与媒体连接相互独立。"""

    sample_rate = 16000

    def __init__(
        self,
        settings: AsteriskSettings,
        callbacks: MediaCallbacks,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings.model_copy(deep=True)
        self._callbacks = callbacks
        self._client = client
        self._owns_client = client is None
        self._events: ClientConnection | None = None
        self._media: ClientConnection | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._start_task: asyncio.Task[Any] | None = None
        self._send_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._can_send = asyncio.Event()
        self._can_send.set()
        self._ready = asyncio.Event()
        self._external_stasis = asyncio.Event()
        self._started = False
        self._closed = False
        self._ended = False
        self._answered = False
        self._media_preparing = False
        self._buffering = False
        self._frame_size = 640
        self._epoch = 0
        self._mark_sequence = 0
        self._marks: dict[str, asyncio.Future[None]] = {}
        self._channel_id = ""
        self._external_id = ""
        self._bridge_id = ""

    def _validate(self, number: str, call_id: str) -> None:
        settings = self._settings
        if not settings.enabled:
            raise TelephonyError("Asterisk provider 尚未启用。")
        if not all(
            (settings.ari_url, settings.username, settings.password, settings.endpoint_template)
        ):
            raise TelephonyError("Asterisk ARI 地址、用户名、密码和拨号模板必须填写。")
        url = urlsplit(settings.ari_url)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
            or not url.path.rstrip("/").endswith("/ari")
        ):
            raise TelephonyError("ARI 地址须为不含凭据或查询参数、以 /ari 结尾的 HTTP 地址。")
        if not re.fullmatch(r"\+?[0-9]{7,15}", number):
            raise TelephonyError("被叫号码只允许 7 至 15 位数字和可选的国际区号加号。")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", call_id):
            raise TelephonyError("通话 ID 格式无效。")
        template = settings.endpoint_template
        if template.count("{number}") != 1 or "{" in template.replace("{number}", ""):
            raise TelephonyError("拨号模板必须且只能包含一个 {number} 占位符。")
        if "}" in template.replace("{number}", "") or any(c in template for c in "\r\n"):
            raise TelephonyError("拨号模板格式无效。")

    def _authorization(self) -> str:
        password = self._settings.password
        assert password is not None
        credentials = f"{self._settings.username}:{password.get_secret_value()}"
        return "Basic " + base64.b64encode(credentials.encode()).decode()

    def _websocket_url(self, path: str, *, media: bool = False) -> str:
        url = urlsplit(self._settings.ari_url.rstrip("/"))
        base_path = url.path.rsplit("/", 1)[0] if media else url.path
        return urlunsplit(
            ("wss" if url.scheme == "https" else "ws", url.netloc, base_path + path, "", "")
        )

    async def _connect(self, url: str, protocol: str) -> ClientConnection:
        return await connect(
            url,
            subprotocols=[protocol],
            additional_headers={"Authorization": self._authorization()},
            proxy=None,
            compression=None,
            open_timeout=self._settings.media_timeout_seconds,
            close_timeout=2,
            max_size=65500,
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str | int] | None = None,
        missing_ok: bool = False,
    ) -> dict[str, Any]:
        assert self._client is not None
        try:
            response = await self._client.request(
                method,
                self._settings.ari_url.rstrip("/") + path,
                params=params,
                headers={"Authorization": self._authorization()},
                timeout=self._settings.media_timeout_seconds,
            )
        except httpx.HTTPError as exc:
            # 不透出 URL、响应正文或认证信息，避免本地界面/历史泄露凭据。
            raise TelephonyError("Asterisk ARI 网络请求失败。") from exc
        if missing_ok and response.status_code == 404:
            return {}
        if not response.is_success:
            raise TelephonyError(f"Asterisk ARI 请求失败（HTTP {response.status_code}）。")
        if not response.content:
            return {}
        try:
            result = response.json()
        except ValueError as exc:
            raise TelephonyError("Asterisk ARI 返回了无效 JSON。") from exc
        if not isinstance(result, dict):
            raise TelephonyError("Asterisk ARI 返回了无效对象。")
        return result

    def _spawn(self, coroutine: Coroutine[Any, Any, None]) -> None:
        task: asyncio.Task[None] = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def start(self, number: str, call_id: str) -> None:
        self._validate(number, call_id)
        if self._started or self._closed:
            raise TelephonyError("Asterisk driver 不能重复启动。")
        self._started = True
        self._channel_id = f"{call_id}-pstn"
        self._external_id = f"{call_id}-media"
        self._bridge_id = f"{call_id}-bridge"
        if self._client is None:
            self._client = httpx.AsyncClient(trust_env=False)
        self._start_task = asyncio.current_task()
        try:
            event_url = (
                self._websocket_url("/events") + "?" + urlencode({"app": self._settings.app})
            )
            self._events = await self._connect(event_url, "ari")
            self._spawn(self._read_events())
            params: dict[str, str | int] = {
                "endpoint": self._settings.endpoint_template.replace("{number}", number),
                "app": self._settings.app,
                "channelId": self._channel_id,
                "timeout": math.ceil(self._settings.ring_timeout_seconds),
            }
            if self._settings.caller_id:
                params["callerId"] = self._settings.caller_id
            await self._request("POST", "/channels", params=params)
            if not self._closed:
                self._spawn(self._watch_answer())
        except asyncio.CancelledError:
            # close 会取消并等待尚在握手/创建通道的 start；此处不重入清理锁。
            if not self._closed:
                await self.close()
            raise
        except Exception as exc:
            await self.close()
            if isinstance(exc, TelephonyError):
                raise
            raise TelephonyError("Asterisk ARI 事件连接失败。") from exc
        finally:
            self._start_task = None

    async def _watch_answer(self) -> None:
        try:
            # ARI 的 timeout 控制振铃；额外留媒体初始化窗口，不把已接听误判无人接听。
            timeout = self._settings.ring_timeout_seconds + self._settings.media_timeout_seconds
            await asyncio.wait_for(self._ready.wait(), timeout)
        except TimeoutError:
            if not self._closed:
                await self._finish("no_answer" if not self._answered else "media_error")

    async def _read_events(self) -> None:
        assert self._events is not None
        try:
            async for raw in self._events:
                if self._closed:
                    return
                event = json.loads(raw)
                channel = event.get("channel", {})
                channel_id = channel.get("id")
                if channel_id == self._channel_id:
                    if event.get("type") in {"StasisStart", "ChannelStateChange"}:
                        if channel.get("state") == "Up":
                            self._answered = True
                            # Up 状态事件可能先于 StasisStart；桥接要求通道已进入应用。
                            if event.get("type") == "StasisStart" and not self._media_preparing:
                                self._media_preparing = True
                                self._spawn(self._prepare_media())
                    elif event.get("type") in {"ChannelDestroyed", "StasisEnd"}:
                        cause = event.get("cause")
                        reason = {17: "busy", 18: "no_answer", 19: "no_answer", 21: "rejected"}.get(
                            cause, "disconnected" if self._answered else "call_failed"
                        )
                        await self._finish(reason)
                        return
                elif channel_id == self._external_id:
                    if event.get("type") == "StasisStart":
                        self._external_stasis.set()
                    elif event.get("type") in {"ChannelDestroyed", "StasisEnd"}:
                        await self._finish("disconnected")
                        return
            if not self._closed:
                await self._finish("disconnected")
        except asyncio.CancelledError:
            raise
        except Exception:
            if not self._closed:
                await self._fail("Asterisk ARI 事件连接中断。")

    async def _prepare_media(self) -> None:
        try:
            async with asyncio.timeout(self._settings.media_timeout_seconds):
                await self._request(
                    "POST",
                    "/bridges",
                    params={"bridgeId": self._bridge_id, "type": "mixing,proxy_media"},
                )
                await self._request(
                    "POST",
                    "/channels/externalMedia",
                    params={
                        "channelId": self._external_id,
                        "app": self._settings.app,
                        "external_host": "INCOMING",
                        "transport": "websocket",
                        "encapsulation": "none",
                        "connection_type": "server",
                        "format": "slin16",
                        "direction": "both",
                        "transport_data": "f(json)",
                    },
                )
                variable = await self._request(
                    "GET",
                    f"/channels/{self._external_id}/variable",
                    params={"variable": "MEDIA_WEBSOCKET_CONNECTION_ID"},
                )
                connection_id = variable.get("value")
                if not isinstance(connection_id, str) or not connection_id:
                    raise TelephonyError("Asterisk 没有返回媒体连接 ID。")
                self._media = await self._connect(
                    self._websocket_url("/media/" + quote(connection_id, safe=""), media=True),
                    "media",
                )
                raw = await self._media.recv()
                if not isinstance(raw, str):
                    raise TelephonyError("Asterisk 首个媒体消息不是 MEDIA_START。")
                event = json.loads(raw)
                if (
                    event.get("event") != "MEDIA_START"
                    or event.get("channel_id") != self._external_id
                    or event.get("format") != "slin16"
                ):
                    raise TelephonyError("Asterisk 媒体通道或音频格式不匹配。")
                frame_size = event.get("optimal_frame_size")
                if (
                    not isinstance(frame_size, int)
                    or isinstance(frame_size, bool)
                    or frame_size < 2
                    or frame_size > 65500
                    or frame_size % 2
                ):
                    raise TelephonyError("Asterisk PCM 帧大小无效。")
                self._frame_size = frame_size
                # MEDIA_START 在媒体握手时发出，不代表 ARI 通道已进入 Stasis。
                await self._external_stasis.wait()
                await self._request(
                    "POST",
                    f"/bridges/{self._bridge_id}/addChannel",
                    params={"channel": f"{self._channel_id},{self._external_id}"},
                )
            if self._closed:
                return
            self._ready.set()
            self._spawn(self._read_media())
            await self._callbacks.on_ready()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._closed:
                message = str(exc) if isinstance(exc, TelephonyError) else "Asterisk 媒体接入失败。"
                await self._fail(message)

    async def _read_media(self) -> None:
        assert self._media is not None
        try:
            async for raw in self._media:
                if self._closed:
                    return
                if isinstance(raw, bytes):
                    if len(raw) % 2:
                        raise TelephonyError("Asterisk 输入 PCM 音频长度无效。")
                    await self._callbacks.on_audio(raw, self.sample_rate)
                    continue
                event = json.loads(raw)
                if event.get("channel_id") not in {None, self._external_id}:
                    continue
                kind = event.get("event")
                if kind == "MEDIA_XOFF":
                    self._can_send.clear()
                elif kind == "MEDIA_XON":
                    self._can_send.set()
                elif kind == "MEDIA_MARK_PROCESSED":
                    future = self._marks.pop(event.get("correlation_id"), None)
                    if future is not None and not future.done():
                        future.set_result(None)
            if not self._closed:
                await self._finish("disconnected")
        except asyncio.CancelledError:
            raise
        except Exception:
            if not self._closed:
                await self._fail("Asterisk 媒体连接中断或音频格式无效。")

    def _require_media(self) -> ClientConnection:
        if self._closed or not self._ready.is_set() or self._media is None:
            raise TelephonyError("Asterisk 被叫尚未接听或媒体连接已结束。")
        return self._media

    async def _command(self, command: str, correlation_id: str | None = None) -> None:
        media = self._require_media()
        payload = {"command": command}
        if correlation_id is not None:
            payload["correlation_id"] = correlation_id
        await media.send(json.dumps(payload))

    async def send_audio(self, audio: bytes) -> None:
        if len(audio) % 2:
            raise TelephonyError("输出音频必须为单声道 16 位 PCM。")
        epoch = self._epoch
        max_size = 65500 // self._frame_size * self._frame_size
        for offset in range(0, len(audio), max_size):
            await self._can_send.wait()
            async with self._send_lock:
                if epoch != self._epoch:
                    return
                media = self._require_media()
                async with asyncio.timeout(self._settings.media_timeout_seconds):
                    if not self._buffering:
                        await self._command("START_MEDIA_BUFFERING")
                        self._buffering = True
                    await media.send(audio[offset : offset + max_size])

    async def wait_played(self) -> None:
        epoch = self._epoch
        async with self._send_lock:
            self._require_media()
            if epoch != self._epoch:
                raise asyncio.CancelledError
            self._mark_sequence += 1
            token = f"{epoch}-{self._mark_sequence}"
            future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            self._marks[token] = future
            try:
                if self._buffering:
                    await self._command("STOP_MEDIA_BUFFERING", token)
                    self._buffering = False
                await self._command("MARK_MEDIA", token)
                await self._command("REPORT_QUEUE_DRAINED")
            except BaseException:
                self._marks.pop(token, None)
                future.cancel()
                raise
        try:
            await asyncio.wait_for(future, self._settings.media_timeout_seconds + 20)
        except TimeoutError as exc:
            raise TelephonyError("Asterisk 未确认语音播放完成。") from exc
        finally:
            self._marks.pop(token, None)

    def _invalidate_marks(self) -> None:
        self._epoch += 1
        for future in self._marks.values():
            future.cancel()
        self._marks.clear()
        # 释放XOFF等待者；它们会检查epoch并舍弃旧音频。
        self._can_send.set()

    async def flush(self) -> None:
        self._invalidate_marks()
        async with self._send_lock:
            self._buffering = False
            if not self._closed and self._ready.is_set():
                await self._command("FLUSH_MEDIA")

    async def _fail(self, message: str) -> None:
        await self.close()
        await self._callbacks.on_error(message)
        await self._notify_ended("provider_error")

    async def _notify_ended(self, reason: str) -> None:
        if not self._ended:
            self._ended = True
            await self._callbacks.on_ended(reason)

    async def _finish(self, reason: str) -> None:
        await self.close()
        await self._notify_ended(reason)

    async def hangup(self) -> None:
        await self._finish("hangup")

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self._invalidate_marks()
            current = asyncio.current_task()
            tasks = [task for task in self._tasks if task is not current]
            if self._start_task is not None and self._start_task is not current:
                tasks.append(self._start_task)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for websocket in (self._media, self._events):
                if websocket is not None:
                    await websocket.close()
            if self._started and self._client is not None:
                # 请求前已分配固定ID；即使创建请求被取消也能回收服务端资源。
                for path in (
                    f"/channels/{self._channel_id}",
                    f"/channels/{self._external_id}",
                    f"/bridges/{self._bridge_id}",
                ):
                    try:
                        await self._request("DELETE", path, missing_ok=True)
                    except TelephonyError:
                        pass
            if self._owns_client and self._client is not None:
                await self._client.aclose()
