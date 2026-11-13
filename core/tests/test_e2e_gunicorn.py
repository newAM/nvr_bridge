"""End-to-end: gunicorn serves the core on an inherited (socket-activated) fd.

This mirrors production, where systemd passes the listening unix socket as
fd 3 and the service binds ``fd://3``.  Chunked push bodies are covered by
the gateway's end-to-end test (that is the interface the NVR talks to).
"""

import json
import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("gunicorn")


def http_over_unix(sock_path: str, request: bytes, timeout: float = 5.0) -> tuple[int, bytes]:
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(timeout)
    try:
        conn.connect(sock_path)
        conn.sendall(request)
        chunks = []
        while True:
            try:
                data = conn.recv(65536)
            except TimeoutError:
                break
            if not data:
                break
            chunks.append(data)
            if b"\r\n\r\n" in b"".join(chunks):
                _head, _, body = b"".join(chunks).partition(b"\r\n\r\n")
                if len(body) >= 26 or b"User-agent" in body:  # robots.txt body
                    break
    finally:
        conn.close()
    raw = b"".join(chunks)
    status_line, _, rest = raw.partition(b"\r\n")
    status = int(status_line.split()[1])
    return status, rest.partition(b"\r\n\r\n")[2]


def wait_serving(sock_path: str, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            status, _ = http_over_unix(sock_path, b"GET /robots.txt HTTP/1.0\r\nHost: x\r\n\r\n")
            if status == 200:
                return
        except OSError:
            time.sleep(0.05)
    raise AssertionError("gunicorn never came up on the inherited fd")


def test_gunicorn_serves_on_inherited_fd(tmp_path: Path) -> None:
    key = secrets.token_urlsafe(32)
    key_file = tmp_path / "api_key"
    key_file.write_text(key)
    secret_key_file = tmp_path / "secret_key"
    secret_key_file.write_text("test-secret-key-0123456789abcdef0123456789")
    oidc_secret_file = tmp_path / "oidc_secret"
    oidc_secret_file.write_text("test-oidc-client-secret")
    webhook_id_file = tmp_path / "webhook_id"
    webhook_id_file.write_text("test-webhook-id-0123456789abcdef")
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "API_KEY_FILE": str(key_file),
                "SECRET_KEY_FILE": str(secret_key_file),
                "OIDC_CLIENT_ID": "nvr_bridge",
                "OIDC_CLIENT_SECRET_FILE": str(oidc_secret_file),
                "OIDC_DISCOVERY_URL": ("https://idp.example.com/.well-known/openid-configuration"),
                "PUBLIC_URL": "https://nvr.example.com",
                "ROLES_PATH": ["nvr_roles"],
                # no push succeeds in this test, so the webhook URL can
                # point nowhere
                "HA_WEBHOOK_URL": "http://127.0.0.1:1/api/webhook",
                "HA_WEBHOOK_ID_FILE": str(webhook_id_file),
                "SNAPS_DIR": str(tmp_path / "snaps"),
                "DATABASE_URL": f"sqlite:///{tmp_path}/events.db",
            }
        )
    )

    sock_path = str(tmp_path / "core.sock")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(sock_path)
    listener.listen(8)
    fd = listener.fileno()

    env = {
        **os.environ,
        "NVR_CORE_CONFIG_FILE": str(config),
        "PYTHONPATH": os.pathsep.join(p for p in sys.path if p),
    }
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "gunicorn",
            "-w",
            "1",
            "--bind",
            f"fd://{fd}",
            "nvr_core.wsgi:application",
        ],
        env=env,
        pass_fds=(fd,),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        wait_serving(sock_path)

        status, body = http_over_unix(sock_path, b"GET /robots.txt HTTP/1.0\r\nHost: x\r\n\r\n")
        assert status == 200
        assert b"Disallow: /" in body

        # pushes without the API key are rejected on the real server too
        request = (
            b"POST /api/webhook HTTP/1.0\r\nHost: x\r\n"
            b"Content-Type: application/json\r\nContent-Length: 2\r\n\r\n{}"
        )
        status, body = http_over_unix(sock_path, request)
        assert status == 401
        assert b"unauthorized" in body
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        listener.close()
