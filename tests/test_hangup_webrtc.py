"""真实 WebRTC 与 SDK 工具调用完成告别后挂断，不使用外部模型或麦克风。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import httpx2
import pytest
from aiortc.mediastreams import MediaStreamTrack
from av import AudioFrame
from pipecat.frames.frames import (
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from test_webrtc import (
    ClientPeer,
    SilentSTT,
    ToneTTS,
    client_peer,
    configured_settings,
    negotiate,
    wait_transcript,
)

import llmautotel.voice as voice_module
from llmautotel.config import RuntimeConfig
from llmautotel.main import create_app
from llmautotel.models import AppSettings
from llmautotel.providers import CompatibleLLMService


@dataclass
class HangupServices:
    stt: SilentSTT
    llm: CompatibleLLMService
    tts: ToneTTS
    closed: bool = False

    async def aclose(self) -> None:
        await self.llm.aclose()
        await self.tts.aclose()
        self.closed = True


async def wait_ai_hangup(peer: ClientPeer) -> dict[str, Any]:
    async with asyncio.timeout(8):
        while True:
            for message in peer.messages:
                if message.get("type") == "server-message":
                    data = message.get("data", {})
                    if data.get("type") == "call-ended":
                        return data
            peer.messages_changed.clear()
            await peer.messages_changed.wait()


async def test_real_webrtc_ai_tool_hangup_preserves_goodbye_and_releases_call_slot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[dict[str, Any]] = []
    bundles: list[HangupServices] = []
    evidence = "不用了，谢谢。"
    goodbye = "好的，祝您一切顺利，再见。"
    opening = "您好，我是测试 AI。"

    async def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(json.loads(await request.aread()))
        chunk = {
            "id": "reply-hangup",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "controlled-llm",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "hangup-1",
                                "type": "function",
                                "function": {
                                    "name": "hang_up",
                                    "arguments": json.dumps(
                                        {
                                            "confirmed": True,
                                            "evidence": evidence,
                                            "goodbye": goodbye,
                                        },
                                        ensure_ascii=False,
                                    ),
                                },
                            }
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ],
        }
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=f"data: {json.dumps(chunk, ensure_ascii=False)}\n\ndata: [DONE]\n\n".encode(),
        )

    def factory(settings: AppSettings) -> HangupServices:
        bundle = HangupServices(
            SilentSTT(),
            CompatibleLLMService(
                settings.llm,
                http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
            ),
            ToneTTS(settings.tts),
        )
        bundles.append(bundle)
        return bundle

    monkeypatch.setattr(voice_module, "create_services", factory)
    app = create_app(RuntimeConfig(data_dir=tmp_path, frontend_dir=tmp_path / "missing-ui"))
    peers: list[ClientPeer] = []
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://local.test",
        ) as client:
            response = await client.put(
                "/api/settings",
                json=configured_settings(opening).private(),
            )
            assert response.status_code == 200
            try:
                started = await client.post("/api/calls")
                call_id = started.json()["call"]["id"]
                peer = client_peer()
                peers.append(peer)
                non_silent_samples: list[int] = []
                quiet = asyncio.Event()

                @peer.pc.on("track")
                def observe_audio(track: MediaStreamTrack) -> None:
                    if track.kind != "audio":
                        return
                    original_recv = track.recv
                    silent_frames = 0

                    async def receive_audio() -> AudioFrame:
                        nonlocal silent_frames
                        frame = await original_recv()
                        assert isinstance(frame, AudioFrame)
                        values = frame.to_ndarray().flatten().tolist()
                        if values and max(abs(value) for value in values) > 500:
                            non_silent_samples.append(frame.samples)
                            silent_frames = 0
                            quiet.clear()
                        elif non_silent_samples:
                            silent_frames += 1
                            if silent_frames >= 4:
                                quiet.set()
                        return frame

                    # 仅观察原 client_peer 唯一消费链，继续使用真正 RTP/Opus 解码。
                    monkeypatch.setattr(track, "recv", receive_audio)

                await negotiate(client, call_id, peer)
                closed = asyncio.Event()
                peer.channel.on("close", closed.set)
                peer.ready("ready-hangup")
                assert (await wait_transcript(peer))["text"] == opening
                await asyncio.wait_for(peer.audio_received.wait(), timeout=5)
                assert requests == []
                # 开场文字提交时 jitter buffer 可能仍有尾帧，等解码后稳定静音再取基线。
                await asyncio.wait_for(quiet.wait(), timeout=5)
                opening_audio_frames = len(non_silent_samples)
                peer.audio_received.clear()

                active = app.state.sessions._active
                assert active is not None and active.voice is not None
                assert active.connection is not None and active.task is not None
                session = active.voice
                assert isinstance(session, voice_module.VoiceSession)
                assert session._worker is not None
                # 静音输入轨道继续发送；只注入最终转写，跳过 ASR 网络与真实设备。
                await session._worker.queue_frames(
                    [
                        VADUserStartedSpeakingFrame(),
                        VADUserStoppedSpeakingFrame(stop_secs=0.6),
                        TranscriptionFrame(evidence, "user", "confirmed-end", finalized=True),
                    ]
                )
                await asyncio.wait_for(peer.audio_received.wait(), timeout=5)
                assert await wait_ai_hangup(peer) == {
                    "type": "call-ended",
                    "reason": "ai_hangup",
                }
                await asyncio.wait_for(closed.wait(), timeout=5)
                await asyncio.wait_for(active.task, timeout=5)

                assert len(requests) == 1, "成功工具调用后不能再生成重复告别"
                assert requests[0]["messages"][-1] == {"role": "user", "content": evidence}
                assert requests[0]["tools"][0]["function"]["name"] == "hang_up"
                assert bundles[0].tts.requests == [opening, goodbye]
                # ToneTTS 告别输出 800 ms，须解码至少 40 个非静音的 20 ms Opus 帧。
                goodbye_samples = non_silent_samples[opening_audio_frames:]
                assert len(goodbye_samples) >= 40
                assert set(goodbye_samples) == {960}
                assert set(peer.decoded_sample_rates) == {48000}
                assert peer.channel.readyState == "closed"
                assert active.connection.pc.connectionState == "closed"
                assert active.handler._pcs_map == {}
                assert bundles[0].closed
                assert bundles[0].llm._provider_http_client.is_closed
                assert bundles[0].tts._http_client.is_closed
                assert (await client.get("/api/calls/active")).json() is None

                record = (await client.get(f"/api/calls/{call_id}")).json()
                assert record["status"] == "ended"
                assert record["end_reason"] == "ai_hangup"
                assert [
                    (entry["role"], entry["text"], entry["interrupted"])
                    for entry in record["transcript"]
                ] == [
                    ("assistant", opening, False),
                    ("user", evidence, False),
                    ("assistant", goodbye, False),
                ]
                # 客户端断连后 REST 收尾不能把已完成 AI 挂断改写为 user_hangup。
                ended = await client.post(f"/api/calls/{call_id}/end")
                assert ended.json()["end_reason"] == "ai_hangup"

                restarted = await client.post("/api/calls")
                assert restarted.status_code == 201
                next_call_id = restarted.json()["call"]["id"]
                next_peer = client_peer()
                peers.append(next_peer)
                await negotiate(client, next_call_id, next_peer)
                next_peer.ready("ready-immediate-restart")
                assert (await wait_transcript(next_peer))["text"] == opening
                await asyncio.wait_for(next_peer.audio_received.wait(), timeout=5)
                assert len(requests) == 1
                assert bundles[1].tts.requests == [opening]
                await client.post(f"/api/calls/{next_call_id}/end")
                assert bundles[1].closed
            finally:
                for peer in peers:
                    await peer.close()
