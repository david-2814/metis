"""SessionManager: the agent turn loop.

Ties together routing → adapter → tool dispatcher → message store, with
event emission at every meaningful boundary. The model chosen at turn start
owns the entire turn including all tool cycles (routing-engine.md §3.2).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from ulid import ULID

from metis.adapters.errors import AdapterError, CancelledError
from metis.adapters.protocol import (
    CanonicalRequest,
    StopReason,
)
from metis.adapters.tool_id_map import ToolIdMap
from metis.canonical.content import (
    ContentBlock,
    ImageBlock,
    TextBlock,
    ToolUseBlock,
)
from metis.canonical.ids import new_message_id
from metis.canonical.messages import (
    Message,
    MessageMetadata,
    MessageStatus,
    Role,
    RoutingDecisionRecord,
    RoutingMode,
    Usage,
)
from metis.events.bus import EventBus
from metis.events.envelope import Actor
from metis.events.payloads import (
    LLMCallCompleted,
    LLMCallFailed,
    LLMCallStarted,
    TurnCancelled,
    TurnCompleted,
    TurnStarted,
    make_event,
)
from metis.pricing import PriceTable
from metis.routing import (
    ModelRegistry,
    OverrideParseResult,
    RoutingDecision,
    RoutingEngine,
    TurnContext,
    parse_per_message_override,
)
from metis.routing.engine import RoutingError
from metis.sessions.store import Session, SessionStore
from metis.tools.dispatcher import ToolDispatcher

logger = logging.getLogger(__name__)


DEFAULT_SYSTEM_PROMPT = (
    "You are Metis, an AI assistant operating in a developer's workspace. "
    "Use the available tools to read and modify files, run shell commands, "
    "and answer questions about the workspace. Be concise."
)


class UnknownAliasError(ValueError):
    """The user typed `@<alias>` but the alias isn't registered."""

    def __init__(self, alias: str) -> None:
        super().__init__(f"unknown model alias: {alias!r}")
        self.alias = alias


@dataclass(frozen=True)
class TurnResult:
    turn_id: str
    chosen_model: str
    stop_reason: StopReason
    assistant_text: str
    cost_usd: Decimal
    input_tokens: int
    output_tokens: int
    llm_call_count: int
    tool_call_count: int
    wall_time_seconds: float


class SessionManager:
    """Coordinates routing, adapter calls, and tool dispatch for a session."""

    def __init__(
        self,
        *,
        registry: ModelRegistry,
        routing: RoutingEngine,
        dispatcher: ToolDispatcher,
        bus: EventBus,
        store: SessionStore,
        pricing: PriceTable,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        global_default_model: str = "anthropic:claude-sonnet-4-6",
        workspace_default_model: str | None = None,
        max_output_tokens: int = 4096,
    ) -> None:
        self._registry = registry
        self._routing = routing
        self._dispatcher = dispatcher
        self._bus = bus
        self._store = store
        self._pricing = pricing
        self._system_prompt = system_prompt
        self._global_default_model = global_default_model
        self._workspace_default_model = workspace_default_model
        self._max_output_tokens = max_output_tokens
        # Per-session bidirectional tool id maps (canonical-format §6.2).
        self._tool_id_maps: dict[str, ToolIdMap] = {}

    # ---- Session lifecycle --------------------------------------------

    def create_session(self, *, workspace_path: str, active_model: str | None = None) -> Session:
        session = self._store.create_session(
            workspace_path=workspace_path, active_model=active_model
        )
        self._tool_id_maps[session.id] = ToolIdMap()
        return session

    def set_active_model(self, session_id: str, model: str | None) -> None:
        """Apply a /model command. `None` clears the sticky."""
        session = self._store.get_session(session_id)
        if model is not None:
            resolved = self._registry.resolve_alias(model) or model
            if not self._registry.is_configured(resolved):
                raise UnknownAliasError(model)
            session.active_model = resolved
        else:
            session.active_model = None
        self._store.update_session(session)

    # ---- Turn loop ----------------------------------------------------

    async def submit_turn(self, session_id: str, user_text: str) -> TurnResult:
        session = self._store.get_session(session_id)
        turn_id = str(ULID())
        loop_start = asyncio.get_event_loop().time()

        # 1. Parse per-message override.
        override = parse_per_message_override(user_text, self._registry)
        if override.is_unknown_alias:
            raise UnknownAliasError(override.raw_alias or "")

        # 2. Add user message to the session.
        user_message = Message(
            id=new_message_id(),
            session_id=session_id,
            role=Role.USER,
            content=[TextBlock(text=override.cleaned_text)],
            created_at=_now(),
        )
        self._store.add_message(session_id, user_message)

        # 3. Emit turn.started.
        history = self._store.get_messages(session_id)
        tool_definitions = self._dispatcher.get_definitions()
        ctx = self._build_turn_context(
            session_id=session_id,
            turn_id=turn_id,
            history=history,
            tool_definitions=tool_definitions,
            session=session,
            override=override,
        )
        turn_started_event = self._emit_turn_started(
            session_id=session_id,
            turn_id=turn_id,
            user_message=user_message,
            history=history,
            ctx=ctx,
        )

        # 4. Route. Hard-failure here propagates without any LLM/tool events.
        try:
            decision = self._routing.decide(ctx)
        except RoutingError:
            self._emit_turn_cancelled(
                session_id,
                turn_id,
                reason="timeout",
                partial_llm_calls=0,
                partial_tool_calls=0,
            )
            raise

        chosen_model = decision.chosen_model
        provider = self._registry.provider_of(chosen_model)
        adapter = self._registry.adapter_for(chosen_model)

        # 5. Tool-cycle loop with the turn-locked model.
        llm_calls = 0
        tool_calls = 0
        total_input_tokens = 0
        total_output_tokens = 0
        total_cost = Decimal("0")
        final_stop_reason = StopReason.END_TURN
        last_assistant_text = ""
        parent_event_id = turn_started_event

        try:
            while True:
                history = self._store.get_messages(session_id)
                request = CanonicalRequest(
                    request_id=new_message_id(),
                    messages=history,
                    tools=tool_definitions,
                    system_prompt=self._system_prompt,
                    model=chosen_model,
                    max_output_tokens=self._max_output_tokens,
                    tool_id_map=self._tool_id_maps.get(session_id),
                )
                est_tokens = adapter.estimate_input_tokens(
                    history, tool_definitions, self._system_prompt
                )

                llm_started_event = self._emit_llm_call_started(
                    session_id=session_id,
                    turn_id=turn_id,
                    model=chosen_model,
                    provider=provider,
                    request_id=request.request_id,
                    estimated_tokens=est_tokens,
                    parent_event_id=parent_event_id,
                )
                try:
                    response = await adapter.complete(request)
                except AdapterError as exc:
                    self._routing.availability.mark_failure(provider, exc.error_class)
                    self._emit_llm_call_failed(
                        session_id=session_id,
                        turn_id=turn_id,
                        model=chosen_model,
                        provider=provider,
                        exc=exc,
                        parent_event_id=llm_started_event,
                    )
                    if isinstance(exc, CancelledError):
                        self._emit_turn_cancelled(
                            session_id,
                            turn_id,
                            reason="user_cancel",
                            partial_llm_calls=llm_calls,
                            partial_tool_calls=tool_calls,
                        )
                    raise
                else:
                    self._routing.availability.mark_success(provider)

                llm_calls += 1
                total_input_tokens += response.usage.input_tokens
                total_output_tokens += response.usage.output_tokens
                cost = self._pricing.compute_cost(chosen_model, response.usage)
                total_cost += cost

                # Build the assistant message with full metadata.
                assistant_message = Message(
                    id=new_message_id(),
                    session_id=session_id,
                    role=Role.ASSISTANT,
                    content=response.content,
                    created_at=_now(),
                    metadata=MessageMetadata(
                        model=chosen_model,
                        provider=provider,
                        routing=RoutingDecisionRecord(
                            mode=_mode_for_chain_index(decision.winner_index),
                            chosen_model=chosen_model,
                            reason=decision.chain[decision.winner_index].reason,
                            rule_name=decision.chain[decision.winner_index].rule_name,
                        ),
                        usage=Usage(
                            input_tokens=response.usage.input_tokens,
                            output_tokens=response.usage.output_tokens,
                            cached_input_tokens=response.usage.cached_input_tokens,
                            cache_creation_input_tokens=(
                                response.usage.cache_creation_input_tokens
                            ),
                            cost_usd=cost,
                            pricing_version=self._pricing.version,
                            latency_ms=response.latency_ms,
                        ),
                        status=MessageStatus.COMPLETE,
                    ),
                )
                self._store.add_message(session_id, assistant_message)
                last_assistant_text = _assistant_text(response.content) or last_assistant_text

                self._emit_llm_call_completed(
                    session_id=session_id,
                    turn_id=turn_id,
                    model=chosen_model,
                    provider=provider,
                    usage=response.usage,
                    cost=cost,
                    latency_ms=response.latency_ms,
                    stop_reason=response.stop_reason,
                    response_content=response.content,
                    parent_event_id=llm_started_event,
                )

                # 6. Decide whether to dispatch tools and continue, or stop.
                if response.stop_reason != StopReason.TOOL_USE:
                    final_stop_reason = response.stop_reason
                    break

                # Parallel-dispatch all tool_use blocks; collect results.
                tool_uses = [b for b in response.content if isinstance(b, ToolUseBlock)]
                if not tool_uses:
                    final_stop_reason = StopReason.END_TURN
                    break

                results = await asyncio.gather(
                    *[
                        self._dispatcher.dispatch(
                            tu,
                            session_id=session_id,
                            turn_id=turn_id,
                            workspace_path=session.workspace_path,
                            parent_event_id=llm_started_event,
                        )
                        for tu in tool_uses
                    ]
                )
                tool_calls += len(results)

                # Each tool_result becomes its own TOOL message; the adapter
                # merges consecutive TOOL messages into one wire user message.
                for result in results:
                    tool_msg = Message(
                        id=new_message_id(),
                        session_id=session_id,
                        role=Role.TOOL,
                        content=[result],
                        created_at=_now(),
                        metadata=MessageMetadata(parent_tool_use_id=result.tool_use_id),
                    )
                    self._store.add_message(session_id, tool_msg)
                parent_event_id = llm_started_event
        finally:
            wall_time = asyncio.get_event_loop().time() - loop_start

        # 7. Update session cost/turn counters.
        session.cost_so_far_usd += float(total_cost)
        session.turn_count += 1
        self._store.update_session(session)

        # 8. Emit turn.completed.
        self._emit_turn_completed(
            session_id=session_id,
            turn_id=turn_id,
            stop_reason=final_stop_reason,
            llm_calls=llm_calls,
            tool_calls=tool_calls,
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            cost=total_cost,
            wall_time=wall_time,
            parent_event_id=turn_started_event,
        )

        return TurnResult(
            turn_id=turn_id,
            chosen_model=chosen_model,
            stop_reason=final_stop_reason,
            assistant_text=last_assistant_text,
            cost_usd=total_cost,
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            llm_call_count=llm_calls,
            tool_call_count=tool_calls,
            wall_time_seconds=wall_time,
        )

    # ---- Helpers ------------------------------------------------------

    def _build_turn_context(
        self,
        *,
        session_id: str,
        turn_id: str,
        history: list[Message],
        tool_definitions,
        session: Session,
        override: OverrideParseResult,
    ) -> TurnContext:
        has_images = any(isinstance(b, ImageBlock) for m in history for b in m.content)
        # Resolve a default model id by alias if the configured default is one.
        workspace_default = (
            self._registry.resolve_alias(self._workspace_default_model)
            if self._workspace_default_model
            else None
        ) or self._workspace_default_model
        global_default = (
            self._registry.resolve_alias(self._global_default_model) or self._global_default_model
        )
        # estimate_input_tokens is provider-specific; we use the configured
        # default's adapter as a pre-routing estimate. Routing only uses
        # this to gate `exceeds_context_window` so it's an upper bound, not
        # a final number.
        seed_model = session.active_model or workspace_default or global_default
        seed_adapter = (
            self._registry.adapter_for(seed_model) if seed_model in self._registry else None
        )
        estimated_tokens = (
            seed_adapter.estimate_input_tokens(history, tool_definitions, self._system_prompt)
            if seed_adapter
            else _heuristic_token_estimate(history, self._system_prompt)
        )
        return TurnContext(
            session_id=session_id,
            turn_id=turn_id,
            estimated_input_tokens=estimated_tokens,
            has_images=has_images,
            has_tool_definitions=bool(tool_definitions),
            has_system_prompt=bool(self._system_prompt),
            per_message_override=override.resolved_model,
            session_active_model=session.active_model,
            workspace_default_model=workspace_default,
            global_default_model=global_default,
        )

    # ---- Event emitters -----------------------------------------------

    def _emit_turn_started(
        self,
        *,
        session_id: str,
        turn_id: str,
        user_message: Message,
        history: list[Message],
        ctx: TurnContext,
    ) -> str:
        user_text = ""
        for block in user_message.content:
            if isinstance(block, TextBlock):
                user_text = block.text
                break
        payload = TurnStarted(
            user_message_hash=hashlib.sha256(user_text.encode()).hexdigest(),
            estimated_input_tokens=ctx.estimated_input_tokens,
            has_images=ctx.has_images,
            has_tool_calls_in_history=any(
                any(isinstance(b, ToolUseBlock) for b in m.content) for m in history
            ),
        )
        event = make_event(
            type="turn.started",
            session_id=session_id,
            turn_id=turn_id,
            actor=Actor.USER,
            payload=payload,
            timestamp=_now(),
        )
        self._bus.emit(event)
        return event.id

    def _emit_llm_call_started(
        self,
        *,
        session_id: str,
        turn_id: str,
        model: str,
        provider: str,
        request_id: str,
        estimated_tokens: int,
        parent_event_id: str | None,
    ) -> str:
        event = make_event(
            type="llm.call_started",
            session_id=session_id,
            turn_id=turn_id,
            actor=Actor.AGENT,
            payload=LLMCallStarted(
                model=model,
                provider=provider,
                estimated_input_tokens=estimated_tokens,
                request_id=request_id,
                is_worker=False,
            ),
            timestamp=_now(),
            parent_event_id=parent_event_id,
        )
        self._bus.emit(event)
        return event.id

    def _emit_llm_call_completed(
        self,
        *,
        session_id: str,
        turn_id: str,
        model: str,
        provider: str,
        usage,
        cost: Decimal,
        latency_ms: int,
        stop_reason: StopReason,
        response_content: list[ContentBlock],
        parent_event_id: str | None,
    ) -> None:
        produced_tool_calls = sum(1 for b in response_content if isinstance(b, ToolUseBlock))
        from metis.canonical.content import ThinkingBlock

        produced_thinking = sum(1 for b in response_content if isinstance(b, ThinkingBlock))
        self._bus.emit(
            make_event(
                type="llm.call_completed",
                session_id=session_id,
                turn_id=turn_id,
                actor=Actor.AGENT,
                payload=LLMCallCompleted(
                    model=model,
                    provider=provider,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cached_input_tokens=usage.cached_input_tokens,
                    cache_creation_input_tokens=usage.cache_creation_input_tokens,
                    cost_usd=float(cost),
                    pricing_version=self._pricing.version,
                    latency_ms=latency_ms,
                    stop_reason=stop_reason.value,  # type: ignore[arg-type]
                    produced_tool_calls=produced_tool_calls,
                    produced_thinking_blocks=produced_thinking,
                ),
                timestamp=_now(),
                parent_event_id=parent_event_id,
            )
        )

    def _emit_llm_call_failed(
        self,
        *,
        session_id: str,
        turn_id: str,
        model: str,
        provider: str,
        exc: AdapterError,
        parent_event_id: str | None,
    ) -> None:
        self._bus.emit(
            make_event(
                type="llm.call_failed",
                session_id=session_id,
                turn_id=turn_id,
                actor=Actor.AGENT,
                payload=LLMCallFailed(
                    model=model,
                    provider=provider,
                    error_class=exc.error_class.value,  # type: ignore[arg-type]
                    error_message_redacted=str(exc),
                    retry_count=0,
                    latency_ms=0,
                ),
                timestamp=_now(),
                parent_event_id=parent_event_id,
            )
        )

    def _emit_turn_completed(
        self,
        *,
        session_id: str,
        turn_id: str,
        stop_reason: StopReason,
        llm_calls: int,
        tool_calls: int,
        input_tokens: int,
        output_tokens: int,
        cost: Decimal,
        wall_time: float,
        parent_event_id: str,
    ) -> None:
        if stop_reason == StopReason.CANCELLED:
            return  # turn.cancelled is its own event type
        # Map adapter stop_reason → catalog enum literal (drop CANCELLED/ERROR).
        catalog_stop = stop_reason.value
        if catalog_stop not in ("end_turn", "max_tokens", "stop_sequence", "tool_use"):
            catalog_stop = "end_turn"
        self._bus.emit(
            make_event(
                type="turn.completed",
                session_id=session_id,
                turn_id=turn_id,
                actor=Actor.AGENT,
                payload=TurnCompleted(
                    stop_reason=catalog_stop,  # type: ignore[arg-type]
                    llm_call_count=llm_calls,
                    tool_call_count=tool_calls,
                    total_input_tokens=input_tokens,
                    total_output_tokens=output_tokens,
                    total_cost_usd=float(cost),
                    wall_time_seconds=wall_time,
                ),
                timestamp=_now(),
                parent_event_id=parent_event_id,
            )
        )

    def _emit_turn_cancelled(
        self,
        session_id: str,
        turn_id: str,
        *,
        reason: str,
        partial_llm_calls: int,
        partial_tool_calls: int,
    ) -> None:
        self._bus.emit(
            make_event(
                type="turn.cancelled",
                session_id=session_id,
                turn_id=turn_id,
                actor=Actor.USER if reason == "user_cancel" else Actor.SYSTEM,
                payload=TurnCancelled(
                    reason=reason,  # type: ignore[arg-type]
                    partial_llm_calls=partial_llm_calls,
                    partial_tool_calls=partial_tool_calls,
                ),
                timestamp=_now(),
            )
        )


# ---------------------------------------------------------------------------
# Module-private helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


def _assistant_text(content: list[ContentBlock]) -> str:
    """Concatenate text blocks for CLI display. Ignores tool_use/thinking."""
    return "\n".join(b.text for b in content if isinstance(b, TextBlock))


def _mode_for_chain_index(index: int) -> RoutingMode:
    """Map a routing-chain index to the RoutingDecisionRecord.mode summary
    enum (canonical-format §4.3 mapping table)."""
    # chain order: per_message_override, manual_sticky, rule, pattern,
    # delegate_request, workspace_default, global_default
    if index == 0:
        return RoutingMode.OVERRIDE
    if index == 1:
        return RoutingMode.MANUAL
    if index == 2:
        return RoutingMode.RULE
    if index == 3:
        return RoutingMode.PATTERN
    if index == 4:
        return RoutingMode.DELEGATE
    return RoutingMode.DEFAULT


def _heuristic_token_estimate(history: list[Message], system_prompt: str | None) -> int:
    chars = len(system_prompt or "")
    for m in history:
        for block in m.content:
            chars += len(getattr(block, "text", ""))
    return max(1, chars // 4)


# Re-export RoutingDecision for callers that want the typed result.
__all__ = ["SessionManager", "TurnResult", "UnknownAliasError"]
_ = RoutingDecision  # exported via metis.routing
