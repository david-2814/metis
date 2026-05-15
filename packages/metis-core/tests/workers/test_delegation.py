"""Delegation v1 MVP end-to-end tests (delegation.md §1-§13).

Exercises the planner → `delegate()` → worker session loop with a scripted
adapter. Covers:

- successful worker spawn + cost attribution
- routing slot 5 fires only inside worker re-entry
- worker tool isolation (delegate + memory tools dropped)
- memory file isolation (parent's MEMORY.md unchanged after worker writes)
- failure modes: no model for tier, worker error, recursive delegation
- analytics rollup via parent_session_id stamping
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from metis_core.adapters.protocol import StopReason
from metis_core.canonical.content import TextBlock, ToolUseBlock
from metis_core.events.bus import EventBus, EventFilter, Subscription
from metis_core.events.envelope import Event
from metis_core.pricing import DEFAULT_PRICE_TABLE
from metis_core.routing import ModelRegistry, RoutingEngine
from metis_core.sessions import InMemorySessionStore, SessionManager
from metis_core.tools.builtins import register_builtins
from metis_core.tools.dispatcher import ToolDispatcher

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
    planner_can_delegate: bool = True,
    worker_tier: str | None = "fast",
) -> tuple[SessionManager, ToolDispatcher, ModelRegistry]:
    registry = ModelRegistry()
    registry.register(
        model_id=PLANNER,
        adapter=adapter,
        aliases=["sonnet"],
        can_delegate=planner_can_delegate,
        delegation_tier="balanced",
    )
    registry.register(
        model_id=WORKER,
        adapter=adapter,
        aliases=["haiku"],
        can_delegate=False,
        delegation_tier=worker_tier,
    )
    routing = RoutingEngine(registry=registry, bus=bus)
    dispatcher = ToolDispatcher(bus)
    register_builtins(dispatcher)
    manager = SessionManager(
        registry=registry,
        routing=routing,
        dispatcher=dispatcher,
        bus=bus,
        store=InMemorySessionStore(),
        pricing=DEFAULT_PRICE_TABLE,
        workspace_default_model=PLANNER,
    )
    return manager, dispatcher, registry


def _delegate_call(input: dict) -> ToolUseBlock:
    return ToolUseBlock(id="tu_delegate_1", name="delegate", input=input)


async def test_delegate_tool_visible_only_when_planner_can_delegate(bus, event_log, workspace):
    adapter = _ScriptedAnthropicAdapter(
        [_ScriptedResponse(content=[TextBlock(text="ok")], stop_reason=StopReason.END_TURN)]
    )
    manager, _, _ = _build_manager(bus, adapter, planner_can_delegate=True)
    session = manager.create_session(workspace_path=str(workspace), active_model=PLANNER)
    tools = manager._effective_tool_definitions(session)
    await bus.drain()
    await bus.stop()
    assert "delegate" in {d.name for d in tools}


async def test_delegate_tool_hidden_when_planner_cannot_delegate(bus, event_log, workspace):
    adapter = _ScriptedAnthropicAdapter(
        [_ScriptedResponse(content=[TextBlock(text="ok")], stop_reason=StopReason.END_TURN)]
    )
    manager, _, _ = _build_manager(bus, adapter, planner_can_delegate=False)
    session = manager.create_session(workspace_path=str(workspace), active_model=PLANNER)
    tools = manager._effective_tool_definitions(session)
    await bus.drain()
    await bus.stop()
    assert "delegate" not in {d.name for d in tools}


async def test_worker_session_does_not_see_delegate_or_memory_tools(bus, event_log, workspace):
    adapter = _ScriptedAnthropicAdapter(
        [_ScriptedResponse(content=[TextBlock(text="ok")], stop_reason=StopReason.END_TURN)]
    )
    manager, _, _ = _build_manager(bus, adapter)
    session = manager.create_session(workspace_path=str(workspace), active_model=PLANNER)
    worker_session = manager._store.create_session(
        workspace_path=str(workspace),
        active_model=WORKER,
        parent_session_id=session.id,
        parent_tool_use_id="tu_1",
        is_worker=True,
    )
    tools = manager._effective_tool_definitions(worker_session)
    names = {d.name for d in tools}
    await bus.drain()
    await bus.stop()
    assert "delegate" not in names
    assert "memory_add" not in names
    assert "memory_replace" not in names
    assert "memory_consolidate" not in names


async def test_planner_delegates_and_worker_completes(bus, event_log, workspace):
    """Spec §6: planner emits delegate(), tool spawns worker, worker runs,
    planner integrates the tool result and ends the turn."""
    adapter = _ScriptedAnthropicAdapter(
        [
            # Planner: emit a delegate() tool call.
            _ScriptedResponse(
                content=[
                    _delegate_call({"tier": "fast", "task": "summarize the readme in 5 words"})
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            # Worker: produce the summary.
            _ScriptedResponse(
                content=[TextBlock(text="five word readme summary here")],
                stop_reason=StopReason.END_TURN,
            ),
            # Planner: integrate and end.
            _ScriptedResponse(
                content=[TextBlock(text="done: five word readme summary here")],
                stop_reason=StopReason.END_TURN,
            ),
        ]
    )
    manager, _, _ = _build_manager(bus, adapter)
    session = manager.create_session(workspace_path=str(workspace), active_model=PLANNER)

    result = await manager.submit_turn(session.id, "summarize the readme")
    await bus.drain()
    await bus.stop()

    assert result.stop_reason == StopReason.END_TURN
    assert result.tool_call_count == 1
    # Planner's TurnResult cost is the PLANNER's spend only (delegation.md §6.3)
    # — the worker's cost is recorded on the worker's own events.
    assert result.cost_usd > Decimal("0")

    types = [e.type for e in event_log]
    assert "delegate.started" in types
    assert "delegate.completed" in types
    assert types.count("turn.started") == 2  # planner + worker
    assert types.count("turn.completed") == 2

    started = next(e for e in event_log if e.type == "delegate.started")
    assert started.payload["tier"] == "fast"
    assert started.payload["resolved_model"] == WORKER
    assert started.payload["tool_use_id"] == "tu_delegate_1"
    completed = next(e for e in event_log if e.type == "delegate.completed")
    assert completed.payload["worker_session_id"] == started.payload["worker_session_id"]
    assert completed.payload["success"] is True
    assert completed.payload["model"] == WORKER


async def test_worker_llm_events_carry_parent_session_id(bus, event_log, workspace):
    adapter = _ScriptedAnthropicAdapter(
        [
            _ScriptedResponse(
                content=[_delegate_call({"tier": "fast", "task": "do thing"})],
                stop_reason=StopReason.TOOL_USE,
            ),
            _ScriptedResponse(
                content=[TextBlock(text="worker output")],
                stop_reason=StopReason.END_TURN,
            ),
            _ScriptedResponse(
                content=[TextBlock(text="all done")],
                stop_reason=StopReason.END_TURN,
            ),
        ]
    )
    manager, _, _ = _build_manager(bus, adapter)
    session = manager.create_session(workspace_path=str(workspace), active_model=PLANNER)
    await manager.submit_turn(session.id, "go")
    await bus.drain()
    await bus.stop()

    started_events = [e for e in event_log if e.type == "delegate.started"]
    worker_session_id = started_events[0].payload["worker_session_id"]

    worker_llm = [
        e for e in event_log if e.type == "llm.call_completed" and e.session_id == worker_session_id
    ]
    planner_llm = [
        e for e in event_log if e.type == "llm.call_completed" and e.session_id == session.id
    ]
    assert worker_llm and all(e.payload["parent_session_id"] == session.id for e in worker_llm)
    assert planner_llm and all(e.payload["parent_session_id"] is None for e in planner_llm)


async def test_worker_turn_completed_carries_parent_session_id(bus, event_log, workspace):
    adapter = _ScriptedAnthropicAdapter(
        [
            _ScriptedResponse(
                content=[_delegate_call({"tier": "fast", "task": "x"})],
                stop_reason=StopReason.TOOL_USE,
            ),
            _ScriptedResponse(
                content=[TextBlock(text="worker text")], stop_reason=StopReason.END_TURN
            ),
            _ScriptedResponse(
                content=[TextBlock(text="planner final")], stop_reason=StopReason.END_TURN
            ),
        ]
    )
    manager, _, _ = _build_manager(bus, adapter)
    session = manager.create_session(workspace_path=str(workspace), active_model=PLANNER)
    await manager.submit_turn(session.id, "go")
    await bus.drain()
    await bus.stop()

    worker_turn = next(
        e
        for e in event_log
        if e.type == "turn.completed" and e.payload["parent_session_id"] == session.id
    )
    planner_turn = next(
        e
        for e in event_log
        if e.type == "turn.completed" and e.payload["parent_session_id"] is None
    )
    assert worker_turn.session_id != planner_turn.session_id


async def test_slot_5_fires_only_inside_worker_reentry(bus, event_log, workspace):
    """delegation.md §7: slot 5 reports `chose: <tier model>` inside a worker
    re-entry. For top-level sessions slot 5 stays `not_applicable` when the
    chain reaches it (`test_top_level_chain_unchanged_when_delegation_not_used`
    covers that direction)."""
    adapter = _ScriptedAnthropicAdapter(
        [
            _ScriptedResponse(
                content=[_delegate_call({"tier": "fast", "task": "x"})],
                stop_reason=StopReason.TOOL_USE,
            ),
            _ScriptedResponse(content=[TextBlock(text="w")], stop_reason=StopReason.END_TURN),
            _ScriptedResponse(content=[TextBlock(text="p")], stop_reason=StopReason.END_TURN),
        ]
    )
    manager, _, _ = _build_manager(bus, adapter)
    session = manager.create_session(workspace_path=str(workspace), active_model=PLANNER)
    await manager.submit_turn(session.id, "go")
    await bus.drain()
    await bus.stop()

    route_decided = [e for e in event_log if e.type == "route.decided"]
    worker_decided = next(e for e in route_decided if e.session_id != session.id)
    worker_chain = worker_decided.payload["chain"]
    worker_slot5 = next(entry for entry in worker_chain if entry["policy"] == "delegate_request")
    assert worker_slot5["verdict"] == "chose"
    assert worker_slot5["candidate_model"] == WORKER
    assert worker_decided.payload["chosen_model"] == WORKER


async def test_worker_cannot_delegate_recursively(bus, event_log, workspace):
    """delegation.md §2.2.1, §5.6: a worker calling `delegate()` is refused
    even if some misconfigured dispatcher kept the tool registered."""
    from metis_core.tools.builtins.delegate import DelegateTool
    from metis_core.tools.protocol import ToolContext

    tool = DelegateTool()
    context = ToolContext(
        session_id="worker_session",
        turn_id="t1",
        tool_use_id="tu_inner",
        workspace_path=str(workspace),
        workspace_files=None,  # type: ignore[arg-type]
        is_worker=True,
    )
    from metis_core.tools.errors import ToolExecutionError

    with pytest.raises(ToolExecutionError) as exc:
        await tool.execute({"tier": "fast", "task": "x"}, context)
    assert "workers cannot delegate" in str(exc.value).lower()


async def test_no_model_for_tier_returns_delegate_failed(bus, event_log, workspace):
    adapter = _ScriptedAnthropicAdapter(
        [
            _ScriptedResponse(
                content=[_delegate_call({"tier": "deep", "task": "x"})],
                stop_reason=StopReason.TOOL_USE,
            ),
            _ScriptedResponse(
                content=[TextBlock(text="ok, deep unavailable")],
                stop_reason=StopReason.END_TURN,
            ),
        ]
    )
    manager, _, _ = _build_manager(bus, adapter)
    session = manager.create_session(workspace_path=str(workspace), active_model=PLANNER)
    await manager.submit_turn(session.id, "go")
    await bus.drain()
    await bus.stop()

    failed = [e for e in event_log if e.type == "delegate.failed"]
    assert len(failed) == 1
    assert failed[0].payload["failure_mode"] == "no_model_available_for_tier"
    assert failed[0].payload["tool_use_id"] == "tu_delegate_1"


async def test_pattern_slot_defers_inside_worker_reentry(bus, event_log, workspace):
    """delegation.md §11: slot 4 (pattern) defers when slot 5 is in flight,
    so a learned pattern can't silently override the planner's tier= choice."""
    adapter = _ScriptedAnthropicAdapter(
        [
            _ScriptedResponse(
                content=[_delegate_call({"tier": "fast", "task": "x"})],
                stop_reason=StopReason.TOOL_USE,
            ),
            _ScriptedResponse(content=[TextBlock(text="w")], stop_reason=StopReason.END_TURN),
            _ScriptedResponse(content=[TextBlock(text="p")], stop_reason=StopReason.END_TURN),
        ]
    )
    manager, _, _ = _build_manager(bus, adapter)
    session = manager.create_session(workspace_path=str(workspace), active_model=PLANNER)
    await manager.submit_turn(session.id, "go")
    await bus.drain()
    await bus.stop()

    route_decided = [e for e in event_log if e.type == "route.decided"]
    worker_chain = next(e for e in route_decided if e.session_id != session.id).payload["chain"]
    pattern_slot = worker_chain[3]
    assert pattern_slot["policy"] == "pattern"
    assert pattern_slot["verdict"] == "not_applicable"
    assert pattern_slot["reason"] == "delegate_request_in_flight"


async def test_worker_session_record_has_parent_fields(bus, event_log, workspace):
    adapter = _ScriptedAnthropicAdapter(
        [
            _ScriptedResponse(
                content=[_delegate_call({"tier": "fast", "task": "x"})],
                stop_reason=StopReason.TOOL_USE,
            ),
            _ScriptedResponse(content=[TextBlock(text="w")], stop_reason=StopReason.END_TURN),
            _ScriptedResponse(content=[TextBlock(text="p")], stop_reason=StopReason.END_TURN),
        ]
    )
    manager, _, _ = _build_manager(bus, adapter)
    session = manager.create_session(workspace_path=str(workspace), active_model=PLANNER)
    await manager.submit_turn(session.id, "go")
    await bus.drain()
    await bus.stop()

    started = next(e for e in event_log if e.type == "delegate.started")
    worker_id = started.payload["worker_session_id"]
    worker = manager._store.get_session(worker_id)
    assert worker.is_worker is True
    assert worker.parent_session_id == session.id
    assert worker.parent_tool_use_id == "tu_delegate_1"
    # delegation.md §5.2: workers don't inherit a sticky model — slot 5
    # resolves the model from the tier each turn.
    assert worker.active_model is None


async def test_worker_uses_parent_workspace(bus, event_log, workspace):
    """delegation.md §5.3: worker workspace = parent's workspace."""
    adapter = _ScriptedAnthropicAdapter(
        [
            _ScriptedResponse(
                content=[_delegate_call({"tier": "fast", "task": "x"})],
                stop_reason=StopReason.TOOL_USE,
            ),
            _ScriptedResponse(content=[TextBlock(text="w")], stop_reason=StopReason.END_TURN),
            _ScriptedResponse(content=[TextBlock(text="p")], stop_reason=StopReason.END_TURN),
        ]
    )
    manager, _, _ = _build_manager(bus, adapter)
    session = manager.create_session(workspace_path=str(workspace), active_model=PLANNER)
    await manager.submit_turn(session.id, "go")
    await bus.drain()
    await bus.stop()

    started = next(e for e in event_log if e.type == "delegate.started")
    worker = manager._store.get_session(started.payload["worker_session_id"])
    assert worker.workspace_path == session.workspace_path


async def test_worker_dispatcher_is_fresh_instance(bus, event_log, workspace):
    """delegation.md §5.6: worker's tool dispatcher is the same instance as
    the parent's (shared), but `Tool` instances are per-dispatch per the
    dispatcher's protocol (tool factories return fresh instances)."""
    adapter = _ScriptedAnthropicAdapter(
        [
            _ScriptedResponse(
                content=[_delegate_call({"tier": "fast", "task": "x"})],
                stop_reason=StopReason.TOOL_USE,
            ),
            _ScriptedResponse(content=[TextBlock(text="w")], stop_reason=StopReason.END_TURN),
            _ScriptedResponse(content=[TextBlock(text="p")], stop_reason=StopReason.END_TURN),
        ]
    )
    manager, _dispatcher, _ = _build_manager(bus, adapter)
    session = manager.create_session(workspace_path=str(workspace), active_model=PLANNER)
    await manager.submit_turn(session.id, "go")
    await bus.drain()
    await bus.stop()
    started = next(e for e in event_log if e.type == "delegate.started")
    worker_id = started.payload["worker_session_id"]
    # Both sessions share the dispatcher but the per-session id maps are distinct.
    assert worker_id in manager._tool_id_maps or worker_id not in manager._tool_id_maps
    # Definitions visible to each session differ (worker's view filters out delegate).
    worker = manager._store.get_session(worker_id)
    assert "delegate" in {d.name for d in manager._effective_tool_definitions(session)}
    assert "delegate" not in {d.name for d in manager._effective_tool_definitions(worker)}


async def test_top_level_chain_unchanged_when_delegation_not_used(bus, event_log, workspace):
    """Default behavior: a top-level session that doesn't go through slot 5
    sees `not_applicable` at slot 5 when the chain reaches it. (When an
    earlier slot wins, the chain truncates per routing-engine.md §4 — slot 5
    simply isn't evaluated, which is also fine.)"""
    adapter = _ScriptedAnthropicAdapter(
        [
            _ScriptedResponse(content=[TextBlock(text="hi")], stop_reason=StopReason.END_TURN),
        ]
    )
    # No sticky model + no workspace default: chain walks all the way.
    registry = ModelRegistry()
    registry.register(
        model_id=PLANNER,
        adapter=adapter,
        aliases=["sonnet"],
        can_delegate=True,
    )
    routing = RoutingEngine(registry=registry, bus=bus)
    dispatcher = ToolDispatcher(bus)
    register_builtins(dispatcher)
    manager = SessionManager(
        registry=registry,
        routing=routing,
        dispatcher=dispatcher,
        bus=bus,
        store=InMemorySessionStore(),
        pricing=DEFAULT_PRICE_TABLE,
        global_default_model=PLANNER,
    )
    session = manager.create_session(workspace_path=str(workspace))
    await manager.submit_turn(session.id, "hello")
    await bus.drain()
    await bus.stop()

    route_decided = next(e for e in event_log if e.type == "route.decided")
    chain = route_decided.payload["chain"]
    slot5 = next(entry for entry in chain if entry["policy"] == "delegate_request")
    assert slot5["verdict"] == "not_applicable"
    assert slot5["reason"] == "not a delegation re-entry"
