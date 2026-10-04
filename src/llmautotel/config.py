"""进程配置与本地数据路径。"""

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RuntimeConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    data_dir: Path = Path("data")
    connection_timeout_seconds: float = 30
    frontend_dir: Path = Path(__file__).resolve().parents[2] / "frontend" / "dist"

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
        )
