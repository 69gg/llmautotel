"""ARI/chan_websocket 协议验证；没有实体 PBX 或真实电话请求。"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from pydantic import SecretStr, ValidationError
from websockets.asyncio.server import ServerConnection, serve

import llmautotel.telephony.asterisk as asterisk_module
from llmautotel.telephony.asterisk import AsteriskDriver, AsteriskIncomingListener
from llmautotel.telephony.base import MediaCallbacks, TelephonyError
from llmautotel.telephony.incoming import IncomingCall, IncomingCallbacks
from llmautotel.telephony.settings import AsteriskSettings


class FakeSocket:
    def __init__(self) -> None:
        self.incoming: asyncio.Queue[str | bytes | None] = asyncio.Queue()
        self.sent: list[str | bytes] = []
        self.closed = False

    def __aiter__(self) -> AsyncIterator[str | bytes]:
        return self

    async def __anext__(self) -> str | bytes:
        message = await self.incoming.get()
        if message is None:
            raise StopAsyncIteration
        return message

    async def recv(self) -> str | bytes:
        return await self.__anext__()

    async def send(self, message: str | bytes) -> None:
        if self.closed:
            raise RuntimeError("closed")
        self.sent.append(message)

    async def close(self) -> None:
        self.closed = True
        self.incoming.put_nowait(None)

    def event(self, event: dict[str, Any]) -> None:
        self.incoming.put_nowait(json.dumps(event))

    def commands(self) -> list[dict[str, str]]:
        return [json.loads(message) for message in self.sent if isinstance(message, str)]


@dataclass
class CallbackLog:
    ready_count: int = 0
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    audio: list[tuple[bytes, int]] = field(default_factory=list)
    ended: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    async def on_ready(self) -> None:
        self.ready_count += 1
        self.ready.set()

    async def on_audio(self, audio: bytes, rate: int) -> None:
        self.audio.append((audio, rate))
        self.changed.set()

    async def on_ended(self, reason: str) -> None:
        self.ended.append(reason)
        self.changed.set()

    async def on_error(self, message: str) -> None:
        self.errors.append(message)
        self.changed.set()

    def callbacks(self) -> MediaCallbacks:
        return MediaCallbacks(self.on_ready, self.on_audio, self.on_ended, self.on_error)


class FakePBX:
    def __init__(self) -> None:
        self.events = FakeSocket()
        self.media = FakeSocket()
        self.requests: list[httpx.Request] = []
        self.connections: list[tuple[str, dict[str, Any]]] = []
        self.channel_id = ""
        self.external_id = ""
        self.codec = "slin16"
        self.auto_media_stasis = True
        self.fail_path = ""
        self.media_created = asyncio.Event()
        self.hold_media: asyncio.Event | None = None
        self.hold_events: asyncio.Event | None = None

    async def request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix("/office/ari")
        if path == self.fail_path:
            return httpx.Response(503, json={"message": "provider test-secret response"})
        params = request.url.params
        if request.method == "DELETE":
            return httpx.Response(204)
        if path == "/channels":
            self.channel_id = params["channelId"]
            return httpx.Response(200, json={"id": self.channel_id, "state": "Down"})
        if path == "/bridges":
            return httpx.Response(200, json={"id": params["bridgeId"]})
        if path == "/channels/externalMedia":
            self.external_id = params["channelId"]
            self.media_created.set()
            if self.hold_media is not None:
                await self.hold_media.wait()
            return httpx.Response(200, json={"id": self.external_id})
        if path.endswith("/variable"):
            return httpx.Response(200, json={"value": "ephemeral-connection"})
        return httpx.Response(204)

    async def connect(self, uri: str, **kwargs: Any) -> FakeSocket:
        self.connections.append((uri, kwargs))
        if urlsplit(uri).path.endswith("/events"):
            if self.hold_events is not None:
                await self.hold_events.wait()
            return self.events
        if self.auto_media_stasis:
            self.events.event(
                {"type": "StasisStart", "channel": {"id": self.external_id, "state": "Up"}}
            )
        self.media.event(
            {
                "event": "MEDIA_START",
                "channel_id": self.external_id,
                "format": self.codec,
                "optimal_frame_size": 640,
            }
        )
        return self.media

    def answered(self) -> None:
        self.events.event(
            {"type": "StasisStart", "channel": {"id": self.channel_id, "state": "Up"}}
        )


@pytest.fixture
def pbx(monkeypatch: pytest.MonkeyPatch) -> FakePBX:
    server = FakePBX()
    monkeypatch.setattr(asterisk_module, "connect", server.connect)
    return server


def settings(**changes: Any) -> AsteriskSettings:
    values: dict[str, Any] = {
        "enabled": True,
        "ari_url": "https://pbx.example/office/ari",
        "username": "api",
        "password": SecretStr("test-secret"),
        "app": "configured-app",
        "endpoint_template": "PJSIP/{number}@configured-trunk",
        "caller_id": "4000000000",
        "ring_timeout_seconds": 45,
        "media_timeout_seconds": 1,
    }
    values.update(changes)
    return AsteriskSettings(**values)


async def opened_driver(
    pbx: FakePBX, client: httpx.AsyncClient
) -> tuple[AsteriskDriver, CallbackLog]:
    log = CallbackLog()
    driver = AsteriskDriver(settings(), log.callbacks(), client=client)
    await driver.start("13800000000", "test-call")
    pbx.answered()
    await asyncio.wait_for(log.ready.wait(), 1)
    return driver, log


async def eventually(predicate: Any) -> None:
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(0)


async def test_disabled_never_opens_http_or_websocket(pbx: FakePBX) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(pbx.request)) as client:
        driver = AsteriskDriver(settings(enabled=False), CallbackLog().callbacks(), client=client)
        with pytest.raises(TelephonyError, match="尚未启用"):
            await driver.start("13800000000", "test-call")
        await driver.close()
    assert pbx.requests == []
    assert pbx.connections == []


async def test_ari_request_protocol_and_ready_only_after_answer_and_bridge(pbx: FakePBX) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(pbx.request)) as client:
        log = CallbackLog()
        driver = AsteriskDriver(settings(), log.callbacks(), client=client)
        await driver.start("13800000000", "test-call")
        assert not log.ready.is_set()
        assert len(pbx.requests) == 1
        request = pbx.requests[0]
        assert request.method == "POST"
        assert request.url.path == "/office/ari/channels"
        assert dict(request.url.params) == {
            "endpoint": "PJSIP/13800000000@configured-trunk",
            "app": "configured-app",
            "channelId": "test-call-pstn",
            "timeout": "45",
            "callerId": "4000000000",
        }
        authorization = "Basic " + base64.b64encode(b"api:test-secret").decode()
        assert request.headers["Authorization"] == authorization
        event_url, options = pbx.connections[0]
        assert urlsplit(event_url).path == "/office/ari/events"
        assert parse_qs(urlsplit(event_url).query) == {"app": ["configured-app"]}
        assert "test-secret" not in event_url
        assert options["additional_headers"] == {"Authorization": authorization}
        assert options["subprotocols"] == ["ari"]
        assert options["proxy"] is None
        pbx.events.event(
            {"type": "ChannelStateChange", "channel": {"id": "test-call-pstn", "state": "Ringing"}}
        )
        pbx.events.event({"type": "StasisStart", "channel": {"id": "unrelated", "state": "Up"}})
        await asyncio.sleep(0)
        assert not log.ready.is_set()
        pbx.answered()
        await asyncio.wait_for(log.ready.wait(), 1)
        external = next(r for r in pbx.requests if r.url.path.endswith("/externalMedia"))
        assert dict(external.url.params) == {
            "channelId": "test-call-media",
            "app": "configured-app",
            "external_host": "INCOMING",
            "transport": "websocket",
            "encapsulation": "none",
            "connection_type": "server",
            "format": "slin16",
            "direction": "both",
            "transport_data": "f(json)",
        }
        assert pbx.requests[-1].url.path.endswith("/test-call-bridge/addChannel")
        assert pbx.requests[-1].url.params["channel"] == "test-call-pstn,test-call-media"
        media_url, media_options = pbx.connections[1]
        assert media_url == "wss://pbx.example/office/media/ephemeral-connection"
        assert media_options["subprotocols"] == ["media"]
        pbx.answered()
        await asyncio.sleep(0)
        assert log.ready_count == 1
        await driver.close()


async def test_pcm_binary_frames_and_correlated_playback_ack(pbx: FakePBX) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(pbx.request)) as client:
        driver, log = await opened_driver(pbx, client)
        try:
            input_audio = b"\x00\x10" * 320
            pbx.media.incoming.put_nowait(input_audio)
            await asyncio.wait_for(log.changed.wait(), 1)
            assert log.audio == [(input_audio, 16000)]
            output_audio = b"\x01\x11" * 40000
            await driver.send_audio(output_audio)
            binaries = [item for item in pbx.media.sent if isinstance(item, bytes)]
            assert b"".join(binaries) == output_audio
            assert all(len(item) <= 65500 for item in binaries)
            assert pbx.media.commands() == [{"command": "START_MEDIA_BUFFERING"}]
            played = asyncio.create_task(driver.wait_played())
            await eventually(lambda: len(pbx.media.commands()) == 4)
            commands = pbx.media.commands()
            assert [item["command"] for item in commands] == [
                "START_MEDIA_BUFFERING",
                "STOP_MEDIA_BUFFERING",
                "MARK_MEDIA",
                "REPORT_QUEUE_DRAINED",
            ]
            token = commands[2]["correlation_id"]
            pbx.media.event({"event": "QUEUE_DRAINED", "channel_id": "test-call-media"})
            pbx.media.event({"event": "MEDIA_MARK_PROCESSED", "correlation_id": "unknown"})
            await asyncio.sleep(0)
            assert not played.done()
            pbx.media.event(
                {
                    "event": "MEDIA_MARK_PROCESSED",
                    "correlation_id": token,
                    "channel_id": "other-call-media",
                }
            )
            await asyncio.sleep(0)
            assert not played.done()
            pbx.media.event(
                {
                    "event": "MEDIA_MARK_PROCESSED",
                    "correlation_id": token,
                    "channel_id": "test-call-media",
                }
            )
            await asyncio.wait_for(played, 1)
        finally:
            await driver.close()


async def test_media_start_waits_for_stasis_before_adding_bridge(pbx: FakePBX) -> None:
    pbx.auto_media_stasis = False
    async with httpx.AsyncClient(transport=httpx.MockTransport(pbx.request)) as client:
        log = CallbackLog()
        driver = AsteriskDriver(settings(), log.callbacks(), client=client)
        await driver.start("13800000000", "test-call")
        # Up 单独先到也不请求媒体；等待 caller StasisStart。
        pbx.events.event(
            {"type": "ChannelStateChange", "channel": {"id": "test-call-pstn", "state": "Up"}}
        )
        await asyncio.sleep(0)
        assert len(pbx.requests) == 1
        pbx.answered()
        await eventually(lambda: len(pbx.connections) == 2)
        assert not log.ready.is_set()
        assert not any(r.url.path.endswith("/addChannel") for r in pbx.requests)
        pbx.events.event(
            {"type": "StasisStart", "channel": {"id": "test-call-media", "state": "Up"}}
        )
        await asyncio.wait_for(log.ready.wait(), 1)
        assert pbx.requests[-1].url.path.endswith("/addChannel")
        await driver.close()


async def test_real_loopback_websocket_auth_audio_and_playback_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实 WS Upgrade、TEXT/BINARY 帧与 ACK；HTTP 由可控 PBX 替身接收。"""
    from websockets.asyncio.client import connect

    monkeypatch.setattr(asterisk_module, "connect", connect)
    pbx = FakePBX()
    log = CallbackLog()
    event_socket: ServerConnection | None = None
    event_connected = asyncio.Event()
    received: list[str | bytes] = []
    handshakes: list[tuple[str, str, str | None]] = []

    async def websocket_handler(socket: ServerConnection) -> None:
        nonlocal event_socket
        assert socket.request is not None
        request = socket.request
        handshakes.append((request.path, request.headers["Authorization"], socket.subprotocol))
        if request.path.startswith("/office/ari/events?"):
            event_socket = socket
            event_connected.set()
            await socket.wait_closed()
            return
        assert request.path == "/office/media/ephemeral-connection"
        assert event_socket is not None
        await event_socket.send(
            json.dumps({"type": "StasisStart", "channel": {"id": "test-call-media", "state": "Up"}})
        )
        await socket.send(
            json.dumps(
                {
                    "event": "MEDIA_START",
                    "channel_id": "test-call-media",
                    "format": "slin16",
                    "optimal_frame_size": 640,
                }
            )
        )
        await socket.send(b"\x05\x00" * 320)
        async for message in socket:
            received.append(message)
            if isinstance(message, str):
                command = json.loads(message)
                if command["command"] == "MARK_MEDIA":
                    await socket.send(
                        json.dumps(
                            {
                                "event": "MEDIA_MARK_PROCESSED",
                                "correlation_id": command["correlation_id"],
                                "channel_id": "test-call-media",
                            }
                        )
                    )

    async with serve(websocket_handler, "127.0.0.1", 0, subprotocols=["ari", "media"]) as server:
        port = server.sockets[0].getsockname()[1]
        async with httpx.AsyncClient(transport=httpx.MockTransport(pbx.request)) as client:
            driver = AsteriskDriver(
                settings(ari_url=f"http://127.0.0.1:{port}/office/ari"),
                log.callbacks(),
                client=client,
            )
            try:
                await driver.start("13800000000", "test-call")
                await asyncio.wait_for(event_connected.wait(), 1)
                assert event_socket is not None
                await event_socket.send(
                    json.dumps(
                        {"type": "StasisStart", "channel": {"id": "test-call-pstn", "state": "Up"}}
                    )
                )
                await asyncio.wait_for(log.ready.wait(), 1)
                await eventually(lambda: bool(log.audio))
                assert log.audio == [(b"\x05\x00" * 320, 16000)]
                audio = b"\x03\x00" * 321  # 非整20ms帧，验证bulk协议不丢尾部。
                await driver.send_audio(audio)
                await asyncio.wait_for(driver.wait_played(), 1)
                assert [item for item in received if isinstance(item, bytes)] == [audio]
                assert handshakes[0][0] == "/office/ari/events?app=configured-app"
                assert handshakes[0][2] == "ari"
                assert handshakes[1][2] == "media"
                assert all(item[1] == "Basic YXBpOnRlc3Qtc2VjcmV0" for item in handshakes)
            finally:
                await driver.close()


async def test_flush_cancels_old_playback_ack_and_late_mark_cannot_finish_next_turn(
    pbx: FakePBX,
) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(pbx.request)) as client:
        driver, _ = await opened_driver(pbx, client)
        try:
            await driver.send_audio(b"\x01\x00" * 320)
            old_played = asyncio.create_task(driver.wait_played())
            await eventually(
                lambda: any(c["command"] == "MARK_MEDIA" for c in pbx.media.commands())
            )
            old_mark = next(
                c["correlation_id"] for c in pbx.media.commands() if c["command"] == "MARK_MEDIA"
            )
            await driver.flush()
            with pytest.raises(asyncio.CancelledError):
                await old_played
            assert pbx.media.commands()[-1] == {"command": "FLUSH_MEDIA"}
            await driver.send_audio(b"\x02\x00" * 320)
            new_played = asyncio.create_task(driver.wait_played())
            await eventually(
                lambda: sum(c["command"] == "MARK_MEDIA" for c in pbx.media.commands()) == 2
            )
            new_mark = [
                c["correlation_id"] for c in pbx.media.commands() if c["command"] == "MARK_MEDIA"
            ][-1]
            assert new_mark != old_mark
            pbx.media.event({"event": "MEDIA_MARK_PROCESSED", "correlation_id": old_mark})
            await asyncio.sleep(0)
            assert not new_played.done()
            pbx.media.event({"event": "MEDIA_MARK_PROCESSED", "correlation_id": new_mark})
            await asyncio.wait_for(new_played, 1)
        finally:
            await driver.close()


async def test_xoff_flush_drops_waiting_old_audio_and_new_audio_can_play(pbx: FakePBX) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(pbx.request)) as client:
        driver, _ = await opened_driver(pbx, client)
        try:
            pbx.media.event({"event": "MEDIA_XOFF"})
            await eventually(lambda: not driver._can_send.is_set())
            old_sender = asyncio.create_task(driver.send_audio(b"\x01\x00" * 320))
            await asyncio.sleep(0)
            assert not old_sender.done()
            await driver.flush()
            await asyncio.wait_for(old_sender, 1)
            await driver.send_audio(b"\x02\x00" * 320)
            assert [m for m in pbx.media.sent if isinstance(m, bytes)] == [b"\x02\x00" * 320]
        finally:
            await driver.close()


async def test_media_format_mismatch_ends_and_cleans_resources(pbx: FakePBX) -> None:
    pbx.codec = "ulaw"
    async with httpx.AsyncClient(transport=httpx.MockTransport(pbx.request)) as client:
        log = CallbackLog()
        driver = AsteriskDriver(settings(), log.callbacks(), client=client)
        await driver.start("13800000000", "test-call")
        pbx.answered()
        await eventually(lambda: bool(log.ended))
        assert log.ready_count == 0
        assert log.ended == ["provider_error"]
        assert "音频格式不匹配" in log.errors[0]
        deleted = [r.url.path for r in pbx.requests if r.method == "DELETE"]
        assert deleted == [
            "/office/ari/channels/test-call-pstn",
            "/office/ari/channels/test-call-media",
            "/office/ari/bridges/test-call-bridge",
        ]
        assert pbx.events.closed and pbx.media.closed


@pytest.mark.parametrize("cause,reason", [(17, "busy"), (18, "no_answer"), (21, "rejected")])
async def test_unanswered_call_reason_and_cleanup(pbx: FakePBX, cause: int, reason: str) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(pbx.request)) as client:
        log = CallbackLog()
        driver = AsteriskDriver(settings(), log.callbacks(), client=client)
        await driver.start("13800000000", "test-call")
        pbx.events.event(
            {"type": "ChannelDestroyed", "channel": {"id": "test-call-pstn"}, "cause": cause}
        )
        await eventually(lambda: bool(log.ended))
        assert log.ended == [reason]
        assert log.ready_count == 0
        assert len(pbx.connections) == 1
        await driver.close()
        assert len([r for r in pbx.requests if r.method == "DELETE"]) == 3


async def test_close_during_media_creation_cancels_background_setup(pbx: FakePBX) -> None:
    pbx.hold_media = asyncio.Event()
    async with httpx.AsyncClient(transport=httpx.MockTransport(pbx.request)) as client:
        log = CallbackLog()
        driver = AsteriskDriver(settings(), log.callbacks(), client=client)
        await driver.start("13800000000", "test-call")
        pbx.answered()
        await asyncio.wait_for(pbx.media_created.wait(), 1)
        closing = asyncio.create_task(driver.close())
        await asyncio.sleep(0)
        assert not closing.done()
        pbx.hold_media.set()
        await asyncio.wait_for(closing, 1)
        assert log.ready_count == 0
        assert len(pbx.connections) == 1
        assert pbx.events.closed
        assert not driver._tasks
        assert len([r for r in pbx.requests if r.method == "DELETE"]) == 3


async def test_close_during_event_handshake_never_originates_late_call(pbx: FakePBX) -> None:
    pbx.hold_events = asyncio.Event()
    async with httpx.AsyncClient(transport=httpx.MockTransport(pbx.request)) as client:
        log = CallbackLog()
        driver = AsteriskDriver(settings(), log.callbacks(), client=client)
        start = asyncio.create_task(driver.start("13800000000", "test-call"))
        await eventually(lambda: bool(pbx.connections))
        await asyncio.wait_for(driver.close(), 1)
        with pytest.raises(asyncio.CancelledError):
            await start
        pbx.hold_events.set()
        await asyncio.sleep(0)
        assert not any(request.method == "POST" for request in pbx.requests)
        assert log.ready_count == 0
        assert not driver._tasks


async def test_ari_error_does_not_expose_body_or_credentials(pbx: FakePBX) -> None:
    pbx.fail_path = "/channels"
    async with httpx.AsyncClient(transport=httpx.MockTransport(pbx.request)) as client:
        driver = AsteriskDriver(settings(), CallbackLog().callbacks(), client=client)
        with pytest.raises(TelephonyError, match="HTTP 503") as error:
            await driver.start("13800000000", "test-call")
        assert "test-secret" not in str(error.value)
        assert "provider" not in str(error.value)
        assert pbx.events.closed


async def test_hangup_closes_sockets_once_and_cancels_pending_playback(pbx: FakePBX) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(pbx.request)) as client:
        driver, log = await opened_driver(pbx, client)
        await driver.send_audio(b"\x01\x00" * 320)
        played = asyncio.create_task(driver.wait_played())
        await eventually(lambda: any(c["command"] == "MARK_MEDIA" for c in pbx.media.commands()))
        await driver.hangup()
        with pytest.raises(asyncio.CancelledError):
            await played
        await driver.hangup()
        assert log.ended == ["hangup"]
        assert pbx.events.closed and pbx.media.closed
        assert len([r for r in pbx.requests if r.method == "DELETE"]) == 3
        with pytest.raises(TelephonyError, match="重复启动"):
            await driver.start("13800000000", "test-call")


@pytest.mark.parametrize("create_path", ["/channels", "/bridges", "/channels/externalMedia"])
@pytest.mark.parametrize("cancel_waiter", [False, True])
async def test_close_waits_for_independent_late_creation_before_deleting(
    pbx: FakePBX, create_path: str, cancel_waiter: bool
) -> None:
    accepted, complete = asyncio.Event(), asyncio.Event()
    remote: set[str] = set()
    jobs: list[asyncio.Task[None]] = []
    deletes: list[tuple[str, bool]] = []

    async def create_remote(resource: str) -> None:
        await complete.wait()
        remote.add(resource)

    async def request(message: httpx.Request) -> httpx.Response:
        path = message.url.path.removeprefix("/office/ari")
        if message.method == "DELETE":
            pbx.requests.append(message)
            resource = path.rsplit("/", 1)[1]
            existed = resource in remote
            remote.discard(resource)
            deletes.append((resource, existed))
            return httpx.Response(204 if existed else 404)
        response = await pbx.request(message)
        if message.method == "POST" and path in {
            "/channels",
            "/bridges",
            "/channels/externalMedia",
        }:
            resource = message.url.params["bridgeId" if path == "/bridges" else "channelId"]
            if path == create_path:
                # 模拟远端已接受请求；本地 HTTP 取消后，服务器仍会独立完成创建。
                job = asyncio.create_task(create_remote(resource))
                jobs.append(job)
                accepted.set()
                await asyncio.shield(job)
            else:
                remote.add(resource)
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(request)) as client:
        # 清理预算比迟到的创建完成更短；创建结果须使用独立请求期限。
        config = settings().model_copy(update={"cleanup_timeout_seconds": 0.01})
        driver = AsteriskDriver(config, CallbackLog().callbacks(), client=client)
        starting = asyncio.create_task(driver.start("13800000000", "test-call"))
        closing: asyncio.Task[None] | None = None
        try:
            if create_path != "/channels":
                await starting
                pbx.answered()
            await asyncio.wait_for(accepted.wait(), 1)
            closing = asyncio.create_task(driver.close())
            await asyncio.sleep(0.02)
            assert not closing.done()
            if cancel_waiter:
                closing.cancel()
                await asyncio.sleep(0)
                assert not closing.done()
            complete.set()
            if cancel_waiter:
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(closing, 1)
            else:
                await asyncio.wait_for(closing, 1)
            await asyncio.gather(starting, return_exceptions=True)
            await asyncio.gather(*jobs)
            assert remote == set()
            assert any(existed for _, existed in deletes)
            assert len(deletes) == 3
            assert not driver._creates and not driver._tasks
            assert pbx.events.closed
            assert (
                len([item for item in pbx.requests if item.method == "POST"])
                == ({"/channels": 1, "/bridges": 2, "/channels/externalMedia": 3}[create_path])
            )
            await driver.close()
            assert len(deletes) == 3
        finally:
            complete.set()
            await asyncio.gather(starting, *jobs, return_exceptions=True)
            if closing is not None:
                await asyncio.gather(closing, return_exceptions=True)
            await driver.close()


@pytest.mark.parametrize(
    "failed_resource", ["test-call-pstn", "test-call-media", "test-call-bridge"]
)
async def test_cleanup_http_failure_is_reported_after_every_resource_and_client_closes(
    pbx: FakePBX, failed_resource: str
) -> None:
    async def request(message: httpx.Request) -> httpx.Response:
        if message.method == "DELETE" and message.url.path.endswith("/" + failed_resource):
            pbx.requests.append(message)
            return httpx.Response(503, json={"message": "test-secret upstream body"})
        return await pbx.request(message)

    client = httpx.AsyncClient(transport=httpx.MockTransport(request))
    driver, _ = await opened_driver(pbx, client)
    driver._owns_client = True
    with pytest.raises(TelephonyError, match="清理失败") as error:
        await driver.close()
    assert "test-secret" not in str(error.value) and "upstream" not in str(error.value)
    assert client.is_closed and pbx.events.closed and pbx.media.closed
    assert len([item for item in pbx.requests if item.method == "DELETE"]) == 3
    await driver.close()
    assert len([item for item in pbx.requests if item.method == "DELETE"]) == 3


async def test_cleanup_timeout_is_shared_by_deletes_and_websockets_and_closes_client(
    pbx: FakePBX,
) -> None:
    cancelled: list[str] = []

    async def request(message: httpx.Request) -> httpx.Response:
        if message.method == "DELETE":
            pbx.requests.append(message)
            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                cancelled.append(message.url.path)
                raise
        return await pbx.request(message)

    async def blocked_close() -> None:
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            cancelled.append("events-websocket")
            raise

    client = httpx.AsyncClient(transport=httpx.MockTransport(request))
    driver, _ = await opened_driver(pbx, client)
    driver._owns_client = True
    driver._settings.cleanup_timeout_seconds = 0.03
    pbx.events.close = blocked_close
    started = asyncio.get_running_loop().time()
    with pytest.raises(TelephonyError, match="清理失败或超时"):
        await driver.close()
    assert asyncio.get_running_loop().time() - started < 0.3
    assert client.is_closed and pbx.media.closed
    assert len(cancelled) == 4
    assert len([item for item in pbx.requests if item.method == "DELETE"]) == 3
    await driver.close()


async def test_websocket_cleanup_failure_does_not_skip_other_resources(pbx: FakePBX) -> None:
    async def broken_close() -> None:
        raise RuntimeError("test-secret websocket details")

    client = httpx.AsyncClient(transport=httpx.MockTransport(pbx.request))
    driver, _ = await opened_driver(pbx, client)
    driver._owns_client = True
    pbx.media.close = broken_close
    with pytest.raises(TelephonyError, match="清理失败") as error:
        await driver.close()
    assert "test-secret" not in str(error.value)
    assert pbx.events.closed and client.is_closed
    assert len([item for item in pbx.requests if item.method == "DELETE"]) == 3


@pytest.mark.parametrize("terminal", ["finish", "fail"])
async def test_cleanup_failure_still_notifies_error_and_end(pbx: FakePBX, terminal: str) -> None:
    async def request(message: httpx.Request) -> httpx.Response:
        if message.method == "DELETE":
            pbx.requests.append(message)
            return httpx.Response(503)
        return await pbx.request(message)

    async with httpx.AsyncClient(transport=httpx.MockTransport(request)) as client:
        driver, log = await opened_driver(pbx, client)
        if terminal == "finish":
            with pytest.raises(TelephonyError, match="清理失败"):
                await driver._finish("hangup")
        else:
            await driver._fail("原始媒体故障。")
        assert len(log.errors) == 1 and "清理失败" in log.errors[0]
        assert log.ended == ["provider_error"]
        await driver.close()
        assert len(log.errors) == 1 and log.ended == ["provider_error"]


def test_cleanup_budget_default_and_limits() -> None:
    assert AsteriskSettings().cleanup_timeout_seconds == 5
    assert AsteriskSettings(cleanup_timeout_seconds=1).cleanup_timeout_seconds == 1
    assert AsteriskSettings(cleanup_timeout_seconds=30).cleanup_timeout_seconds == 30
    for value in (0.5, 31):
        with pytest.raises(ValidationError):
            AsteriskSettings(cleanup_timeout_seconds=value)


async def test_unconfirmed_creation_timeout_reports_remote_cleanup_uncertainty(
    pbx: FakePBX,
) -> None:
    async def request(message: httpx.Request) -> httpx.Response:
        if message.method == "POST":
            pbx.requests.append(message)
            await asyncio.Event().wait()
        return await pbx.request(message)

    client = httpx.AsyncClient(transport=httpx.MockTransport(request))
    config = settings().model_copy(update={"media_timeout_seconds": 0.03})
    driver = AsteriskDriver(config, CallbackLog().callbacks(), client=client)
    driver._owns_client = True
    with pytest.raises(TelephonyError, match="清理失败或超时"):
        await driver.start("13800000000", "test-call")
    assert client.is_closed and pbx.events.closed
    assert len([item for item in pbx.requests if item.method == "DELETE"]) == 3
    assert not driver._creates
    await driver.close()


def inbound_event(remote_id: str = "1728000000.12", **changes: Any) -> dict[str, Any]:
    event: dict[str, Any] = {
        "type": "StasisStart",
        "application": "llmautotel-inbound",
        "args": ["llmautotel-inbound"],
        "channel": {
            "id": remote_id,
            "state": "Ring",
            "caller": {"number": "13800138000"},
            "dialplan": {"exten": "4001234567"},
        },
    }
    event.update(changes)
    return event


async def test_inbound_answer_existing_channel_shared_events_and_pcm(pbx: FakePBX) -> None:
    calls: list[IncomingCall] = []
    errors: list[str] = []

    async def accept(call: IncomingCall) -> bool:
        calls.append(call)
        return True

    async def error(message: str) -> None:
        errors.append(message)

    configured = settings(inbound_enabled=True, endpoint_template="", caller_id="")
    async with httpx.AsyncClient(transport=httpx.MockTransport(pbx.request)) as client:
        listener = AsteriskIncomingListener(
            configured, IncomingCallbacks(accept, error), client=client
        )
        await listener.start()
        assert listener.connected
        pbx.events.event(inbound_event())
        await eventually(lambda: len(calls) == 1)
        call = calls[0]
        assert (call.caller, call.destination, call.remote_id) == (
            "13800138000",
            "4001234567",
            "1728000000.12",
        )
        log = CallbackLog()
        driver = AsteriskDriver(configured, log.callbacks(), incoming=call, client=client)
        await driver.start("", "inbound-test")
        await asyncio.wait_for(log.ready.wait(), 1)
        assert len(pbx.connections) == 2  # 一条共享 ARI、一条单通媒体；没有抢应用的第二条 ARI。
        assert parse_qs(urlsplit(pbx.connections[0][0]).query) == {"app": ["llmautotel-inbound"]}
        assert not any(request.url.path.endswith("/ari/channels") for request in pbx.requests)
        assert any(
            request.url.path.endswith("/channels/1728000000.12/answer") for request in pbx.requests
        )
        external = next(
            request for request in pbx.requests if request.url.path.endswith("externalMedia")
        )
        assert external.url.params["app"] == "llmautotel-inbound"
        pbx.media.incoming.put_nowait(b"\x01\x00" * 320)
        await eventually(lambda: len(log.audio) == 1)
        await driver.send_audio(b"\x02\x00" * 320)
        assert b"\x02\x00" * 320 in pbx.media.sent
        # 停止接纳入口不关闭已借给单通的事件连接，不挂电话。
        await listener.close()
        assert not pbx.events.closed
        assert not any(request.method == "DELETE" for request in pbx.requests)
        await driver.close()
        await eventually(lambda: pbx.events.closed)
        assert errors == []


async def test_inbound_scoping_busy_dedup_and_prestart_close(pbx: FakePBX) -> None:
    calls: list[IncomingCall] = []
    errors: list[str] = []

    async def accept(call: IncomingCall) -> bool:
        calls.append(call)
        return len(calls) == 1

    async def error(message: str) -> None:
        errors.append(message)

    async with httpx.AsyncClient(transport=httpx.MockTransport(pbx.request)) as client:
        listener = AsteriskIncomingListener(
            settings(inbound_enabled=True, inbound_numbers=["4001234567"]),
            IncomingCallbacks(accept, error),
            client=client,
        )
        await listener.start()
        pbx.events.event(inbound_event("foreign-app", application="unrelated"))
        pbx.events.event(inbound_event("foreign-marker", args=["someone-else"]))
        pbx.events.event(inbound_event("no-marker", args=[]))
        pbx.events.event(inbound_event())
        pbx.events.event(inbound_event())
        pbx.events.event(inbound_event("busy.1"))
        pbx.events.event(
            inbound_event(
                "wrong-number", channel={"id": "wrong-number", "dialplan": {"exten": "4009999999"}}
            )
        )
        await eventually(lambda: len(calls) == 2 and len(pbx.requests) == 2)
        assert [call.remote_id for call in calls] == ["1728000000.12", "busy.1"]
        assert all(
            request.method == "DELETE" and request.url.params["reason"] == "busy"
            for request in pbx.requests
        )
        assert not any("foreign" in request.url.path for request in pbx.requests)
        await calls[0].close()  # session task 尚未启动，仍立即清理原通道。
        await calls[0].close()
        assert len(pbx.requests) == 3
        assert pbx.requests[-1].url.params["reason"] == "normal"
        await listener.close()
        assert errors == []


async def test_inbound_disabled_does_not_connect_or_require_outbound_fields(pbx: FakePBX) -> None:
    async def accept(call: IncomingCall) -> bool:
        raise AssertionError("must not admit")

    async def error(message: str) -> None:
        return

    listener = AsteriskIncomingListener(
        settings(inbound_enabled=False), IncomingCallbacks(accept, error)
    )
    with pytest.raises(TelephonyError, match="尚未启用"):
        await listener.start()
    await listener.close()
    assert pbx.connections == []
    configured = settings(inbound_enabled=True, endpoint_template="", caller_id="")
    assert configured.missing_inbound_fields() == []
    with pytest.raises(ValidationError, match="不同 ARI 应用"):
        settings(inbound_enabled=True, incoming_app="configured-app")


async def test_inbound_disconnect_reconnect_does_not_readmit_existing_id(
    pbx: FakePBX,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[IncomingCall] = []
    errors: list[str] = []
    sockets = [FakeSocket(), FakeSocket()]

    async def connect(uri: str, **kwargs: Any) -> FakeSocket:
        return sockets.pop(0)

    async def accept(call: IncomingCall) -> bool:
        calls.append(call)
        return True

    async def error(message: str) -> None:
        errors.append(message)

    first, second = sockets
    monkeypatch.setattr(asterisk_module, "connect", connect)
    configured = settings(inbound_enabled=True)
    configured.inbound_reconnect_seconds = 0.01
    async with httpx.AsyncClient(transport=httpx.MockTransport(pbx.request)) as client:
        listener = AsteriskIncomingListener(
            configured, IncomingCallbacks(accept, error), client=client
        )
        await listener.start()
        first.event(inbound_event())
        await eventually(lambda: len(calls) == 1)
        first.incoming.put_nowait(None)
        await eventually(lambda: listener.connected and not sockets)
        second.event(inbound_event())
        second.event(inbound_event("new.1"))
        await eventually(lambda: len(calls) == 2)
        assert [call.remote_id for call in calls] == ["1728000000.12", "new.1"]
        assert errors and "test-secret" not in errors[0]
        await calls[0].close()
        await calls[1].close()
        await listener.close()
