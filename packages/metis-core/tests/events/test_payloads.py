"""Tests for the event payload catalog and make_event helper."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from metis_core.events.envelope import Actor, Sensitivity
from metis_core.events.errors import EventValidationError, UnknownEventTypeError
from metis_core.events.payloads import (
    PAYLOAD_REGISTRY,
    LLMCallCompleted,
    PolicyEvaluation,
    RouteDecided,
    SessionCreated,
    TurnStarted,
    make_event,
    payload_for_type,
)


def _now() -> datetime:
    return datetime.now(UTC)


def test_catalog_contains_phase1_types():
    expected = {
        "session.created",
        "session.resumed",
        "session.ended",
        "turn.started",
        "turn.completed",
        "turn.cancelled",
        "llm.call_started",
        "llm.call_completed",
        "llm.call_failed",
        "tool.called",
        "tool.completed",
        "tool.failed",
        "tool.input_invalid",
        "tool.confirmation_requested",
        "tool.confirmation_resolved",
        "route.decided",
        "routing.policy_invalid",
        "routing.provider_unavailable",
        "routing.provider_recovered",
        "bus.subscriber_registered",
        "bus.subscriber_unregistered",
        "bus.gap_detected",
    }
    assert expected.issubset(PAYLOAD_REGISTRY.keys())


def test_make_event_session_created():
    payload = SessionCreated(
        workspace_path="/Users/me/code/x",
        workspace_hash="abc123",
        initial_active_model="anthropic:claude-sonnet-4-6",
        routing_policy_version="def456",
    )
    event = make_event(
        type="session.created",
        session_id="sess_1",
        actor=Actor.SYSTEM,
        payload=payload,
        timestamp=_now(),
    )
    assert event.type == "session.created"
    assert event.sensitivity == Sensitivity.PSEUDONYMOUS
    assert event.payload["workspace_path"] == "/Users/me/code/x"


def test_make_event_uses_registered_default_sensitivity():
    """turn.started is PRIVATE by default."""
    event = make_event(
        type="turn.started",
        session_id="sess_1",
        actor=Actor.USER,
        payload=TurnStarted(
            user_message_hash="h",
            estimated_input_tokens=5,
            has_images=False,
            has_tool_calls_in_history=False,
        ),
        timestamp=_now(),
    )
    assert event.sensitivity == Sensitivity.PRIVATE


def test_make_event_allows_explicit_sensitivity_upgrade():
    """§4.4.1: opt-in payloads can upgrade sensitivity at emit time."""
    event = make_event(
        type="turn.started",
        session_id="sess_1",
        actor=Actor.USER,
        payload=TurnStarted(
            user_message_hash="h",
            user_message_text_redacted="hello",  # opt-in field populated
            estimated_input_tokens=5,
            has_images=False,
            has_tool_calls_in_history=False,
        ),
        timestamp=_now(),
        sensitivity=Sensitivity.USER_CONTROLLED,
    )
    assert event.sensitivity == Sensitivity.USER_CONTROLLED


def test_make_event_rejects_wrong_payload_class():
    with pytest.raises(EventValidationError) as exc:
        make_event(
            type="session.created",
            session_id="sess_1",
            actor=Actor.SYSTEM,
            payload=TurnStarted(  # wrong type for session.created
                user_message_hash="x",
                estimated_input_tokens=0,
                has_images=False,
                has_tool_calls_in_history=False,
            ),
            timestamp=_now(),
        )
    assert "TurnStarted" in str(exc.value)
    assert "SessionCreated" in str(exc.value)


def test_make_event_rejects_unknown_type():
    with pytest.raises(UnknownEventTypeError):
        make_event(
            type="not.in.catalog",
            session_id="sess_1",
            actor=Actor.SYSTEM,
            payload=SessionCreated(
                workspace_path="x",
                workspace_hash="y",
                initial_active_model=None,
                routing_policy_version="z",
            ),
            timestamp=_now(),
        )


def test_payload_for_type_lookup():
    assert payload_for_type("session.created") is SessionCreated
    assert payload_for_type("llm.call_completed") is LLMCallCompleted
    with pytest.raises(UnknownEventTypeError):
        payload_for_type("nope")


def test_route_decided_with_chain():
    chain = [
        PolicyEvaluation(
            policy="rule",
            verdict="rejected",
            candidate_model="anthropic:claude-opus-4-7",
            reason="rule 'deep for architecture' matched",
            rule_name="deep for architecture",
            validation_failure="provider_unavailable",
        ),
        PolicyEvaluation(
            policy="workspace_default",
            verdict="chose",
            candidate_model="anthropic:claude-sonnet-4-6",
            reason="workspace default",
        ),
    ]
    payload = RouteDecided(
        chosen_model="anthropic:claude-sonnet-4-6",
        winner_index=1,
        elapsed_ms=2.3,
        chain=chain,
    )
    event = make_event(
        type="route.decided",
        session_id="sess_1",
        actor=Actor.SYSTEM,
        payload=payload,
        timestamp=_now(),
        turn_id="01HZ_t1",
    )
    assert event.payload["chosen_model"] == "anthropic:claude-sonnet-4-6"
    assert len(event.payload["chain"]) == 2
    assert event.payload["chain"][0]["verdict"] == "rejected"
