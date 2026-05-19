"""GDPR right-to-be-forgotten CLI helper.

See `docs/specs/redaction.md §5`. Wraps the `PseudonymizingRedactor`
shipped by 12a-2 with a higher-level entry point that:

1. Counts events that *would* be touched (dry-run mode for the no-confirm path).
2. Invokes `PseudonymizingRedactor.forget_user` when `confirm=True`.
3. Emits one `analytics.user_forgotten` audit event so the audit trail
   records the action (idempotent re-calls still emit, with
   `pseudonymized_rows=0`, per the existing payload contract).

The audit event is emitted via direct trace-store write rather than a
running bus — `metis user forget` is a one-shot CLI command and
spinning up a bus + dispatch loop just for one event is unnecessary
overhead.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from metis.core.events.envelope import Actor
from metis.core.events.payloads import AnalyticsUserForgotten, make_event
from metis.core.redaction.default import PseudonymizingRedactor, pseudonym_for
from metis.core.trace.store import TraceStore


@dataclass(frozen=True)
class ForgetResult:
    """Returned by `forget_user`. Stable shape for the CLI to print + tests
    to assert against."""

    user_id: str
    pseudonym: str
    matched_events: int  # pre-forget COUNT — what would be (or was) touched
    pseudonymized_rows: int  # actual UPDATE rowcount; 0 on dry-run / re-run
    confirmed: bool
    forgotten_at: datetime


def _count_events_for_user(db_path: Path, user_id: str) -> int:
    """Count events whose payload `$.user_id` matches `user_id`."""
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM events WHERE json_extract(payload_json, '$.user_id') = ?",
            (user_id,),
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def forget_user(
    db_path: str | Path,
    user_id: str,
    *,
    confirm: bool = False,
    requested_by: str | None = None,
) -> ForgetResult:
    """Pseudonymize `user_id` across the trace DB and emit the audit event.

    `confirm=False` is a dry-run: counts the rows that would be touched
    and returns without mutating the DB. `confirm=True` runs the UPDATE
    and emits `analytics.user_forgotten`.

    `requested_by` is the caller's identity (None for the CLI to match
    the loopback-dashboard convention; HTTP handlers can set it).

    Idempotent: re-calling for a forgotten user matches zero rows and
    still emits an audit event with `pseudonymized_rows = 0`.
    """
    db_path = Path(db_path)
    if not db_path.exists():
        raise FileNotFoundError(f"trace DB does not exist: {db_path}")
    pseudonym = pseudonym_for(user_id)
    matched = _count_events_for_user(db_path, user_id)
    forgotten_at = datetime.now(UTC)

    if not confirm:
        # Dry run — do not touch the DB and do not emit an audit event.
        return ForgetResult(
            user_id=user_id,
            pseudonym=pseudonym,
            matched_events=matched,
            pseudonymized_rows=0,
            confirmed=False,
            forgotten_at=forgotten_at,
        )

    redactor = PseudonymizingRedactor(db_path)
    pseudonymized_rows = redactor.forget_user(user_id)
    _emit_audit_event(
        db_path=db_path,
        subject_user_id=user_id,
        pseudonym=pseudonym,
        requested_by=requested_by,
        pseudonymized_rows=pseudonymized_rows,
        forgotten_at=forgotten_at,
    )
    return ForgetResult(
        user_id=user_id,
        pseudonym=pseudonym,
        matched_events=matched,
        pseudonymized_rows=pseudonymized_rows,
        confirmed=True,
        forgotten_at=forgotten_at,
    )


def _emit_audit_event(
    *,
    db_path: Path,
    subject_user_id: str,
    pseudonym: str,
    requested_by: str | None,
    pseudonymized_rows: int,
    forgotten_at: datetime,
) -> None:
    """Write one `analytics.user_forgotten` event to the trace DB.

    The bus dispatch path is bypassed deliberately — the CLI is one-shot
    and the only consumer that cares is the trace store itself. The
    event format matches what the HTTP forget surface emits (so the
    audit trail is uniform regardless of entry point).
    """
    payload = AnalyticsUserForgotten(
        subject_user_id=subject_user_id,
        pseudonym=pseudonym,
        requested_by=requested_by,
        pseudonymized_rows=pseudonymized_rows,
    )
    # session_id is required on the envelope; for an admin-action audit
    # event there is no session of origin. Match the convention 12a-2's
    # `metis user forget` CLI uses (`session_id="analytics"`) so both
    # entry points emit byte-compatible audit envelopes.
    event = make_event(
        type="analytics.user_forgotten",
        session_id="analytics",
        actor=Actor.SYSTEM,
        payload=payload,
        timestamp=forgotten_at,
    )
    store = TraceStore(db_path)
    try:
        store.write(event)
    finally:
        store.close()
