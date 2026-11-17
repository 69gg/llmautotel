"""无交互 GPU 对照任务；标准输出及结果目录双写，完成即退出释放 GPU。"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from typing import Any


def prepare_cached_environment(
    root: Path, environment: Path, lock_name: str, *, installer: Path | None = None
) -> None:
    """镜像只缓存权重和部分依赖；重放完整锁，不能把目录存在当作准备完成。"""
    if __package__:
        from .prepare_worker import image_requirements
    else:
        from prepare_worker import image_requirements

    requirements = root / (lock_name + ".image.run.lock")
    requirements.write_text(image_requirements((root / lock_name).read_text()))
    subprocess.run(
        [
            str(installer or environment / "bin/uv"),
            "pip",
            "install",
            "--python",
            str(environment / "bin/python"),
            "--index-url",
            "https://mirrors.aliyun.com/pypi/simple",
            "--no-deps",
            "-r",
            str(requirements),
        ],
        check=True,
        timeout=7200,
    )


def run_candidate(
    root: Path, name: str, engine: str, python: Path, config: dict[str, Any]
) -> dict[str, Any]:
    path = root / f"{name}.run.json"
    path.write_text(json.dumps(config, ensure_ascii=False))
    print("TTS_PHASE:" + name, flush=True)
    env = dict(
        os.environ,
        PYTHONPATH=str(root / "CosyVoice") + ":" + str(root / "CosyVoice/third_party/Matcha-TTS"),
        OMP_NUM_THREADS="4",
        TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD="1",
        TOKENIZERS_PARALLELISM="false",
    )
    result = subprocess.run(
        [
            str(python),
            "-u",
            str(root / "tts_candidates.py"),
            "--engine",
            engine,
            "--config",
            str(path),
        ],
        cwd=root,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=3600,
    )
    (Path("/tmp/output") / f"{name}.log").write_text(result.stdout)
    for line in result.stdout.splitlines():
        if line.startswith("TTS_REPORT:"):
            report: dict[str, Any] = json.loads(line.removeprefix("TTS_REPORT:"))
            print(
                "TTS_CANDIDATE:" + json.dumps({"name": name, "report": report}, ensure_ascii=False),
                flush=True,
            )
            return report
    print(result.stdout[-12000:], flush=True)
    return {"error": "candidate_failed", "returncode": result.returncode}


def install(root: Path, environment: Path, packages: list[str]) -> None:
    installer = Path("/tmp/llmautotel-qwen3-worker/bin/uv")
    if not environment.exists():
        subprocess.run(
            ["/tmp/llmautotel-latency-worker/bin/python", "-m", "venv", str(environment)],
            check=True,
            timeout=60,
        )
    # A copied --system-site-packages venv inherits CUDA extensions built for the
    # image's Torch. vLLM replaces Torch, so its environment must be isolated.
    configuration = (environment / "pyvenv.cfg").read_text().lower()
    if "include-system-site-packages = false" not in configuration:
        raise ValueError("candidate_environment_inherits_system_packages")
    prepare_cached_environment(
        root, environment, "requirements-worker.lock", installer=installer
    )
    subprocess.run(
        [
            str(installer),
            "pip",
            "install",
            "--python",
            str(environment / "bin/python"),
            "--index-url",
            "https://mirrors.aliyun.com/pypi/simple",
            *packages,
        ],
        check=True,
        timeout=7200,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--vllm-only", action="store_true", help="补测隔离环境中的 vLLM，并重跑配对 Qwen 基线"
    )
    arguments = parser.parse_args()
    root = Path(__file__).resolve().parent
    os.chdir(root)
    output = Path("/tmp/output")
    output.mkdir(exist_ok=True)
    subprocess.run(["tar", "-xzf", "cosyvoice-source.tar.gz"], check=True)
    from tts_candidates import choose_fastest

    prepare_cached_environment(
        root, Path("/tmp/llmautotel-qwen3-worker"), "requirements-qwen3-tts.lock"
    )
    prepare_cached_environment(
        root, Path("/tmp/llmautotel-latency-worker"), "requirements-worker.lock"
    )

    base: dict[str, Any] = {
        "reference_audio": str(root / "CosyVoice/asset/zero_shot_prompt.wav"),
        "reference_text": (
            "You are a helpful assistant.<|endofprompt|>希望你以后能够做的比我还好呦。"
        ),
        "warmup_texts": ["它可以整理文档。"],
        "texts": [
            "它可以帮您整理文档、总结会议、辅助写作。",
            "订阅每月99元。",
            "支持先试用再订阅。",
        ],
        "repeats": 2,
        "cpu_threads": 4,
    }
    reports: dict[str, dict[str, Any]] = {}

    def candidate(name: str, engine: str, environment: str, updates: dict[str, Any]) -> None:
        try:
            reports[name] = run_candidate(
                root, name, engine, Path(environment) / "bin/python", base | updates
            )
        except Exception as exc:
            reports[name] = {"error": type(exc).__name__, "detail": str(exc)[:500]}
            print("TTS_FAILED:" + json.dumps({"name": name, **reports[name]}), flush=True)
        (output / "tts-comparison.json").write_text(
            json.dumps(reports, ensure_ascii=False, indent=2)
        )

    candidate(
        "qwen3_graph4",
        "qwen3",
        "/tmp/llmautotel-qwen3-worker",
        {"model_path": "/tmp/llmautotel-qwen3-model", "chunk_size": 4},
    )
    cosy = {
        "model_path": "/tmp/pretrainmodel/Fun-CosyVoice3-0.5B-2512",
        "fp16": True,
        "load_vllm": False,
        "poll_interval_seconds": 0.005,
    }
    if not arguments.vllm_only:
        candidate("cosyvoice3_native_5ms", "cosyvoice3", "/tmp/llmautotel-latency-worker", cosy)
    try:
        environment = Path("/tmp/llmautotel-cosy-vllm")
        install(
            root,
            environment,
            ["vllm==0.9.0", "torchaudio==2.7.0", "transformers==4.51.3", "numpy==1.26.4"],
        )
        source, writable = Path(cosy["model_path"]), Path("/tmp/cosyvoice3-vllm-model")
        writable.mkdir(exist_ok=True)
        for item in source.iterdir():
            target = writable / item.name
            if not target.exists():
                target.symlink_to(item)
        candidate(
            "cosyvoice3_vllm_5ms",
            "cosyvoice3",
            str(environment),
            cosy | {"model_path": str(writable), "load_vllm": True},
        )
    except Exception as exc:
        reports["cosyvoice3_vllm_5ms"] = {"error": type(exc).__name__, "detail": str(exc)[:500]}
    try:
        winner = choose_fastest(reports, baseline="qwen3_graph4")
    except ValueError:
        winner = "none"
    if not arguments.vllm_only and not winner.startswith("cosyvoice3"):
        try:
            environment = Path("/tmp/llmautotel-voxcpm15")
            install(
                root,
                environment,
                ["voxcpm==1.5.0", "torch==2.6.0", "torchaudio==2.6.0", "transformers==4.51.3"],
            )
            # Download only on the GPU; a long deadline and domestic mirror avoid repeated restarts.
            downloader = (
                "from modelscope import snapshot_download; "
                "snapshot_download('OpenBMB/VoxCPM1.5', "
                "local_dir='/tmp/llmautotel-voxcpm15-model')"
            )
            subprocess.run(
                [str(environment / "bin/python"), "-u", "-c", downloader],
                check=True,
                timeout=7200,
                env=dict(
                    os.environ,
                    HF_HUB_DOWNLOAD_TIMEOUT="600",
                    HF_HUB_ETAG_TIMEOUT="120",
                    HF_HUB_DISABLE_XET="1",
                ),
            )
            candidate(
                "voxcpm15_compiled",
                "voxcpm15",
                str(environment),
                {
                    "model_path": "/tmp/llmautotel-voxcpm15-model",
                    "reference_text": "希望你以后能够做的比我还好呦。",
                    "optimize": True,
                    "inference_timesteps": 10,
                },
            )
        except Exception as exc:
            reports["voxcpm15_compiled"] = {"error": type(exc).__name__, "detail": str(exc)[:500]}
    try:
        winner = choose_fastest(reports, baseline="qwen3_graph4")
    except ValueError:
        winner = "none"
    result = {
        "winner": winner,
        "reports": reports,
        "scope": "same_A100_reference_texts_24k_pcm_not_browser",
    }
    (output / "tts-comparison.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print("TTS_FINAL:" + json.dumps(result, ensure_ascii=False), flush=True)
    try:
        from c2net.context import upload_output

        upload_output()
    except Exception as exc:
        print("TTS_OUTPUT_UPLOAD:" + type(exc).__name__, flush=True)
    if winner == "none":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
