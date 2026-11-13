"""Tests for instant capture (go2rtc URLs), event groups, and the group page."""

import json
import secrets
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from conftest import (
    FAKE_JPEG,
    PUBLIC_URL,
    alarm_body,
    authed,
    make_config,
    push,
    snapshot_body,
    wait_for,
)
from nvr_core import create_app
from nvr_core.auth import _safe_next
from nvr_core.models import Event, db

NOT_JPEG = b"this is not a jpeg"


class StubSources:
    """JPEG URL stubs standing in for go2rtc: ok / not-a-jpeg / slow."""

    def __init__(self) -> None:
        self.slow_delay = 30.0
        # base64 credentials in the URL exercise the redaction path
        self.urls: dict[str, bytes | None] = {}
        self.hits: dict[str, int] = {}

        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:
                outer.hits[self.path] = outer.hits.get(self.path, 0) + 1
                if self.path == "/slow.jpg":
                    time.sleep(outer.slow_delay)
                body = outer.urls.get(self.path, FAKE_JPEG)
                if body is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        ).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def sources():
    stubs = StubSources()
    yield stubs
    stubs.stop()


def make_app(
    tmp_path: Path,
    ha_url: str,
    snapshot_urls: list[str],
    key: str,
    group_window_s: int = 60,
):
    key_file = tmp_path / "api_key"
    key_file.write_text(key)
    application = create_app(
        make_config(
            tmp_path,
            key_file,
            ha_webhook_url=ha_url,
            snapshot_urls=snapshot_urls,
            group_window_s=group_window_s,
        )
    )
    application.config["TESTING"] = True
    return application


@pytest.fixture
def core_with_sources(tmp_path: Path, ha_recorder, sources):
    ha_url, recording = ha_recorder
    key = secrets.token_urlsafe(32)
    urls = [f"{sources.base}/ok0.jpg", f"{sources.base}/ok1.jpg"]
    sources.urls = {"/ok0.jpg": FAKE_JPEG, "/ok1.jpg": FAKE_JPEG}
    application = make_app(tmp_path, ha_url, urls, key)
    client = application.test_client()
    yield application, client, recording, sources, key


# ---------------------------------------------------------------------------
# instant capture + alarm enrichment
# ---------------------------------------------------------------------------
def test_alarm_captures_all_urls(core_with_sources) -> None:
    app, client, _recording, _sources, key = core_with_sources
    started = time.monotonic()
    assert push(client, alarm_body(), key).status_code == 200
    assert time.monotonic() - started < 3.0  # bounded, never stalls the relay

    with app.app_context():
        event = db.session.execute(db.select(Event)).scalar_one()
        names = event.live_snaps_parsed
        assert len(names) == 2
        assert all(n.startswith(f"snap_{event.group_token}_live") for n in names)
        assert event.kind == "alarm"

    # the events list shows the captures in its Images column
    page = authed(app)
    html = page.get("/events").get_data(as_text=True)
    for name in names:
        assert f"/snaps/{name}" in html
    # ... and the event detail page shows the same stills
    detail = page.get("/events/1").get_data(as_text=True)
    for name in names:
        assert f"/snaps/{name}" in detail


def test_forwarded_alarm_carries_notification_image(core_with_sources) -> None:
    app, client, recording, _sources, key = core_with_sources
    assert push(client, alarm_body(), key).status_code == 200
    wait_for(lambda: len(recording.bodies()) == 1)
    forwarded = json.loads(recording.bodies()[0])
    bridge = forwarded["nvr_bridge"]
    with app.app_context():
        event = db.session.execute(db.select(Event)).scalar_one()
    assert bridge["group"] == event.group_token
    assert bridge["url"] == f"{PUBLIC_URL}/events/group/{event.group_token}"
    # the FIRST url's capture is the notification image
    assert bridge["image_url"] == f"{PUBLIC_URL}/snaps/{event.live_snaps_parsed[0]}"
    assert bridge["image_urls"] == [f"{PUBLIC_URL}/snaps/{n}" for n in event.live_snaps_parsed]


def test_failed_source_falls_back_to_next_url(tmp_path, ha_recorder, sources) -> None:
    ha_url, recording = ha_recorder
    key = secrets.token_urlsafe(32)
    sources.urls = {"/ok0.jpg": None, "/ok1.jpg": FAKE_JPEG}  # first 404s
    app = make_app(tmp_path, ha_url, [f"{sources.base}/ok0.jpg", f"{sources.base}/ok1.jpg"], key)
    client = app.test_client()
    assert push(client, alarm_body(), key).status_code == 200
    wait_for(lambda: len(recording.bodies()) == 1)
    bridge = json.loads(recording.bodies()[0])["nvr_bridge"]
    assert bridge["image_url"].endswith("_live1.jpg")  # the second source


def test_non_jpeg_and_slow_sources_skipped(tmp_path, ha_recorder, sources) -> None:
    ha_url, _recording = ha_recorder
    key = secrets.token_urlsafe(32)
    sources.urls = {"/bad.jpg": NOT_JPEG, "/slow.jpg": FAKE_JPEG}
    started = time.monotonic()
    app = make_app(
        tmp_path,
        ha_url,
        [f"{sources.base}/bad.jpg", f"{sources.base}/slow.jpg"],
        key,
    )
    client = app.test_client()
    assert push(client, alarm_body(), key).status_code == 200
    elapsed = time.monotonic() - started
    assert elapsed < 5.0, f"capture must stay inside the relay timeout ({elapsed:.1f}s)"
    with app.app_context():
        event = db.session.execute(db.select(Event)).scalar_one()
    assert event.live_snaps_parsed == []  # both skipped
    assert sources.hits["/slow.jpg"] == 1  # attempted, not hung forever


def test_alarm_without_urls_has_no_image(tmp_path, ha_recorder) -> None:
    ha_url, recording = ha_recorder
    key = secrets.token_urlsafe(32)
    app = make_app(tmp_path, ha_url, [], key)
    client = app.test_client()
    assert push(client, alarm_body(), key).status_code == 200
    wait_for(lambda: len(recording.bodies()) == 1)
    bridge = json.loads(recording.bodies()[0])["nvr_bridge"]
    assert bridge["group"]
    assert "image_url" not in bridge


# ---------------------------------------------------------------------------
# alarm -> snapshot group correlation
# ---------------------------------------------------------------------------
def test_snapshot_joins_alarm_group(core_with_sources) -> None:
    app, client, _recording, _sources, key = core_with_sources
    assert push(client, alarm_body(), key).status_code == 200
    assert push(client, snapshot_body(), key).status_code == 200
    with app.app_context():
        events = list(db.session.execute(db.select(Event).order_by(Event.id)).scalars())
    assert len(events) == 2
    assert events[0].kind == "alarm"
    assert events[1].kind == "snapshot"
    assert events[1].group_token == events[0].group_token

    # the snapshot event's detail page shows the stills captured at alarm
    # time (resolved through the group), with the AI images under the
    # payload heading
    page = authed(app)
    detail = page.get("/events/2").get_data(as_text=True)
    assert "_live0.jpg" in detail and "_live1.jpg" in detail
    assert "_object.jpg" in detail
    payload_at = detail.find("Payload")
    object_at = detail.find("_object.jpg")
    assert 0 < payload_at < object_at, "images must render under the payload"


def test_snapshot_after_window_starts_new_group(tmp_path, ha_recorder, sources) -> None:
    ha_url, _recording = ha_recorder
    key = secrets.token_urlsafe(32)
    sources.urls = {}
    app = make_app(tmp_path, ha_url, [], key, group_window_s=60)
    client = app.test_client()
    assert push(client, alarm_body(), key).status_code == 200
    with app.app_context():
        alarm = db.session.execute(db.select(Event)).scalar_one()
        alarm.received_at = datetime.now(UTC) - timedelta(seconds=120)
        db.session.commit()
    assert push(client, snapshot_body(), key).status_code == 200
    with app.app_context():
        events = list(db.session.execute(db.select(Event).order_by(Event.id)).scalars())
    assert events[1].group_token != events[0].group_token


def test_snapshot_captures_own_stills(core_with_sources) -> None:
    """Snapshot events capture stills too (fresh token), so their rows
    show imagery even without a preceding alarm."""
    app, client, _recording, _sources, key = core_with_sources
    assert push(client, snapshot_body(), key).status_code == 200
    with app.app_context():
        event = db.session.execute(db.select(Event)).scalar_one()
        stills = event.live_snaps_parsed
        assert len(stills) == 2
        # fresh token: the names are this event's own, not a group's
        assert all(not n.startswith(f"snap_{event.group_token}_") for n in stills)
    # the main page Images column shows them
    html = authed(app).get("/events").get_data(as_text=True)
    for name in stills:
        assert f"/snaps/{name}" in html


def test_snapshot_without_alarm_gets_own_group(tmp_path, ha_recorder) -> None:
    ha_url, _recording = ha_recorder
    key = secrets.token_urlsafe(32)
    app = make_app(tmp_path, ha_url, [], key)
    client = app.test_client()
    assert push(client, snapshot_body(), key).status_code == 200
    with app.app_context():
        event = db.session.execute(db.select(Event)).scalar_one()
    assert event.group_token  # still grouped: notifications can link to it


# ---------------------------------------------------------------------------
# group page + SSO redirect-back
# ---------------------------------------------------------------------------
def test_group_page_shows_all_images(core_with_sources) -> None:
    app, client, _recording, _sources, key = core_with_sources
    push(client, alarm_body(), key)
    push(client, snapshot_body(), key)
    with app.app_context():
        first = db.session.execute(db.select(Event).order_by(Event.id)).scalars().first()
        assert first is not None
        group = first.group_token
    page = authed(app)
    response = page.get(f"/events/group/{group}")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "_live0.jpg" in html and "_live1.jpg" in html  # go2rtc captures
    assert "_object.jpg" in html and "_background.jpg" in html  # AI snaps
    # every referenced image is served
    import re

    for name in set(re.findall(r"/snaps/(snap_[^\"']+\.jpg)", html)):
        assert page.get(f"/snaps/{name}").status_code == 200


def test_notification_images_need_no_sso(core_with_sources) -> None:
    """Phones fetch notification images without a session: the snap URLs
    (instant captures and AI snapshots) are capability URLs and must stay
    readable anonymously -- only the pages are SSO-protected."""
    app, client, _recording, _sources, key = core_with_sources
    push(client, alarm_body(), key)
    push(client, snapshot_body(), key)

    anonymous = app.test_client()  # no session, no cookies
    assert anonymous.get("/events").status_code == 302  # pages: SSO
    assert anonymous.get("/events/group/whatever").status_code == 302

    with app.app_context():
        events = list(db.session.execute(db.select(Event).order_by(Event.id)).scalars())
    live = events[0].live_snaps_parsed[0]
    assert anonymous.get(f"/snaps/{live}").status_code == 200  # images: open
    assert anonymous.get(f"/snaps/{events[1].object_snap}").status_code == 200
    assert anonymous.get("/snaps/snap_08588_CH2_object.jpg").status_code == 404


def test_group_page_unknown_token_404(core_with_sources) -> None:
    app, _client, _recording, _sources, _key = core_with_sources
    response = authed(app).get("/events/group/nosuchtoken")
    assert response.status_code == 404


def test_unauthenticated_group_page_stashes_next(core_with_sources) -> None:
    app, _client, _recording, _sources, _key = core_with_sources
    client = app.test_client()
    response = client.get("/events/group/abc123")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]
    with client.session_transaction() as sess:
        assert sess["login_next"] == "/events/group/abc123"


def test_safe_next_rejects_open_redirects() -> None:
    assert _safe_next("/events/group/abc") == "/events/group/abc"
    assert _safe_next("/events?x=1") == "/events?x=1"
    assert _safe_next("https://evil.example.com") == "/events"
    assert _safe_next("//evil.example.com") == "/events"
    assert _safe_next("") == "/events"
    assert _safe_next(None) == "/events"


# ---------------------------------------------------------------------------
# flattened nvr_bridge notification fields (pre-rendered for HA)
# ---------------------------------------------------------------------------
def test_forwarded_bridge_block_is_notification_ready(core_with_sources) -> None:
    """Both pushes carry ready-made title/message/image: the HA automation
    needs no inter-variable templates (HA renders variables in an order
    that broke those)."""

    _app, client, recording, _sources, key = core_with_sources
    push(client, alarm_body(), key)
    wait_for(lambda: len(recording.bodies()) >= 1)
    alarm_bridge = json.loads(recording.bodies()[0])["nvr_bridge"]
    assert alarm_bridge["title"] == "Motion on CH2"
    assert alarm_bridge["message"] == "Motion detected on CH2"
    assert alarm_bridge["image_url"].endswith("_live0.jpg")
    assert alarm_bridge["url"].endswith(f"/events/group/{alarm_bridge['group']}")

    push(client, snapshot_body(), key)
    wait_for(lambda: len(recording.bodies()) >= 2)
    snap_bridge = json.loads(recording.bodies()[1])["nvr_bridge"]
    # same group -> same notification tag -> the image updates in place
    assert snap_bridge["group"] == alarm_bridge["group"]
    assert snap_bridge["title"] == "Person on CH2"
    assert "Adult" in snap_bridge["message"] and "Female" in snap_bridge["message"]
    # the AI object image wins over the instant still on snapshot pushes
    assert snap_bridge["image_url"].endswith("_object.jpg")
    assert snap_bridge["background_url"].endswith("_background.jpg")


def test_person_message_extras() -> None:
    from nvr_core.payload import person_message, person_title

    assert (
        person_message(
            {
                "person_age": "Adult",
                "person_gender": "Female",
                "person_hat": "Yes",
                "person_bag": "Backpack",
            }
        )
        == "Adult Female, hat, Backpack"
    )
    assert person_message({"person_hat": "No"}) == "Person"
    assert person_message({}) == "Person"
    assert person_title({"StrChn": "CH1"}) == "Person on CH1"
    assert person_title({}, fallback_channel="CH2") == "Person on CH2"
    assert person_title({}) == "Person on camera"
