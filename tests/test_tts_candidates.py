"""候选模型公平比较、持续供音和重采样边界；不加载模型。"""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from experiments.openi_latency.tts_candidates import FloatPCM, choose_fastest


def report(latency: float, *, starvation: float = 0, rtf: float = 0.2) -> dict[str, Any]:
    return {
        "samples": [
            {
                "repeat": 0,
                "case": 0,
                "text": "测试。",
                "ok": True,
                "effective_audio_ms": latency,
                "rtf": rtf,
                "starvation_ms": starvation,
            }
        ]
    }


def test_selection_rejects_fast_first_chunk_with_broken_continuous_audio() -> None:
    assert choose_fastest({"steady": report(300), "gaps": report(80, starvation=200)}) == "steady"
    assert (
        choose_fastest({"steady": report(300), "slower_than_playback": report(80, rtf=1.1)})
        == "steady"
    )
    with pytest.raises(ValueError, match="no_continuous"):
        choose_fastest({"broken": report(80, starvation=200)})


def test_selection_requires_same_text_and_repeats_and_all_successful_samples() -> None:
    mismatch = report(80)
    mismatch["samples"][0]["text"] = "其他文本。"
    with pytest.raises(ValueError, match="unpaired"):
        choose_fastest({"baseline": report(300), "other": mismatch})
    failed = report(80)
    failed["samples"][0]["ok"] = False
    assert choose_fastest({"baseline": report(300), "failed": failed}) == "baseline"


def test_failed_baseline_cannot_promote_an_uncompared_model() -> None:
    with pytest.raises(ValueError, match="unverified_tts_baseline"):
        choose_fastest(
            {"qwen": {"error": "ModuleNotFoundError"}, "vox": report(80)}, baseline="qwen"
        )


def test_cached_environment_replays_non_cuda_locked_dependencies(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from experiments.openi_latency.tts_autotest import prepare_cached_environment

    (tmp_path / "test.lock").write_text("torch==2.6.0\nfaster-qwen3-tts==0.5.4\nwget==3.2\n")
    commands: list[list[str]] = []
    monkeypatch.setattr(
        "experiments.openi_latency.tts_autotest.subprocess.run",
        lambda args, **kwargs: commands.append(args),
    )
    prepare_cached_environment(tmp_path, tmp_path / "venv", "test.lock")
    actual = (tmp_path / "test.lock.image.run.lock").read_text()
    assert "torch==" not in actual
    assert "wget==3.2" in actual and "faster-qwen3-tts==0.5.4" in actual
    assert "--no-deps" in commands[0] and "-r" in commands[0]


def test_resampling_keeps_duration_and_frequency_across_arbitrary_chunk_boundaries() -> None:
    source_rate = 44100
    wave = np.sin(np.arange(source_rate, dtype=np.float32) * (2 * np.pi * 440 / source_rate)) * 0.2
    converter = FloatPCM(source_rate)
    blocks = [converter.feed(wave[index : index + 703]) for index in range(0, len(wave), 703)]
    blocks.append(converter.feed([], final=True))
    samples = np.frombuffer(b"".join(blocks), dtype="<i2")
    assert len(samples) == 24000
    peak = np.argmax(np.abs(np.fft.rfft(samples)))
    assert peak == 440


def test_candidate_environment_rejects_inherited_cuda_extensions(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from experiments.openi_latency.tts_autotest import install

    environment = tmp_path / "candidate"
    environment.mkdir()
    (environment / "pyvenv.cfg").write_text("include-system-site-packages = true\n")
    commands: list[list[str]] = []
    monkeypatch.setattr(
        "experiments.openi_latency.tts_autotest.subprocess.run",
        lambda args, **kwargs: commands.append(args),
    )
    with pytest.raises(ValueError, match="inherits_system_packages"):
        install(tmp_path, environment)
    assert not commands


def test_dependency_downloads_use_long_timeout_and_preserve_overrides(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from experiments.openi_latency.tts_autotest import prepare_cached_environment

    (tmp_path / "test.lock").write_text("wget==3.2\n")
    environments: list[dict[str, str]] = []

    def capture(args: list[str], **kwargs: Any) -> None:
        environments.append(kwargs["env"])

    monkeypatch.delenv("UV_HTTP_TIMEOUT", raising=False)
    monkeypatch.delenv("UV_HTTP_RETRIES", raising=False)
    monkeypatch.setenv("UV_CONCURRENT_DOWNLOADS", "2")
    monkeypatch.setenv("TTS_DOWNLOAD_TEST", "preserved")
    monkeypatch.setattr("experiments.openi_latency.tts_autotest.subprocess.run", capture)
    prepare_cached_environment(tmp_path, tmp_path / "venv", "test.lock")
    assert environments[0]["UV_HTTP_TIMEOUT"] == "600"
    assert environments[0]["UV_HTTP_RETRIES"] == "5"
    assert environments[0]["UV_CONCURRENT_DOWNLOADS"] == "2"
    assert environments[0]["TTS_DOWNLOAD_TEST"] == "preserved"


def test_pcm_passthrough_clips_without_changing_sample_count() -> None:
    assert np.frombuffer(FloatPCM(24000).feed([-2, 0, 2]), dtype="<i2").tolist() == [
        -32767,
        0,
        32767,
    ]


def test_vox_replays_only_non_cuda_dependencies_between_runtime_checks(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from experiments.openi_latency.tts_autotest import prepare_voxcpm

    (tmp_path / "settings.run.json").write_text('{"torch_version":"2.6.0"}')
    (tmp_path / "requirements-voxcpm.lock").write_text(
        "torch==2.6.0\ntorchaudio==2.6.0\nnvidia-cublas-cu12==12.4.5.8\nvoxcpm==1.5.0\n"
    )
    commands: list[list[str]] = []
    monkeypatch.setattr(
        "experiments.openi_latency.tts_autotest.subprocess.run",
        lambda args, **kwargs: commands.append(args),
    )
    prepare_voxcpm(tmp_path, tmp_path / "cached")
    assert len(commands) == 3 and commands[0] == commands[2]
    assert commands[0][-1] == "2.6.0"
    assert "torch.cuda.is_available()" in commands[0][2]
    assert "--no-deps" in commands[1]
    lock = (tmp_path / "requirements-voxcpm.lock.image.run.lock").read_text()
    assert lock == "voxcpm==1.5.0\n"


def test_vox_refuses_to_install_when_cached_cuda_runtime_fails(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    from experiments.openi_latency.tts_autotest import prepare_voxcpm

    (tmp_path / "settings.run.json").write_text('{"torch_version":"2.6.0"}')
    calls: list[list[str]] = []

    def fail(args: list[str], **kwargs: Any) -> None:
        calls.append(args)
        raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr("experiments.openi_latency.tts_autotest.subprocess.run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        prepare_voxcpm(tmp_path, tmp_path / "cached")
    assert len(calls) == 1


def test_candidate_retry_modes_are_exclusive_before_any_gpu_work() -> None:
    from experiments.openi_latency.tts_autotest import main

    with pytest.raises(SystemExit) as error:
        main(["--vox-only", "--vllm-only"])
    assert error.value.code == 2


def test_vllm_replays_full_lock_with_cuda_in_an_isolated_environment(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from experiments.openi_latency.tts_autotest import install

    environment = tmp_path / "isolated"
    environment.mkdir()
    (environment / "pyvenv.cfg").write_text("include-system-site-packages = false\n")
    lock = tmp_path / "requirements-cosy-vllm.lock"
    lock.write_text("torch==2.7.0\nvllm==0.9.0\n")
    commands: list[list[str]] = []
    monkeypatch.setattr(
        "experiments.openi_latency.tts_autotest.subprocess.run",
        lambda args, **kwargs: commands.append(args),
    )
    install(tmp_path, environment)
    assert len(commands) == 1
    assert commands[0][-3:] == ["--no-deps", "-r", str(lock)]
    assert "--python" in commands[0] and str(environment / "bin/python") in commands[0]
    assert lock.read_text() == "torch==2.7.0\nvllm==0.9.0\n"
