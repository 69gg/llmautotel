"""进程配置与本地数据路径。"""

import os
from dataclasses import dataclass, field
from pathlib import Path

from aiortc import RTCIceServer
from aiortc.rtcicetransport import parse_stun_turn_uri
from pydantic import TypeAdapter


def parse_ice_servers(value: str) -> list[RTCIceServer]:
    """复用运输层的格式；错误不回显含中继凭据的输入。"""
    try:
        servers = TypeAdapter(list[RTCIceServer]).validate_json(value)
        for server in servers:
            urls = [server.urls] if isinstance(server.urls, str) else server.urls
            if not urls or server.credentialType != "password":
                raise ValueError
            for url in urls:
                parsed = parse_stun_turn_uri(url)
                if parsed["scheme"] == "stuns":
                    raise ValueError  # 锁定的 aiortc 不支持 TLS STUN。
                if parsed["scheme"] in {"turn", "turns"}:
                    if not server.username or not server.credential:
                        raise ValueError
                    if parsed["transport"] not in {"udp", "tcp"} or (
                        parsed["scheme"] == "turns" and parsed["transport"] != "tcp"
                    ):
                        raise ValueError
        return servers
    except (ValueError, TypeError):
        raise ValueError("LLMAUTOTEL_ICE_SERVERS 格式无效，请检查 STUN／TURN 配置") from None


@dataclass(frozen=True)
class RuntimeConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    data_dir: Path = Path("data")
    connection_timeout_seconds: float = 30
    frontend_dir: Path = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    ice_servers: list[RTCIceServer] = field(default_factory=list, repr=False)

    @classmethod
    def from_environment(cls) -> "RuntimeConfig":
        return cls(
            host=os.environ.get("LLMAUTOTEL_HOST", "127.0.0.1"),
            port=int(os.environ.get("LLMAUTOTEL_PORT", "8765")),
            data_dir=Path(os.environ.get("LLMAUTOTEL_DATA_DIR", "data")),
            connection_timeout_seconds=float(
                os.environ.get("LLMAUTOTEL_CONNECTION_TIMEOUT_SECONDS", "30")
            ),
            frontend_dir=Path(os.environ.get("LLMAUTOTEL_FRONTEND_DIR", str(cls().frontend_dir))),
            ice_servers=parse_ice_servers(os.environ.get("LLMAUTOTEL_ICE_SERVERS", "[]")),
        )
