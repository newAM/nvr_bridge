"""nvr_common: the contract shared by the nvr_bridge gateway and core.

Only functionality required by both services lives here; everything else
belongs to the service that uses it.
"""

from nvr_common.apikey import load_api_key
from nvr_common.config import (
    ConfigError,
    load_log_level,
    load_required_str,
    load_str,
    read_json_object,
)
from nvr_common.forwarder import BridgeState, HttpForwarder
from nvr_common.helpers import MAX_BODY_BYTES, redact_url

__all__ = [
    "MAX_BODY_BYTES",
    "BridgeState",
    "ConfigError",
    "HttpForwarder",
    "load_api_key",
    "load_log_level",
    "load_required_str",
    "load_str",
    "read_json_object",
    "redact_url",
]
