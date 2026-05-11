"""SQLite-backed session store.

Schema per canonical-message-format.md §9.1. Shares the same SQLite file as
the trace store in v1 (per §7.1 note).

This v1 implementation skips the `tool_calls` denormalized table — tool calls
are already discoverable via the JSON content of ASSISTANT and TOOL messages.
Adding it later is non-breaking; queries like "find unanswered tool calls"
become cheaper but aren't yet load-bearing.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import msgspec

from metis.canonical.content import ContentBlock
from metis.canonical.ids import new_session_id
from metis.canonical.messages import (
    SCHEMA_VERSION,
    Message,
    MessageMetadata,
    Role,
)
from metis.sessions.store import Session

_SCHEMA = """
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
  schema_version INTEGER NOT NULL,
  FOREIGN KEY (session_id) REFERENCES sessions(id)
);

CREATE INDEX IF NOT EXISTS idx_messages_session_created
    ON messages(session_id, created_at);
"""


class SqliteSessionStore:
    """Persistent SessionStore backed by SQLite.

    Implements the same Protocol as InMemorySessionStore. Opens its own
    connection; can share the DB file with the TraceStore.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._conn = sqlite3.connect(self._db_path, isolation_level=None, check_same_thread=False)
        # Same mode commitments as the trace store: WAL + NORMAL.
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._conn.executescript(_SCHEMA)
        self._content_encoder = msgspec.json.Encoder()
        self._metadata_encoder = msgspec.json.Encoder()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> SqliteSessionStore:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # ---- SessionStore protocol ---------------------------------------

    def create_session(self, *, workspace_path: str, active_model: str | None = None) -> Session:
        now = _now()
        session_id = new_session_id()
        self._conn.execute(
            "INSERT INTO sessions "
            "(id, workspace_path, active_model, routing_policy_json, "
            "cost_so_far_usd, turn_count, schema_version, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                workspace_path,
                active_model,
                None,
                0.0,
                0,
                SCHEMA_VERSION,
                _to_micros(now),
                _to_micros(now),
            ),
        )
        return Session(
            id=session_id,
            workspace_path=workspace_path,
            active_model=active_model,
            created_at=now,
            cost_so_far_usd=0.0,
            turn_count=0,
        )

    def get_session(self, session_id: str) -> Session:
        cursor = self._conn.execute(
            "SELECT id, workspace_path, active_model, cost_so_far_usd, turn_count, created_at "
            "FROM sessions WHERE id = ?",
            (session_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise KeyError(session_id)
        return _row_to_session(row)

    def list_sessions(self) -> list[Session]:
        cursor = self._conn.execute(
            "SELECT id, workspace_path, active_model, cost_so_far_usd, turn_count, created_at "
            "FROM sessions ORDER BY created_at DESC"
        )
        return [_row_to_session(row) for row in cursor.fetchall()]

    def update_session(self, session: Session) -> None:
        now = _now()
        self._conn.execute(
            "UPDATE sessions SET workspace_path = ?, active_model = ?, "
            "cost_so_far_usd = ?, turn_count = ?, updated_at = ? "
            "WHERE id = ?",
            (
                session.workspace_path,
                session.active_model,
                session.cost_so_far_usd,
                session.turn_count,
                _to_micros(now),
                session.id,
            ),
        )

    def add_message(self, session_id: str, message: Message) -> None:
        content_json = self._content_encoder.encode(message.content).decode()
        metadata_json = self._metadata_encoder.encode(message.metadata).decode()
        self._conn.execute(
            "INSERT INTO messages "
            "(id, session_id, role, content_json, metadata_json, created_at, schema_version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                message.id,
                session_id,
                message.role.value,
                content_json,
                metadata_json,
                _to_micros(message.created_at),
                message.schema_version,
            ),
        )

    def get_messages(self, session_id: str) -> list[Message]:
        cursor = self._conn.execute(
            "SELECT id, session_id, role, content_json, metadata_json, "
            "created_at, schema_version "
            "FROM messages WHERE session_id = ? ORDER BY created_at, id",
            (session_id,),
        )
        return [_row_to_message(row) for row in cursor.fetchall()]


# ---- helpers ---------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


def _to_micros(ts: datetime) -> int:
    epoch = datetime(1970, 1, 1, tzinfo=ts.tzinfo)
    delta = ts - epoch
    return delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds


def _from_micros(us: int) -> datetime:
    return datetime.fromtimestamp(us / 1_000_000, tz=UTC)


def _row_to_session(row) -> Session:
    return Session(
        id=row[0],
        workspace_path=row[1],
        active_model=row[2],
        cost_so_far_usd=row[3],
        turn_count=row[4],
        created_at=_from_micros(row[5]),
    )


def _row_to_message(row) -> Message:
    content = msgspec.json.decode(row[3].encode(), type=list[ContentBlock])
    metadata = msgspec.json.decode(row[4].encode(), type=MessageMetadata)
    return Message(
        id=row[0],
        session_id=row[1],
        role=Role(row[2]),
        content=content,
        metadata=metadata,
        created_at=_from_micros(row[5]),
        schema_version=row[6],
    )
