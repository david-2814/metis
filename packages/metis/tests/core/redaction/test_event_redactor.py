"""Unit tests for `EventRedactor` and the per-mode rule sets.

See `docs/specs/redaction.md`. Covers each mode against synthetic events,
idempotence, determinism, salt-based correlation-breaking, and the
PRIVATE-tier strip targets.
"""

from __future__ import annotations

from datetime import UTC, datetime

from metis.core.events.envelope import Actor
from metis.core.events.payloads import (
    GatewayKeyIssued,
    LLMCallCompleted,
    SessionCreated,
    ToolCompleted,
    ToolFailed,
    TurnCompleted,
    TurnStarted,
    make_event,
)
from metis.core.redaction import (
    PSEUDONYM_PREFIX,
    REDACTED_SENTINEL,
    EventRedactor,
    RedactionMode,
    pseudonym_for,
    pseudonymize_value,
)
from metis.core.redaction.modes import PseudonymTag


def _llm_call_completed_event(
    *,
    user_id: str | None = "alice",
    team_id: str | None = "team_a",
    session_id: str = "sess-1",
    turn_id: str | None = "turn-1",
    cost: float = 0.005,
) -> object:
    return make_event(
        type="llm.call_completed",
        session_id=session_id,
        turn_id=turn_id,
        actor=Actor.AGENT,
        payload=LLMCallCompleted(
            model="anthropic:claude-haiku-4-5",
            provider="anthropic",
            input_tokens=10,
            output_tokens=20,
            cached_input_tokens=0,
            cache_creation_input_tokens=0,
            cost_usd=cost,
            pricing_version="v1",
            latency_ms=100,
            stop_reason="end_turn",
            produced_tool_calls=0,
            produced_thinking_blocks=0,
            gateway_key_id="gk_abc",
            user_id=user_id,
            team_id=team_id,
        ),
        timestamp=datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC),
    )


def _turn_started_event(*, text: str | None = "raw user prompt") -> object:
    return make_event(
        type="turn.started",
        session_id="sess-1",
        turn_id="turn-1",
        actor=Actor.USER,
        payload=TurnStarted(
            user_message_hash="hash",
            estimated_input_tokens=5,
            has_images=False,
            has_tool_calls_in_history=False,
            user_message_text_redacted=text,
        ),
        timestamp=datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC),
    )


def _tool_completed_event() -> object:
    return make_event(
        type="tool.completed",
        session_id="sess-1",
        turn_id="turn-1",
        actor=Actor.TOOL,
        payload=ToolCompleted(
            tool_use_id="tu_1",
            success=True,
            output_size_bytes=100,
            latency_ms=50,
            files_modified=["/home/user/secret.py"],
            command_executed="cat /etc/passwd",
        ),
        timestamp=datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC),
    )


def _tool_failed_event() -> object:
    return make_event(
        type="tool.failed",
        session_id="sess-1",
        turn_id="turn-1",
        actor=Actor.TOOL,
        payload=ToolFailed(
            tool_use_id="tu_1",
            error_class="execution_error",
            error_message="user prompt: rm -rf /",
            latency_ms=50,
        ),
        timestamp=datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC),
    )


def _turn_completed_with_signals(
    *,
    user_prompt: str = "what's the weather",
    assistant_response: str = "I cannot check the weather",
) -> object:
    return make_event(
        type="turn.completed",
        session_id="sess-1",
        turn_id="turn-1",
        actor=Actor.AGENT,
        payload=TurnCompleted(
            stop_reason="end_turn",
            llm_call_count=1,
            tool_call_count=0,
            total_input_tokens=10,
            total_output_tokens=20,
            total_cost_usd=0.001,
            wall_time_seconds=1.0,
            signals_extra={
                "user_prompt_text": user_prompt,
                "assistant_response_text": assistant_response,
                "grounding_check_passed": True,
            },
            user_id="alice",
            team_id="team_a",
        ),
        timestamp=datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC),
    )


def _gateway_key_issued_event() -> object:
    return make_event(
        type="gateway.key_issued",
        session_id="admin",
        actor=Actor.SYSTEM,
        payload=GatewayKeyIssued(
            gateway_key_id="gk_secret",
            name="alice",
            workspace_path="/srv/alice/private",
            issued_at=datetime(2026, 5, 15, tzinfo=UTC),
            user_id="alice",
            team_id="team_a",
        ),
        timestamp=datetime(2026, 5, 15, tzinfo=UTC),
    )


def _session_created_event() -> object:
    return make_event(
        type="session.created",
        session_id="sess-1",
        actor=Actor.SYSTEM,
        payload=SessionCreated(
            workspace_path="/srv/alice/private",
            workspace_hash="ws_hash_already_a_digest",
            initial_active_model=None,
            routing_policy_version="rp_v1",
        ),
        timestamp=datetime(2026, 5, 15, tzinfo=UTC),
    )


# ---- passthrough mode ------------------------------------------------------


def test_passthrough_returns_event_unchanged():
    event = _llm_call_completed_event()
    redactor = EventRedactor(RedactionMode.PASSTHROUGH)
    result = redactor.redact(event)
    assert result is event
    assert redactor.finalize() is None


# ---- pseudonymize mode -----------------------------------------------------


def test_pseudonymize_hashes_envelope_identity():
    event = _llm_call_completed_event()
    redactor = EventRedactor(RedactionMode.PSEUDONYMIZE)
    result = redactor.redact(event)
    assert result is not None
    assert result.session_id != "sess-1"
    assert result.session_id.startswith(PSEUDONYM_PREFIX)
    assert "sess" in result.session_id
    assert result.turn_id is not None
    assert result.turn_id.startswith(PSEUDONYM_PREFIX)
    # Envelope-level non-identity fields preserved
    assert result.id == event.id
    assert result.timestamp == event.timestamp
    assert result.actor == event.actor
    assert result.type == event.type
    assert result.sensitivity == event.sensitivity


def test_pseudonymize_hashes_payload_identity_fields():
    event = _llm_call_completed_event(user_id="alice", team_id="team_a")
    redactor = EventRedactor(RedactionMode.PSEUDONYMIZE)
    result = redactor.redact(event)
    assert result is not None
    assert result.payload["user_id"] != "alice"
    assert result.payload["user_id"].startswith(PSEUDONYM_PREFIX)
    assert result.payload["team_id"].startswith(PSEUDONYM_PREFIX)
    assert result.payload["gateway_key_id"].startswith(PSEUDONYM_PREFIX)
    # Cost / token fields kept verbatim
    assert result.payload["cost_usd"] == 0.005
    assert result.payload["input_tokens"] == 10
    assert result.payload["model"] == "anthropic:claude-haiku-4-5"
    assert result.payload["provider"] == "anthropic"


def test_pseudonymize_preserves_null_identity_fields():
    """`user_id=None` (agent-loop traffic) must stay None, not be hashed."""
    event = _llm_call_completed_event(user_id=None, team_id=None)
    redactor = EventRedactor(RedactionMode.PSEUDONYMIZE)
    result = redactor.redact(event)
    assert result is not None
    assert result.payload["user_id"] is None
    assert result.payload["team_id"] is None


def test_pseudonymize_does_not_redact_private_text():
    """PRIVATE-tier text fields stay verbatim under pseudonymize."""
    event = _turn_started_event(text="my actual prompt")
    redactor = EventRedactor(RedactionMode.PSEUDONYMIZE)
    result = redactor.redact(event)
    assert result is not None
    assert result.payload["user_message_text_redacted"] == "my actual prompt"


def test_pseudonymize_leaves_already_hashed_workspace_hash_alone():
    event = _session_created_event()
    redactor = EventRedactor(RedactionMode.PSEUDONYMIZE)
    result = redactor.redact(event)
    assert result is not None
    # workspace_hash is already a SHA-256 fingerprint; not re-hashed
    assert result.payload["workspace_hash"] == "ws_hash_already_a_digest"
    # workspace_path IS hashed (it's a plaintext identifier)
    assert result.payload["workspace_path"].startswith(PSEUDONYM_PREFIX)


# ---- redact_private mode ---------------------------------------------------


def test_redact_private_strips_user_message_text():
    event = _turn_started_event(text="my actual prompt")
    redactor = EventRedactor(RedactionMode.REDACT_PRIVATE)
    result = redactor.redact(event)
    assert result is not None
    assert result.payload["user_message_text_redacted"] == REDACTED_SENTINEL


def test_redact_private_strips_tool_completed_text_fields():
    event = _tool_completed_event()
    redactor = EventRedactor(RedactionMode.REDACT_PRIVATE)
    result = redactor.redact(event)
    assert result is not None
    # files_modified is a list — every entry replaced with sentinel
    assert result.payload["files_modified"] == [REDACTED_SENTINEL]
    assert result.payload["command_executed"] == REDACTED_SENTINEL
    # Non-text fields kept
    assert result.payload["tool_use_id"] == "tu_1"
    assert result.payload["success"] is True
    assert result.payload["latency_ms"] == 50


def test_redact_private_strips_tool_failed_error_message():
    event = _tool_failed_event()
    redactor = EventRedactor(RedactionMode.REDACT_PRIVATE)
    result = redactor.redact(event)
    assert result is not None
    assert result.payload["error_message"] == REDACTED_SENTINEL


def test_redact_private_strips_signals_extra_text_keys():
    event = _turn_completed_with_signals()
    redactor = EventRedactor(RedactionMode.REDACT_PRIVATE)
    result = redactor.redact(event)
    assert result is not None
    assert result.payload["signals_extra"]["user_prompt_text"] == REDACTED_SENTINEL
    assert result.payload["signals_extra"]["assistant_response_text"] == REDACTED_SENTINEL
    # Non-text signal keys preserved
    assert result.payload["signals_extra"]["grounding_check_passed"] is True


def test_redact_private_also_pseudonymizes_identity():
    """REDACT_PRIVATE is a superset of PSEUDONYMIZE."""
    event = _llm_call_completed_event()
    redactor = EventRedactor(RedactionMode.REDACT_PRIVATE)
    result = redactor.redact(event)
    assert result is not None
    assert result.session_id.startswith(PSEUDONYM_PREFIX)
    assert result.payload["user_id"].startswith(PSEUDONYM_PREFIX)


# ---- aggregate_only mode ---------------------------------------------------


def test_aggregate_only_returns_none_per_event_and_finalizes_to_dict():
    redactor = EventRedactor(RedactionMode.AGGREGATE_ONLY)
    events = [
        _llm_call_completed_event(user_id="alice", cost=0.005),
        _llm_call_completed_event(user_id="bob", cost=0.010),
        _turn_started_event(),
    ]
    for event in events:
        assert redactor.redact(event) is None
    agg = redactor.finalize()
    assert agg is not None
    assert agg["event_count"] == 3
    assert agg["llm_call_count"] == 2
    assert agg["distinct_users"] == 2
    assert agg["distinct_sessions"] == 1
    assert agg["events_by_type"] == {"llm.call_completed": 2, "turn.started": 1}
    # cost_usd is a float on LLMCallCompleted; Decimal(str(float)) round-trips
    # via Python's default repr which strips trailing zeros (0.010 -> "0.01").
    assert agg["cost_usd_sum"] == "0.015"
    assert agg["cost_usd_min"] == "0.005"
    assert agg["cost_usd_max"] == "0.01"


def test_aggregate_only_handles_empty_stream():
    redactor = EventRedactor(RedactionMode.AGGREGATE_ONLY)
    agg = redactor.finalize()
    assert agg is not None
    assert agg["event_count"] == 0
    assert agg["distinct_users"] == 0
    assert agg["cost_usd_min"] is None


# ---- idempotence (redaction.md §7.2) --------------------------------------


def test_redact_is_idempotent_for_pseudonymize():
    event = _llm_call_completed_event()
    redactor = EventRedactor(RedactionMode.PSEUDONYMIZE)
    once = redactor.redact(event)
    twice = EventRedactor(RedactionMode.PSEUDONYMIZE).redact(once)
    assert twice is not None
    assert once is not None
    assert twice.session_id == once.session_id
    assert twice.payload["user_id"] == once.payload["user_id"]
    assert twice.payload["gateway_key_id"] == once.payload["gateway_key_id"]


def test_redact_is_idempotent_for_redact_private():
    event = _turn_started_event(text="raw prompt")
    redactor = EventRedactor(RedactionMode.REDACT_PRIVATE)
    once = redactor.redact(event)
    twice = EventRedactor(RedactionMode.REDACT_PRIVATE).redact(once)
    assert twice is not None
    assert once is not None
    assert twice.payload["user_message_text_redacted"] == REDACTED_SENTINEL
    assert twice.payload["user_message_text_redacted"] == once.payload["user_message_text_redacted"]


def test_pseudonymize_value_recognizes_already_hashed_input():
    once = pseudonymize_value("alice", PseudonymTag.USER)
    twice = pseudonymize_value(once, PseudonymTag.USER)
    assert once == twice
    assert once.startswith(PSEUDONYM_PREFIX)


# ---- determinism + salt ---------------------------------------------------


def test_redactor_is_deterministic_across_invocations():
    event = _llm_call_completed_event()
    r1 = EventRedactor(RedactionMode.PSEUDONYMIZE).redact(event)
    r2 = EventRedactor(RedactionMode.PSEUDONYMIZE).redact(event)
    assert r1 == r2


def test_salt_breaks_correlation_across_exports():
    event = _llm_call_completed_event()
    no_salt = EventRedactor(RedactionMode.PSEUDONYMIZE).redact(event)
    with_salt = EventRedactor(RedactionMode.PSEUDONYMIZE, salt=b"export-42").redact(event)
    assert no_salt is not None
    assert with_salt is not None
    assert no_salt.payload["user_id"] != with_salt.payload["user_id"]
    # Both still start with the same prefix tag
    assert no_salt.payload["user_id"].startswith(PSEUDONYM_PREFIX)
    assert with_salt.payload["user_id"].startswith(PSEUDONYM_PREFIX)


def test_no_salt_pseudonym_matches_default_pseudonym_for():
    """A pseudonymized user_id matches the GDPR-forget pseudonym byte-for-byte
    when no salt is used. Cross-compatibility with 12a-2's `forget_user`."""
    expected = pseudonym_for("alice")
    actual = pseudonymize_value("alice")
    assert actual == expected


# ---- input immutability ----------------------------------------------------


def test_redact_does_not_mutate_input_event():
    event = _llm_call_completed_event()
    original_payload = dict(event.payload)
    EventRedactor(RedactionMode.REDACT_PRIVATE).redact(event)
    assert event.payload == original_payload
    assert event.session_id == "sess-1"
