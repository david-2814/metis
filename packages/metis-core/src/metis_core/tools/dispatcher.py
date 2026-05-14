"""Tool dispatcher: registry, validation, dispatch flow, event emission.

See tool-dispatcher.md §3, §4. Every tool_use block from the agent goes through
dispatch(), which: looks up the tool, validates input, applies confirmation
policy, executes under timeout, emits the appropriate tool.* events, and
returns a canonical ToolResultBlock.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime

import jsonschema
import msgspec

from metis_core.canonical.content import TextBlock, ToolResultBlock, ToolUseBlock
from metis_core.canonical.ids import next_monotonic_ulid
from metis_core.canonical.tools import (
    SideEffects,
    ToolDefinition,
    ToolSchemaError,
    validate_tool_input_schema,
)
from metis_core.events.bus import EventBus
from metis_core.events.envelope import Actor
from metis_core.events.payloads import (
    ToolCalled,
    ToolCompleted,
    ToolConfirmationRequested,
    ToolConfirmationResolved,
    ToolFailed,
    ToolInputInvalid,
    make_event,
)
from metis_core.tools.confirmation import (
    DEFAULT_POLICY,
    AutoAllowHandler,
    ConfirmationDecision,
    ConfirmationHandler,
    ConfirmationMode,
    ConfirmationPolicy,
    ConfirmationRequest,
)
from metis_core.tools.errors import (
    ConfirmationTimeoutError,
    ToolCancelledError,
    ToolError,
    ToolExecutionError,
    ToolNotFoundError,
    ToolPermissionDeniedError,
    ToolRegistrationError,
    ToolTimeoutError,
    ToolUserDeniedError,
    ToolValidationError,
)
from metis_core.tools.protocol import Tool, ToolContext, ToolFactory, ToolOutput
from metis_core.tools.workspace import WorkspaceEscapeError, WorkspaceFileAPI

logger = logging.getLogger(__name__)


# Default timeouts in seconds (tool-dispatcher.md §4).
_TIMEOUT_DEFAULTS: dict[SideEffects, float] = {
    SideEffects.NONE: 60.0,
    SideEffects.READ: 60.0,
    SideEffects.WRITE: 60.0,
    SideEffects.EXECUTE: 600.0,
    SideEffects.NETWORK: 600.0,
}

# Per-session concurrency cap (tool-dispatcher.md §4.1). Excess dispatches queue
# in arrival order behind an asyncio.Semaphore keyed by session_id.
_DEFAULT_CONCURRENCY_CAP_PER_SESSION = 4


@dataclass
class _Registered:
    factory: ToolFactory
    definition: ToolDefinition
    validator: jsonschema.Draft7Validator


@dataclass
class _InFlight:
    tool: Tool
    context: ToolContext


class ToolDispatcher:
    """Registry + execution wrapper for tools."""

    def __init__(
        self,
        bus: EventBus,
        *,
        confirmation_policy: ConfirmationPolicy = DEFAULT_POLICY,
        confirmation_handler: ConfirmationHandler | None = None,
        confirmation_timeout_seconds: float = 300.0,
        timeouts: dict[SideEffects, float] | None = None,
        concurrency_cap_per_session: int = _DEFAULT_CONCURRENCY_CAP_PER_SESSION,
    ) -> None:
        if concurrency_cap_per_session < 1:
            raise ValueError("concurrency_cap_per_session must be >= 1")
        self._bus = bus
        self._confirmation_policy = confirmation_policy
        self._confirmation_handler: ConfirmationHandler = confirmation_handler or AutoAllowHandler()
        self._confirmation_timeout = confirmation_timeout_seconds
        self._timeouts = {**_TIMEOUT_DEFAULTS, **(timeouts or {})}
        self._concurrency_cap = concurrency_cap_per_session
        self._registry: dict[str, _Registered] = {}
        # Per-session in-flight registry for cancel_session_tools.
        self._in_flight: dict[str, list[_InFlight]] = {}
        # Per-session semaphore enforcing the concurrency cap (§4.1). Created
        # lazily on first dispatch for the session so test sessions that never
        # dispatch leave no state behind.
        self._session_semaphores: dict[str, asyncio.Semaphore] = {}

    # ---- Registry ------------------------------------------------------

    def register(self, factory: ToolFactory) -> None:
        # Build one instance to read the definition; further dispatches will
        # use the factory to produce fresh instances per call.
        sample = factory()
        definition = sample.definition
        if definition.name in self._registry:
            raise ToolRegistrationError(definition.name, "tool name already registered")
        try:
            validate_tool_input_schema(definition.input_schema)
        except ToolSchemaError as exc:
            raise ToolRegistrationError(
                definition.name, f"input_schema violates canonical subset: {exc}"
            ) from exc
        try:
            jsonschema.Draft7Validator.check_schema(definition.input_schema)
        except jsonschema.SchemaError as exc:
            raise ToolRegistrationError(definition.name, f"invalid input_schema: {exc}") from exc
        validator = jsonschema.Draft7Validator(definition.input_schema)
        self._registry[definition.name] = _Registered(factory, definition, validator)

    def unregister(self, tool_name: str) -> None:
        self._registry.pop(tool_name, None)

    @property
    def confirmation_handler(self) -> ConfirmationHandler:
        return self._confirmation_handler

    def set_confirmation_handler(self, handler: ConfirmationHandler) -> None:
        """Swap the confirmation handler. The HTTP/WS server uses this to
        install a remote handler that awaits a REST response per
        server-api.md §4.2."""
        self._confirmation_handler = handler

    def get_definitions(self) -> list[ToolDefinition]:
        return [r.definition for r in self._registry.values()]

    # ---- Dispatch ------------------------------------------------------

    async def dispatch(
        self,
        tool_use: ToolUseBlock,
        *,
        session_id: str,
        turn_id: str,
        workspace_path: str,
        parent_event_id: str | None = None,
        memory: object | None = None,
        skills: object | None = None,
    ) -> ToolResultBlock:
        """Run a tool_use end-to-end. Always returns a ToolResultBlock; never
        raises for tool failures (errors come back as is_error result blocks).
        """
        # Step 1: lookup
        registered = self._registry.get(tool_use.name)
        if registered is None:
            error = ToolNotFoundError(
                f"tool {tool_use.name!r} not registered", tool_use_id=tool_use.id
            )
            self._emit_tool_failed(
                tool_use,
                error,
                latency_ms=0,
                session_id=session_id,
                turn_id=turn_id,
                parent_event_id=parent_event_id,
            )
            return _error_result(tool_use, error)

        # Step 2: schema validation
        errors = sorted(registered.validator.iter_errors(tool_use.input), key=lambda e: e.path)
        if errors:
            messages = [_format_validation_error(e) for e in errors]
            self._bus.emit(
                make_event(
                    type="tool.input_invalid",
                    session_id=session_id,
                    turn_id=turn_id,
                    actor=Actor.SYSTEM,
                    payload=ToolInputInvalid(tool_name=tool_use.name, validation_errors=messages),
                    timestamp=_now(),
                    parent_event_id=parent_event_id,
                )
            )
            err = ToolValidationError(
                "input failed schema validation",
                tool_use_id=tool_use.id,
                validation_errors=messages,
            )
            return _error_result(tool_use, err, body="\n".join(messages))

        definition = registered.definition

        # Step 3: workspace scope pre-check (tool-dispatcher.md §9.2).
        # Escape rejection must emit tool.failed *without* a preceding
        # tool.called, so the check has to happen before step 5.
        workspace = WorkspaceFileAPI(workspace_path) if definition.requires_workspace else None
        if workspace is not None:
            try:
                _precheck_workspace_paths(tool_use.input, workspace)
            except WorkspaceEscapeError as exc:
                err = ToolPermissionDeniedError(str(exc), tool_use_id=tool_use.id)
                self._emit_tool_failed(
                    tool_use,
                    err,
                    latency_ms=0,
                    session_id=session_id,
                    turn_id=turn_id,
                    parent_event_id=parent_event_id,
                )
                return _error_result(tool_use, err)

        # Step 4: confirmation
        mode = self._confirmation_policy.mode_for(definition.name, definition.side_effects)
        called_event_id: str | None = None
        if mode == ConfirmationMode.DENY:
            err = ToolUserDeniedError(
                f"tool {definition.name!r} denied by policy", tool_use_id=tool_use.id
            )
            self._emit_tool_failed(
                tool_use,
                err,
                latency_ms=0,
                session_id=session_id,
                turn_id=turn_id,
                parent_event_id=parent_event_id,
            )
            return _error_result(tool_use, err)
        elif mode == ConfirmationMode.PROMPT:
            decision = await self._run_confirmation(
                tool_use=tool_use,
                definition=definition,
                session_id=session_id,
                turn_id=turn_id,
                parent_event_id=parent_event_id,
            )
            if decision != ConfirmationDecision.ALLOW:
                err_cls = (
                    ConfirmationTimeoutError
                    if decision == ConfirmationDecision.TIMEOUT
                    else ToolUserDeniedError
                )
                err = err_cls(
                    f"confirmation {decision.value} for {definition.name!r}",
                    tool_use_id=tool_use.id,
                )
                self._emit_tool_failed(
                    tool_use,
                    err,
                    latency_ms=0,
                    session_id=session_id,
                    turn_id=turn_id,
                    parent_event_id=parent_event_id,
                )
                return _error_result(tool_use, err)

        # Step 5: emit tool.called (after escape + confirmation gates).
        called_event_id = self._emit_tool_called(
            tool_use,
            definition,
            session_id=session_id,
            turn_id=turn_id,
            parent_event_id=parent_event_id,
        )

        # Step 6: instantiate fresh tool, build context, execute under the
        # per-session concurrency cap (§4.1).
        tool = registered.factory()
        context = ToolContext(
            session_id=session_id,
            turn_id=turn_id,
            tool_use_id=tool_use.id,
            workspace_path=workspace_path,
            workspace_files=workspace,  # type: ignore[arg-type]
            memory=memory,
            skills=skills,
            bus=self._bus,
        )
        in_flight = _InFlight(tool=tool, context=context)
        self._in_flight.setdefault(session_id, []).append(in_flight)

        timeout = self._timeouts[definition.side_effects]
        semaphore = self._semaphore_for(session_id)
        start = time.monotonic()
        try:
            async with semaphore:
                output = await asyncio.wait_for(tool.execute(tool_use.input, context), timeout)
        except TimeoutError:
            err = ToolTimeoutError(
                f"tool {definition.name!r} exceeded {timeout}s timeout",
                tool_use_id=tool_use.id,
            )
            await _safe_cancel(tool)
            self._emit_tool_failed(
                tool_use,
                err,
                latency_ms=_elapsed_ms(start),
                session_id=session_id,
                turn_id=turn_id,
                parent_event_id=called_event_id,
            )
            self._remove_in_flight(session_id, in_flight)
            return _error_result(tool_use, err)
        except ToolError as err:
            err.tool_use_id = err.tool_use_id or tool_use.id
            self._emit_tool_failed(
                tool_use,
                err,
                latency_ms=_elapsed_ms(start),
                session_id=session_id,
                turn_id=turn_id,
                parent_event_id=called_event_id,
            )
            self._remove_in_flight(session_id, in_flight)
            return _error_result(tool_use, err)
        except asyncio.CancelledError:
            err = ToolCancelledError(f"tool {definition.name!r} cancelled", tool_use_id=tool_use.id)
            self._emit_tool_failed(
                tool_use,
                err,
                latency_ms=_elapsed_ms(start),
                session_id=session_id,
                turn_id=turn_id,
                parent_event_id=called_event_id,
            )
            self._remove_in_flight(session_id, in_flight)
            raise
        except Exception as exc:
            logger.exception("tool %s raised", definition.name)
            err = ToolExecutionError(
                f"tool {definition.name!r} raised: {exc}",
                tool_use_id=tool_use.id,
                underlying=exc,
            )
            self._emit_tool_failed(
                tool_use,
                err,
                latency_ms=_elapsed_ms(start),
                session_id=session_id,
                turn_id=turn_id,
                parent_event_id=called_event_id,
            )
            self._remove_in_flight(session_id, in_flight)
            return _error_result(tool_use, err)
        else:
            self._emit_tool_completed(
                tool_use,
                output,
                latency_ms=_elapsed_ms(start),
                session_id=session_id,
                turn_id=turn_id,
                parent_event_id=called_event_id,
            )
            self._remove_in_flight(session_id, in_flight)
            return ToolResultBlock(
                tool_use_id=tool_use.id,
                content=output.content,
                is_error=not output.success,
            )

    async def cancel_session_tools(self, session_id: str) -> None:
        """Signal cancel_event and call cancel() on every in-flight tool."""
        in_flight = self._in_flight.get(session_id, [])
        for entry in in_flight:
            entry.context.cancel_event.set()
            await _safe_cancel(entry.tool)

    def _semaphore_for(self, session_id: str) -> asyncio.Semaphore:
        sem = self._session_semaphores.get(session_id)
        if sem is None:
            sem = asyncio.Semaphore(self._concurrency_cap)
            self._session_semaphores[session_id] = sem
        return sem

    # ---- Event emission helpers ----------------------------------------

    def _emit_tool_called(
        self,
        tool_use: ToolUseBlock,
        definition: ToolDefinition,
        *,
        session_id: str,
        turn_id: str,
        parent_event_id: str | None,
    ) -> str:
        input_bytes = json.dumps(tool_use.input, sort_keys=True).encode()
        event = make_event(
            type="tool.called",
            session_id=session_id,
            turn_id=turn_id,
            actor=Actor.AGENT,
            payload=ToolCalled(
                tool_use_id=tool_use.id,
                tool_name=tool_use.name,
                input_hash=hashlib.sha256(input_bytes).hexdigest(),
                input_size_bytes=len(input_bytes),
                side_effects=definition.side_effects.value,  # type: ignore[arg-type]
            ),
            timestamp=_now(),
            parent_event_id=parent_event_id,
        )
        self._bus.emit(event)
        return event.id

    def _emit_tool_completed(
        self,
        tool_use: ToolUseBlock,
        output: ToolOutput,
        *,
        latency_ms: int,
        session_id: str,
        turn_id: str,
        parent_event_id: str | None,
    ) -> None:
        body = _serialized_size(output.content)
        self._bus.emit(
            make_event(
                type="tool.completed",
                session_id=session_id,
                turn_id=turn_id,
                actor=Actor.TOOL,
                payload=ToolCompleted(
                    tool_use_id=tool_use.id,
                    success=output.success,
                    output_size_bytes=body,
                    latency_ms=latency_ms,
                    files_modified=output.files_modified,
                    command_executed=output.command_executed,
                ),
                timestamp=_now(),
                parent_event_id=parent_event_id,
            )
        )

    def _emit_tool_failed(
        self,
        tool_use: ToolUseBlock,
        err: ToolError,
        *,
        latency_ms: int,
        session_id: str,
        turn_id: str,
        parent_event_id: str | None,
    ) -> None:
        self._bus.emit(
            make_event(
                type="tool.failed",
                session_id=session_id,
                turn_id=turn_id,
                actor=Actor.TOOL,
                payload=ToolFailed(
                    tool_use_id=tool_use.id,
                    error_class=err.error_class.value,  # type: ignore[arg-type]
                    error_message=err.message if err.is_user_visible else err.error_class.value,
                    latency_ms=latency_ms,
                ),
                timestamp=_now(),
                parent_event_id=parent_event_id,
            )
        )

    async def _run_confirmation(
        self,
        *,
        tool_use: ToolUseBlock,
        definition: ToolDefinition,
        session_id: str,
        turn_id: str,
        parent_event_id: str | None,
    ) -> ConfirmationDecision:
        request_id = str(next_monotonic_ulid())
        expires_at = datetime.fromtimestamp(time.time() + self._confirmation_timeout, tz=UTC)
        self._bus.emit(
            make_event(
                type="tool.confirmation_requested",
                session_id=session_id,
                turn_id=turn_id,
                actor=Actor.SYSTEM,
                payload=ToolConfirmationRequested(
                    tool_use_id=tool_use.id,
                    tool_name=definition.name,
                    side_effects=definition.side_effects.value,  # type: ignore[arg-type]
                    confirmation_request_id=request_id,
                    input_summary=_summarize(tool_use.input),
                    expires_at=expires_at,
                ),
                timestamp=_now(),
                parent_event_id=parent_event_id,
            )
        )
        req = ConfirmationRequest(
            tool_use_id=tool_use.id,
            tool_name=definition.name,
            side_effects=definition.side_effects,
            input_summary=_summarize(tool_use.input),
        )
        try:
            decision = await asyncio.wait_for(
                self._confirmation_handler.request(req), self._confirmation_timeout
            )
        except TimeoutError:
            decision = ConfirmationDecision.TIMEOUT
        self._bus.emit(
            make_event(
                type="tool.confirmation_resolved",
                session_id=session_id,
                turn_id=turn_id,
                actor=Actor.USER if decision != ConfirmationDecision.TIMEOUT else Actor.SYSTEM,
                payload=ToolConfirmationResolved(
                    tool_use_id=tool_use.id,
                    confirmation_request_id=request_id,
                    decision=decision.value,  # type: ignore[arg-type]
                    scope="once" if decision == ConfirmationDecision.ALLOW else None,
                ),
                timestamp=_now(),
                parent_event_id=parent_event_id,
            )
        )
        return decision

    def _remove_in_flight(self, session_id: str, entry: _InFlight) -> None:
        bucket = self._in_flight.get(session_id)
        if bucket is None:
            return
        try:
            bucket.remove(entry)
        except ValueError:
            pass
        if not bucket:
            self._in_flight.pop(session_id, None)


# ---- Helpers ---------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


def _elapsed_ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


def _error_result(
    tool_use: ToolUseBlock, err: ToolError, *, body: str | None = None
) -> ToolResultBlock:
    text = (
        body
        if body is not None
        else (err.message if err.is_user_visible else "Tool execution failed.")
    )
    return ToolResultBlock(
        tool_use_id=tool_use.id,
        content=[TextBlock(text=text)],
        is_error=True,
    )


def _serialized_size(content: list) -> int:
    return len(msgspec.json.encode(content))


def _summarize(input: dict, max_len: int = 200) -> str:
    serialized = json.dumps(input, sort_keys=True)
    if len(serialized) > max_len:
        return serialized[: max_len - 3] + "..."
    return serialized


def _format_validation_error(err: jsonschema.ValidationError) -> str:
    path = ".".join(str(p) for p in err.absolute_path) or "$"
    return f"{path}: {err.message}"


async def _safe_cancel(tool: Tool) -> None:
    try:
        await tool.cancel()
    except Exception:
        logger.exception("tool.cancel raised")


# Conventional input keys carrying a workspace path. Any tool with
# requires_workspace=True that uses these names has its paths pre-checked at
# dispatch time (before tool.called) so workspace-escape rejections do not emit
# an orphaned tool.called event (tool-dispatcher.md §9.2).
_PATH_INPUT_KEYS: frozenset[str] = frozenset({"path", "paths"})


def _precheck_workspace_paths(input: dict, workspace: WorkspaceFileAPI) -> None:
    """Resolve any path-like top-level input field through the workspace API.

    Raises WorkspaceEscapeError on the first escape attempt. Tools whose path
    arguments use non-conventional names still get caught at execute-time by
    the workspace API; the pre-check is the spec-required fast path for the
    common case.
    """
    for key in _PATH_INPUT_KEYS:
        if key not in input:
            continue
        value = input[key]
        if isinstance(value, str):
            workspace._resolve(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    workspace._resolve(item)


__all__ = ["ToolDispatcher"]
