"""Tests for nvr_common.apikey (shared key loading)."""

import secrets
from pathlib import Path

import pytest
from nvr_common import ConfigError, load_api_key


def write_key(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "api_key"
    path.write_text(content)
    return path


def test_load_api_key_strips_whitespace(tmp_path: Path) -> None:
    key = secrets.token_urlsafe(32)
    assert load_api_key(write_key(tmp_path, f"{key}\n")) == key
    assert load_api_key(write_key(tmp_path, f"  {key}  \n")) == key


def test_load_api_key_rejects_short_keys(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="at least 32"):
        load_api_key(write_key(tmp_path, "tooshort"))


def test_load_api_key_rejects_unreadable_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read"):
        load_api_key(tmp_path / "missing")
