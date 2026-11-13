"""Shared fixtures and helpers for the core test suite.

Everything here is imported (or, for fixtures, requested) by more than one
test module; single-module helpers stay in their module.
"""

import base64
import dataclasses
import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from nvr_core.config import Config

TEST_SECRET_KEY = "test-secret-key-0123456789abcdef0123456789"

# For tests without a fake Home Assistant: port 1 refuses instantly, so
# forwarding just logs a warning and moves on.
DEAD_HA_URL = "http://127.0.0.1:1/api/webhook/test"

# A minimal fake JPEG: the core only validates the SOI magic bytes.
# Long enough that its base64 form crosses the redaction threshold.
FAKE_JPEG = b"\xff\xd8\xff\xe0" + b"<fake jpeg payload>" * 32
FAKE_JPEG_B64 = base64.b64encode(FAKE_JPEG).decode()

PUBLIC_URL = "https://nvr.example.com"
"""The public URL snapshot links are asserted against (config.public_url)."""
DEV_NET = [{"phy": "eth1", "ip": "192.168.0.20", "mac": "04-B0:66:XX-XX-XX"}]


# ---------------------------------------------------------------------------
# push body builders (shapes from captured device pushes)
# ---------------------------------------------------------------------------
def alarm_body(channel: str = "CH2", subtype: str = "pd_vd") -> dict:
    return {
        "data": {
            "alarm_list": [
                {
                    "time": "2026-11-02T21:55:51Z-07:00",
                    "channel_alarm": [
                        {
                            "channel": channel,
                            "chn_alias": "",
                            "int_alarm": {
                                "take_alarm_snap": 1,
                                "time": "2026-11-02T21:55:51Z-07:00",
                                "alarm_val": True,
                                "int_subtype": subtype,
                            },
                        }
                    ],
                }
            ],
            "dev_net_info": DEV_NET,
            "device_name": "testing",
        }
    }


def snapshot_body(
    snap_id_: int = 8588,
    chn: int = 1,
    str_chn: str = "CH2",
    object_b64: str = FAKE_JPEG_B64,
    background_b64: str = FAKE_JPEG_B64,
) -> dict:
    return {
        "data": {
            "ai_snap_picture": {
                "SnapedObjInfo": [
                    {
                        "Chn": chn,
                        "StrChn": str_chn,
                        "chn_alias": "",
                        "StartTime": 1793631350,
                        "EndTime": 1793631352,
                        "SnapId": snap_id_,
                        "Type": 1,
                        "person_gender": "Female",
                        "person_age": "Adult",
                        "person_hat": "No",
                        "person_upper_color": "UpperBlack",
                        "ObjectImage": object_b64,
                        "Background": background_b64,
                    }
                ]
            },
            "dev_net_info": DEV_NET,
            "device_name": "testing",
        }
    }


# ---------------------------------------------------------------------------
# fakes and fixtures
# ---------------------------------------------------------------------------
def wait_for(predicate, timeout: float = 8.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met within timeout")


class Recording:
    """Thread-safe record of what the fake Home Assistant webhook received."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.requests: list[dict] = []

    def add(self, content_type: str, body: bytes) -> None:
        with self.lock:
            self.requests.append({"content_type": content_type, "body": body})

    def bodies(self) -> list[bytes]:
        with self.lock:
            return [r["body"] for r in self.requests]

    def count(self) -> int:
        with self.lock:
            return len(self.requests)


@pytest.fixture
def ha_recorder():
    """Fake Home Assistant webhook recording every POST it receives."""
    recorder = Recording()

    class FakeHAHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            recorder.add(
                self.headers.get("Content-Type") or "",
                self.rfile.read(length),
            )
            payload = json.dumps({"status": "received"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeHAHandler)
    server.daemon_threads = True
    threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    ).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/api/webhook/nvr", recorder
    server.shutdown()
    server.server_close()


@pytest.fixture
def api_key(tmp_path: Path) -> tuple[Path, str]:
    key = secrets.token_urlsafe(32)
    key_file = tmp_path / "api_key"
    key_file.write_text(key)
    return key_file, key


def make_config(
    tmp_path: Path, key_file: Path, ha_webhook_url: str = DEAD_HA_URL, **overrides: object
) -> Config:
    config = Config(
        api_key_file=key_file,
        secret_key=TEST_SECRET_KEY,
        oidc_client_id="test-client-id",
        oidc_client_secret="test-client-secret",
        oidc_discovery_url="https://idp.example.com/.well-known/openid-configuration",
        public_url="https://nvr.example.com",
        roles_path=["nvr_roles"],
        ha_webhook_url=ha_webhook_url,
        snaps_dir=tmp_path / "snaps",
        database_url=f"sqlite:///{tmp_path}/events.db",
    )
    return dataclasses.replace(config, **overrides)


def authed(app, roles: tuple[str, ...] = ("nvr_viewers",)):
    """A test client with a session simulating a signed-in user."""
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user"] = {
            "sub": "test-user-sub",
            "email": "test@example.com",
            "name": "Test User",
            "roles": list(roles),
        }
    return client


def push(client, body: bytes | dict, key: str, path: str = "/api/webhook"):
    payload = json.dumps(body).encode() if isinstance(body, dict) else body
    return client.post(
        path,
        data=payload,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
