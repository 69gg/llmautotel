"""真实 aiortc、HTTP 信令、RTVI 和语音管线联通测试，不访问外部模型。"""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from aiortc import RTCConfiguration, RTCDataChannel, RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import AudioStreamTrack, MediaStreamError, MediaStreamTrack
from pipecat.frames.frames import (
    Frame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TTSAudioRawFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.processors.frameworks.rtvi import models as rtvi_models

import llmautotel.voice as voice_module
from llmautotel.config import RuntimeConfig
from llmautotel.main import create_app
from llmautotel.models import AppSettings, TTSSettings
from llmautotel.providers import CompatibleTTSService


class SilentSTT(FrameProcessor):
    """仅透传静音输入，避免访问真实 ASR。"""

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)


class OpeningLLM(FrameProcessor):
    def __init__(self) -> None:
        super().__init__()
        self.inputs: list[list[dict[str, Any]]] = []

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMContextFrame):
            self.inputs.append([dict(message) for message in frame.context.get_messages()])
            await self.push_frame(LLMFullResponseStartFrame())
            await self.push_frame(LLMTextFrame("您好，我是 AI 助手。"))
            await self.push_frame(LLMFullResponseEndFrame())
        else:
            await self.push_frame(frame, direction)


class ToneTTS(CompatibleTTSService):
    """用已知非静音 PCM 替换模型 HTTP，保留框架句级生命周期。"""

    def __init__(self, settings: TTSSettings) -> None:
        super().__init__(settings)
        self.requests: list[str] = []

    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame, None]:
        self.requests.append(text)
        rate = self._provider_settings.sample_rate
        sample_count = rate // 25  # 40 ms，符合输出运输层默认音频分块。
        pcm = b"".join(
            int(8000 * math.sin(2 * math.pi * 440 * sample / rate)).to_bytes(
                2, "little", signed=True
            )
            for sample in range(sample_count)
        )
        for _ in range(20):
            yield TTSAudioRawFrame(pcm, rate, 1, context_id=context_id)


@dataclass
class ControlledServices:
    stt: SilentSTT
    llm: OpeningLLM
    tts: ToneTTS
    closed: bool = False

    async def aclose(self) -> None:
        await self.tts.aclose()
        self.closed = True


@dataclass
class ClientPeer:
    pc: RTCPeerConnection
    channel: RTCDataChannel
    input_track: AudioStreamTrack
    opened: asyncio.Event = field(default_factory=asyncio.Event)
    audio_received: asyncio.Event = field(default_factory=asyncio.Event)
    messages_changed: asyncio.Event = field(default_factory=asyncio.Event)
    messages: list[dict[str, Any]] = field(default_factory=list)
    decoded_sample_rates: list[int] = field(default_factory=list)
    readers: list[asyncio.Task[None]] = field(default_factory=list)

    async def close(self) -> None:
        self.input_track.stop()
        await self.pc.close()
        for task in self.readers:
            task.cancel()
        await asyncio.gather(*self.readers, return_exceptions=True)

    def ready(self, request_id: str) -> None:
        self.channel.send(
            json.dumps(
                {
                    "label": rtvi_models.MESSAGE_LABEL,
                    "type": "client-ready",
                    "id": request_id,
                    "data": {
                        "version": rtvi_models.PROTOCOL_VERSION,
                        "about": {"library": "aiortc-integration-test"},
                    },
                }
            )
        )


def client_peer() -> ClientPeer:
    pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    input_track = AudioStreamTrack()  # aiortc 自带静音轨道，不打开任何真实麦克风。
    pc.addTrack(input_track)
    channel = pc.createDataChannel("pipecat")
    peer = ClientPeer(pc, channel, input_track)

    @channel.on("open")
    def on_open() -> None:
        peer.opened.set()

    @channel.on("message")
    def on_message(raw: str | bytes) -> None:
        if isinstance(raw, str) and raw.startswith("pong"):
            return
        message = json.loads(raw)
        if isinstance(message, dict):
            peer.messages.append(message)
            peer.messages_changed.set()

    async def consume(track: MediaStreamTrack) -> None:
        try:
            while True:
                frame = await track.recv()
                if track.kind == "audio":
                    peer.decoded_sample_rates.append(frame.sample_rate)
                    values = frame.to_ndarray().flatten().tolist()
                    if values and max(abs(value) for value in values) > 500:
                        peer.audio_received.set()
        except MediaStreamError:
            return

    @pc.on("track")
    def on_track(track: MediaStreamTrack) -> None:
        peer.readers.append(asyncio.create_task(consume(track)))

    return peer


def strip_ice_candidates(sdp: str) -> tuple[str, list[dict[str, Any]]]:
    """将 aiortc 收集的真实候选地址放入 PATCH，验证实际 trickle 路径。"""
    sections = sdp.split("\r\nm=")
    output: list[str] = [sections[0]]
    candidates: list[dict[str, Any]] = []
    for index, section in enumerate(sections[1:]):
        lines = section.split("\r\n")
        mid = next(line.removeprefix("a=mid:") for line in lines if line.startswith("a=mid:"))
        for line in lines:
            if line.startswith("a=candidate:"):
                candidates.append(
                    {
                        "candidate": line.removeprefix("a=candidate:"),
                        "sdp_mid": mid,
                        "sdp_mline_index": index,
                    }
                )
        output.append(
            "\r\n".join(
                line
                for line in lines
                if not line.startswith(("a=candidate:", "a=end-of-candidates"))
            )
        )
    return "\r\nm=".join(output), candidates


async def negotiate(client: httpx.AsyncClient, call_id: str, peer: ClientPeer) -> str:
    await peer.pc.setLocalDescription(await peer.pc.createOffer())
    assert peer.pc.localDescription is not None
    sdp, candidates = strip_ice_candidates(peer.pc.localDescription.sdp)
    assert candidates
    response = await client.post(
        f"/api/offer?call_id={call_id}", json={"sdp": sdp, "type": "offer"}
    )
    assert response.status_code == 200, response.text
    answer = response.json()
    assert answer["type"] == "answer"
    assert "a=candidate:" in answer["sdp"]
    patched = await client.patch(
        f"/api/offer?call_id={call_id}",
        json={
            "pc_id": answer["pc_id"],
            "candidates": candidates,
        },
    )
    assert patched.status_code == 200, patched.text
    await peer.pc.setRemoteDescription(
        RTCSessionDescription(sdp=answer["sdp"], type=answer["type"])
    )
    await asyncio.wait_for(peer.opened.wait(), timeout=5)
    return str(answer["pc_id"])


async def wait_transcript(peer: ClientPeer) -> dict[str, Any]:
    async with asyncio.timeout(5):
        while True:
            for message in peer.messages:
                if message.get("type") == "server-message":
                    data = message.get("data", {})
                    if data.get("type") == "transcript":
                        return data["entry"]
            peer.messages_changed.clear()
            await peer.messages_changed.wait()


def configured_settings(opening: str) -> AppSettings:
    return AppSettings.model_validate(
        {
            "conversation": {"mode": "sales"},
            "sales": {
                "goal": "介绍本机测试订阅",
                "product_info": "测试订阅每月十元",
                "opening": opening,
            },
            "asr": {"base_url": "http://fixture.invalid/v1", "model": "controlled-asr"},
            "llm": {"base_url": "http://fixture.invalid/v1", "model": "controlled-llm"},
            "tts": {
                "base_url": "http://fixture.invalid/v1",
                "model": "controlled-tts",
                "voice": "fixture",
            },
        }
    )


@pytest.mark.parametrize("opening", ["您好，我是测试 AI。", ""])
async def test_real_webrtc_rtvi_opening_audio_hangup_and_immediate_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    opening: str,
) -> None:
    bundles: list[ControlledServices] = []

    def factory(settings: AppSettings) -> ControlledServices:
        bundle = ControlledServices(SilentSTT(), OpeningLLM(), ToneTTS(settings.tts))
        bundles.append(bundle)
        return bundle

    monkeypatch.setattr(voice_module, "create_services", factory)
    app = create_app(RuntimeConfig(data_dir=tmp_path, frontend_dir=tmp_path / "missing-ui"))
    peers: list[ClientPeer] = []
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://local.test"
        ) as client:
            response = await client.put(
                "/api/settings", json=configured_settings(opening).private()
            )
            assert response.status_code == 200
            try:
                for index in range(2):
                    started = await client.post("/api/calls")
                    assert started.status_code == 201
                    call_id = started.json()["call"]["id"]
                    peer = client_peer()
                    peers.append(peer)
                    pc_id = await negotiate(client, call_id, peer)
                    active = app.state.sessions._active
                    assert active is not None and active.connection is not None
                    connection = active.connection
                    assert connection.pc_id == pc_id
                    assert connection.pc.connectionState == "connected"
                    assert connection.is_connected()
                    assert bundles[index].tts.requests == []  # ready 前不主动发言。

                    peer.ready(f"ready-{index}-1")
                    peer.ready(f"ready-{index}-2")
                    transcript = await wait_transcript(peer)
                    await asyncio.wait_for(peer.audio_received.wait(), timeout=5)
                    expected = opening or "您好，我是 AI 助手。"
                    assert transcript["text"] == expected and transcript["role"] == "assistant"
                    assert bundles[index].tts.requests == [expected]
                    assert len(bundles[index].llm.inputs) == (0 if opening else 1)
                    assert peer.decoded_sample_rates and set(peer.decoded_sample_rates) == {48000}
                    record = (await client.get(f"/api/calls/{call_id}")).json()
                    assert record["transcript"][0]["text"] == expected

                    # 错误 pc_id 的 PATCH 与重复连接不能抢占当前真正连接。
                    wrong_patch = await client.patch(
                        f"/api/offer?call_id={call_id}",
                        json={
                            "pc_id": "other-peer",
                            "candidates": [],
                        },
                    )
                    assert wrong_patch.status_code == 404
                    duplicate = await client.post(
                        f"/api/offer?call_id={call_id}",
                        json={
                            "sdp": peer.pc.localDescription.sdp,
                            "type": "offer",
                        },
                    )
                    assert duplicate.status_code == 409
                    assert app.state.sessions._active.connection is connection

                    ended = await client.post(f"/api/calls/{call_id}/end")
                    assert ended.status_code == 200
                    assert ended.json()["status"] == "ended"
                    assert ended.json()["end_reason"] == "user_hangup"
                    assert (await client.get("/api/calls/active")).json() is None
                    assert connection.pc.connectionState == "closed"
                    assert active.handler._pcs_map == {}
                    assert active.task is not None and active.task.done()
                    assert bundles[index].closed
                    assert bundles[index].tts._http_client.is_closed
                    await peer.close()
                    assert peer.input_track.readyState == "ended"
                    assert peer.pc.connectionState == "closed"
                    stale = await client.patch(
                        f"/api/offer?call_id={call_id}",
                        json={
                            "pc_id": pc_id,
                            "candidates": [],
                        },
                    )
                    assert stale.status_code == 409
            finally:
                for peer in peers:
                    await peer.close()


async def test_two_real_clients_compete_for_one_signaling_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundles: list[ControlledServices] = []

    def factory(settings: AppSettings) -> ControlledServices:
        bundle = ControlledServices(SilentSTT(), OpeningLLM(), ToneTTS(settings.tts))
        bundles.append(bundle)
        return bundle

    monkeypatch.setattr(voice_module, "create_services", factory)
    app = create_app(RuntimeConfig(data_dir=tmp_path, frontend_dir=tmp_path / "missing-ui"))
    peers = [client_peer(), client_peer()]
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://local.test"
        ) as client:
            await client.put("/api/settings", json=configured_settings("您好。").private())
            started = await client.post("/api/calls")
            call_id = started.json()["call"]["id"]
            try:
                for peer in peers:
                    await peer.pc.setLocalDescription(await peer.pc.createOffer())
                offers = [{"sdp": peer.pc.localDescription.sdp, "type": "offer"} for peer in peers]
                results = await asyncio.gather(
                    *[client.post(f"/api/offer?call_id={call_id}", json=offer) for offer in offers]
                )
                assert sorted(result.status_code for result in results) == [200, 409]
                active = app.state.sessions._active
                assert active is not None and active.connection is not None
                assert len(active.handler._pcs_map) == 1
                winner = next(
                    index for index, result in enumerate(results) if result.status_code == 200
                )
                answer = results[winner].json()
                await peers[winner].pc.setRemoteDescription(
                    RTCSessionDescription(
                        sdp=answer["sdp"],
                        type=answer["type"],
                    )
                )
                await asyncio.wait_for(peers[winner].opened.wait(), timeout=5)
                peers[winner].ready("race-ready")
                await wait_transcript(peers[winner])
                assert len(bundles) == 1
                assert bundles[0].tts.requests == ["您好。"]
                await client.post(f"/api/calls/{call_id}/end")
                assert bundles[0].closed
                assert (await client.get("/api/calls/active")).json() is None
            finally:
                for peer in peers:
                    await peer.close()
