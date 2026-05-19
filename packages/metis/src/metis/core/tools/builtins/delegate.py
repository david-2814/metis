"""`delegate()` built-in tool — planner-LLM-driven worker spawn.

See `docs/specs/delegation.md` §4-6. The tool body validates input, hands a
`DelegateRequest` to `ToolContext.worker_spawner`, awaits the `DelegateOutcome`
synchronously, emits `delegate.completed` / `delegate.failed`, and returns the
worker's output to the planner as the tool result.

v1 MVP:
  - blocking (no streaming worker output back to the planner)
  - one worker per call (no fan-out cap; planner can emit multiple `delegate()`
    tool_uses to fan out via the existing tool dispatcher parallelism)
  - workers cannot delegate (recursive delegation is non-goal §2.2.1)
"""

from __future__ import annotations

from datetime import UTC, datetime

import msgspec

from metis.core.canonical.content import TextBlock
from metis.core.canonical.tools import SideEffects, ToolDefinition
from metis.core.events.envelope import Actor
from metis.core.events.payloads import (
    DelegateCompleted,
    DelegateFailed,
    make_event,
)
from metis.core.tools.errors import ToolExecutionError
from metis.core.tools.protocol import ToolContext, ToolOutput
from metis.core.workers.protocol import (
    ContextSpec,
    DelegateRequest,
    DelegateUsageSummary,
)

_TIER_VALUES = ("fast", "balanced", "deep")
_CONTEXT_MODE_VALUES = ("minimal", "explicit")


class DelegateTool:
    """Spawn a worker session on a (typically cheaper) tier model.

    Registered for planner sessions whose active model has
    `can_delegate=True` in the registry (delegation.md §3.1, §4.2). The
    session manager filters this tool out of `tool_definitions` for worker
    sessions (§5.6) so the worker LLM never sees it.
    """

    definition = ToolDefinition(
        name="delegate",
        description=(
            "Spawn a worker LLM on a cheaper tier to run a focused sub-task and "
            "return its output. Use for mechanical sub-tasks (formatting, "
            "rename, summarise, regex) where the planner's full reasoning is "
            "overkill. Blocks until the worker completes."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "tier": {
                    "type": "string",
                    "enum": list(_TIER_VALUES),
                    "description": "Worker model tier: fast / balanced / deep.",
                },
                "task": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Focused instruction for the worker.",
                },
                "context": {
                    "type": "object",
                    "properties": {
                        "mode": {
                            "type": "string",
                            "enum": list(_CONTEXT_MODE_VALUES),
                            "default": "minimal",
                        },
                        "include": {
                            "type": "array",
                            "items": {"type": "string"},
                            "default": [],
                        },
                    },
                    "additionalProperties": False,
                    "description": (
                        "Context spec. mode=minimal sends only the task brief; "
                        "mode=explicit additionally passes the strings in "
                        "`include` to the worker."
                    ),
                },
                "allowed_tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Subset of planner tools the worker may call.",
                },
                "max_tokens": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Cap on worker output tokens.",
                },
            },
            "required": ["tier", "task"],
            "additionalProperties": False,
        },
        side_effects=SideEffects.EXECUTE,
        requires_workspace=False,
    )

    async def cancel(self) -> bool:
        return True

    async def execute(self, input: dict, context: ToolContext) -> ToolOutput:
        if context.is_worker:
            raise ToolExecutionError(
                "workers cannot delegate (recursive delegation is a v1 non-goal)",
                tool_use_id=context.tool_use_id,
            )
        spawner = context.worker_spawner
        if spawner is None:
            raise ToolExecutionError(
                "delegate is not configured for this session (no worker spawner)",
                tool_use_id=context.tool_use_id,
            )

        raw_context = input.get("context") or {}
        context_spec = ContextSpec(
            mode=raw_context.get("mode", "minimal"),
            include=tuple(raw_context.get("include") or ()),
        )
        request = DelegateRequest(
            parent_session_id=context.session_id,
            parent_tool_use_id=context.tool_use_id,
            tier=input["tier"],
            task=input["task"],
            context=context_spec,
            allowed_tools=tuple(input["allowed_tools"]) if input.get("allowed_tools") else None,
            max_tokens=input.get("max_tokens"),
            output_schema=input.get("output_schema"),
        )

        outcome = await spawner.spawn_worker(request)

        bus = context.bus
        if bus is not None:
            payload_kwargs = {
                "tool_use_id": context.tool_use_id,
                "worker_session_id": outcome.worker_session_id,
            }
            if outcome.success:
                bus.emit(
                    make_event(
                        type="delegate.completed",
                        session_id=context.session_id,
                        turn_id=context.turn_id,
                        actor=Actor.SYSTEM,
                        payload=DelegateCompleted(
                            success=True,
                            output_size_bytes=_output_size(outcome.output),
                            worker_total_cost_usd=outcome.usage_summary.cost_usd,
                            pricing_version=getattr(spawner, "pricing_version", "") or "",
                            turn_count=outcome.usage_summary.turn_count,
                            llm_call_count=outcome.usage_summary.turn_count,
                            tool_call_count=outcome.usage_summary.tool_call_count,
                            wall_time_seconds=outcome.usage_summary.wall_time_seconds,
                            model=outcome.usage_summary.model,
                            **payload_kwargs,
                        ),
                        timestamp=datetime.now(UTC),
                    )
                )
            else:
                bus.emit(
                    make_event(
                        type="delegate.failed",
                        session_id=context.session_id,
                        turn_id=context.turn_id,
                        actor=Actor.SYSTEM,
                        payload=DelegateFailed(
                            failure_mode=outcome.failure_mode or "worker_error",
                            error_message=outcome.error or "",
                            worker_total_cost_usd=outcome.usage_summary.cost_usd,
                            pricing_version=getattr(spawner, "pricing_version", "") or "",
                            **payload_kwargs,
                        ),
                        timestamp=datetime.now(UTC),
                    )
                )

        if outcome.success:
            text = _serialize_output(outcome.output)
        else:
            text = outcome.error or "delegation failed"
        return ToolOutput(
            content=[TextBlock(text=text)],
            success=outcome.success,
            metadata={
                "worker_session_id": outcome.worker_session_id,
                "worker_total_cost_usd": str(outcome.usage_summary.cost_usd),
                "worker_model": outcome.usage_summary.model,
                "worker_turn_count": outcome.usage_summary.turn_count,
                "worker_tool_call_count": outcome.usage_summary.tool_call_count,
                "failure_mode": outcome.failure_mode or None,
                "dropped_tools": list(outcome.dropped_tools),
            },
        )


def _serialize_output(output: str | dict) -> str:
    if isinstance(output, dict):
        return msgspec.json.encode(output).decode()
    return output


def _output_size(output: str | dict) -> int:
    if isinstance(output, dict):
        return len(msgspec.json.encode(output))
    return len(output.encode("utf-8"))


def make_usage_summary(
    *,
    model: str,
    turn_count: int,
    input_tokens: int,
    output_tokens: int,
    cost_usd,
    wall_time_seconds: float,
    tool_call_count: int,
) -> DelegateUsageSummary:
    """Construct a DelegateUsageSummary. Provided as a stable helper so the
    session manager doesn't have to re-import the struct."""
    return DelegateUsageSummary(
        model=model,
        turn_count=turn_count,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
        wall_time_seconds=wall_time_seconds,
        tool_call_count=tool_call_count,
    )


__all__ = ["DelegateTool", "make_usage_summary"]
