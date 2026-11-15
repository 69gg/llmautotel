"""复用 Qwen3 缓存镜像与站内模型，ASR 仅隔离必要依赖。"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from prepare_qwen3 import prepare as prepare_qwen3
from prepare_worker import image_python, image_requirements


def prepare(root: Path, config: dict[str, Any]) -> None:
    selected = image_python(config)
    subprocess.run(
        [selected, "-u", str(root / "model_paths.py")],
        check=True,
        timeout=config["model_prepare_timeout_seconds"],
    )
    environment = Path(config["asr_worker_environment"])
    subprocess.run(
        [selected, "-m", "venv", "--system-site-packages", str(environment)], check=True, timeout=60
    )
    python = environment / "bin/python"
    subprocess.run(
        [str(python), "-m", "pip", "install", "--index-url", config["pip_index_url"], "uv==0.13.8"],
        check=True,
        timeout=config["dependency_prepare_timeout_seconds"],
    )
    lock = root / "requirements-asr-image.run.lock"
    lock.write_text(image_requirements((root / "requirements-asr.lock").read_text()))
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
            str(lock),
        ],
        check=True,
        timeout=config["dependency_prepare_timeout_seconds"],
    )
    prepare_qwen3(root, config)
