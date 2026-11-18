"""隔离模型依赖，避免准备过程中替换已启动网关使用的模块。"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path
from typing import Any


def download_environment() -> dict[str, str]:
    """生产与对照任务共用下载策略，调用方设置始终优先。"""
    defaults = {
        "UV_HTTP_TIMEOUT": "600",
        "UV_HTTP_RETRIES": "5",
        "UV_CONCURRENT_DOWNLOADS": "4",
        "PIP_DEFAULT_TIMEOUT": "600",
        "PIP_RETRIES": "5",
    }
    return defaults | dict(os.environ)


def image_python(config: dict[str, Any]) -> str:
    """验证镜像自带的 GPU 环境，避免误用平台启动器的 Python。"""
    probe = (
        "import json, sys, torch, torchaudio, triton; "
        "assert torch.cuda.is_available(); "
        "torch.ones(1, device='cuda').sum().item(); "
        "print(json.dumps({'python':sys.executable,'version':sys.version.split()[0],"
        "'torch':torch.__version__,'torchaudio':torchaudio.__version__,"
        "'triton':triton.__version__,'cuda':torch.version.cuda}))"
    )
    candidates: list[str] = []
    for pattern in config["python_candidates"]:
        path = shutil.which(pattern)
        candidates.extend([path] if path else sorted(glob.glob(pattern)))
    for candidate in dict.fromkeys(candidates):
        try:
            result = subprocess.run(
                [candidate, "-c", probe], capture_output=True, text=True, timeout=30
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"runtime_probe {candidate}: {type(exc).__name__}", flush=True)
            continue
        if result.returncode == 0:
            info = json.loads(result.stdout.strip().splitlines()[-1])
            print("image_runtime " + json.dumps(info), flush=True)
            if all(
                info[name].split("+")[0] == config["torch_version"]
                for name in ("torch", "torchaudio")
            ):
                return candidate
        else:
            print(f"runtime_probe {candidate}: {result.stderr[-500:]}", flush=True)
    raise RuntimeError("no_verified_image_torch_environment")


def image_requirements(lock: str) -> str:
    """Torch/CUDA 来自已验证镜像，其余依赖仍按完整锁定版本安装。"""
    requirements: list[str] = []
    for line in lock.splitlines():
        name = line.split("==", 1)[0].strip().lower()
        if name in {"torch", "torchaudio", "triton"} or name.startswith("nvidia-"):
            continue
        requirements.append(line)
    return "\n".join(requirements) + "\n"


def prepare_source(root: Path, config: dict[str, Any]) -> None:
    """可先在本机打包已锁定的源码，避免 GPU 空等 GitHub 克隆。"""
    source = root / "CosyVoice"
    if archive_name := config.get("source_archive"):
        archive = root / archive_name
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        if digest != config.get("source_archive_sha256"):
            raise ValueError("source_archive_checksum_mismatch")
        with tarfile.open(archive) as bundle:
            bundle.extractall(root, filter="data")
        if not (source / "third_party/Matcha-TTS/matcha").is_dir():
            raise ValueError("source_archive_missing_matcha")
        print(f"source_archive_verified {digest}", flush=True)
        return
    subprocess.run(
        ["git", "clone", config["cosyvoice_source"], str(source)], check=True, timeout=180
    )
    subprocess.run(
        ["git", "checkout", config["cosyvoice_revision"]], cwd=source, check=True, timeout=30
    )
    subprocess.run(
        ["git", "submodule", "update", "--init", "--recursive", "--depth", "1"],
        cwd=source,
        check=True,
        timeout=180,
    )


def main() -> None:
    root = Path(__file__).resolve().parent
    config = json.loads((root / "settings.run.json").read_text())
    if config.get("model_backend") == "hybrid_qwen3":
        from prepare_hybrid import prepare

        prepare(root, config)
        return
    if config.get("model_backend") == "qwen3_tts":
        from prepare_qwen3 import prepare

        prepare(root, config)
        return
    if config.get("model_backend") == "minicpm_o45":
        from prepare_omni import prepare

        prepare(root, config)
        return
    environment = Path(config.get("worker_environment", str(root / ".worker-env")))
    prepare_source(root, config)
    selected = image_python(config)
    if config.get("mounted_models") or config.get("cached_models"):
        print('OPENI_EVENT:{"type":"status","phase":"preparing_local_models"}', flush=True)
        subprocess.run(
            [selected, "-u", str(root / "model_paths.py")],
            check=True,
            timeout=config.get("model_prepare_timeout_seconds", 600),
        )
    subprocess.run(
        [selected, "-m", "venv", "--system-site-packages", str(environment)],
        check=True,
        timeout=60,
    )
    python = environment / "bin/python"
    requirements = root / "requirements-image.run.lock"
    requirements.write_text(image_requirements((root / "requirements-worker.lock").read_text()))
    subprocess.run(
        [str(python), "-m", "pip", "install", "--index-url", config["pip_index_url"], "uv==0.13.8"],
        check=True,
        timeout=config.get("dependency_prepare_timeout_seconds", 1500),
        env=download_environment(),
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
            "--no-deps",
            "-r",
            str(requirements),
        ],
        check=True,
        timeout=config.get("dependency_prepare_timeout_seconds", 1500),
        env=download_environment(),
    )
    os.environ.setdefault("TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD", "1")
    os.environ.setdefault("OMP_NUM_THREADS", "4")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    if not config.get("allow_model_downloads", True):
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.execv(str(python), [str(python), "-u", str(root / "worker.py")])


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(
            "OPENI_EVENT:"
            + json.dumps(
                {"type": "error", "phase": "model_initialization_failed", "detail": str(exc)[:400]}
            ),
            flush=True,
        )
        raise
