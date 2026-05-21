"""Delegation async/concurrent tests (delegation.md §6.4 / §6.5 — Wave 17).

Covers the lift of delegation from synchronous-only (v1 MVP) to
async/concurrent:

- concurrent fan-out: N `delegate()` calls in one planner turn run the worker
  turn-loops concurrently — wall-clock is `max(workers)`, not `sum(workers)`
- the `max_concurrent_workers` fan-out cap serializes excess workers
- the cancellation cascade: a planner cancel propagates into every in-flight
  worker session
- per-worker wall-clock timeout: a hung worker fires `delegate.failed` with
  `failure_mode="timeout"` and does not tear down the planner

Recursive delegation and streaming worker output stay deferred — see
`test_delegation.py::test_worker_cannot_delegate_recursively` and
delegation.md §3.6.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from metis.core.adapters.protocol import StopReason
from metis.core.canonical.content import TextBlock, ToolUseBlock
from metis.core.events.bus import EventBus, EventFilter, Subscription
from metis.core.events.envelope import Event
from metis.core.pricing import DEFAULT_PRICE_TABLE
from metis.core.routing import ModelRegistry, RoutingEngine
from metis.core.sessions import InMemorySessionStore, SessionManager
from metis.core.tools.builtins import register_builtins
from metis.core.tools.dispatcher import ToolDispatcher

from tests_shared.scripted_adapter import _ScriptedAnthropicAdapter, _ScriptedResponse

PLANNER = "anthropic:claude-sonnet-4-6"
WORKER = "anthropic:claude-haiku-4-5"


@pytest.fixture
async def bus() -> EventBus:
    bus = EventBus()
    bus.start()
    return bus


@pytest.fixture
async def event_log(bus: EventBus) -> list[Event]:
    log: list[Event] = []

    async def handler(e: Event) -> None:
        if e.type.startswith("bus."):
            return
        log.append(e)

    bus.subscribe(Subscription(filter=EventFilter(), handler=handler, name="log", fast_path=True))
    return log


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return tmp_path


def _build_manager(
    bus: EventBus,
    adapter: _ScriptedAnthropicAdapter,
    *,
    max_concurrent_workers: int = 4,
    worker_timeout_seconds: float = 300.0,
) -> SessionManager:
    registry = ModelRegistry()
    registry.register(
        model_id=PLANNER,
        adapter=adapter,
        aliases=["sonnet"],
        can_delegate=True,
        delegation_tier="balanced",
    )
    registry.register(
        model_id=WORKER,
        adapter=adapter,
        aliases=["haiku"],
        can_delegate=False,
        delegation_tier="fast",
    )
    routing = RoutingEngine(registry=registry, bus=bus)
    dispatcher = ToolDispatcher(bus)
    register_builtins(dispatcher)
    return SessionManager(
        registry=registry,
        routing=routing,
        dispatcher=dispatcher,
        bus=bus,
        store=InMemorySessionStore(),
        pricing=DEFAULT_PRICE_TABLE,
        workspace_default_model=PLANNER,
        max_concurrent_workers=max_concurrent_workers,
        worker_timeout_seconds=worker_timeout_seconds,
    )


def _delegate_call(idx: int, task: str = "do work") -> ToolUseBlock:
    return ToolUseBlock(
        id=f"tu_delegate_{idx}",
        name="delegate",
        input={"tier": "fast", "task": task},
    )


def _worker_response(delay: float = 0.0) -> _ScriptedResponse:
    return _ScriptedResponse(
        content=[TextBlock(text="worker done")],
        stop_reason=StopReason.END_TURN,
        delay_seconds=delay,
    )


async def _wait_until(predicate, *, timeout: float = 3.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met within timeout")


# --------------------------------------------------------------------------
# 1. Concurrent fan-out — wall-clock is max(workers), not sum(workers).
# --------------------------------------------------------------------------


async def test_three_concurrent_workers_wall_clock_is_max_not_sum(bus, event_log, workspace):
    """delegation.md §6.5: a planner that fans out 3 `delegate()` calls runs
    the worker turn-loops concurrently. Each worker sleeps 0.3s; concurrent
    execution finishes in ~0.3s, serial execution would take ~0.9s."""
    worker_delay = 0.3
    adapter = _ScriptedAnthropicAdapter(
        [
            # Planner: fan out three delegate() calls in one assistant message.
            _ScriptedResponse(
                content=[_delegate_call(1), _delegate_call(2), _delegate_call(3)],
                stop_reason=StopReason.TOOL_USE,
            ),
            _worker_response(worker_delay),
            _worker_response(worker_delay),
            _worker_response(worker_delay),
            # Planner: integrate and end.
            _ScriptedResponse(
                content=[TextBlock(text="all workers done")],
                stop_reason=StopReason.END_TURN,
            ),
        ]
    )
    manager = _build_manager(bus, adapter)
    session = manager.create_session(workspace_path=str(workspace), active_model=PLANNER)

    t0 = time.monotonic()
    result = await manager.submit_turn(session.id, "fan out three sub-tasks")
    elapsed = time.monotonic() - t0
    await bus.drain()
    await bus.stop()

    assert result.stop_reason == StopReason.END_TURN
    assert result.tool_call_count == 3
    # Concurrent: ~max(0.3) not sum(0.9). Generous ceiling for CI scheduling.
    assert elapsed < 0.7, f"workers did not overlap: {elapsed:.3f}s for 3x{worker_delay}s"

    types = [e.type for e in event_log]
    assert types.count("delegate.started") == 3
    assert types.count("delegate.completed") == 3
    assert types.count("delegate.failed") == 0
    # 1 planner turn + 3 worker turns, all completed.
    assert types.count("turn.completed") == 4
    completed = [e for e in event_log if e.type == "delegate.completed"]
    assert all(e.payload["success"] is True for e in completed)
    # Each worker has its own distinct session.
    worker_ids = {e.payload["worker_session_id"] for e in completed}
    assert len(worker_ids) == 3


# --------------------------------------------------------------------------
# 2. The fan-out cap serializes excess workers.
# --------------------------------------------------------------------------


async def test_concurrency_cap_serializes_excess_workers(bus, event_log, workspace):
    """delegation.md §6.5: with `max_concurrent_workers=2`, fanning out 4
    workers (each 0.3s) runs in 2 batches — ~0.6s, between fully-concurrent
    (~0.3s) and fully-serial (~1.2s)."""
    worker_delay = 0.3
    adapter = _ScriptedAnthropicAdapter(
        [
            _ScriptedResponse(
                content=[_delegate_call(i) for i in range(1, 5)],
                stop_reason=StopReason.TOOL_USE,
            ),
            _worker_response(worker_delay),
            _worker_response(worker_delay),
            _worker_response(worker_delay),
            _worker_response(worker_delay),
            _ScriptedResponse(content=[TextBlock(text="done")], stop_reason=StopReason.END_TURN),
        ]
    )
    manager = _build_manager(bus, adapter, max_concurrent_workers=2)
    session = manager.create_session(workspace_path=str(workspace), active_model=PLANNER)

    t0 = time.monotonic()
    result = await manager.submit_turn(session.id, "fan out four sub-tasks")
    elapsed = time.monotonic() - t0
    await bus.drain()
    await bus.stop()

    assert result.tool_call_count == 4
    # 2 batches of 2: faster than serial (1.2s), slower than full concurrency.
    assert 0.5 <= elapsed < 1.0, f"cap not enforced as 2 batches: {elapsed:.3f}s"

    types = [e.type for e in event_log]
    assert types.count("delegate.completed") == 4


# --------------------------------------------------------------------------
# 3. Cancellation cascade — planner cancel reaches every in-flight worker.
# --------------------------------------------------------------------------


async def test_planner_cancel_cascades_to_in_flight_workers(bus, event_log, workspace):
    """delegation.md §6.4: cancelling a planner turn cascades into every
    in-flight worker session. Each worker emits its own `turn.cancelled`
    before the planner's `turn.cancelled` follows."""
    adapter = _ScriptedAnthropicAdapter(
        [
            _ScriptedResponse(
                content=[_delegate_call(1), _delegate_call(2), _delegate_call(3)],
                stop_reason=StopReason.TOOL_USE,
            ),
            # Workers sleep long enough to be cancelled mid-flight.
            _worker_response(delay=5.0),
            _worker_response(delay=5.0),
            _worker_response(delay=5.0),
            _ScriptedResponse(
                content=[TextBlock(text="never reached")], stop_reason=StopReason.END_TURN
            ),
        ]
    )
    manager = _build_manager(bus, adapter)
    session = manager.create_session(workspace_path=str(workspace), active_model=PLANNER)

    planner_task = asyncio.create_task(manager.submit_turn(session.id, "fan out then cancel"))
    # Wait until all three workers are registered in-flight, then cancel.
    await _wait_until(lambda: len(manager._worker_tasks.get(session.id, {})) == 3)
    planner_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await planner_task

    await asyncio.sleep(0.05)  # let orphaned dispatch tasks settle
    await bus.drain()
    await bus.stop()

    types = [e.type for e in event_log]
    # The planner turn never completed — no work integrated.
    assert types.count("turn.completed") == 0
    cancelled = [e for e in event_log if e.type == "turn.cancelled"]
    worker_cancelled = [e for e in cancelled if e.session_id != session.id]
    planner_cancelled = [e for e in cancelled if e.session_id == session.id]
    assert len(worker_cancelled) == 3, "cascade did not reach all in-flight workers"
    assert len(planner_cancelled) == 1
    # No worker ran to completion.
    assert types.count("delegate.completed") == 0
    # The delegate() tool body still reports each cancelled delegation.
    failed = [e for e in event_log if e.type == "delegate.failed"]
    assert len(failed) == 3
    assert all(e.payload["failure_mode"] == "cancelled_by_user" for e in failed)
    # Worker book-keeping was torn down — nothing leaks past the turn.
    assert manager._worker_tasks.get(session.id) is None


# --------------------------------------------------------------------------
# 4. Per-worker timeout — hung worker fires delegate.failed(timeout).
# --------------------------------------------------------------------------


async def test_worker_timeout_emits_delegate_failed_timeout(bus, event_log, workspace):
    """delegation.md §6.5: a worker exceeding `worker_timeout_seconds` is
    cancelled and surfaces as `delegate.failed` with `failure_mode="timeout"`.
    The planner is NOT torn down — it integrates the failed delegation and
    completes its own turn."""
    adapter = _ScriptedAnthropicAdapter(
        [
            _ScriptedResponse(
                content=[_delegate_call(1, task="a task that hangs")],
                stop_reason=StopReason.TOOL_USE,
            ),
            # Worker hangs far past the 0.2s budget.
            _worker_response(delay=5.0),
            # Planner survives and ends its turn.
            _ScriptedResponse(
                content=[TextBlock(text="worker timed out, carrying on")],
                stop_reason=StopReason.END_TURN,
            ),
        ]
    )
    manager = _build_manager(bus, adapter, worker_timeout_seconds=0.2)
    session = manager.create_session(workspace_path=str(workspace), active_model=PLANNER)

    t0 = time.monotonic()
    result = await manager.submit_turn(session.id, "delegate a hung task")
    elapsed = time.monotonic() - t0
    await bus.drain()
    await bus.stop()

    # The planner completed despite the worker timing out.
    assert result.stop_reason == StopReason.END_TURN
    # Bounded by the timeout, not the worker's 5s hang.
    assert elapsed < 2.0, f"timeout did not bound the worker: {elapsed:.3f}s"

    failed = [e for e in event_log if e.type == "delegate.failed"]
    assert len(failed) == 1
    assert failed[0].payload["failure_mode"] == "timeout"
    assert failed[0].payload["error_message"].startswith("timeout")
    assert [e for e in event_log if e.type == "delegate.completed"] == []

    # The planner's own turn.completed fired; the worker's turn.cancelled
    # records `timeout`, not `user_cancel`.
    planner_completed = [
        e for e in event_log if e.type == "turn.completed" and e.session_id == session.id
    ]
    assert len(planner_completed) == 1
    worker_cancelled = [
        e for e in event_log if e.type == "turn.cancelled" and e.session_id != session.id
    ]
    assert len(worker_cancelled) == 1
    assert worker_cancelled[0].payload["reason"] == "timeout"


async def test_worker_timeout_config_validation():
    """`worker_timeout_seconds` and `max_concurrent_workers` are validated."""
    bus = EventBus()
    registry = ModelRegistry()
    routing = RoutingEngine(registry=registry, bus=bus)
    dispatcher = ToolDispatcher(bus)
    kwargs = dict(
        registry=registry,
        routing=routing,
        dispatcher=dispatcher,
        bus=bus,
        store=InMemorySessionStore(),
        pricing=DEFAULT_PRICE_TABLE,
    )
    with pytest.raises(ValueError, match="max_concurrent_workers"):
        SessionManager(**kwargs, max_concurrent_workers=0)
    with pytest.raises(ValueError, match="worker_timeout_seconds"):
        SessionManager(**kwargs, worker_timeout_seconds=0)


# --------------------------------------------------------------------------
# 5. The async machinery is transparent to a single synchronous-style call.
# --------------------------------------------------------------------------


async def test_single_worker_unaffected_by_async_machinery(bus, event_log, workspace):
    """A single `delegate()` call (the v1 MVP shape) still completes cleanly
    through the task/semaphore/timeout wrapper."""
    adapter = _ScriptedAnthropicAdapter(
        [
            _ScriptedResponse(
                content=[_delegate_call(1, task="summarize")],
                stop_reason=StopReason.TOOL_USE,
            ),
            _worker_response(),
            _ScriptedResponse(content=[TextBlock(text="done")], stop_reason=StopReason.END_TURN),
        ]
    )
    manager = _build_manager(bus, adapter)
    session = manager.create_session(workspace_path=str(workspace), active_model=PLANNER)

    result = await manager.submit_turn(session.id, "delegate one task")
    await bus.drain()
    await bus.stop()

    assert result.stop_reason == StopReason.END_TURN
    assert result.tool_call_count == 1
    completed = [e for e in event_log if e.type == "delegate.completed"]
    assert len(completed) == 1
    assert completed[0].payload["success"] is True
    # No leaked worker book-keeping.
    assert manager._worker_tasks == {}
    assert manager._worker_cancel_reasons == {}
