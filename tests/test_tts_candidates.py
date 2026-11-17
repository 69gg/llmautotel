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


def test_pcm_passthrough_clips_without_changing_sample_count() -> None:
    assert np.frombuffer(FloatPCM(24000).feed([-2, 0, 2]), dtype="<i2").tolist() == [
        -32767,
        0,
        32767,
    ]
