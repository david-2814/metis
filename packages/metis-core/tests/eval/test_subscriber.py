"""Bus wiring: subscriber emits eval.* events for terminal triggers."""

from __future__ import annotations

from pathlib import Path

import pytest
from metis_core.eval import register_evaluator
from metis_core.events.bus import EventBus, EventFilter, Subscription
from metis_core.events.envelope import Event
from metis_core.trace.store import TraceStore

from .helpers import (
    build_session_ended,
    build_tool_called,
    build_tool_completed,
    build_turn_completed,
    new_tool_use_id,
    new_turn_id,
)


@pytest.fixture
async def bus():
    b = EventBus()
    b.start()
    try:
        yield b
    finally:
        await b.drain()
        await b.stop()


async def _collect_events(bus: EventBus) -> list[Event]:
    received: list[Event] = []

    async def handler(event: Event) -> None:
        received.append(event)

    bus.subscribe(
        Subscription(filter=EventFilter(), handler=handler, name="collector", fast_path=False)
    )
    return received


async def test_subscriber_emits_eval_started_and_completed_for_turn(tmp_path: Path):
    bus = EventBus()
    bus.start()
    trace = TraceStore(tmp_path / "t.db")
    trace.attach_to(bus)
    received: list[Event] = []

    async def handler(event: Event) -> None:
        if event.type.startswith("eval."):
            received.append(event)

    bus.subscribe(Subscription(filter=EventFilter(), handler=handler, name="c", fast_path=False))

    evaluator, _ = register_evaluator(bus, trace)
    try:
        session_id = "sess_t"
        turn_id = new_turn_id()
        # Persist the turn-completed event in the trace store FIRST so the
        # subscriber can read the turn's events when it fires.
        trace.write(build_turn_completed(session_id=session_id, turn_id=turn_id))
        # Now emit the trigger.
        bus.emit(build_turn_completed(session_id=session_id, turn_id=turn_id))
        await bus.drain()
    finally:
        evaluator.unregister()
        await bus.drain()
        await bus.stop()
        trace.close()

    started = [e for e in received if e.type == "eval.started"]
    completed = [e for e in received if e.type == "eval.completed"]
    failed = [e for e in received if e.type == "eval.failed"]

    assert len(started) >= 1
    assert len(completed) >= 1
    assert not failed
    payload = completed[0].payload
    assert payload["subject_kind"] == "turn"
    assert payload["subject_id"] == turn_id
    assert payload["judge_kind"] == "heuristic"
    assert payload["rubric_id"] == "turn-heuristic-v1"
    # judge_cost_usd is Decimal serialized as string ("0") for heuristic
    assert str(payload["judge_cost_usd"]) in ("0", "0.0")


async def test_subscriber_emits_for_tool_completed(tmp_path: Path):
    bus = EventBus()
    bus.start()
    trace = TraceStore(tmp_path / "t.db")
    trace.attach_to(bus)
    received: list[Event] = []

    async def handler(event: Event) -> None:
        if event.type.startswith("eval."):
            received.append(event)

    bus.subscribe(Subscription(filter=EventFilter(), handler=handler, name="c", fast_path=False))

    evaluator, _ = register_evaluator(bus, trace)
    try:
        session_id = "sess_tool"
        turn_id = new_turn_id()
        tool_use_id = new_tool_use_id()
        trace.write(
            build_tool_called(
                session_id=session_id,
                turn_id=turn_id,
                tool_use_id=tool_use_id,
                tool_name="read_file",
            )
        )
        trace.write(
            build_tool_completed(session_id=session_id, turn_id=turn_id, tool_use_id=tool_use_id)
        )
        bus.emit(
            build_tool_completed(session_id=session_id, turn_id=turn_id, tool_use_id=tool_use_id)
        )
        await bus.drain()
    finally:
        evaluator.unregister()
        await bus.drain()
        await bus.stop()
        trace.close()

    completed = [e for e in received if e.type == "eval.completed"]
    assert any(e.payload["subject_kind"] == "tool_cycle" for e in completed)


async def test_subscriber_emits_failed_for_missing_subject(tmp_path: Path):
    """Direct API call with a non-existent turn_id → eval.failed."""
    bus = EventBus()
    bus.start()
    trace = TraceStore(tmp_path / "t.db")
    trace.attach_to(bus)
    received: list[Event] = []

    async def handler(event: Event) -> None:
        if event.type.startswith("eval."):
            received.append(event)

    bus.subscribe(Subscription(filter=EventFilter(), handler=handler, name="c", fast_path=False))
    evaluator, _ = register_evaluator(bus, trace)
    try:
        await evaluator.evaluate_turn(
            session_id="sess_missing", turn_id="01ZNOPE0000000000000000000"
        )
        await bus.drain()
    finally:
        evaluator.unregister()
        await bus.drain()
        await bus.stop()
        trace.close()

    failed = [e for e in received if e.type == "eval.failed"]
    assert any(e.payload["failure_mode"] == "subject_not_found" for e in failed)


async def test_subscriber_filter_does_not_fire_on_non_terminal_events(tmp_path: Path):
    """`llm.call_completed` is not a terminal trigger (evaluator.md §6.1)."""
    bus = EventBus()
    bus.start()
    trace = TraceStore(tmp_path / "t.db")
    trace.attach_to(bus)
    received: list[Event] = []

    async def handler(event: Event) -> None:
        if event.type.startswith("eval."):
            received.append(event)

    bus.subscribe(Subscription(filter=EventFilter(), handler=handler, name="c", fast_path=False))
    evaluator, _ = register_evaluator(bus, trace)
    try:
        from .helpers import build_llm_completed

        session_id = "sess_filter"
        turn_id = new_turn_id()
        bus.emit(build_llm_completed(session_id=session_id, turn_id=turn_id))
        await bus.drain()
    finally:
        evaluator.unregister()
        await bus.drain()
        await bus.stop()
        trace.close()

    assert received == []


async def test_subscriber_session_evaluation_reads_child_verdicts(tmp_path: Path):
    bus = EventBus()
    bus.start()
    trace = TraceStore(tmp_path / "t.db")
    trace.attach_to(bus)
    received: list[Event] = []

    async def handler(event: Event) -> None:
        if event.type.startswith("eval."):
            received.append(event)

    bus.subscribe(Subscription(filter=EventFilter(), handler=handler, name="c", fast_path=False))

    evaluator, _ = register_evaluator(bus, trace)
    try:
        session_id = "sess_agg"
        turn_id = new_turn_id()
        trace.write(build_turn_completed(session_id=session_id, turn_id=turn_id))
        bus.emit(build_turn_completed(session_id=session_id, turn_id=turn_id))
        await bus.drain()

        trace.write(build_session_ended(session_id=session_id))
        bus.emit(build_session_ended(session_id=session_id))
        await bus.drain()
    finally:
        evaluator.unregister()
        await bus.drain()
        await bus.stop()
        trace.close()

    session_verdicts = [
        e for e in received if e.type == "eval.completed" and e.payload["subject_kind"] == "session"
    ]
    assert session_verdicts
    payload = session_verdicts[-1].payload
    assert payload["signals"]["turn_count"] >= 1
    assert payload["signals"]["child_eval_ids"]
