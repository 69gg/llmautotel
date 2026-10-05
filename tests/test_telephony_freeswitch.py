"""真实 TCP ESL 和 UDP PCM 线格式的可控 FreeSWITCH 对端，无外部线路。"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncGenerator
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import pytest

from llmautotel.telephony.base import MediaCallbacks, TelephonyError
from llmautotel.telephony.freeswitch import FreeSwitchDriver, _ESLConnection, _read_frame
from llmautotel.telephony.settings import FreeswitchSettings

PCM = (1234).to_bytes(2, "little", signed=True) * 160


@dataclass
class CallbackLog:
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    received: asyncio.Event = field(default_factory=asyncio.Event)
    ended: asyncio.Event = field(default_factory=asyncio.Event)
    failed: asyncio.Event = field(default_factory=asyncio.Event)
    audio: list[tuple[bytes, int]] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    async def on_ready(self) -> None:
        self.ready.set()

    async def on_audio(self, audio: bytes, rate: int) -> None:
        self.audio.append((audio, rate))
        self.received.set()

    async def on_ended(self, reason: str) -> None:
        self.reasons.append(reason)
        self.ended.set()

    async def on_error(self, error: str) -> None:
        self.errors.append(error)
        self.failed.set()

    def callbacks(self) -> MediaCallbacks:
        return MediaCallbacks(self.on_ready, self.on_audio, self.on_ended, self.on_error)


class FakePCM(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.received: asyncio.Queue[bytes] = asyncio.Queue()

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self.received.put_nowait(data)


class FakeFreeSwitch:
    def __init__(self) -> None:
        self.commands: list[str] = []
        self.auto_answer = True
        self.send_media = True
        self.auth_ok = True
        self.rate = b"8000"
        self.password = "test-esl-password"
        self.call_id = ""
        self.create_delay = 0.0
        self.create_never = False
        self.active_calls: set[str] = set()
        self.originate_jobs: set[asyncio.Task[None]] = set()
        self.writer: asyncio.StreamWriter | None = None
        self.peer: tuple[str, int] | None = None
        self.client_closed = asyncio.Event()
        self.tasks: set[asyncio.Task[None]] = set()
        self.media = FakePCM()
        self.udp: asyncio.DatagramTransport | None = None
        self.server: asyncio.Server | None = None

    async def open(self) -> None:
        transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
            lambda: self.media, local_addr=("127.0.0.1", 0)
        )
        self.udp = transport
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)

    def settings(self, **changes: Any) -> FreeswitchSettings:
        assert self.server is not None and self.udp is not None
        values: dict[str, Any] = {
            "enabled": True,
            "host": "127.0.0.1",
            "port": self.server.sockets[0].getsockname()[1],
            "password": self.password,
            "gateway": "customer_sip_trunk",
            "caller_id": "01012345678",
            "fs_media_host": "127.0.0.1",
            "fs_media_port": self.udp.get_extra_info("sockname")[1],
            "audio_bind_host": "127.0.0.1",
            "audio_advertised_host": "127.0.0.1",
        }
        values.update(changes)
        return FreeswitchSettings(**values)

    async def packet(self, headers: dict[str, str], body: bytes = b"") -> None:
        assert self.writer is not None
        if body:
            headers = {**headers, "Content-Length": str(len(body))}
        wire = "".join(f"{key}: {value}\n" for key, value in headers.items()).encode()
        # 刻意拆分头/body TCP 写入，客户端不能假定一次 recv 等于一帧。
        self.writer.write(wire + b"\n")
        await self.writer.drain()
        if body:
            self.writer.write(body[:7])
            self.writer.write(body[7:])
            await self.writer.drain()

    async def event(self, event: str, *, call_id: str | None = None, cause: str = "") -> None:
        body = f"Event-Name: {event}\nUnique-ID: {call_id or self.call_id}\n"
        if cause:
            body += f"Hangup-Cause: {cause}\n"
        await self.packet({"Content-Type": "text/event-plain"}, body.encode())

    async def background_failure(self, cause: str) -> None:
        originate = next(command for command in self.commands if command.startswith("bgapi"))
        body = (
            "Event-Name: BACKGROUND_JOB\n"
            f"Job-Command-Arg: {originate.removeprefix('bgapi originate ')}\n\n"
            f"-ERR {cause}\n"
        ).encode()
        await self.packet({"Content-Type": "text/event-plain"}, body)

    async def media_frames(self) -> None:
        assert self.udp is not None and self.peer is not None
        for _ in range(3):
            await asyncio.sleep(0.01)
            self.udp.sendto(PCM, self.peer)

    async def create_channel(self) -> None:
        # 后台作业独立于 ESL 连接：关闭控制连接不能取消已接受的 originate。
        await asyncio.sleep(self.create_delay)
        self.active_calls.add(self.call_id)
        if self.writer is not None and not self.writer.is_closing():
            await self.event("CHANNEL_CREATE")
            if self.auto_answer:
                await self.event("CHANNEL_ANSWER")

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self.tasks.add(task)
        self.writer = writer
        try:
            await self.packet({"Content-Type": "auth/request"})
            while True:
                try:
                    command = (await reader.readuntil(b"\n\n")).decode().strip()
                except asyncio.IncompleteReadError:
                    return
                self.commands.append(command)
                if command.startswith("auth "):
                    assert command == f"auth {self.password}"
                    await self.packet(
                        {
                            "Content-Type": "command/reply",
                            "Reply-Text": "+OK accepted" if self.auth_ok else "-ERR invalid",
                        }
                    )
                    if not self.auth_ok:
                        return
                elif command.startswith("event plain"):
                    await self.packet({"Content-Type": "command/reply", "Reply-Text": "+OK"})
                elif command.startswith("bgapi originate"):
                    match = re.search(r"origination_uuid=([^,]+)", command)
                    assert match is not None
                    self.call_id = match[1]
                    if self.create_delay:
                        job = asyncio.create_task(self.create_channel())
                        self.originate_jobs.add(job)
                        job.add_done_callback(self.originate_jobs.discard)
                    elif not self.create_never:
                        self.active_calls.add(self.call_id)
                        # 通话事件可先于 originate 的命令响应到达。
                        await self.event("CHANNEL_CREATE")
                        if self.auto_answer:
                            await self.event("CHANNEL_ANSWER")
                    await self.packet(
                        {"Content-Type": "command/reply", "Reply-Text": "+OK Job-UUID: test-job"}
                    )
                elif command.startswith("api uuid_getvar"):
                    await self.packet({"Content-Type": "api/response"}, self.rate)
                elif command.startswith("sendmsg "):
                    fields = dict(line.split(": ", 1) for line in command.splitlines()[1:])
                    assert fields["call-command"] == "unicast"
                    assert fields["transport"] == "udp"
                    assert "flags" not in fields
                    self.peer = fields["remote-ip"], int(fields["remote-port"])
                    assert self.udp is not None
                    assert int(fields["local-port"]) == self.udp.get_extra_info("sockname")[1]
                    await self.packet({"Content-Type": "command/reply", "Reply-Text": "+OK"})
                    if self.send_media:
                        await self.media_frames()
                elif command.startswith("api uuid_kill"):
                    call_id = command.split()[2]
                    if call_id in self.active_calls:
                        self.active_calls.discard(call_id)
                        await self.packet({"Content-Type": "api/response"}, b"+OK\n")
                    else:
                        await self.packet(
                            {"Content-Type": "api/response"}, b"-ERR No such channel!\n"
                        )
                else:
                    raise AssertionError(f"unexpected command: {command}")
        finally:
            self.client_closed.set()
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()
            self.tasks.discard(task)

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
        if self.writer is not None:
            self.writer.close()
        if self.udp is not None:
            self.udp.close()
        tasks = list(self.tasks)
        tasks += list(self.originate_jobs)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.fixture
async def freeswitch() -> AsyncGenerator[FakeFreeSwitch, None]:
    fake = FakeFreeSwitch()
    await fake.open()
    try:
        yield fake
    finally:
        await fake.close()


async def ready_driver(fake: FakeFreeSwitch) -> tuple[FreeSwitchDriver, CallbackLog]:
    log = CallbackLog()
    driver = FreeSwitchDriver(fake.settings(), log.callbacks())
    await driver.start("13800138000", str(uuid4()))
    await asyncio.wait_for(log.received.wait(), timeout=2)
    return driver, log


async def test_esl_originate_and_bidirectional_raw_pcm(freeswitch: FakeFreeSwitch) -> None:
    driver, log = await ready_driver(freeswitch)
    try:
        assert log.ready.is_set()
        assert log.audio[0] == (PCM, 8000)
        originate = next(command for command in freeswitch.commands if command.startswith("bgapi"))
        assert "sofia/gateway/customer_sip_trunk/13800138000 &park()" in originate
        assert "origination_caller_id_number=01012345678" in originate
        assert "absolute_codec_string='PCMA,PCMU'" in originate
        assert "ignore_early_media=true" in originate
        await driver.send_audio(PCM)
        assert await asyncio.wait_for(freeswitch.media.received.get(), timeout=1) == PCM
        await driver.hangup()
        assert freeswitch.commands[-1] == f"api uuid_kill {freeswitch.call_id} NORMAL_CLEARING"
    finally:
        await driver.close()
    await asyncio.wait_for(freeswitch.client_closed.wait(), timeout=1)


async def test_disabled_provider_opens_no_network(freeswitch: FakeFreeSwitch) -> None:
    driver = FreeSwitchDriver(freeswitch.settings(enabled=False), CallbackLog().callbacks())
    with pytest.raises(TelephonyError, match="尚未启用"):
        await driver.start("13800138000", str(uuid4()))
    assert freeswitch.commands == []


@pytest.mark.parametrize("number", ["13800138000\napi status", "100&echo", "../100"])
async def test_destination_cannot_inject_esl_commands(
    freeswitch: FakeFreeSwitch, number: str
) -> None:
    driver = FreeSwitchDriver(freeswitch.settings(), CallbackLog().callbacks())
    with pytest.raises(TelephonyError, match="号码格式"):
        await driver.start(number, str(uuid4()))
    assert freeswitch.commands == []


async def test_esl_auth_failure_is_redacted_and_releases_ports(freeswitch: FakeFreeSwitch) -> None:
    freeswitch.auth_ok = False
    driver = FreeSwitchDriver(freeswitch.settings(), CallbackLog().callbacks())
    with pytest.raises(TelephonyError, match="认证失败") as error:
        await driver.start("13800138000", str(uuid4()))
    assert freeswitch.password not in str(error.value)
    assert not any(command.startswith("bgapi") for command in freeswitch.commands)
    assert driver._closed


async def test_ready_requires_answer_and_real_media(freeswitch: FakeFreeSwitch) -> None:
    freeswitch.auto_answer = False
    freeswitch.send_media = False
    log = CallbackLog()
    driver = FreeSwitchDriver(freeswitch.settings(), log.callbacks())
    try:
        await driver.start("13800138000", str(uuid4()))
        assert not log.ready.is_set()
        await freeswitch.event("CHANNEL_ANSWER", call_id=str(uuid4()))
        await asyncio.sleep(0.02)
        assert not any(command.startswith("sendmsg") for command in freeswitch.commands)
        await freeswitch.event("CHANNEL_ANSWER")
        await asyncio.wait_for(driver._answered.wait(), timeout=1)
        assert not log.ready.is_set()
        await freeswitch.media_frames()
        await asyncio.wait_for(log.ready.wait(), timeout=1)
    finally:
        await driver.close()


async def test_wrong_udp_peer_and_odd_pcm_are_rejected(freeswitch: FakeFreeSwitch) -> None:
    freeswitch.send_media = False
    log = CallbackLog()
    driver = FreeSwitchDriver(freeswitch.settings(), log.callbacks())
    transport: asyncio.DatagramTransport | None = None
    try:
        await driver.start("13800138000", str(uuid4()))
        await asyncio.wait_for(driver._answered.wait(), timeout=1)
        assert freeswitch.peer is not None and freeswitch.udp is not None
        transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
            asyncio.DatagramProtocol, local_addr=("127.0.0.1", 0)
        )
        transport.sendto(PCM, freeswitch.peer)
        freeswitch.udp.sendto(b"odd", freeswitch.peer)
        await asyncio.sleep(0.03)
        assert not log.ready.is_set()
        await freeswitch.media_frames()
        await asyncio.wait_for(log.received.wait(), timeout=1)
        assert log.audio[0] == (PCM, 8000)
    finally:
        if transport is not None:
            transport.close()
        await driver.close()


async def test_non_8k_codec_ends_instead_of_mislabeled_audio(freeswitch: FakeFreeSwitch) -> None:
    freeswitch.rate = b"16000"
    log = CallbackLog()
    driver = FreeSwitchDriver(freeswitch.settings(), log.callbacks())
    try:
        await driver.start("13800138000", str(uuid4()))
        await asyncio.wait_for(log.failed.wait(), timeout=1)
        assert "8 kHz" in log.errors[0]
        assert not log.ready.is_set()
        await driver.close()
        assert any(command.startswith("api uuid_kill") for command in freeswitch.commands)
    finally:
        await driver.close()


@pytest.mark.parametrize(
    ("cause", "reason"),
    [("USER_BUSY", "busy"), ("NO_ANSWER", "no_answer"), ("NORMAL_CLEARING", "remote_hangup")],
)
async def test_remote_terminal_events_are_isolated_and_release_resources(
    freeswitch: FakeFreeSwitch, cause: str, reason: str
) -> None:
    driver, log = await ready_driver(freeswitch)
    try:
        await freeswitch.event("CHANNEL_HANGUP_COMPLETE", call_id=str(uuid4()), cause=cause)
        await asyncio.sleep(0.02)
        assert not log.ended.is_set()
        await freeswitch.event("CHANNEL_HANGUP_COMPLETE", cause=cause)
        await asyncio.wait_for(log.ended.wait(), timeout=1)
        assert log.reasons == [reason]
        await asyncio.wait_for(freeswitch.client_closed.wait(), timeout=1)
        await driver.send_audio(PCM)
        assert freeswitch.media.received.empty()
    finally:
        await driver.close()


async def test_originate_background_failure_is_not_confused_with_answer(
    freeswitch: FakeFreeSwitch,
) -> None:
    freeswitch.auto_answer = False
    log = CallbackLog()
    driver = FreeSwitchDriver(freeswitch.settings(), log.callbacks())
    try:
        await driver.start("13800138000", str(uuid4()))
        await freeswitch.background_failure("NO_ANSWER")
        await asyncio.wait_for(log.ended.wait(), timeout=1)
        assert log.reasons == ["no_answer"]
        assert not log.ready.is_set()
    finally:
        await driver.close()


async def test_media_timeout_hangs_up_and_reports_audio_stage(freeswitch: FakeFreeSwitch) -> None:
    freeswitch.send_media = False
    log = CallbackLog()
    driver = FreeSwitchDriver(freeswitch.settings(media_timeout_seconds=1), log.callbacks())
    try:
        await driver.start("13800138000", str(uuid4()))
        await asyncio.wait_for(log.failed.wait(), timeout=2)
        assert "未收到可信的音频" in log.errors[0]
        await driver.close()
        assert any(command.startswith("api uuid_kill") for command in freeswitch.commands)
    finally:
        await driver.close()


async def test_interrupt_cancels_playback_wait_and_no_old_buffer_reappears(
    freeswitch: FakeFreeSwitch,
) -> None:
    driver, _ = await ready_driver(freeswitch)
    try:
        old_pcm = b"\x34\x12" * 160
        new_pcm = b"\x78\x56" * 160
        await driver.send_audio(old_pcm)
        assert await asyncio.wait_for(freeswitch.media.received.get(), timeout=1) == old_pcm
        pending = asyncio.create_task(driver.wait_played())
        await asyncio.sleep(0)
        await driver.flush()
        with pytest.raises(asyncio.CancelledError):
            await pending
        await driver.wait_played()  # 旧队列清空，等待不会复活旧音频。
        await driver.send_audio(new_pcm)
        assert await asyncio.wait_for(freeswitch.media.received.get(), timeout=1) == new_pcm
        await asyncio.sleep(0.05)
        assert freeswitch.media.received.empty()
    finally:
        await driver.close()


async def test_control_disconnect_ends_active_call(freeswitch: FakeFreeSwitch) -> None:
    driver, log = await ready_driver(freeswitch)
    try:
        assert freeswitch.writer is not None
        freeswitch.writer.close()
        await asyncio.wait_for(log.failed.wait(), timeout=1)
        assert "连接已断开" in log.errors[0]
        assert driver._terminal
        await driver.close()
        # 断开 inbound ESL 不会让 park 电话自动结束；新控制连接只挂掉原 UUID。
        assert sum(command.startswith("auth ") for command in freeswitch.commands) == 2
        assert freeswitch.commands[-1] == f"api uuid_kill {freeswitch.call_id} NORMAL_CLEARING"
    finally:
        await driver.close()


async def test_close_active_call_hangs_up_once(freeswitch: FakeFreeSwitch) -> None:
    driver, _ = await ready_driver(freeswitch)
    await driver.close()
    await driver.close()
    assert sum(command.startswith("api uuid_kill") for command in freeswitch.commands) == 1


async def test_hangup_before_background_originate_creates_channel_leaves_no_call(
    freeswitch: FakeFreeSwitch,
) -> None:
    freeswitch.auto_answer = False
    freeswitch.create_delay = 0.05
    driver = FreeSwitchDriver(freeswitch.settings(), CallbackLog().callbacks())
    await driver.start("13800138000", str(uuid4()))
    await driver.close()
    await asyncio.sleep(0.08)
    assert not freeswitch.active_calls
    kills = [command for command in freeswitch.commands if command.startswith("api uuid_kill")]
    assert len(kills) >= 2
    assert set(kills) == {f"api uuid_kill {freeswitch.call_id} NORMAL_CLEARING"}
    assert sum(command.startswith("bgapi") for command in freeswitch.commands) == 1
    assert driver._hangup_confirmed and driver._closed


async def test_pending_originate_cleanup_timeout_is_visible_and_releases_local_resources(
    freeswitch: FakeFreeSwitch,
) -> None:
    freeswitch.auto_answer = False
    freeswitch.create_never = True
    driver = FreeSwitchDriver(
        freeswitch.settings(cleanup_timeout_seconds=1), CallbackLog().callbacks()
    )
    await driver.start("13800138000", str(uuid4()))
    with pytest.raises(TelephonyError, match="未确认电话挂断"):
        await driver.close()
    assert driver._closed
    assert driver._udp is not None and driver._udp.is_closing()
    assert driver._esl is not None and driver._esl._closed
    assert not driver._hangup_confirmed
    assert sum(command.startswith("bgapi") for command in freeswitch.commands) == 1
    attempts = len(freeswitch.commands)
    with pytest.raises(TelephonyError, match="未确认电话挂断"):
        await driver.close()
    assert len(freeswitch.commands) == attempts  # 不重复清理预算，也不隐藏未确认状态。


async def test_start_cancellation_releases_parked_call(freeswitch: FakeFreeSwitch) -> None:
    original = freeswitch.packet

    async def blocked_originate(headers: dict[str, str], body: bytes = b"") -> None:
        if headers.get("Reply-Text", "").startswith("+OK Job-UUID"):
            await asyncio.sleep(0.1)
        await original(headers, body)

    freeswitch.auto_answer = False
    freeswitch.packet = blocked_originate
    driver = FreeSwitchDriver(freeswitch.settings(), CallbackLog().callbacks())
    starting = asyncio.create_task(driver.start("13800138000", str(uuid4())))
    for _ in range(100):
        if any(command.startswith("bgapi") for command in freeswitch.commands):
            break
        await asyncio.sleep(0.005)
    starting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await starting
    assert driver._closed
    assert any(command.startswith("api uuid_kill") for command in freeswitch.commands)


async def test_header_length_counts_bytes_and_accepts_crlf() -> None:
    reader = asyncio.StreamReader()
    body = "正文：你好".encode()
    reader.feed_data(f"Content-Type: api/response\r\nContent-Length: {len(body)}\r\n\r\n".encode())
    reader.feed_data(body)
    result = await _read_frame(reader)
    assert result.body == body


async def test_timed_out_command_closes_esl_instead_of_reusing_late_reply(
    freeswitch: FakeFreeSwitch,
) -> None:
    connection = await _ESLConnection.connect(
        freeswitch.settings().host, freeswitch.settings().port, freeswitch.password
    )
    original = freeswitch.packet

    async def delayed(headers: dict[str, str], body: bytes = b"") -> None:
        if headers.get("Content-Type") == "api/response":
            await asyncio.sleep(0.2)
        await original(headers, body)

    freeswitch.packet = delayed
    try:
        with pytest.raises(TimeoutError):
            await connection.command("api uuid_getvar test read_rate", timeout=0.02)
        with pytest.raises(TelephonyError, match="已关闭"):
            await connection.command("api uuid_getvar test read_rate")
    finally:
        await connection.close()


class IncomingFreeSwitch(FakeFreeSwitch):
    """多条真实 ESL TCP 连接：监听器与单通驱动分别认证及收事件。"""

    def __init__(self) -> None:
        super().__init__()
        self.writers: set[asyncio.StreamWriter] = set()
        self.subscribed: set[asyncio.StreamWriter] = set()

    async def send(
        self, writer: asyncio.StreamWriter, headers: dict[str, str], body: bytes = b""
    ) -> None:
        if body:
            headers = {**headers, "Content-Length": str(len(body))}
        wire = "".join(f"{key}: {value}\n" for key, value in headers.items()).encode()
        writer.write(wire + b"\n" + body)
        await writer.drain()

    async def broadcast(self, headers: dict[str, str]) -> None:
        body = "".join(f"{key}: {value}\n" for key, value in headers.items()).encode()
        for writer in tuple(self.subscribed):
            if not writer.is_closing():
                await self.send(writer, {"Content-Type": "text/event-plain"}, body)

    async def incoming(
        self,
        remote_id: str,
        *,
        marker: str = "llmautotel-inbound",
        direction: str = "inbound",
        destination: str = "4001234567",
    ) -> None:
        self.active_calls.add(remote_id)
        await self.broadcast(
            {
                "Event-Name": "CHANNEL_PARK",
                "Unique-ID": remote_id,
                "Call-Direction": direction,
                "variable_llmautotel_inbound": marker,
                "Caller-Caller-ID-Number": "13800138000",
                "Caller-Destination-Number": destination,
            }
        )

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self.tasks.add(task)
        self.writers.add(writer)
        try:
            await self.send(writer, {"Content-Type": "auth/request"})
            while True:
                try:
                    command = (await reader.readuntil(b"\n\n")).decode().strip()
                except asyncio.IncompleteReadError:
                    return
                self.commands.append(command)
                if command.startswith("auth "):
                    assert command == f"auth {self.password}"
                    await self.send(writer, {"Content-Type": "command/reply", "Reply-Text": "+OK"})
                elif command.startswith("event plain"):
                    self.subscribed.add(writer)
                    await self.send(writer, {"Content-Type": "command/reply", "Reply-Text": "+OK"})
                elif command.startswith("api uuid_answer"):
                    remote_id = command.split()[2]
                    await self.send(writer, {"Content-Type": "api/response"}, b"+OK\n")
                    await self.broadcast({"Event-Name": "CHANNEL_ANSWER", "Unique-ID": remote_id})
                elif command.startswith("api uuid_getvar"):
                    await self.send(writer, {"Content-Type": "api/response"}, self.rate)
                elif command.startswith("sendmsg "):
                    fields = dict(line.split(": ", 1) for line in command.splitlines()[1:])
                    assert fields["call-command"] == "unicast"
                    assert fields["transport"] == "udp"
                    self.peer = fields["remote-ip"], int(fields["remote-port"])
                    await self.send(writer, {"Content-Type": "command/reply", "Reply-Text": "+OK"})
                    if self.send_media:
                        await self.media_frames()
                elif command.startswith("api uuid_kill"):
                    remote_id = command.split()[2]
                    self.active_calls.discard(remote_id)
                    await self.send(writer, {"Content-Type": "api/response"}, b"+OK\n")
                    await self.broadcast(
                        {
                            "Event-Name": "CHANNEL_HANGUP_COMPLETE",
                            "Unique-ID": remote_id,
                            "Hangup-Cause": "NORMAL_CLEARING",
                        }
                    )
                else:
                    raise AssertionError(f"unexpected incoming command: {command}")
        finally:
            self.writers.discard(writer)
            self.subscribed.discard(writer)
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()
            self.tasks.discard(task)

    async def close(self) -> None:
        for writer in tuple(self.writers):
            writer.close()
        await super().close()


async def incoming_eventually(predicate: Any) -> None:
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0)


async def test_inbound_esl_answer_existing_uuid_and_udp_media() -> None:
    from llmautotel.telephony.freeswitch import FreeSwitchIncomingListener
    from llmautotel.telephony.incoming import IncomingCall, IncomingCallbacks

    fake = IncomingFreeSwitch()
    calls: list[IncomingCall] = []
    errors: list[str] = []

    async def accept(call: IncomingCall) -> bool:
        calls.append(call)
        return True

    async def error(message: str) -> None:
        errors.append(message)

    await fake.open()
    config = fake.settings(inbound_enabled=True, gateway="", caller_id="")
    listener = FreeSwitchIncomingListener(config, IncomingCallbacks(accept, error))
    driver: FreeSwitchDriver | None = None
    try:
        await listener.start()
        remote_id = str(uuid4())
        await fake.incoming(remote_id)
        await incoming_eventually(lambda: len(calls) == 1)
        assert (calls[0].caller, calls[0].destination) == ("13800138000", "4001234567")
        log = CallbackLog()
        driver = FreeSwitchDriver(config, log.callbacks(), incoming=calls[0])
        await driver.start("", str(uuid4()))
        await asyncio.wait_for(log.received.wait(), 2)
        assert f"api uuid_answer {remote_id}" in fake.commands
        assert not any("originate" in command for command in fake.commands)
        assert len([command for command in fake.commands if command.startswith("sendmsg")]) == 1
        assert log.audio[0] == (PCM, 8000)
        await driver.send_audio(PCM)
        assert await asyncio.wait_for(fake.media.received.get(), 1) == PCM
        await listener.close()
        assert not any("uuid_kill" in command for command in fake.commands)
        assert remote_id in fake.active_calls  # 关监听器不挂已接纳电话。
        await driver.close()
        assert remote_id not in fake.active_calls
        assert errors == []
    finally:
        if driver is not None:
            await driver.close()
        await listener.close()
        await fake.close()


async def test_inbound_esl_scopes_busy_duplicates_and_early_close() -> None:
    from llmautotel.telephony.freeswitch import FreeSwitchIncomingListener
    from llmautotel.telephony.incoming import IncomingCall, IncomingCallbacks

    fake = IncomingFreeSwitch()
    calls: list[IncomingCall] = []
    errors: list[str] = []

    async def accept(call: IncomingCall) -> bool:
        calls.append(call)
        return len(calls) == 1

    async def error(message: str) -> None:
        errors.append(message)

    await fake.open()
    listener = FreeSwitchIncomingListener(
        fake.settings(inbound_enabled=True, inbound_numbers=["4001234567"]),
        IncomingCallbacks(accept, error),
    )
    try:
        await listener.start()
        foreign = str(uuid4())
        outgoing = str(uuid4())
        accepted = str(uuid4())
        busy = str(uuid4())
        wrong_number = str(uuid4())
        await fake.incoming(foreign, marker="foreign")
        await fake.incoming(outgoing, direction="outbound")
        await fake.incoming(accepted)
        await fake.incoming(accepted)
        await fake.incoming(busy)
        await fake.incoming(wrong_number, destination="4009999999")
        await incoming_eventually(
            lambda: (
                len(calls) == 2
                and busy not in fake.active_calls
                and wrong_number not in fake.active_calls
            )
        )
        assert [call.remote_id for call in calls] == [accepted, busy]
        assert f"api uuid_kill {busy} USER_BUSY" in fake.commands
        assert {foreign, outgoing, accepted}.issubset(fake.active_calls)
        await calls[0].close()
        await calls[0].close()
        assert fake.commands.count(f"api uuid_kill {accepted} NORMAL_CLEARING") == 1
        await listener.close()
        assert {foreign, outgoing}.issubset(fake.active_calls)
        assert errors == []
    finally:
        await listener.close()
        await fake.close()


async def test_inbound_esl_disabled_never_connects(freeswitch: FakeFreeSwitch) -> None:
    from llmautotel.telephony.freeswitch import FreeSwitchIncomingListener
    from llmautotel.telephony.incoming import IncomingCall, IncomingCallbacks

    async def accept(call: IncomingCall) -> bool:
        raise AssertionError("must not admit")

    async def error(message: str) -> None:
        return

    listener = FreeSwitchIncomingListener(
        freeswitch.settings(inbound_enabled=False), IncomingCallbacks(accept, error)
    )
    with pytest.raises(TelephonyError, match="尚未启用"):
        await listener.start()
    await listener.close()
    assert freeswitch.commands == []
    assert (
        freeswitch.settings(inbound_enabled=True, gateway="", caller_id="").missing_inbound_fields()
        == []
    )


async def test_inbound_esl_reconnect_does_not_reclaim_old_uuid() -> None:
    from llmautotel.telephony.freeswitch import FreeSwitchIncomingListener
    from llmautotel.telephony.incoming import IncomingCall, IncomingCallbacks

    fake = IncomingFreeSwitch()
    calls: list[IncomingCall] = []
    errors: list[str] = []

    async def accept(call: IncomingCall) -> bool:
        calls.append(call)
        return True

    async def error(message: str) -> None:
        errors.append(message)

    await fake.open()
    config = fake.settings(inbound_enabled=True)
    config.inbound_reconnect_seconds = 0.01
    listener = FreeSwitchIncomingListener(config, IncomingCallbacks(accept, error))
    try:
        await listener.start()
        first = str(uuid4())
        await fake.incoming(first)
        await incoming_eventually(lambda: len(calls) == 1)
        old_connection = listener._esl
        assert old_connection is not None
        next(iter(fake.writers)).close()
        await incoming_eventually(
            lambda: listener.connected and listener._esl is not old_connection
        )
        await fake.incoming(first)
        second = str(uuid4())
        await fake.incoming(second)
        await incoming_eventually(lambda: len(calls) == 2)
        assert [call.remote_id for call in calls] == [first, second]
        assert errors and fake.password not in errors[0]
        await calls[0].close()
        await calls[1].close()
    finally:
        await listener.close()
        await fake.close()
