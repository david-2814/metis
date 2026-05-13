"""Helpers for seeding the analytics test DB.

The AnalyticsStore reads SQL directly; tests insert rows directly rather than
going through the full SessionManager / TraceStore stack so each test isolates
exactly the data shape it cares about.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

# Schema fragments copied verbatim from trace/store.py and sessions/sqlite_store.py.
# Duplicating is preferable to importing — these tests should fail loudly if the
# real schemas drift and break analytics assumptions.
_EVENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
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

CREATE INDEX IF NOT EXISTS idx_events_session_id     ON events(session_id, id);
CREATE INDEX IF NOT EXISTS idx_events_type_timestamp ON events(type, timestamp_us);
CREATE INDEX IF NOT EXISTS idx_events_turn           ON events(turn_id);
CREATE INDEX IF NOT EXISTS idx_events_parent         ON events(parent_event_id);
"""

_SESSIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
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

CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  role TEXT NOT NULL,
  content_json TEXT NOT NULL,
  metadata_json TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  schema_version INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_session_created
    ON messages(session_id, created_at);
"""


def _to_micros(dt: datetime) -> int:
    epoch = datetime(1970, 1, 1, tzinfo=dt.tzinfo)
    delta = dt - epoch
    return delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds


class DBSeeder:
    """Thin write helper that mirrors what TraceStore / SqliteSessionStore do."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self._event_seq = 0

    def insert_event(
        self,
        *,
        event_type: str,
        payload: dict,
        timestamp: datetime,
        session_id: str = "sess_test",
        turn_id: str | None = None,
        parent_event_id: str | None = None,
        actor: str = "system",
        sensitivity: str = "pseudonymous",
        event_id: str | None = None,
    ) -> str:
        self._event_seq += 1
        eid = event_id or f"01HZ{self._event_seq:020d}"
        self.conn.execute(
            "INSERT INTO events "
            "(id, timestamp_us, session_id, turn_id, parent_event_id, type, "
            " actor, sensitivity, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                eid,
                _to_micros(timestamp),
                session_id,
                turn_id,
                parent_event_id,
                event_type,
                actor,
                sensitivity,
                json.dumps(payload, default=str),
            ),
        )
        return eid

    def insert_llm_call_completed(
        self,
        *,
        timestamp: datetime,
        model: str,
        provider: str,
        cost_usd: float | str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cached_input_tokens: int = 0,
        cache_creation_input_tokens: int = 0,
        latency_ms: int = 1000,
        session_id: str = "sess_test",
        turn_id: str | None = None,
    ) -> str:
        return self.insert_event(
            event_type="llm.call_completed",
            timestamp=timestamp,
            session_id=session_id,
            turn_id=turn_id,
            actor="agent",
            payload={
                "model": model,
                "provider": provider,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cached_input_tokens": cached_input_tokens,
                "cache_creation_input_tokens": cache_creation_input_tokens,
                "cost_usd": cost_usd,
                "pricing_version": "test-1",
                "latency_ms": latency_ms,
                "stop_reason": "end_turn",
                "produced_tool_calls": 0,
                "produced_thinking_blocks": 0,
            },
        )

    def insert_llm_call_failed(
        self,
        *,
        timestamp: datetime,
        model: str,
        provider: str,
        error_class: str,
        session_id: str = "sess_test",
        turn_id: str | None = None,
    ) -> str:
        return self.insert_event(
            event_type="llm.call_failed",
            timestamp=timestamp,
            session_id=session_id,
            turn_id=turn_id,
            actor="agent",
            payload={
                "model": model,
                "provider": provider,
                "error_class": error_class,
                "error_message_redacted": "boom",
                "retry_count": 0,
                "latency_ms": 0,
            },
        )

    def insert_route_decided(
        self,
        *,
        timestamp: datetime,
        chosen_model: str,
        winner_index: int,
        chain: list[dict],
        session_id: str = "sess_test",
        turn_id: str | None = None,
    ) -> str:
        return self.insert_event(
            event_type="route.decided",
            timestamp=timestamp,
            session_id=session_id,
            turn_id=turn_id,
            payload={
                "chosen_model": chosen_model,
                "winner_index": winner_index,
                "elapsed_ms": 1.0,
                "chain": chain,
            },
        )

    def insert_session(
        self,
        *,
        session_id: str,
        workspace_path: str = "/tmp/ws",
        active_model: str | None = "anthropic:claude-sonnet-4-6",
        cost_so_far_usd: float = 0.0,
        turn_count: int = 0,
        created_at: datetime,
        updated_at: datetime | None = None,
    ) -> None:
        ts = updated_at or created_at
        self.conn.execute(
            "INSERT INTO sessions "
            "(id, workspace_path, active_model, routing_policy_json, "
            " cost_so_far_usd, turn_count, schema_version, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                workspace_path,
                active_model,
                None,
                cost_so_far_usd,
                turn_count,
                1,
                _to_micros(created_at),
                _to_micros(ts),
            ),
        )

    def insert_message(
        self,
        *,
        message_id: str,
        session_id: str,
        role: str,
        content: list[dict],
        metadata: dict,
        created_at: datetime,
    ) -> None:
        self.conn.execute(
            "INSERT INTO messages "
            "(id, session_id, role, content_json, metadata_json, "
            " created_at, schema_version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                message_id,
                session_id,
                role,
                json.dumps(content),
                json.dumps(metadata, default=str),
                _to_micros(created_at),
                1,
            ),
        )


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "analytics.db"


@pytest.fixture
def seeded_db(db_path: Path) -> tuple[Path, DBSeeder]:
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(_EVENTS_SCHEMA)
    conn.executescript(_SESSIONS_SCHEMA)
    seeder = DBSeeder(conn)
    try:
        yield db_path, seeder
    finally:
        conn.close()


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 5, 12, 12, 0, 0, tzinfo=UTC)
