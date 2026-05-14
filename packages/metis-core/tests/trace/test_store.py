"""Tests for the trace store: persistence, replay query, causal walk."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from metis_core.events.bus import EventBus
from metis_core.events.envelope import Actor
from metis_core.events.payloads import (
    LLMCallCompleted,
    PolicyEvaluation,
    RouteDecided,
    SessionCreated,
    ToolConfirmationRequested,
    TurnStarted,
    make_event,
)
from metis_core.trace.store import TraceStore


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "trace.db"


def _now() -> datetime:
    return datetime.now(UTC)


def _session_created_event(session_id: str):
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
        timestamp=_now(),
    )


def _turn_started_event(session_id: str):
    return make_event(
        type="turn.started",
        session_id=session_id,
        actor=Actor.USER,
        payload=TurnStarted(
            user_message_hash="h",
            estimated_input_tokens=1,
            has_images=False,
            has_tool_calls_in_history=False,
        ),
        timestamp=_now(),
    )


def _route_decided_event(session_id: str, turn_id: str, parent_event_id: str):
    return make_event(
        type="route.decided",
        session_id=session_id,
        turn_id=turn_id,
        parent_event_id=parent_event_id,
        actor=Actor.SYSTEM,
        payload=RouteDecided(
            chosen_model="anthropic:claude-sonnet-4-6",
            winner_index=0,
            elapsed_ms=1.0,
            chain=[
                PolicyEvaluation(
                    policy="workspace_default",
                    verdict="chose",
                    candidate_model="anthropic:claude-sonnet-4-6",
                    reason="workspace default",
                )
            ],
        ),
        timestamp=_now(),
    )


def _llm_completed_event(session_id: str, turn_id: str, parent_event_id: str):
    return make_event(
        type="llm.call_completed",
        session_id=session_id,
        turn_id=turn_id,
        parent_event_id=parent_event_id,
        actor=Actor.AGENT,
        payload=LLMCallCompleted(
            model="anthropic:claude-sonnet-4-6",
            provider="anthropic",
            input_tokens=10,
            output_tokens=5,
            cached_input_tokens=0,
            cache_creation_input_tokens=0,
            cost_usd=0.001,
            pricing_version="v1",
            latency_ms=100,
            stop_reason="end_turn",
            produced_tool_calls=0,
            produced_thinking_blocks=0,
        ),
        timestamp=_now(),
    )


# ---- Direct write/read -------------------------------------------------


def test_write_and_read_event(db_path: Path):
    store = TraceStore(db_path)
    event = _session_created_event("sess_1")
    store.write(event)

    rows = store.events_for_session("sess_1")
    assert len(rows) == 1
    assert rows[0].id == event.id
    assert rows[0].type == "session.created"
    assert rows[0].payload["workspace_path"] == "/x"
    store.close()


def test_events_for_session_ordered_by_id(db_path: Path):
    store = TraceStore(db_path)
    events = [_session_created_event("sess_a") for _ in range(5)]
    for e in events:
        store.write(e)

    rows = store.events_for_session("sess_a")
    assert [r.id for r in rows] == sorted(e.id for e in events)
    store.close()


def test_events_for_session_since_cursor(db_path: Path):
    store = TraceStore(db_path)
    events = [_session_created_event("sess_a") for _ in range(5)]
    for e in events:
        store.write(e)

    rows = store.events_for_session("sess_a", since_id=events[2].id)
    # since is exclusive: returns events with id > events[2].id
    assert [r.id for r in rows] == sorted(e.id for e in events[3:])
    store.close()


def test_events_for_session_isolates_sessions(db_path: Path):
    store = TraceStore(db_path)
    store.write(_session_created_event("sess_a"))
    store.write(_session_created_event("sess_b"))

    a_rows = store.events_for_session("sess_a")
    b_rows = store.events_for_session("sess_b")
    assert len(a_rows) == 1
    assert len(b_rows) == 1
    assert a_rows[0].session_id == "sess_a"
    assert b_rows[0].session_id == "sess_b"
    store.close()


def test_causal_chain_walk(db_path: Path):
    store = TraceStore(db_path)
    session = _session_created_event("sess_1")
    turn = _turn_started_event("sess_1")
    route = _route_decided_event("sess_1", turn_id="01HZ_t1", parent_event_id=turn.id)
    llm = _llm_completed_event("sess_1", turn_id="01HZ_t1", parent_event_id=route.id)

    for e in [session, turn, route, llm]:
        store.write(e)

    chain = store.causal_chain(llm.id)
    # Root-first ordering: turn -> route -> llm. session has no parent
    # link to turn, so it isn't reachable from llm.
    assert [e.type for e in chain] == ["turn.started", "route.decided", "llm.call_completed"]
    store.close()


def test_count_by_type(db_path: Path):
    store = TraceStore(db_path)
    for _ in range(3):
        store.write(_session_created_event("sess_1"))
    for _ in range(2):
        store.write(_turn_started_event("sess_1"))

    assert store.count_by_type("session.created") == 3
    assert store.count_by_type("turn.started") == 2
    assert store.count_by_type("llm.call_completed") == 0
    store.close()


# ---- SQLite mode commitments -------------------------------------------


def test_wal_mode_enabled(db_path: Path):
    store = TraceStore(db_path)
    # Connect a second time to read the pragma; the store's connection set it.
    cursor = store._conn.execute("PRAGMA journal_mode")
    mode = cursor.fetchone()[0]
    assert mode.lower() == "wal"
    cursor = store._conn.execute("PRAGMA synchronous")
    sync_mode = cursor.fetchone()[0]
    # NORMAL = 1 in SQLite's pragma response.
    assert sync_mode == 1
    store.close()


# ---- Bus integration ---------------------------------------------------


async def test_attach_to_bus_persists_emitted_events(db_path: Path):
    bus = EventBus()
    bus.start()
    store = TraceStore(db_path)
    store.attach_to(bus)

    bus.emit(_session_created_event("sess_x"))
    bus.emit(_turn_started_event("sess_x"))
    await bus.drain()
    await bus.stop()

    rows = store.events_for_session("sess_x")
    assert [r.type for r in rows] == ["session.created", "turn.started"]
    store.close()


def test_schema_creates_required_indexes(db_path: Path):
    store = TraceStore(db_path)
    cursor = store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='events'"
    )
    index_names = {row[0] for row in cursor.fetchall()}
    assert "idx_events_session_id" in index_names
    assert "idx_events_type_timestamp" in index_names
    assert "idx_events_turn" in index_names
    assert "idx_events_parent" in index_names
    store.close()


def test_datetime_payload_fields_round_trip_as_datetime(db_path: Path):
    """Catalog-declared `datetime` fields must come back as `datetime`,
    not as ISO strings (see docs/KNOWN_ISSUES.md, fixed)."""
    store = TraceStore(db_path)
    expires = datetime(2026, 5, 13, 12, 30, 45, 123456, tzinfo=UTC)
    event = make_event(
        type="tool.confirmation_requested",
        session_id="sess_1",
        actor=Actor.AGENT,
        payload=ToolConfirmationRequested(
            tool_use_id="t1",
            tool_name="shell_exec",
            side_effects="execute",
            confirmation_request_id="r1",
            input_summary="echo hi",
            expires_at=expires,
        ),
        timestamp=_now(),
    )
    store.write(event)

    rows = store.events_for_session("sess_1")
    assert len(rows) == 1
    assert isinstance(rows[0].payload["expires_at"], datetime)
    assert rows[0].payload["expires_at"] == expires
    store.close()


def test_event_id_primary_key_uniqueness(db_path: Path):
    store = TraceStore(db_path)
    event = _session_created_event("sess_1")
    store.write(event)
    # Second insert with the same id should violate PRIMARY KEY.
    with pytest.raises(sqlite3.IntegrityError):
        store.write(event)
    store.close()


# ---- Gap detection -----------------------------------------------------


def _llm_completed_at(*, session_id: str, turn_id: str, parent_event_id: str, timestamp: datetime):
    """Like _llm_completed_event but with a caller-controlled timestamp."""
    return make_event(
        type="llm.call_completed",
        session_id=session_id,
        turn_id=turn_id,
        parent_event_id=parent_event_id,
        actor=Actor.AGENT,
        payload=LLMCallCompleted(
            model="anthropic:claude-sonnet-4-6",
            provider="anthropic",
            input_tokens=10,
            output_tokens=5,
            cached_input_tokens=0,
            cache_creation_input_tokens=0,
            cost_usd=0.001,
            pricing_version="v1",
            latency_ms=100,
            stop_reason="end_turn",
            produced_tool_calls=0,
            produced_thinking_blocks=0,
        ),
        timestamp=timestamp,
    )


def test_detect_gaps_finds_intra_turn_time_gap(db_path: Path):
    """Spec §6.10: two intra-turn events separated by > threshold flag a gap."""
    store = TraceStore(db_path)
    base = datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC)
    early = _llm_completed_at(
        session_id="sess_1", turn_id="turn_a", parent_event_id="p1", timestamp=base
    )
    # 120s later: well past the 60s default threshold.
    late = _llm_completed_at(
        session_id="sess_1",
        turn_id="turn_a",
        parent_event_id="p1",
        timestamp=base.replace(minute=2),
    )
    store.write(early)
    store.write(late)

    gaps = store.detect_gaps()
    assert len(gaps) == 1
    gap = gaps[0]
    assert gap.session_id == "sess_1"
    assert gap.gap_start_id == early.id
    assert gap.gap_end_id == late.id
    assert gap.estimated_missing_count > 0
    store.close()


def test_detect_gaps_skips_cross_turn_boundaries(db_path: Path):
    """A long pause between turns is normal — only intra-turn gaps count."""
    store = TraceStore(db_path)
    base = datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC)
    a = _llm_completed_at(
        session_id="sess_1", turn_id="turn_a", parent_event_id="p1", timestamp=base
    )
    # Five minutes later, but different turn.
    b = _llm_completed_at(
        session_id="sess_1",
        turn_id="turn_b",
        parent_event_id="p2",
        timestamp=base.replace(minute=5),
    )
    store.write(a)
    store.write(b)

    assert store.detect_gaps() == []
    store.close()


def test_detect_gaps_no_gap_when_events_consecutive(db_path: Path):
    """Closely-spaced intra-turn events are not flagged."""
    store = TraceStore(db_path)
    base = datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC)
    a = _llm_completed_at(
        session_id="sess_1", turn_id="turn_a", parent_event_id="p1", timestamp=base
    )
    b = _llm_completed_at(
        session_id="sess_1",
        turn_id="turn_a",
        parent_event_id="p1",
        timestamp=base.replace(microsecond=500_000),  # 0.5s later
    )
    store.write(a)
    store.write(b)

    assert store.detect_gaps() == []
    store.close()


async def test_scan_for_gaps_and_emit_emits_bus_event(db_path: Path):
    """Spec §9.1 test 3: gap scan emits bus.gap_detected on the bus."""
    from metis_core.events.bus import EventFilter, Subscription

    store = TraceStore(db_path)
    base = datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC)
    early = _llm_completed_at(
        session_id="sess_g", turn_id="turn_a", parent_event_id="p1", timestamp=base
    )
    late = _llm_completed_at(
        session_id="sess_g",
        turn_id="turn_a",
        parent_event_id="p1",
        timestamp=base.replace(minute=2),
    )
    store.write(early)
    store.write(late)

    bus = EventBus()
    bus.start()
    received: list = []

    async def collector(event):
        received.append(event)

    bus.subscribe(
        Subscription(
            filter=EventFilter(event_types=frozenset({"bus.gap_detected"})),
            handler=collector,
            name="gap-watcher",
            fast_path=True,
        )
    )

    count = store.scan_for_gaps_and_emit(bus)
    await bus.drain()
    await bus.stop()

    assert count == 1
    assert len(received) == 1
    payload = received[0].payload
    assert payload["session_id"] == "sess_g"
    assert payload["gap_start_id"] == early.id
    assert payload["gap_end_id"] == late.id
    assert payload["estimated_missing_count"] > 0
    store.close()


def test_detect_gaps_respects_scan_limit(db_path: Path):
    """A small `limit` causes older events (and their gaps) to be skipped."""
    store = TraceStore(db_path)
    base = datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC)
    # An old intra-turn gap.
    old_a = _llm_completed_at(
        session_id="sess_1", turn_id="turn_old", parent_event_id="p1", timestamp=base
    )
    old_b = _llm_completed_at(
        session_id="sess_1",
        turn_id="turn_old",
        parent_event_id="p1",
        timestamp=base.replace(minute=2),
    )
    # Plus a third event with a different turn so the bounded scan can drop one.
    fresh = _llm_completed_at(
        session_id="sess_1",
        turn_id="turn_fresh",
        parent_event_id="p3",
        timestamp=base.replace(hour=13),
    )
    store.write(old_a)
    store.write(old_b)
    store.write(fresh)

    # limit=1 keeps only the most-recent event; the older gap is invisible.
    assert store.detect_gaps(limit=1) == []
    # limit=3 reveals the gap.
    assert len(store.detect_gaps(limit=3)) == 1
    store.close()
