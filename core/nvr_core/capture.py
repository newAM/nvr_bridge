"""Instant JPEG capture from external snapshot URLs (e.g. go2rtc).

When a motion alarm arrives -- typically ~15 s before the NVR's own AI
snapshot push -- every configured URL is fetched in parallel and stored
under unguessable names, so the first Home Assistant notification already
carries an image.
"""

import logging
from concurrent.futures import ThreadPoolExecutor

import requests
from nvr_common import redact_url

from nvr_core.payload import JPEG_MAGIC, MAX_IMAGE_BYTES
from nvr_core.store import SnapshotStore

CAPTURE_TIMEOUT_S = 3.0
"""Per-URL fetch timeout; bounded well inside the gateway's 5 s relay timeout."""

LOG = logging.getLogger(__name__)


def capture_live_snapshots(store: SnapshotStore, urls: list[str], token: str) -> list[str]:
    """Fetch JPEGs from all ``urls`` in parallel; store as
    ``snap_<token>_live<i>.jpg``.  Returns the stored names in URL order
    (failed sources are logged and skipped)."""
    if not urls:
        return []
    with ThreadPoolExecutor(max_workers=len(urls)) as pool:
        results = list(pool.map(_fetch_jpeg, urls))
    names: list[str] = []
    for index, data in enumerate(results):
        if data is None:
            continue
        name = f"snap_{token}_live{index}.jpg"
        store.save(name, data)
        names.append(name)
        LOG.info("captured live snapshot from url %d: %s (%d bytes)", index, name, len(data))
    return names


def _fetch_jpeg(url: str) -> bytes | None:
    """Fetch one URL and return raw JPEG bytes, or None if it fails."""
    try:
        response = requests.get(url, timeout=CAPTURE_TIMEOUT_S)
        response.raise_for_status()
        data = response.content
    except requests.RequestException as exc:
        LOG.warning("snapshot fetch from %s failed: %r", redact_url(url), exc)
        return None
    if len(data) > MAX_IMAGE_BYTES:
        LOG.warning(
            "snapshot fetch from %s exceeds %d bytes; skipped",
            redact_url(url),
            MAX_IMAGE_BYTES,
        )
        return None
    if not data.startswith(JPEG_MAGIC):
        LOG.warning("snapshot fetch from %s is not a JPEG; skipped", redact_url(url))
        return None
    return data
