"""Snapshots on disk: JPEG files under unguessable names (core-only)."""

import os
import re
import threading
import time
from pathlib import Path

SNAP_NAME_RE = re.compile(r"^snap_[0-9a-f]{32}_(?:object|background|live[0-9]+)\.jpg$")
"""Snapshot names carry 122 bits of CSPRNG entropy (``uuid4``): each URL
is an unguessable capability, and nothing sequential (the device's
``SnapId``, channel labels) appears in it.  ``live`` names are instant
captures from external JPEG URLs (go2rtc)."""


class SnapshotStore:
    """Snapshot storage for the core's push and viewer routes.

    Writes are atomic (tmp file + rename), so readers -- in this or another
    process -- never observe partial images.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, name: str, data: bytes) -> None:
        """Atomically write a snapshot file (readers never see partials)."""
        tmp = self.root / f".{name}.{os.getpid()}.{threading.get_ident()}.tmp"
        tmp.write_bytes(data)
        tmp.replace(self.root / name)

    def read(self, name: str) -> bytes | None:
        """Read a snapshot file; only flat generated ``snap_*.jpg`` names pass."""
        if name != Path(name).name or not SNAP_NAME_RE.fullmatch(name):
            return None
        try:
            return (self.root / name).read_bytes()
        except OSError:
            return None

    def exists(self, name: str) -> bool:
        """Whether a stored snapshot is still present (name rules as in
        :meth:`read`)."""
        if name != Path(name).name or not SNAP_NAME_RE.fullmatch(name):
            return False
        return (self.root / name).is_file()

    def age_days(self, name: str) -> float:
        """Age of a stored snapshot in days (``float("inf")`` if absent).

        The file mtime is set at capture time and survives the atomic
        rename used by :meth:`save`, so it is the age of the image.
        """
        try:
            mtime = (self.root / name).stat().st_mtime
        except OSError:
            return float("inf")
        return (time.time() - mtime) / 86400

    def prune(self, retention_days: int) -> int:
        """Delete snapshots older than ``retention_days``; 0 disables."""
        if retention_days <= 0:
            return 0
        cutoff = time.time() - retention_days * 86400
        removed = 0
        for path in self.root.glob("snap_*.jpg"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
        return removed
