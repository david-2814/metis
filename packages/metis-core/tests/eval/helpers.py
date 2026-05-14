"""Shared helpers for evaluator tests (importable, not auto-loaded).

Builds typed Event objects via `make_event` against the catalog so the
eval subscriber sees the same payloads the bus would deliver in
production.
"""

from __future__ import annotations

from datetime import UTC, datetime

from metis_core.canonical.ids import new_tool_use_id, next_monotonic_ulid
from metis_core.events.envelope import Actor, Event
from metis_core.events.payloads import (
    LLMCallCompleted,
    LLMCallFailed,
    SessionEnded,
    ToolCalled,
    ToolCompleted,
    ToolFailed,
    TurnCompleted,
    make_event,
)


def now() -> datetime:
    return datetime.now(UTC)


def new_turn_id() -> str:
    return str(next_monotonic_ulid())


def build_turn_completed(
    *,
    session_id: str,
    turn_id: str,
    stop_reason: str = "end_turn",
    tool_call_count: int = 0,
    llm_call_count: int = 1,
    signals_extra: dict | None = None,
) -> Event:
    return make_event(
        type="turn.completed",
        session_id=session_id,
        actor=Actor.AGENT,
        timestamp=now(),
        turn_id=turn_id,
        payload=TurnCompleted(
            stop_reason=stop_reason,  # type: ignore[arg-type]
            llm_call_count=llm_call_count,
            tool_call_count=tool_call_count,
            total_input_tokens=100,
            total_output_tokens=50,
            total_cost_usd=0.001,
            wall_time_seconds=1.5,
            signals_extra=signals_extra,
        ),
    )


def build_tool_called(
    *,
    session_id: str,
    turn_id: str,
    tool_use_id: str,
    tool_name: str,
    input_hash: str = "h",
) -> Event:
    return make_event(
        type="tool.called",
        session_id=session_id,
        actor=Actor.AGENT,
        timestamp=now(),
        turn_id=turn_id,
        payload=ToolCalled(
            tool_use_id=tool_use_id,
            tool_name=tool_name,
            input_hash=input_hash,
            input_size_bytes=42,
            side_effects="read",
        ),
    )


def build_tool_completed(
    *,
    session_id: str,
    turn_id: str,
    tool_use_id: str,
    success: bool = True,
) -> Event:
    return make_event(
        type="tool.completed",
        session_id=session_id,
        actor=Actor.TOOL,
        timestamp=now(),
        turn_id=turn_id,
        payload=ToolCompleted(
            tool_use_id=tool_use_id,
            success=success,
            output_size_bytes=128,
            latency_ms=10,
        ),
    )


def build_tool_failed(
    *,
    session_id: str,
    turn_id: str,
    tool_use_id: str,
    error_class: str = "execution_error",
) -> Event:
    return make_event(
        type="tool.failed",
        session_id=session_id,
        actor=Actor.TOOL,
        timestamp=now(),
        turn_id=turn_id,
        payload=ToolFailed(
            tool_use_id=tool_use_id,
            error_class=error_class,  # type: ignore[arg-type]
            error_message="boom",
            latency_ms=10,
        ),
    )


def build_llm_completed(
    *,
    session_id: str,
    turn_id: str,
    stop_reason: str = "end_turn",
    model: str = "anthropic:claude-haiku-4-5",
) -> Event:
    return make_event(
        type="llm.call_completed",
        session_id=session_id,
        actor=Actor.AGENT,
        timestamp=now(),
        turn_id=turn_id,
        payload=LLMCallCompleted(
            model=model,
            provider="anthropic",
            input_tokens=100,
            output_tokens=50,
            cached_input_tokens=0,
            cache_creation_input_tokens=0,
            cost_usd=0.001,
            pricing_version="test",
            latency_ms=500,
            stop_reason=stop_reason,  # type: ignore[arg-type]
            produced_tool_calls=0,
            produced_thinking_blocks=0,
        ),
    )


def build_llm_failed(
    *,
    session_id: str,
    turn_id: str,
    error_class: str = "server_error",
    model: str = "anthropic:claude-haiku-4-5",
) -> Event:
    return make_event(
        type="llm.call_failed",
        session_id=session_id,
        actor=Actor.AGENT,
        timestamp=now(),
        turn_id=turn_id,
        payload=LLMCallFailed(
            model=model,
            provider="anthropic",
            error_class=error_class,  # type: ignore[arg-type]
            error_message_redacted="upstream 502",
            retry_count=0,
            latency_ms=10,
        ),
    )


def build_session_ended(
    *,
    session_id: str,
    disposition: str = "completed",
    turn_count: int = 1,
) -> Event:
    return make_event(
        type="session.ended",
        session_id=session_id,
        actor=Actor.SYSTEM,
        timestamp=now(),
        payload=SessionEnded(
            disposition=disposition,  # type: ignore[arg-type]
            turn_count=turn_count,
            total_cost_usd=0.001,
            duration_seconds=5.0,
        ),
    )


__all__ = [
    "build_llm_completed",
    "build_llm_failed",
    "build_session_ended",
    "build_tool_called",
    "build_tool_completed",
    "build_tool_failed",
    "build_turn_completed",
    "new_tool_use_id",
    "new_turn_id",
    "now",
]
