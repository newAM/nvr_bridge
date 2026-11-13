"""The shared API key: one file, read by both services (mounted read-only
into both units); it never enters the Nix store."""

from pathlib import Path

from nvr_common.config import ConfigError

MIN_API_KEY_LEN = 32
"""Minimum key length in characters; generate with
  ``python3 -c "import secrets; print(secrets.token_urlsafe(32))"``."""


def load_api_key(path: str | Path) -> str:
    """Read and validate the API key file (surrounding whitespace stripped)."""
    path = Path(path)
    try:
        key = path.read_text().strip()
    except OSError as e:
        raise ConfigError(f"cannot read API key file {path}: {e}") from e
    if len(key) < MIN_API_KEY_LEN:
        raise ConfigError(f"API key file {path} must contain at least {MIN_API_KEY_LEN} characters")
    return key
