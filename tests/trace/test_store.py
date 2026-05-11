"""Tests for the trace store: persistence, replay query, causal walk."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from metis.events.bus import EventBus
from metis.events.envelope import Actor
from metis.events.payloads import (
    LLMCallCompleted,
    PolicyEvaluation,
    RouteDecided,
    SessionCreated,
    TurnStarted,
    make_event,
)
from metis.trace.store import TraceStore


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


def test_event_id_primary_key_uniqueness(db_path: Path):
    store = TraceStore(db_path)
    event = _session_created_event("sess_1")
    store.write(event)
    # Second insert with the same id should violate PRIMARY KEY.
    with pytest.raises(sqlite3.IntegrityError):
        store.write(event)
    store.close()
