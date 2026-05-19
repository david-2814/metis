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

from metis.core.events.bus import EventBus, EventFilter, Subscription, SubscriptionHandle
from metis.core.events.envelope import Actor, Event, Sensitivity
from metis.core.events.payloads import (
    AUDIT_EVENT_TYPES,
    PAYLOAD_REGISTRY,
    BusGapDetected,
    TraceSwept,
    make_event,
)
from metis.core.trace.retention import PurgeResult

# Trace-DB schema version. Stored in `PRAGMA user_version` on every opened
# trace DB so the backup/restore module (`metis.core.trace.backup`) can
# refuse to restore a backup whose schema doesn't match the running code.
# Bump in lockstep with breaking edits to `_SCHEMA` below. Wave 13's index
# additions are additive (CREATE INDEX IF NOT EXISTS) and do NOT bump the
# version — older code reading a Wave-13 DB simply ignores the new
# indexes; newer code reading an older DB picks them up on next open.
TRACE_SCHEMA_VERSION = 1

# WAL auto-checkpoint threshold in pages. SQLite's default is 1000 pages
# (~4 MB at the default 4 KB page size). Wave 13 raises this to 8192 (~32 MB)
# so a high-throughput burst doesn't trigger a checkpoint mid-burst — the
# checkpoint stalls writers while it copies pages from the WAL into the
# main DB. The trade-off is recovery time on hard crash: a 32 MB WAL
# replay on startup is still <1 s on local SSD. Operators with very tight
# crash-recovery SLAs can lower this via the `wal_autocheckpoint_pages`
# constructor argument; the default is safe for typical multi-tenant
# gateway loads. See docs/operations/trace-performance.md §WAL.
DEFAULT_WAL_AUTOCHECKPOINT_PAGES = 8192


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
-- Wave 13: composite (turn_id, id) eliminates the TEMP B-TREE FOR ORDER BY
-- the planner picks when serving `events_for_turn` (which always sorts by
-- id). The single-column `idx_events_turn` from v1 is left in place — its
-- presence is harmless and the additive contract requires that we don't
-- drop indexes from existing DBs (TRACE_SCHEMA_VERSION stays at 1).
CREATE INDEX IF NOT EXISTS idx_events_turn           ON events(turn_id);
CREATE INDEX IF NOT EXISTS idx_events_turn_id_id     ON events(turn_id, id);
CREATE INDEX IF NOT EXISTS idx_events_parent         ON events(parent_event_id);
-- Single-column timestamp index for the retention sweep
-- (trace-retention.md §4). Additive; existing DBs pick it up on next
-- open. The `(type, timestamp_us)` index does NOT serve `WHERE
-- timestamp_us < ?` cleanly because the planner would walk one range
-- per `type`, which is the opposite of what the sweep wants.
CREATE INDEX IF NOT EXISTS idx_events_timestamp_us   ON events(timestamp_us);

-- Wave 13: expression indexes on payload fields used by the multi-tenant
-- analytics rollups (gateway-key / user / team) and the GDPR portability
-- export. Without these, every cost-by-key query post-filters the entire
-- `llm.call_completed` slice in Python, and `user_export` does a full
-- table scan. The expressions match `analytics/store.py`'s queries
-- byte-for-byte so the planner picks them up. Partial WHERE clauses
-- skip rows whose stamp is null (agent-loop traffic, pre-multi-user
-- keys) — those are bucketed under the `null` row and don't benefit
-- from the index.
CREATE INDEX IF NOT EXISTS idx_events_gateway_key_id
    ON events(json_extract(payload_json, '$.gateway_key_id'))
    WHERE json_extract(payload_json, '$.gateway_key_id') IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_events_user_id
    ON events(json_extract(payload_json, '$.user_id'))
    WHERE json_extract(payload_json, '$.user_id') IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_events_team_id
    ON events(json_extract(payload_json, '$.team_id'))
    WHERE json_extract(payload_json, '$.team_id') IS NOT NULL;
-- Eval-quality slice: `/analytics/quality` always filters on type +
-- subject_kind. The composite expression index lets the planner serve
-- the combined predicate from a single index walk.
CREATE INDEX IF NOT EXISTS idx_events_eval_subject_kind
    ON events(json_extract(payload_json, '$.subject_kind'), timestamp_us)
    WHERE type = 'eval.completed';
"""


def _to_micros(ts: datetime) -> int:
    epoch = datetime(1970, 1, 1, tzinfo=ts.tzinfo)
    delta = ts - epoch
    return delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds


def _build_audit_clause(audit_types: frozenset[str]) -> tuple[str, tuple[str, ...]]:
    """Build ` AND type NOT IN (?, ?, ...)` plus its params.

    Returns `("", ())` when `audit_types` is empty so the caller's SQL
    doesn't end with a trailing AND. The leading space on the non-empty
    branch lets the caller concatenate without thinking about
    whitespace.
    """
    if not audit_types:
        return "", ()
    # Sort for deterministic SQL — easier to debug + cache plan.
    ordered = tuple(sorted(audit_types))
    placeholders = ",".join(["?"] * len(ordered))
    return f" AND type NOT IN ({placeholders})", ordered


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

    def __init__(
        self,
        db_path: str | Path,
        *,
        wal_autocheckpoint_pages: int = DEFAULT_WAL_AUTOCHECKPOINT_PAGES,
    ) -> None:
        self._db_path = str(db_path)
        self._conn = sqlite3.connect(self._db_path, isolation_level=None, check_same_thread=False)
        self._wal_autocheckpoint_pages = int(wal_autocheckpoint_pages)
        self._configure()
        self._conn.executescript(_SCHEMA)

    def _configure(self) -> None:
        # Mode commitment per §7.2: WAL + synchronous=NORMAL is required to
        # meet the fast-path budget. The durability trade-off (lose ~1s on
        # hard crash) is acceptable because events are not the system of
        # record for any user-visible state.
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = NORMAL")
        # Wave 13: bump WAL auto-checkpoint above SQLite's 1000-page default.
        # Larger window reduces checkpoint-induced writer stalls during
        # high-throughput bursts at the cost of a longer crash-recovery
        # replay. See docs/operations/trace-performance.md §WAL.
        self._conn.execute(f"PRAGMA wal_autocheckpoint = {self._wal_autocheckpoint_pages}")
        # Stamp the schema version so `trace.backup.restore()` can verify the
        # backup matches the running code. Cheap; runs once per open.
        self._conn.execute(f"PRAGMA user_version = {TRACE_SCHEMA_VERSION}")

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

    # ---- Retention -----------------------------------------------------

    def purge_older_than(
        self,
        cutoff: datetime,
        *,
        bus: EventBus | None = None,
        dry_run: bool = True,
        exempt_audit: bool = True,
    ) -> PurgeResult:
        """Delete events older than `cutoff` per trace-retention.md §3.

        Defaults to `dry_run=True` — the library-side caller must opt
        into actual deletion. The CLI inverts this default for operator
        ergonomics.

        Audit-flagged event types (owned by Wave 12a-1's
        `AUDIT_EVENT_TYPES` in `metis.core.events.payloads`) are excluded
        from the DELETE predicate so sweep history, key lifecycle
        records, and other audit-class events survive every sweep
        regardless of age. Tests can disable this via
        `exempt_audit=False` to verify raw timestamp math.

        On a non-dry-run sweep with `bus` provided, a single
        `trace.swept` event is emitted after the DELETE returns. In
        dry-run mode the event is NOT emitted (only the in-memory
        PurgeResult is returned).
        """
        cutoff_us = _to_micros(cutoff)
        audit_types = AUDIT_EVENT_TYPES if exempt_audit else frozenset()

        # Counts under the SAME predicate the DELETE will use, so dry-run
        # and apply report identical eligibility.
        audit_clause, audit_params = _build_audit_clause(audit_types)
        eligible_sql = f"SELECT COUNT(*) FROM events WHERE timestamp_us < ?{audit_clause}"
        rows_eligible = int(
            self._conn.execute(eligible_sql, (cutoff_us, *audit_params)).fetchone()[0]
        )

        # Count audit-exempt rows under the cutoff so the operator sees
        # how many old-but-preserved rows are sitting in the DB.
        if exempt_audit and audit_types:
            placeholders = ",".join(["?"] * len(audit_types))
            exempt_sql = (
                f"SELECT COUNT(*) FROM events WHERE timestamp_us < ? AND type IN ({placeholders})"
            )
            rows_audit_exempt = int(
                self._conn.execute(exempt_sql, (cutoff_us, *audit_params)).fetchone()[0]
            )
        else:
            rows_audit_exempt = 0

        if dry_run:
            rows_deleted = 0
        else:
            delete_sql = f"DELETE FROM events WHERE timestamp_us < ?{audit_clause}"
            cursor = self._conn.execute(delete_sql, (cutoff_us, *audit_params))
            # SQLite's `rowcount` after a DELETE returns the affected
            # row count under autocommit (no transaction wrapper here).
            rows_deleted = cursor.rowcount if cursor.rowcount >= 0 else rows_eligible

        oldest_kept_timestamp = self._oldest_event_timestamp()
        swept_at = datetime.now(UTC)

        result = PurgeResult(
            cutoff_timestamp=cutoff,
            rows_eligible=rows_eligible,
            rows_audit_exempt=rows_audit_exempt,
            rows_deleted=rows_deleted,
            oldest_kept_timestamp=oldest_kept_timestamp,
            dry_run=dry_run,
            swept_at=swept_at,
        )

        # Only emit on an actual sweep — dry-runs are silent on the bus
        # per trace-retention.md §3.3.
        if bus is not None and not dry_run:
            bus.emit(
                make_event(
                    type="trace.swept",
                    session_id="system",
                    actor=Actor.SYSTEM,
                    payload=TraceSwept(
                        rows_deleted=rows_deleted,
                        rows_audit_exempt=rows_audit_exempt,
                        cutoff_timestamp=cutoff,
                        oldest_kept_timestamp=oldest_kept_timestamp,
                        dry_run=False,
                        swept_at=swept_at,
                    ),
                    timestamp=swept_at,
                )
            )

        return result

    # ---- Maintenance ---------------------------------------------------

    def vacuum(self) -> int:
        """Reclaim free pages and defragment the DB. Returns reclaimed bytes.

        SQLite's `VACUUM` rebuilds the file in place and is safe under
        WAL — readers see the rebuilt DB on their next transaction.
        Documented operational pattern in
        `docs/operations/trace-performance.md §VACUUM`: run from a
        separate CronJob pod so the rebuild doesn't compete with the
        gateway/server's own writes. With `auto_vacuum=INCREMENTAL` set
        on a freshly-created DB (see `_configure`), this can be replaced
        by `incremental_vacuum`, which is cheaper but only available
        when `auto_vacuum` was set BEFORE any tables were created — for
        long-lived databases the only path is `VACUUM`.

        Returns the byte delta `(size_before - size_after)`. Negative
        deltas (rare) indicate the rebuild grew the file slightly to
        round up to a page boundary; the operator can ignore.
        """
        size_before = self._file_size_bytes()
        # `VACUUM` cannot run inside a transaction. With
        # `isolation_level=None` we're in autocommit so this is fine, but
        # the planner still raises if any prepared statement holds a
        # lock; this method assumes the caller has quiesced writes.
        self._conn.execute("VACUUM")
        size_after = self._file_size_bytes()
        return size_before - size_after

    def wal_checkpoint(self, *, mode: str = "PASSIVE") -> tuple[int, int, int]:
        """Run `PRAGMA wal_checkpoint(<mode>)`. Returns SQLite's tuple verbatim.

        SQLite returns `(busy, log_pages, checkpointed_pages)`:
          * `busy` is 0 on success, 1 if a writer was holding the lock
            (PASSIVE only — TRUNCATE / RESTART block until the writer
            releases).
          * `log_pages` is the WAL size in pages immediately before the
            checkpoint.
          * `checkpointed_pages` is how many pages were copied into the
            main DB.

        Default mode `PASSIVE` is non-blocking — it copies what it can
        without stalling writers and returns. `TRUNCATE` resets the WAL
        to zero bytes and is the right choice when an operator wants
        to reclaim disk after a large burst.
        """
        normalized = mode.strip().upper()
        if normalized not in ("PASSIVE", "FULL", "RESTART", "TRUNCATE"):
            raise ValueError(f"unknown wal_checkpoint mode: {mode!r}")
        row = self._conn.execute(f"PRAGMA wal_checkpoint({normalized})").fetchone()
        # SQLite returns a tuple of three ints; defensive-coerce in case a
        # test harness shims the connection.
        return (int(row[0]), int(row[1]), int(row[2]))

    def wal_size_bytes(self) -> int:
        """Return the current WAL file size in bytes (0 if no WAL file).

        Backs the `metis_trace_wal_bytes` Prometheus gauge in
        `metis.core.observability.metrics`. Returning 0 (rather than
        raising) when the WAL doesn't exist matches the operational
        expectation: a freshly-opened DB or a checkpointed-then-deleted
        WAL is a healthy state, not a missing file.
        """
        wal_path = Path(self._db_path + "-wal")
        try:
            return wal_path.stat().st_size
        except FileNotFoundError:
            return 0

    def _file_size_bytes(self) -> int:
        try:
            return Path(self._db_path).stat().st_size
        except FileNotFoundError:
            return 0

    def _oldest_event_timestamp(self) -> datetime | None:
        row = self._conn.execute("SELECT MIN(timestamp_us) FROM events").fetchone()
        if row is None or row[0] is None:
            return None
        return datetime.fromtimestamp(int(row[0]) / 1_000_000, tz=UTC)

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
