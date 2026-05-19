"""Forward-compat tests for `TraceStore` schema_version handling.

A pre-Wave-10 trace DB that was opened by code which had not yet stamped
`PRAGMA user_version` must still be readable, writable, and roundtrippable
by the current `TraceStore`. This is the upgrade-in-place contract documented
in `docs/operations/upgrade-guide.md`.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from metis.core.events.envelope import Actor
from metis.core.events.payloads import SessionCreated, make_event
from metis.core.trace.store import TRACE_SCHEMA_VERSION, TraceStore

# Pre-Wave-10 schema, captured verbatim from the pre-stamp DDL. The Wave 10
# change was additive (added the `PRAGMA user_version` stamp); the columns
# and indexes are unchanged.
_PRE_WAVE10_SCHEMA = """
CREATE TABLE events (
  id TEXT PRIMARY KEY,
  timestamp_us INTEGER NOT NULL,
  session_id TEXT NOT NULL,
  turn_id TEXT,
  parent_event_id TEXT,
  type TEXT NOT NULL,
  actor TEXT NOT NULL,
  sensitivity TEXT NOT NULL,
  payload_json TEXT NOT NULL
);
CREATE INDEX idx_events_session_id     ON events(session_id, id);
CREATE INDEX idx_events_type_timestamp ON events(type, timestamp_us);
CREATE INDEX idx_events_turn           ON events(turn_id);
CREATE INDEX idx_events_parent         ON events(parent_event_id);
"""


def _seed_pre_wave10_db(path: Path) -> None:
    """Lay down a trace DB that looks like one written by pre-Wave-10 code."""
    conn = sqlite3.connect(str(path), isolation_level=None)
    try:
        conn.executescript(_PRE_WAVE10_SCHEMA)
        conn.execute(
            "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "01HEXAMPLEPREWAVE10ID0000",
                1_700_000_000_000_000,
                "sess_legacy",
                None,
                None,
                "session.created",
                "system",
                "pseudonymous",
                '{"workspace_path":"/legacy","workspace_hash":"h","initial_active_model":null,'
                '"routing_policy_version":"v"}',
            ),
        )
    finally:
        conn.close()


def test_legacy_db_gets_schema_stamp_on_open(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    _seed_pre_wave10_db(db_path)

    pre_open = sqlite3.connect(str(db_path))
    try:
        assert pre_open.execute("PRAGMA user_version").fetchone()[0] == 0
    finally:
        pre_open.close()

    store = TraceStore(db_path)
    try:
        stamped = store._conn.execute("PRAGMA user_version").fetchone()[0]
        assert stamped == TRACE_SCHEMA_VERSION
    finally:
        store._conn.close()


def test_legacy_rows_survive_open(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    _seed_pre_wave10_db(db_path)

    store = TraceStore(db_path)
    try:
        rows = store._conn.execute("SELECT id, type FROM events").fetchall()
        assert rows == [("01HEXAMPLEPREWAVE10ID0000", "session.created")]
    finally:
        store._conn.close()


def test_legacy_db_accepts_new_writes(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    _seed_pre_wave10_db(db_path)

    store = TraceStore(db_path)
    try:
        event = make_event(
            type="session.created",
            session_id="sess_post_upgrade",
            actor=Actor.SYSTEM,
            payload=SessionCreated(
                workspace_path="/new",
                workspace_hash="h2",
                initial_active_model=None,
                routing_policy_version="v",
            ),
            timestamp=datetime.now(UTC),
        )
        store.write(event)
        ids = sorted(r[0] for r in store._conn.execute("SELECT id FROM events").fetchall())
        assert "01HEXAMPLEPREWAVE10ID0000" in ids
        assert any(i.startswith("01") and i != "01HEXAMPLEPREWAVE10ID0000" for i in ids)
    finally:
        store._conn.close()
