"""Tests for nvr_core.payload."""

import base64
import json
import time

import pytest
from conftest import FAKE_JPEG
from nvr_core.payload import MAX_IMAGE_BYTES, decode_jpeg, pretty_body, redact_body, snap_id


def test_snap_id_fallbacks() -> None:
    assert snap_id({"SnapId": 42}) == 42
    assert snap_id({"SnapId": "43"}) == 43
    assert snap_id({"StartTime": 1793631350}) == 1793631350
    assert snap_id({"Chn": 1, "StrChn": ""}) == pytest.approx(time.time(), abs=5)
    # malformed fields must never raise (the device sends broken ISO strings)
    ugly = {"SnapId": "not-a-number", "StartTime": "2026-11-02 21:55:51"}
    assert snap_id(ugly) == pytest.approx(time.time(), abs=5)
    assert snap_id({"StartTime": ["nope"]}) == pytest.approx(time.time(), abs=5)


def test_decode_jpeg_caps_size() -> None:
    oversized = b"\xff\xd8\xff" + b"x" * (MAX_IMAGE_BYTES + 1)
    assert decode_jpeg(base64.b64encode(oversized).decode()) is None
    assert decode_jpeg(base64.b64encode(FAKE_JPEG).decode()) == FAKE_JPEG
    assert decode_jpeg(None) is None
    assert decode_jpeg("") is None
    assert decode_jpeg("not base64 !!!") is None
    assert decode_jpeg(base64.b64encode(b"not a jpeg").decode()) is None


def test_pretty_body_elides_long_strings() -> None:
    body = json.dumps({"data": {"image": "a" * 1024, "note": "hello"}}).encode()
    pretty = pretty_body(body)
    assert "hello" in pretty
    assert "aaaa" not in pretty
    assert "<redacted:" in pretty
    # non-JSON bodies fall back to repr, never raising
    assert isinstance(pretty_body(b"\xff\xfe not json"), str)


def test_redact_body_elides_and_truncates() -> None:
    body = json.dumps({"data": {"image": base64.b64encode(FAKE_JPEG).decode()}}).encode()
    stored = redact_body(body)
    assert base64.b64encode(FAKE_JPEG).decode() not in stored
    assert "<redacted:" in stored
    # key order is preserved (insertion order of the original document)
    ordered = json.loads(redact_body(b'{"z": 1, "a": 2}'))
    assert list(ordered) == ["z", "a"]
    huge = b"x" * (1024 * 1024)  # non-JSON garbage
    assert len(redact_body(huge)) <= 4096 + len("…<truncated>")
