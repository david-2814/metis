"""Audit log reader / exporter.

`AuditLog` is a thin filter over a `TraceStore` SQLite connection that selects
the audit subset of events (per `audit-log.md §4`) and exports them
deterministically as JSONL or CSV.

The export is byte-deterministic for the same (window, registry, DB) tuple:
- Rows are ordered by ULID `id` (lexicographic = timestamp + per-process
  monotonic counter).
- JSON field order is preserved by msgspec from the registered payload class.
- No timestamps are embedded in the file beyond the events' own.

This module never writes to the trace DB — read-only contract.
"""

from __future__ import annotations

import csv
import io
import sqlite3
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import msgspec

from metis_core.analytics.windows import TimeWindow
from metis_core.events.envelope import Actor, Event, Sensitivity
from metis_core.events.payloads import AUDIT_EVENT_TYPES, PAYLOAD_REGISTRY
from metis_core.trace.store import TraceStore

if TYPE_CHECKING:
    from metis_core.redaction import EventRedactor

ExportFormat = Literal["jsonl", "csv"]

# Fixed CSV header order; stable across catalog evolution. Buyers ingesting
# this format SHOULD re-parse the `payload_json` column to access per-payload
# fields — flattening into per-type columns would force a re-header on every
# additive payload change.
_CSV_HEADER: tuple[str, ...] = (
    "id",
    "timestamp",
    "session_id",
    "turn_id",
    "parent_event_id",
    "type",
    "actor",
    "sensitivity",
    "payload_json",
)


@dataclass(frozen=True)
class AuditExportResult:
    """Metadata captured at export time. Returned to callers and printed by
    the CLI so the operator has a paper trail without re-opening the file.

    Same shape posture as `BackupResult` (trace/backup.py) — deterministic,
    no random ids.
    """

    dest_path: Path
    format: ExportFormat
    event_count: int
    window_start: datetime
    window_end: datetime
    oldest_event_id: str | None
    newest_event_id: str | None
    byte_count: int


class AuditLog:
    """Filtered projection of the trace store.

    See `audit-log.md §5` (Storage) and `§8` (API surface). One physical
    SQLite store, two logical tiers: this class reads the audit subset.
    Retention sweeps (12a-2) filter the operational subset for deletion;
    audit rows are preserved by construction.
    """

    def __init__(self, trace: TraceStore) -> None:
        self._trace = trace
        # Reach through to the trace store's connection. The audit log is a
        # read-only consumer of the same DB; opening a second connection
        # would burn an FD without giving us anything (SQLite WAL allows
        # concurrent reads, but we run sync on the main thread).
        self._conn: sqlite3.Connection = trace._conn

    # ---- Query --------------------------------------------------------

    def query(
        self,
        *,
        window: TimeWindow,
        event_types: Iterable[str] | None = None,
    ) -> Iterator[Event]:
        """Yield audit events in the window, ordered by event id ascending.

        `event_types` is intersected with `AUDIT_EVENT_TYPES` — passing a
        non-audit type silently filters it out rather than including it
        (audit-log.md §8.1). The default is the full audit subset.
        """
        wanted = _resolve_event_types(event_types)
        if not wanted:
            return iter(())
        placeholders = ",".join("?" * len(wanted))
        sql = (
            "SELECT id, timestamp_us, session_id, turn_id, parent_event_id, "
            "type, actor, sensitivity, payload_json "
            "FROM events "
            f"WHERE type IN ({placeholders}) "
            "AND timestamp_us >= ? AND timestamp_us < ? "
            "ORDER BY id"
        )
        params: list[object] = [*sorted(wanted), window.start_us, window.end_us]
        cursor = self._conn.execute(sql, params)
        return (_row_to_event(row) for row in cursor.fetchall())

    # ---- Export -------------------------------------------------------

    def export(
        self,
        dest: Path,
        *,
        window: TimeWindow,
        format: ExportFormat = "jsonl",
        event_types: Iterable[str] | None = None,
        redactor: EventRedactor | None = None,
    ) -> AuditExportResult:
        """Write the audit events in `window` to `dest` in `format`.

        Refuses to overwrite an existing destination — the operator should
        delete or move the previous file first. Determinism (audit-log.md
        §7.3) means the same window twice produces byte-identical output;
        accidentally clobbering a checksum-anchored export is bad ergonomics.

        When `redactor` is provided, every event is passed through
        `redactor.redact(event)` before serialization (per redaction.md §9).
        In `AGGREGATE_ONLY` mode the row stream is dropped and the format
        is forced to a single-object JSON dump (the JSONL / CSV row shape
        does not fit an aggregate; callers wanting per-row output should
        choose a non-aggregate mode).
        """
        dest = Path(dest)
        if dest.exists():
            raise FileExistsError(
                f"audit export destination already exists: {dest} "
                "(delete or move the file before re-exporting)"
            )
        if format not in ("jsonl", "csv"):
            raise ValueError(f"unsupported audit export format: {format!r}")

        raw_events = list(self.query(window=window, event_types=event_types))
        oldest = raw_events[0].id if raw_events else None
        newest = raw_events[-1].id if raw_events else None

        dest.parent.mkdir(parents=True, exist_ok=True)
        if redactor is None:
            redacted_events = raw_events
            aggregate: dict | None = None
        else:
            redacted_events = []
            for event in raw_events:
                result = redactor.redact(event)
                if result is not None:
                    redacted_events.append(result)
            aggregate = redactor.finalize()

        if aggregate is not None:
            # AGGREGATE_ONLY: single-object JSON dump regardless of `format`.
            data = msgspec.json.encode(aggregate, enc_hook=_enc_hook) + b"\n"
            dest.write_bytes(data)
            byte_count = len(data)
        elif format == "jsonl":
            byte_count = _write_jsonl(dest, redacted_events)
        else:
            byte_count = _write_csv(dest, redacted_events)

        return AuditExportResult(
            dest_path=dest,
            format=format,
            event_count=len(redacted_events) if aggregate is None else len(raw_events),
            window_start=window.start,
            window_end=window.end,
            oldest_event_id=oldest,
            newest_event_id=newest,
            byte_count=byte_count,
        )


# ---- Module-level helpers ----------------------------------------------


def export_audit_events(
    db_path: str | Path,
    dest: Path,
    *,
    window: TimeWindow,
    format: ExportFormat = "jsonl",
    event_types: Iterable[str] | None = None,
    redactor: EventRedactor | None = None,
) -> AuditExportResult:
    """Open a trace DB, export the audit window, close. CLI convenience."""
    trace = TraceStore(db_path)
    try:
        audit = AuditLog(trace)
        return audit.export(
            dest,
            window=window,
            format=format,
            event_types=event_types,
            redactor=redactor,
        )
    finally:
        trace.close()


def _resolve_event_types(event_types: Iterable[str] | None) -> set[str]:
    if event_types is None:
        return set(AUDIT_EVENT_TYPES)
    requested = set(event_types)
    return requested & AUDIT_EVENT_TYPES


def _row_to_event(row: tuple) -> Event:
    (
        event_id,
        ts_us,
        session_id,
        turn_id,
        parent_event_id,
        type_,
        actor,
        sensitivity,
        payload_json,
    ) = row
    from datetime import UTC

    timestamp = datetime.fromtimestamp(int(ts_us) / 1_000_000, tz=UTC)
    payload = _decode_payload(type_, payload_json)
    return Event(
        id=event_id,
        timestamp=timestamp,
        session_id=session_id,
        turn_id=turn_id,
        parent_event_id=parent_event_id,
        type=type_,
        actor=Actor(actor),
        sensitivity=Sensitivity(sensitivity),
        payload=payload,
    )


def _decode_payload(event_type: str, payload_json: str) -> dict:
    """Decode the stored JSON back into a dict.

    For known audit types, round-trip through the registered Struct so
    `datetime` / `Decimal` fields are restored to their declared types
    rather than left as ISO strings. Unknown types (would only happen if
    `AUDIT_EVENT_TYPES` references an unregistered type — defended by a
    test) fall back to a raw JSON decode.
    """
    entry = PAYLOAD_REGISTRY.get(event_type)
    if entry is None:
        return msgspec.json.decode(payload_json)
    struct_class, _ = entry
    struct = msgspec.json.decode(payload_json, type=struct_class)
    return msgspec.to_builtins(struct, builtin_types=(datetime,))


# ---- Format writers ----------------------------------------------------


def _event_to_envelope(event: Event) -> dict:
    """Serialize an Event to a dict whose key order matches `_CSV_HEADER`
    (also the JSONL field order). Field order matters for determinism."""
    return {
        "id": event.id,
        "timestamp": event.timestamp.isoformat(),
        "session_id": event.session_id,
        "turn_id": event.turn_id,
        "parent_event_id": event.parent_event_id,
        "type": event.type,
        "actor": event.actor.value,
        "sensitivity": event.sensitivity.value,
        "payload": event.payload,
    }


def _encode_jsonl_line(event: Event) -> bytes:
    """Encode one event as a JSONL line ending in `\\n`.

    msgspec preserves dict insertion order, which gives us deterministic
    field order. `enc_hook` handles `Decimal` (serializes as string per the
    canonical-format convention) and any stray `datetime` instances
    surviving from the payload round-trip (serialized as ISO strings).
    """
    envelope = _event_to_envelope(event)
    return msgspec.json.encode(envelope, enc_hook=_enc_hook) + b"\n"


def _enc_hook(obj: object) -> object:
    from decimal import Decimal

    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"audit export: unsupported type {type(obj).__name__}")


def _write_jsonl(dest: Path, events: list[Event]) -> int:
    byte_count = 0
    with dest.open("wb") as fh:
        for event in events:
            line = _encode_jsonl_line(event)
            fh.write(line)
            byte_count += len(line)
    return byte_count


def _write_csv(dest: Path, events: list[Event]) -> int:
    """Write events as RFC 4180 CSV with a fixed header.

    The payload column embeds the same JSON object as JSONL's `payload`
    field, as a CSV-quoted string. SIEMs typically re-parse this column.
    """
    # Build the CSV in memory first so we can write atomically and return a
    # byte count without re-reading the file. At audit-event volume
    # (single-digit thousands per export window) this is comfortably cheap.
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, dialect="excel", lineterminator="\n")
    writer.writerow(_CSV_HEADER)
    for event in events:
        envelope = _event_to_envelope(event)
        payload_json_bytes = msgspec.json.encode(envelope["payload"], enc_hook=_enc_hook)
        writer.writerow(
            [
                envelope["id"],
                envelope["timestamp"],
                envelope["session_id"],
                envelope["turn_id"] if envelope["turn_id"] is not None else "",
                envelope["parent_event_id"] if envelope["parent_event_id"] is not None else "",
                envelope["type"],
                envelope["actor"],
                envelope["sensitivity"],
                payload_json_bytes.decode("utf-8"),
            ]
        )
    data = buffer.getvalue().encode("utf-8")
    dest.write_bytes(data)
    return len(data)
