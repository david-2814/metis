"""Phase 1 event payload schemas + catalog registry.

See event-bus-and-trace-catalog.md §6. Each entry is a typed msgspec.Struct
plus a default sensitivity. The PAYLOAD_REGISTRY maps event type names to
(payload_class, default_sensitivity) pairs and is the closed catalog.

To add a new event type: define the Struct, add to PAYLOAD_REGISTRY, update
the spec, log to CHANGES.md. New types are deliberate spec changes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

import msgspec

from metis.events.envelope import Actor, Event, Sensitivity, new_event_id
from metis.events.errors import EventValidationError, UnknownEventTypeError

# --- §6.1 Session domain ----------------------------------------------------


class SessionCreated(msgspec.Struct, frozen=True):
    workspace_path: str
    workspace_hash: str
    initial_active_model: str | None
    routing_policy_version: str  # SHA-256 of routing.yaml contents


class SessionResumed(msgspec.Struct, frozen=True):
    workspace_hash: str
    last_event_id_at_resume: str | None = None


class SessionEnded(msgspec.Struct, frozen=True):
    disposition: Literal["completed", "abandoned", "error"]
    turn_count: int
    total_cost_usd: float
    duration_seconds: float


# --- §6.2 Turn domain -------------------------------------------------------


class TurnStarted(msgspec.Struct, frozen=True):
    user_message_hash: str
    estimated_input_tokens: int
    has_images: bool
    has_tool_calls_in_history: bool
    user_message_text_redacted: str | None = None  # populated only on opt-in


class TurnCompleted(msgspec.Struct, frozen=True):
    stop_reason: Literal["end_turn", "max_tokens", "stop_sequence", "tool_use"]
    llm_call_count: int
    tool_call_count: int
    total_input_tokens: int
    total_output_tokens: int
    total_cost_usd: float
    wall_time_seconds: float


class TurnCancelled(msgspec.Struct, frozen=True):
    reason: Literal["user_cancel", "client_disconnect", "timeout"]
    partial_llm_calls: int
    partial_tool_calls: int


# --- §6.3 LLM domain --------------------------------------------------------


class LLMCallStarted(msgspec.Struct, frozen=True):
    model: str  # canonical "provider:name"
    provider: str
    estimated_input_tokens: int
    request_id: str
    is_worker: bool


class LLMCallCompleted(msgspec.Struct, frozen=True):
    model: str
    provider: str
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    cache_creation_input_tokens: int
    cost_usd: float
    pricing_version: str
    latency_ms: int
    stop_reason: Literal["end_turn", "max_tokens", "stop_sequence", "tool_use"]
    produced_tool_calls: int
    produced_thinking_blocks: int


# 8-value error_class enum per provider-adapter §6.1.
LLMErrorClass = Literal[
    "rate_limit",
    "auth",
    "server_error",
    "network",
    "context_overflow",
    "invalid_request",
    "cancelled",
    "other",
]


class LLMCallFailed(msgspec.Struct, frozen=True):
    model: str
    provider: str
    error_class: LLMErrorClass
    error_message_redacted: str
    retry_count: int
    latency_ms: int


# --- §6.4 Tool domain -------------------------------------------------------

ToolSideEffect = Literal["none", "read", "write", "execute", "network"]


class ToolCalled(msgspec.Struct, frozen=True):
    tool_use_id: str
    tool_name: str
    input_hash: str
    input_size_bytes: int
    side_effects: ToolSideEffect


class ToolCompleted(msgspec.Struct, frozen=True):
    tool_use_id: str
    success: bool
    output_size_bytes: int
    latency_ms: int
    files_modified: list[str] | None = None
    command_executed: str | None = None


# 8-value error_class enum per tool-dispatcher §6.1.
ToolErrorClass = Literal[
    "timeout",
    "permission_denied",
    "not_found",
    "validation_error",
    "execution_error",
    "cancelled",
    "user_denied",
    "confirmation_timeout",
]


class ToolFailed(msgspec.Struct, frozen=True):
    tool_use_id: str
    error_class: ToolErrorClass
    error_message: str
    latency_ms: int


class ToolInputInvalid(msgspec.Struct, frozen=True):
    tool_name: str
    validation_errors: list[str]


class ToolConfirmationRequested(msgspec.Struct, frozen=True):
    tool_use_id: str
    tool_name: str
    side_effects: Literal["write", "execute", "network"]
    confirmation_request_id: str
    input_summary: str
    expires_at: datetime
    projected_modifications: list[str] | None = None
    command_summary: str | None = None


class ToolConfirmationResolved(msgspec.Struct, frozen=True):
    tool_use_id: str
    confirmation_request_id: str
    decision: Literal["allow", "deny", "timeout"]
    scope: Literal["once", "session"] | None = None
    responding_client_attach_token: str | None = None


# --- §6.5 Route domain ------------------------------------------------------

RoutingPolicyName = Literal[
    "per_message_override",
    "manual_sticky",
    "rule",
    "pattern",
    "delegate_request",
    "workspace_default",
    "global_default",
]

RoutingVerdict = Literal["not_applicable", "deferred", "rejected", "chose"]

ValidationFailure = Literal[
    "no_vision_support",
    "exceeds_context_window",
    "no_tool_support",
    "no_system_prompt_support",
    "no_structured_output_support",
    "provider_unavailable",
    "not_configured",
]


class PatternAlternative(msgspec.Struct, frozen=True):
    model: str
    score: float
    sample_size: int


class PolicyEvaluation(msgspec.Struct, frozen=True):
    policy: RoutingPolicyName
    verdict: RoutingVerdict
    reason: str
    candidate_model: str | None = None
    rule_name: str | None = None
    confidence: float | None = None
    pattern_alternatives: list[PatternAlternative] | None = None
    validation_failure: ValidationFailure | None = None


class RouteDecided(msgspec.Struct, frozen=True):
    chosen_model: str
    winner_index: int
    elapsed_ms: float
    chain: list[PolicyEvaluation]


class RoutingPolicyInvalid(msgspec.Struct, frozen=True):
    policy_path: str
    errors: list[str]
    using_last_known_good: bool


class RoutingProviderUnavailable(msgspec.Struct, frozen=True):
    provider: str
    scope: Literal["model_specific", "provider_wide"]
    models_affected: list[str]
    trigger_reason: str


class RoutingProviderRecovered(msgspec.Struct, frozen=True):
    provider: str
    scope: Literal["model_specific", "provider_wide"]
    models_recovered: list[str]
    downtime_seconds: float


# --- §6.6 Skill domain ------------------------------------------------------


SkillLoadReason = Literal["always", "on_demand", "auto_suggested"]
SkillSourceLiteral = Literal["global", "workspace"]


class SkillLoaded(msgspec.Struct, frozen=True):
    """`skill.loaded` per event-bus-and-trace-catalog §6.6.

    `source` is an additive field tracking which directory served the
    skill (global ~/.metis/skills/ or per-workspace .metis/skills/) so
    traces can show provenance after a merge.
    """

    skill_id: str
    skill_version: str
    load_reason: SkillLoadReason
    load_size_tokens: int
    source: SkillSourceLiteral
    triggered_by_tool_use_id: str | None = None


# --- §6.7 Memory domain -----------------------------------------------------


MemoryFileLiteral = Literal["MEMORY.md", "USER.md"]


class MemoryUpdated(msgspec.Struct, frozen=True):
    file: MemoryFileLiteral
    operation: Literal["add", "replace", "consolidate"]
    before_hash: str
    after_hash: str
    before_size_bytes: int
    after_size_bytes: int


class MemoryEviction(msgspec.Struct, frozen=True):
    file: MemoryFileLiteral
    trigger: Literal["size_cap_exceeded", "manual"]
    entries_evicted: int
    size_before_bytes: int
    size_after_bytes: int


# --- §6.10 Bus meta-events --------------------------------------------------


class BusSubscriberRegistered(msgspec.Struct, frozen=True):
    subscription_name: str
    filter: dict
    fast_path: bool


class BusSubscriberUnregistered(msgspec.Struct, frozen=True):
    subscription_name: str
    reason: Literal["explicit", "client_disconnect", "shutdown", "removed_after_errors"]


class BusGapDetected(msgspec.Struct, frozen=True):
    session_id: str
    gap_start_id: str
    gap_end_id: str
    estimated_missing_count: int
    detected_at: datetime


# --- Catalog registry -------------------------------------------------------

PAYLOAD_REGISTRY: dict[str, tuple[type[msgspec.Struct], Sensitivity]] = {
    # session
    "session.created": (SessionCreated, Sensitivity.PSEUDONYMOUS),
    "session.resumed": (SessionResumed, Sensitivity.PSEUDONYMOUS),
    "session.ended": (SessionEnded, Sensitivity.PSEUDONYMOUS),
    # turn
    "turn.started": (TurnStarted, Sensitivity.PRIVATE),
    "turn.completed": (TurnCompleted, Sensitivity.PSEUDONYMOUS),
    "turn.cancelled": (TurnCancelled, Sensitivity.PSEUDONYMOUS),
    # llm
    "llm.call_started": (LLMCallStarted, Sensitivity.PRIVATE),
    "llm.call_completed": (LLMCallCompleted, Sensitivity.PSEUDONYMOUS),
    "llm.call_failed": (LLMCallFailed, Sensitivity.PSEUDONYMOUS),
    # tool
    "tool.called": (ToolCalled, Sensitivity.PRIVATE),
    "tool.completed": (ToolCompleted, Sensitivity.PRIVATE),
    "tool.failed": (ToolFailed, Sensitivity.PRIVATE),
    "tool.input_invalid": (ToolInputInvalid, Sensitivity.PSEUDONYMOUS),
    "tool.confirmation_requested": (ToolConfirmationRequested, Sensitivity.PRIVATE),
    "tool.confirmation_resolved": (ToolConfirmationResolved, Sensitivity.PRIVATE),
    # route
    "route.decided": (RouteDecided, Sensitivity.PSEUDONYMOUS),
    "routing.policy_invalid": (RoutingPolicyInvalid, Sensitivity.PSEUDONYMOUS),
    "routing.provider_unavailable": (RoutingProviderUnavailable, Sensitivity.PSEUDONYMOUS),
    "routing.provider_recovered": (RoutingProviderRecovered, Sensitivity.PSEUDONYMOUS),
    # skills (Phase 2)
    "skill.loaded": (SkillLoaded, Sensitivity.PSEUDONYMOUS),
    # memory (Phase 2)
    "memory.updated": (MemoryUpdated, Sensitivity.PRIVATE),
    "memory.eviction": (MemoryEviction, Sensitivity.PRIVATE),
    # bus
    "bus.subscriber_registered": (BusSubscriberRegistered, Sensitivity.PSEUDONYMOUS),
    "bus.subscriber_unregistered": (BusSubscriberUnregistered, Sensitivity.PSEUDONYMOUS),
    "bus.gap_detected": (BusGapDetected, Sensitivity.PSEUDONYMOUS),
}


def payload_for_type(event_type: str) -> type[msgspec.Struct]:
    """Look up the payload class for a registered event type."""
    if event_type not in PAYLOAD_REGISTRY:
        raise UnknownEventTypeError(event_type)
    return PAYLOAD_REGISTRY[event_type][0]


def make_event(
    *,
    type: str,
    session_id: str,
    actor: Actor,
    payload: msgspec.Struct,
    timestamp: datetime,
    turn_id: str | None = None,
    parent_event_id: str | None = None,
    sensitivity: Sensitivity | None = None,
) -> Event:
    """Build an Event from a typed payload struct.

    Validates that the payload's type matches the catalog entry for `type`.
    Converts the struct to a dict payload (suitable for the Event envelope
    and SQLite JSON storage). Uses the registered default sensitivity unless
    explicitly overridden (e.g., for opt-in dynamic upgrades per §4.4.1).
    """
    if type not in PAYLOAD_REGISTRY:
        raise UnknownEventTypeError(type)
    expected_class, default_sensitivity = PAYLOAD_REGISTRY[type]
    if not isinstance(payload, expected_class):
        raise EventValidationError(
            type,
            [
                f"payload class {payload.__class__.__name__} does not match "
                f"registered {expected_class.__name__}"
            ],
        )
    return Event(
        id=new_event_id(),
        timestamp=timestamp,
        session_id=session_id,
        turn_id=turn_id,
        parent_event_id=parent_event_id,
        type=type,
        actor=actor,
        payload=msgspec.to_builtins(payload),
        sensitivity=sensitivity if sensitivity is not None else default_sensitivity,
    )
