"""Tests for the nvr_bridge core service (Flask test client).

The fake Home Assistant receiver runs in-process on an ephemeral localhost
port -- no external services.  Push bodies mirror captured device pushes.
"""

import base64
import json
import os
import re
import secrets
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import (
    FAKE_JPEG,
    FAKE_JPEG_B64,
    PUBLIC_URL,
    TEST_SECRET_KEY,
    alarm_body,
    make_config,
    push,
    snapshot_body,
    wait_for,
)
from nvr_common import MAX_BODY_BYTES, HttpForwarder
from nvr_core import create_app
from nvr_core.config import Config, ConfigError, load_config
from nvr_core.models import Event

NOT_JPEG_B64 = base64.b64encode(b"this is not a jpeg at all").decode()


def keepalive_body() -> dict:
    return {"result": "success", "data": {}}


@pytest.fixture
def core(tmp_path: Path, ha_recorder, api_key):
    """The core app (test client) forwarding to a fake HA webhook."""
    ha_url, recorder = ha_recorder
    key_file, key = api_key
    cfg = make_config(tmp_path, key_file, ha_webhook_url=ha_url)
    app = create_app(cfg)
    yield app.test_client(), app, cfg, recorder, key


def stored_names(cfg: Config, suffix: str) -> list[str]:
    return sorted(path.name for path in cfg.snaps_dir.glob(f"snap_*_{suffix}.jpg"))


# ---------------------------------------------------------------------------
# config loading
# ---------------------------------------------------------------------------
def write_config(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data))
    return path


def write_secrets(tmp_path: Path) -> dict:
    secret_key_file = tmp_path / "secret_key"
    secret_key_file.write_text(TEST_SECRET_KEY)
    oidc_secret_file = tmp_path / "oidc_secret"
    oidc_secret_file.write_text("test-oidc-client-secret")
    webhook_id_file = tmp_path / "webhook_id"
    webhook_id_file.write_text("test-webhook-id-0123456789abcdef")
    return {
        "SECRET_KEY_FILE": str(secret_key_file),
        "OIDC_CLIENT_SECRET_FILE": str(oidc_secret_file),
        "HA_WEBHOOK_ID_FILE": str(webhook_id_file),
    }


def minimal_config(tmp_path: Path) -> dict:
    return {
        "API_KEY_FILE": str(tmp_path / "key"),
        "OIDC_CLIENT_ID": "nvr_bridge",
        "OIDC_DISCOVERY_URL": "https://idp.example.com/.well-known/openid-configuration",
        "PUBLIC_URL": "https://nvr.example.com",
        "ROLES_PATH": ["nvr_roles"],
        "HA_WEBHOOK_URL": "http://homeassistant:8123/api/webhook",
        **write_secrets(tmp_path),
    }


def test_load_config_defaults(tmp_path: Path) -> None:
    cfg = load_config(write_config(tmp_path, minimal_config(tmp_path)))
    assert (
        cfg.ha_webhook_url
        == "http://homeassistant:8123/api/webhook/test-webhook-id-0123456789abcdef"
    )
    assert cfg.snaps_dir == Path("snaps")
    assert cfg.url_retention_days == 1
    assert cfg.event_retention_days == 3650
    assert cfg.log_level == "INFO"
    assert cfg.database_url == "sqlite:////var/lib/nvr_bridge/events.db"
    assert cfg.user_role == "nvr_viewers"
    assert cfg.session_cookie_samesite == "Lax"
    assert cfg.oidc_client_secret == "test-oidc-client-secret"
    assert cfg.secret_key == TEST_SECRET_KEY


def test_load_config_full(tmp_path: Path) -> None:
    data = minimal_config(tmp_path)
    data.update(
        {
            "HA_WEBHOOK_URL": "http://homeassistant:8123/api/webhook/",
            "SNAPS_DIR": "/var/lib/nvr_bridge/snaps",
            "URL_RETENTION_DAYS": 7,
            "EVENT_RETENTION_DAYS": 0,
            "DATABASE_URL": "sqlite:////elsewhere/events.db",
            "USER_ROLE": "camera_viewers",
            "SESSION_COOKIE_SAMESITE": "Strict",
            "LOG_LEVEL": "debug",
        }
    )
    cfg = load_config(write_config(tmp_path, data))
    assert cfg.api_key_file == Path(str(tmp_path / "key"))
    assert cfg.oidc_client_id == "nvr_bridge"
    assert cfg.oidc_discovery_url == ("https://idp.example.com/.well-known/openid-configuration")
    assert cfg.public_url == "https://nvr.example.com"
    assert cfg.roles_path == ["nvr_roles"]
    assert cfg.ha_webhook_url == (
        "http://homeassistant:8123/api/webhook/test-webhook-id-0123456789abcdef"
    )
    assert cfg.snaps_dir == Path("/var/lib/nvr_bridge/snaps")
    assert cfg.url_retention_days == 7
    assert cfg.event_retention_days == 0
    assert cfg.database_url == "sqlite:////elsewhere/events.db"
    assert cfg.user_role == "camera_viewers"
    assert cfg.session_cookie_samesite == "Strict"
    assert cfg.log_level == "DEBUG"  # case-normalised


@pytest.mark.parametrize(
    "data",
    [
        {},  # the required keys are missing
        {"API_KEY_FILE": ""},
        {"API_KEY_FILE": 5},
        # secrets must exist as files, not inline values
        {"SECRET_KEY_FILE": "/nonexistent/secret"},
        {"OIDC_CLIENT_SECRET_FILE": "/nonexistent/secret"},
        {"OIDC_DISCOVERY_URL": "http://idp.example.com/.well-known/openid-configuration"},
        {"OIDC_DISCOVERY_URL": "not a url"},
        {"PUBLIC_URL": "nvr.example.com"},
        {"ROLES_PATH": "nvr_roles"},
        {"ROLES_PATH": []},
        {"ROLES_PATH": [5]},
        {"SESSION_COOKIE_SAMESITE": "None"},
        {"URL_RETENTION_DAYS": -1},
        {"URL_RETENTION_DAYS": "14"},
        {"URL_RETENTION_DAYS": True},
        {"EVENT_RETENTION_DAYS": -1},
        {"EVENT_RETENTION_DAYS": "14"},
        {"EVENT_RETENTION_DAYS": True},
        {"HA_WEBHOOK_URL": "notaurl"},
        {"HA_WEBHOOK_URL": "ftp://ha:8123/hook"},
        {"HA_WEBHOOK_URL": None},
        # the webhook id must exist as a file
        {"HA_WEBHOOK_ID_FILE": "/nonexistent/webhook_id"},
        {"HA_WEBHOOK_ID_FILE": 5},
        {"SNAPS_DIR": None},
        {"LOG_LEVEL": "LOUD"},
    ],
)
def test_load_config_rejects_bad_values(tmp_path: Path, data: dict) -> None:
    # the empty dict is tested bare: every required key missing
    merged = data if not data else {**minimal_config(tmp_path), **data}
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, merged))


def test_short_secret_key_rejected(tmp_path: Path) -> None:
    data = minimal_config(tmp_path)
    (tmp_path / "secret_key").write_text("too short")
    with pytest.raises(ConfigError, match="at least 32"):
        load_config(write_config(tmp_path, data))


def test_webhook_id_file_stripped_and_appended(tmp_path: Path) -> None:
    """A secret file's trailing newline is fine; the id joins the base URL once."""
    data = minimal_config(tmp_path)
    (tmp_path / "webhook_id").write_text("  secret-webhook-id-abc123 \n")
    cfg = load_config(write_config(tmp_path, data))
    assert cfg.ha_webhook_url == "http://homeassistant:8123/api/webhook/secret-webhook-id-abc123"


@pytest.mark.parametrize(
    "contents",
    [
        "",  # empty file
        "   \n",  # whitespace only
        "two\nlines",  # a secret file holds one value
        "has space",  # whitespace cannot appear in a URL path segment
        "has/slash",
        "has?query",
        "has#fragment",
    ],
)
def test_bad_webhook_id_file_rejected(tmp_path: Path, contents: str) -> None:
    data = minimal_config(tmp_path)
    (tmp_path / "webhook_id").write_text(contents)
    with pytest.raises(ConfigError, match="HA_WEBHOOK_ID_FILE"):
        load_config(write_config(tmp_path, data))


def test_short_api_key_rejected_at_startup(tmp_path: Path) -> None:
    key_file = tmp_path / "key"
    key_file.write_text("too short")
    cfg = make_config(tmp_path, key_file)
    with pytest.raises(Exception, match="at least 32"):
        create_app(cfg)


# ---------------------------------------------------------------------------
# API key authentication
# ---------------------------------------------------------------------------
def test_push_without_key_is_unauthorized(core) -> None:
    client, _app, _cfg, recorder, _key = core
    for headers in (
        {},  # no header at all
        {"Authorization": "Bearer wrong-key-wrong-key-wrong-key-wrong"},
        {"Authorization": "wrong-key-wrong-key-wrong-key-wrong-key-wr"},  # no Bearer
        {"Authorization": f"Basic {secrets.token_urlsafe(32)}"},
    ):
        response = client.post("/api/webhook", json=alarm_body(), headers=headers)
        assert response.status_code == 401, headers
        assert response.get_json() == {"error": "unauthorized"}
    assert recorder.count() == 0


def test_only_the_webhook_route_is_authenticated(core) -> None:
    """Nothing but POST /api/webhook accepts pushes; other paths stay closed."""
    client, _app, _cfg, _recorder, key = core
    for path in ("/", "/API/AlarmEvent/EventPush"):
        response = push(client, alarm_body(), key, path=path)
        # "/" is the web UI root (GET-only -> 405); unknown paths 404
        expected = 405 if path == "/" else 404
        assert response.status_code == expected, path
    # a known viewer route exists but is GET-only: wrong method, not 404
    response = push(client, alarm_body(), key, path="/snaps/x.jpg")
    assert response.status_code == 405
    # GET on the webhook route is a wrong method too
    response = client.get("/api/webhook", headers={"Authorization": f"Bearer {key}"})
    assert response.status_code == 405


# ---------------------------------------------------------------------------
# push processing / HA forwarding
# ---------------------------------------------------------------------------
def test_keepalive_acknowledged_not_forwarded(core) -> None:
    client, _app, _cfg, recorder, key = core
    # the device's KeepLive path reaches the core only via the gateway's
    # relay target; the core itself has exactly one push route
    response = push(client, keepalive_body(), key)
    assert response.status_code == 200
    assert response.get_json() == {"status": "received", "kind": "keepalive"}
    # heartbeats carry no information: they are answered but never forwarded
    assert recorder.count() == 0


def test_alarm_forwarded_enriched(core) -> None:
    client, _app, _cfg, recorder, key = core
    response = push(client, alarm_body(), key)
    assert response.status_code == 200
    assert response.get_json()["kind"] == "alarm"
    wait_for(lambda: recorder.count() == 1)
    forwarded = json.loads(recorder.bodies()[0])
    # the original payload is untouched except for the added bridge block
    bridge = forwarded.pop("nvr_bridge")
    assert forwarded == alarm_body()
    assert bridge["group"]
    assert "image_url" not in bridge  # no SNAPSHOT_URLS configured


def test_garbage_push_still_returns_200(core) -> None:
    client, _app, _cfg, recorder, key = core
    response = push(client, b"not json at all", key)
    assert response.status_code == 200
    assert response.get_json() == {"status": "received", "kind": None}
    wait_for(lambda: recorder.count() == 1)
    assert recorder.bodies()[0] == b"not json at all"  # passed through untouched


def test_oversized_body_rejected_and_not_forwarded(core) -> None:
    """Bodies over MAX_BODY_BYTES get a 413 without being forwarded."""
    client, _app, _cfg, recorder, key = core
    # declared Content-Length above the limit: rejected before the view runs
    response = client.post(
        "/api/webhook",
        data=b"x" * (MAX_BODY_BYTES + 2),
        headers={"Authorization": f"Bearer {key}"},
    )
    assert response.status_code == 413
    assert response.get_json() == {"error": "body too large"}
    # exactly one byte over the cap: caught by the view's own check
    response = client.post(
        "/api/webhook",
        data=b"x" * (MAX_BODY_BYTES + 1),
        headers={"Authorization": f"Bearer {key}"},
    )
    assert response.status_code == 413
    assert recorder.count() == 0


def test_snapshot_saved_served_and_enriched(core) -> None:
    client, _app, cfg, recorder, key = core
    response = push(client, snapshot_body(), key)
    assert response.status_code == 200
    assert response.get_json()["kind"] == "snapshot"

    # both images written under unguessable names (stored before the 200)
    object_name = stored_names(cfg, "object")[0]
    background_name = stored_names(cfg, "background")[0]
    assert (cfg.snaps_dir / object_name).read_bytes() == FAKE_JPEG
    assert (cfg.snaps_dir / background_name).read_bytes() == FAKE_JPEG

    # served back over HTTP
    response = client.get(f"/snaps/{object_name}")
    assert response.status_code == 200
    assert response.data == FAKE_JPEG
    assert response.headers["Content-Type"] == "image/jpeg"

    # forwarded payload enriched with absolute URLs the phones can fetch
    wait_for(lambda: recorder.count() == 1)
    forwarded = json.loads(recorder.bodies()[0])
    fsnap = forwarded["data"]["ai_snap_picture"]["SnapedObjInfo"][0]
    assert fsnap["object_url"] == f"{PUBLIC_URL}/snaps/{object_name}"
    assert fsnap["background_url"] == f"{PUBLIC_URL}/snaps/{background_name}"
    assert fsnap["ObjectImage"]  # original payload otherwise untouched
    assert fsnap["person_gender"] == "Female"  # attributes survive for automations


def test_non_jpeg_image_skipped_but_push_forwarded(core) -> None:
    client, _app, cfg, recorder, key = core
    response = push(client, snapshot_body(object_b64=NOT_JPEG_B64), key)
    assert response.status_code == 200
    assert response.get_json()["kind"] == "snapshot"
    # bad object image skipped, good background saved
    assert not stored_names(cfg, "object")
    assert stored_names(cfg, "background")
    # forwarded payload has no object_url for that entry
    wait_for(lambda: recorder.count() == 1)
    snap = json.loads(recorder.bodies()[0])["data"]["ai_snap_picture"]["SnapedObjInfo"][0]
    assert "object_url" not in snap
    assert snap["background_url"].endswith("background.jpg")


def test_snapshot_names_are_unguessable(core) -> None:
    """Names must not leak or correlate device ids, channels, or each other."""
    client, _app, cfg, _recorder, key = core
    push(client, snapshot_body(snap_id_=7001), key)
    push(client, snapshot_body(snap_id_=7002), key)
    names = sorted(path.name for path in cfg.snaps_dir.glob("snap_*.jpg"))
    assert len(names) == 4  # object + background per event
    for name in names:
        assert re.fullmatch(r"snap_[0-9a-f]{32}_(object|background)\.jpg", name)
        # channel labels cannot appear: the token is hex-only
        assert "CH2" not in name
    # one fresh CSPRNG token per event, shared by its object/background pair
    # (not the device's sequential SnapId)
    tokens = {name.split("_")[1] for name in names}
    assert len(tokens) == 2
    assert tokens.isdisjoint({"7001", "7002"})


def test_snapshot_same_id_stored_twice(core) -> None:
    """Re-pushed events are stored again under fresh unguessable names."""
    client, _app, cfg, _recorder, key = core
    push(client, snapshot_body(snap_id_=7000), key)
    push(client, snapshot_body(snap_id_=7000), key)
    assert len(stored_names(cfg, "object")) == 2
    assert len(stored_names(cfg, "background")) == 2


def test_malformed_snapshot_still_acked_and_served(core) -> None:
    """Malformed device fields must never break the 200 or image serving."""
    client, _app, cfg, recorder, key = core
    body = snapshot_body()
    info = body["data"]["ai_snap_picture"]["SnapedObjInfo"][0]
    info["SnapId"] = "not-a-number"
    info["StartTime"] = "2026-11-02 21:55:51"  # the device's malformed ISO string
    response = push(client, body, key)
    assert response.status_code == 200
    assert response.get_json()["kind"] == "snapshot"
    # image still stored under the fallback (time-based) id and still served
    name = stored_names(cfg, "object")[0]
    wait_for(lambda: recorder.count() == 1)
    snap = json.loads(recorder.bodies()[0])["data"]["ai_snap_picture"]["SnapedObjInfo"][0]
    assert snap["background_url"].endswith(".jpg")
    response = client.get(f"/snaps/{name}")
    assert response.status_code == 200
    assert response.data == FAKE_JPEG


def test_forward_failure_does_not_break_push(tmp_path: Path, api_key) -> None:
    """An unreachable Home Assistant must not affect the 200 the gateway gets."""
    key_file, key = api_key
    cfg = make_config(tmp_path, key_file, ha_webhook_url="http://127.0.0.1:1/api/webhook/nvr")
    forwarder = HttpForwarder(cfg.ha_webhook_url, timeout=0.5, workers=1)
    client = create_app(cfg, forwarder=forwarder).test_client()
    try:
        response = push(client, alarm_body(), key)
        assert response.status_code == 200
        assert response.get_json()["kind"] == "alarm"
        wait_for(lambda: forwarder.state.last_forward_error is not None)
    finally:
        forwarder.shutdown()
    assert forwarder.state.last_forward_error is not None


def test_forwarder_queue_overflow_drops_push(tmp_path: Path, ha_recorder, api_key) -> None:
    ha_url, recorder = ha_recorder
    key_file, key = api_key
    forwarder = HttpForwarder(ha_url, maxsize=1, workers=0)
    cfg = make_config(tmp_path, key_file, ha_webhook_url=ha_url)
    client = create_app(cfg, forwarder=forwarder).test_client()
    try:
        assert push(client, alarm_body(), key).status_code == 200
        assert push(client, alarm_body(), key).status_code == 200  # dropped, not failed
        assert forwarder.state.last_forward_error == "forward queue full; push dropped"
    finally:
        forwarder.shutdown()
    assert recorder.count() == 0


# ---------------------------------------------------------------------------
# viewer routes
# ---------------------------------------------------------------------------
def test_viewer_serves_snaps_and_robots_only(core) -> None:
    client, _app, cfg, _recorder, key = core
    push(client, snapshot_body(), key)
    object_name = stored_names(cfg, "object")[0]

    response = client.get(f"/snaps/{object_name}")
    assert response.status_code == 200
    assert response.data == FAKE_JPEG
    assert response.headers["Content-Type"] == "image/jpeg"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Cache-Control"] == "no-store"

    response = client.get("/robots.txt")
    assert response.status_code == 200
    assert response.data == b"User-agent: *\nDisallow: /\n"
    assert response.headers["Content-Type"] == "text/plain"

    # no status page, no manifest, no health endpoint, no listing; the root
    # is the web UI and redirects to SSO login instead of 404ing
    assert client.get("/").status_code == 302
    for path in (
        "/healthz",
        "/latest.json",
        "/nope",
        "/snaps/",
        "/snaps/nothere.jpg",
        "/snaps/snap_08588_CH2_object.jpg",  # legacy sequential-style name
        "/snaps/../../etc/passwd",
        # an encoded NUL in the name must 404, not crash the handler
        "/snaps/%00evil.jpg",
    ):
        response = client.get(path)
        assert response.status_code == 404, path
        assert response.get_json() == {"error": "not found"}


def test_viewer_rejects_post(core) -> None:
    client, _app, cfg, _recorder, key = core
    push(client, snapshot_body(), key)
    object_name = stored_names(cfg, "object")[0]
    response = client.post(f"/snaps/{object_name}", json={})
    assert response.status_code == 405
    assert response.get_json() == {"error": "method not allowed"}
    response = client.post("/robots.txt")
    assert response.status_code == 405


# ---------------------------------------------------------------------------
# event recording
# ---------------------------------------------------------------------------
def all_events(app) -> list:
    with app.app_context():
        from nvr_core.models import db

        return list(db.session.execute(db.select(Event).order_by(Event.id)).scalars().all())


def test_alarm_recorded_as_event(core) -> None:
    client, app, _cfg, _recorder, key = core
    assert push(client, alarm_body(), key).status_code == 200
    events = all_events(app)
    assert len(events) == 1
    event = events[0]
    assert event.kind == "alarm"
    assert event.device_name == "testing"
    assert event.str_chn == "CH2"
    assert event.object_snap is None
    assert json.loads(event.payload)["data"]["alarm_list"]


def test_snapshot_recorded_with_snap_names(core) -> None:
    client, app, cfg, _recorder, key = core
    assert push(client, snapshot_body(), key).status_code == 200
    event = all_events(app)[0]
    assert event.kind == "snapshot"
    assert event.str_chn == "CH2"
    assert event.object_snap in [p.name for p in cfg.snaps_dir.glob("snap_*_object.jpg")]
    assert event.background_snap in [p.name for p in cfg.snaps_dir.glob("snap_*_background.jpg")]


def test_snapshot_payload_redacted_in_db(core) -> None:
    """The base64 JPEGs must never reach the database."""
    client, app, _cfg, _recorder, key = core
    assert push(client, snapshot_body(), key).status_code == 200
    payload = all_events(app)[0].payload
    assert FAKE_JPEG_B64 not in payload
    assert "<redacted:" in payload
    assert "person_gender" in payload  # the rest of the payload survives


def test_keepalive_not_recorded(core) -> None:
    client, app, _cfg, _recorder, key = core
    assert push(client, keepalive_body(), key).status_code == 200
    assert all_events(app) == []


def test_garbage_push_recorded_as_unknown(core) -> None:
    client, app, _cfg, _recorder, key = core
    assert push(client, b"not json at all", key).status_code == 200
    events = all_events(app)
    assert len(events) == 1
    assert events[0].kind == "unknown"
    assert "not json" in events[0].payload


def test_huge_garbage_payload_truncated(core) -> None:
    client, app, _cfg, _recorder, key = core
    body = b"x" * (1024 * 1024)  # 1 MiB of non-JSON garbage
    assert push(client, body, key).status_code == 200
    payload = all_events(app)[0].payload
    assert len(payload) <= 4096 + len("…<truncated>")


def test_events_and_images_pruned_together(tmp_path: Path, ha_recorder, api_key) -> None:
    """EVENT_RETENTION_DAYS is the only thing that deletes: image files and
    their event rows go together."""
    ha_url, _recorder = ha_recorder
    key_file, key = api_key
    cfg = make_config(tmp_path, key_file, ha_webhook_url=ha_url, event_retention_days=1)
    app = create_app(cfg)
    client = app.test_client()
    push(client, snapshot_body(), key)
    name = stored_names(cfg, "object")[0]
    assert (cfg.snaps_dir / name).exists()

    from nvr_core.models import db

    with app.app_context():
        event = db.session.execute(db.select(Event)).scalar_one()
        event.received_at = datetime.now(UTC) - timedelta(days=30)
        db.session.commit()
    ancient = time.time() - 30 * 86400
    os.utime(cfg.snaps_dir / name, (ancient, ancient))

    # the next recorded push prunes the old event AND its image together
    assert push(client, alarm_body(), key).status_code == 200
    with app.app_context():
        remaining = list(db.session.execute(db.select(Event)).scalars().all())
    assert [e.kind for e in remaining] == ["alarm"]
    assert not (cfg.snaps_dir / name).exists()


def test_notification_urls_expire_but_images_survive(tmp_path: Path, ha_recorder, api_key) -> None:
    """URL_RETENTION_DAYS only ages out the anonymous capability: after it,
    the URL 404s for anonymous fetchers (old notifications) while
    signed-in users (the events pages) keep getting the image."""
    ha_url, _recorder = ha_recorder
    key_file, key = api_key
    cfg = make_config(tmp_path, key_file, ha_webhook_url=ha_url, url_retention_days=1)
    app = create_app(cfg)
    client = app.test_client()
    push(client, snapshot_body(), key)
    name = stored_names(cfg, "object")[0]

    # young: anonymous access works (phones, no session)
    assert client.get(f"/snaps/{name}").status_code == 200

    # age the image past the URL retention
    ancient = time.time() - 3 * 86400
    os.utime(cfg.snaps_dir / name, (ancient, ancient))

    # expired: anonymous access is denied
    assert client.get(f"/snaps/{name}").status_code == 404
    # unprivileged signed-in user: still no
    unroled = app.test_client()
    with unroled.session_transaction() as sess:
        sess["user"] = {"sub": "u", "roles": []}
    assert unroled.get(f"/snaps/{name}").status_code == 404
    # authorized signed-in user: the image is preserved with its event
    authed_client = app.test_client()
    with authed_client.session_transaction() as sess:
        sess["user"] = {"sub": "v", "roles": ["nvr_viewers"]}
    response = authed_client.get(f"/snaps/{name}")
    assert response.status_code == 200
    assert response.data == FAKE_JPEG
    # ... and the file was NOT deleted by URL expiry
    assert (cfg.snaps_dir / name).exists()


# ---------------------------------------------------------------------------
# api_key_matches (constant-time bearer compare)
# ---------------------------------------------------------------------------
def test_api_key_matches() -> None:
    import secrets

    from nvr_core.push import api_key_matches

    key = secrets.token_urlsafe(32)
    assert api_key_matches(key, key) is True
    assert api_key_matches(secrets.token_urlsafe(32), key) is False
    assert api_key_matches(None, key) is False
    assert api_key_matches("", key) is False
    # a prefix of the key must not match
    assert api_key_matches(key[: len(key) // 2], key) is False


# ---------------------------------------------------------------------------
# core-only config loaders
# ---------------------------------------------------------------------------
def test_load_path_loader() -> None:
    from nvr_core.config import load_path

    assert load_path({"SNAPS_DIR": "/var/lib/snaps"}, "SNAPS_DIR", "snaps") == Path(
        "/var/lib/snaps"
    )
    assert load_path({}, "SNAPS_DIR", "snaps") == Path("snaps")
    with pytest.raises(ConfigError):
        load_path({"SNAPS_DIR": ""}, "SNAPS_DIR", "snaps")
