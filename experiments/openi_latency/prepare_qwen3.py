"""仅在 OpenI 中准备隔离的 Qwen3-TTS 环境和镜像权重。"""

from __future__ import annotations

import json
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

if __package__:
    from .prepare_omni import download_asset
    from .prepare_worker import image_python, image_requirements
else:
    from prepare_omni import download_asset
    from prepare_worker import image_python, image_requirements


def prepare(root: Path, config: dict[str, Any]) -> None:
    selected = image_python(config)
    print('OPENI_EVENT:{"type":"status","phase":"preparing_qwen3_weights"}', flush=True)
    model = Path(config["qwen3_model_directory"])
    deadline = time.monotonic() + config["asset_prepare_timeout_seconds"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [
            pool.submit(
                download_asset,
                model,
                asset,
                config["asset_sources"],
                deadline,
                request_timeout=config["asset_request_timeout_seconds"],
                chunk_bytes=config["asset_download_chunk_bytes"],
                progress_interval=15,
            )
            for asset in config["qwen3_assets"]
        ]
        for job in jobs:
            job.result()
    environment = Path(config["worker_environment"])
    subprocess.run([selected, "-m", "venv", "--system-site-packages", str(environment)], check=True)
    python = environment / "bin/python"
    requirements = root / "requirements-qwen3-image.run.lock"
    requirements.write_text(image_requirements((root / "requirements-qwen3-tts.lock").read_text()))
    print('OPENI_EVENT:{"type":"status","phase":"installing_qwen3_dependencies"}', flush=True)
    subprocess.run(
        [str(python), "-m", "pip", "install", "--index-url", config["pip_index_url"], "uv==0.13.8"],
        check=True,
        timeout=config["dependency_prepare_timeout_seconds"],
    )
    subprocess.run(
        [
            str(environment / "bin/uv"),
            "pip",
            "install",
            "--python",
            str(python),
            "--index-url",
            config["pip_index_url"],
            "--no-deps",
            "-r",
            str(requirements),
        ],
        check=True,
        timeout=config["dependency_prepare_timeout_seconds"],
    )
    os.environ.update(
        HF_HUB_OFFLINE="1",
        TRANSFORMERS_OFFLINE="1",
        OMP_NUM_THREADS="4",
        TOKENIZERS_PARALLELISM="false",
    )
    os.execv(str(python), [str(python), "-u", str(root / "qwen3_worker.py")])


if __name__ == "__main__":
    folder = Path(__file__).resolve().parent
    prepare(folder, json.loads((folder / "settings.run.json").read_text()))
