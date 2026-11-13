"""Flask application factory for the nvr_bridge core service.

The core is the real forwarder: it accepts pushes relayed by the gateway
(API-key authenticated behind the reverse proxy), classifies them, decodes
and stores AI snapshot JPEGs, answers fast, and forwards to the Home
Assistant webhook through a bounded queue.  It also serves stored
snapshots under unguessable names (the URL is the capability), records
every push in a small database, and shows those events on an
SSO-protected web page.
"""

import json
import logging
from datetime import timedelta

from flask import Flask, Response, abort, jsonify, request
from flask_session import Session
from flask_wtf.csrf import CSRFProtect
from nvr_common import MAX_BODY_BYTES, HttpForwarder, load_api_key
from systemd.journal import JournalHandler

from nvr_core.auth import auth_bp, current_user, init_oauth
from nvr_core.config import Config
from nvr_core.models import Event, db
from nvr_core.payload import pretty_body, redact_body
from nvr_core.push import ProcessedPush, process_push, require_api_key
from nvr_core.store import SnapshotStore
from nvr_core.web import create_web_blueprint

ROBOTS_TXT = "User-agent: *\nDisallow: /\n"
"""Served so crawlers stay out of the snapshots."""

csrf = CSRFProtect()

_session_interface = None
"""flask-session 0.8 re-registers its model class into the shared ``db``
metadata on every init, which only works once per process (fine in
production: one app per gunicorn worker).  Later apps -- created by the
test suite with identical session settings -- reuse the interface."""


def _init_session(app: Flask) -> None:
    global _session_interface
    if _session_interface is None:
        Session(app)
        _session_interface = app.session_interface
    else:
        app.session_interface = _session_interface


def _setup_logging(level: int = logging.INFO) -> None:
    handler = JournalHandler(SYSLOG_IDENTIFIER="nvr_bridge_core")
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


def _record_event(processed) -> None:
    """Store one push as an event row; never breaks the push response."""
    if processed.kind == "keepalive":
        return
    try:
        db.session.add(
            Event(
                kind=processed.kind or "unknown",
                device_name=processed.device_name,
                str_chn=processed.str_chn,
                object_snap=processed.object_snap,
                background_snap=processed.background_snap,
                live_snaps=json.dumps(processed.live_snaps or []),
                group_token=processed.group,
                payload=processed.redacted_payload,
            )
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        logging.getLogger(__name__).exception("failed recording event")


def _prune(store: SnapshotStore, config: Config) -> None:
    """Apply the event retention: image files and their event rows go
    together, so the pages never reference missing images.  Never breaks
    the push response."""
    if config.event_retention_days <= 0:
        return
    try:
        removed = store.prune(config.event_retention_days)
        if removed:
            logging.getLogger(__name__).info(
                "pruned %d snapshots (retention %dd)", removed, config.event_retention_days
            )
        deleted = Event.prune(config.event_retention_days)
        if deleted:
            logging.getLogger(__name__).info(
                "pruned %d events (retention %dd)", deleted, config.event_retention_days
            )
    except Exception:
        logging.getLogger(__name__).exception("failed pruning")


def create_app(
    config: Config,
    store: SnapshotStore | None = None,
    forwarder: HttpForwarder | None = None,
) -> Flask:
    _setup_logging(logging.getLevelNamesMapping()[config.log_level])

    app = Flask(__name__)
    # One byte above the cap so the view can detect oversized chunked
    # (terminated) streams itself: werkzeug silently truncates them at the
    # limit, but rejects declared Content-Lengths above it before the view
    # runs.
    app.config["MAX_CONTENT_LENGTH"] = MAX_BODY_BYTES + 1
    app.config["API_KEY"] = load_api_key(config.api_key_file)
    app.config.update(
        {
            "SECRET_KEY": config.secret_key,
            "SQLALCHEMY_DATABASE_URI": config.database_url,
            # OIDC / SSO
            "OIDC_CLIENT_ID": config.oidc_client_id,
            "OIDC_CLIENT_SECRET": config.oidc_client_secret,
            "OIDC_DISCOVERY_URL": config.oidc_discovery_url,
            "PUBLIC_URL": config.public_url,
            "ROLES_PATH": config.roles_path,
            "USER_ROLE": config.user_role,
            # Server-side sessions in the same database as the events.
            "SESSION_TYPE": "sqlalchemy",
            "SESSION_SQLALCHEMY": db,
            "SESSION_COOKIE_HTTPONLY": True,
            "SESSION_COOKIE_SECURE": True,
            "SESSION_COOKIE_NAME": "__Host-session",
            "SESSION_PERMANENT": True,
            "PERMANENT_SESSION_LIFETIME": timedelta(hours=24),
        }
    )
    app.config.setdefault("SESSION_COOKIE_SAMESITE", config.session_cookie_samesite)

    db.init_app(app)
    _init_session(app)  # registers the session table before create_all below
    csrf.init_app(app)

    with app.app_context():
        db.create_all()

    store = store or SnapshotStore(config.snaps_dir)
    forwarder = forwarder or HttpForwarder(config.ha_webhook_url)

    @app.post("/api/webhook")
    @require_api_key
    def webhook() -> Response:
        body = request.get_data()
        if len(body) > MAX_BODY_BYTES:
            abort(413)
        try:
            processed = process_push(body, store, config, request.path)
        except Exception:  # the NVR (via the gateway) must always get its 200
            app.logger.exception("failed processing push (%d bytes)", len(body))
            processed = ProcessedPush(
                kind=None, forward_body=body, redacted_payload=redact_body(body)
            )
        if processed.kind != "keepalive":
            app.logger.debug("webhook rx (%s):\n%s", processed.kind, pretty_body(body))
        # The submit only queues: the device never waits on Home Assistant.
        if processed.forward_body is not None:
            forwarder.submit(processed.forward_body, request.content_type or "application/json")
        _record_event(processed)
        _prune(store, config)
        return jsonify({"status": "received", "kind": processed.kind})

    # The push webhook authenticates with the shared API key (from the
    # gateway), not with session cookies: no CSRF token applies.
    csrf.exempt(webhook)

    # -- viewer routes: the smallest surface the phone notifications need ----
    @app.get("/snaps/<name>")
    def snaps(name: str) -> Response:
        data = store.read(name)  # only flat generated snap_* names pass
        if data is None:
            abort(404)
        # The unguessable URL is the credential, but only for a short
        # window: after URL_RETENTION_DAYS anonymous access ends, while
        # signed-in users keep the image until EVENT_RETENTION_DAYS prunes
        # the event and its files.
        if store.age_days(name) > config.url_retention_days:
            user = current_user()
            if user is None or app.config["USER_ROLE"] not in user.get("roles", []):
                abort(404)
        return Response(data, mimetype="image/jpeg")

    @app.get("/robots.txt")
    def robots() -> Response:
        return Response(ROBOTS_TXT, content_type="text/plain")

    @app.errorhandler(401)
    def unauthorized(_error: object) -> Response:
        return _json_error(401, "unauthorized")

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
    def set_security_headers(response: Response) -> Response:
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
        # Allowlist only the resource types the events pages use; deny
        # everything else by default.
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; "
            "style-src 'self'; "
            "img-src 'self'; "
            "form-action 'self'; "
            "base-uri 'none'; "
            "frame-ancestors 'none'; "
            "upgrade-insecure-requests"
        )
        # Prevent browsers from MIME-sniffing responses away from the
        # declared content type.
        response.headers["X-Content-Type-Options"] = "nosniff"
        # Legacy framing protection for pre-CSP browsers.
        response.headers["X-Frame-Options"] = "DENY"
        # Snapshot URLs are capabilities: never cache them (or anything
        # else) in browsers or proxies.
        response.headers["Cache-Control"] = "no-store"
        # Event URLs must not leak to third-party sites via Referer.
        response.headers["Referrer-Policy"] = "same-origin"
        # Deny access to unused sensitive browser APIs.
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=()"
        )
        return response

    app.register_blueprint(auth_bp)
    init_oauth(app)
    create_web_blueprint(app)

    return app
