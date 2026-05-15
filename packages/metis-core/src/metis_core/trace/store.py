"""SQLite-backed trace store.

Schema and mode commitments per event-bus-and-trace-catalog.md §7:
- WAL journal_mode + synchronous=NORMAL (required for fast-path budget).
- timestamp_us as microseconds (wall-clock accuracy; ordering via id).
- payload stored as JSON text.
- Indexes on (session_id, id), (type, timestamp_us), (turn_id), (parent_event_id).

Payloads are serialized via `msgspec.json.encode` and re-typed on read by
looking up the registered Struct class in `PAYLOAD_REGISTRY`. This restores
fields like `datetime` that JSON cannot represent natively — without the
re-typing, `tool.confirmation_requested.expires_at` would stay an ISO string
forever, even though the catalog declares it `datetime`.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path

import msgspec

from metis_core.events.bus import EventBus, EventFilter, Subscription, SubscriptionHandle
from metis_core.events.envelope import Actor, Event, Sensitivity
from metis_core.events.payloads import PAYLOAD_REGISTRY, BusGapDetected, make_event

# Default scan bound for `detect_gaps` / `scan_for_gaps_and_emit`. Spec §6.10
# documents this as a startup health-check; older events fall out of the
# window. 10k is well above typical single-user activity and keeps the scan
# cheap (a single indexed query plus an O(n) walk).
DEFAULT_GAP_SCAN_LIMIT = 10_000

# Default wall-clock threshold for flagging a gap (microseconds). Events
# within a single turn are emitted from the agent loop in rapid succession;
# a multi-second silence within one turn_id is strong evidence of dropped
# events. Cross-turn pauses (user thinking between prompts) are normal and
# deliberately not flagged — only intra-turn gaps are reported.
DEFAULT_GAP_THRESHOLD_US = 60_000_000


@dataclass(frozen=True)
class GapInfo:
    """A detected hole in a session's per-turn event stream.

    See event-bus-and-trace-catalog.md §6.10 `bus.gap_detected`. The bounds
    `gap_start_id` / `gap_end_id` are the last persisted event before the
    gap and the first persisted event after, respectively.
    `estimated_missing_count` is a rough estimate from the time delta — the
    missing events are by definition gone, so it's not exact.
    """

    session_id: str
    gap_start_id: str
    gap_end_id: str
    estimated_missing_count: int


# Schema per event-bus-and-trace-catalog.md §7.1. The FK on `session_id`
# references the `sessions` table owned by `SqliteSessionStore` (defined in
# canonical-message-format.md §9.1) — both tables share the same SQLite DB
# in v1. The FK is declared so the schema matches the spec and tooling
# (`sqlite3` introspection, future migrations) sees the relationship;
# enforcement is left at SQLite's default (`PRAGMA foreign_keys = OFF`)
# because the trace store may open the DB before the sessions table is
# created (e.g. in unit tests that don't construct a session store).
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
  payload_json TEXT NOT NULL,
  FOREIGN KEY (session_id) REFERENCES sessions(id)
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


def _decode_payload(event_type: str, payload_json: str) -> dict:
    """Decode a stored payload back to a dict with catalog-typed fields restored.

    The payload is decoded into its registered Struct first (so msgspec parses
    ISO strings into `datetime`, etc.), then converted back to a dict with
    `datetime` kept as a builtin. Unknown event types fall back to a raw JSON
    decode so unrecognized rows still surface as dicts rather than raising.
    """
    entry = PAYLOAD_REGISTRY.get(event_type)
    if entry is None:
        return msgspec.json.decode(payload_json)
    struct_class, _ = entry
    struct = msgspec.json.decode(payload_json, type=struct_class)
    return msgspec.to_builtins(struct, builtin_types=(datetime,))


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
                msgspec.json.encode(event.payload).decode("utf-8"),
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

    # ---- Gap detection -------------------------------------------------

    def detect_gaps(
        self,
        *,
        limit: int = DEFAULT_GAP_SCAN_LIMIT,
        threshold_us: int = DEFAULT_GAP_THRESHOLD_US,
    ) -> list[GapInfo]:
        """Scan recent events for holes in per-session event streams.

        Spec §6.10: a "gap" is a consecutive pair of events in the same
        session and turn whose ULID timestamps differ by more than
        `threshold_us`. Within a single turn, events come from the agent
        loop in rapid succession; a multi-second silence is strong evidence
        of dropped events (typically a trace-store crash mid-write).

        Cross-turn boundaries are NOT considered gaps — natural user-thinking
        pauses between turns would generate false positives. Events with
        `turn_id IS NULL` (session-level lifecycle events) are also skipped
        for the same reason.

        The scan is bounded to the most recent `limit` events to keep
        startup cheap; older gaps go undetected, which is acceptable for a
        startup health-check.
        """
        cursor = self._conn.execute(
            "SELECT id, session_id, turn_id, timestamp_us FROM events ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        rows = cursor.fetchall()
        # Group by session, then walk each session in ascending ULID order.
        by_session: dict[str, list[tuple[str, str | None, int]]] = {}
        for event_id, session_id, turn_id, ts_us in rows:
            by_session.setdefault(session_id, []).append((event_id, turn_id, ts_us))
        gaps: list[GapInfo] = []
        for session_id, events in by_session.items():
            events.sort(key=lambda r: r[0])  # ULIDs sort lexicographically
            for prev, curr in pairwise(events):
                prev_id, prev_turn, prev_ts = prev
                curr_id, curr_turn, curr_ts = curr
                # Only intra-turn gaps count; skip null/cross-turn boundaries.
                if prev_turn is None or curr_turn is None or prev_turn != curr_turn:
                    continue
                delta_us = curr_ts - prev_ts
                if delta_us <= threshold_us:
                    continue
                # Estimate: assume one event every ~50ms during active turn work.
                # This is rough; the missing events are gone, so we can't be exact.
                estimated = max(1, delta_us // 50_000)
                gaps.append(
                    GapInfo(
                        session_id=session_id,
                        gap_start_id=prev_id,
                        gap_end_id=curr_id,
                        estimated_missing_count=int(estimated),
                    )
                )
        return gaps

    def scan_for_gaps_and_emit(
        self,
        bus: EventBus,
        *,
        limit: int = DEFAULT_GAP_SCAN_LIMIT,
        threshold_us: int = DEFAULT_GAP_THRESHOLD_US,
    ) -> int:
        """Run `detect_gaps` and emit a `bus.gap_detected` event per gap.

        Spec §6.10: this is the startup health-check. Callers wire it after
        attaching the trace store to the bus and before any session work
        begins so gap events flow through the normal dispatch path. Returns
        the number of gap events emitted.
        """
        gaps = self.detect_gaps(limit=limit, threshold_us=threshold_us)
        now = datetime.now(UTC)
        for gap in gaps:
            bus.emit(
                make_event(
                    type="bus.gap_detected",
                    session_id=gap.session_id,
                    actor=Actor.SYSTEM,
                    payload=BusGapDetected(
                        session_id=gap.session_id,
                        gap_start_id=gap.gap_start_id,
                        gap_end_id=gap.gap_end_id,
                        estimated_missing_count=gap.estimated_missing_count,
                        detected_at=now,
                    ),
                    timestamp=now,
                )
            )
        return len(gaps)

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
            payload=_decode_payload(cols["type"], cols["payload_json"]),
        )

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> TraceStore:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()
