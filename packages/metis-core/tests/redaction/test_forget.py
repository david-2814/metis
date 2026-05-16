"""Tests for `metis_core.redaction.forget.forget_user`.

See `docs/specs/redaction.md §5`. Covers dry-run, pseudonymization, audit
event emission, idempotence, and the "subsequent exports return empty"
invariant.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from metis_core.analytics import AnalyticsStore
from metis_core.events.envelope import Actor
from metis_core.events.payloads import (
    LLMCallCompleted,
    TurnCompleted,
    make_event,
)
from metis_core.redaction import forget_user, pseudonym_for
from metis_core.trace.store import TraceStore


def _seed_events_for_user(db_path: Path, user_id: str, count: int = 3) -> None:
    """Seed `count` events stamped with `user_id`."""
    store = TraceStore(db_path)
    try:
        for i in range(count):
            store.write(
                make_event(
                    type="llm.call_completed",
                    session_id=f"sess-{i}",
                    turn_id=f"turn-{i}",
                    actor=Actor.AGENT,
                    payload=LLMCallCompleted(
                        model="anthropic:claude-haiku-4-5",
                        provider="anthropic",
                        input_tokens=10,
                        output_tokens=20,
                        cached_input_tokens=0,
                        cache_creation_input_tokens=0,
                        cost_usd=0.001,
                        pricing_version="v1",
                        latency_ms=100,
                        stop_reason="end_turn",
                        produced_tool_calls=0,
                        produced_thinking_blocks=0,
                        gateway_key_id="gk_abc",
                        user_id=user_id,
                        team_id="team_a",
                    ),
                    timestamp=datetime(2026, 5, 15, 12, i, 0, tzinfo=UTC),
                )
            )
        # Add one event for a different user that must NOT be touched.
        store.write(
            make_event(
                type="turn.completed",
                session_id="sess-other",
                turn_id="turn-other",
                actor=Actor.AGENT,
                payload=TurnCompleted(
                    stop_reason="end_turn",
                    llm_call_count=1,
                    tool_call_count=0,
                    total_input_tokens=10,
                    total_output_tokens=20,
                    total_cost_usd=0.001,
                    wall_time_seconds=1.0,
                    user_id="bob",
                ),
                timestamp=datetime(2026, 5, 15, 13, 0, 0, tzinfo=UTC),
            )
        )
    finally:
        store.close()


def test_dry_run_does_not_touch_db(tmp_path: Path):
    db = tmp_path / "trace.db"
    _seed_events_for_user(db, "alice", count=3)

    result = forget_user(db, "alice", confirm=False)

    assert result.user_id == "alice"
    assert result.matched_events == 3
    assert result.pseudonymized_rows == 0
    assert result.confirmed is False
    assert result.pseudonym == pseudonym_for("alice")
    # DB still has the original user_id
    with AnalyticsStore(db) as store:
        assert store.user_event_count("alice") == 3
    # No audit event was emitted
    with AnalyticsStore(db) as store:
        assert (
            store._conn.execute(
                "SELECT COUNT(*) FROM events WHERE type = 'analytics.user_forgotten'"
            ).fetchone()[0]
            == 0
        )


def test_confirmed_forget_pseudonymizes_and_emits_audit(tmp_path: Path):
    db = tmp_path / "trace.db"
    _seed_events_for_user(db, "alice", count=3)

    result = forget_user(db, "alice", confirm=True)

    assert result.confirmed is True
    assert result.pseudonymized_rows == 3
    assert result.matched_events == 3
    # Original user_id is gone; pseudonym is in place
    with AnalyticsStore(db) as store:
        assert store.user_event_count("alice") == 0
        assert store.user_event_count(result.pseudonym) == 3
        # The other user's events are untouched
        assert store.user_event_count("bob") == 1
    # The audit event landed
    store = TraceStore(db)
    try:
        count = store.count_by_type("analytics.user_forgotten")
        assert count == 1
    finally:
        store.close()


def test_idempotent_re_forget_returns_zero_rows(tmp_path: Path):
    db = tmp_path / "trace.db"
    _seed_events_for_user(db, "alice", count=3)

    first = forget_user(db, "alice", confirm=True)
    second = forget_user(db, "alice", confirm=True)

    assert first.pseudonymized_rows == 3
    assert second.pseudonymized_rows == 0
    assert second.matched_events == 0  # already pseudonymized
    # Two audit events recorded — one per request, per the existing
    # AnalyticsUserForgotten contract.
    store = TraceStore(db)
    try:
        assert store.count_by_type("analytics.user_forgotten") == 2
    finally:
        store.close()


def test_subsequent_export_returns_empty_for_forgotten_user(tmp_path: Path):
    """Per redaction.md §5 invariant: re-exporting for the original
    user_id matches zero events; exporting by hash returns the rows."""
    db = tmp_path / "trace.db"
    _seed_events_for_user(db, "alice", count=3)
    result = forget_user(db, "alice", confirm=True)

    with AnalyticsStore(db) as store:
        assert store.user_event_count("alice") == 0
        chunks = list(store.user_export("alice"))
        assert chunks == []
        # By hash, the rows surface
        chunks_by_hash = list(store.user_export(result.pseudonym))
        assert len(chunks_by_hash) == 3


def test_forget_missing_db_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        forget_user(tmp_path / "missing.db", "alice", confirm=False)


def test_forget_audit_event_carries_subject_pseudonym_rows(tmp_path: Path):
    db = tmp_path / "trace.db"
    _seed_events_for_user(db, "alice", count=2)
    result = forget_user(db, "alice", confirm=True)

    store = TraceStore(db)
    try:
        rows = store._conn.execute(
            "SELECT payload_json FROM events WHERE type = 'analytics.user_forgotten'"
        ).fetchall()
        assert len(rows) == 1
        import json

        payload = json.loads(rows[0][0])
        assert payload["subject_user_id"] == "alice"
        assert payload["pseudonym"] == result.pseudonym
        assert payload["pseudonymized_rows"] == 2
        assert payload["requested_by"] is None
    finally:
        store.close()
