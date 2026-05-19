"""Bus wiring: subscriber emits eval.* events for terminal triggers."""

from __future__ import annotations

from pathlib import Path

import pytest
from metis.core.eval import register_evaluator
from metis.core.events.bus import EventBus, EventFilter, Subscription
from metis.core.events.envelope import Event
from metis.core.trace.store import TraceStore

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


async def _run_online_evaluator(
    bus: EventBus,
    trace: TraceStore,
    *,
    session_id: str,
    signals_extra: dict | None,
) -> dict:
    """Drive one online turn evaluation; return the eval.completed payload.

    Writes the trace-side event first (so the subscriber's
    `trace.events_for_turn` lookup hits) then emits a second event with
    the same `turn_id` but a fresh event id on the bus to trigger
    evaluation. The two events share `signals_extra` so the subscriber's
    payload extraction sees the same content the trace-side event carries.
    """
    turn_id = new_turn_id()
    received: list[Event] = []

    async def handler(event: Event) -> None:
        if event.type.startswith("eval."):
            received.append(event)

    bus.subscribe(Subscription(filter=EventFilter(), handler=handler, name="c", fast_path=False))
    evaluator, _ = register_evaluator(bus, trace)
    try:
        trace.write(
            build_turn_completed(
                session_id=session_id, turn_id=turn_id, signals_extra=signals_extra
            )
        )
        bus.emit(
            build_turn_completed(
                session_id=session_id, turn_id=turn_id, signals_extra=signals_extra
            )
        )
        await bus.drain()
    finally:
        evaluator.unregister()
        await bus.drain()
    completed = [e for e in received if e.type == "eval.completed"]
    assert completed, "expected eval.completed"
    return completed[0].payload


async def test_online_subscriber_applies_refusal_penalty_from_signals_extra(tmp_path: Path):
    """A clean-lifecycle turn whose assistant text begins with a refusal
    phrase should score lower than the same turn without the refusal text.

    Validates the §5.1 "content penalty (opt-in)" path fires on the *online*
    subscriber path now that the session manager plumbs `final_response_text`
    through `turn.completed.signals_extra`. Previously this only fired on
    the workload harness path.
    """
    bus = EventBus()
    bus.start()
    trace = TraceStore(tmp_path / "t.db")
    trace.attach_to(bus)
    try:
        clean_payload = await _run_online_evaluator(
            bus,
            trace,
            session_id="sess_clean",
            signals_extra={"final_response_text": "Here is the answer you asked for."},
        )
        refusal_payload = await _run_online_evaluator(
            bus,
            trace,
            session_id="sess_refuse",
            signals_extra={"final_response_text": "I cannot help with that."},
        )
    finally:
        await bus.drain()
        await bus.stop()
        trace.close()

    assert refusal_payload["score"] < clean_payload["score"]
    assert "assistant_refusal_detected" in refusal_payload["signals"]["flags_negative"]
    assert "assistant_refusal_detected" not in clean_payload["signals"]["flags_negative"]


async def test_online_subscriber_no_penalty_when_signals_extra_missing(tmp_path: Path):
    """Absent signals_extra → no content penalty (back-compat with callers
    that don't plumb assistant text)."""
    bus = EventBus()
    bus.start()
    trace = TraceStore(tmp_path / "t.db")
    trace.attach_to(bus)
    try:
        payload = await _run_online_evaluator(
            bus, trace, session_id="sess_nosig", signals_extra=None
        )
    finally:
        await bus.drain()
        await bus.stop()
        trace.close()

    assert "assistant_refusal_detected" not in payload["signals"]["flags_negative"]
    assert "empty_assistant_response" not in payload["signals"]["flags_negative"]
