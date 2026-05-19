"""Tests for `TraceStore.purge_older_than` per trace-retention.md §9."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from metis.core.events.bus import EventBus, EventFilter, Subscription
from metis.core.events.envelope import Actor, Event
from metis.core.events.payloads import (
    AUDIT_EVENT_TYPES,
    GatewayKeyIssued,
    SessionCreated,
    TraceSwept,
    make_event,
)
from metis.core.trace.retention import PurgeResult, is_audit_event
from metis.core.trace.store import TraceStore


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "trace.db"


def _now() -> datetime:
    return datetime.now(UTC)


def _session_created(session_id: str, *, ts: datetime) -> Event:
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
        timestamp=ts,
    )


def _key_issued(session_id: str, *, ts: datetime, key_id: str = "gk_test") -> Event:
    return make_event(
        type="gateway.key_issued",
        session_id=session_id,
        actor=Actor.SYSTEM,
        payload=GatewayKeyIssued(
            gateway_key_id=key_id,
            name="test",
            workspace_path="/x",
            issued_at=ts,
        ),
        timestamp=ts,
    )


# ---- Cutoff math --------------------------------------------------------


def test_purge_deletes_events_strictly_older_than_cutoff(db_path: Path):
    store = TraceStore(db_path)
    try:
        now = _now()
        cutoff = now - timedelta(days=30)
        # Strictly older — should be deleted.
        store.write(_session_created("old", ts=cutoff - timedelta(seconds=1)))
        # At cutoff exactly — survives (predicate is strict `<`).
        store.write(_session_created("at-cutoff", ts=cutoff))
        # Newer — survives.
        store.write(_session_created("new", ts=now))

        result = store.purge_older_than(cutoff, dry_run=False)

        assert result.rows_deleted == 1
        assert result.rows_eligible == 1
        assert result.dry_run is False
        remaining = store._conn.execute("SELECT session_id FROM events").fetchall()
        assert {r[0] for r in remaining} == {"at-cutoff", "new"}
    finally:
        store.close()


def test_purge_preserves_audit_flagged_events(db_path: Path):
    """`AUDIT_EVENT_TYPES` rows survive a sweep that would otherwise delete them."""
    store = TraceStore(db_path)
    try:
        now = _now()
        cutoff = now - timedelta(days=30)
        # An old non-audit event (deleted) and an old audit event (preserved).
        store.write(_session_created("ops", ts=cutoff - timedelta(days=60)))
        store.write(_key_issued("audit", ts=cutoff - timedelta(days=60)))

        result = store.purge_older_than(cutoff, dry_run=False)

        assert result.rows_deleted == 1
        assert result.rows_audit_exempt == 1
        remaining = {r[0] for r in store._conn.execute("SELECT type FROM events").fetchall()}
        assert "session.created" not in remaining
        assert "gateway.key_issued" in remaining
    finally:
        store.close()


def test_trace_swept_event_is_itself_audit_preserved(db_path: Path):
    """A sweep cannot delete the audit trail of prior sweeps.

    `trace.swept` is added to `AUDIT_EVENT_TYPES`; this test pins the
    invariant by writing a synthetic old `trace.swept` event and running
    a sweep with a cutoff *past* that event's timestamp.
    """
    store = TraceStore(db_path)
    try:
        ancient = _now() - timedelta(days=400)
        store.write(
            make_event(
                type="trace.swept",
                session_id="system",
                actor=Actor.SYSTEM,
                payload=TraceSwept(
                    rows_deleted=42,
                    rows_audit_exempt=0,
                    cutoff_timestamp=ancient - timedelta(days=10),
                    oldest_kept_timestamp=ancient,
                    dry_run=False,
                    swept_at=ancient,
                ),
                timestamp=ancient,
            )
        )

        cutoff = _now() - timedelta(days=30)
        result = store.purge_older_than(cutoff, dry_run=False)

        assert result.rows_audit_exempt == 1
        assert result.rows_deleted == 0
        types = {r[0] for r in store._conn.execute("SELECT type FROM events").fetchall()}
        assert "trace.swept" in types
    finally:
        store.close()


async def test_dry_run_reports_without_deleting_and_does_not_emit(db_path: Path):
    captured: list[Event] = []

    async def _capture(event: Event) -> None:
        if event.type == "trace.swept":
            captured.append(event)

    store = TraceStore(db_path)
    bus = EventBus()
    bus.start()
    bus.subscribe(Subscription(filter=EventFilter(), handler=_capture, name="cap"))
    try:
        now = _now()
        cutoff = now - timedelta(days=30)
        store.write(_session_created("old", ts=cutoff - timedelta(days=10)))

        result = store.purge_older_than(cutoff, bus=bus, dry_run=True)
        await bus.drain()

        assert result.dry_run is True
        assert result.rows_eligible == 1
        assert result.rows_deleted == 0
        (count,) = store._conn.execute("SELECT COUNT(*) FROM events").fetchone()
        assert count == 1
    finally:
        store.close()
        await bus.stop()
    # Trace.swept emission is suppressed in dry-run.
    assert captured == []


def test_purge_on_empty_db_is_a_no_op(db_path: Path):
    store = TraceStore(db_path)
    try:
        result = store.purge_older_than(_now(), dry_run=False)
        assert result.rows_deleted == 0
        assert result.rows_eligible == 0
        assert result.oldest_kept_timestamp is None
    finally:
        store.close()


async def test_apply_emits_trace_swept_with_matching_counts(db_path: Path):
    """`purge_older_than` emits exactly one `trace.swept` event with
    matching counts when a bus is provided in non-dry-run mode."""
    captured: list[Event] = []

    async def _capture(event: Event) -> None:
        if event.type == "trace.swept":
            captured.append(event)

    store = TraceStore(db_path)
    bus = EventBus()
    bus.start()
    bus.subscribe(Subscription(filter=EventFilter(), handler=_capture, name="cap"))
    try:
        now = _now()
        cutoff = now - timedelta(days=30)
        store.write(_session_created("old1", ts=cutoff - timedelta(days=10)))
        store.write(_session_created("old2", ts=cutoff - timedelta(days=5)))
        store.write(_session_created("new", ts=now))

        result = store.purge_older_than(cutoff, bus=bus, dry_run=False)
        await bus.drain()
    finally:
        store.close()
        await bus.stop()

    assert result.rows_deleted == 2
    assert len(captured) == 1
    payload = captured[0].payload
    assert payload["rows_deleted"] == 2
    assert payload["dry_run"] is False


def test_timestamp_index_exists_after_open(db_path: Path):
    store = TraceStore(db_path)
    try:
        rows = store._conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
    finally:
        store.close()
    names = {r[0] for r in rows}
    assert "idx_events_timestamp_us" in names


def test_is_audit_event_includes_trace_swept():
    """`trace.swept` must be in `AUDIT_EVENT_TYPES` for sweep history
    to survive — pins the integration with Wave 12a-1's audit subset."""
    assert "trace.swept" in AUDIT_EVENT_TYPES
    assert is_audit_event("trace.swept") is True
    # Non-audit type: not exempt.
    assert is_audit_event("session.created") is False


def test_exempt_audit_false_lets_audit_rows_be_deleted(db_path: Path):
    """`exempt_audit=False` is a test-only escape hatch; verifies the
    cutoff math without the audit predicate confounding it."""
    store = TraceStore(db_path)
    try:
        now = _now()
        cutoff = now - timedelta(days=30)
        store.write(_key_issued("audit", ts=cutoff - timedelta(days=60)))

        result = store.purge_older_than(cutoff, dry_run=False, exempt_audit=False)
        assert result.rows_deleted == 1
        (count,) = store._conn.execute("SELECT COUNT(*) FROM events").fetchone()
        assert count == 0
    finally:
        store.close()


def test_purge_result_carries_oldest_kept_timestamp(db_path: Path):
    store = TraceStore(db_path)
    try:
        now = _now()
        cutoff = now - timedelta(days=30)
        old_ts = cutoff - timedelta(days=10)
        new_ts = now - timedelta(hours=1)
        store.write(_session_created("old", ts=old_ts))
        store.write(_session_created("new", ts=new_ts))

        result = store.purge_older_than(cutoff, dry_run=False)

        assert result.rows_deleted == 1
        # The remaining `new` row's timestamp matches `oldest_kept`.
        assert result.oldest_kept_timestamp is not None
        # Compare at second resolution; SQLite stores microseconds and
        # `_to_micros` round-trips exactly, so equality should hold.
        assert abs((result.oldest_kept_timestamp - new_ts).total_seconds()) < 1e-3
    finally:
        store.close()


def test_purge_result_is_frozen_dataclass():
    """`PurgeResult` is intentionally `frozen=True` so callers can't
    mutate the audit-trail-bound return value."""
    from dataclasses import FrozenInstanceError

    result = PurgeResult(
        cutoff_timestamp=_now(),
        rows_eligible=0,
        rows_audit_exempt=0,
        rows_deleted=0,
        oldest_kept_timestamp=None,
        dry_run=True,
        swept_at=_now(),
    )
    with pytest.raises(FrozenInstanceError):
        result.rows_deleted = 99  # type: ignore[misc]
