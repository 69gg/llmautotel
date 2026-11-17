"""OpenI 启动入口：先启动轻量网关，收到认证指令后才准备模型。"""

from __future__ import annotations

import json
import os
import subprocess
import venv
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parent
    os.chdir(root)
    os.environ.setdefault("PIP_NO_CACHE_DIR", "1")
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    config = json.loads((root / "settings.run.json").read_text())
    if config.get("serve_models", False):
        from reverse_tunnel import start

        start(root, config)
        os.execv(os.sys.executable, [os.sys.executable, "-u", str(root / "prepare_worker.py")])
    environment = root / ".gateway-env"
    python = environment / "bin/python"
    if not python.exists():
        venv.EnvBuilder(with_pip=True).create(environment)
    subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--index-url",
            config["pip_index_url"],
            "-r",
            str(root / "requirements-gateway.lock"),
        ],
        check=True,
        timeout=300,
    )
    os.execv(str(python), [str(python), "-u", str(root / "gateway.py")])


if __name__ == "__main__":
    main()
