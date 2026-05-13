"""Tests for the Event envelope and id generation."""

from __future__ import annotations

from datetime import UTC, datetime

import msgspec
from metis_core.events.envelope import Actor, Event, Sensitivity, new_event_id


def test_event_roundtrip():
    event = Event(
        id=new_event_id(),
        timestamp=datetime.now(UTC),
        session_id="sess_x",
        type="turn.started",
        actor=Actor.USER,
        payload={"user_message_hash": "abc", "estimated_input_tokens": 10},
        sensitivity=Sensitivity.PRIVATE,
    )
    encoded = msgspec.json.encode(event)
    decoded = msgspec.json.decode(encoded, type=Event)
    assert decoded == event


def test_event_optional_fields_default_none():
    event = Event(
        id=new_event_id(),
        timestamp=datetime.now(UTC),
        session_id="sess_x",
        type="session.created",
        actor=Actor.SYSTEM,
        payload={},
        sensitivity=Sensitivity.PSEUDONYMOUS,
    )
    assert event.turn_id is None
    assert event.parent_event_id is None


def test_event_ids_are_monotonic():
    ids = [new_event_id() for _ in range(100)]
    assert len(set(ids)) == 100
    assert ids == sorted(ids)


def test_actor_values_are_lowercase():
    assert Actor.USER.value == "user"
    assert Actor.SYSTEM.value == "system"
    assert Actor.WORKER.value == "worker"


def test_sensitivity_values():
    assert Sensitivity.PRIVATE.value == "private"
    assert Sensitivity.AGGREGATABLE.value == "aggregatable"
