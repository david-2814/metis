"""Routing engine: the policy chain.

See routing-engine.md §4. Implements:

    1. PER_MESSAGE_OVERRIDE   ← from TurnContext.per_message_override
    2. MANUAL_STICKY          ← from TurnContext.session_active_model
    3. CONFIGURED_RULES       ← first matching rule in RoutingPolicy
    4. PATTERN_RECOMMENDATION ← per-workspace PatternStore (Phase 2.5)
    5. DELEGATE_REQUEST       ← stub (Phase 4)
    6. WORKSPACE_DEFAULT      ← from RoutingPolicy.workspaces[].default or ctx.workspace_default_model
    7. GLOBAL_DEFAULT         ← from RoutingPolicy.global_default or ctx.global_default_model

Validation (§4.4) rejects unconfigured / unavailable / capability-mismatched
candidates and falls through. Hard failure (§4.7) raises RoutingError so the
session manager can surface "no model available" to the user.

Every decision — including hard failure — emits exactly one `route.decided`
event (§7.2). When slot 4 wins, an additional `pattern.matched` event fires
per `pattern-store.md §10.2`.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from metis_core.events.bus import EventBus
from metis_core.events.envelope import Actor
from metis_core.events.payloads import (
    PatternAlternative,
    PatternMatched,
    PolicyEvaluation,
    RouteDecided,
    make_event,
)
from metis_core.patterns.fingerprint import (
    FingerprintInputs,
    compute_fingerprint,
)
from metis_core.patterns.store import PatternRecommendation, PatternStore
from metis_core.routing.availability import ProviderAvailability
from metis_core.routing.context import RoutingDecision, TurnContext
from metis_core.routing.policy import EMPTY_POLICY, PatternConfig, RoutingPolicy
from metis_core.routing.predicates import evaluate as evaluate_predicate
from metis_core.routing.registry import ModelRegistry

PatternStoreResolver = Callable[[str], PatternStore | None]
"""Maps an absolute `workspace_path` to its PatternStore (or None when no
store is available for this workspace)."""

FingerprintInputsBuilder = Callable[[TurnContext], FingerprintInputs]
"""Builds `FingerprintInputs` from a TurnContext. Lets the engine call into
the pattern store without depending on session-state extraction details."""


@dataclass
class _Candidate:
    policy: str  # matches RoutingPolicyName literal in events.payloads
    model: str | None
    rule_name: str | None = None
    reason_when_applicable: str = ""
    reason_when_not_applicable: str = ""
    confidence: float | None = None
    pattern_alternatives: list[PatternAlternative] | None = None


class RoutingError(Exception):
    """No model was available for this turn (routing chain exhausted).

    Carries the chain trace so the session manager can surface the failure
    to the user (per server-api.md §4.2 `routing_failed` response shape).
    """

    def __init__(self, chain: list[PolicyEvaluation]) -> None:
        attempts = [
            f"{e.candidate_model} ({e.validation_failure or 'not configured'})"
            for e in chain
            if e.candidate_model
        ]
        super().__init__(f"no model available; tried: {', '.join(attempts) or 'nothing'}")
        self.chain = chain


class RoutingEngine:
    """Phase 1 routing engine: manual override + sticky + default with full
    capability validation, provider availability tracking, and one
    `route.decided` event per turn."""

    def __init__(
        self,
        *,
        registry: ModelRegistry,
        bus: EventBus,
        availability: ProviderAvailability | None = None,
        policy: RoutingPolicy | None = None,
        pattern_store_resolver: PatternStoreResolver | None = None,
        fingerprint_inputs_builder: FingerprintInputsBuilder | None = None,
    ) -> None:
        self._registry = registry
        self._bus = bus
        self._availability = availability or ProviderAvailability()
        self._policy = policy or EMPTY_POLICY
        self._pattern_store_resolver = pattern_store_resolver
        self._fingerprint_inputs_builder = fingerprint_inputs_builder
        # Memoized per-turn so we can attach the chosen recommendation to
        # the route.decided event and emit `pattern.matched` after the win.
        self._last_pattern_recommendation: PatternRecommendation | None = None

    @property
    def availability(self) -> ProviderAvailability:
        return self._availability

    @property
    def policy(self) -> RoutingPolicy:
        return self._policy

    def set_policy(self, policy: RoutingPolicy) -> None:
        """Swap the active policy. Used by hot-reload (not wired in v1)."""
        self._policy = policy

    # ---- Decide --------------------------------------------------------

    def decide(self, ctx: TurnContext) -> RoutingDecision:
        start = time.monotonic()
        self._last_pattern_recommendation = None
        candidates = self._build_chain(ctx)
        chain: list[PolicyEvaluation] = []
        winner_index = -1
        chosen_model = ""

        for index, candidate in enumerate(candidates):
            if candidate.model is None:
                chain.append(
                    PolicyEvaluation(
                        policy=candidate.policy,  # type: ignore[arg-type]
                        verdict="not_applicable",
                        reason=candidate.reason_when_not_applicable,
                        rule_name=candidate.rule_name,
                        confidence=candidate.confidence,
                        pattern_alternatives=candidate.pattern_alternatives,
                    )
                )
                continue
            failure = self._validate(candidate.model, ctx)
            if failure is None:
                chain.append(
                    PolicyEvaluation(
                        policy=candidate.policy,  # type: ignore[arg-type]
                        verdict="chose",
                        candidate_model=candidate.model,
                        reason=candidate.reason_when_applicable,
                        rule_name=candidate.rule_name,
                        confidence=candidate.confidence,
                        pattern_alternatives=candidate.pattern_alternatives,
                    )
                )
                winner_index = index
                chosen_model = candidate.model
                break
            chain.append(
                PolicyEvaluation(
                    policy=candidate.policy,  # type: ignore[arg-type]
                    verdict="rejected",
                    candidate_model=candidate.model,
                    reason=candidate.reason_when_applicable,
                    rule_name=candidate.rule_name,
                    confidence=candidate.confidence,
                    pattern_alternatives=candidate.pattern_alternatives,
                    validation_failure=failure,  # type: ignore[arg-type]
                )
            )

        elapsed_ms = (time.monotonic() - start) * 1000
        decision = RoutingDecision(
            chosen_model=chosen_model,
            winner_index=winner_index,
            elapsed_ms=elapsed_ms,
            chain=chain,
        )
        self._emit_route_decided(ctx, decision)
        if winner_index == -1:
            raise RoutingError(chain)
        # Emit pattern.matched only when slot 4 (pattern) actually won.
        if (
            winner_index < len(candidates)
            and candidates[winner_index].policy == "pattern"
            and self._last_pattern_recommendation is not None
        ):
            self._emit_pattern_matched(ctx, self._last_pattern_recommendation)
        return decision

    # ---- Chain construction --------------------------------------------

    def _build_chain(self, ctx: TurnContext) -> list[_Candidate]:
        rule_candidate = self._evaluate_rules(ctx)
        # Workspace and global defaults come from the policy when set; the
        # TurnContext fields are the legacy fallback (used when no
        # routing.yaml is loaded).
        workspace_scope = (
            self._policy.workspace_for(ctx.workspace_path) if ctx.workspace_path else None
        )
        workspace_default = (
            workspace_scope.default
            if workspace_scope and workspace_scope.default is not None
            else ctx.workspace_default_model
        )
        global_default = self._policy.global_default or ctx.global_default_model
        return [
            _Candidate(
                policy="per_message_override",
                model=ctx.per_message_override,
                reason_when_applicable="per-message @model override",
                reason_when_not_applicable="no @model in message",
            ),
            _Candidate(
                policy="manual_sticky",
                model=ctx.session_active_model,
                reason_when_applicable="session sticky model (/model)",
                reason_when_not_applicable="no sticky model set",
            ),
            rule_candidate,
            self._evaluate_pattern(ctx, workspace_scope),
            _Candidate(
                policy="delegate_request",
                model=None,
                reason_when_not_applicable="not a delegation re-entry",
            ),
            _Candidate(
                policy="workspace_default",
                model=workspace_default,
                reason_when_applicable="workspace default",
                reason_when_not_applicable="no workspace default configured",
            ),
            _Candidate(
                policy="global_default",
                model=global_default,
                reason_when_applicable="global default fallback",
                reason_when_not_applicable="no global default configured",
            ),
        ]

    def _evaluate_pattern(
        self,
        ctx: TurnContext,
        workspace_scope,
    ) -> _Candidate:
        """Slot 4. Returns a candidate with the pattern recommendation.

        Falls back to `not_applicable` when:
        - No `pattern_store_resolver` was injected (Phase 2.5 not wired).
        - The workspace has no PatternStore.
        - No `fingerprint_inputs_builder` is available.
        - `recommend()` returns `chosen_model=None`.
        """
        if self._pattern_store_resolver is None:
            return _Candidate(
                policy="pattern",
                model=None,
                reason_when_not_applicable="pattern store not configured",
            )
        if not ctx.workspace_path:
            return _Candidate(
                policy="pattern",
                model=None,
                reason_when_not_applicable="no workspace path on turn context",
            )
        if self._fingerprint_inputs_builder is None:
            return _Candidate(
                policy="pattern",
                model=None,
                reason_when_not_applicable="no fingerprint inputs builder",
            )
        try:
            store = self._pattern_store_resolver(ctx.workspace_path)
        except Exception:  # pragma: no cover - defensive
            return _Candidate(
                policy="pattern",
                model=None,
                reason_when_not_applicable="pattern store resolver error",
            )
        if store is None:
            return _Candidate(
                policy="pattern",
                model=None,
                reason_when_not_applicable="no pattern store for workspace",
            )

        config = self._resolve_pattern_config(workspace_scope)
        inputs = self._fingerprint_inputs_builder(ctx)
        if config.fingerprint_version == "v2" and config.embedding_provider is not None:
            inputs = self._attach_cached_embedding(inputs, store, config.embedding_provider)
        fingerprint = compute_fingerprint(inputs)
        try:
            recommendation = store.recommend(
                fingerprint,
                cost_weight=config.cost_weight,
                min_confidence=config.min_confidence,
                min_sample_size=config.min_sample_size,
                k=10,
            )
        except Exception:  # pragma: no cover - read failure isolated
            return _Candidate(
                policy="pattern",
                model=None,
                reason_when_not_applicable="pattern store unavailable",
            )

        self._last_pattern_recommendation = recommendation
        alternatives = [
            PatternAlternative(
                model=alt.model,
                score=alt.score,
                sample_size=alt.sample_size,
            )
            for alt in recommendation.alternatives
        ] or None

        if recommendation.chosen_model is None:
            return _Candidate(
                policy="pattern",
                model=None,
                reason_when_not_applicable=(
                    "no high-confidence pattern recommendation"
                    if recommendation.alternatives
                    else "no neighbors in pattern store"
                ),
                confidence=recommendation.confidence,
                pattern_alternatives=alternatives,
            )

        return _Candidate(
            policy="pattern",
            model=recommendation.chosen_model,
            reason_when_applicable=(
                f"pattern K-NN (confidence={recommendation.confidence:.2f}, "
                f"sample={recommendation.sample_size})"
            ),
            confidence=recommendation.confidence,
            pattern_alternatives=alternatives,
        )

    def _resolve_pattern_config(self, workspace_scope) -> PatternConfig:
        if workspace_scope is not None and workspace_scope.pattern is not None:
            return workspace_scope.pattern
        return self._policy.pattern

    def _attach_cached_embedding(
        self,
        inputs: FingerprintInputs,
        store: PatternStore,
        provider_id: str,
    ) -> FingerprintInputs:
        """Cache-only lookup for the query fingerprint's embedding.

        On hit the inputs are returned with `embedding` set; on miss they
        are returned unchanged so the K-NN falls back to v1 jaccard
        without blocking on a network call (pattern-store.md §16.6).
        """
        try:
            vector = store.lookup_embedding(inputs.user_message_text, provider_id)
        except Exception:  # pragma: no cover - cache read failure isolated
            return inputs
        if vector is None:
            return inputs
        return replace(inputs, embedding=vector, embedding_provider=provider_id)

    def _evaluate_rules(self, ctx: TurnContext) -> _Candidate:
        """Return a rule-slot candidate. Workspace rules evaluate before
        global rules; first match wins (routing-engine.md §5.2).
        """
        workspace_scope = (
            self._policy.workspace_for(ctx.workspace_path) if ctx.workspace_path else None
        )
        candidates = []
        if workspace_scope is not None:
            candidates.extend(workspace_scope.rules)
        candidates.extend(self._policy.rules)
        for rule in candidates:
            if evaluate_predicate(rule.when, ctx):
                return _Candidate(
                    policy="rule",
                    model=rule.use,
                    rule_name=rule.name,
                    reason_when_applicable=f"matched rule {rule.name!r}",
                )
        if not candidates:
            return _Candidate(
                policy="rule",
                model=None,
                reason_when_not_applicable="no rules configured",
            )
        return _Candidate(
            policy="rule",
            model=None,
            reason_when_not_applicable="no rule matched this turn",
        )

    # ---- Validation ----------------------------------------------------

    def _validate(self, model: str, ctx: TurnContext) -> str | None:
        """Return the `validation_failure` enum value, or None on success."""
        if not self._registry.is_configured(model):
            return "not_configured"
        provider = self._registry.provider_of(model)
        if not self._availability.is_available(provider, model):
            return "provider_unavailable"
        caps = self._registry.capabilities_for(model)
        if ctx.has_images and not caps.supports_images:
            return "no_vision_support"
        if ctx.estimated_input_tokens > caps.max_context_tokens:
            return "exceeds_context_window"
        if ctx.has_tool_definitions and not caps.supports_tools:
            return "no_tool_support"
        if ctx.has_system_prompt and not caps.supports_system_prompt:
            return "no_system_prompt_support"
        if ctx.requires_structured_output and not caps.supports_structured_output:
            return "no_structured_output_support"
        return None

    # ---- Event emission ------------------------------------------------

    def _emit_route_decided(self, ctx: TurnContext, decision: RoutingDecision) -> None:
        self._bus.emit(
            make_event(
                type="route.decided",
                session_id=ctx.session_id,
                turn_id=ctx.turn_id,
                actor=Actor.SYSTEM,
                payload=RouteDecided(
                    chosen_model=decision.chosen_model,
                    winner_index=decision.winner_index,
                    elapsed_ms=decision.elapsed_ms,
                    chain=decision.chain,
                ),
                timestamp=datetime.now(UTC),
                parent_event_id=ctx.parent_event_id,
            )
        )

    def _emit_pattern_matched(
        self, ctx: TurnContext, recommendation: PatternRecommendation
    ) -> None:
        if recommendation.chosen_model is None:
            return
        self._bus.emit(
            make_event(
                type="pattern.matched",
                session_id=ctx.session_id,
                turn_id=ctx.turn_id,
                actor=Actor.SYSTEM,
                payload=PatternMatched(
                    fingerprint_id=recommendation.fingerprint_id or "",
                    fingerprint_kind=recommendation.fingerprint_kind.value,
                    chosen_model=recommendation.chosen_model,
                    confidence=recommendation.confidence,
                    sample_size=recommendation.sample_size,
                    k_cluster_size=recommendation.k_cluster_size,
                    alternatives_count=len(recommendation.alternatives),
                ),
                timestamp=datetime.now(UTC),
                parent_event_id=ctx.parent_event_id,
            )
        )
