"""Tests for the SSO-protected events web UI.

Sessions are simulated by injecting the same user dict the OIDC callback
would store; the real IdP flow is exercised by the NixOS VM test.
"""

import json
import re
import secrets
import threading
from datetime import UTC, date, datetime, time, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from conftest import FAKE_JPEG, FAKE_JPEG_B64, alarm_body, authed, make_config, snapshot_body
from itsdangerous import URLSafeTimedSerializer
from nvr_core import create_app
from nvr_core.models import Event, db


@pytest.fixture
def app(tmp_path: Path, ha_recorder, api_key):
    # A minimal OIDC discovery endpoint so the login/logout plumbing is
    # exercisable without an IdP: authlib fetches this document before
    # redirecting.  This is NOT an IdP; the full browser dance is exercised
    # by the NixOS VM test with kanidm.
    class DiscoveryHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            document = json.dumps(
                {
                    "issuer": "https://idp.example.com",
                    "authorization_endpoint": "https://idp.example.com/authorize",
                    "token_endpoint": "https://idp.example.com/token",
                    "userinfo_endpoint": "https://idp.example.com/userinfo",
                    "end_session_endpoint": "https://idp.example.com/logout",
                    "jwks_uri": "https://idp.example.com/jwks",
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(document)))
            self.end_headers()
            self.wfile.write(document)

        def log_message(self, format: str, *args: object) -> None:
            pass

    discovery = ThreadingHTTPServer(("127.0.0.1", 0), DiscoveryHandler)
    discovery.daemon_threads = True
    threading.Thread(
        target=discovery.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    ).start()

    key_file, key = api_key
    config = make_config(
        tmp_path,
        key_file,
        ha_webhook_url=ha_recorder[0],
        oidc_discovery_url=(
            f"http://127.0.0.1:{discovery.server_address[1]}/.well-known/openid-configuration"
        ),
    )
    application = create_app(config)
    application.config["TESTING"] = True
    yield application, key
    discovery.shutdown()
    discovery.server_close()


@pytest.fixture
def client(app):
    application, _key = app
    return application.test_client()


@pytest.fixture
def authed_client(app):
    """A test client with a session simulating a user holding the role."""
    application, _key = app
    return authed(application)


@pytest.fixture
def unroled_client(app):
    """A logged-in user without the required role."""
    application, _key = app
    return authed(application, roles=())


def seed_push(app, kind: str = "snapshot"):
    """Push an event through the real webhook route; returns the API key."""
    application, key = app
    body = snapshot_body() if kind == "snapshot" else alarm_body()
    response = application.test_client().post(
        "/api/webhook",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    assert response.status_code == 200
    return key


def backdate(app, event_id: int, day: date, hour: int = 12) -> None:
    """Move a seeded event to ``hour`` o'clock host-local on ``day``.

    Mid-day keeps the local-date bucketing unambiguous across DST
    transitions and midnight edges.
    """
    application, _key = app
    when = datetime.combine(day, time(hour)).astimezone(UTC)
    with application.app_context():
        event = db.session.get(Event, event_id)
        assert event is not None
        event.received_at = when
        db.session.commit()


def day_links(html: str) -> set[str]:
    """The days the previous/next pager links to."""
    return set(re.findall(r"[?&]day=(\d{4}-\d{2}-\d{2})", html))


def get_csrf_token(client) -> str:
    with client.session_transaction() as sess:
        if "csrf_token" not in sess:
            sess["csrf_token"] = secrets.token_hex(32)
        csrf_secret = sess["csrf_token"]
    secret_key = client.application.secret_key
    assert secret_key is not None
    s = URLSafeTimedSerializer(secret_key, salt="wtf-csrf-token")
    return s.dumps(csrf_secret)


# ---------------------------------------------------------------------------
# access control
# ---------------------------------------------------------------------------
def test_unauthenticated_events_redirects_to_login(client) -> None:
    response = client.get("/events")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_unauthenticated_root_redirects_to_login(client) -> None:
    response = client.get("/")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_events_require_role(unroled_client) -> None:
    response = unroled_client.get("/events")
    assert response.status_code == 403
    assert "Permission denied" in response.get_data(as_text=True)


def test_authed_events_renders(app, authed_client) -> None:
    seed_push(app, "snapshot")
    response = authed_client.get("/events")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Webhook events" in html
    assert "snapshot" in html
    assert "CH2" in html
    # the list shows the per-camera stills; the AI snaps live on the
    # detail page (checked in the capture tests, which configure stills)
    assert "/snaps/" not in html


def test_event_detail_shows_payload_and_images(app, authed_client) -> None:
    seed_push(app, "snapshot")
    response = authed_client.get("/events/1")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "person_gender" in html  # redacted payload
    assert "<img" in html
    assert FAKE_JPEG_B64 not in html  # base64 never rendered
    # the payload is pretty-printed (stored compact, rendered indented)
    match = re.search(r'(?s)<pre class="payload">(.*?)</pre>', html)
    assert match, "payload block missing"
    assert "\n" in match.group(1), "payload not pretty-printed"


def test_event_detail_unknown_id_404(app, authed_client) -> None:
    seed_push(app, "alarm")
    response = authed_client.get("/events/999")
    assert response.status_code == 404
    assert "Not found" in response.get_data(as_text=True)


def test_empty_events_page(authed_client) -> None:
    response = authed_client.get("/events")
    assert response.status_code == 200
    assert "No events for this day" in response.get_data(as_text=True)


def seed_events_on_three_days(app):
    """Events today (id 3), yesterday (id 2), and 3 days ago (id 1)."""
    seed_push(app, "alarm")  # id 1
    seed_push(app, "snapshot")  # id 2
    seed_push(app, "snapshot")  # id 3
    today = date.today()
    backdate(app, 1, today - timedelta(days=3))
    backdate(app, 2, today - timedelta(days=1))
    return today, today - timedelta(days=3), today - timedelta(days=1)


def test_events_page_shows_only_one_day(app, authed_client) -> None:
    today, oldest, _yesterday = seed_events_on_three_days(app)
    # default: the newest day holding events, and only its events
    html = authed_client.get("/events").get_data(as_text=True)
    assert today.isoformat() in html
    assert "/events/3" in html
    assert "/events/1" not in html
    assert "/events/2" not in html
    # an explicit ?day= shows only that day's events
    html = authed_client.get(f"/events?day={oldest.isoformat()}").get_data(as_text=True)
    assert "/events/1" in html
    assert "/events/2" not in html
    assert "/events/3" not in html


def test_events_day_navigation_skips_empty_days(app, authed_client) -> None:
    today, oldest, yesterday = seed_events_on_three_days(app)
    two_days_ago = today - timedelta(days=2)

    # newest day: previous -> yesterday, no next (nothing newer exists)
    html = authed_client.get("/events").get_data(as_text=True)
    assert day_links(html) == {yesterday.isoformat()}

    # a day in between with no events still pages to the nearest ones
    html = authed_client.get(f"/events?day={two_days_ago.isoformat()}").get_data(as_text=True)
    assert "No events for this day" in html
    assert day_links(html) == {oldest.isoformat(), yesterday.isoformat()}

    # oldest day: no previous, next -> yesterday
    html = authed_client.get(f"/events?day={oldest.isoformat()}").get_data(as_text=True)
    assert day_links(html) == {yesterday.isoformat()}


def test_events_malformed_day_falls_back_to_newest(app, authed_client) -> None:
    seed_push(app, "snapshot")  # id 1
    seed_push(app, "alarm")  # id 2
    backdate(app, 1, date.today() - timedelta(days=1))
    html = authed_client.get("/events?day=not-a-date").get_data(as_text=True)
    assert "/events/2" in html
    assert "/events/1" not in html


def test_event_images_are_served(app, authed_client) -> None:
    seed_push(app, "snapshot")
    html = authed_client.get("/events/1").get_data(as_text=True)
    names = set(re.findall(r"/snaps/(snap_[0-9a-f]{32}_\w+\.jpg)", html))
    assert len(names) == 2
    for name in names:
        response = authed_client.get(f"/snaps/{name}")
        assert response.status_code == 200
        assert response.data == FAKE_JPEG


def test_kind_badge_classes(app, authed_client) -> None:
    seed_push(app, "alarm")
    html = authed_client.get("/events").get_data(as_text=True)
    assert "kind-alarm" in html


# ---------------------------------------------------------------------------
# login/logout plumbing (session mechanics; the IdP dance is VM-tested)
# ---------------------------------------------------------------------------
def test_login_redirects_to_idp(client) -> None:
    response = client.get("/login", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["Location"].startswith("https://idp.example.com/authorize")


def test_logout_requires_csrf(authed_client) -> None:
    response = authed_client.post("/logout")
    # flask-wtf rejects the missing token
    assert response.status_code in (400, 302)


def test_logout_with_csrf_clears_session(authed_client) -> None:
    csrf_token = get_csrf_token(authed_client)
    response = authed_client.post("/logout", data={"csrf_token": csrf_token})
    assert response.status_code == 302
    # the stub IdP advertises an end_session_endpoint, so logout redirects
    # there with the post-logout URL
    assert "idp.example.com/logout" in response.headers["Location"]
    # session cleared: /events now redirects to login again
    response = authed_client.get("/events")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_snaps_and_api_stay_cookie_free(client, app) -> None:
    """The API-key webhook route must not require CSRF or a session."""
    _application, key = app
    # without a CSRF token or session: still works (API key only)
    response = client.post(
        "/api/webhook",
        data=json.dumps(alarm_body()).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    assert response.status_code == 200
    # and unauthenticated stays 401 (no session fallback)
    assert client.post("/api/webhook", json={}).status_code == 401


def test_security_headers_on_html(client) -> None:
    response = client.get("/logged-out")
    assert response.headers["Content-Security-Policy"].startswith("default-src 'none'")
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "same-origin"
    assert response.headers["Cache-Control"] == "no-store"


def test_session_cookie_flags(authed_client) -> None:
    response = authed_client.get("/events")
    cookie = response.headers.get("Set-Cookie", "")
    # the session cookie set earlier in the fixture already exists; if a
    # new one is set it must carry the hardening flags
    if cookie:
        assert "HttpOnly" in cookie
        assert "Secure" in cookie


def test_events_page_times_show_seconds(app, authed_client) -> None:
    """Received times render with seconds, without a timezone suffix."""
    seed_push(app, "alarm")
    html = authed_client.get("/events").get_data(as_text=True)
    # e.g. "2026-11-09 13:05:33"
    assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?! [A-Z])", html)
    detail = authed_client.get("/events/1").get_data(as_text=True)
    assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?! [A-Z])", detail)
