"""Tests for the shared config primitives (nvr_common.config)."""

import json
from pathlib import Path

import pytest
from nvr_common import ConfigError, read_json_object
from nvr_common.config import load_log_level, load_required_str, load_str


def write_config(tmp_path: Path, data: object) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data))
    return path


def test_read_json_object_rejects_bad_files(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        read_json_object(tmp_path / "missing.json")
    (tmp_path / "bad.json").write_text("{not json")
    with pytest.raises(ConfigError, match="not valid JSON"):
        read_json_object(tmp_path / "bad.json")
    (tmp_path / "list.json").write_text("[]")
    with pytest.raises(ConfigError, match="JSON object"):
        read_json_object(tmp_path / "list.json")
    assert read_json_object(write_config(tmp_path, {"PORT": 1})) == {"PORT": 1}


def test_load_str() -> None:
    assert load_str({"LISTEN": "0.0.0.0"}, "LISTEN", "x") == "0.0.0.0"
    assert load_str({}, "LISTEN", "fallback") == "fallback"
    for bad in ("", 5, None, True):
        with pytest.raises(ConfigError):
            load_str({"LISTEN": bad}, "LISTEN", "x")


def test_load_required_str() -> None:
    with pytest.raises(ConfigError, match="required"):
        load_required_str({}, "CORE_URL")
    assert load_required_str({"CORE_URL": "https://x"}, "CORE_URL") == "https://x"
    with pytest.raises(ConfigError):
        load_required_str({"CORE_URL": ""}, "CORE_URL")


def test_load_log_level() -> None:
    assert load_log_level({}) == "INFO"
    assert load_log_level({"LOG_LEVEL": "debug"}) == "DEBUG"  # case-normalised
    for bad in ("LOUD", 3, None):
        with pytest.raises(ConfigError):
            load_log_level({"LOG_LEVEL": bad})
