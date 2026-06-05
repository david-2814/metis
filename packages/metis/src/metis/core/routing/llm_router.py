"""LLM_ROUTER slot — auxiliary-LLM model picker (routing-engine.md §4.6).

The engine is synchronous; LLM calls are not. The session manager (or
gateway harness) awaits `LLMRouter.decide()` BEFORE invoking
`RoutingEngine.decide(ctx)` and stuffs the outcome onto
`TurnContext.llm_router_result`. The engine then reads it like it reads
`worker_tier_model` — pure data lookup, no I/O.

Every failure mode collapses to `LLMRouterResult(chosen_model=None,
failure_reason=<reason>)` so the chain can fall through cleanly. The
router never raises into the engine.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from metis.core.adapters.errors import CancelledError as AdapterCancelledError
from metis.core.adapters.protocol import CanonicalRequest
from metis.core.canonical.capabilities import AdapterCapabilities
from metis.core.canonical.content import TextBlock, ToolUseBlock
from metis.core.canonical.ids import new_message_id, next_monotonic_ulid
from metis.core.canonical.messages import Message, MessageMetadata, Role
from metis.core.canonical.tools import SideEffects, ToolDefinition
from metis.core.eval.budget import BudgetTracker
from metis.core.events.bus import EventBus
from metis.core.events.envelope import Actor
from metis.core.events.payloads import LLMCallCompleted, LLMCallStarted, make_event
from metis.core.pricing.table import PriceTable, UnknownPricingModelError
from metis.core.routing.availability import AvailabilityState, ProviderAvailability
from metis.core.routing.policy import LLMRouterConfig
from metis.core.routing.registry import ModelRegistry, UnknownModelError

logger = logging.getLogger(__name__)


_CHOOSE_MODEL_TOOL = "choose_model"

# Rough output-token budget for the meta-call. The router replies with a
# single tool call carrying a model id + a short reason. ~150 tokens is
# generous; it caps cost while leaving room for thinking-prefix providers.
_MAX_OUTPUT_TOKENS = 150

# How much assistant-message text to persist on the
# `llm.call_completed.response_text_preview` field. Truncated upstream
# so a router that emitted prose instead of a tool call is debuggable
# from the trace store without dragging the full output blob into
# every event (routing-engine.md §4.6.7).
_RESPONSE_PREVIEW_MAX_CHARS = 500


@dataclass(frozen=True)
class LLMRouterResult:
    """One LLM_ROUTER meta-call outcome.

    Exactly one of `chosen_model` or `failure_reason` is set:
    - `chosen_model is not None` → router picked successfully; engine's
      slot 5 emits `verdict="chose"` with this candidate (still subject
      to §4.4 validation in the engine).
    - `failure_reason is not None` → router failed; engine's slot 5 emits
      `verdict="not_applicable"` with `reason=failure_reason`.

    `meta_cost_usd` and the token counts are surfaced on the slot's
    PolicyEvaluation regardless of outcome (so failed attempts that still
    spent tokens are accountable). They are zero when the slot short-
    circuited before the LLM call (budget_exhausted, no_candidates,
    delegate_request_in_flight, llm_router disabled).
    """

    chosen_model: str | None
    failure_reason: str | None
    meta_cost_usd: Decimal = Decimal("0")
    meta_tokens_input: int = 0
    meta_tokens_output: int = 0
    reason_text: str | None = None  # router's natural-language rationale, when available


def disabled() -> LLMRouterResult:
    """Sentinel: `llm_router.enabled is False` in the active policy."""
    return LLMRouterResult(chosen_model=None, failure_reason="llm_router disabled")


def deferred_for_delegate() -> LLMRouterResult:
    """Sentinel: worker re-entry; planner already chose the tier (§4.6.4)."""
    return LLMRouterResult(chosen_model=None, failure_reason="delegate_request_in_flight")


class LLMRouter:
    """Async meta-call that picks a model from the registered candidates."""

    def __init__(
        self,
        *,
        config: LLMRouterConfig,
        registry: ModelRegistry,
        availability: ProviderAvailability,
        price_table: PriceTable,
        budget_tracker: BudgetTracker,
        bus: EventBus | None = None,
    ) -> None:
        self._config = config
        self._registry = registry
        self._availability = availability
        self._price_table = price_table
        self._budget = budget_tracker
        # Optional event bus for `llm.call_started` / `llm.call_completed`
        # emission stamped `Actor.ROUTER` (routing-engine.md §4.6.7).
        # `None` keeps the in-test substrate working with no event-store
        # plumbing; production runtime always passes the shared bus.
        self._bus = bus

    @property
    def config(self) -> LLMRouterConfig:
        return self._config

    @property
    def budget(self) -> BudgetTracker:
        return self._budget

    async def decide(
        self,
        *,
        user_prompt: str,
        session_id: str,
        ctx_requirements: _CtxRequirements | None = None,
        now: datetime | None = None,
    ) -> LLMRouterResult:
        """Run one meta-call. Returns a fully-populated `LLMRouterResult`.

        `ctx_requirements` filters the candidate set per §4.4 — only
        models whose `AdapterCapabilities` cover the turn's needs (vision,
        tools, etc.) are presented to the router. When `None`, no
        capability filter is applied (all configured + healthy models are
        candidates).
        """
        candidates = self._candidate_models(ctx_requirements)
        if not candidates:
            return LLMRouterResult(chosen_model=None, failure_reason="no_candidates")

        # Self-call guard: the router model itself must not be in the
        # candidate enum, otherwise the LLM could route every turn to
        # itself by default and quietly inflate router budget. Match by
        # both alias-resolved canonical id and the raw configured string
        # so a `router.model: haiku` config (alias) doesn't leak in via
        # the canonical `anthropic:claude-haiku-4-5`.
        router_canonical = self._registry.resolve_alias(self._config.model)
        candidates = [c for c in candidates if c != router_canonical and c != self._config.model]
        if not candidates:
            return LLMRouterResult(chosen_model=None, failure_reason="no_candidates")

        # Budget check uses a coarse projection. The actual cost is recorded
        # post-call regardless; this gate just keeps a runaway router from
        # blowing past the cap mid-burst.
        projected = self._projected_cost(user_prompt, candidates)
        throttle = self._budget.throttle_reason(
            session_id=session_id,
            projected_cost_usd=projected,
            now=now,
        )
        if throttle is not None:
            return LLMRouterResult(chosen_model=None, failure_reason="budget_exhausted")

        # Resolve the router model itself.
        if router_canonical is None or router_canonical not in self._registry:
            return LLMRouterResult(
                chosen_model=None,
                failure_reason=f"invalid_router_model: {self._config.model}",
            )
        try:
            router_entry = self._registry.get(router_canonical)
        except UnknownModelError:
            return LLMRouterResult(
                chosen_model=None,
                failure_reason=f"invalid_router_model: {self._config.model}",
            )

        # The router itself must be healthy.
        if (
            self._availability.state(self._registry.provider_of(router_canonical), router_canonical)
            != AvailabilityState.HEALTHY
        ):
            return LLMRouterResult(
                chosen_model=None,
                failure_reason="router_model_unavailable",
            )

        system_prompt = _build_system_prompt(self._registry, candidates, self._price_table)
        tool = _build_choose_model_tool(candidates)
        request_id = str(next_monotonic_ulid())
        # Frame the user prompt as data the planner will receive, not as a
        # prompt directed at the router. Without this wrapper haiku-as-router
        # answered "what's today's date" directly on 2026-06-05 instead of
        # calling choose_model. The system prompt's "TASK FOR PLANNER"
        # marker bookends this so the LLM treats the text as routable
        # subject matter, not as a question to itself.
        framed_prompt = f"--- TASK FOR PLANNER ---\n{user_prompt or ''}\n--- END TASK ---"
        request = CanonicalRequest(
            request_id=request_id,
            messages=[
                Message(
                    id=new_message_id(),
                    session_id=session_id,
                    role=Role.USER,
                    content=[TextBlock(text=framed_prompt)],
                    created_at=datetime.now(UTC),
                    metadata=MessageMetadata(),
                )
            ],
            tools=[tool],
            system_prompt=system_prompt,
            model=router_canonical,
            max_output_tokens=_MAX_OUTPUT_TOKENS,
            temperature=0.0,  # deterministic per-prompt picks
        )

        # Emit `llm.call_started` stamped Actor.ROUTER so analytics can
        # attribute meta-call spend separately from planner / worker
        # traffic (routing-engine.md §4.6.7).
        provider = self._registry.provider_of(router_canonical)
        started_at_ms = _monotonic_ms()
        self._emit_llm_call_started(
            session_id=session_id,
            request_id=request_id,
            model=router_canonical,
            provider=provider,
            estimated_input_tokens=router_entry.adapter.estimate_input_tokens(
                request.messages, request.tools, request.system_prompt
            ),
        )

        try:
            response = await asyncio.wait_for(
                router_entry.adapter.complete(request),
                timeout=self._config.timeout_seconds,
            )
        except TimeoutError:
            return LLMRouterResult(chosen_model=None, failure_reason="timeout")
        except AdapterCancelledError:
            # Adapters wrap `asyncio.CancelledError` as their own typed
            # error (provider-adapter-contract §6.1). When *our* wait_for
            # fires its timeout, that cancellation propagates INTO the
            # adapter and re-emerges as `AdapterCancelledError` — the
            # adapter ate the cancellation signal, so wait_for never
            # converts it back to TimeoutError. Classify it as timeout
            # here for accurate per-failure-mode telemetry instead of the
            # misleading "network_error: CancelledError" surfaced in v3.4
            # first-cut field testing (2026-06-04).
            return LLMRouterResult(chosen_model=None, failure_reason="timeout")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "LLM_ROUTER meta-call failed for session %s: %s",
                session_id,
                exc,
            )
            return LLMRouterResult(
                chosen_model=None,
                failure_reason=f"network_error: {type(exc).__name__}",
            )

        # Record actual cost regardless of parse outcome.
        try:
            meta_cost = self._price_table.compute_cost(router_canonical, response.usage)
        except UnknownPricingModelError:
            meta_cost = Decimal("0")
        self._budget.record(session_id=session_id, cost_usd=meta_cost, now=now)

        tokens_in = response.usage.input_tokens + response.usage.cached_input_tokens
        tokens_out = response.usage.output_tokens

        # Parse the tool call. The router MUST emit exactly one
        # `choose_model` call; anything else collapses to a documented
        # failure mode.
        tool_call = _find_choose_model_call(response.content)
        text_preview = _extract_text_preview(response.content)

        # Emit `llm.call_completed` with the truncated response preview
        # so a `no_model_chosen` failure is debuggable post-hoc from the
        # trace store (the previous implementation discarded the
        # response content entirely).
        self._emit_llm_call_completed(
            session_id=session_id,
            model=router_canonical,
            provider=provider,
            response_usage=response.usage,
            cost_usd=meta_cost,
            stop_reason=str(response.stop_reason),
            latency_ms=_monotonic_ms() - started_at_ms,
            produced_tool_calls=1 if tool_call is not None else 0,
            response_text_preview=text_preview,
        )

        if tool_call is None:
            return LLMRouterResult(
                chosen_model=None,
                failure_reason="no_model_chosen",
                meta_cost_usd=meta_cost,
                meta_tokens_input=tokens_in,
                meta_tokens_output=tokens_out,
            )

        raw_model_id = tool_call.input.get("model_id")
        reason_text = tool_call.input.get("reason")
        if not isinstance(raw_model_id, str) or not raw_model_id:
            return LLMRouterResult(
                chosen_model=None,
                failure_reason="no_model_chosen",
                meta_cost_usd=meta_cost,
                meta_tokens_input=tokens_in,
                meta_tokens_output=tokens_out,
            )
        if raw_model_id not in candidates:
            return LLMRouterResult(
                chosen_model=None,
                failure_reason=f"invalid_model_id: {raw_model_id}",
                meta_cost_usd=meta_cost,
                meta_tokens_input=tokens_in,
                meta_tokens_output=tokens_out,
            )

        return LLMRouterResult(
            chosen_model=raw_model_id,
            failure_reason=None,
            meta_cost_usd=meta_cost,
            meta_tokens_input=tokens_in,
            meta_tokens_output=tokens_out,
            reason_text=reason_text if isinstance(reason_text, str) else None,
        )

    # ------------------------------------------------------------------
    # Event emission helpers (routing-engine.md §4.6.7)
    # ------------------------------------------------------------------

    def _emit_llm_call_started(
        self,
        *,
        session_id: str,
        request_id: str,
        model: str,
        provider: str,
        estimated_input_tokens: int,
    ) -> None:
        if self._bus is None:
            return
        try:
            self._bus.emit(
                make_event(
                    type="llm.call_started",
                    payload=LLMCallStarted(
                        model=model,
                        provider=provider,
                        estimated_input_tokens=estimated_input_tokens,
                        request_id=request_id,
                        is_worker=False,
                    ),
                    session_id=session_id,
                    actor=Actor.ROUTER,
                    timestamp=datetime.now(UTC),
                )
            )
        except Exception:
            logger.exception("LLM_ROUTER failed to emit llm.call_started")

    def _emit_llm_call_completed(
        self,
        *,
        session_id: str,
        model: str,
        provider: str,
        response_usage,
        cost_usd: Decimal,
        stop_reason: str,
        latency_ms: int,
        produced_tool_calls: int,
        response_text_preview: str | None,
    ) -> None:
        if self._bus is None:
            return
        try:
            self._bus.emit(
                make_event(
                    type="llm.call_completed",
                    payload=LLMCallCompleted(
                        model=model,
                        provider=provider,
                        input_tokens=response_usage.input_tokens,
                        output_tokens=response_usage.output_tokens,
                        cached_input_tokens=response_usage.cached_input_tokens,
                        cache_creation_input_tokens=response_usage.cache_creation_input_tokens,
                        cost_usd=float(cost_usd),
                        pricing_version=self._price_table.version,
                        latency_ms=latency_ms,
                        stop_reason=_normalize_stop_reason(stop_reason),
                        produced_tool_calls=produced_tool_calls,
                        produced_thinking_blocks=0,
                        response_text_preview=response_text_preview,
                    ),
                    session_id=session_id,
                    actor=Actor.ROUTER,
                    timestamp=datetime.now(UTC),
                )
            )
        except Exception:
            logger.exception("LLM_ROUTER failed to emit llm.call_completed")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _candidate_models(self, requirements: _CtxRequirements | None) -> list[str]:
        """Registered, healthy, capability-valid model ids (sorted)."""
        out: list[str] = []
        for model_id in self._registry.list_models():
            entry = self._registry.get(model_id)
            if (
                self._availability.state(self._registry.provider_of(model_id), model_id)
                != AvailabilityState.HEALTHY
            ):
                continue
            if requirements is not None and not _capabilities_satisfy(
                entry.capabilities, requirements
            ):
                continue
            out.append(model_id)
        return out

    def _projected_cost(self, user_prompt: str, candidates: list[str]) -> Decimal:
        """Cheap upper-bound estimate for the budget gate.

        We don't have a tokenizer plumbed in here; approximate input as
        ~one token per 4 characters of (system_prompt + user_prompt), and
        cap output at `_MAX_OUTPUT_TOKENS`. This is intentionally a
        floor-of-an-estimate — the actual recorded cost (used for
        accounting) is authoritative.
        """
        approx_system_chars = 600 + 80 * len(candidates)
        approx_user_chars = len(user_prompt or "")
        approx_input_tokens = (approx_system_chars + approx_user_chars) // 4 + 1
        router_canonical = self._registry.resolve_alias(self._config.model)
        if router_canonical is None:
            return Decimal("0")
        try:
            pricing = self._price_table.pricing_for(router_canonical)
        except UnknownPricingModelError:
            return Decimal("0")
        million = Decimal("1000000")
        return (
            (Decimal(approx_input_tokens) * pricing.input_per_mtok)
            + (Decimal(_MAX_OUTPUT_TOKENS) * pricing.output_per_mtok)
        ) / million


@dataclass(frozen=True)
class _CtxRequirements:
    """Subset of `TurnContext` the router uses to filter candidates.

    Mirrors the §4.4 capability gate: we exclude any model that would be
    `rejected` by the engine anyway, so the router cannot pick something
    that fails validation downstream.
    """

    estimated_input_tokens: int
    has_images: bool
    has_tool_definitions: bool
    has_system_prompt: bool
    requires_structured_output: bool


def requirements_from_ctx(
    *,
    estimated_input_tokens: int,
    has_images: bool,
    has_tool_definitions: bool,
    has_system_prompt: bool,
    requires_structured_output: bool,
) -> _CtxRequirements:
    return _CtxRequirements(
        estimated_input_tokens=estimated_input_tokens,
        has_images=has_images,
        has_tool_definitions=has_tool_definitions,
        has_system_prompt=has_system_prompt,
        requires_structured_output=requires_structured_output,
    )


def _capabilities_satisfy(caps: AdapterCapabilities, req: _CtxRequirements) -> bool:
    if req.has_images and not caps.supports_images:
        return False
    if req.has_tool_definitions and not caps.supports_tools:
        return False
    if req.has_system_prompt and not caps.supports_system_prompt:
        return False
    if req.requires_structured_output and not caps.supports_structured_output:
        return False
    if req.estimated_input_tokens > caps.max_context_tokens:
        return False
    return True


def _build_system_prompt(
    registry: ModelRegistry,
    candidates: list[str],
    price_table: PriceTable,
) -> str:
    """Stable across turns for a given candidate set → provider prompt
    caching applies (§4.6.8).

    The catalog line includes per-MTok input + output rates so the router
    has concrete numbers to anchor "cheapest" against. Without prices the
    router can only compare on task-profile tags like `fast` / `balanced`,
    which led qwen-plus to pick `claude-sonnet-4-6` for a one-word "test"
    prompt on 2026-06-04 (routing-engine.md §5.6.2 history note).
    """
    lines = [
        "You are Metis's model router. Your ONLY job is to choose which model "
        "should handle a task. You are NOT the assistant responding to the "
        "user — a separate planner model will handle the actual task once "
        "you have picked it.",
        "",
        "Your response MUST be exactly one call to the `choose_model` tool. "
        "Do not write text. Do not greet the user. Do not answer their "
        "question. Even if the user asks for the date, the weather, or "
        "your opinion, your reply is the tool call — the planner model "
        "you pick will answer them afterwards.",
        "",
        "The message after `--- TASK FOR PLANNER ---` below is the task the "
        "planner will receive. Read it to judge complexity, then call "
        "`choose_model`. Do NOT reply to it directly.",
        "",
        "Candidates (price = per-million-token rate; lower = cheaper):",
    ]
    for model_id in candidates:
        try:
            entry = registry.get(model_id)
        except UnknownModelError:
            continue
        caps = entry.capabilities
        bits: list[str] = []
        if entry.task_profile:
            bits.append(", ".join(entry.task_profile))
        if caps.supports_tools:
            bits.append("tools")
        if caps.supports_images:
            bits.append("vision")
        if caps.supports_thinking:
            bits.append("thinking")
        bits.append(f"{caps.max_context_tokens // 1000}k ctx")
        try:
            pricing = price_table.pricing_for(model_id)
            bits.append(
                f"in ${_format_price(pricing.input_per_mtok)}/MTok, "
                f"out ${_format_price(pricing.output_per_mtok)}/MTok"
            )
        except UnknownPricingModelError:
            bits.append("price unknown")
        lines.append(f"- {model_id}  [{'; '.join(bits)}]")
    lines.extend(
        [
            "",
            "Guidance — bias hard toward the cheapest viable model:",
            "- **Default to the cheapest candidate** for short, ambiguous, "
            "conversational, or low-stakes prompts. A one-line user "
            "message is almost never worth a deep model.",
            "- Escalate to a mid-tier model ONLY when the task explicitly "
            "calls for multi-step reasoning, code synthesis across "
            "multiple files, careful refactoring, or non-trivial debugging.",
            "- Escalate to the most expensive (deep) tier ONLY for "
            "architecture design, security review, multi-document "
            "synthesis, or tasks that explicitly request extended "
            'reasoning. "Test", "hi", "continue", or any '
            "single-sentence question is NOT in this category.",
            "- Cost matters. A 4x more expensive model that gives a 5% "
            "better answer on a trivial task is the wrong pick.",
        ]
    )
    return "\n".join(lines)


def _format_price(rate: Decimal) -> str:
    """Render a per-MTok price compactly. Drops trailing zeros."""
    # Strip trailing zeros without losing precision: "0.80" -> "0.8".
    text = f"{rate:.4f}".rstrip("0").rstrip(".")
    return text or "0"


def _build_choose_model_tool(candidates: list[str]) -> ToolDefinition:
    schema = {
        "type": "object",
        "required": ["model_id", "reason"],
        "properties": {
            "model_id": {
                "type": "string",
                "enum": list(candidates),
                "description": "The canonical model id to handle this turn.",
            },
            "reason": {
                "type": "string",
                "maxLength": 200,
                "description": "One-sentence rationale for the pick.",
            },
        },
    }
    return ToolDefinition(
        name=_CHOOSE_MODEL_TOOL,
        description=(
            "Pick the best model for the current user task. Call this tool "
            "exactly once. Return one of the model ids from the enum."
        ),
        input_schema=schema,
        side_effects=SideEffects.NONE,
        requires_workspace=False,
    )


def _find_choose_model_call(content: list) -> ToolUseBlock | None:
    for block in content:
        if isinstance(block, ToolUseBlock) and block.name == _CHOOSE_MODEL_TOOL:
            return block
    return None


def _extract_text_preview(content: list) -> str | None:
    """First TextBlock content, truncated for the trace event.

    Returns None when the response has no TextBlock at all (e.g. the
    router only emitted a ToolUseBlock — the happy path). Returns the
    truncated text otherwise, including for the no_model_chosen failure
    mode where the router wrote prose instead of calling the tool.
    """
    parts: list[str] = []
    for block in content:
        if isinstance(block, TextBlock) and block.text:
            parts.append(block.text)
    if not parts:
        return None
    joined = "\n".join(parts)
    if len(joined) > _RESPONSE_PREVIEW_MAX_CHARS:
        return joined[: _RESPONSE_PREVIEW_MAX_CHARS - 1] + "…"
    return joined


def _normalize_stop_reason(raw: str) -> str:
    """Map adapter `StopReason` to the LLMCallCompleted literal set."""
    if "tool_use" in raw:
        return "tool_use"
    if "max_tokens" in raw:
        return "max_tokens"
    if "stop_sequence" in raw:
        return "stop_sequence"
    return "end_turn"


def _monotonic_ms() -> int:
    import time

    return int(time.monotonic() * 1000)
