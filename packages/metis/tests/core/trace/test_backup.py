"""Tests for `metis.core.trace.backup` — backup/restore of the trace DB.

See `docs/specs/event-bus-and-trace-catalog.md` §7.5 for the contract.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from metis.core.events.envelope import Actor
from metis.core.events.payloads import SessionCreated, make_event
from metis.core.trace.backup import (
    BackupError,
    backup,
    restore,
)
from metis.core.trace.store import TRACE_SCHEMA_VERSION, TraceStore


def _session_created_event(session_id: str):
    return make_event(
        type="session.created",
        session_id=session_id,
        actor=Actor.SYSTEM,
        payload=SessionCreated(
            workspace_path="/x",
            workspace_hash="h",
            initial_active_model=None,
            routing_policy_version="v",
        ),
        timestamp=datetime.now(UTC),
    )


def _seed_store(path: Path, n: int) -> TraceStore:
    """Open a fresh trace store at `path` and write n events to it."""
    store = TraceStore(path)
    for i in range(n):
        store.write(_session_created_event(f"sess_{i}"))
    return store


# ---- Schema-version stamp ------------------------------------------------


def test_fresh_trace_store_stamps_user_version(tmp_path: Path):
    """The store sets PRAGMA user_version = TRACE_SCHEMA_VERSION on open."""
    db_path = tmp_path / "trace.db"
    store = TraceStore(db_path)
    try:
        version = store._conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        store.close()
    assert version == TRACE_SCHEMA_VERSION


# ---- Round-trip ----------------------------------------------------------


def test_backup_then_restore_round_trip(tmp_path: Path):
    """Write events → backup → restore to a fresh path → events match."""
    src_db = tmp_path / "src.db"
    backup_path = tmp_path / "snap.db"
    fresh_db = tmp_path / "restored.db"

    store = _seed_store(src_db, n=5)
    seeded_ids = sorted(e.id for e in store.events_for_session("sess_0"))
    # Capture all events by re-querying every seeded session.
    all_events_before = []
    for i in range(5):
        all_events_before.extend(store.events_for_session(f"sess_{i}"))
    store.close()

    result = backup(src_db, backup_path)
    assert result.byte_count > 0
    assert result.event_count == 5
    assert result.schema_version == TRACE_SCHEMA_VERSION
    assert result.oldest_event_timestamp is not None
    assert result.newest_event_timestamp is not None
    assert backup_path.exists()

    # Restore over a fresh path. The destination DB must not already exist
    # for default behavior.
    restore_result = restore(backup_path, fresh_db)
    assert restore_result.event_count == 5
    assert restore_result.schema_version == TRACE_SCHEMA_VERSION

    # Re-open the restored DB and confirm round-trip.
    restored = TraceStore(fresh_db)
    try:
        all_events_after = []
        for i in range(5):
            all_events_after.extend(restored.events_for_session(f"sess_{i}"))
    finally:
        restored.close()

    assert sorted(e.id for e in all_events_after) == sorted(e.id for e in all_events_before)
    # Spot-check that the first seeded event made the round trip.
    assert seeded_ids[0] in {e.id for e in all_events_after}


def test_backup_metadata_records_event_window(tmp_path: Path):
    """Oldest/newest timestamps span the inserted events."""
    src_db = tmp_path / "src.db"
    store = _seed_store(src_db, n=3)
    store.close()
    backup_path = tmp_path / "snap.db"

    result = backup(src_db, backup_path)
    assert result.oldest_event_timestamp is not None
    assert result.newest_event_timestamp is not None
    assert result.oldest_event_timestamp <= result.newest_event_timestamp


def test_backup_empty_db_has_no_event_window(tmp_path: Path):
    """An empty trace DB backs up cleanly with null timestamps."""
    src_db = tmp_path / "src.db"
    store = TraceStore(src_db)
    store.close()
    backup_path = tmp_path / "empty.db"

    result = backup(src_db, backup_path)
    assert result.event_count == 0
    assert result.oldest_event_timestamp is None
    assert result.newest_event_timestamp is None


# ---- Schema-version mismatch --------------------------------------------


def test_restore_refuses_schema_mismatch(tmp_path: Path):
    """A backup with a non-matching user_version is rejected with a clear
    diagnostic that mentions both versions and the migration path."""
    # Fabricate a backup with the wrong schema version by writing a fresh DB
    # and forcing a different PRAGMA value.
    fake_backup = tmp_path / "wrong-version.db"
    conn = sqlite3.connect(fake_backup, isolation_level=None)
    conn.execute("PRAGMA user_version = 999")
    conn.execute("CREATE TABLE events (id TEXT PRIMARY KEY)")  # bare shape
    conn.close()

    dest = tmp_path / "dest.db"
    with pytest.raises(BackupError, match="schema-version mismatch"):
        restore(fake_backup, dest)
    assert not dest.exists()  # destination untouched


# ---- Overwrite protection -----------------------------------------------


def test_restore_refuses_overwrite_by_default(tmp_path: Path):
    src_db = tmp_path / "src.db"
    store = _seed_store(src_db, n=1)
    store.close()

    backup_path = tmp_path / "snap.db"
    backup(src_db, backup_path)

    dest = tmp_path / "dest.db"
    dest.write_bytes(b"existing-db-bytes")

    with pytest.raises(BackupError, match="already exists"):
        restore(backup_path, dest)
    # Destination is untouched (still has the canary bytes).
    assert dest.read_bytes() == b"existing-db-bytes"


def test_restore_with_force_overwrites(tmp_path: Path):
    src_db = tmp_path / "src.db"
    store = _seed_store(src_db, n=2)
    store.close()
    backup_path = tmp_path / "snap.db"
    backup(src_db, backup_path)

    dest = tmp_path / "dest.db"
    dest.write_bytes(b"existing-db-bytes")

    result = restore(backup_path, dest, allow_overwrite=True)
    assert result.event_count == 2
    # The destination is now a valid SQLite DB (not the canary string).
    assert dest.read_bytes()[:16].startswith(b"SQLite format 3")


def test_backup_refuses_to_overwrite_existing_dest(tmp_path: Path):
    src_db = tmp_path / "src.db"
    store = _seed_store(src_db, n=1)
    store.close()
    target = tmp_path / "snap.db"
    target.write_bytes(b"do-not-clobber")

    with pytest.raises(BackupError, match="already exists"):
        backup(src_db, target)
    assert target.read_bytes() == b"do-not-clobber"


# ---- WAL companion invariant --------------------------------------------


def test_restore_refuses_when_wal_companion_present(tmp_path: Path):
    """A clean backup is one file — no `-wal` / `-shm` companions next to it."""
    src_db = tmp_path / "src.db"
    store = _seed_store(src_db, n=1)
    store.close()

    backup_path = tmp_path / "snap.db"
    backup(src_db, backup_path)
    # Simulate a hand-edited backup with WAL companion alongside it.
    (tmp_path / "snap.db-wal").write_bytes(b"")

    dest = tmp_path / "dest.db"
    with pytest.raises(BackupError, match="WAL companion"):
        restore(backup_path, dest)
    assert not dest.exists()


# ---- Hot backup: source can be open ------------------------------------


def test_backup_works_while_source_is_open(tmp_path: Path):
    """VACUUM INTO is a hot backup — the source store stays open and usable."""
    src_db = tmp_path / "src.db"
    store = _seed_store(src_db, n=2)
    backup_path = tmp_path / "snap.db"
    try:
        result = backup(src_db, backup_path)
        # Source is still writable.
        store.write(_session_created_event("sess_new"))
    finally:
        store.close()

    assert result.event_count == 2  # snapshot reflects pre-backup state
    # Re-open the source: the post-backup write landed and the snapshot
    # didn't see it.
    reopened = TraceStore(src_db)
    try:
        # 3 sessions total now.
        all_sessions = {e.session_id for e in reopened.events_for_session("sess_new")}
    finally:
        reopened.close()
    assert "sess_new" in all_sessions


def test_backup_missing_source_raises(tmp_path: Path):
    src_db = tmp_path / "nonexistent.db"
    with pytest.raises(BackupError, match="does not exist"):
        backup(src_db, tmp_path / "snap.db")


def test_restore_missing_source_raises(tmp_path: Path):
    with pytest.raises(BackupError, match="does not exist"):
        restore(tmp_path / "nope.db", tmp_path / "dest.db")


# ---- Large DB smoke -----------------------------------------------------


def test_backup_100k_events_under_five_seconds(tmp_path: Path):
    """Smoke test for the buyer-visible recipe: 100k events should snapshot
    in well under 5 seconds on a developer laptop. Not a load test."""
    src_db = tmp_path / "big.db"
    store = TraceStore(src_db)
    try:
        # Bulk insert via the store's own write path. ~100k rows fits in
        # ~10MB at ~100B/row JSON, well within SQLite's comfort zone.
        for i in range(100_000):
            store.write(_session_created_event(f"sess_{i % 256}"))
    finally:
        store.close()

    backup_path = tmp_path / "big-snap.db"
    started = time.perf_counter()
    result = backup(src_db, backup_path)
    elapsed = time.perf_counter() - started

    assert result.event_count == 100_000
    assert elapsed < 5.0, f"backup of 100k events took {elapsed:.2f}s (>5s budget)"
