"""Helpers shared by the nvr_bridge gateway and core services."""

import urllib.parse

MAX_BODY_BYTES = 32 * 1024 * 1024
"""Largest accepted push body (enforced by both the gateway and the core)."""


def redact_url(url: str) -> str:
    """Redact a URL for logging -- the path carries the secret webhook id."""
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, "***", "", ""))
