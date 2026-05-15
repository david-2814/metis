"""Worker / delegation data shapes (delegation.md §4-6).

These are msgspec frozen structs and plain dataclasses for the in-process
contract between the `delegate()` tool body and the `SessionManager` that
owns worker-session lifecycle. v1 MVP shapes — fan-out, streaming, and
recursive delegation are deferred.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal, Protocol

import msgspec

DelegateTier = Literal["fast", "balanced", "deep"]
"""Coarse worker-model tiers per delegation.md §4.2. Resolved against the
model registry's `delegation_tier` field at spawn time."""

DelegateContextMode = Literal["minimal", "explicit"]
"""Whether the planner asked for the worker to receive only the task brief
(`minimal`) or an explicit list of references (`explicit`). v1 honors
`minimal` only; explicit references pass through unmodified as inline notes
in the worker's synthetic user message."""

DelegateFailureMode = Literal[
    "worker_error",
    "max_tokens_exceeded",
    "insufficient_context",
    "output_schema_validation_failed",
    "no_model_available_for_tier",
    "cancelled_by_user",
]


class ContextSpec(msgspec.Struct, frozen=True, kw_only=True):
    """`ContextSpec` for `delegate()` (delegation.md §6.3).

    `mode="minimal"`: the worker sees only the task brief.
    `mode="explicit"`: the worker additionally sees the inline strings in
    `include`. v1 does not resolve message-id or file-path references; the
    caller resolves them into strings before passing them in.
    """

    mode: DelegateContextMode = "minimal"
    include: tuple[str, ...] = ()


class WorkerSpec(msgspec.Struct, frozen=True, kw_only=True):
    """Resolved worker configuration after tier → model lookup.

    The session manager constructs one of these from a `DelegateRequest` +
    the model registry; tests can build one directly to drive a worker
    without going through the `delegate()` tool.
    """

    tier: DelegateTier
    resolved_model: str
    task: str
    context: ContextSpec
    allowed_tools: tuple[str, ...] | None = None
    max_tokens: int | None = None
    output_schema: dict | None = None


class DelegateRequest(msgspec.Struct, frozen=True, kw_only=True):
    """The planner's `delegate(...)` tool input, validated.

    Constructed by the `delegate()` tool body from raw tool-use input; passed
    to the session manager which resolves the tier, spawns a worker session,
    and runs it to completion.
    """

    parent_session_id: str
    parent_tool_use_id: str
    tier: DelegateTier
    task: str
    context: ContextSpec
    allowed_tools: tuple[str, ...] | None = None
    max_tokens: int | None = None
    output_schema: dict | None = None


class DelegateUsageSummary(msgspec.Struct, frozen=True, kw_only=True):
    """Per-delegation usage rollup carried on `delegate.completed`."""

    model: str
    turn_count: int
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal
    wall_time_seconds: float
    tool_call_count: int


class DelegateResult(msgspec.Struct, frozen=True, kw_only=True):
    """The planner-visible return value of `delegate()` (delegation.md §4.3).

    `output` is text by default; structured-output workers return a dict.
    `error` is `None` on success, otherwise carries a `DelegateFailureMode`-
    prefixed message ("worker_error: ..."). The full usage summary lives on
    the matching `delegate.completed` event so the planner isn't flooded.
    """

    success: bool
    output: str | dict
    error: str | None
    usage_summary: DelegateUsageSummary
    worker_session_id: str


@dataclass(frozen=True)
class DelegateOutcome:
    """Internal handoff between the session manager's spawn flow and the
    `delegate()` tool body. Carries everything the tool needs to emit the
    `delegate.completed` / `delegate.failed` event and build the planner's
    `DelegateResult`."""

    worker_session_id: str
    success: bool
    output: str | dict
    error: str | None
    failure_mode: DelegateFailureMode | None
    usage_summary: DelegateUsageSummary
    dropped_tools: tuple[str, ...] = field(default_factory=tuple)
    allowed_tool_count: int = 0
    task_size_tokens: int = 0


class WorkerSpawner(Protocol):
    """Protocol the `delegate()` tool consumes to ask the session manager to
    run a worker. Implemented by `SessionManager` (delegation.md §6.1).

    Implementations must:
      - resolve `tier` → model via the registry; on miss return a
        `no_model_available_for_tier` outcome and **do not** create a session
      - create a worker `Session` with parent_session_id / parent_tool_use_id
        / is_worker=True; same workspace as the parent
      - run the worker's turn loop synchronously to completion
      - emit `delegate.started` before the loop and let the tool body emit
        `delegate.completed` / `delegate.failed` from the returned outcome
    """

    async def spawn_worker(self, request: DelegateRequest) -> DelegateOutcome: ...


SyncWorkerSpawner = Callable[[DelegateRequest], Awaitable[DelegateOutcome]]
"""Lightweight alternative for tests: a plain async callable matching the
`WorkerSpawner.spawn_worker` signature. The `delegate()` tool accepts
either form via duck-typing on `worker_spawner`."""
