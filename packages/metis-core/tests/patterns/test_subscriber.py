"""Bus subscriber tests for `PatternEventSubscriber`.

Records outcomes when `route.decided` + `turn.completed` pair up; patches
score on `eval.completed`; emits `pattern.recorded` and `pattern.evicted`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from metis_core.events.bus import EventBus, EventFilter, Subscription
from metis_core.events.envelope import Actor, Event
from metis_core.events.payloads import (
    EvalCompleted,
    RouteDecided,
    TurnCompleted,
    make_event,
)
from metis_core.patterns.fingerprint import FingerprintInputs
from metis_core.patterns.store import PatternStore
from metis_core.patterns.subscriber import PatternEventSubscriber


@pytest.fixture
async def bus() -> EventBus:
    b = EventBus()
    b.start()
    yield b
    await b.stop()


@pytest.fixture
async def event_log(bus: EventBus) -> list[Event]:
    log: list[Event] = []

    async def handler(e: Event) -> None:
        log.append(e)

    bus.subscribe(Subscription(filter=EventFilter(), handler=handler, name="log", fast_path=True))
    return log


def _fingerprint_inputs(workspace_path: str) -> FingerprintInputs:
    return FingerprintInputs(
        user_message_text="refactor",
        workspace_path=workspace_path,
        estimated_input_tokens=1000,
        has_images=False,
        has_tool_calls_in_history=False,
        file_extensions=(".py",),
        file_path_buckets=("src",),
        tool_names=("read_file",),
        side_effect_classes=("read",),
    )


def _emit_route(bus: EventBus, *, session_id: str, turn_id: str, model: str) -> Event:
    event = make_event(
        type="route.decided",
        session_id=session_id,
        turn_id=turn_id,
        actor=Actor.SYSTEM,
        payload=RouteDecided(
            chosen_model=model,
            winner_index=0,
            elapsed_ms=1.0,
            chain=[],
        ),
        timestamp=datetime.now(UTC),
    )
    bus.emit(event)
    return event


def _emit_turn_completed(bus: EventBus, *, session_id: str, turn_id: str, cost: float) -> Event:
    event = make_event(
        type="turn.completed",
        session_id=session_id,
        turn_id=turn_id,
        actor=Actor.SYSTEM,
        payload=TurnCompleted(
            stop_reason="end_turn",
            llm_call_count=1,
            tool_call_count=0,
            total_input_tokens=100,
            total_output_tokens=50,
            total_cost_usd=cost,
            wall_time_seconds=1.0,
        ),
        timestamp=datetime.now(UTC),
    )
    bus.emit(event)
    return event


async def test_subscriber_records_outcome_on_turn_completed(bus, event_log, tmp_path: Path) -> None:
    store = PatternStore(tmp_path)
    try:
        subscriber = PatternEventSubscriber(
            store_factory=lambda _ws: store,
            workspace_resolver=lambda _sid: str(tmp_path),
            bus=bus,
        )
        subscriber.attach()
        session_id = "sess_x"
        turn_id = "turn_x"
        subscriber.set_fingerprint_inputs(turn_id, _fingerprint_inputs(str(tmp_path)))
        _emit_route(bus, session_id=session_id, turn_id=turn_id, model="m_a")
        _emit_turn_completed(bus, session_id=session_id, turn_id=turn_id, cost=0.01)
        await bus.drain()
        # The subscriber emits pattern.recorded inside the turn.completed
        # handler; a second drain ensures the follow-on event has been
        # dispatched to the log subscriber.
        await bus.drain()
        assert store.size().outcomes == 1
        recorded = [e for e in event_log if e.type == "pattern.recorded"]
        assert len(recorded) == 1
        payload = recorded[0].payload
        assert payload["primary_model"] == "m_a"
        assert payload["was_new_fingerprint"] is True
    finally:
        store.close()


async def test_eval_completed_patches_score(bus, event_log, tmp_path: Path) -> None:
    store = PatternStore(tmp_path)
    try:
        subscriber = PatternEventSubscriber(
            store_factory=lambda _ws: store,
            workspace_resolver=lambda _sid: str(tmp_path),
            bus=bus,
        )
        subscriber.attach()
        session_id = "sess_y"
        turn_id = "turn_y"
        subscriber.set_fingerprint_inputs(turn_id, _fingerprint_inputs(str(tmp_path)))
        _emit_route(bus, session_id=session_id, turn_id=turn_id, model="m_a")
        _emit_turn_completed(bus, session_id=session_id, turn_id=turn_id, cost=0.01)
        await bus.drain()
        # The subscriber emits pattern.recorded inside the turn.completed
        # handler; a second drain ensures the follow-on event has been
        # dispatched to the log subscriber.
        await bus.drain()
        # Now an eval.completed lands for the same turn.
        eval_evt = make_event(
            type="eval.completed",
            session_id=session_id,
            turn_id=turn_id,
            actor=Actor.SYSTEM,
            payload=EvalCompleted(
                eval_id="eval_1",
                subject_kind="turn",
                subject_id=turn_id,
                score=0.9,
                confidence=0.8,
                judge_kind="heuristic",
                judge_cost_usd=Decimal("0"),
                judge_latency_ms=10,
                rubric_id="default",
                rubric_version="v1",
                signals={},
            ),
            timestamp=datetime.now(UTC),
        )
        bus.emit(eval_evt)
        await bus.drain()
        # The outcome's success_score_mean should reflect the score.
        from metis_core.patterns.fingerprint import (
            build_structural_features,
            structural_signature,
        )

        sig = structural_signature(build_structural_features(_fingerprint_inputs(str(tmp_path))))
        fp_id = store._lookup_fingerprint_by_sig(sig, None)
        row = store._lookup_outcome(fp_id, "m_a")
        assert row is not None
        assert row["success_score_mean"] == pytest.approx(0.9)
        assert row["success_score_count"] == 1
    finally:
        store.close()
