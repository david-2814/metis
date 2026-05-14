"""Tests for the event payload catalog and make_event helper."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import msgspec
import pytest
from metis_core.events.envelope import Actor, Sensitivity
from metis_core.events.errors import EventValidationError, UnknownEventTypeError
from metis_core.events.payloads import (
    PAYLOAD_REGISTRY,
    EvalCompleted,
    EvalFailed,
    EvalStarted,
    LLMCallCompleted,
    PatternEvicted,
    PatternMatched,
    PatternRecorded,
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


# --- Pattern domain (§6.5b, Phase 2.5) --------------------------------------


def test_pattern_registry_membership():
    for type_name, expected_class, expected_sens in [
        ("pattern.recorded", PatternRecorded, Sensitivity.PSEUDONYMOUS),
        ("pattern.matched", PatternMatched, Sensitivity.PSEUDONYMOUS),
        ("pattern.evicted", PatternEvicted, Sensitivity.PSEUDONYMOUS),
    ]:
        assert type_name in PAYLOAD_REGISTRY
        cls, sens = PAYLOAD_REGISTRY[type_name]
        assert cls is expected_class
        assert sens is expected_sens


def test_pattern_recorded_roundtrip():
    payload = PatternRecorded(
        fingerprint_id="01HZPATTERN1",
        fingerprint_kind="structural",
        primary_model="anthropic:claude-haiku-4-5",
        sample_size_before=3,
        sample_size_after=4,
        was_new_fingerprint=False,
        success_score=0.82,
        cost_usd_at_record=Decimal("0.001234"),
        pricing_version="pt-2026-05-13",
        over_soft_cap=False,
    )
    data = msgspec.to_builtins(payload)
    # Decimal → string per canonical-format §6.4 convention.
    assert data["cost_usd_at_record"] == "0.001234"
    decoded = msgspec.convert(data, PatternRecorded)
    assert decoded == payload


def test_pattern_recorded_make_event():
    payload = PatternRecorded(
        fingerprint_id="01HZFP",
        fingerprint_kind="structural",
        primary_model="anthropic:claude-haiku-4-5",
        sample_size_before=0,
        sample_size_after=1,
        was_new_fingerprint=True,
        success_score=None,
        cost_usd_at_record=Decimal("0"),
        pricing_version="pt-2026-05-13",
        over_soft_cap=False,
    )
    event = make_event(
        type="pattern.recorded",
        session_id="sess_1",
        actor=Actor.SYSTEM,
        payload=payload,
        timestamp=_now(),
    )
    assert event.type == "pattern.recorded"
    assert event.sensitivity == Sensitivity.PSEUDONYMOUS
    assert event.payload["was_new_fingerprint"] is True
    assert event.payload["success_score"] is None


def test_pattern_matched_roundtrip_and_event():
    payload = PatternMatched(
        fingerprint_id="01HZFP",
        fingerprint_kind="hybrid",
        chosen_model="anthropic:claude-sonnet-4-6",
        confidence=0.72,
        sample_size=12,
        k_cluster_size=10,
        alternatives_count=3,
    )
    data = msgspec.to_builtins(payload)
    assert msgspec.convert(data, PatternMatched) == payload
    event = make_event(
        type="pattern.matched",
        session_id="sess_1",
        turn_id="t_1",
        actor=Actor.SYSTEM,
        payload=payload,
        timestamp=_now(),
    )
    assert event.payload["chosen_model"] == "anthropic:claude-sonnet-4-6"
    assert event.sensitivity == Sensitivity.PSEUDONYMOUS


def test_pattern_evicted_roundtrip_and_event():
    payload = PatternEvicted(
        trigger="hard_cap_evict",
        fingerprints_before=3200,
        fingerprints_after=3000,
        outcomes_before=10100,
        outcomes_after=9800,
        entries_evicted=300,
        oldest_evicted_age_days=190.5,
    )
    data = msgspec.to_builtins(payload)
    assert msgspec.convert(data, PatternEvicted) == payload
    event = make_event(
        type="pattern.evicted",
        session_id="sess_1",
        actor=Actor.SYSTEM,
        payload=payload,
        timestamp=_now(),
    )
    assert event.payload["trigger"] == "hard_cap_evict"
    assert event.payload["entries_evicted"] == 300


def test_pattern_evicted_soft_cap_signal_zero_evicted():
    # soft_cap_signal: signal only, entries_evicted == 0, age field optional.
    payload = PatternEvicted(
        trigger="soft_cap_signal",
        fingerprints_before=1500,
        fingerprints_after=1500,
        outcomes_before=5001,
        outcomes_after=5001,
        entries_evicted=0,
    )
    data = msgspec.to_builtins(payload)
    assert data["oldest_evicted_age_days"] is None
    assert msgspec.convert(data, PatternEvicted) == payload


def test_pattern_make_event_rejects_wrong_payload():
    with pytest.raises(EventValidationError) as exc:
        make_event(
            type="pattern.recorded",
            session_id="sess_1",
            actor=Actor.SYSTEM,
            payload=PatternMatched(  # wrong type for pattern.recorded
                fingerprint_id="x",
                fingerprint_kind="structural",
                chosen_model="m",
                confidence=0.5,
                sample_size=1,
                k_cluster_size=1,
                alternatives_count=1,
            ),
            timestamp=_now(),
        )
    assert "PatternMatched" in str(exc.value)
    assert "PatternRecorded" in str(exc.value)


# --- Eval domain (§6.12, Phase 3) -------------------------------------------


def test_eval_registry_membership():
    for type_name, expected_class, expected_sens in [
        ("eval.started", EvalStarted, Sensitivity.PSEUDONYMOUS),
        ("eval.completed", EvalCompleted, Sensitivity.PSEUDONYMOUS),
        ("eval.failed", EvalFailed, Sensitivity.PSEUDONYMOUS),
    ]:
        assert type_name in PAYLOAD_REGISTRY
        cls, sens = PAYLOAD_REGISTRY[type_name]
        assert cls is expected_class
        assert sens is expected_sens


def test_eval_started_roundtrip_and_event():
    payload = EvalStarted(
        eval_id="01HZEVAL1",
        subject_kind="turn",
        subject_id="t_1",
        rubric_id="turn-hybrid-v1",
        rubric_version="1.0.0",
        judge_kind_planned="hybrid",
        trigger="bus",
    )
    data = msgspec.to_builtins(payload)
    assert msgspec.convert(data, EvalStarted) == payload
    event = make_event(
        type="eval.started",
        session_id="sess_1",
        turn_id="t_1",
        actor=Actor.SYSTEM,
        payload=payload,
        timestamp=_now(),
    )
    assert event.payload["judge_kind_planned"] == "hybrid"
    assert event.sensitivity == Sensitivity.PSEUDONYMOUS


def test_eval_completed_heuristic_zero_cost():
    payload = EvalCompleted(
        eval_id="01HZEVAL2",
        subject_kind="turn",
        subject_id="t_1",
        score=0.9,
        confidence=0.85,
        judge_kind="heuristic",
        judge_cost_usd=Decimal("0"),
        judge_latency_ms=2,
        rubric_id="turn-heuristic-v1",
        rubric_version="1.0.0",
        signals={"flags": ["stop_reason_clean", "no_tool_failure"], "flags_negative": []},
    )
    data = msgspec.to_builtins(payload)
    # Decimal → string per canonical-format §6.4.
    assert data["judge_cost_usd"] == "0"
    assert data["judge_model"] is None
    assert data["judge_pricing_version"] is None
    decoded = msgspec.convert(data, EvalCompleted)
    assert decoded == payload


def test_eval_completed_llm_with_cost():
    payload = EvalCompleted(
        eval_id="01HZEVAL3",
        subject_kind="turn",
        subject_id="t_2",
        score=0.65,
        confidence=0.55,
        judge_kind="hybrid",
        judge_cost_usd=Decimal("0.00342"),
        judge_latency_ms=1850,
        rubric_id="turn-hybrid-v1",
        rubric_version="1.0.0",
        signals={
            "heuristic_score": 0.55,
            "heuristic_confidence": 0.4,
            "escalated": True,
            "rationale_hash": "sha256:abc",
        },
        judge_model="anthropic:claude-haiku-4-5",
        judge_pricing_version="pt-2026-05-13",
        parent_eval_id="01HZEVAL2",
    )
    event = make_event(
        type="eval.completed",
        session_id="sess_1",
        turn_id="t_2",
        actor=Actor.SYSTEM,
        payload=payload,
        timestamp=_now(),
    )
    assert event.payload["judge_cost_usd"] == "0.00342"
    assert event.payload["judge_model"] == "anthropic:claude-haiku-4-5"
    assert event.payload["parent_eval_id"] == "01HZEVAL2"


def test_eval_completed_sensitivity_uplift_on_opt_in():
    """§4.4.1: rationale_redacted opt-in upgrades sensitivity."""
    payload = EvalCompleted(
        eval_id="01HZEVAL4",
        subject_kind="turn",
        subject_id="t_3",
        score=0.7,
        confidence=0.6,
        judge_kind="llm",
        judge_cost_usd=Decimal("0.001"),
        judge_latency_ms=900,
        rubric_id="turn-llm-v1",
        rubric_version="1.0.0",
        signals={
            "rationale_hash": "sha256:xyz",
            "rationale_redacted": "Turn ended cleanly; tool calls succeeded.",
        },
        judge_model="anthropic:claude-haiku-4-5",
        judge_pricing_version="pt-2026-05-13",
    )
    event = make_event(
        type="eval.completed",
        session_id="sess_1",
        actor=Actor.SYSTEM,
        payload=payload,
        timestamp=_now(),
        sensitivity=Sensitivity.USER_CONTROLLED,
    )
    assert event.sensitivity == Sensitivity.USER_CONTROLLED


def test_eval_failed_roundtrip_and_event():
    payload = EvalFailed(
        eval_id="01HZEVAL5",
        subject_kind="turn",
        subject_id="t_4",
        failure_mode="judge_output_invalid",
        error_message="rubric schema parse error: missing 'score' field",
        judge_latency_ms=520,
    )
    data = msgspec.to_builtins(payload)
    assert msgspec.convert(data, EvalFailed) == payload
    event = make_event(
        type="eval.failed",
        session_id="sess_1",
        actor=Actor.SYSTEM,
        payload=payload,
        timestamp=_now(),
    )
    assert event.payload["failure_mode"] == "judge_output_invalid"


def test_eval_make_event_rejects_wrong_payload():
    with pytest.raises(EventValidationError) as exc:
        make_event(
            type="eval.completed",
            session_id="sess_1",
            actor=Actor.SYSTEM,
            payload=EvalStarted(  # wrong type for eval.completed
                eval_id="x",
                subject_kind="turn",
                subject_id="t",
                rubric_id="r",
                rubric_version="v",
                judge_kind_planned="heuristic",
                trigger="bus",
            ),
            timestamp=_now(),
        )
    assert "EvalStarted" in str(exc.value)
    assert "EvalCompleted" in str(exc.value)


def test_payload_for_type_finds_new_types():
    assert payload_for_type("pattern.recorded") is PatternRecorded
    assert payload_for_type("pattern.matched") is PatternMatched
    assert payload_for_type("pattern.evicted") is PatternEvicted
    assert payload_for_type("eval.started") is EvalStarted
    assert payload_for_type("eval.completed") is EvalCompleted
    assert payload_for_type("eval.failed") is EvalFailed
