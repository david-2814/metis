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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from ulid import ULID

from metis.adapters.errors import AdapterError, CancelledError
from metis.adapters.protocol import (
    CanonicalRequest,
    StopReason,
    TokenUsage,
)
from metis.adapters.streaming import (
    MessageComplete,
    StreamingEvent,
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
    MemoryEviction,
    MemoryUpdated,
    TurnCancelled,
    TurnCompleted,
    TurnStarted,
    make_event,
)
from metis.memory.store import MemoryStore
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

    def memory_for(self, session_id: str) -> MemoryStore | None:
        """Return the per-session memory store, if memory is configured."""
        return self._memory_stores.get(session_id)

    def skills_for(self, session_id: str) -> Any:
        """Return the per-session skill store, if skills are configured."""
        return self._skill_stores.get(session_id)

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
                est_tokens = adapter.estimate_input_tokens(
                    history, tool_definitions, system_prompt
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
            from metis.memory.store import MemoryFile
            from metis.memory.store import MemoryStore as _MS

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
    lines = ["## Available skills",
             "Use `skill_search(query)` to filter and `skill_load(name)` to read a body.",
             ""]
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
