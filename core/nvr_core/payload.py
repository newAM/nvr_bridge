"""Push-payload helpers: device field extraction, JPEG decoding, and the
redacted forms used for logging and event storage (core-only)."""

import base64
import binascii
import json
import time

MAX_IMAGE_BYTES = 16 * 1024 * 1024
"""Largest decoded snapshot image stored; larger payloads are discarded."""

JPEG_MAGIC = b"\xff\xd8\xff"

REDACT_STRING_MAX = 256
"""Strings longer than this (embedded base64 images) are elided in debug logs."""


def snap_id(snap: dict) -> int:
    """Best-effort integer snap id (device-global, unique per snapshot)."""
    value = snap.get("SnapId")
    if isinstance(value, bool):
        pass
    elif isinstance(value, int):
        return value
    elif isinstance(value, str) and value.isdigit():
        return int(value)
    try:
        return int(snap.get("StartTime") or time.time())
    except TypeError, ValueError:
        # malformed StartTime (e.g. the device's ISO strings); never raise
        return int(time.time())


def decode_jpeg(encoded: object, max_bytes: int = MAX_IMAGE_BYTES) -> bytes | None:
    """Decode base64 to raw JPEG bytes, or None if invalid or too large."""
    if not isinstance(encoded, str) or not encoded:
        return None
    try:
        raw = base64.b64decode(encoded)
    except binascii.Error, ValueError:
        return None
    if len(raw) > max_bytes:
        return None
    return raw if raw.startswith(JPEG_MAGIC) else None


def _redact_long_strings(obj: object) -> object:
    """Recursively replace over-long strings (base64 JPEGs) with a size note."""
    if isinstance(obj, dict):
        return {key: _redact_long_strings(value) for key, value in obj.items()}
    if isinstance(obj, list):
        return [_redact_long_strings(item) for item in obj]
    if isinstance(obj, str) and len(obj) > REDACT_STRING_MAX:
        return f"<redacted: {len(obj)}-char string>"
    return obj


def pretty_body(body: bytes) -> str:
    """Pretty-print a push body for debug logs (JSON indented, else raw).

    Long strings (embedded base64 images) are elided so snapshot pushes
    stay readable.
    """
    try:
        return json.dumps(
            _redact_long_strings(json.loads(body)), indent=2, sort_keys=True, ensure_ascii=False
        )
    except ValueError:
        return repr(body)


def redact_body(body: bytes, max_len: int = 4096) -> str:
    """Compact redacted JSON text of a push body for storage.

    Long strings (embedded base64 JPEGs) are elided, non-JSON bodies fall
    back to ``repr``, and the result is truncated to ``max_len`` characters
    so a hostile body cannot bloat the database.
    """
    try:
        text = json.dumps(_redact_long_strings(json.loads(body)))
    except ValueError:
        text = repr(body)
    return text if len(text) <= max_len else text[:max_len] + "…<truncated>"


def person_title(info: dict, fallback_channel: str | None = None) -> str:
    """Notification title for a person-detection event."""
    channel = info.get("StrChn") or fallback_channel or "camera"
    return f"Person on {channel}" if isinstance(channel, str) else "Person detected"


def person_message(info: dict) -> str:
    """Notification body for a person-detection event."""
    extras = []
    if info.get("person_hat") == "Yes":
        extras.append("hat")
    bag = info.get("person_bag")
    if isinstance(bag, str) and bag and bag != "No":
        extras.append(bag)
    age = info.get("person_age")
    gender = info.get("person_gender")
    parts = [p for p in (age, gender) if isinstance(p, str) and p] or ["Person"]
    text = " ".join(parts)
    if extras:
        text += ", " + ", ".join(extras)
    return text
