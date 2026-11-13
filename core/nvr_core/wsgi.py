"""
WSGI entrypoint for the nvr_bridge core service.

The config file path is read from the NVR_CORE_CONFIG_FILE environment variable.
"""

import os
import sys

from nvr_core.app import create_app
from nvr_core.config import ConfigError, load_config

_config_path = os.environ.get("NVR_CORE_CONFIG_FILE")
if not _config_path:
    print("Error: NVR_CORE_CONFIG_FILE environment variable is not set", file=sys.stderr)
    sys.exit(1)

try:
    _config = load_config(_config_path)
except ConfigError as e:
    print(f"Error: {e}", file=sys.stderr)
    sys.exit(1)

application = create_app(_config)
