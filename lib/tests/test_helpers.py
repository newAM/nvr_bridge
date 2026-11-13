"""Tests for nvr_common.helpers (shared)."""

from nvr_common import redact_url


def test_redact_url() -> None:
    # the path carries the secret webhook id and must never reach the logs
    assert redact_url("http://ha:8123/api/webhook/secret-id") == "http://ha:8123/***"
    assert redact_url("https://ha.example.com/hook?token=x") == "https://ha.example.com/***"
    assert redact_url("http://ha:8123") == "http://ha:8123/***"
