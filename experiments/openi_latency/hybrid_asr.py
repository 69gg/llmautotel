"""独立 FunASR 进程，模型只在 OpenI 加载，协议不保存录音。"""

from __future__ import annotations

import base64
import importlib.metadata
import json
import sys
from pathlib import Path
from typing import Any

from worker import ASRStream


def emit_asr(event: dict[str, Any]) -> None:
    print("ASR_EVENT:" + json.dumps(event, ensure_ascii=False), flush=True)


def main() -> None:
    import numpy as np
    import torch
    from funasr import AutoModel

    root = Path(__file__).resolve().parent
    config = json.loads((root / "settings.run.json").read_text())
    paths = json.loads((root / "model-paths.run.json").read_text())
    torch.set_num_threads(4)
    model = AutoModel(
        model=paths["asr"], device=config["asr_device"], disable_update=True, disable_pbar=True
    )
    asr = ASRStream(
        model,
        config["asr_chunk"],
        np,
        streaming=config["asr_streaming"],
        generate_options=config["asr_generate_options"],
    )
    emit_asr(
        {
            "ready": True,
            "versions": {p: importlib.metadata.version(p) for p in ["funasr", "transformers"]},
        }
    )
    for line in sys.stdin:
        command = json.loads(line)
        try:
            if command["type"] == "reset":
                asr.reset()
                text = ""
            else:
                text = asr.feed(
                    base64.b64decode(command["pcm"], validate=True), final=command["final"]
                )
            emit_asr({"text": text, "compute_seconds": asr.compute_seconds})
        except Exception as exc:
            emit_asr({"error": type(exc).__name__})


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        emit_asr({"error": type(exc).__name__})
        raise
