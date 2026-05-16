"""Forward-compat tests for `SqliteSessionStore`.

A pre-Wave-10 session DB doesn't have the delegation columns
(`parent_session_id`, `parent_tool_use_id`, `is_worker`). The current store
backfills them via `_migrate_sessions_table()` on open. The partial index
`idx_sessions_parent` referencing the new column is created by the migration
(NOT by `executescript(_SCHEMA)`), so opening a pre-Wave-10 DB doesn't error
on a missing column at index-create time.

This is the upgrade-in-place contract documented in
`docs/operations/upgrade-guide.md`.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from metis_core.sessions.sqlite_store import SqliteSessionStore

# Pre-Wave-10 schema (delegation columns absent).
_PRE_WAVE10_SCHEMA = """
CREATE TABLE sessions (
  id TEXT PRIMARY KEY,
  workspace_path TEXT NOT NULL,
  active_model TEXT,
  routing_policy_json TEXT,
  cost_so_far_usd REAL NOT NULL DEFAULT 0,
  turn_count INTEGER NOT NULL DEFAULT 0,
  schema_version INTEGER NOT NULL,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE TABLE messages (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  role TEXT NOT NULL,
  content_json TEXT NOT NULL,
  metadata_json TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  schema_version INTEGER NOT NULL
);
CREATE INDEX idx_messages_session_created ON messages(session_id, created_at);
"""


def _seed_pre_wave10_db(path: Path) -> None:
    conn = sqlite3.connect(str(path), isolation_level=None)
    try:
        conn.executescript(_PRE_WAVE10_SCHEMA)
        conn.execute(
            "INSERT INTO sessions("
            "id, workspace_path, active_model, routing_policy_json, "
            "cost_so_far_usd, turn_count, schema_version, created_at, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("sess_legacy", "/ws", "haiku", '{"version":"v1"}', 0.0, 0, 1, 1700000000, 1700000000),
        )
    finally:
        conn.close()


@pytest.fixture
def legacy_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "session.db"
    _seed_pre_wave10_db(db_path)
    return db_path


def test_open_legacy_db_backfills_delegation_columns(legacy_db: Path) -> None:
    store = SqliteSessionStore(legacy_db)
    try:
        cols = {r[1] for r in store._conn.execute("PRAGMA table_info(sessions)").fetchall()}
        assert {"parent_session_id", "parent_tool_use_id", "is_worker"}.issubset(cols)
    finally:
        store.close()


def test_open_legacy_db_creates_parent_index(legacy_db: Path) -> None:
    store = SqliteSessionStore(legacy_db)
    try:
        names = {
            r[0]
            for r in store._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='sessions'"
            ).fetchall()
        }
        assert "idx_sessions_parent" in names
    finally:
        store.close()


def test_legacy_rows_survive_with_default_is_worker(legacy_db: Path) -> None:
    store = SqliteSessionStore(legacy_db)
    try:
        row = store._conn.execute(
            "SELECT id, is_worker, parent_session_id FROM sessions WHERE id = ?",
            ("sess_legacy",),
        ).fetchone()
        assert row == ("sess_legacy", 0, None)
    finally:
        store.close()


def test_legacy_db_accepts_new_worker_session(legacy_db: Path) -> None:
    store = SqliteSessionStore(legacy_db)
    try:
        sess = store.create_session(
            workspace_path="/ws",
            active_model="haiku",
            parent_session_id="sess_legacy",
            parent_tool_use_id="tu_42",
            is_worker=True,
        )
        row = store._conn.execute(
            "SELECT parent_session_id, parent_tool_use_id, is_worker FROM sessions WHERE id = ?",
            (sess.id,),
        ).fetchone()
        assert row == ("sess_legacy", "tu_42", 1)
    finally:
        store.close()
