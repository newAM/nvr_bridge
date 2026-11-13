"""End-to-end: the device transport contract against real gunicorn.

The NVR uses one persistent HTTP/1.1 keep-alive connection and may send
bodies with ``Transfer-Encoding: chunked`` (which werkzeug's dev server
cannot parse -- this is why the gateway runs under gunicorn).  These tests
boot the real gateway under gunicorn with a stub core and exercise both.
"""

import http.client
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytest.importorskip("gunicorn")

RELAYED: list[dict] = []
LOCK = threading.Lock()


class StubCoreHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        with LOCK:
            RELAYED.append(
                {
                    "authorization": self.headers.get("Authorization") or "",
                    "content_type": self.headers.get("Content-Type") or "",
                    "body": self.rfile.read(length),
                }
            )
        payload = json.dumps({"status": "received"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        pass


def relayed() -> list[dict]:
    with LOCK:
        return list(RELAYED)


def wait_for(predicate, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("condition not met within timeout")


@pytest.fixture(scope="module")
def servers(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("gateway-e2e")

    stub = ThreadingHTTPServer(("127.0.0.1", 0), StubCoreHandler)
    stub.daemon_threads = True
    threading.Thread(target=stub.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    core_url = f"http://127.0.0.1:{stub.server_address[1]}/api/webhook"

    key = secrets.token_urlsafe(32)
    key_file = tmp_path / "api_key"
    key_file.write_text(key)
    # The gateway's config loader requires an https:// CORE_URL (that is its
    # job in production); the transport e2e uses a plain-HTTP stub core, so
    # it builds the Config directly.  Config validation is covered by unit
    # tests, chunked/keep-alive transport -- the point here -- is not
    # testable through werkzeug's dev server.
    wsgi = tmp_path / "wsgi_e2e.py"
    wsgi.write_text(
        "from nvr_gateway.app import create_app\n"
        "from nvr_gateway.config import Config\n"
        f'application = create_app(Config(core_url="{core_url}", api_key_file="{key_file}"))\n'
    )

    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([*filter(None, sys.path), str(tmp_path)]),
    }
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "gunicorn",
            "--config",
            "python:nvr_gateway.gunicorn_conf",
            "-w",
            "1",
            "-k",
            "gthread",
            "--threads",
            "4",
            "--bind",
            "127.0.0.1:0",
            "wsgi_e2e:application",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        # port 0 -> parse the bound port from gunicorn's startup output
        port = None
        deadline = time.time() + 15
        output: list[str] = []
        while time.time() < deadline and port is None:
            line = proc.stdout.readline() if proc.stdout else ""
            if line:
                output.append(line)
                match = re.search(r"Listening at: http://127\.0\.0\.1:(\d+)", line)
                if match:
                    port = int(match.group(1))
            elif proc.poll() is not None:
                break
        assert port is not None, "gunicorn did not report its port:\n" + "\n".join(output)
        yield f"127.0.0.1:{port}", key
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        stub.shutdown()
        stub.server_close()


def conn(servers) -> http.client.HTTPConnection:
    host_port, _key = servers
    return http.client.HTTPConnection(host_port, timeout=10)


def test_chunked_body_relayed(servers) -> None:
    """The device may send chunked bodies; gunicorn de-chunks, the app relays."""
    connection = conn(servers)
    _host, key = servers
    connection.putrequest("POST", "API/AlarmEvent/EventPush")  # no leading slash, like the device
    connection.putheader("Transfer-Encoding", "chunked")
    connection.putheader("Content-Type", "application/json;charset=UTF-8")
    connection.endheaders()
    for chunk in (b'{"data": {"alarm_', b'list": [1]}}'):
        connection.send(b"%x\r\n%s\r\n" % (len(chunk), chunk))
    connection.send(b"0\r\n\r\n")
    response = connection.getresponse()
    assert response.status == 200
    assert json.loads(response.read()) == {"status": "accepted"}
    connection.close()

    wait_for(lambda: len(relayed()) >= 1)
    request = relayed()[0]
    assert request["body"] == b'{"data": {"alarm_list": [1]}}'
    assert request["authorization"] == f"Bearer {key}"
    assert request["content_type"] == "application/json;charset=UTF-8"


def test_plain_body_without_leading_slash(servers) -> None:
    """The NVR omits the leading slash on some request lines (see
    device_http.py); gunicorn would 400 these without the shim."""
    base = len(relayed())
    connection = conn(servers)
    connection.putrequest("POST", "API/AlarmEvent/EventPush")
    connection.putheader("Content-Type", "application/json")
    body = b'{"result": "success", "data": {}}'
    connection.putheader("Content-Length", str(len(body)))
    connection.endheaders()
    connection.send(body)
    response = connection.getresponse()
    assert response.status == 200
    assert json.loads(response.read()) == {"status": "accepted"}
    connection.close()

    wait_for(lambda: len(relayed()) >= base + 1)
    assert relayed()[base]["body"] == body


def test_keepalive_connections_concurrent_and_reused(servers) -> None:
    """The NVR pushes everything over one keep-alive connection; a second
    connection must be served concurrently, and the first must stay usable."""
    base = len(relayed())  # earlier module tests also relay
    first = conn(servers)
    first.request("POST", "/API/HttpListening/KeepLive", body=b'{"data": {}}')
    response = first.getresponse()
    assert response.status == 200
    response.read()

    second = conn(servers)  # served while the first connection is held open
    second.request("POST", "/API/AlarmEvent/EventPush", body=b'{"data": {}}')
    response = second.getresponse()
    assert response.status == 200
    response.read()

    # the first (keep-alive) connection is still usable afterwards
    first.request("POST", "/API/AlarmEvent/EventPush", body=b'{"data": {}}')
    response = first.getresponse()
    assert response.status == 200
    response.read()

    first.close()
    second.close()
    wait_for(lambda: len(relayed()) >= base + 3)
    mine = relayed()[base:]
    assert all(r["body"] == b'{"data": {}}' for r in mine)
