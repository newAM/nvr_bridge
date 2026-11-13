"""Configuration loading for the nvr_bridge gateway."""

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from nvr_common.config import (
    ConfigError,
    load_log_level,
    load_required_str,
    load_str,
    read_json_object,
)


def load_int(data: dict, name: str, default: int, *, minimum: int, maximum: int) -> int:
    value = data.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ConfigError(f"{name} must be an integer between {minimum} and {maximum}")
    return value


DEFAULT_LISTEN = "0.0.0.0"
DEFAULT_PORT = 8520


@dataclass
class Config:
    """Settings for the gateway service (JSON keys in parentheses)."""

    core_url: str
    """The core's HTTPS webhook URL pushes are relayed to (CORE_URL, required)."""

    api_key_file: Path
    """File with the shared API key, sent as a bearer token to the core (API_KEY_FILE, required)."""

    listen: str = DEFAULT_LISTEN
    """Address the NVR-facing interface binds (LISTEN)."""

    port: int = DEFAULT_PORT
    """Port the NVR-facing interface binds; point the NVR's HTTP listener here (PORT)."""

    log_level: str = "INFO"


def load_config(path: str | Path) -> Config:
    data = read_json_object(path)

    core_url = load_required_str(data, "CORE_URL")
    parsed = urlparse(core_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ConfigError(
            "CORE_URL must be an https:// URL (upgrading to HTTPS is the gateway's job)"
        )

    return Config(
        core_url=core_url,
        api_key_file=Path(load_required_str(data, "API_KEY_FILE")),
        listen=load_str(data, "LISTEN", DEFAULT_LISTEN),
        port=load_int(data, "PORT", DEFAULT_PORT, minimum=1, maximum=65535),
        log_level=load_log_level(data),
    )
