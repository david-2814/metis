"""Per-request stateless harness.

`GatewayHarness` is the gateway's equivalent of `SessionManager`: it composes
routing + adapter call + cost stamping + event emission for one inbound
request. There is no session, no message history persisted, no tool loop —
the client owns those (gateway.md §2).

Trace events emitted per call:
- `route.decided` (emitted by `RoutingEngine.decide`)
- `llm.call_started`
- `llm.call_completed` (success) or `llm.call_failed` (error)
- `turn.completed` (so analytics counts gateway requests in the turn rollup)

`gateway_key_id` and `inbound_shape` are stamped onto the `llm.call_completed`
and `turn.completed` payload dicts as additive fields per `gateway.md §6`.
`LLMCallCompleted` now carries these as typed catalog fields (defaulting to
`None` for agent-loop traffic); `TurnCompleted` still gets a dict-envelope
stamp until the typed extension lands there too.

`user_id` / `team_id` (multi-user.md §4.4) are stamped onto both events as
dict-envelope fields. They are typed extensions in the spec but currently
land on the wire via the same envelope path until the typed extension on
`LLMCallCompleted` / `TurnCompleted` ships (Agent 8a-4 in this wave).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from metis_core.adapters.errors import AdapterError, CancelledError
from metis_core.adapters.protocol import (
    CanonicalRequest,
    CanonicalResponse,
    StopReason,
)
from metis_core.adapters.streaming import MessageComplete, StreamingEvent
from metis_core.adapters.tool_id_map import ToolIdMap
from metis_core.canonical.ids import new_message_id
from metis_core.canonical.messages import Message
from metis_core.canonical.tools import ToolDefinition
from metis_core.events.bus import EventBus
from metis_core.events.envelope import Actor
from metis_core.events.payloads import (
    LLMCallCompleted,
    LLMCallFailed,
    LLMCallStarted,
    TurnCompleted,
    make_event,
)
from metis_core.pricing import PriceTable
from metis_core.routing import ModelRegistry, RoutingEngine, TurnContext
from metis_core.routing.engine import RoutingError

from metis_gateway.auth import Identity

logger = logging.getLogger(__name__)


class GatewayError(Exception):
    """Base class for harness-level failures surfaced to the HTTP handler."""


class RoutingFailedError(GatewayError):
    """Routing chain exhausted with no eligible model."""

    def __init__(self, message: str, *, chain: list) -> None:
        super().__init__(message)
        self.chain = chain


class ModelNotAllowedError(GatewayError):
    """The resolved model is not in this key's `allowed_models` list."""


class UpstreamProviderError(GatewayError):
    """The provider adapter raised — wrap so the handler can format an error body."""

    def __init__(self, exc: AdapterError) -> None:
        super().__init__(str(exc))
        self.adapter_error = exc


class ClientDisconnected(GatewayError):
    """The client closed the connection while the adapter call was in-flight."""


@dataclass(frozen=True)
class GatewayCallResult:
    response: CanonicalResponse
    chosen_model: str
    requested_model: str
    cost_usd: Decimal


@dataclass
class GatewayHarness:
    """Routes + dispatches + traces a single inbound LLM request.

    The harness is workspace-scoped because routing rules and pattern store
    are workspace-keyed; gateway keys map to a workspace (gateway.md §3.3).
    """

    bus: EventBus
    registry: ModelRegistry
    routing: RoutingEngine
    pricing: PriceTable
    global_default_model: str
    inbound_shape: str = "openai"

    async def call(
        self,
        *,
        messages: list[Message],
        tools: list[ToolDefinition],
        system_prompt: str | None,
        max_output_tokens: int,
        temperature: float | None,
        stop_sequences: list[str],
        output_schema: dict | None,
        requested_model: str,
        identity: Identity,
        allowed_models: tuple[str, ...] | None,
        is_disconnected: _DisconnectProbe | None = None,
        system_prompt_volatile: str | None = None,
    ) -> GatewayCallResult:
        workspace_path = identity.workspace_path
        session_id = f"gw_{new_message_id()}"
        turn_id = f"gt_{new_message_id()}"
        loop_start = time.monotonic()

        resolved_override = self.registry.resolve_alias(requested_model)

        ctx = self._build_turn_context(
            session_id=session_id,
            turn_id=turn_id,
            messages=messages,
            tools=tools,
            system_prompt=system_prompt,
            output_schema=output_schema,
            per_message_override=resolved_override,
            workspace_path=workspace_path,
        )
        try:
            decision = self.routing.decide(ctx)
        except RoutingError as exc:
            self._emit_turn_completed(
                session_id=session_id,
                turn_id=turn_id,
                stop_reason="end_turn",
                llm_calls=0,
                input_tokens=0,
                output_tokens=0,
                cost=Decimal("0"),
                wall_time=time.monotonic() - loop_start,
                identity=identity,
            )
            raise RoutingFailedError(str(exc), chain=exc.chain) from exc

        chosen_model = decision.chosen_model
        if allowed_models is not None and chosen_model not in allowed_models:
            raise ModelNotAllowedError(
                f"model {chosen_model!r} is not in this key's allowed_models list"
            )

        provider = self.registry.provider_of(chosen_model)
        adapter = self.registry.adapter_for(chosen_model)

        tool_map = ToolIdMap()
        request = CanonicalRequest(
            request_id=new_message_id(),
            messages=messages,
            tools=tools,
            system_prompt=system_prompt,
            model=chosen_model,
            max_output_tokens=max_output_tokens,
            stop_sequences=stop_sequences,
            temperature=temperature,
            output_schema=output_schema,
            stream=False,
            tool_id_map=tool_map,
            workspace_path=workspace_path,
            system_prompt_volatile=system_prompt_volatile,
        )
        estimated_tokens = adapter.estimate_input_tokens(messages, tools, system_prompt)
        llm_started_event = self._emit_llm_call_started(
            session_id=session_id,
            turn_id=turn_id,
            model=chosen_model,
            provider=provider,
            request_id=request.request_id,
            estimated_tokens=estimated_tokens,
        )

        adapter_task = asyncio.create_task(adapter.complete(request))
        disconnect_task: asyncio.Task | None = None
        if is_disconnected is not None:
            disconnect_task = asyncio.create_task(_watch_disconnect(is_disconnected))

        try:
            response = await _await_with_disconnect_guard(
                adapter_task,
                disconnect_task=disconnect_task,
                adapter=adapter,
                request_id=request.request_id,
            )
        except AdapterError as exc:
            self.routing.availability.mark_failure(provider, chosen_model, exc.error_class)
            self._emit_llm_call_failed(
                session_id=session_id,
                turn_id=turn_id,
                model=chosen_model,
                provider=provider,
                exc=exc,
                parent_event_id=llm_started_event,
            )
            if isinstance(exc, CancelledError):
                self._emit_turn_completed(
                    session_id=session_id,
                    turn_id=turn_id,
                    stop_reason="end_turn",
                    llm_calls=1,
                    input_tokens=0,
                    output_tokens=0,
                    cost=Decimal("0"),
                    wall_time=time.monotonic() - loop_start,
                    identity=identity,
                )
                raise ClientDisconnected("client disconnected") from exc
            raise UpstreamProviderError(exc) from exc
        finally:
            if disconnect_task is not None and not disconnect_task.done():
                disconnect_task.cancel()

        self.routing.availability.mark_success(provider, chosen_model)

        try:
            cost = self.pricing.compute_cost(chosen_model, response.usage)
        except Exception:
            logger.exception("pricing lookup failed for %s; using zero", chosen_model)
            cost = Decimal("0")

        self._emit_llm_call_completed(
            session_id=session_id,
            turn_id=turn_id,
            model=chosen_model,
            provider=provider,
            response=response,
            cost=cost,
            parent_event_id=llm_started_event,
            identity=identity,
        )
        self._emit_turn_completed(
            session_id=session_id,
            turn_id=turn_id,
            stop_reason=_stop_reason_to_catalog(response.stop_reason),
            llm_calls=1,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cost=cost,
            wall_time=time.monotonic() - loop_start,
            identity=identity,
        )

        return GatewayCallResult(
            response=response,
            chosen_model=chosen_model,
            requested_model=requested_model,
            cost_usd=cost,
        )

    async def stream(
        self,
        *,
        messages: list[Message],
        tools: list[ToolDefinition],
        system_prompt: str | None,
        max_output_tokens: int,
        temperature: float | None,
        stop_sequences: list[str],
        output_schema: dict | None,
        requested_model: str,
        identity: Identity,
        allowed_models: tuple[str, ...] | None,
        is_disconnected: _DisconnectProbe | None = None,
        system_prompt_volatile: str | None = None,
    ) -> AsyncIterator[StreamingEvent]:
        workspace_path = identity.workspace_path
        """Stream canonical StreamingEvents while emitting the same trace
        events as `call()`.

        Yields each `StreamingEvent` from the chosen adapter as it arrives.
        Emits `llm.call_started` before the first event, `llm.call_completed`
        + `turn.completed` after `MessageComplete`, and `llm.call_failed` +
        `turn.completed` on adapter error or client disconnect.

        Raises the same exception classes as `call()` so the HTTP handler can
        translate them into status codes uniformly. Routing errors and
        `ModelNotAllowedError` surface before any SSE byte is written; the
        handler can therefore still return a regular JSON error in those
        cases.
        """
        session_id = f"gw_{new_message_id()}"
        turn_id = f"gt_{new_message_id()}"
        loop_start = time.monotonic()

        resolved_override = self.registry.resolve_alias(requested_model)
        ctx = self._build_turn_context(
            session_id=session_id,
            turn_id=turn_id,
            messages=messages,
            tools=tools,
            system_prompt=system_prompt,
            output_schema=output_schema,
            per_message_override=resolved_override,
            workspace_path=workspace_path,
        )
        try:
            decision = self.routing.decide(ctx)
        except RoutingError as exc:
            self._emit_turn_completed(
                session_id=session_id,
                turn_id=turn_id,
                stop_reason="end_turn",
                llm_calls=0,
                input_tokens=0,
                output_tokens=0,
                cost=Decimal("0"),
                wall_time=time.monotonic() - loop_start,
                identity=identity,
            )
            raise RoutingFailedError(str(exc), chain=exc.chain) from exc

        chosen_model = decision.chosen_model
        if allowed_models is not None and chosen_model not in allowed_models:
            raise ModelNotAllowedError(
                f"model {chosen_model!r} is not in this key's allowed_models list"
            )

        provider = self.registry.provider_of(chosen_model)
        adapter = self.registry.adapter_for(chosen_model)

        tool_map = ToolIdMap()
        request = CanonicalRequest(
            request_id=new_message_id(),
            messages=messages,
            tools=tools,
            system_prompt=system_prompt,
            model=chosen_model,
            max_output_tokens=max_output_tokens,
            stop_sequences=stop_sequences,
            temperature=temperature,
            output_schema=output_schema,
            stream=True,
            tool_id_map=tool_map,
            workspace_path=workspace_path,
            system_prompt_volatile=system_prompt_volatile,
        )
        estimated_tokens = adapter.estimate_input_tokens(messages, tools, system_prompt)
        llm_started_event = self._emit_llm_call_started(
            session_id=session_id,
            turn_id=turn_id,
            model=chosen_model,
            provider=provider,
            request_id=request.request_id,
            estimated_tokens=estimated_tokens,
        )

        final_complete: MessageComplete | None = None
        try:
            async for event in _stream_with_disconnect_guard(
                adapter.stream(request),
                is_disconnected=is_disconnected,
                adapter=adapter,
                request_id=request.request_id,
            ):
                if isinstance(event, MessageComplete):
                    final_complete = event
                yield event
        except AdapterError as exc:
            self.routing.availability.mark_failure(provider, chosen_model, exc.error_class)
            self._emit_llm_call_failed(
                session_id=session_id,
                turn_id=turn_id,
                model=chosen_model,
                provider=provider,
                exc=exc,
                parent_event_id=llm_started_event,
            )
            if isinstance(exc, CancelledError):
                self._emit_turn_completed(
                    session_id=session_id,
                    turn_id=turn_id,
                    stop_reason="end_turn",
                    llm_calls=1,
                    input_tokens=0,
                    output_tokens=0,
                    cost=Decimal("0"),
                    wall_time=time.monotonic() - loop_start,
                    identity=identity,
                )
                raise ClientDisconnected("client disconnected") from exc
            raise UpstreamProviderError(exc) from exc

        self.routing.availability.mark_success(provider, chosen_model)

        if final_complete is None:
            # Adapter exited without a MessageComplete. Treat as empty success.
            final_complete = MessageComplete(
                message_id=new_message_id(),
                stop_reason=StopReason.END_TURN,
                final_content=[],
                usage=_zero_usage(),
                latency_ms=0,
            )

        synthetic_response = CanonicalResponse(
            request_id=request.request_id,
            model=chosen_model,
            provider=provider,
            content=final_complete.final_content,
            stop_reason=final_complete.stop_reason,
            usage=final_complete.usage,
            latency_ms=final_complete.latency_ms,
        )
        try:
            cost = self.pricing.compute_cost(chosen_model, final_complete.usage)
        except Exception:
            logger.exception("pricing lookup failed for %s; using zero", chosen_model)
            cost = Decimal("0")

        self._emit_llm_call_completed(
            session_id=session_id,
            turn_id=turn_id,
            model=chosen_model,
            provider=provider,
            response=synthetic_response,
            cost=cost,
            parent_event_id=llm_started_event,
            identity=identity,
        )
        self._emit_turn_completed(
            session_id=session_id,
            turn_id=turn_id,
            stop_reason=_stop_reason_to_catalog(final_complete.stop_reason),
            llm_calls=1,
            input_tokens=final_complete.usage.input_tokens,
            output_tokens=final_complete.usage.output_tokens,
            cost=cost,
            wall_time=time.monotonic() - loop_start,
            identity=identity,
        )

    # ---- TurnContext --------------------------------------------------

    def _build_turn_context(
        self,
        *,
        session_id: str,
        turn_id: str,
        messages: list[Message],
        tools: list[ToolDefinition],
        system_prompt: str | None,
        output_schema: dict | None,
        per_message_override: str | None,
        workspace_path: str,
    ) -> TurnContext:
        seed_model = per_message_override or self.global_default_model
        seed_adapter = (
            self.registry.adapter_for(seed_model) if seed_model in self.registry else None
        )
        estimated_tokens = (
            seed_adapter.estimate_input_tokens(messages, tools, system_prompt)
            if seed_adapter is not None
            else _heuristic_token_estimate(messages, system_prompt)
        )
        from metis_core.canonical.content import ImageBlock, TextBlock, ToolUseBlock

        has_images = any(isinstance(b, ImageBlock) for m in messages for b in m.content)
        has_tool_calls_in_history = any(
            isinstance(b, ToolUseBlock) for m in messages for b in m.content
        )
        user_message_text = ""
        for msg in reversed(messages):
            if msg.role.value == "user":
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
            has_tool_definitions=bool(tools),
            has_system_prompt=bool(system_prompt),
            has_tool_calls_in_history=has_tool_calls_in_history,
            requires_structured_output=output_schema is not None,
            per_message_override=per_message_override,
            session_active_model=None,
            workspace_default_model=None,
            global_default_model=self.global_default_model,
            user_message_text=user_message_text,
            workspace_path=workspace_path,
        )

    # ---- Event emitters ----------------------------------------------

    def _emit_llm_call_started(
        self,
        *,
        session_id: str,
        turn_id: str,
        model: str,
        provider: str,
        request_id: str,
        estimated_tokens: int,
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
        )
        self.bus.emit(event)
        return event.id

    def _emit_llm_call_completed(
        self,
        *,
        session_id: str,
        turn_id: str,
        model: str,
        provider: str,
        response: CanonicalResponse,
        cost: Decimal,
        parent_event_id: str,
        identity: Identity,
    ) -> None:
        from metis_core.canonical.content import ThinkingBlock, ToolUseBlock

        produced_tool_calls = sum(1 for b in response.content if isinstance(b, ToolUseBlock))
        produced_thinking = sum(1 for b in response.content if isinstance(b, ThinkingBlock))
        event = make_event(
            type="llm.call_completed",
            session_id=session_id,
            turn_id=turn_id,
            actor=Actor.AGENT,
            payload=LLMCallCompleted(
                model=model,
                provider=provider,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                cached_input_tokens=response.usage.cached_input_tokens,
                cache_creation_input_tokens=response.usage.cache_creation_input_tokens,
                cost_usd=float(cost),
                pricing_version=self.pricing.version,
                latency_ms=response.latency_ms,
                stop_reason=_stop_reason_to_catalog(response.stop_reason),  # type: ignore[arg-type]
                produced_tool_calls=produced_tool_calls,
                produced_thinking_blocks=produced_thinking,
                gateway_key_id=identity.gateway_key_id,
                inbound_shape=self.inbound_shape,  # type: ignore[arg-type]
                user_id=identity.user_id,
                team_id=identity.team_id,
            ),
            timestamp=_now(),
            parent_event_id=parent_event_id,
        )
        self.bus.emit(event)

    def _emit_llm_call_failed(
        self,
        *,
        session_id: str,
        turn_id: str,
        model: str,
        provider: str,
        exc: AdapterError,
        parent_event_id: str,
    ) -> None:
        self.bus.emit(
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
        stop_reason: str,
        llm_calls: int,
        input_tokens: int,
        output_tokens: int,
        cost: Decimal,
        wall_time: float,
        identity: Identity,
    ) -> None:
        if stop_reason not in ("end_turn", "max_tokens", "stop_sequence", "tool_use"):
            stop_reason = "end_turn"
        event = make_event(
            type="turn.completed",
            session_id=session_id,
            turn_id=turn_id,
            actor=Actor.AGENT,
            payload=TurnCompleted(
                stop_reason=stop_reason,  # type: ignore[arg-type]
                llm_call_count=llm_calls,
                tool_call_count=0,
                total_input_tokens=input_tokens,
                total_output_tokens=output_tokens,
                total_cost_usd=float(cost),
                wall_time_seconds=wall_time,
                user_id=identity.user_id,
                team_id=identity.team_id,
            ),
            timestamp=_now(),
        )
        # `gateway_key_id` / `inbound_shape` remain dict-envelope stamps on
        # `TurnCompleted` until the typed extension lands (gateway.md §11
        # follow-on). The identity fields above are typed because the spec
        # (multi-user.md §4.4) and Agent 8a-4 landed them as typed catalog
        # fields when `LLMCallCompleted` got the same dimensions.
        event.payload["gateway_key_id"] = identity.gateway_key_id
        event.payload["inbound_shape"] = self.inbound_shape
        self.bus.emit(event)


# ---------------------------------------------------------------------------
# Disconnect handling
# ---------------------------------------------------------------------------


@dataclass
class _DisconnectProbe:
    """Async callable that returns True once the client has disconnected.

    Wraps `starlette.Request.is_disconnected` so the harness has no Starlette
    dependency.
    """

    probe: object = field(repr=False)

    async def __call__(self) -> bool:
        result = await self.probe()  # type: ignore[operator]
        return bool(result)


def make_disconnect_probe(coro_factory) -> _DisconnectProbe:
    """Build a probe from any zero-arg async callable returning bool."""
    return _DisconnectProbe(probe=coro_factory)


async def _watch_disconnect(probe: _DisconnectProbe, *, interval: float = 0.1) -> None:
    while True:
        if await probe():
            return
        await asyncio.sleep(interval)


async def _await_with_disconnect_guard(
    adapter_task: asyncio.Task,
    *,
    disconnect_task: asyncio.Task | None,
    adapter,
    request_id: str,
) -> CanonicalResponse:
    if disconnect_task is None:
        return await adapter_task
    done, _pending = await asyncio.wait(
        {adapter_task, disconnect_task},
        return_when=asyncio.FIRST_COMPLETED,
    )
    if adapter_task in done:
        return adapter_task.result()
    # Disconnect won the race. Signal the adapter (provider-specific cancel)
    # and force the task down so we don't leak it.
    await adapter.cancel(request_id)
    adapter_task.cancel()
    try:
        await adapter_task
    except AdapterError:
        raise
    except asyncio.CancelledError:
        raise CancelledError("client disconnected", request_id=request_id) from None
    raise CancelledError("client disconnected", request_id=request_id)


_END_OF_STREAM = object()


async def _stream_with_disconnect_guard(
    stream: AsyncIterator[StreamingEvent],
    *,
    is_disconnected: _DisconnectProbe | None,
    adapter,
    request_id: str,
) -> AsyncIterator[StreamingEvent]:
    """Iterate `stream`, racing each `__anext__` against the disconnect probe.

    On disconnect: cancel the adapter (provider-side cancel) and the pending
    `__anext__` task, then raise `CancelledError` so the harness's exception
    handler can emit `llm.call_failed` + `turn.completed` and surface
    `ClientDisconnected` to the HTTP layer."""
    if is_disconnected is None:
        async for event in stream:
            yield event
        return

    aiter_obj = stream.__aiter__()
    while True:
        next_task = asyncio.create_task(_safe_anext(aiter_obj))
        disc_task = asyncio.create_task(_watch_disconnect(is_disconnected))
        try:
            done, _pending = await asyncio.wait(
                {next_task, disc_task}, return_when=asyncio.FIRST_COMPLETED
            )
        except BaseException:
            next_task.cancel()
            disc_task.cancel()
            raise
        if next_task in done:
            disc_task.cancel()
            result = next_task.result()
            if result is _END_OF_STREAM:
                return
            yield result  # type: ignore[misc]
        else:
            next_task.cancel()
            await adapter.cancel(request_id)
            try:
                await next_task
            except (asyncio.CancelledError, Exception):
                pass
            raise CancelledError("client disconnected", request_id=request_id)


async def _safe_anext(aiter):
    try:
        return await aiter.__anext__()
    except StopAsyncIteration:
        return _END_OF_STREAM


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


def _stop_reason_to_catalog(reason: StopReason) -> str:
    value = reason.value
    if value in ("end_turn", "max_tokens", "stop_sequence", "tool_use"):
        return value
    return "end_turn"


def _heuristic_token_estimate(messages: list[Message], system_prompt: str | None) -> int:
    chars = len(system_prompt or "")
    for m in messages:
        for block in m.content:
            chars += len(getattr(block, "text", "") or "")
    return max(1, chars // 4)


def _zero_usage():
    from metis_core.adapters.protocol import TokenUsage

    return TokenUsage(input_tokens=0, output_tokens=0)
