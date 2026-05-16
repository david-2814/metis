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


# Reasons the gateway can reject an inbound request at the auth gate.
# Closed set — drives the `metis_gateway_auth_failures_total{reason}` label
# (observability.md §3) and is bounded for cardinality control. `key_revoked`
# covers both explicit revocations and grace-period-expired keys (auth-time
# `is_active(now=…)` returns False in both cases).
GatewayAuthFailureReason = Literal[
    "missing_token",
    "invalid_token",
    "key_revoked",
]


class GatewayAuthFailed(msgspec.Struct, frozen=True):
    """`gateway.auth_failed` per observability.md §3.

    Emitted at the gateway's auth gate when an inbound request is rejected
    before reaching routing / adapters. Drives both
    ``metis_gateway_auth_failures_total{reason}`` (operator-facing rate alert)
    and gives compliance / SIEM ingest a row per failed authentication.

    The payload deliberately omits the raw bearer token; it carries only
    the rejection ``reason``, the ``inbound_shape`` of the rejected
    request, and the SHA-256-hash prefix of the offered token (``token_hash_prefix``,
    8 hex chars) so operators can correlate repeated attempts of the same
    leaked credential without persisting the credential itself. ``gateway_key_id``
    is populated only when the token matched a known key (i.e. the
    ``key_revoked`` reason path).
    """

    reason: GatewayAuthFailureReason
    inbound_shape: InboundShapeLiteral
    token_hash_prefix: str | None = None
    gateway_key_id: str | None = None


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
    # Wave 14b concierge-onboarding tag — optional. Pre-Wave-14b key
    # issuances omit this; pre-existing audit consumers ignore the field.
    customer_tier: str | None = None


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


# --- §6.9.1 GDPR / portability + forget audit (analytics-api.md §4.10) -----


class AnalyticsUserExported(msgspec.Struct, frozen=True):
    """Audit event for a portability export (GET /analytics/user/{user_id}/export).

    Stamped once per successful export, after the stream completes. The
    `subject_user_id` is the user whose data was exported; `requested_by`
    is the caller's identity if known (loopback dashboard → `None`).
    The byte count and row count let an auditor reconcile the export
    artifact against the trace.
    """

    subject_user_id: str
    requested_by: str | None
    row_count: int
    byte_count: int
    window_start: datetime | None
    window_end: datetime | None


class AnalyticsUserForgotten(msgspec.Struct, frozen=True):
    """Audit event for a forget operation (POST /analytics/user/{user_id}/forget).

    Stamped once per call, regardless of how many rows the redactor
    touched (`pseudonymized_rows` is the count). Idempotent re-calls
    still emit an audit event with `pseudonymized_rows = 0` so the
    audit trail records every request, not just the first.
    """

    subject_user_id: str
    pseudonym: str
    requested_by: str | None
    pseudonymized_rows: int


# --- §6.14 Trace domain (Wave 12 — trace-retention.md) ----------------------


class TraceSwept(msgspec.Struct, frozen=True):
    """`trace.swept` per event-bus-and-trace-catalog §6.14.

    Emitted once per `metis trace prune` invocation or `CronJob` firing
    after the `DELETE` statement returns. The event is audit-flagged in
    `PAYLOAD_REGISTRY` so subsequent sweeps preserve sweep history.

    `oldest_kept_timestamp` is None when the DB is empty after the
    sweep — operators can chart "retention floor over time" by tailing
    this field. In `dry_run=True` mode, callers receive a `PurgeResult`
    but no `trace.swept` event is emitted (per trace-retention.md §3.3).
    """

    rows_deleted: int
    rows_audit_exempt: int
    cutoff_timestamp: datetime
    oldest_kept_timestamp: datetime | None
    dry_run: bool
    swept_at: datetime


# --- §6.15 Billing domain (Wave 15 — pricing.md §5.5.4) --------------------
#
# Six audit-flagged events that record the lifecycle of a paying account's
# subscription. The bus never sees a Stripe API key, payment-method id, or
# customer card data — only the opaque ids the gateway needs to correlate
# billing state with the account record (audit-log.md §5.1).
#
# `account_id` mirrors `apps/gateway/.../signup.py`'s `ACCOUNT_ID_PREFIX`-
# prefixed ULID; pre-Wave-15 accounts get a `None` `stripe_customer_id`.
# `tier` is the post-transition tier ("free" / "pro" / "enterprise") so a
# replay of the bus reconstructs the entitlement state without needing
# the BillingStore. `current_period_end` lets dashboards chart upcoming
# renewals without round-tripping to Stripe.


BillingTier = Literal["free", "pro", "enterprise"]


class BillingCustomerCreated(msgspec.Struct, frozen=True):
    """`billing.customer_created` — Stripe customer object created.

    Stamped once per account after the first successful `Customer.create`
    against Stripe. Re-runs against an already-created customer don't
    re-emit (idempotent on the account side).
    """

    account_id: str
    stripe_customer_id: str
    email_sha256: str
    created_at: datetime


class BillingSubscriptionCreated(msgspec.Struct, frozen=True):
    """`billing.subscription_created` — new Subscription against an account.

    `tier` distinguishes Pro vs Enterprise. `pro_seats` is the
    `SubscriptionItem.quantity` for the per-seat line; `enterprise_addon`
    is True when the metered usage SubscriptionItem for the %-of-savings
    add-on is attached at creation (Pro-only subs leave it False).
    """

    account_id: str
    stripe_customer_id: str
    stripe_subscription_id: str
    tier: BillingTier
    pro_seats: int
    enterprise_addon: bool
    current_period_end: datetime
    created_at: datetime


class BillingSubscriptionUpdated(msgspec.Struct, frozen=True):
    """`billing.subscription_updated` — tier change, seat count, or status.

    Stamped on every Stripe `customer.subscription.updated` webhook the
    gateway processes. `previous_status` / `status` are Stripe's literal
    status strings (active / past_due / unpaid / canceled / incomplete /
    incomplete_expired / trialing / paused).
    """

    account_id: str
    stripe_subscription_id: str
    previous_status: str
    status: str
    previous_tier: BillingTier
    tier: BillingTier
    pro_seats: int
    current_period_end: datetime
    updated_at: datetime


class BillingSubscriptionCanceled(msgspec.Struct, frozen=True):
    """`billing.subscription_canceled` — subscription terminated.

    Covers both `cancel_at_period_end=True` (which lands as a webhook on
    period boundary) and immediate cancellation (`cancel_at` set, status
    flips to `canceled`).
    """

    account_id: str
    stripe_subscription_id: str
    canceled_at: datetime
    reason: Literal["user_requested", "payment_failed", "admin"]
    final_period_end: datetime


class BillingInvoicePaid(msgspec.Struct, frozen=True):
    """`billing.invoice_paid` — `invoice.payment_succeeded` webhook fired.

    The amount is what Stripe collected, in cents to avoid float drift on
    cent-level math. The webhook carries the line-item breakdown but v1
    rolls it up to a single `amount_paid_cents` per invoice; the breakdown
    survives in Stripe and the BillingStore's processed-event log.
    """

    account_id: str
    stripe_subscription_id: str
    stripe_invoice_id: str
    amount_paid_cents: int
    paid_at: datetime


class BillingInvoicePaymentFailed(msgspec.Struct, frozen=True):
    """`billing.invoice_payment_failed` — Stripe couldn't collect.

    Stripe retries on its own schedule per the dunning settings; the
    gateway just records the audit trail. After Stripe exhausts its
    retries the subscription transitions to `unpaid` or `canceled`
    via `customer.subscription.updated` — that fires a separate event.
    `attempt_count` mirrors Stripe's `Invoice.attempt_count` so dashboards
    can chart "how many tries before churn."
    """

    account_id: str
    stripe_subscription_id: str
    stripe_invoice_id: str
    amount_due_cents: int
    attempt_count: int
    failed_at: datetime


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
    # gateway auth (Wave 14a — observability.md §3 — error-rate alerts)
    "gateway.auth_failed": (GatewayAuthFailed, Sensitivity.PSEUDONYMOUS),
    # delegate (Phase 4 v1 MVP — delegation.md §9)
    "delegate.started": (DelegateStarted, Sensitivity.PSEUDONYMOUS),
    "delegate.completed": (DelegateCompleted, Sensitivity.PSEUDONYMOUS),
    "delegate.failed": (DelegateFailed, Sensitivity.PSEUDONYMOUS),
    # gateway admin (Wave 10 — gateway.md §11 key lifecycle)
    "gateway.key_issued": (GatewayKeyIssued, Sensitivity.PSEUDONYMOUS),
    "gateway.key_revoked": (GatewayKeyRevoked, Sensitivity.PSEUDONYMOUS),
    "gateway.key_rotated": (GatewayKeyRotated, Sensitivity.PSEUDONYMOUS),
    # GDPR / portability audit (analytics-api.md §4.10, multi-user.md §7.4.4)
    "analytics.user_exported": (AnalyticsUserExported, Sensitivity.PSEUDONYMOUS),
    "analytics.user_forgotten": (AnalyticsUserForgotten, Sensitivity.PSEUDONYMOUS),
    # billing (Wave 15 — pricing.md §5.5.4)
    "billing.customer_created": (BillingCustomerCreated, Sensitivity.PSEUDONYMOUS),
    "billing.subscription_created": (BillingSubscriptionCreated, Sensitivity.PSEUDONYMOUS),
    "billing.subscription_updated": (BillingSubscriptionUpdated, Sensitivity.PSEUDONYMOUS),
    "billing.subscription_canceled": (BillingSubscriptionCanceled, Sensitivity.PSEUDONYMOUS),
    "billing.invoice_paid": (BillingInvoicePaid, Sensitivity.PSEUDONYMOUS),
    "billing.invoice_payment_failed": (BillingInvoicePaymentFailed, Sensitivity.PSEUDONYMOUS),
    # bus
    "bus.subscriber_registered": (BusSubscriberRegistered, Sensitivity.PSEUDONYMOUS),
    "bus.subscriber_unregistered": (BusSubscriberUnregistered, Sensitivity.PSEUDONYMOUS),
    "bus.gap_detected": (BusGapDetected, Sensitivity.PSEUDONYMOUS),
    # trace retention (Wave 12 — trace-retention.md). 12a-1 will fold an
    # `audit: bool` flag in here as the third tuple element; until then,
    # `metis_core.trace.retention.is_audit_event` carries `trace.swept`
    # in its fallback allowlist so sweep history is preserved by design.
    "trace.swept": (TraceSwept, Sensitivity.PSEUDONYMOUS),
}


def payload_for_type(event_type: str) -> type[msgspec.Struct]:
    """Look up the payload class for a registered event type."""
    if event_type not in PAYLOAD_REGISTRY:
        raise UnknownEventTypeError(event_type)
    return PAYLOAD_REGISTRY[event_type][0]


# --- Audit subset (audit-log.md §4, §5.1) -----------------------------------
#
# An audit event is a trace event flagged as security/compliance-relevant.
# The retention sweep (12a-2) MUST NOT delete events whose type is in this
# set; the audit log surfaces them as a deterministic export for SIEM ingest.
# See `audit-log.md §4` for the per-type rationale. Adding or removing a type
# is a deliberate spec change with a CHANGES.md entry.
AUDIT_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "gateway.key_issued",
        "gateway.key_revoked",
        "gateway.key_rotated",
        "gateway.quota_exceeded",
        # Wave 14a — observability.md §3. Audit-flagged so brute-force /
        # credential-stuffing attempts are preserved past the retention
        # window for incident-response forensics.
        "gateway.auth_failed",
        "quota.alert",
        "routing.policy_invalid",
        "memory.eviction",
        "pattern.evicted",
        "tool.confirmation_resolved",
        # Wave 12a-2 — trace-retention.md §6. Preserves sweep history so a
        # later sweep with a cutoff past the first sweep's timestamp does
        # not delete the audit trail of the prune mechanism itself.
        "trace.swept",
        # GDPR portability + forget audit (analytics-api.md §4.10,
        # multi-user.md §7.4.4). Both events are explicitly documented as
        # audit-trail records for subject-rights operations.
        "analytics.user_exported",
        "analytics.user_forgotten",
        # Wave 15 — pricing.md §5.5.4. Billing lifecycle is SOC2/finance-
        # audit territory; the retention sweep must preserve every record
        # of customer / subscription / invoice state for the lifetime of
        # the trace DB, not just the 90-day window.
        "billing.customer_created",
        "billing.subscription_created",
        "billing.subscription_updated",
        "billing.subscription_canceled",
        "billing.invoice_paid",
        "billing.invoice_payment_failed",
    }
)


def is_audit_event(event_type: str) -> bool:
    """Return True if the event type is in the audit subset (audit-log.md §3)."""
    return event_type in AUDIT_EVENT_TYPES


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
