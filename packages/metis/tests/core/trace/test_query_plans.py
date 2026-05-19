"""EXPLAIN QUERY PLAN coverage for the analytics queries (Wave 13).

Each test asserts that a representative `analytics/store.py` query is
served by an index, not by `SCAN events` (full table scan). Failure
means a new query landed without index coverage — fix the index, not
the test.

These tests build a small fixture (~200 events) so the planner has
ANALYZE-able statistics without the per-test cost of a multi-thousand-
row insert.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from metis.core.events.envelope import Actor
from metis.core.events.payloads import (
    EvalCompleted,
    LLMCallCompleted,
    make_event,
)
from metis.core.trace.store import TraceStore


@pytest.fixture
def populated_store(tmp_path: Path) -> TraceStore:
    db = tmp_path / "trace.db"
    store = TraceStore(db)
    now = datetime.now(UTC)
    for i in range(200):
        store.write(
            make_event(
                type="llm.call_completed",
                session_id=f"sess_{i % 5}",
                turn_id=f"turn_{i}",
                parent_event_id=None,
                actor=Actor.SYSTEM,
                payload=LLMCallCompleted(
                    model="anthropic:claude-sonnet-4-6",
                    provider="anthropic",
                    input_tokens=100,
                    output_tokens=50,
                    cached_input_tokens=0,
                    cache_creation_input_tokens=0,
                    cost_usd=0.001,
                    pricing_version="2026-05-01",
                    latency_ms=120,
                    stop_reason="end_turn",
                    produced_tool_calls=0,
                    produced_thinking_blocks=0,
                    gateway_key_id=f"gw_{i % 3}",
                    inbound_shape="openai",
                    user_id=f"u_{i % 2}" if i % 4 else None,
                    team_id="team_alpha" if i % 4 else None,
                    parent_session_id=None,
                ),
                timestamp=now,
            )
        )
    # A handful of eval.completed for the eval-subject-kind index check.
    for i in range(20):
        store.write(
            make_event(
                type="eval.completed",
                session_id=f"sess_{i % 5}",
                turn_id=f"turn_{i}",
                parent_event_id=None,
                actor=Actor.SYSTEM,
                payload=EvalCompleted(
                    eval_id=f"eval_{i}",
                    subject_kind="turn",
                    subject_id=f"turn_{i}",
                    score=0.8,
                    confidence=0.9,
                    judge_kind="heuristic",
                    judge_cost_usd=Decimal("0"),
                    judge_latency_ms=1,
                    rubric_id="default",
                    rubric_version="1",
                    signals={},
                ),
                timestamp=now,
            )
        )
    store._conn.execute("ANALYZE")
    yield store
    store.close()


def _explain(store: TraceStore, sql: str, params: tuple) -> list[str]:
    """Return one detail string per row in the query plan."""
    return [row[3] for row in store._conn.execute("EXPLAIN QUERY PLAN " + sql, params)]


def _assert_uses_index(plan: list[str], expected_index: str) -> None:
    matched = any(expected_index in line and "SCAN " not in line for line in plan)
    assert matched, f"Query plan did not use {expected_index!r}; got: {plan}"


def test_user_export_uses_user_id_expression_index(populated_store: TraceStore):
    """`/analytics/user/{id}/export` must NOT scan the table.

    Pre-Wave-13 this was a full SCAN; Wave 13's `idx_events_user_id`
    expression index serves the lookup.
    """
    plan = _explain(
        populated_store,
        "SELECT id FROM events WHERE json_extract(payload_json, '$.user_id') = ?",
        ("u_1",),
    )
    _assert_uses_index(plan, "idx_events_user_id")


def test_events_for_turn_no_temp_btree_for_order_by(populated_store: TraceStore):
    """Wave 13 composite `(turn_id, id)` eliminates the TEMP B-TREE FOR ORDER BY."""
    plan = _explain(
        populated_store,
        "SELECT * FROM events WHERE turn_id = ? ORDER BY id",
        ("turn_0",),
    )
    assert any("idx_events_turn_id_id" in line for line in plan), (
        f"Expected composite turn_id+id index; got: {plan}"
    )
    assert not any("USE TEMP B-TREE" in line for line in plan), (
        f"Composite index should eliminate the temp sort; got: {plan}"
    )


def test_eval_quality_uses_subject_kind_index(populated_store: TraceStore):
    """`/analytics/quality` must hit `idx_events_eval_subject_kind`."""
    plan = _explain(
        populated_store,
        (
            "SELECT json_extract(payload_json, '$.score') FROM events "
            "WHERE type = 'eval.completed' "
            "  AND json_extract(payload_json, '$.subject_kind') = ? "
            "  AND timestamp_us >= ? AND timestamp_us < ?"
        ),
        ("turn", 0, 9999999999999999),
    )
    _assert_uses_index(plan, "idx_events_eval_subject_kind")


def test_cost_by_window_uses_type_timestamp_index(populated_store: TraceStore):
    """The default `/analytics/cost` slice rides `idx_events_type_timestamp`.

    This is the Wave-12 baseline — we re-assert it so a future migration
    that drops the index is caught immediately.
    """
    plan = _explain(
        populated_store,
        (
            "SELECT json_extract(payload_json, '$.cost_usd') FROM events "
            "WHERE type = 'llm.call_completed' "
            "  AND timestamp_us >= ? AND timestamp_us < ?"
        ),
        (0, 9999999999999999),
    )
    _assert_uses_index(plan, "idx_events_type_timestamp")


def test_session_replay_uses_session_id_index(populated_store: TraceStore):
    """`events_for_session` rides `idx_events_session_id` (composite with id)."""
    plan = _explain(
        populated_store,
        "SELECT * FROM events WHERE session_id = ? ORDER BY id",
        ("sess_0",),
    )
    _assert_uses_index(plan, "idx_events_session_id")


def test_no_query_uses_full_scan(populated_store: TraceStore):
    """Sanity: every documented analytics query has a SEARCH plan, never a SCAN.

    Catches regressions where a future query hits the table without an
    index. The list mirrors the queries in `analytics/store.py`.
    """
    documented = [
        # /analytics/cost — type+timestamp slice
        (
            "SELECT json_extract(payload_json, '$.cost_usd') FROM events "
            "WHERE type = 'llm.call_completed' "
            "  AND timestamp_us >= ? AND timestamp_us < ?",
            (0, 9999999999999999),
        ),
        # /analytics/quality — eval subject_kind slice
        (
            "SELECT json_extract(payload_json, '$.score') FROM events "
            "WHERE type = 'eval.completed' "
            "  AND json_extract(payload_json, '$.subject_kind') = ? "
            "  AND timestamp_us >= ? AND timestamp_us < ?",
            ("turn", 0, 9999999999999999),
        ),
        # /analytics/turns/{turn_id}
        (
            "SELECT * FROM events WHERE turn_id = ? ORDER BY id",
            ("turn_0",),
        ),
        # /analytics/user/{id}/export
        (
            "SELECT id FROM events WHERE json_extract(payload_json, '$.user_id') = ? ORDER BY id",
            ("u_1",),
        ),
        # /sessions/{id} replay
        (
            "SELECT * FROM events WHERE session_id = ? ORDER BY id",
            ("sess_0",),
        ),
        # retention sweep
        (
            "DELETE FROM events WHERE timestamp_us < ? AND type NOT IN (?, ?)",
            (0, "trace.swept", "gateway.key_issued"),
        ),
    ]
    for sql, params in documented:
        plan = _explain(populated_store, sql, params)
        # Allow USE TEMP B-TREE FOR ORDER BY (sort step) but reject any SCAN
        # of the events table itself.
        scans = [line for line in plan if line.startswith("SCAN events")]
        assert not scans, f"Full table scan in: {sql!r}\n  plan: {plan}"
