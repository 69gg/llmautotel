"""为 Omni 实验复用已验证的镜像，只补齐锁定的源码和语音资产。"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tarfile
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

if __package__:
    from .model_paths import validate_model
    from .prepare_worker import image_python, image_requirements
else:
    from model_paths import validate_model
    from prepare_worker import image_python, image_requirements


def verify_file(path: Path, digest: str) -> None:
    with path.open("rb") as source:
        actual = hashlib.file_digest(source, "sha256").hexdigest()
    if actual != digest:
        raise ValueError(f"asset_checksum_mismatch: {path.name}")


def download_asset(root: Path, item: dict[str, Any], endpoint: str, deadline: float) -> None:
    target = root / item["path"]
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError("asset_outside_model")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file():
        if target.stat().st_size != item["bytes"]:
            raise ValueError("asset_size_mismatch")
        verify_file(target, item["sha256"])
        return
    query = urllib.parse.urlencode({"Revision": item["revision"], "FilePath": item["path"]})
    temporary = target.with_suffix(target.suffix + ".partial")
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("asset_prepare_deadline")
        with urllib.request.urlopen(endpoint + "?" + query, timeout=min(30, remaining)) as response:
            with temporary.open("wb") as output:
                while chunk := response.read(1024 * 1024):
                    if time.monotonic() >= deadline:
                        raise TimeoutError("asset_prepare_deadline")
                    output.write(chunk)
                    if output.tell() > item["bytes"]:
                        raise ValueError("asset_exceeded_expected_size")
        if temporary.stat().st_size != item["bytes"]:
            raise ValueError("asset_size_mismatch")
        verify_file(temporary, item["sha256"])
        temporary.replace(target)
        print(f"omni_asset_verified {item['path']} {item['bytes']}", flush=True)
    finally:
        temporary.unlink(missing_ok=True)


def prepare(root: Path, config: dict[str, Any]) -> None:
    selected = image_python(config)
    print('OPENI_EVENT:{"type":"status","phase":"preparing_omni_mount"}', flush=True)
    probe = (
        "from c2net.context import prepare; import json; "
        "print('OMNI_MOUNT:'+json.dumps(prepare().pretrain_model_path))"
    )
    result = subprocess.run(
        [selected, "-u", "-c", probe],
        check=True,
        capture_output=True,
        text=True,
        timeout=config["model_prepare_timeout_seconds"],
    )
    line = next(x for x in result.stdout.splitlines() if x.startswith("OMNI_MOUNT:"))
    pretrain = Path(json.loads(line.removeprefix("OMNI_MOUNT:")))
    mounted = pretrain / config["omni_mounted_model"] / config["omni_mounted_subdirectory"]
    validate_model(mounted, ["model.safetensors.index.json", "*.safetensors"])
    model = Path(config["omni_model_directory"])
    model.mkdir(parents=True, exist_ok=True)
    # A private view keeps the shared platform model immutable.
    for shard in mounted.glob("*.safetensors"):
        link = model / shard.name
        if not link.exists():
            link.symlink_to(shard.resolve())
    archive = root / config["omni_source_archive"]
    verify_file(archive, config["omni_source_archive_sha256"])
    with tarfile.open(archive) as bundle:
        bundle.extractall(model, filter="data")
    validate_model(model, ["config.json", "model.safetensors.index.json", "*.safetensors"])
    print('OPENI_EVENT:{"type":"status","phase":"preparing_omni_audio_assets"}', flush=True)
    deadline = time.monotonic() + config["asset_prepare_timeout_seconds"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(download_asset, model, item, config["asset_endpoint"], deadline)
            for item in config["omni_assets"]
        ]
        for future in futures:
            future.result()
    (root / "model-paths.run.json").write_text(json.dumps({"omni": str(model)}))
    print('OPENI_EVENT:{"type":"status","phase":"installing_omni_dependencies"}', flush=True)
    environment = Path(config["worker_environment"])
    subprocess.run(
        [selected, "-m", "venv", "--system-site-packages", str(environment)],
        check=True,
        timeout=60,
    )
    python = environment / "bin/python"
    requirements = root / "requirements-omni-image.run.lock"
    requirements.write_text(image_requirements((root / "requirements-omni.lock").read_text()))
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
            "--no-deps",
            "-r",
            str(requirements),
        ],
        check=True,
        timeout=config["dependency_prepare_timeout_seconds"],
    )
    os.environ.update(
        {
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "OMP_NUM_THREADS": "4",
            "TOKENIZERS_PARALLELISM": "false",
        }
    )
    os.execv(str(python), [str(python), "-u", str(root / "worker.py")])
