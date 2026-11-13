"""Tests for nvr_common.forwarder."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from nvr_common import BridgeState, HttpForwarder


class Recording:
    """Thread-safe record of what the receiving stub got."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.requests: list[dict] = []

    def add(self, path: str, headers: dict[str, str], body: bytes) -> None:
        with self.lock:
            self.requests.append({"path": path, "headers": headers, "body": body})

    def count(self) -> int:
        with self.lock:
            return len(self.requests)


@pytest.fixture
def receiver():
    """A stub HTTP endpoint recording every POST it receives."""
    recorder = Recording()

    class StubHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            recorder.add(
                self.path,
                {k.lower(): v for k, v in self.headers.items()},
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

    server = ThreadingHTTPServer(("127.0.0.1", 0), StubHandler)
    server.daemon_threads = True
    threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    ).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/api/webhook", recorder
    server.shutdown()
    server.server_close()


def wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met within timeout")


def test_forward_delivers_body_content_type_and_headers(receiver) -> None:
    url, recorder = receiver
    state = BridgeState()
    forwarder = HttpForwarder(
        url,
        state,
        headers={"Authorization": "Bearer test-key"},
        workers=1,
    )
    try:
        forwarder.submit(b'{"push": 1}', "application/json;charset=UTF-8")
        wait_for(lambda: recorder.count() == 1)
        request = recorder.requests[0]
        assert request["path"] == "/api/webhook"
        assert request["body"] == b'{"push": 1}'
        assert request["headers"]["content-type"] == "application/json;charset=UTF-8"
        assert request["headers"]["authorization"] == "Bearer test-key"
        assert state.last_forward_error is None
    finally:
        forwarder.shutdown()


def test_forwarder_drops_when_queue_full() -> None:
    state = BridgeState()
    forwarder = HttpForwarder("http://127.0.0.1:1/hook", state, maxsize=1, workers=0)
    forwarder.submit(b"first", "application/json")
    forwarder.submit(b"second", "application/json")  # dropped and noted
    assert state.last_forward_error == "forward queue full; push dropped"
    forwarder.shutdown()


def test_forwarder_noop_without_url() -> None:
    state = BridgeState()
    forwarder = HttpForwarder(None, state, workers=2)
    forwarder.submit(b"push", "application/json")  # must neither raise nor enqueue
    assert state.last_forward_error is None
    forwarder.shutdown()


def test_forwarder_notes_connection_errors() -> None:
    # Port 1 on localhost: nothing listens there, so the POST fails.
    state = BridgeState()
    forwarder = HttpForwarder("http://127.0.0.1:1/hook", state, timeout=0.5, workers=1)
    try:
        forwarder.submit(b"push", "application/json")
        wait_for(lambda: state.last_forward_error is not None)
    finally:
        forwarder.shutdown()
    assert state.last_forward_error is not None
