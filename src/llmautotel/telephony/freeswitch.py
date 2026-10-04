"""FreeSWITCH 原生 ESL 呼叫控制和 unicast 双向 PCM，不依赖商业媒体模块。"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import sys
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from llmautotel.telephony.base import MediaCallbacks, TelephonyError

if TYPE_CHECKING:
    from llmautotel.telephony.settings import FreeswitchSettings


@dataclass(frozen=True)
class _ESLFrame:
    headers: dict[str, str]
    body: bytes = b""


async def _read_frame(reader: asyncio.StreamReader) -> _ESLFrame:
    """ESL 的 Content-Length 是字节数，事件可能与命令响应交错。"""
    headers: dict[str, str] = {}
    size = 0
    while True:
        line = await reader.readline()
        if not line:
            raise TelephonyError("FreeSWITCH ESL 连接已断开")
        size += len(line)
        if size > 65536:
            raise TelephonyError("FreeSWITCH ESL 消息头过大")
        if line in (b"\n", b"\r\n"):
            break
        name, separator, value = line.decode("utf-8", errors="replace").partition(":")
        if not separator:
            raise TelephonyError("FreeSWITCH ESL 消息头格式无效")
        headers[name.lower()] = value.strip()
    try:
        length = int(headers.get("content-length", "0"))
    except ValueError as exc:
        raise TelephonyError("FreeSWITCH ESL 消息长度无效") from exc
    if not 0 <= length <= 1024 * 1024:
        raise TelephonyError("FreeSWITCH ESL 消息长度超出限制")
    return _ESLFrame(headers, await reader.readexactly(length))


class _ESLConnection:
    """仅实现本 provider 所需的官方帧格式，单读任务分发响应和事件。"""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self.events: asyncio.Queue[_ESLFrame | None] = asyncio.Queue(maxsize=256)
        self._response: asyncio.Future[_ESLFrame] | None = None
        self._lock = asyncio.Lock()
        self._closed = False
        self._failed = False
        self._read_task: asyncio.Task[None] | None = None

    @classmethod
    async def connect(cls, host: str, port: int, password: str) -> _ESLConnection:
        if not password or "\n" in password or "\r" in password:
            raise TelephonyError("请设置有效的 FreeSWITCH ESL 密码")
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=10)
        connection = cls(reader, writer)
        try:
            challenge = await asyncio.wait_for(_read_frame(reader), timeout=10)
            if challenge.headers.get("content-type") != "auth/request":
                raise TelephonyError("FreeSWITCH ESL 拒绝连接或未发送认证请求")
            writer.write(f"auth {password}\n\n".encode())
            await writer.drain()
            reply = await asyncio.wait_for(_read_frame(reader), timeout=10)
            if not reply.headers.get("reply-text", "").startswith("+OK"):
                raise TelephonyError("FreeSWITCH ESL 认证失败")
            connection._read_task = asyncio.create_task(connection._read())
            return connection
        except BaseException:
            await connection.close()
            raise

    async def _read(self) -> None:
        try:
            while True:
                frame = await _read_frame(self.reader)
                content_type = frame.headers.get("content-type", "")
                if content_type.startswith("text/event-"):
                    self.events.put_nowait(frame)
                elif content_type == "text/disconnect-notice":
                    raise TelephonyError("FreeSWITCH ESL 连接已断开")
                elif content_type in ("api/response", "command/reply"):
                    if self._response is None or self._response.done():
                        raise TelephonyError("FreeSWITCH ESL 收到未对应的命令响应")
                    self._response.set_result(frame)
        except (Exception, asyncio.CancelledError):
            if self._response is not None and not self._response.done():
                self._response.set_exception(TelephonyError("FreeSWITCH ESL 连接已断开"))
        finally:
            self._failed = True
            self.writer.close()
            with suppress(asyncio.QueueFull):
                self.events.put_nowait(None)

    async def command(
        self, command: str, *, timeout: float = 10, allow_error: bool = False
    ) -> _ESLFrame:
        async with self._lock:
            if self._closed or self._failed:
                raise TelephonyError("FreeSWITCH ESL 连接已关闭")
            self._response = asyncio.get_running_loop().create_future()
            try:
                self.writer.write(command.encode() + b"\n\n")
                await self.writer.drain()
                result = await asyncio.wait_for(self._response, timeout=timeout)
                response = result.headers.get("reply-text", "") or result.body.decode(
                    "utf-8", errors="replace"
                )
                if response.startswith("-ERR") and not allow_error:
                    raise TelephonyError("FreeSWITCH 拒绝电话控制命令")
                return result
            except BaseException:
                # 超时以后无法安全区分迟到响应；关闭连接，禁止串给下一条命令。
                await self.close()
                raise
            finally:
                self._response = None

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.writer.close()
        with suppress(Exception):
            await self.writer.wait_closed()
        if self._read_task is not None and self._read_task is not asyncio.current_task():
            self._read_task.cancel()
            await asyncio.gather(self._read_task, return_exceptions=True)


def _event_headers(frame: _ESLFrame) -> dict[str, str]:
    from urllib.parse import unquote

    return {
        name: unquote(value.strip())
        for line in frame.body.decode("utf-8", errors="replace").split("\n\n", 1)[0].splitlines()
        for name, separator, value in [line.partition(":")]
        if separator
    }


class _PCMReceiver(asyncio.DatagramProtocol):
    def __init__(self, peer: tuple[str, int], queue: asyncio.Queue[bytes | None]) -> None:
        self.peer = peer
        self.queue = queue

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        # 原生 unicast 无应用层认证，只接收配置的可信主机与预留端口。
        if addr != self.peer or not data or len(data) % 2 or len(data) > 3200:
            return
        try:
            self.queue.put_nowait(data)
        except asyncio.QueueFull:
            # 不静默丢掉已接收的语音；以结束标记让 driver 报告过载。
            self.queue.get_nowait()
            self.queue.put_nowait(None)

    def error_received(self, exc: Exception) -> None:
        with suppress(asyncio.QueueFull):
            self.queue.put_nowait(None)


class FreeSwitchDriver:
    """一个外呼对应一个 ESL 连接和 UDP 端口，默认配置不会发起网络连接。"""

    sample_rate = 8000

    def __init__(self, settings: FreeswitchSettings, callbacks: MediaCallbacks) -> None:
        self.settings = settings.model_copy(deep=True)
        self.callbacks = callbacks
        self._esl: _ESLConnection | None = None
        self._udp: asyncio.DatagramTransport | None = None
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=256)
        self._tasks: list[asyncio.Task[None]] = []
        self._answered = asyncio.Event()
        self._ready = asyncio.Event()
        self._closed = False
        self._terminal = False
        self._started = False
        self._originate_sent = False
        self._hangup_requested = False
        self._hangup_confirmed = False
        self._hangup_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._hangup_failure: TelephonyError | None = None
        self._closing = False
        self._channel_created = asyncio.Event()
        self._originate_done = asyncio.Event()
        self._remote_ended = asyncio.Event()
        self._call_id = ""
        self._last_sent_at = 0.0
        self._generation = 0
        self._flushed = asyncio.Event()

    def _validate(self, number: str, call_id: str) -> None:
        if not self.settings.enabled:
            raise TelephonyError("FreeSWITCH provider 尚未启用")
        if not self.settings.host or self.settings.password is None:
            raise TelephonyError("请配置 FreeSWITCH ESL 地址和密码")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", self.settings.gateway):
            raise TelephonyError("请配置有效的 FreeSWITCH SIP gateway 名称")
        if not re.fullmatch(r"\+?[0-9]{3,32}", number):
            raise TelephonyError("被叫号码格式无效")
        if not re.fullmatch(r"\+?[0-9]{1,32}", self.settings.caller_id):
            raise TelephonyError("请配置有效的外呼主叫号码")
        try:
            UUID(call_id)
            ipaddress.IPv4Address(self.settings.fs_media_host)
            ipaddress.IPv4Address(self.settings.audio_advertised_host)
        except ValueError as exc:
            raise TelephonyError("FreeSWITCH 需要会话 UUID 和有效的媒体 IPv4 地址") from exc
        if self.settings.fs_media_host == "0.0.0.0":
            raise TelephonyError("FreeSWITCH 媒体地址必须为实际可达的主机地址")
        if self.settings.audio_advertised_host == "0.0.0.0":
            raise TelephonyError("应用媒体发布地址必须为 FreeSWITCH 可达的实际地址")
        if not 1 <= self.settings.fs_media_port <= 65535:
            raise TelephonyError("请为 FreeSWITCH 设置已预留的媒体 UDP 端口")
        if sys.byteorder != "little":
            raise TelephonyError("FreeSWITCH unicast 首版仅支持小端服务器")

    async def start(self, number: str, call_id: str) -> None:
        if self._started or self._closed:
            raise TelephonyError("电话 driver 已启动或关闭")
        self._validate(number, call_id)
        self._started = True
        self._call_id = str(UUID(call_id))
        try:
            transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
                lambda: _PCMReceiver(
                    (self.settings.fs_media_host, self.settings.fs_media_port), self._queue
                ),
                local_addr=(self.settings.audio_bind_host, self.settings.audio_bind_port),
                family=socket.AF_INET,
            )
            self._udp = transport
            self._esl = await _ESLConnection.connect(
                self.settings.host, self.settings.port, self.settings.password.get_secret_value()
            )
            self._tasks.append(asyncio.create_task(self._events()))
            await self._esl.command(
                "event plain CHANNEL_CREATE CHANNEL_ANSWER CHANNEL_HANGUP_COMPLETE BACKGROUND_JOB"
            )
            self._tasks.append(asyncio.create_task(self._audio()))
            self._tasks.append(asyncio.create_task(self._watchdog()))
            # UUID 由本应用分配，后台任务和通话事件均按该 UUID 隔离。
            variables = (
                f"origination_uuid={self._call_id},"
                f"origination_caller_id_number={self.settings.caller_id},"
                f"originate_timeout={self.settings.ring_timeout_seconds:g},"
                "ignore_early_media=true,absolute_codec_string='PCMA,PCMU'"
            )
            self._originate_sent = True
            await self._esl.command(
                f"bgapi originate {{{variables}}}"
                f"sofia/gateway/{self.settings.gateway}/{number} &park()"
            )
        except BaseException:
            await self.close()
            raise

    async def _activate_media(self) -> None:
        if (
            self._esl is None
            or self._udp is None
            or self._answered.is_set()
            or self._hangup_requested
        ):
            return
        # 后续原生 PCM codec 的速率取决于通话 codec；这里只接受 8k PCMA/PCMU。
        rate = await self._esl.command(f"api uuid_getvar {self._call_id} read_rate")
        if rate.body.strip() != b"8000":
            raise TelephonyError("FreeSWITCH 通话未协商到 8 kHz，无法接入原生 PCM")
        application_port = self._udp.get_extra_info("sockname")[1]
        await self._esl.command(
            f"sendmsg {self._call_id}\ncall-command: unicast\n"
            f"local-ip: {self.settings.fs_media_host}\n"
            f"local-port: {self.settings.fs_media_port}\n"
            f"remote-ip: {self.settings.audio_advertised_host}\n"
            f"remote-port: {application_port}\ntransport: udp"
        )
        self._answered.set()

    async def _events(self, connection: _ESLConnection | None = None) -> None:
        connection = connection or self._esl
        assert connection is not None
        try:
            while not self._closed:
                frame = await connection.events.get()
                if frame is None:
                    if not self._terminal and not self._hangup_requested:
                        await self._fail("FreeSWITCH 电话控制连接已断开")
                    return
                event = _event_headers(frame)
                if event.get("Event-Name") == "BACKGROUND_JOB":
                    if self._call_id in event.get("Job-Command-Arg", ""):
                        self._originate_done.set()
                        _, _, body = frame.body.partition(b"\n\n")
                        if body.strip().startswith(b"-ERR"):
                            self._remote_ended.set()
                            cause = (
                                body.strip()
                                .removeprefix(b"-ERR")
                                .strip()
                                .decode("utf-8", errors="replace")
                            )
                            if not self._hangup_requested:
                                await self._ended(self._end_reason(cause))
                    continue
                if event.get("Unique-ID") != self._call_id:
                    continue
                if event.get("Event-Name") == "CHANNEL_CREATE":
                    self._channel_created.set()
                elif event.get("Event-Name") == "CHANNEL_ANSWER":
                    self._channel_created.set()
                    await self._activate_media()
                elif event.get("Event-Name") == "CHANNEL_HANGUP_COMPLETE":
                    self._remote_ended.set()
                    if not self._hangup_requested:
                        await self._ended(self._end_reason(event.get("Hangup-Cause", "")))
                    return
        except asyncio.CancelledError:
            raise
        except TelephonyError as exc:
            await self._fail(str(exc))
        except Exception:
            await self._fail("FreeSWITCH 电话控制失败")

    @staticmethod
    def _end_reason(cause: str) -> str:
        if cause in ("USER_BUSY", "CALL_REJECTED"):
            return "busy"
        if cause in ("NO_ANSWER", "NO_USER_RESPONSE", "ALLOTTED_TIMEOUT"):
            return "no_answer"
        if cause in ("NORMAL_CLEARING", "ORIGINATOR_CANCEL"):
            return "remote_hangup"
        return "provider_error"

    async def _audio(self) -> None:
        try:
            while not self._closed:
                audio = await self._queue.get()
                if audio is None:
                    await self._fail("FreeSWITCH 媒体连接错误或接收队列过载")
                    return
                if not self._answered.is_set() or self._hangup_requested or self._closing:
                    continue
                if not self._ready.is_set():
                    self._ready.set()
                    await self.callbacks.on_ready()
                await self.callbacks.on_audio(audio, self.sample_rate)
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._fail("FreeSWITCH 音频处理失败")

    async def _watchdog(self) -> None:
        try:
            await asyncio.wait_for(
                self._answered.wait(), timeout=self.settings.ring_timeout_seconds + 5
            )
        except TimeoutError:
            await self._ended("no_answer")
            return
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=self.settings.media_timeout_seconds)
        except TimeoutError:
            await self._fail("FreeSWITCH 已接听但未收到可信的音频，检查媒体地址和端口")

    async def _ended(self, reason: str) -> None:
        if self._terminal or self._closed:
            return
        self._terminal = True
        await self.callbacks.on_ended(reason)
        await self._close_after_terminal()

    async def _fail(self, message: str) -> None:
        if self._terminal or self._closed:
            return
        self._terminal = True
        try:
            await self.callbacks.on_error(message)
        finally:
            await self._close_after_terminal()

    async def _close_after_terminal(self) -> None:
        try:
            await self.close()
        except TelephonyError as exc:
            # driver 独立运行时同样暴露清理未确认；外层 transport 仍会最终 close。
            await self.callbacks.on_error(str(exc))

    async def send_audio(self, audio: bytes) -> None:
        if (
            self._closed
            or self._terminal
            or self._hangup_requested
            or not self._ready.is_set()
            or self._udp is None
        ):
            return
        if not audio or len(audio) % 2 or len(audio) > 3200:
            raise TelephonyError("FreeSWITCH 音频必须为不超过 200 ms 的 PCM16 单声道数据")
        self._udp.sendto(audio, (self.settings.fs_media_host, self.settings.fs_media_port))
        # 公共输出 transport 负责实时节奏；这里不缓存已取消回合的音频。
        self._last_sent_at = asyncio.get_running_loop().time() + len(audio) / (8000 * 2)

    async def flush(self) -> None:
        self._generation += 1
        self._last_sent_at = 0
        self._flushed.set()
        self._flushed = asyncio.Event()

    async def wait_played(self) -> None:
        generation = self._generation
        if not self._last_sent_at:
            return
        seconds = max(
            0,
            self._last_sent_at
            + self.settings.playback_tail_seconds
            - asyncio.get_running_loop().time(),
        )
        try:
            await asyncio.wait_for(self._flushed.wait(), timeout=seconds)
        except TimeoutError:
            return
        if generation != self._generation:
            raise asyncio.CancelledError("播放等待已被用户打断")

    async def hangup(self) -> None:
        await self.flush()
        async with self._hangup_lock:
            if self._hangup_failure is not None:
                raise self._hangup_failure
            if self._hangup_confirmed or not self._originate_sent or self._closed:
                return
            self._hangup_requested = True
            connection = self._esl
            temporary: _ESLConnection | None = None
            event_task: asyncio.Task[None] | None = None
            try:
                async with asyncio.timeout(self.settings.cleanup_timeout_seconds):
                    if connection is None or connection._failed or connection._closed:
                        # Inbound ESL 断开不会自动挂掉 park 电话；只重连清理原 UUID。
                        assert self.settings.password is not None
                        temporary = await _ESLConnection.connect(
                            self.settings.host,
                            self.settings.port,
                            self.settings.password.get_secret_value(),
                        )
                        connection = temporary
                        await connection.command(
                            "event plain CHANNEL_CREATE CHANNEL_ANSWER "
                            "CHANNEL_HANGUP_COMPLETE BACKGROUND_JOB"
                        )
                        event_task = asyncio.create_task(self._events(connection))
                    while not self._hangup_confirmed:
                        if self._remote_ended.is_set():
                            self._hangup_confirmed = True
                            break
                        result = await connection.command(
                            f"api uuid_kill {self._call_id} NORMAL_CLEARING", allow_error=True
                        )
                        reply = result.body.strip()
                        if reply.startswith(b"+OK"):
                            self._hangup_confirmed = True
                        elif reply.startswith(b"-ERR No such channel"):
                            if self._channel_created.is_set() or self._originate_done.is_set():
                                self._hangup_confirmed = True
                            else:
                                # bgapi +OK 仅接受任务；通道可能稍后才创建。保留 reader，
                                # 在有限预算内重试同 UUID，绝不重新 originate。
                                await asyncio.sleep(0.05)
                        else:
                            raise TelephonyError("FreeSWITCH 未确认电话挂断，请检查遗留通道")
            except Exception as exc:
                self._hangup_failure = TelephonyError("FreeSWITCH 未确认电话挂断，请检查遗留通道")
                raise self._hangup_failure from exc
            finally:
                if event_task is not None:
                    event_task.cancel()
                    await asyncio.gather(event_task, return_exceptions=True)
                if temporary is not None:
                    await temporary.close()

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed:
                if self._hangup_failure is not None:
                    raise self._hangup_failure
                return
            self._closing = True
            try:
                if not self._remote_ended.is_set():
                    await self.hangup()
            finally:
                self._closed = True
                await self.flush()
                if self._udp is not None:
                    self._udp.close()
                tasks = [task for task in self._tasks if task is not asyncio.current_task()]
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                if self._esl is not None:
                    await self._esl.close()
