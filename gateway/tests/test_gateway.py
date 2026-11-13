"""Tests for the nvr_bridge gateway (Flask test client).

A stub core records what the gateway relays.  Transport-level behaviour
(chunked bodies, keep-alive connections) is covered by the gunicorn
end-to-end test.
"""

import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from nvr_common import MAX_BODY_BYTES, HttpForwarder
from nvr_gateway import create_app
from nvr_gateway.config import Config, ConfigError, load_config


def wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met within timeout")


class Recording:
    """Thread-safe record of what the stub core received."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.requests: list[dict] = []

    def add(self, headers: dict[str, str], body: bytes) -> None:
        with self.lock:
            self.requests.append({"headers": headers, "body": body})

    def count(self) -> int:
        with self.lock:
            return len(self.requests)


@pytest.fixture
def stub_core():
    """A stub core recording every POST it receives (and its auth header)."""
    recorder = Recording()

    class StubCoreHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            recorder.add(
                {k.lower(): v for k, v in self.headers.items()},
                self.rfile.read(length),
            )
            payload = json.dumps({"status": "received", "kind": "alarm"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), StubCoreHandler)
    server.daemon_threads = True
    threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    ).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/api/webhook", recorder
    server.shutdown()
    server.server_close()


@pytest.fixture
def api_key(tmp_path: Path) -> tuple[Path, str]:
    key = secrets.token_urlsafe(32)
    key_file = tmp_path / "api_key"
    key_file.write_text(key)
    return key_file, key


@pytest.fixture
def gateway(stub_core, api_key):
    """The gateway app (test client) relaying to a stub core."""
    core_url, recorder = stub_core
    key_file, key = api_key
    cfg = Config(core_url=core_url, api_key_file=key_file)
    app = create_app(cfg)
    yield app.test_client(), recorder, key


def relay(client, body: bytes, path: str = "/API/AlarmEvent/EventPush"):
    return client.post(
        path,
        data=body,
        headers={"Content-Type": "application/json;charset=UTF-8"},
    )


# ---------------------------------------------------------------------------
# config loading
# ---------------------------------------------------------------------------
def write_config(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data))
    return path


def test_load_config_defaults(tmp_path: Path) -> None:
    cfg = load_config(
        write_config(
            tmp_path,
            {"CORE_URL": "https://nvr.example.com/api/webhook", "API_KEY_FILE": "/run/k"},
        )
    )
    assert cfg.listen == "0.0.0.0"
    assert cfg.port == 8520
    assert cfg.log_level == "INFO"


def test_load_config_full(tmp_path: Path) -> None:
    cfg = load_config(
        write_config(
            tmp_path,
            {
                "CORE_URL": "https://nvr.example.com/api/webhook",
                "API_KEY_FILE": "/run/secrets/key",
                "LISTEN": "127.0.0.1",
                "PORT": 9000,
                "LOG_LEVEL": "debug",
            },
        )
    )
    assert cfg.listen == "127.0.0.1"
    assert cfg.port == 9000
    assert cfg.log_level == "DEBUG"  # case-normalised


@pytest.mark.parametrize(
    "data",
    [
        {},  # CORE_URL and API_KEY_FILE are required
        {"CORE_URL": "https://nvr.example.com"},  # API_KEY_FILE missing
        {"API_KEY_FILE": "/run/k"},  # CORE_URL missing
        # the gateway exists to upgrade to HTTPS: plain http is a config error
        {"CORE_URL": "http://nvr.example.com/api/webhook", "API_KEY_FILE": "/run/k"},
        {"CORE_URL": "notaurl", "API_KEY_FILE": "/run/k"},
        {"CORE_URL": "https://nvr.example.com", "API_KEY_FILE": ""},
        {"CORE_URL": "https://nvr.example.com", "API_KEY_FILE": "/run/k", "PORT": 0},
        {"CORE_URL": "https://nvr.example.com", "API_KEY_FILE": "/run/k", "PORT": 70000},
        {"CORE_URL": "https://nvr.example.com", "API_KEY_FILE": "/run/k", "PORT": True},
        {"CORE_URL": "https://nvr.example.com", "API_KEY_FILE": "/run/k", "LISTEN": ""},
        {"CORE_URL": "https://nvr.example.com", "API_KEY_FILE": "/run/k", "LOG_LEVEL": "LOUD"},
    ],
)
def test_load_config_rejects_bad_values(tmp_path: Path, data: dict) -> None:
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, data))


# ---------------------------------------------------------------------------
# relay behaviour
# ---------------------------------------------------------------------------
def test_push_accepted_and_relayed_with_api_key(gateway) -> None:
    client, recorder, key = gateway
    body = json.dumps({"data": {"alarm_list": []}}).encode()
    response = relay(client, body)
    assert response.status_code == 200
    assert response.get_json() == {"status": "accepted"}
    wait_for(lambda: recorder.count() == 1)
    request = recorder.requests[0]
    assert request["body"] == body  # relayed byte-for-byte
    assert request["headers"]["content-type"] == "application/json;charset=UTF-8"
    assert request["headers"]["authorization"] == f"Bearer {key}"


@pytest.mark.parametrize("path", ["/", "/API/AlarmEvent/EventPush", "/anything/else"])
def test_every_post_path_is_accepted(gateway, path: str) -> None:
    """The NVR POSTs to various device paths and must always get a 200."""
    client, recorder, _key = gateway
    assert relay(client, b"{}", path=path).status_code == 200
    wait_for(lambda: recorder.count() == 1)


def test_non_json_body_relayed_untouched(gateway) -> None:
    client, recorder, _key = gateway
    response = relay(client, b"\xff\xfe binary garbage")
    assert response.status_code == 200
    wait_for(lambda: recorder.count() == 1)
    assert recorder.requests[0]["body"] == b"\xff\xfe binary garbage"


def test_get_serves_nothing(gateway) -> None:
    """The NVR-facing port must not leak anything on GET (404 or 405)."""
    client, _recorder, _key = gateway
    for path in ("/", "/robots.txt", "/snaps/snap_x_object.jpg"):
        response = client.get(path)
        assert response.status_code in (404, 405), path
        assert response.get_json() == {"error": "not found"} or response.status_code == 405


def test_responses_are_not_stored(gateway) -> None:
    client, _recorder, _key = gateway
    response = relay(client, b"{}")
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_oversized_declared_body_rejected_without_relay(gateway) -> None:
    client, recorder, _key = gateway
    response = client.post(
        "/API/AlarmEvent/EventPush",
        data=b"x" * (MAX_BODY_BYTES + 2),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413
    assert response.get_json() == {"error": "body too large"}
    # exactly one byte over the cap: caught by the view's own check
    response = client.post(
        "/API/AlarmEvent/EventPush",
        data=b"x" * (MAX_BODY_BYTES + 1),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413
    assert recorder.count() == 0


def test_unreachable_core_never_breaks_the_nvr(stub_core, api_key) -> None:
    _url, _recorder = stub_core
    key_file, _key = api_key
    forwarder = HttpForwarder(
        "http://127.0.0.1:1/api/webhook", timeout=0.5, workers=1
    )  # nothing listens there
    cfg = Config(core_url="https://core.example.com/api/webhook", api_key_file=key_file)
    client = create_app(cfg, forwarder=forwarder).test_client()
    try:
        response = relay(client, b'{"data": {}}')
        assert response.status_code == 200
        assert response.get_json() == {"status": "accepted"}
        wait_for(lambda: forwarder.state.last_forward_error is not None)
    finally:
        forwarder.shutdown()


def test_core_rejecting_key_still_answers_the_nvr(api_key, stub_core) -> None:
    """The relay is asynchronous: core-side auth failures never reach the NVR."""
    core_url, recorder = stub_core
    key_file, _wrong_key = api_key
    forwarder = HttpForwarder(
        core_url,
        headers={"Authorization": "Bearer not-the-real-key"},
        workers=1,
        timeout=2,
    )
    cfg = Config(core_url=core_url, api_key_file=key_file)
    client = create_app(cfg, forwarder=forwarder).test_client()
    try:
        assert relay(client, b"{}").status_code == 200
        wait_for(lambda: recorder.count() == 1)  # the core saw (and logged) the attempt
    finally:
        forwarder.shutdown()


def test_queue_overflow_drops_instead_of_blocking(api_key) -> None:
    key_file, _key = api_key
    forwarder = HttpForwarder("https://core.example.com/api/webhook", maxsize=1, workers=0)
    cfg = Config(core_url="https://core.example.com/api/webhook", api_key_file=key_file)
    client = create_app(cfg, forwarder=forwarder).test_client()
    try:
        assert relay(client, b"first").status_code == 200
        assert relay(client, b"second").status_code == 200  # dropped, not failed
        assert forwarder.state.last_forward_error == "forward queue full; push dropped"
    finally:
        forwarder.shutdown()


# ---------------------------------------------------------------------------
# load_int (gateway-only config loader)
# ---------------------------------------------------------------------------
def test_load_int_bounds() -> None:
    from nvr_gateway.config import load_int

    assert load_int({"PORT": 9000}, "PORT", 8520, minimum=1, maximum=65535) == 9000
    assert load_int({}, "PORT", 8520, minimum=1, maximum=65535) == 8520
    for bad in ("not an int", 0, 70000, True, None):
        with pytest.raises(ConfigError):
            load_int({"PORT": bad}, "PORT", 8520, minimum=1, maximum=65535)
