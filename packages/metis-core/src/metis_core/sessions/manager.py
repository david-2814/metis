"""SessionManager: the agent turn loop.

Ties together routing → adapter → tool dispatcher → message store, with
event emission at every meaningful boundary. The model chosen at turn start
owns the entire turn including all tool cycles (routing-engine.md §3.2).
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from ulid import ULID

from metis_core.adapters.errors import AdapterError, CancelledError
from metis_core.adapters.protocol import (
    CanonicalRequest,
    StopReason,
    TokenUsage,
)
from metis_core.adapters.streaming import (
    MessageComplete,
    StreamingEvent,
)
from metis_core.adapters.tool_id_map import ToolIdMap
from metis_core.canonical.content import (
    ContentBlock,
    ImageBlock,
    TextBlock,
    ToolUseBlock,
)
from metis_core.canonical.ids import new_message_id
from metis_core.canonical.messages import (
    Message,
    MessageMetadata,
    MessageStatus,
    Role,
    RoutingDecisionRecord,
    RoutingMode,
    Usage,
)
from metis_core.events.bus import EventBus
from metis_core.events.envelope import Actor
from metis_core.events.payloads import (
    LLMCallCompleted,
    LLMCallFailed,
    LLMCallStarted,
    MemoryEviction,
    MemoryUpdated,
    TurnCancelled,
    TurnCompleted,
    TurnStarted,
    make_event,
)
from metis_core.memory.store import MemoryStore
from metis_core.pricing import PriceTable
from metis_core.routing import (
    ModelRegistry,
    OverrideParseResult,
    RoutingDecision,
    RoutingEngine,
    TurnContext,
    parse_per_message_override,
)
from metis_core.routing.engine import RoutingError
from metis_core.sessions.store import Session, SessionStore
from metis_core.tools.dispatcher import ToolDispatcher

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


class AmbiguousModelError(ValueError):
    """The user's model input matches multiple registered canonical ids.

    Raised by `/model` resolution when a suffix match returns 2+ candidates.
    Carries the candidate list so the CLI/TUI can prompt for clarification.
    """

    def __init__(self, input: str, candidates: list[str]) -> None:
        super().__init__(
            f"ambiguous model {input!r}; matches {len(candidates)} ids: {', '.join(candidates)}"
        )
        self.input = input
        self.candidates = list(candidates)


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


class UserExplicitModelRejectedError(Exception):
    """Raised when the user's explicit model choice — a per-message
    ``@model`` override or the session sticky model set via ``/model`` —
    fails routing capability validation.

    Without this check, routing silently falls through to the next chain
    slot (typically the global default), so the user is billed for a model
    they didn't pick. The clear UX is to refuse the turn and tell the user
    why so they can switch model, clear the sticky, or change the turn so
    the missing capability isn't needed.

    The ``route.decided`` event still records the rejection in the trace.
    """

    def __init__(
        self,
        *,
        source: str,
        model: str,
        validation_failure: str,
        would_fall_back_to: str | None,
    ) -> None:
        self.source = source
        self.model = model
        self.validation_failure = validation_failure
        self.would_fall_back_to = would_fall_back_to
        fallback_phrase = (
            f" (would have fallen back to {would_fall_back_to})" if would_fall_back_to else ""
        )
        super().__init__(
            f"{source} {model} can't handle this turn: {validation_failure}"
            f"{fallback_phrase}. "
            f"Pick a different model, clear the sticky with `/model -`, or "
            f"adjust the turn (e.g. drop tools/images)."
        )


# Callback signature for live streaming events. May be sync or async.
StreamHandler = Callable[[StreamingEvent], Awaitable[None] | None]


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
        memory_factory: Callable[[str], MemoryStore] | None = None,
        skill_store_factory: Callable[[str], Any] | None = None,
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
        # Per-session memory stores (Phase 2 bounded MEMORY.md / USER.md).
        # None means the session has no memory; the memory tools will refuse.
        self._memory_factory = memory_factory
        self._memory_stores: dict[str, MemoryStore | None] = {}
        # Per-session skill stores. None means the session has no skills;
        # skill tools refuse to run.
        self._skill_store_factory = skill_store_factory
        self._skill_stores: dict[str, Any] = {}
        # /share state — captures the most recent slash-command output per
        # session so the next user message can include it. See `/share` in
        # the CLI/TUI. One-shot: cleared on consumption.
        self._slash_buffers: dict[str, str] = {}
        self._share_pending: set[str] = set()

    # ---- Session lifecycle --------------------------------------------

    def create_session(self, *, workspace_path: str, active_model: str | None = None) -> Session:
        # Resolve aliases to canonical ids for consistency with set_active_model.
        resolved: str | None = None
        if active_model is not None:
            resolved = self._registry.resolve_alias(active_model) or active_model
            if not self._registry.is_configured(resolved):
                raise UnknownAliasError(active_model)
        session = self._store.create_session(workspace_path=workspace_path, active_model=resolved)
        self._tool_id_maps[session.id] = ToolIdMap()
        if self._memory_factory is not None:
            self._memory_stores[session.id] = self._memory_factory(workspace_path)
        else:
            self._memory_stores[session.id] = None
        if self._skill_store_factory is not None:
            self._skill_stores[session.id] = self._skill_store_factory(workspace_path)
        else:
            self._skill_stores[session.id] = None
        return session

    def get_session(self, session_id: str) -> Session:
        """Return the current Session record from the store.

        Always re-reads from `SessionStore` — never returns a cached copy.
        Callers that hold a long-lived Session reference (REPL, TUI) should
        call this after any mutation (`set_active_model`, post-turn updates)
        to avoid showing stale fields like `active_model` or `cost_so_far_usd`.
        """
        return self._store.get_session(session_id)

    def memory_for(self, session_id: str) -> MemoryStore | None:
        """Return the per-session memory store, if memory is configured."""
        return self._memory_stores.get(session_id)

    def skills_for(self, session_id: str) -> Any:
        """Return the per-session skill store, if skills are configured."""
        return self._skill_stores.get(session_id)

    # ---- /share bridge ------------------------------------------------

    def buffer_slash_output(self, session_id: str, text: str) -> None:
        """Capture the rendered output of a slash command so the user can
        later run `/share` to inject it into the next turn's context.

        Buffers per-session; each call overwrites any prior buffer. The
        agent doesn't see the buffer until the user explicitly opts in via
        `/share` — slash commands are otherwise local to the client.
        """
        if text:
            self._slash_buffers[session_id] = text

    def mark_share_pending(self, session_id: str) -> str | None:
        """Flag the buffered slash output for inclusion in the next turn.

        Returns the buffered text (for the caller to render a preview /
        confirmation), or None if nothing has been buffered yet.
        """
        text = self._slash_buffers.get(session_id)
        if text is None:
            return None
        self._share_pending.add(session_id)
        return text

    def consume_pending_share(self, session_id: str) -> str | None:
        """Return the buffered slash output if `/share` was pending, then
        clear the flag. Internal: called by `submit_turn` at turn start.

        Does NOT clear the buffer itself — subsequent slash commands
        overwrite it normally. Only the one-shot pending flag clears.
        """
        if session_id not in self._share_pending:
            return None
        self._share_pending.discard(session_id)
        return self._slash_buffers.get(session_id)

    def set_active_model(self, session_id: str, model: str | None) -> str | None:
        """Apply a /model command. `None` clears the Active model.

        Returns the resolved canonical id (or None when cleared) so callers
        can display the resolution result without re-reading their stale
        local Session reference.

        Resolution policy for non-None inputs:

        1. Exact alias / canonical-id match → use it.
        2. Boundary-respecting suffix match (`ModelRegistry.find_by_suffix`):
           - Exactly one match: auto-resolve. Common case for users typing
             `openai/gpt-oss-20b` instead of `openrouter:openai/gpt-oss-20b`.
           - Two or more matches: raise `AmbiguousModelError` carrying the
             candidate list so the caller can prompt for clarification.
        3. No match anywhere: raise `UnknownAliasError`.
        """
        session = self._store.get_session(session_id)
        if model is not None:
            resolved = self._resolve_model_input(model)
            session.active_model = resolved
        else:
            session.active_model = None
        self._store.update_session(session)
        return session.active_model

    def _resolve_model_input(self, input: str) -> str:
        """Lookup policy for `/model <input>`. See `set_active_model` docs."""
        direct = self._registry.resolve_alias(input)
        if direct is not None:
            return direct
        candidates = self._registry.find_by_suffix(input)
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            raise AmbiguousModelError(input, candidates)
        raise UnknownAliasError(input)

    # ---- Turn loop ----------------------------------------------------

    async def submit_turn(
        self,
        session_id: str,
        user_text: str,
        *,
        on_streaming_event: StreamHandler | None = None,
    ) -> TurnResult:
        session = self._store.get_session(session_id)
        turn_id = str(ULID())
        loop_start = asyncio.get_event_loop().time()

        # 1. Parse per-message override.
        override = parse_per_message_override(user_text, self._registry)
        if override.is_unknown_alias:
            raise UnknownAliasError(override.raw_alias or "")

        # 2. If `/share` was pending, prepend the buffered slash-command
        #    output to the user message. One-shot: the flag clears here.
        #    The composed text is what gets persisted as the user Message
        #    so the agent's behavior is reconstructible from history.
        message_text = override.cleaned_text
        shared = self.consume_pending_share(session_id)
        if shared:
            message_text = _compose_message_with_shared(shared, message_text)

        # 3. Add user message to the session.
        user_message = Message(
            id=new_message_id(),
            session_id=session_id,
            role=Role.USER,
            content=[TextBlock(text=message_text)],
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

        # Refuse to silently fall through when the user picked a model
        # explicitly (@override or sticky) and it failed validation. The
        # `route.decided` event already carries the rejection in the trace;
        # we just don't proceed to call a different model behind their back.
        explicit_rejection = _user_explicit_rejection(decision)
        if explicit_rejection is not None:
            raise explicit_rejection

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

        memory = self._memory_stores.get(session_id)
        skill_store = self._skill_stores.get(session_id)

        try:
            while True:
                history = self._store.get_messages(session_id)
                # Compose system prompt fresh each LLM call so mid-turn
                # memory writes (from a tool) are reflected in the next call.
                turn_memory = self._memory_stores.get(session_id)
                system_prompt = (
                    turn_memory.assemble_system_prompt(self._system_prompt)
                    if turn_memory is not None
                    else self._system_prompt
                )
                # Inject the skill discovery index (agentskills.io stage 1).
                if skill_store is not None and len(skill_store) > 0:
                    system_prompt = _append_skill_index(system_prompt, skill_store)
                request = CanonicalRequest(
                    request_id=new_message_id(),
                    messages=history,
                    tools=tool_definitions,
                    system_prompt=system_prompt,
                    model=chosen_model,
                    max_output_tokens=self._max_output_tokens,
                    tool_id_map=self._tool_id_maps.get(session_id),
                )
                est_tokens = adapter.estimate_input_tokens(history, tool_definitions, system_prompt)

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
                    final = await _consume_stream(adapter.stream(request), on_streaming_event)
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
                total_input_tokens += final.usage.input_tokens
                total_output_tokens += final.usage.output_tokens
                cost = self._pricing.compute_cost(chosen_model, final.usage)
                total_cost += cost

                # Build the assistant message with full metadata.
                assistant_message = Message(
                    id=new_message_id(),
                    session_id=session_id,
                    role=Role.ASSISTANT,
                    content=final.final_content,
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
                            input_tokens=final.usage.input_tokens,
                            output_tokens=final.usage.output_tokens,
                            cached_input_tokens=final.usage.cached_input_tokens,
                            cache_creation_input_tokens=(final.usage.cache_creation_input_tokens),
                            cost_usd=cost,
                            pricing_version=self._pricing.version,
                            latency_ms=final.latency_ms,
                        ),
                        status=MessageStatus.COMPLETE,
                    ),
                )
                self._store.add_message(session_id, assistant_message)
                last_assistant_text = _assistant_text(final.final_content) or last_assistant_text

                self._emit_llm_call_completed(
                    session_id=session_id,
                    turn_id=turn_id,
                    model=chosen_model,
                    provider=provider,
                    usage=final.usage,
                    cost=cost,
                    latency_ms=final.latency_ms,
                    stop_reason=final.stop_reason,
                    response_content=final.final_content,
                    parent_event_id=llm_started_event,
                )

                # 6. Decide whether to dispatch tools and continue, or stop.
                if final.stop_reason != StopReason.TOOL_USE:
                    final_stop_reason = final.stop_reason
                    break

                # Parallel-dispatch all tool_use blocks; collect results.
                tool_uses = [b for b in final.final_content if isinstance(b, ToolUseBlock)]
                if not tool_uses:
                    final_stop_reason = StopReason.END_TURN
                    break

                # Snapshot memory hashes before tool dispatch so we can
                # detect mutations performed by memory tools and emit
                # memory.updated events.
                memory_before = _memory_hashes(memory)

                results = await asyncio.gather(
                    *[
                        self._dispatcher.dispatch(
                            tu,
                            session_id=session_id,
                            turn_id=turn_id,
                            workspace_path=session.workspace_path,
                            parent_event_id=llm_started_event,
                            memory=memory,
                            skills=skill_store,
                        )
                        for tu in tool_uses
                    ]
                )
                tool_calls += len(results)

                # Emit memory.updated / memory.eviction events for any file
                # that changed during this batch of tool calls.
                self._emit_memory_events_for_changes(
                    memory_before=memory_before,
                    memory=memory,
                    tool_uses=tool_uses,
                    session_id=session_id,
                    turn_id=turn_id,
                    parent_event_id=llm_started_event,
                )

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
        has_tool_calls_in_history = any(
            isinstance(b, ToolUseBlock) for m in history for b in m.content
        )
        user_message_text = ""
        # The new USER message is the last USER message in the (already-stored)
        # history. Pull the first text block's content out for predicate eval.
        for msg in reversed(history):
            if msg.role == Role.USER:
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        user_message_text = block.text
                        break
                break
        return TurnContext(
            session_id=session_id,
            turn_id=turn_id,
            estimated_input_tokens=estimated_tokens,
            has_images=has_images,
            has_tool_definitions=bool(tool_definitions),
            has_system_prompt=bool(self._system_prompt),
            has_tool_calls_in_history=has_tool_calls_in_history,
            per_message_override=override.resolved_model,
            session_active_model=session.active_model,
            workspace_default_model=workspace_default,
            global_default_model=global_default,
            user_message_text=user_message_text,
            workspace_path=session.workspace_path,
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
        from metis_core.canonical.content import ThinkingBlock

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

    def _emit_memory_events_for_changes(
        self,
        *,
        memory_before: dict,
        memory: MemoryStore | None,
        tool_uses: list,
        session_id: str,
        turn_id: str,
        parent_event_id: str,
    ) -> None:
        """Diff before/after hashes per file and emit memory.updated for
        each change, plus memory.eviction if the file is over its soft cap.

        Operation is inferred from which memory tool ran in this batch.
        """
        if memory is None:
            return
        memory_after = _memory_hashes(memory)
        ops_by_file: dict[str, str] = {}
        for tu in tool_uses:
            name = tu.name
            file_arg = (tu.input or {}).get("file")
            if not file_arg:
                continue
            if name == "memory_add":
                ops_by_file[file_arg] = "add"
            elif name == "memory_replace":
                ops_by_file[file_arg] = "replace"
            elif name == "memory_consolidate":
                ops_by_file[file_arg] = "consolidate"
        for file_name, after in memory_after.items():
            before = memory_before.get(file_name)
            if before is None or before == after:
                continue
            operation = ops_by_file.get(file_name, "consolidate")
            self._bus.emit(
                make_event(
                    type="memory.updated",
                    session_id=session_id,
                    turn_id=turn_id,
                    actor=Actor.AGENT,
                    payload=MemoryUpdated(
                        file=file_name,  # type: ignore[arg-type]
                        operation=operation,  # type: ignore[arg-type]
                        before_hash=before["hash"],
                        after_hash=after["hash"],
                        before_size_bytes=before["size"],
                        after_size_bytes=after["size"],
                    ),
                    timestamp=_now(),
                    parent_event_id=parent_event_id,
                )
            )
            # Over-soft-cap → eviction warning (no auto-truncate).
            from metis_core.memory.store import MemoryFile
            from metis_core.memory.store import MemoryStore as _MS

            mf = MemoryFile(file_name)
            if after["size"] > _MS.soft_cap(mf):
                self._bus.emit(
                    make_event(
                        type="memory.eviction",
                        session_id=session_id,
                        turn_id=turn_id,
                        actor=Actor.SYSTEM,
                        payload=MemoryEviction(
                            file=file_name,  # type: ignore[arg-type]
                            trigger="size_cap_exceeded",
                            entries_evicted=0,
                            size_before_bytes=before["size"],
                            size_after_bytes=after["size"],
                        ),
                        timestamp=_now(),
                        parent_event_id=parent_event_id,
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


def _append_skill_index(system_prompt: str, skill_store: Any) -> str:
    """Append the discovery index (agentskills.io stage 1) to the system prompt.

    One line per skill: `- <name>: <description>`. Bodies are NOT injected —
    the agent calls `skill_load(name)` to activate one.
    """
    lines = [
        "## Available skills",
        "Use `skill_search(query)` to filter and `skill_load(name)` to read a body.",
        "",
    ]
    for name, description in skill_store.discovery_index():
        lines.append(f"- {name}: {description}")
    return system_prompt.rstrip() + "\n\n" + "\n".join(lines)


def _memory_hashes(memory: MemoryStore | None) -> dict:
    """Snapshot the current hash + size of each memory file. Empty/missing
    files have an empty hash and size 0."""
    import hashlib as _hashlib

    if memory is None:
        return {}
    snapshot: dict = {}
    for name in ("MEMORY.md", "USER.md"):
        content = memory.read(name)
        snapshot[name] = {
            "hash": _hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "size": len(content.encode("utf-8")),
        }
    return snapshot


async def _consume_stream(
    stream,
    on_event: StreamHandler | None,
) -> MessageComplete:
    """Iterate an adapter stream, forwarding each event to `on_event` if set,
    and return the MessageComplete event (which carries final state)."""
    final: MessageComplete | None = None
    async for event in stream:
        if on_event is not None:
            result = on_event(event)
            if inspect.isawaitable(result):
                await result
        if isinstance(event, MessageComplete):
            final = event
    if final is None:
        # Stream ended without MessageComplete — synthesize an empty one so
        # the caller has something to work with. This shouldn't happen with
        # a well-behaved adapter.
        final = MessageComplete(
            message_id="",
            stop_reason=StopReason.END_TURN,
            final_content=[],
            usage=TokenUsage(0, 0),
            latency_ms=0,
        )
    return final


_USER_EXPLICIT_POLICIES = ("per_message_override", "manual_sticky")


def _user_explicit_rejection(decision: RoutingDecision) -> UserExplicitModelRejectedError | None:
    """If the user's explicit model choice (per-message override or session
    sticky) was rejected by routing validation, build the error to raise.

    Routing's default behavior is to fall through to the next candidate
    when validation fails. For user-explicit choices that's the wrong UX:
    the user picked a specific model, so we'd rather refuse the turn than
    bill them for something else. Returns None when neither user-explicit
    slot was rejected (either the user's choice won, or no explicit choice
    was set in the first place).
    """
    for evaluation in decision.chain:
        if evaluation.policy not in _USER_EXPLICIT_POLICIES:
            continue
        if evaluation.verdict != "rejected":
            continue
        source = (
            "@model override" if evaluation.policy == "per_message_override" else "active model"
        )
        return UserExplicitModelRejectedError(
            source=source,
            model=evaluation.candidate_model or "",
            validation_failure=evaluation.validation_failure or "rejected",
            would_fall_back_to=decision.chosen_model or None,
        )
    return None


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


_INTERNAL_WHITESPACE_RUN = re.compile(r" {2,}")
_LEADING_SPACE_RUN = re.compile(r"^( *)")


def _normalize_shared_text(text: str) -> str:
    """Strip alignment whitespace before the shared output crosses into the
    LLM context.

    `/models` and similar slash output use column padding (often 4+ spaces
    between fields) and trailing whitespace from right-padding — useful for
    visual alignment on the human's screen, pure noise to the LLM. Tokenizers
    don't compress mid-line whitespace runs well, so a 30-row /models dump
    can carry 100+ tokens of pure padding.

    Transforms applied per line:

    - Tabs are expanded to 4 spaces (so they don't bias whitespace handling).
    - Trailing whitespace is dropped.
    - Empty lines are dropped.
    - Leading indent is preserved (it carries the tree hierarchy of nested
      provider / namespace headers in `/models` output).
    - Internal runs of 2+ spaces in the line body collapse to a single space.

    The original buffer (what the user saw on screen) is unaffected — only
    the LLM-bound version goes through this. Trace history records the
    normalized text, which is what the agent actually saw.
    """
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.expandtabs(4).rstrip()
        if not line:
            continue
        match = _LEADING_SPACE_RUN.match(line)
        lead = match.group(1) if match else ""
        body = line[len(lead) :]
        normalized_body = _INTERNAL_WHITESPACE_RUN.sub(" ", body)
        out.append(lead + normalized_body)
    return "\n".join(out)


def _compose_message_with_shared(shared: str, user_text: str) -> str:
    """Format the user message when `/share` injected slash output.

    Wraps the (normalized) shared block in clear delimiters so the agent
    can see the boundary between "context the user shared from their
    terminal" and "what the user is actually asking." The normalized text
    is what's persisted in history.
    """
    normalized = _normalize_shared_text(shared)
    return (
        "[Shared from my terminal — output of a slash command I ran:]\n"
        f"{normalized}\n"
        "[End of shared output]\n"
        "\n"
        f"{user_text}"
    )


# Re-export RoutingDecision for callers that want the typed result.
__all__ = [
    "SessionManager",
    "TurnResult",
    "UnknownAliasError",
    "UserExplicitModelRejectedError",
]
_ = RoutingDecision  # exported via metis_core.routing
