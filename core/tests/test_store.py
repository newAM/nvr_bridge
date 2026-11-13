"""Tests for nvr_core.store."""

import os
import time
from pathlib import Path

from nvr_core.store import SnapshotStore


def test_read_rejects_traversal(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path / "snaps")
    assert store.read("../etc/passwd") is None
    assert store.read("sub/dir/file.jpg") is None
    assert store.read("notes.txt") is None
    assert store.read("missing.jpg") is None
    # only generated snapshot names are served (NUL bytes, other prefixes)
    assert store.read("\x00evil.jpg") is None
    assert store.read("evil.jpg") is None
    assert store.read("latest.jpg") is None
    # legacy sequential-style names are rejected too
    assert store.read("snap_08588_CH2_object.jpg") is None


def test_save_then_read_roundtrip(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path / "snaps")
    name = "snap_" + "a" * 32 + "_object.jpg"
    store.save(name, b"\xff\xd8\xffdata")
    assert store.read(name) == b"\xff\xd8\xffdata"
    # no temp files are left behind
    assert [p.name for p in store.root.iterdir()] == [name]


def test_live_names_and_age(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path / "snaps")
    name = "snap_" + "b" * 32 + "_live0.jpg"
    store.save(name, b"\xff\xd8\xff")
    assert store.exists(name)
    assert 0 <= store.age_days(name) < 1
    ancient = time.time() - 3 * 86400
    os.utime(store.root / name, (ancient, ancient))
    assert 2 < store.age_days(name) < 4
    assert store.age_days("snap_" + "c" * 32 + "_live9.jpg") == float("inf")
    assert not store.exists("snap_" + "c" * 32 + "_live9.jpg")


def test_prune_by_age(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path / "snaps")
    old = tmp_path / "snaps" / "snap_00001_CH1_object.jpg"
    fresh = tmp_path / "snaps" / "snap_00002_CH1_object.jpg"
    for path in (old, fresh):
        path.write_bytes(b"\xff\xd8\xff")
    ancient = time.time() - 30 * 86400
    os.utime(old, (ancient, ancient))
    assert store.prune(14) == 1
    assert not old.exists()
    assert fresh.exists()
    assert store.prune(0) == 0  # retention disabled
