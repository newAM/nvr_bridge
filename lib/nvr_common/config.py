"""Config primitives shared by both services.

Each service defines its own ``Config`` dataclass from these; anything
only one service needs lives in that service instead.
"""

import json
from pathlib import Path

VALID_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


class ConfigError(Exception):
    pass


def read_json_object(path: str | Path) -> dict:
    """Read a config file: must exist, be valid JSON, and be an object."""
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise ConfigError(f"config file is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError("config file must be a JSON object")
    return data


def load_str(data: dict, name: str, default: str) -> str:
    value = data.get(name, default)
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{name} must be a non-empty string")
    return value


def load_required_str(data: dict, name: str) -> str:
    if name not in data:
        raise ConfigError(f"{name} is required")
    value = data[name]
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{name} must be a non-empty string")
    return value


def load_log_level(data: dict, default: str = "INFO") -> str:
    level = data.get("LOG_LEVEL", default)
    if isinstance(level, str):
        level = level.upper()
    if level not in VALID_LOG_LEVELS:
        raise ConfigError(f"LOG_LEVEL must be one of: {', '.join(sorted(VALID_LOG_LEVELS))}")
    return level
