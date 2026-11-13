"""Flask application factory for the nvr_bridge gateway.

The gateway is the NVR-facing edge: the Uniview HTTP Listening protocol
can neither authenticate nor encrypt, so the gateway accepts the device's
plain-HTTP POSTs on any path, answers ``200`` fast (the NVR marks slow
listeners dead), and relays each push byte-for-byte to the core over
HTTPS with the shared API key, through a bounded queue so the device
never waits on anything downstream.  It knows nothing about push
contents -- classification lives in the core.
"""

import logging

from flask import Flask, Response, abort, jsonify, request
from flask.typing import ResponseReturnValue
from nvr_common import MAX_BODY_BYTES, HttpForwarder, load_api_key
from systemd.journal import JournalHandler
from werkzeug.exceptions import HTTPException

from nvr_gateway.config import Config


def _setup_logging(level: int = logging.INFO) -> None:
    handler = JournalHandler(SYSLOG_IDENTIFIER="nvr_bridge_gateway")
    handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter("[{name}] {message}", style="{")
    handler.setFormatter(formatter)
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    root_logger.addHandler(handler)


def _json_error(status: int, message: str) -> Response:
    response = jsonify({"error": message})
    response.status_code = status
    return response


def create_app(config: Config, forwarder: HttpForwarder | None = None) -> Flask:
    _setup_logging(logging.getLevelNamesMapping()[config.log_level])

    app = Flask(__name__)
    # One byte above the cap so the view can detect oversized chunked
    # (terminated) streams itself: werkzeug silently truncates them at the
    # limit, but rejects declared Content-Lengths above it before the view
    # runs.
    app.config["MAX_CONTENT_LENGTH"] = MAX_BODY_BYTES + 1
    if forwarder is None:
        forwarder = HttpForwarder(
            config.core_url,
            headers={"Authorization": f"Bearer {load_api_key(config.api_key_file)}"},
        )

    @app.post("/")
    @app.post("/<path:device_path>")
    def relay(device_path: str | None = None) -> ResponseReturnValue:
        try:
            body = request.get_data()
            if len(body) > MAX_BODY_BYTES:
                abort(413)
            # The submit only queues: the NVR never waits on the core.
            forwarder.submit(body, request.content_type or "application/json")
        except HTTPException:
            raise
        except Exception:  # the NVR must always get its 200
            app.logger.exception("failed relaying push to %s", device_path)
        return jsonify({"status": "accepted"})

    @app.errorhandler(404)
    def not_found(_error: object) -> Response:
        return _json_error(404, "not found")

    @app.errorhandler(405)
    def method_not_allowed(_error: object) -> Response:
        return _json_error(405, "method not allowed")

    @app.errorhandler(413)
    def body_too_large(_error: object) -> Response:
        return _json_error(413, "body too large")

    @app.after_request
    def security_headers(response: Response) -> Response:
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    return app
