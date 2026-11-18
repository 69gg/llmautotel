"""生产启动也必须使用已验证的镜像下载策略；测试不下载依赖或加载模型。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from experiments.openi_latency import prepare_hybrid, prepare_qwen3
from experiments.openi_latency.prepare_worker import download_environment


def test_download_policy_preserves_explicit_caller_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in ("UV_HTTP_TIMEOUT", "UV_HTTP_RETRIES", "PIP_DEFAULT_TIMEOUT", "PIP_RETRIES"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("UV_CONCURRENT_DOWNLOADS", "2")
    monkeypatch.setenv("UV_CACHE_DIR", "/cached-image/uv")
    policy = download_environment()
    assert policy["UV_HTTP_TIMEOUT"] == policy["PIP_DEFAULT_TIMEOUT"] == "600"
    assert policy["UV_HTTP_RETRIES"] == policy["PIP_RETRIES"] == "5"
    assert policy["UV_CONCURRENT_DOWNLOADS"] == "2"
    assert policy["UV_CACHE_DIR"] == "/cached-image/uv"
    assert "UV_HTTP_TIMEOUT" not in os.environ


def test_production_asr_and_tts_installers_use_same_download_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in (
        "requirements-asr.lock", "requirements-qwen3-tts.lock", "requirements-realtime.lock"
    ):
        (tmp_path / name).write_text("numpy==1.26.4\ntorch==2.6.0\n")
    settings: dict[str, Any] = {
        "asr_worker_environment": str(tmp_path / "asr"),
        "worker_environment": str(tmp_path / "tts"),
        "pip_index_url": "https://example.invalid/simple",
        "dependency_prepare_timeout_seconds": 7200,
        "model_prepare_timeout_seconds": 600,
        "qwen3_model_directory": str(tmp_path / "weights"),
        "asset_prepare_timeout_seconds": 600,
        "qwen3_assets": [],
        "serve_models": True,
        "serve_realtime": True,
    }
    calls: list[tuple[list[str], dict[str, Any]]] = []
    launches: list[list[str]] = []
    monkeypatch.setenv("UV_HTTP_TIMEOUT", "456")
    monkeypatch.setenv("PIP_DEFAULT_TIMEOUT", "123")
    for name in (
        "HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "OMP_NUM_THREADS",
        "TOKENIZERS_PARALLELISM", "PYTHONPATH",
    ):
        monkeypatch.setenv(name, os.environ.get(name, ""))
    monkeypatch.setattr(prepare_hybrid, "image_python", lambda config: "/image/python")
    monkeypatch.setattr(prepare_qwen3, "image_python", lambda config: "/image/python")
    monkeypatch.setattr(
        prepare_hybrid.subprocess, "run", lambda args, **kwargs: calls.append((args, kwargs))
    )
    monkeypatch.setattr(prepare_qwen3.os, "execv", lambda path, args: launches.append(args))
    prepare_hybrid.prepare(tmp_path, settings)
    installs = [(args, kw) for args, kw in calls if "install" in args]
    assert len(installs) == 5  # ASR pip/uv、TTS pip/uv 和完整业务依赖。
    for args, options in installs:
        assert options["check"] and options["timeout"] == 7200
        assert options["env"]["UV_HTTP_TIMEOUT"] == "456"
        assert options["env"]["PIP_DEFAULT_TIMEOUT"] == "123"
        assert settings["pip_index_url"] in args
    for args, _ in installs:
        if "-r" in args:
            assert "--no-deps" in args
    assert len(launches) == 1 and launches[0][-1] == str(tmp_path / "hybrid_api.py")
    assert "torch==" not in (tmp_path / "requirements-qwen3-image.run.lock").read_text()
