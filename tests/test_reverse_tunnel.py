"""反向转发的监听边界和密钥分离，不启动模型或 SSH 连接。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from experiments.openi_latency.reverse_tunnel import prepare_identity, ssh_command, start


def settings(tmp_path: Path) -> dict[str, Any]:
    return {
        "host": "voice.example.com",
        "username": "voice_tunnel",
        "remote_port": 18765,
        "known_host": "voice.example.com ssh-ed25519 AAAATEST",
        "identity_directory": str(tmp_path / "private"),
        "public_key_output": str(tmp_path / "output" / "tunnel-public-key.json"),
    }


def test_forwarding_pins_host_key_and_listens_only_on_server_loopback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENI_SELF_PORT", "8007")
    command = ssh_command(settings(tmp_path), tmp_path / "key", tmp_path / "known")
    assert command[command.index("-R") + 1] == "127.0.0.1:18765:127.0.0.1:8007"
    assert "StrictHostKeyChecking=yes" in command
    assert "IdentitiesOnly=yes" in command and "ExitOnForwardFailure=yes" in command
    assert command[-1] == "voice_tunnel@voice.example.com"


@pytest.mark.parametrize("field,value", [("host", "-oProxyCommand=id"), ("username", "root;id")])
def test_endpoint_options_cannot_be_injected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: str
) -> None:
    monkeypatch.setenv("OPENI_SELF_PORT", "8000")
    config = settings(tmp_path)
    config[field] = value
    with pytest.raises(ValueError):
        ssh_command(config, tmp_path / "key", tmp_path / "known")


def test_rejects_host_key_for_another_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENI_SELF_PORT", "8000")
    config = settings(tmp_path)
    config["known_host"] = "other.example.com ssh-ed25519 AAAATEST"
    with pytest.raises(ValueError, match="known_host_mismatch"):
        ssh_command(config, tmp_path / "key", tmp_path / "known")


def test_private_material_cannot_be_in_platform_output(tmp_path: Path) -> None:
    config = settings(tmp_path)
    config["identity_directory"] = str(tmp_path / "output" / "private")
    with pytest.raises(ValueError, match="identity_must_not_be_in_output"):
        prepare_identity(config)


def test_only_public_key_is_published_with_restricted_private_permissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = settings(tmp_path)
    directory = Path(config["identity_directory"])
    directory.mkdir()
    identity = directory / "identity"
    identity.write_text("PRIVATE_TEST_MATERIAL")
    monkeypatch.setattr(
        "experiments.openi_latency.reverse_tunnel.subprocess.check_output",
        lambda *args, **kwargs: "ssh-ed25519 AAAATEST\n",
    )
    _, known = prepare_identity(config)
    published = Path(config["public_key_output"]).read_text()
    assert "AAAATEST" in published and "PRIVATE_TEST_MATERIAL" not in published
    assert identity.stat().st_mode & 0o777 == 0o600
    assert directory.stat().st_mode & 0o777 == 0o700
    assert known.stat().st_mode & 0o777 == 0o600


def test_disabled_tunnel_does_not_spawn_any_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected(*args: Any, **kwargs: Any) -> None:
        pytest.fail("disabled tunnel spawned a process")

    monkeypatch.setattr("experiments.openi_latency.reverse_tunnel.subprocess.Popen", unexpected)
    start(tmp_path, {})
