"""Phase 1 event payload schemas + catalog registry.

See event-bus-and-trace-catalog.md §6. Each entry is a typed msgspec.Struct
plus a default sensitivity. The PAYLOAD_REGISTRY maps event type names to
(payload_class, default_sensitivity) pairs and is the closed catalog.

To add a new event type: define the Struct, add to PAYLOAD_REGISTRY, update
the spec, log to CHANGES.md. New types are deliberate spec changes.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

import msgspec

from metis_core.events.envelope import Actor, Event, Sensitivity, new_event_id, sensitivity_rank
from metis_core.events.errors import EventValidationError, UnknownEventTypeError

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
    # Supplementary content/judgement fields downstream subscribers (the
    # evaluator's content-penalty path; future LLM judge tier) need but
    # the lifecycle event itself doesn't structurally model. Keys are
    # conventions, not contract: `final_response_text` carries the
    # assistant's terminal text blocks so the evaluator subscriber path
    # can fire the refusal / empty-response penalties from `evaluator.md`
    # §5.1. Bus emitters set fields they have; subscribers treat missing
    # keys as "no signal," not as an error.
    signals_extra: dict | None = None
    # Multi-user identity dimensions (multi-user.md §3, §4.4). Mirrors the
    # `LLMCallCompleted` stamping so the analytics surface can roll up by
    # user/team at the turn grain (matches the pattern of `gateway_key_id`).
    # `None` for agent-loop traffic and pre-multi-user gateway keys; rolls up
    # under the null bucket per multi-user.md §3.4.
    user_id: str | None = None
    team_id: str | None = None
    # Delegation dimension (delegation.md §8.1). Set on every turn that ran
    # inside a worker session; analytics joins worker spend back to the
    # planner via this field. `None` for non-worker sessions.
    parent_session_id: str | None = None


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
    parent_session_id: str | None = None


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
    # Gateway dimensions (gateway.md §6). Both `None` when the call originated
    # from the in-process agent loop (CLI / TUI / `metis serve`); set when the
    # call entered through the gateway HTTP surface so analytics can roll up by
    # key and inbound translator.
    gateway_key_id: str | None = None
    inbound_shape: Literal["openai", "anthropic"] | None = None
    # Multi-user identity dimensions (multi-user.md §3, §4.4). Stable
    # principal ids resolved from the gateway key at request entry; agent-loop
    # traffic and pre-multi-user gateway keys leave both as `None` and roll up
    # under the null bucket per multi-user.md §3.4. Both are pseudonymous
    # identifiers — no plaintext PII; emails live in `users.json` only
    # (multi-user.md §3.3).
    user_id: str | None = None
    team_id: str | None = None
    # Delegation dimension (delegation.md §8.1). Set on every LLM call made
    # from inside a worker session; the analytics surface joins worker spend
    # back to the planner via this field. `None` for non-worker sessions and
    # all gateway traffic (gateway never delegates).
    parent_session_id: str | None = None


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


# --- §6.5b Pattern domain (Phase 2.5) ---------------------------------------

FingerprintKindLiteral = Literal["structural", "hybrid"]


class PatternRecorded(msgspec.Struct, frozen=True):
    """`pattern.recorded` per pattern-store.md §10.1.

    Emitted by the session-ended batch subscriber after a fingerprint/outcome
    pair is upserted into the pattern store. One event per (fingerprint,
    primary_model) write.

    The cost field is named `cost_usd_at_record` (not `cost_usd`) to
    disambiguate from `llm.call_completed.cost_usd` and to follow the
    `Decimal` convention from canonical-message-format.md §6.4. Pattern-store
    spec §10.1 currently drafts the field as `cost_usd`; this rename is
    flagged for the Wave 4 reconciliation sweep.
    """

    fingerprint_id: str
    fingerprint_kind: FingerprintKindLiteral
    primary_model: str
    sample_size_before: int
    sample_size_after: int
    was_new_fingerprint: bool
    success_score: float | None
    cost_usd_at_record: Decimal
    pricing_version: str
    over_soft_cap: bool


class PatternMatched(msgspec.Struct, frozen=True):
    """`pattern.matched` per pattern-store.md §10.2.

    Emitted when the routing engine's slot 4 wins (the pattern policy chose
    the model used for the turn). Distinct from `route.decided` so consumers
    can query "how often does pattern routing fire?" without a JSON scan
    over `route.decided.chain`. Not emitted when the pattern policy deferred.
    """

    fingerprint_id: str
    fingerprint_kind: FingerprintKindLiteral
    chosen_model: str
    confidence: float
    sample_size: int
    k_cluster_size: int
    alternatives_count: int


PatternEvictionTrigger = Literal[
    "soft_cap_signal",
    "hard_cap_evict",
    "age_trim",
    "manual_clear",
]


class PatternEvicted(msgspec.Struct, frozen=True):
    """`pattern.evicted` per pattern-store.md §10.3.

    Mirrors `memory.eviction`. Fired on soft-cap signal, hard-cap auto-evict,
    age-based continuous trim, or manual `/patterns clear`. Counts and ages
    only — no content.
    """

    trigger: PatternEvictionTrigger
    fingerprints_before: int
    fingerprints_after: int
    outcomes_before: int
    outcomes_after: int
    entries_evicted: int
    oldest_evicted_age_days: float | None = None


# --- §6.12 Eval domain (Phase 3) --------------------------------------------

EvalSubjectKind = Literal["turn", "tool_cycle", "session", "workload"]
EvalJudgeKind = Literal["heuristic", "llm", "hybrid"]
EvalTrigger = Literal["bus", "batch", "feedback_arrived", "benchmark"]
EvalFailureMode = Literal[
    "judge_output_invalid",
    "judge_call_failed",
    "throttled_no_heuristic",
    "subject_not_found",
    "rubric_invalid",
]


class EvalStarted(msgspec.Struct, frozen=True):
    """`eval.started` per evaluator.md §8.1.

    Emitted when the evaluator begins scoring a subject. Pairs 1:1 with a
    later `eval.completed` or `eval.failed` carrying the same `eval_id`.
    """

    eval_id: str
    subject_kind: EvalSubjectKind
    subject_id: str
    rubric_id: str
    rubric_version: str
    judge_kind_planned: EvalJudgeKind
    trigger: EvalTrigger


class EvalCompleted(msgspec.Struct, frozen=True):
    """`eval.completed` per evaluator.md §8.2.

    `judge_cost_usd` is `Decimal` (serialized as string by msgspec, mirroring
    `Usage.cost_usd` per canonical-message-format.md §6.4). `signals` is an
    opaque JSON-roundtrippable dict; rationale-redacted opt-in fields inside
    it trigger sensitivity uplift per §4.4.1 (caller passes the elevated
    sensitivity to `make_event`).
    """

    eval_id: str
    subject_kind: EvalSubjectKind
    subject_id: str
    score: float
    confidence: float
    judge_kind: EvalJudgeKind
    judge_cost_usd: Decimal
    judge_latency_ms: int
    rubric_id: str
    rubric_version: str
    signals: dict
    judge_model: str | None = None
    judge_pricing_version: str | None = None
    parent_eval_id: str | None = None


class EvalFailed(msgspec.Struct, frozen=True):
    """`eval.failed` per evaluator.md §8.3.

    Emitted instead of `eval.completed` when the judge couldn't produce a
    verdict (LLM parse failure, missing subject, rubric load failure, etc.).
    """

    eval_id: str
    subject_kind: EvalSubjectKind
    subject_id: str
    failure_mode: EvalFailureMode
    error_message: str
    judge_latency_ms: int


# --- §6.4 Gateway quota domain (multi-user.md §5, §7.2) ---------------------

QuotaScopeLiteral = Literal[
    "key_daily",
    "key_monthly",
    "user_daily",
    "user_monthly",
    "team_daily",
    "team_monthly",
]

QuotaSeverityLiteral = Literal["warning", "critical"]

InboundShapeLiteral = Literal["openai", "anthropic"]


class QuotaAlert(msgspec.Struct, frozen=True):
    """`quota.alert` per multi-user.md §5 / gateway.md §6.4.

    Soft alert emitted when an authenticated request lands on a key /
    user / team whose spend has crossed a configured warn threshold
    (`warning` at 80%, `critical` at 95%) but is still below the hard
    breaker (1.0). One event per request that crosses the threshold —
    no alert when spend stays under 80%, no alert when the hard breaker
    fires (the `gateway.quota_exceeded` event covers that case).

    `current_usd` and `limit_usd` are Decimals (the same convention as
    `EvalCompleted.judge_cost_usd`). `percentage` is `float(current/limit)`,
    convenient for SPA rendering.
    """

    scope: QuotaScopeLiteral
    severity: QuotaSeverityLiteral
    current_usd: Decimal
    limit_usd: Decimal
    percentage: float
    gateway_key_id: str | None = None
    user_id: str | None = None
    team_id: str | None = None


class GatewayQuotaExceeded(msgspec.Struct, frozen=True):
    """`gateway.quota_exceeded` per multi-user.md §7.2.

    Hard-cap rejection: an inbound gateway request hit a configured cap
    and the harness short-circuited before routing/adapter invocation.
    The HTTP layer concurrently returns 429 with the documented body
    (gateway.md §6.4); this event is the audit trail.
    """

    scope: QuotaScopeLiteral
    current_usd: Decimal
    limit_usd: Decimal
    inbound_shape: InboundShapeLiteral
    gateway_key_id: str | None = None
    user_id: str | None = None
    team_id: str | None = None


# --- §6.8 Delegate domain (delegation.md §9; v1 MVP) -----------------------

DelegateTierLiteral = Literal["fast", "balanced", "deep"]
DelegateContextModeLiteral = Literal["minimal", "explicit"]
DelegateFailureModeLiteral = Literal[
    "worker_error",
    "max_tokens_exceeded",
    "insufficient_context",
    "output_schema_validation_failed",
    "no_model_available_for_tier",
    "cancelled_by_user",
]


class DelegateStarted(msgspec.Struct, frozen=True):
    """`delegate.started` per event-bus-and-trace-catalog §6.8.

    Emitted by the `delegate()` tool body after the worker session has been
    created and immediately before the worker's turn loop runs. `tool_use_id`
    is the planner's `delegate()` tool_use_id; the worker session is the row
    created with `parent_tool_use_id == tool_use_id`.
    """

    tool_use_id: str
    worker_session_id: str
    tier: DelegateTierLiteral
    resolved_model: str
    context_mode: DelegateContextModeLiteral
    context_reference_count: int
    task_size_tokens: int
    allowed_tool_count: int
    dropped_tools: list[str]


class DelegateCompleted(msgspec.Struct, frozen=True):
    """`delegate.completed` per event-bus-and-trace-catalog §6.8.

    Emitted when the worker session ends with `disposition: completed` and
    the delegate-tool body has the worker's final `TurnResult`. The cost
    summary is **derived** — analytics joins worker spend via
    `llm.call_completed.parent_session_id` (delegation.md §8.3).
    """

    tool_use_id: str
    worker_session_id: str
    success: bool
    output_size_bytes: int
    worker_total_cost_usd: Decimal
    pricing_version: str
    turn_count: int
    llm_call_count: int
    tool_call_count: int
    wall_time_seconds: float
    model: str


class DelegateFailed(msgspec.Struct, frozen=True):
    """`delegate.failed` per event-bus-and-trace-catalog §6.8.

    Emitted instead of `delegate.completed` when the worker couldn't produce a
    usable result (no model for tier, worker raised, schema validation
    failed, cancelled mid-flight, etc.). Partial cost is still recorded.
    """

    tool_use_id: str
    worker_session_id: str | None  # None when failure precedes session creation
    failure_mode: DelegateFailureModeLiteral
    error_message: str
    worker_total_cost_usd: Decimal
    pricing_version: str


# --- §6.13 Gateway admin domain (gateway.md §11 — Wave 10 key lifecycle) ----

GatewayKeyRevokeReason = Literal["admin_revoke", "grace_period_expired", "rotated"]


class GatewayKeyIssued(msgspec.Struct, frozen=True):
    """`gateway.key_issued` per gateway.md §11.

    Emitted once by `metis gateway issue-key` after the keystore is
    updated. The audit trail records who/what the key is scoped to so
    operators can correlate cost-attribution rows back to the issuance
    event; the plaintext token is never on the bus.

    Both identity tags follow the multi-user.md §3.4 null-bucket
    convention — pre-multi-user issuance leaves them `None`.
    """

    gateway_key_id: str
    name: str
    workspace_path: str
    issued_at: datetime
    user_id: str | None = None
    team_id: str | None = None
    allowed_models: list[str] | None = None
    daily_cap_usd: Decimal | None = None
    monthly_cap_usd: Decimal | None = None


class GatewayKeyRevoked(msgspec.Struct, frozen=True):
    """`gateway.key_revoked` per gateway.md §11.

    Emitted on explicit `metis gateway revoke-key` invocation or when the
    grace-period sweep auto-revokes a rotated predecessor. `reason`
    distinguishes the two paths so dashboards can chart manual revocations
    separately from rotation tail-offs.
    """

    gateway_key_id: str
    revoked_at: datetime
    reason: GatewayKeyRevokeReason


class GatewayKeyRotated(msgspec.Struct, frozen=True):
    """`gateway.key_rotated` per gateway.md §11.

    Emitted by `metis gateway rotate-key`. Carries both the predecessor
    and successor ids so operators can follow the migration in the trace.
    The predecessor stays `active` until `grace_period_until`; after that
    boundary the next admin sweep emits a paired `gateway.key_revoked`
    with `reason="grace_period_expired"`.
    """

    old_gateway_key_id: str
    new_gateway_key_id: str
    grace_period_until: datetime
    workspace_path: str
    user_id: str | None = None
    team_id: str | None = None


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
    # pattern (Phase 2.5)
    "pattern.recorded": (PatternRecorded, Sensitivity.PSEUDONYMOUS),
    "pattern.matched": (PatternMatched, Sensitivity.PSEUDONYMOUS),
    "pattern.evicted": (PatternEvicted, Sensitivity.PSEUDONYMOUS),
    # eval (Phase 3)
    "eval.started": (EvalStarted, Sensitivity.PSEUDONYMOUS),
    "eval.completed": (EvalCompleted, Sensitivity.USER_CONTROLLED),
    "eval.failed": (EvalFailed, Sensitivity.PSEUDONYMOUS),
    # gateway quota (Phase 3 — multi-user.md §5, §7.2)
    "quota.alert": (QuotaAlert, Sensitivity.PSEUDONYMOUS),
    "gateway.quota_exceeded": (GatewayQuotaExceeded, Sensitivity.PSEUDONYMOUS),
    # delegate (Phase 4 v1 MVP — delegation.md §9)
    "delegate.started": (DelegateStarted, Sensitivity.PSEUDONYMOUS),
    "delegate.completed": (DelegateCompleted, Sensitivity.PSEUDONYMOUS),
    "delegate.failed": (DelegateFailed, Sensitivity.PSEUDONYMOUS),
    # gateway admin (Wave 10 — gateway.md §11 key lifecycle)
    "gateway.key_issued": (GatewayKeyIssued, Sensitivity.PSEUDONYMOUS),
    "gateway.key_revoked": (GatewayKeyRevoked, Sensitivity.PSEUDONYMOUS),
    "gateway.key_rotated": (GatewayKeyRotated, Sensitivity.PSEUDONYMOUS),
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
    if sensitivity is not None and sensitivity_rank(sensitivity) < sensitivity_rank(
        default_sensitivity
    ):
        raise EventValidationError(
            type,
            [
                f"sensitivity override {sensitivity.value!r} is more private than the "
                f"catalog floor {default_sensitivity.value!r}; §4.4.1 allows only "
                f"moves toward less private"
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
