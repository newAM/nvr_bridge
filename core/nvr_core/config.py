"""Configuration loading for the nvr_bridge core service."""

from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from nvr_common.config import (
    ConfigError,
    load_log_level,
    load_required_str,
    load_str,
    read_json_object,
)

DEFAULT_SNAPS_DIR = "snaps"
DEFAULT_URL_RETENTION_DAYS = 1
DEFAULT_EVENT_RETENTION_DAYS = 3650
DEFAULT_DATABASE_URL = "sqlite:////var/lib/nvr_bridge/events.db"
DEFAULT_USER_ROLE = "nvr_viewers"
DEFAULT_SESSION_COOKIE_SAMESITE = "Lax"
DEFAULT_GROUP_WINDOW_S = 60

MIN_SECRET_KEY_LEN = 32


def load_path(data: dict, name: str, default: str) -> Path:
    value = data.get(name, default)
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{name} must be a non-empty string")
    return Path(value)


@dataclass
class Config:
    """Settings for the core service (JSON keys in parentheses)."""

    api_key_file: Path
    """File with the shared API key; pushes without it are rejected (API_KEY_FILE)."""

    secret_key: str
    """Flask secret key (signs CSRF tokens; sessions are server-side),
    read from SECRET_KEY_FILE (>= 32 chars)."""

    oidc_client_id: str
    """OIDC client ID registered with the identity provider (OIDC_CLIENT_ID)."""

    oidc_client_secret: str
    """OIDC client secret, read from OIDC_CLIENT_SECRET_FILE."""

    oidc_discovery_url: str
    """OIDC discovery document URL of the identity provider (OIDC_DISCOVERY_URL)."""

    public_url: str
    """Public HTTPS base URL of this service, for the auth callback and
    the snapshot links embedded in forwarded pushes (PUBLIC_URL)."""

    roles_path: list[str]
    """JSON path within the OIDC token or userinfo claims to a list of roles (ROLES_PATH)."""

    ha_webhook_url: str
    """Home Assistant webhook pushes are forwarded to: the HA_WEBHOOK_URL
    base plus the secret id from HA_WEBHOOK_ID_FILE."""

    snaps_dir: Path = field(default_factory=lambda: Path(DEFAULT_SNAPS_DIR))
    """Directory where decoded snapshot JPEGs are stored (SNAPS_DIR)."""

    url_retention_days: int = DEFAULT_URL_RETENTION_DAYS
    """Days the unguessable notification URLs grant anonymous access
    (URL_RETENTION_DAYS).  After this, ``/snaps/<name>`` serves only
    signed-in users (the events pages); 0 disables anonymous access
    entirely.  The images themselves live as long as their events."""

    event_retention_days: int = DEFAULT_EVENT_RETENTION_DAYS
    """Delete recorded events -- and their image files -- older than this
    many days; 0 keeps them forever (EVENT_RETENTION_DAYS)."""

    database_url: str = DEFAULT_DATABASE_URL
    """SQLAlchemy URL for the events database (DATABASE_URL)."""

    snapshot_urls: list[str] = field(default_factory=list)
    """Instant-capture JPEG URLs (e.g. go2rtc) fetched on every alarm push;
    the first successful capture becomes the notification image (SNAPSHOT_URLS)."""

    group_window_s: int = DEFAULT_GROUP_WINDOW_S
    """Seconds after an alarm within which AI snapshots join its group
    (the notification's click target) (GROUP_WINDOW_S)."""

    user_role: str = DEFAULT_USER_ROLE
    """Role that grants access to the events pages (USER_ROLE)."""

    session_cookie_samesite: str = DEFAULT_SESSION_COOKIE_SAMESITE
    """SameSite attribute for the session cookie (SESSION_COOKIE_SAMESITE)."""

    log_level: str = "INFO"


def _read_secret(path: str, name: str, min_len: int) -> str:
    try:
        value = Path(path).read_text().strip()
    except OSError as e:
        raise ConfigError(f"cannot read {name} ({path}): {e}") from e
    if len(value) < min_len:
        raise ConfigError(f"{name} must contain at least {min_len} characters")
    return value


def _read_webhook_id(path: str) -> str:
    """The secret webhook id: one line, no path separators or whitespace."""
    try:
        webhook_id = Path(path).read_text(encoding="utf-8").strip()
    except OSError as e:
        raise ConfigError(f"cannot read HA_WEBHOOK_ID_FILE ({path}): {e}") from e
    if not webhook_id or any(c.isspace() for c in webhook_id):
        raise ConfigError(f"HA_WEBHOOK_ID_FILE ({path}) must contain a single non-empty webhook id")
    if any(c in webhook_id for c in "/?#"):
        raise ConfigError(f"HA_WEBHOOK_ID_FILE ({path}) must not contain '/', '?' or '#'")
    return webhook_id


def _load_ha_webhook_url(data: dict) -> str:
    """The full webhook URL: HA_WEBHOOK_URL base + the id from HA_WEBHOOK_ID_FILE."""
    base = load_required_str(data, "HA_WEBHOOK_URL")
    parsed = urlparse(base)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigError("HA_WEBHOOK_URL must be a valid HTTP(S) URL")
    webhook_id = _read_webhook_id(load_required_str(data, "HA_WEBHOOK_ID_FILE"))
    return f"{base.rstrip('/')}/{webhook_id}"


def _load_https_url(data: dict, name: str) -> str:
    url = load_required_str(data, name)
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ConfigError(f"{name} must be an https:// URL")
    return url


def _load_roles_path(data: dict) -> list[str]:
    value = data.get("ROLES_PATH")
    if not isinstance(value, list) or not value or not all(isinstance(k, str) for k in value):
        raise ConfigError("ROLES_PATH must be a non-empty list of strings")
    return value


def _load_retention(data: dict, name: str, default: int) -> int:
    value = data.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConfigError(f"{name} must be a non-negative integer")
    return value


def _load_samesite(data: dict) -> str:
    value = data.get("SESSION_COOKIE_SAMESITE", DEFAULT_SESSION_COOKIE_SAMESITE)
    if value not in ("Strict", "Lax"):
        raise ConfigError("SESSION_COOKIE_SAMESITE must be one of: Lax, Strict")
    return value


def _load_snapshot_urls(data: dict) -> list[str]:
    urls = data.get("SNAPSHOT_URLS", [])
    if not isinstance(urls, list) or any(not isinstance(u, str) for u in urls):
        raise ConfigError("SNAPSHOT_URLS must be a list of URLs")
    for url in urls:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ConfigError("SNAPSHOT_URLS must contain valid HTTP(S) URLs")
    return urls


def _load_group_window(data: dict) -> int:
    value = data.get("GROUP_WINDOW_S", DEFAULT_GROUP_WINDOW_S)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError("GROUP_WINDOW_S must be a positive integer")
    return value


def load_config(path: str | Path) -> Config:
    data = read_json_object(path)

    return Config(
        api_key_file=Path(load_required_str(data, "API_KEY_FILE")),
        secret_key=_read_secret(
            load_required_str(data, "SECRET_KEY_FILE"), "SECRET_KEY_FILE", MIN_SECRET_KEY_LEN
        ),
        oidc_client_id=load_required_str(data, "OIDC_CLIENT_ID"),
        oidc_client_secret=_read_secret(
            load_required_str(data, "OIDC_CLIENT_SECRET_FILE"), "OIDC_CLIENT_SECRET_FILE", 1
        ),
        oidc_discovery_url=_load_https_url(data, "OIDC_DISCOVERY_URL"),
        public_url=_load_https_url(data, "PUBLIC_URL"),
        roles_path=_load_roles_path(data),
        ha_webhook_url=_load_ha_webhook_url(data),
        snaps_dir=load_path(data, "SNAPS_DIR", DEFAULT_SNAPS_DIR),
        url_retention_days=_load_retention(data, "URL_RETENTION_DAYS", DEFAULT_URL_RETENTION_DAYS),
        event_retention_days=_load_retention(
            data, "EVENT_RETENTION_DAYS", DEFAULT_EVENT_RETENTION_DAYS
        ),
        database_url=load_str(data, "DATABASE_URL", DEFAULT_DATABASE_URL),
        snapshot_urls=_load_snapshot_urls(data),
        group_window_s=_load_group_window(data),
        user_role=load_str(data, "USER_ROLE", DEFAULT_USER_ROLE),
        session_cookie_samesite=_load_samesite(data),
        log_level=load_log_level(data),
    )
