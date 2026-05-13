"""Tests for the EventBus dispatch behavior."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

import pytest
from metis_core.events.bus import EventBus, EventFilter, Subscription, ValidationMode
from metis_core.events.envelope import Actor, Event, Sensitivity
from metis_core.events.errors import (
    EventBusOverflowError,
    EventValidationError,
    UnknownEventTypeError,
)
from metis_core.events.payloads import (
    LLMCallCompleted,
    SessionCreated,
    TurnStarted,
    make_event,
)


def _now() -> datetime:
    return datetime.now(UTC)


def _session_created(session_id: str = "sess_1") -> Event:
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


def _turn_started(session_id: str = "sess_1") -> Event:
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


def _llm_completed(session_id: str = "sess_1") -> Event:
    return make_event(
        type="llm.call_completed",
        session_id=session_id,
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


# ---- Basic dispatch -----------------------------------------------------


async def test_fast_path_subscriber_receives_event():
    bus = EventBus()
    bus.start()
    received: list[Event] = []

    async def handler(event: Event) -> None:
        received.append(event)

    bus.subscribe(Subscription(filter=EventFilter(), handler=handler, name="t", fast_path=True))
    bus.emit(_session_created())
    await bus.drain()
    await bus.stop()

    assert len(received) == 1
    assert received[0].type == "session.created"


async def test_multiple_subscribers_all_receive():
    bus = EventBus()
    bus.start()
    a: list[Event] = []
    b: list[Event] = []

    async def ha(e: Event) -> None:
        a.append(e)

    async def hb(e: Event) -> None:
        b.append(e)

    bus.subscribe(Subscription(filter=EventFilter(), handler=ha, name="a", fast_path=True))
    bus.subscribe(Subscription(filter=EventFilter(), handler=hb, name="b", fast_path=True))
    bus.emit(_session_created())
    await bus.drain()
    await bus.stop()

    assert len(a) == 1
    assert len(b) == 1


async def test_non_fast_path_subscriber_receives_event():
    bus = EventBus()
    bus.start()
    received: list[Event] = []

    async def handler(event: Event) -> None:
        await asyncio.sleep(0)  # yield to scheduler
        received.append(event)

    bus.subscribe(Subscription(filter=EventFilter(), handler=handler, name="t", fast_path=False))
    bus.emit(_session_created())
    await bus.drain()
    await bus.stop()

    assert len(received) == 1


# ---- Filters ------------------------------------------------------------


async def test_filter_by_event_type():
    bus = EventBus()
    bus.start()
    received: list[Event] = []

    async def handler(event: Event) -> None:
        received.append(event)

    bus.subscribe(
        Subscription(
            filter=EventFilter(event_types=frozenset({"turn.started"})),
            handler=handler,
            name="t",
            fast_path=True,
        )
    )
    bus.emit(_session_created())
    bus.emit(_turn_started())
    bus.emit(_llm_completed())
    await bus.drain()
    await bus.stop()

    assert [e.type for e in received] == ["turn.started"]


async def test_filter_by_session_id():
    bus = EventBus()
    bus.start()
    received: list[Event] = []

    async def handler(event: Event) -> None:
        received.append(event)

    bus.subscribe(
        Subscription(
            filter=EventFilter(session_ids=frozenset({"sess_keep"})),
            handler=handler,
            name="t",
            fast_path=True,
        )
    )
    bus.emit(_session_created(session_id="sess_keep"))
    bus.emit(_session_created(session_id="sess_drop"))
    bus.emit(_turn_started(session_id="sess_keep"))
    await bus.drain()
    await bus.stop()

    assert {e.session_id for e in received} == {"sess_keep"}
    assert len(received) == 2


async def test_filter_by_actor():
    bus = EventBus()
    bus.start()
    received: list[Event] = []

    async def handler(event: Event) -> None:
        received.append(event)

    bus.subscribe(
        Subscription(
            filter=EventFilter(actors=frozenset({Actor.USER})),
            handler=handler,
            name="t",
            fast_path=True,
        )
    )
    bus.emit(_session_created())  # SYSTEM
    bus.emit(_turn_started())  # USER
    bus.emit(_llm_completed())  # AGENT
    await bus.drain()
    await bus.stop()

    assert {e.actor for e in received} == {Actor.USER}


# ---- Validation ---------------------------------------------------------


async def test_strict_mode_raises_on_unknown_type():
    bus = EventBus(mode=ValidationMode.STRICT)
    bus.start()

    bad = Event(
        id="01HZ",
        timestamp=_now(),
        session_id="s",
        type="not.in.catalog",
        actor=Actor.SYSTEM,
        payload={},
        sensitivity=Sensitivity.PSEUDONYMOUS,
    )
    with pytest.raises(UnknownEventTypeError):
        bus.emit(bad)
    await bus.stop()


async def test_lenient_mode_drops_unknown_type(caplog):
    bus = EventBus(mode=ValidationMode.LENIENT)
    bus.start()
    bad = Event(
        id="01HZ",
        timestamp=_now(),
        session_id="s",
        type="not.in.catalog",
        actor=Actor.SYSTEM,
        payload={},
        sensitivity=Sensitivity.PSEUDONYMOUS,
    )
    with caplog.at_level(logging.WARNING):
        bus.emit(bad)  # should not raise
    await bus.drain()
    await bus.stop()
    assert any("unknown event" in rec.message for rec in caplog.records)


async def test_strict_mode_raises_on_malformed_payload():
    bus = EventBus(mode=ValidationMode.STRICT)
    bus.start()
    bad = Event(
        id="01HZ",
        timestamp=_now(),
        session_id="s",
        type="session.created",
        actor=Actor.SYSTEM,
        payload={"missing": "required fields"},  # no workspace_path
        sensitivity=Sensitivity.PSEUDONYMOUS,
    )
    with pytest.raises(EventValidationError):
        bus.emit(bad)
    await bus.stop()


# ---- Backpressure -------------------------------------------------------


async def test_overflow_raises_when_queue_full():
    bus = EventBus(queue_size=3)
    # Don't start the dispatch worker; let the queue fill.
    bus.emit(_session_created())
    bus.emit(_session_created())
    bus.emit(_session_created())
    with pytest.raises(EventBusOverflowError) as exc:
        bus.emit(_session_created())
    assert exc.value.queue_size == 3
    assert exc.value.rejected_type == "session.created"


# ---- Handler error isolation -------------------------------------------


async def test_handler_error_does_not_block_other_subscribers(caplog):
    bus = EventBus()
    bus.start()
    good: list[Event] = []

    async def bad(event: Event) -> None:
        raise RuntimeError("boom")

    async def ok(event: Event) -> None:
        good.append(event)

    bus.subscribe(Subscription(filter=EventFilter(), handler=bad, name="bad", fast_path=True))
    bus.subscribe(Subscription(filter=EventFilter(), handler=ok, name="ok", fast_path=True))
    with caplog.at_level(logging.WARNING):
        bus.emit(_session_created())
        await bus.drain()
    await bus.stop()

    assert len(good) == 1
    assert any("event handler raised" in rec.message for rec in caplog.records)


async def test_unsubscribe_stops_delivery():
    bus = EventBus()
    bus.start()
    received: list[Event] = []

    async def handler(event: Event) -> None:
        received.append(event)

    handle = bus.subscribe(
        Subscription(filter=EventFilter(), handler=handler, name="t", fast_path=True)
    )
    bus.emit(_session_created())
    await bus.drain()
    bus.unsubscribe(handle)
    bus.emit(_session_created())
    await bus.drain()
    await bus.stop()

    assert len(received) == 1


async def test_unsubscribe_is_idempotent():
    bus = EventBus()

    async def handler(event: Event) -> None:
        pass

    handle = bus.subscribe(
        Subscription(filter=EventFilter(), handler=handler, name="t", fast_path=True)
    )
    bus.unsubscribe(handle)
    bus.unsubscribe(handle)  # second call must not raise
