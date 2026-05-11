"""SQLite-backed trace store.

Schema and mode commitments per event-bus-and-trace-catalog.md §7:
- WAL journal_mode + synchronous=NORMAL (required for fast-path budget).
- timestamp_us as microseconds (wall-clock accuracy; ordering via id).
- payload stored as JSON text.
- Indexes on (session_id, id), (type, timestamp_us), (turn_id), (parent_event_id).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from metis.events.bus import EventBus, EventFilter, Subscription, SubscriptionHandle
from metis.events.envelope import Actor, Event, Sensitivity

_SCHEMA = """
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


def _to_micros(ts: datetime) -> int:
    epoch = datetime(1970, 1, 1, tzinfo=ts.tzinfo)
    delta = ts - epoch
    return delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds


class TraceStore:
    """Append-only SQLite log of bus events.

    Open with `TraceStore(path)`, register as a bus subscriber via
    `attach_to(bus)`, and query via `events_for_session`.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._conn = sqlite3.connect(self._db_path, isolation_level=None, check_same_thread=False)
        self._configure()
        self._conn.executescript(_SCHEMA)

    def _configure(self) -> None:
        # Mode commitment per §7.2: WAL + synchronous=NORMAL is required to
        # meet the fast-path budget. The durability trade-off (lose ~1s on
        # hard crash) is acceptable because events are not the system of
        # record for any user-visible state.
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = NORMAL")

    # ---- Writes --------------------------------------------------------

    def write(self, event: Event) -> None:
        self._conn.execute(
            "INSERT INTO events "
            "(id, timestamp_us, session_id, turn_id, parent_event_id, type, actor, sensitivity, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event.id,
                _to_micros(event.timestamp),
                event.session_id,
                event.turn_id,
                event.parent_event_id,
                event.type,
                event.actor.value,
                event.sensitivity.value,
                json.dumps(event.payload, default=str),
            ),
        )

    async def handle(self, event: Event) -> None:
        """Bus-compatible async handler. Writes are synchronous SQLite calls
        which under WAL+NORMAL typically complete in sub-millisecond."""
        self.write(event)

    def attach_to(self, bus: EventBus, name: str = "trace-store") -> SubscriptionHandle:
        """Register self as a fast-path subscriber on the bus."""
        return bus.subscribe(
            Subscription(
                filter=EventFilter(),
                handler=self.handle,
                name=name,
                fast_path=True,
            )
        )

    # ---- Reads ---------------------------------------------------------

    def events_for_session(
        self,
        session_id: str,
        *,
        since_id: str | None = None,
        limit: int | None = None,
    ) -> list[Event]:
        """Return events for a session in id (ULID) order.

        `since_id` is exclusive: returned events have id > since_id.
        Used by streaming-protocol §3.6 replay-on-reconnect.
        """
        sql = "SELECT * FROM events WHERE session_id = ?"
        params: list[object] = [session_id]
        if since_id is not None:
            sql += " AND id > ?"
            params.append(since_id)
        sql += " ORDER BY id"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        cursor = self._conn.execute(sql, params)
        return [self._row_to_event(row, cursor.description) for row in cursor.fetchall()]

    def events_for_turn(self, turn_id: str) -> list[Event]:
        cursor = self._conn.execute(
            "SELECT * FROM events WHERE turn_id = ? ORDER BY id", (turn_id,)
        )
        return [self._row_to_event(row, cursor.description) for row in cursor.fetchall()]

    def causal_chain(self, leaf_event_id: str) -> list[Event]:
        """Walk parent_event_id from leaf back to root; return root-first."""
        chain: list[Event] = []
        current = leaf_event_id
        while current is not None:
            cursor = self._conn.execute("SELECT * FROM events WHERE id = ?", (current,))
            row = cursor.fetchone()
            if row is None:
                break
            event = self._row_to_event(row, cursor.description)
            chain.append(event)
            current = event.parent_event_id
        return list(reversed(chain))

    def count_by_type(self, event_type: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM events WHERE type = ?", (event_type,)
        ).fetchone()
        return int(row[0])

    # ---- Helpers -------------------------------------------------------

    def _row_to_event(self, row: Iterable, description: list) -> Event:
        cols = {desc[0]: value for desc, value in zip(description, row, strict=True)}
        ts_us = cols["timestamp_us"]
        # Reconstruct UTC datetime from microseconds-since-epoch.
        from datetime import UTC

        timestamp = datetime.fromtimestamp(ts_us / 1_000_000, tz=UTC)
        return Event(
            id=cols["id"],
            timestamp=timestamp,
            session_id=cols["session_id"],
            turn_id=cols["turn_id"],
            parent_event_id=cols["parent_event_id"],
            type=cols["type"],
            actor=Actor(cols["actor"]),
            sensitivity=Sensitivity(cols["sensitivity"]),
            payload=json.loads(cols["payload_json"]),
        )

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> TraceStore:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()
