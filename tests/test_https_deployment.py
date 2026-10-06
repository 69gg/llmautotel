"""真实 Nginx TLS、鉴权与代理隔离测试；无 Nginx 时跳过，可独立运行。"""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen


class ProbeUpstream(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        payload = json.dumps(
            {
                "authorization": self.headers.get("Authorization"),
                "origin": self.headers.get("Origin"),
                "host": self.headers.get("Host"),
                "scheme": self.headers.get("X-Forwarded-Proto"),
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_POST = do_GET
    do_PATCH = do_GET

    def log_message(self, format: str, *args: Any) -> None:
        pass


class HTTPSDeploymentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        executable = shutil.which("nginx")
        if executable is None or shutil.which("openssl") is None:
            raise unittest.SkipTest("需要 Nginx 和 OpenSSL；可在部署服务器独立运行")
        cls.directory = tempfile.TemporaryDirectory(prefix="llmautotel-nginx-test-")
        cls.addClassCleanup(cls.directory.cleanup)
        root = Path(cls.directory.name)
        root.chmod(0o755)  # 仅包含测试凭据，允许 Nginx worker 遍历。
        cert = root / "certificate.pem"
        key = root / "private-key.pem"
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "1",
                "-subj",
                "/CN=127.0.0.1",
                "-addext",
                "subjectAltName=IP:127.0.0.1",
                "-keyout",
                str(key),
                "-out",
                str(cert),
            ],
            check=True,
            capture_output=True,
        )
        password_hash = subprocess.run(
            ["openssl", "passwd", "-6", "-stdin"],
            input="fixture-password\n",
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        password_file = root / "test.htpasswd"
        password_file.write_text("fixture-user:" + password_hash + "\n")
        password_file.chmod(0o644)  # 固定测试密码，不使用真实部署凭据。

        cls.upstream = ThreadingHTTPServer(("127.0.0.1", 0), ProbeUpstream)
        cls.addClassCleanup(cls.upstream.server_close)
        cls.thread = threading.Thread(target=cls.upstream.serve_forever, daemon=True)
        cls.thread.start()
        cls.addClassCleanup(cls.upstream.shutdown)
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            cls.port = reservation.getsockname()[1]
        cls.origin = f"https://127.0.0.1:{cls.port}"
        template = Path(__file__).resolve().parents[1] / "deploy" / "nginx-https.conf.example"
        configuration = template.read_text()
        replacements = {
            "REPLACE_WITH_HTTPS_LISTEN": f"127.0.0.1:{cls.port}",
            "REPLACE_WITH_PUBLIC_IP": "127.0.0.1",
            "REPLACE_WITH_PUBLIC_ORIGIN": cls.origin,
            "REPLACE_WITH_FULLCHAIN": str(cert),
            "REPLACE_WITH_PRIVATE_KEY": str(key),
            "REPLACE_WITH_PASSWORD_FILE": str(password_file),
            "REPLACE_WITH_UPSTREAM": f"http://127.0.0.1:{cls.upstream.server_port}",
        }
        for name, value in replacements.items():
            configuration = configuration.replace(name, value)
        site = root / "site.conf"
        site.write_text(configuration)
        main = root / "nginx.conf"
        worker_user = "user www-data;\n" if os.geteuid() == 0 else ""
        main.write_text(
            worker_user
            + f"pid {root / 'nginx.pid'};\nerror_log {root / 'error.log'};\n"
            + "events { worker_connections 64; }\n"
            + f"http {{ access_log off; include {site}; }}\n"
        )
        subprocess.run(
            [executable, "-t", "-c", str(main), "-p", str(root)], check=True, capture_output=True
        )
        cls.nginx = subprocess.Popen(
            [executable, "-c", str(main), "-p", str(root), "-g", "daemon off;"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        cls.addClassCleanup(cls.stop_nginx)
        cls.context = ssl.create_default_context(cafile=str(cert))
        deadline = time.monotonic() + 5
        while True:
            try:
                with socket.create_connection(("127.0.0.1", cls.port), timeout=0.2):
                    break
            except OSError:
                if cls.nginx.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError("测试 Nginx 未启动：" + (root / "error.log").read_text())
                time.sleep(0.05)

    @classmethod
    def stop_nginx(cls) -> None:
        cls.nginx.terminate()
        cls.nginx.wait(timeout=5)

    def request(
        self,
        path: str,
        *,
        password: str | None = None,
        origin: str | None = None,
        method: str = "GET",
    ) -> tuple[int, bytes]:
        headers: dict[str, str] = {}
        if password is not None:
            token = base64.b64encode(f"fixture-user:{password}".encode()).decode()
            headers["Authorization"] = "Basic " + token
        if origin is not None:
            headers["Origin"] = origin
        request = Request(self.origin + path, headers=headers, method=method)
        try:
            with urlopen(request, context=self.context, timeout=5) as response:
                return response.status, response.read()
        except HTTPError as error:
            with error:
                return error.code, error.read()

    def test_authentication_covers_page_configuration_history_and_signaling(self) -> None:
        for path, method in (
            ("/", "GET"),
            ("/api/settings", "GET"),
            ("/api/calls", "GET"),
            ("/api/calls", "POST"),
            ("/api/offer", "POST"),
            ("/api/offer", "PATCH"),
            ("/assets/app.js", "GET"),
        ):
            with self.subTest(path=path, method=method):
                self.assertEqual(self.request(path, method=method)[0], 401)
                self.assertEqual(
                    self.request(
                        path,
                        password="wrong-password",
                        method=method,
                    )[0],
                    401,
                )

    def test_login_is_not_forwarded_and_https_origin_is_preserved(self) -> None:
        status, body = self.request("/api/health", password="fixture-password")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertIsNone(payload["authorization"])
        self.assertEqual(payload["host"], f"127.0.0.1:{self.port}")
        self.assertEqual(payload["scheme"], "https")

    def test_foreign_browser_origin_is_rejected_and_same_origin_allowed(self) -> None:
        self.assertEqual(
            self.request(
                "/api/calls",
                password="fixture-password",
                origin="https://other.example.test",
                method="POST",
            )[0],
            403,
        )
        status, body = self.request(
            "/api/calls",
            password="fixture-password",
            origin=self.origin,
            method="POST",
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["origin"], self.origin)


if __name__ == "__main__":
    unittest.main()
