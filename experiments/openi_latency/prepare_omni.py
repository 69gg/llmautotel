"""为 Omni 实验复用已验证的镜像，只补齐锁定的源码和语音资产。"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
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


def download_asset(
    root: Path,
    item: dict[str, Any],
    sources: list[dict[str, str]],
    deadline: float,
    *,
    request_timeout: float = 120,
    chunk_bytes: int = 4 * 1024 * 1024,
    retry_delay: float = 2,
    progress_interval: float = 15,
) -> None:
    """从国内镜像分块续传；网络失败和到期保留已下载内容。"""
    target = root / item["path"]
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError("asset_outside_model")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file():
        if target.stat().st_size != item["bytes"]:
            raise ValueError("asset_size_mismatch")
        verify_file(target, item["sha256"])
        return
    if not sources or chunk_bytes <= 0 or request_timeout <= 0:
        raise ValueError("invalid_asset_download_settings")
    temporary = target.with_suffix(target.suffix + ".partial")
    expected = int(item["bytes"])
    last_progress = 0.0
    attempt = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("asset_prepare_deadline")
        offset = temporary.stat().st_size if temporary.exists() else 0
        if offset == expected:
            try:
                verify_file(temporary, item["sha256"])
            except ValueError:
                temporary.unlink()
                print(f"omni_asset_corrupt_partial_reset {item['path']}", flush=True)
                attempt += 1
                continue
            temporary.replace(target)
            print(f"omni_asset_verified {item['path']} {expected}", flush=True)
            return
        if offset > expected:
            raise ValueError("asset_exceeded_expected_size")
        source = sources[attempt % len(sources)]
        url = source["url_template"].format(
            path=urllib.parse.quote(item["path"], safe="/"),
            revision=urllib.parse.quote(source["revision"], safe=""),
        )
        last_byte = min(offset + chunk_bytes, expected) - 1
        request = urllib.request.Request(
            url,
            headers={"Range": f"bytes={offset}-{last_byte}", "Accept-Encoding": "identity"},
        )
        try:
            with urllib.request.urlopen(
                request, timeout=min(request_timeout, remaining)
            ) as response:
                if response.status == 206:
                    content_range = response.headers.get("Content-Range", "")
                    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", content_range)
                    if not match or tuple(map(int, match.groups())) != (
                        offset,
                        last_byte,
                        expected,
                    ):
                        raise ValueError("asset_invalid_content_range")
                    response_bytes = last_byte - offset + 1
                    mode = "ab"
                elif response.status == 200:
                    # A mirror may ignore Range: never append its full response to a partial.
                    offset, response_bytes, mode = 0, expected, "wb"
                else:
                    raise ValueError("asset_unexpected_http_status")
                with temporary.open(mode) as output:
                    received = 0
                    while received < response_bytes:
                        if time.monotonic() >= deadline:
                            raise TimeoutError("asset_prepare_deadline")
                        chunk = response.read1(min(1024 * 1024, response_bytes - received))
                        if not chunk:
                            raise ConnectionError("asset_response_incomplete")
                        output.write(chunk)
                        received += len(chunk)
                        now = time.monotonic()
                        if now - last_progress >= progress_interval:
                            output.flush()
                            print(
                                f"omni_asset_progress {item['path']} "
                                f"{output.tell()}/{expected} "
                                f"{output.tell() / expected:.1%} "
                                f"source={urllib.parse.urlsplit(url).hostname}",
                                flush=True,
                            )
                            last_progress = now
                    if response.read(1):
                        raise ValueError("asset_response_exceeded_expected_size")
        except (OSError, http.client.HTTPException, ValueError) as exc:
            if time.monotonic() >= deadline:
                raise TimeoutError("asset_prepare_deadline") from exc
            # Do not print signed redirect URLs or response bodies.
            print(
                f"omni_asset_retry {item['path']} offset="
                f"{temporary.stat().st_size if temporary.exists() else 0} "
                f"error={type(exc).__name__} source={urllib.parse.urlsplit(url).hostname}",
                flush=True,
            )
            attempt += 1
            time.sleep(min(retry_delay, max(0, deadline - time.monotonic())))


def prepare(root: Path, config: dict[str, Any]) -> None:
    prepare_deadline = time.monotonic() + config["prepare_timeout_seconds"]

    def remaining(limit: float) -> float:
        value = min(limit, prepare_deadline - time.monotonic())
        if value <= 0:
            raise TimeoutError("omni_prepare_deadline")
        return value

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
        timeout=remaining(config["model_prepare_timeout_seconds"]),
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
    deadline = time.monotonic() + remaining(config["asset_prepare_timeout_seconds"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                download_asset,
                model,
                item,
                config["asset_sources"],
                deadline,
                request_timeout=config["asset_request_timeout_seconds"],
                chunk_bytes=config["asset_download_chunk_bytes"],
                progress_interval=config["asset_progress_interval_seconds"],
            )
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
        timeout=remaining(60),
    )
    python = environment / "bin/python"
    requirements = root / "requirements-omni-image.run.lock"
    requirements.write_text(image_requirements((root / "requirements-omni.lock").read_text()))
    subprocess.run(
        [str(python), "-m", "pip", "install", "--index-url", config["pip_index_url"], "uv==0.13.8"],
        check=True,
        timeout=remaining(config["dependency_prepare_timeout_seconds"]),
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
        timeout=remaining(config["dependency_prepare_timeout_seconds"]),
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
