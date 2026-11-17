"""可选的 GPU 主动 SSH 转发；私钥仅在 GPU 临时目录生成。"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def prepare_identity(config: dict[str, Any]) -> tuple[Path, Path]:
    directory = Path(config["identity_directory"]).resolve()
    output = Path(config["public_key_output"]).resolve()
    if directory == output.parent or directory.is_relative_to(output.parent):
        raise ValueError("identity_must_not_be_in_output_directory")
    if output.is_relative_to(directory):
        raise ValueError("output_must_not_be_in_identity_directory")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    identity = directory / "identity"
    if not identity.exists():
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(identity)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    identity.chmod(0o600)
    public = subprocess.check_output(["ssh-keygen", "-y", "-f", str(identity)], text=True).strip()
    if not public.startswith("ssh-ed25519 ") or "\n" in public:
        raise ValueError("unexpected_public_key_format")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"public_key": public}, ensure_ascii=False))
    output.chmod(0o600)
    known_hosts = directory / "known_hosts"
    known_hosts.write_text(config["known_host"].strip() + "\n")
    known_hosts.chmod(0o600)
    return identity, known_hosts


def ssh_command(config: dict[str, Any], identity: Path, known_hosts: Path) -> list[str]:
    host, username = config["host"], config["username"]
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.-]*", host):
        raise ValueError("invalid_tunnel_host")
    if not re.fullmatch(r"[a-z_][a-z0-9_-]*", username):
        raise ValueError("invalid_tunnel_username")
    ssh_port = int(config.get("ssh_port", 22))
    remote_port = int(config["remote_port"])
    target_port = int(os.environ["OPENI_SELF_PORT"])
    if not all(1 <= port <= 65535 for port in (ssh_port, remote_port, target_port)):
        raise ValueError("invalid_tunnel_port")
    expected = host if ssh_port == 22 else f"[{host}]:{ssh_port}"
    if config["known_host"].split()[0] != expected:
        raise ValueError("known_host_mismatch")
    return [
        "ssh",
        "-N",
        "-T",
        "-p",
        str(ssh_port),
        "-i",
        str(identity),
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={known_hosts}",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=3",
        "-R",
        f"127.0.0.1:{remote_port}:127.0.0.1:{target_port}",
        f"{username}@{host}",
    ]


def parent_alive(parent_pid: int) -> bool:
    try:
        os.kill(parent_pid, 0)
    except ProcessLookupError:
        return False
    return True


def run(config: dict[str, Any], parent_pid: int) -> None:
    identity, known_hosts = prepare_identity(config)
    command = ssh_command(config, identity, known_hosts)
    # 此处只上传公钥；私钥目录与平台输出目录严格分离。
    from c2net.context import upload_output

    upload_output()
    print('OPENI_EVENT:{"type":"status","phase":"tunnel_public_key_uploaded"}', flush=True)
    while parent_alive(parent_pid):
        with subprocess.Popen(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        ) as process:
            while parent_alive(parent_pid) and process.poll() is None:
                time.sleep(1)
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
        if parent_alive(parent_pid):
            time.sleep(float(config.get("retry_seconds", 5)))


def start(root: Path, config: dict[str, Any]) -> None:
    if not config.get("reverse_tunnel", {}).get("enabled", False):
        return
    if not shutil.which("ssh") or not shutil.which("ssh-keygen"):
        raise RuntimeError("reverse_tunnel_requires_openssh_client")
    subprocess.Popen(
        [
            sys.executable,
            "-u",
            str(root / "reverse_tunnel.py"),
            "--config",
            str(root / "settings.run.json"),
            "--parent-pid",
            str(os.getpid()),
        ],
        start_new_session=True,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--parent-pid", type=int, required=True)
    arguments = parser.parse_args()
    run(json.loads(arguments.config.read_text())["reverse_tunnel"], arguments.parent_pid)
