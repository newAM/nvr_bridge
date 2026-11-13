"""Push classification, snapshot storage, and enrichment for the core."""

import copy
import functools
import hmac
import json
import logging
import uuid
from dataclasses import dataclass

from flask import abort, current_app, request

from nvr_core.capture import capture_live_snapshots
from nvr_core.config import Config
from nvr_core.models import recent_group
from nvr_core.payload import (
    MAX_IMAGE_BYTES,
    decode_jpeg,
    person_message,
    person_title,
    redact_body,
    snap_id,
)
from nvr_core.store import SnapshotStore

LOG = logging.getLogger(__name__)


@dataclass
class ProcessedPush:
    """The outcome of classifying one push."""

    kind: str | None
    """``snapshot``, ``alarm``, ``keepalive``, or None for unknown/non-JSON."""

    forward_body: bytes | None
    """Body to forward to Home Assistant, or None to skip (keepalives)."""

    redacted_payload: str = ""
    """The push body as redacted JSON, for event storage."""

    device_name: str | None = None
    str_chn: str | None = None
    object_snap: str | None = None
    background_snap: str | None = None
    live_snaps: list[str] | None = None
    """Instant-capture file names taken at alarm time (go2rtc)."""

    group: str | None = None
    """Correlation token shared by an alarm and its following snapshots."""


def api_key_matches(provided: str | None, expected: str) -> bool:
    """Constant-time comparison of a presented bearer token against the key."""
    if provided is None:
        return False
    return hmac.compare_digest(provided.encode(), expected.encode())


def require_api_key(view):
    """Reject the request with 401 unless it carries the shared API key."""

    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        token = auth.removeprefix("Bearer ") if auth.startswith("Bearer ") else None
        if not api_key_matches(token, current_app.config["API_KEY"]):
            LOG.warning(
                "rejected push with missing or invalid API key from %s", request.remote_addr
            )
            abort(401)
        return view(*args, **kwargs)

    return wrapped


def process_push(body: bytes, store: SnapshotStore, config: Config, path: str) -> ProcessedPush:
    """Classify a push; returns what to forward and what to record."""
    try:
        payload = json.loads(body)
    except ValueError:
        LOG.info("non-JSON push to %s (%d bytes)", path, len(body))
        return ProcessedPush(kind=None, forward_body=body, redacted_payload=redact_body(body))

    data = payload.get("data") if isinstance(payload, dict) else None
    device_name = data.get("device_name") if isinstance(data, dict) else None

    if isinstance(data, dict) and "ai_snap_picture" in data:
        enriched, object_snap, background_snap = _store_snapshots(payload, data, store, config)
        infos = data.get("ai_snap_picture", {}).get("SnapedObjInfo") or []
        info = infos[0] if infos and isinstance(infos[0], dict) else {}
        str_chn = info.get("StrChn")
        # Join the alarm's group when the snapshot follows it within the
        # window: one notification, updated in place.
        group = recent_group(config.group_window_s) or uuid.uuid4().hex
        # Fresh token for the stills so the alarm's captures of the same
        # group are never overwritten.
        live = capture_live_snapshots(store, config.snapshot_urls, uuid.uuid4().hex)
        out_infos = enriched["data"]["ai_snap_picture"].get("SnapedObjInfo") or []
        out_info = out_infos[0] if out_infos and isinstance(out_infos[0], dict) else {}
        _add_bridge_block(enriched, config, group, live=live, info=info, enriched_info=out_info)
        return ProcessedPush(
            kind="snapshot",
            forward_body=json.dumps(enriched).encode("utf-8"),
            redacted_payload=redact_body(body),
            device_name=device_name,
            str_chn=str_chn,
            object_snap=object_snap,
            background_snap=background_snap,
            live_snaps=live,
            group=group,
        )
    if isinstance(data, dict) and "alarm_list" in data:
        return _process_alarm(payload, body, data, store, config, device_name)
    if data == {}:
        # {"result": "success", "data": {}} heartbeat: acknowledged but
        # not forwarded or recorded -- it carries no information.
        return ProcessedPush(kind="keepalive", forward_body=None)

    LOG.info("unrecognized push to %s, data keys: %s", path, sorted(data or {}))
    return ProcessedPush(
        kind=None, forward_body=body, redacted_payload=redact_body(body), device_name=device_name
    )


def _process_alarm(
    payload: dict,
    body: bytes,
    data: dict,
    store: SnapshotStore,
    config: Config,
    device_name: str | None,
) -> ProcessedPush:
    """Capture instant snapshots from every configured URL, start a new
    event group, and enrich the forwarded alarm with the notification
    image (the first successful capture) and the group's page URL."""
    group = uuid.uuid4().hex
    channel = _alarm_channel(data)
    live = capture_live_snapshots(store, config.snapshot_urls, group)
    enriched = copy.deepcopy(payload)
    _add_bridge_block(
        enriched,
        config,
        group,
        live=live,
        title=f"Motion on {channel or 'camera'}",
        message=f"Motion detected{f' on {channel}' if channel else ''}",
    )
    return ProcessedPush(
        kind="alarm",
        forward_body=json.dumps(enriched).encode("utf-8"),
        redacted_payload=redact_body(body),
        device_name=device_name,
        str_chn=channel,
        live_snaps=live,
        group=group,
    )


def _add_bridge_block(
    payload: dict,
    config: Config,
    group: str,
    live: list[str] | None = None,
    info: dict | None = None,
    enriched_info: dict | None = None,
    title: str | None = None,
    message: str | None = None,
) -> None:
    """Attach the ``nvr_bridge`` block the HA automation wires into the
    notification: identity, text, images, and click target, all
    pre-rendered here in plain, testable Python.  URLs derive from the
    configured PUBLIC_URL only, never from request headers (Host header
    injection)."""
    if title is None and info is not None:
        title = person_title(info)
    if message is None and info is not None:
        message = person_message(info)

    block: dict = {"group": group}
    if title:
        block["title"] = title
    if message:
        block["message"] = message

    base = config.public_url.rstrip("/")
    block["url"] = f"{base}/events/group/{group}"
    image = None
    if enriched_info is not None:
        # the AI snapshot's own images take precedence
        image = enriched_info.get("object_url") or None
    if image is None and live:
        image = f"{base}/snaps/{live[0]}"
    if image:
        block["image_url"] = image
    if enriched_info is not None and enriched_info.get("background_url"):
        block["background_url"] = enriched_info["background_url"]
    if live:
        block["image_urls"] = [f"{base}/snaps/{name}" for name in live]
    payload["nvr_bridge"] = block


def _alarm_channel(data: dict) -> str | None:
    """Best-effort channel label from an alarm payload."""
    try:
        entries = data["alarm_list"]
        channels = entries[0]["channel_alarm"]
        channel = channels[0]["channel"]
        return channel if isinstance(channel, str) else None
    except (KeyError, IndexError, TypeError):
        return None


def _store_snapshots(
    payload: dict, data: dict, store: SnapshotStore, config: Config
) -> tuple[dict, str | None, str | None]:
    """Decode + save all snapshot images; return enriched payload + file names."""
    enriched = copy.deepcopy(payload)
    # PUBLIC_URL only, never request headers (Host header injection).
    base = config.public_url.rstrip("/")
    infos = data.get("ai_snap_picture", {}).get("SnapedObjInfo") or []
    out_infos = enriched["data"]["ai_snap_picture"].get("SnapedObjInfo") or []

    object_snap: str | None = None
    background_snap: str | None = None

    for snap, out in zip(infos, out_infos, strict=True):
        sid = snap_id(snap)
        # One token per snapshot record: object and background arrive in
        # the same notification, so sharing it adds no exposure; re-pushed
        # events get a fresh token.
        token = uuid.uuid4().hex
        for field_name, suffix in (
            ("ObjectImage", "object"),
            ("Background", "background"),
        ):
            raw = decode_jpeg(snap.get(field_name))
            if raw is None:
                if snap.get(field_name):
                    LOG.warning(
                        "%s of snap %d is not a JPEG or exceeds %d bytes; skipped",
                        field_name,
                        sid,
                        MAX_IMAGE_BYTES,
                    )
                continue
            name = f"snap_{token}_{suffix}.jpg"
            store.save(name, raw)
            if suffix == "object":
                object_snap = name
            else:
                background_snap = name
            out[f"{suffix}_url"] = f"{base}/snaps/{name}"
            LOG.info("saved snap %d: %s (%d bytes)", sid, name, len(raw))

    return enriched, object_snap, background_snap
