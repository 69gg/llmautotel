"""Asterisk 22.8+ ARI 呼叫控制与原生 chan_websocket 双向媒体。"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import re
from collections.abc import Coroutine
from contextlib import suppress
from typing import Any
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

import httpx
from websockets.asyncio.client import ClientConnection, connect

from llmautotel.telephony.base import MediaCallbacks, TelephonyError
from llmautotel.telephony.incoming import IncomingCall, IncomingCallbacks, IncomingEvents
from llmautotel.telephony.settings import AsteriskSettings

_CREATE_PATHS = {"/channels", "/bridges", "/channels/externalMedia"}


class AsteriskDriver:
    """仅显式 start 发起一通电话；ARI 与媒体连接相互独立。"""

    sample_rate = 16000

    def __init__(
        self,
        settings: AsteriskSettings,
        callbacks: MediaCallbacks,
        *,
        client: httpx.AsyncClient | None = None,
        incoming: IncomingCall | None = None,
    ) -> None:
        self._settings = settings.model_copy(deep=True)
        self._callbacks = callbacks
        self._incoming = incoming
        self._client = client
        self._owns_client = client is None
        self._events: ClientConnection | None = None
        self._media: ClientConnection | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._creates: set[asyncio.Task[dict[str, Any]]] = set()
        self._unconfirmed_create = False
        self._start_task: asyncio.Task[Any] | None = None
        self._cleanup_task: asyncio.Task[None] | None = None
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
        if self._incoming is not None:
            if not settings.inbound_enabled or settings.missing_inbound_fields():
                raise TelephonyError("Asterisk 入呼未启用或 ARI 配置不完整。")
            if self._incoming.provider != "asterisk" or self._incoming.events is None:
                raise TelephonyError("Asterisk 来电事件订阅无效。")
            if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", self._incoming.remote_id):
                raise TelephonyError("Asterisk 来电通道 ID 无效。")
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", call_id):
                raise TelephonyError("通话 ID 格式无效。")
            if not urlsplit(settings.ari_url).path.rstrip("/").endswith("/ari"):
                raise TelephonyError("ARI 地址须以 /ari 结尾。")
            return
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
        if method == "POST" and path in _CREATE_PATHS:
            # 取消本地等待不等于撤销远端创建。保留请求到收到结果或独立请求超时，
            # close 必须在这些请求结束后才删除预先分配的资源 ID。
            task = asyncio.create_task(self._perform_request(method, path, params=params))
            self._creates.add(task)
            return await asyncio.shield(task)
        return await self._perform_request(method, path, params=params, missing_ok=missing_ok)

    async def _perform_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str | int] | None = None,
        missing_ok: bool = False,
    ) -> dict[str, Any]:
        assert self._client is not None
        try:
            async with asyncio.timeout(self._settings.media_timeout_seconds):
                response = await self._client.request(
                    method,
                    self._settings.ari_url.rstrip("/") + path,
                    params=params,
                    headers={"Authorization": self._authorization()},
                    timeout=self._settings.media_timeout_seconds,
                )
        except (httpx.HTTPError, TimeoutError):
            if method == "POST" and path in _CREATE_PATHS:
                self._unconfirmed_create = True
            # 不透出 URL、响应正文或认证信息，避免本地界面/历史泄露凭据。
            raise TelephonyError("Asterisk ARI 网络请求失败或超时。") from None
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
        self._channel_id = self._incoming.remote_id if self._incoming else f"{call_id}-pstn"
        self._external_id = f"{call_id}-media"
        self._bridge_id = f"{call_id}-bridge"
        if self._client is None:
            self._client = httpx.AsyncClient(trust_env=False)
        self._start_task = asyncio.current_task()
        try:
            if self._incoming is not None:
                assert self._incoming.events is not None
                self._incoming.events.add_channel(self._external_id)
                self._spawn(self._read_events())
                await self._request("POST", f"/channels/{self._channel_id}/answer")
                self._answered = True
                self._media_preparing = True
                self._spawn(self._prepare_media())
                self._spawn(self._watch_answer())
                return
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
        events = self._incoming.events if self._incoming else self._events
        assert events is not None
        try:
            async for raw in events:
                if self._closed:
                    return
                event = raw if isinstance(raw, dict) else json.loads(raw)
                channel = event.get("channel", {})
                channel_id = channel.get("id")
                if channel_id == self._channel_id:
                    if event.get("type") in {"StasisStart", "ChannelStateChange"}:
                        if channel.get("state") == "Up":
                            self._answered = True
                            # Up 状态事件可能先于 StasisStart；桥接要求通道已进入应用。
                            if (
                                self._incoming is None
                                and event.get("type") == "StasisStart"
                                and not self._media_preparing
                            ):
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
                        "app": self._settings.incoming_app
                        if self._incoming
                        else self._settings.app,
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
        try:
            await self.close()
        except TelephonyError as exc:
            message = f"{message} {exc}"
        finally:
            try:
                await self._callbacks.on_error(message)
            finally:
                await self._notify_ended("provider_error")

    async def _notify_ended(self, reason: str) -> None:
        if not self._ended:
            self._ended = True
            await self._callbacks.on_ended(reason)

    async def _finish(self, reason: str) -> None:
        try:
            await self.close()
        except TelephonyError as exc:
            reason = "provider_error"
            await self._callbacks.on_error(str(exc))
            raise
        finally:
            await self._notify_ended(reason)

    async def hangup(self) -> None:
        await self._finish("hangup")

    async def close(self) -> None:
        caller = asyncio.current_task()
        async with self._close_lock:
            if self._cleanup_task is not None and self._cleanup_task.done():
                return
            if self._cleanup_task is None:
                self._closed = True
                self._invalidate_marks()
                self._cleanup_task = asyncio.create_task(self._cleanup(caller))
            task = self._cleanup_task
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # driver 自己的任务由 cleanup 取消，必须立即退出，避免互相等待。
            if caller in self._tasks or caller is self._start_task:
                raise
            # 上层取消只取消等待者；继续确认远端创建与删除后才释放本通槽位。
            await asyncio.shield(task)
            raise

    async def _cleanup(self, caller: asyncio.Task[Any] | None) -> None:
        tasks = [task for task in self._tasks if task is not caller]
        if self._start_task is not None and self._start_task is not caller:
            tasks.append(self._start_task)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        failed = False
        try:
            # 创建请求有自己的 media_timeout，不能被较短的 cleanup 预算截断。
            if self._creates:
                await asyncio.gather(*self._creates, return_exceptions=True)
                self._creates.clear()
            failed = self._unconfirmed_create
            operations: list[Coroutine[Any, Any, Any]] = [
                websocket.close()
                for websocket in (self._media, self._events)
                if websocket is not None
            ]
            if self._started and self._client is not None:
                operations.extend(
                    self._request("DELETE", path, missing_ok=True)
                    for path in (
                        f"/channels/{self._channel_id}",
                        f"/channels/{self._external_id}",
                        f"/bridges/{self._bridge_id}",
                    )
                )
            try:
                async with asyncio.timeout(self._settings.cleanup_timeout_seconds):
                    results = await asyncio.gather(*operations, return_exceptions=True)
                    failed = failed or any(isinstance(result, BaseException) for result in results)
            except TimeoutError:
                failed = True
        finally:
            if self._incoming is not None:
                if not self._started:
                    try:
                        await self._incoming.close()
                    except Exception:
                        failed = True
                else:
                    self._incoming.release()
            if self._owns_client and self._client is not None:
                try:
                    await self._client.aclose()
                except Exception:
                    failed = True
        if failed:
            raise TelephonyError("Asterisk 远端资源清理失败或超时，请确认 PBX 电话状态。")


class AsteriskIncomingListener:
    """独占入呼 ARI 应用事件连接，仅接管带专属 Stasis 参数的通道。"""

    def __init__(
        self,
        settings: AsteriskSettings,
        callbacks: IncomingCallbacks,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings.model_copy(deep=True)
        self.callbacks = callbacks
        self.connected = False
        self.last_error: str | None = None
        # 复用 ARI 请求、头鉴权和 WS URL，实现不启动其拨号逻辑。
        self._control = AsteriskDriver(
            self.settings,
            MediaCallbacks(self._noop, self._audio_noop, self._reason_noop, callbacks.on_error),
            client=client,
        )
        self._socket: ClientConnection | None = None
        self._worker: asyncio.Task[None] | None = None
        self._stop_task: asyncio.Task[None] | None = None
        self._admissions: set[asyncio.Task[None]] = set()
        self._subscriptions: set[IncomingEvents] = set()
        self._seen: set[str] = set()
        self._closing = False
        self._closed = False

    @staticmethod
    async def _noop() -> None:
        return

    @staticmethod
    async def _audio_noop(audio: bytes, rate: int) -> None:
        return

    @staticmethod
    async def _reason_noop(reason: str) -> None:
        return

    async def _connect(self) -> None:
        event_url = (
            self._control._websocket_url("/events")
            + "?"
            + urlencode({"app": self.settings.incoming_app})
        )
        self._socket = await self._control._connect(event_url, "ari")
        self.connected = True
        self.last_error = None

    async def start(self) -> None:
        if self._worker is not None or self._closing:
            raise TelephonyError("Asterisk 来电监听器不能重复启动。")
        if not self.settings.enabled or not self.settings.inbound_enabled:
            raise TelephonyError("Asterisk 来电接听尚未启用。")
        if self.settings.missing_inbound_fields():
            raise TelephonyError("Asterisk 入呼 ARI 配置不完整。")
        if not urlsplit(self.settings.ari_url).path.rstrip("/").endswith("/ari"):
            raise TelephonyError("ARI 地址须以 /ari 结尾。")
        if self._control._client is None:
            self._control._client = httpx.AsyncClient(trust_env=False)
        try:
            await self._connect()
        except Exception:
            self.last_error = "Asterisk 入呼 ARI 事件连接失败。"
            await self._control.close()
            raise TelephonyError(self.last_error) from None
        self._worker = asyncio.create_task(self._run())

    def _release(self, subscription: IncomingEvents) -> None:
        self._subscriptions.discard(subscription)
        if self._closing and not self._subscriptions:
            self._schedule_stop()

    def _schedule_stop(self) -> asyncio.Task[None]:
        if self._stop_task is None:
            self._stop_task = asyncio.create_task(self._stop())
            self._stop_task.add_done_callback(self._stop_completed)
        return self._stop_task

    def _stop_completed(self, task: asyncio.Task[None]) -> None:
        if not task.cancelled() and task.exception() is not None:
            self.last_error = "Asterisk 来电监听连接清理失败。"

    async def _hangup(self, channel_id: str, reason: str) -> None:
        await self._control._request(
            "DELETE",
            "/channels/" + quote(channel_id, safe=""),
            params={"reason": "busy" if reason == "busy" else "normal"},
            missing_ok=True,
        )

    async def _admit(self, call: IncomingCall) -> None:
        try:
            accepted = False if self._closing else await self.callbacks.on_call(call)
            if not accepted:
                await call.reject()
        except asyncio.CancelledError:
            await call.close()
            raise
        except Exception:
            with suppress(Exception):
                await call.close()
            await self.callbacks.on_error("Asterisk 来电接管或拒接失败。")

    def _event(self, event: dict[str, Any]) -> None:
        channel = event.get("channel", {})
        if not isinstance(channel, dict):
            return
        channel_id = channel.get("id", "")
        for subscription in tuple(self._subscriptions):
            if channel_id in subscription.channels:
                subscription.put(event)
        if (
            event.get("type") != "StasisStart"
            or event.get("application") != self.settings.incoming_app
            or event.get("args", []) != [self.settings.inbound_marker]
            or not isinstance(channel_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", channel_id)
            or channel_id in self._seen
        ):
            return
        destination = str(channel.get("dialplan", {}).get("exten", ""))
        self._seen.add(channel_id)
        events = IncomingEvents(channel_id, self._release)
        self._subscriptions.add(events)
        events.put(event)

        async def hangup(reason: str) -> None:
            await self._hangup(channel_id, reason)

        call = IncomingCall(
            "asterisk",
            channel_id,
            str(channel.get("caller", {}).get("number", "")),
            destination,
            events=events,
            hangup=hangup,
        )
        allowed = not self.settings.inbound_numbers or destination in self.settings.inbound_numbers
        task = asyncio.create_task(self._admit_allowed(call, allowed))
        self._admissions.add(task)
        task.add_done_callback(self._admissions.discard)

    async def _admit_allowed(self, call: IncomingCall, allowed: bool) -> None:
        if allowed:
            await self._admit(call)
        else:
            try:
                await call.reject()
            except Exception:
                await self.callbacks.on_error("Asterisk 来电拒接失败。")

    async def _run(self) -> None:
        try:
            while not self._closed:
                try:
                    assert self._socket is not None
                    async for raw in self._socket:
                        self._event(json.loads(raw))
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass
                self.connected = False
                if self._socket is not None:
                    with suppress(Exception):
                        await self._socket.close()
                for subscription in tuple(self._subscriptions):
                    subscription.disconnect()
                if self._closing:
                    return
                self.last_error = "Asterisk 来电监听连接中断，正在重连。"
                with suppress(Exception):
                    await self.callbacks.on_error(self.last_error)
                while not self._closing:
                    await asyncio.sleep(self.settings.inbound_reconnect_seconds)
                    try:
                        await self._connect()
                        break
                    except Exception:
                        self.last_error = "Asterisk 来电监听重连失败。"
        finally:
            self.connected = False

    async def _stop(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.connected = False
        if self._worker is not None and self._worker is not asyncio.current_task():
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
        failed = False
        try:
            if self._socket is not None:
                await self._socket.close()
        except Exception:
            failed = True
        finally:
            try:
                await self._control.close()
            except Exception:
                failed = True
        if failed:
            raise TelephonyError("Asterisk 来电监听连接清理失败。")

    async def close(self) -> None:
        self._closing = True
        await asyncio.gather(*tuple(self._admissions), return_exceptions=True)
        # 入呼驱动借用本连接；关闭接听入口后，已有单通完成时才关 WS。
        if not self._subscriptions:
            await asyncio.shield(self._schedule_stop())
