"""Routing engine: the policy chain.

See routing-engine.md §4. For Phase 1, this implements:

    1. PER_MESSAGE_OVERRIDE   ← from TurnContext.per_message_override
    2. MANUAL_STICKY          ← from TurnContext.session_active_model
    3. CONFIGURED_RULES       ← stub (always not_applicable)
    4. PATTERN_RECOMMENDATION ← stub
    5. DELEGATE_REQUEST       ← stub
    6. WORKSPACE_DEFAULT      ← from TurnContext.workspace_default_model
    7. GLOBAL_DEFAULT         ← from TurnContext.global_default_model

Validation (§4.4) rejects unconfigured / unavailable / capability-mismatched
candidates and falls through. Hard failure (§4.7) raises RoutingError so the
session manager can surface "no model available" to the user.

Every decision — including hard failure — emits exactly one `route.decided`
event (§7.2).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime

from metis.events.bus import EventBus
from metis.events.envelope import Actor
from metis.events.payloads import (
    PolicyEvaluation,
    RouteDecided,
    make_event,
)
from metis.routing.availability import ProviderAvailability
from metis.routing.context import RoutingDecision, TurnContext
from metis.routing.registry import ModelRegistry


@dataclass
class _Candidate:
    policy: str  # matches RoutingPolicyName literal in events.payloads
    model: str | None
    rule_name: str | None = None
    reason_when_applicable: str = ""
    reason_when_not_applicable: str = ""


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
    ) -> None:
        self._registry = registry
        self._bus = bus
        self._availability = availability or ProviderAvailability()

    @property
    def availability(self) -> ProviderAvailability:
        return self._availability

    # ---- Decide --------------------------------------------------------

    def decide(self, ctx: TurnContext) -> RoutingDecision:
        start = time.monotonic()
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
        return decision

    # ---- Chain construction --------------------------------------------

    def _build_chain(self, ctx: TurnContext) -> list[_Candidate]:
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
            _Candidate(
                policy="rule",
                model=None,
                reason_when_not_applicable="configured rules not enabled (Phase 2)",
            ),
            _Candidate(
                policy="pattern",
                model=None,
                reason_when_not_applicable="pattern store not enabled (Phase 2.5)",
            ),
            _Candidate(
                policy="delegate_request",
                model=None,
                reason_when_not_applicable="not a delegation re-entry",
            ),
            _Candidate(
                policy="workspace_default",
                model=ctx.workspace_default_model,
                reason_when_applicable="workspace default",
                reason_when_not_applicable="no workspace default configured",
            ),
            _Candidate(
                policy="global_default",
                model=ctx.global_default_model,
                reason_when_applicable="global default fallback",
                reason_when_not_applicable="no global default configured",
            ),
        ]

    # ---- Validation ----------------------------------------------------

    def _validate(self, model: str, ctx: TurnContext) -> str | None:
        """Return the `validation_failure` enum value, or None on success."""
        if not self._registry.is_configured(model):
            return "not_configured"
        provider = self._registry.provider_of(model)
        if not self._availability.is_available(provider):
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
