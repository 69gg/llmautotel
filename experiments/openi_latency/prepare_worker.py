"""隔离模型依赖，避免准备过程中替换已启动网关使用的模块。"""

from __future__ import annotations

import json
import os
import subprocess
import venv
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parent
    config = json.loads((root / "settings.run.json").read_text())
    environment = root / ".worker-env"
    venv.EnvBuilder(with_pip=True, system_site_packages=True).create(environment)
    python = environment / "bin/python"
    subprocess.run(
        [str(python), "-m", "pip", "install", "--index-url", config["pip_index_url"], "uv==0.13.8"],
        check=True,
        timeout=120,
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
            "--build-constraints",
            str(root / "requirements-build.lock"),
            "-r",
            str(root / "requirements-worker.lock"),
        ],
        check=True,
        timeout=1500,
    )
    source = root / "CosyVoice"
    subprocess.run(
        ["git", "clone", config["cosyvoice_source"], str(source)], check=True, timeout=180
    )
    subprocess.run(
        ["git", "checkout", config["cosyvoice_revision"]], cwd=source, check=True, timeout=30
    )
    subprocess.run(
        ["git", "submodule", "update", "--init", "--recursive"], cwd=source, check=True, timeout=180
    )
    os.environ.setdefault("TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD", "1")
    os.environ.setdefault("OMP_NUM_THREADS", "4")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    os.execv(str(python), [str(python), "-u", str(root / "worker.py")])


if __name__ == "__main__":
    main()
