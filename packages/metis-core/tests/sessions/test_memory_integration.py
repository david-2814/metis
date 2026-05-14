"""Session-level memory integration: factory wiring, system prompt composition, events."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from metis_core.adapters.protocol import CanonicalRequest, StopReason, TokenUsage
from metis_core.adapters.streaming import (
    MessageComplete,
    MessageStart,
    TextDelta,
    ToolUseEnd,
    ToolUseStart,
)
from metis_core.canonical.capabilities import AdapterCapabilities
from metis_core.canonical.content import TextBlock, ToolUseBlock
from metis_core.canonical.ids import new_message_id
from metis_core.events.bus import EventBus, EventFilter, Subscription
from metis_core.events.envelope import Event
from metis_core.memory.store import MemoryFile, MemoryStore
from metis_core.memory.tools import register_memory_tools
from metis_core.pricing import DEFAULT_PRICE_TABLE
from metis_core.routing import ModelRegistry, RoutingEngine
from metis_core.sessions import InMemorySessionStore, SessionManager
from metis_core.tools.dispatcher import ToolDispatcher


@dataclass
class _Scripted:
    content: list
    stop_reason: StopReason


class _RecordingAdapter:
    """Records every request — exposes the system_prompt for assertions."""

    name = "anthropic"

    def __init__(self, responses: list[_Scripted]) -> None:
        self._responses = list(responses)
        self.requests: list[CanonicalRequest] = []

    def capabilities_for(self, model: str) -> AdapterCapabilities:
        return AdapterCapabilities(
            supports_thinking=False,
            supports_images=True,
            supports_tools=True,
            supports_system_prompt=True,
            supports_structured_output=False,
            supports_streaming=True,
            supports_streaming_tool_calls=True,
            supports_parallel_tool_calls=True,
            supports_prompt_caching=True,
            supports_system_messages_in_list=False,
            max_context_tokens=200_000,
            max_output_tokens=8192,
        )

    async def stream(self, request: CanonicalRequest):
        self.requests.append(request)
        scripted = self._responses.pop(0)
        message_id = new_message_id()
        yield MessageStart(message_id=message_id, model=request.model)
        for idx, block in enumerate(scripted.content):
            if isinstance(block, TextBlock):
                yield TextDelta(message_id=message_id, content_block_index=idx, text=block.text)
            elif isinstance(block, ToolUseBlock):
                yield ToolUseStart(
                    message_id=message_id,
                    content_block_index=idx,
                    tool_use_id=block.id,
                    tool_name=block.name,
                )
                yield ToolUseEnd(
                    message_id=message_id,
                    content_block_index=idx,
                    tool_use_id=block.id,
                    final_input=block.input,
                )
        yield MessageComplete(
            message_id=message_id,
            stop_reason=scripted.stop_reason,
            final_content=scripted.content,
            usage=TokenUsage(input_tokens=10, output_tokens=5),
            latency_ms=10,
        )

    async def cancel(self, request_id: str) -> bool:
        return False

    async def close(self) -> None:
        return

    def estimate_input_tokens(self, messages, tools, system_prompt) -> int:
        return len(system_prompt or "") // 4


@pytest.fixture
async def bus() -> EventBus:
    b = EventBus()
    b.start()
    return b


@pytest.fixture
async def event_log(bus: EventBus) -> list[Event]:
    log: list[Event] = []

    async def handler(e: Event) -> None:
        log.append(e)

    bus.subscribe(Subscription(filter=EventFilter(), handler=handler, name="log", fast_path=True))
    return log


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return tmp_path


def _build_manager(
    bus: EventBus,
    adapter: _RecordingAdapter,
    *,
    memory_factory=None,
):
    registry = ModelRegistry()
    registry.register(
        model_id="anthropic:claude-sonnet-4-6",
        adapter=adapter,
        aliases=["sonnet"],
    )
    routing = RoutingEngine(registry=registry, bus=bus)
    dispatcher = ToolDispatcher(bus)
    register_memory_tools(dispatcher)
    manager = SessionManager(
        registry=registry,
        routing=routing,
        dispatcher=dispatcher,
        bus=bus,
        store=InMemorySessionStore(),
        pricing=DEFAULT_PRICE_TABLE,
        memory_factory=memory_factory,
    )
    return manager, dispatcher


async def test_no_memory_factory_means_no_memory_in_prompt(bus, workspace):
    adapter = _RecordingAdapter(
        [_Scripted(content=[TextBlock(text="ok")], stop_reason=StopReason.END_TURN)]
    )
    manager, _ = _build_manager(bus, adapter, memory_factory=None)
    session = manager.create_session(workspace_path=str(workspace))
    assert manager.memory_for(session.id) is None
    await manager.submit_turn(session.id, "hi")
    await bus.drain()
    await bus.stop()
    # System prompt should not contain memory sections.
    sp = adapter.requests[0].system_prompt
    assert "USER.md" not in sp
    assert "MEMORY.md" not in sp


async def test_memory_factory_creates_per_session_store(bus, workspace):
    adapter = _RecordingAdapter(
        [_Scripted(content=[TextBlock(text="ok")], stop_reason=StopReason.END_TURN)]
    )
    manager, _ = _build_manager(bus, adapter, memory_factory=MemoryStore)
    session = manager.create_session(workspace_path=str(workspace))
    store = manager.memory_for(session.id)
    assert isinstance(store, MemoryStore)
    assert Path(store.workspace_path) == workspace.resolve()


async def test_memory_appears_in_system_prompt_on_next_turn(bus, workspace):
    """Pre-populating USER.md/MEMORY.md flows into the next turn's volatile
    system segment (separated from the stable prefix so the cache breakpoint
    can ride on the latter — see context-assembler.md §5)."""
    # Pre-write to the store before submitting a turn.
    store = MemoryStore(workspace)
    store.add_entry(MemoryFile.USER, "user is a Go developer")
    store.add_entry(MemoryFile.MEMORY, "tests live in tests/")

    adapter = _RecordingAdapter(
        [_Scripted(content=[TextBlock(text="ok")], stop_reason=StopReason.END_TURN)]
    )
    # Factory returns a fresh store pointing at the same workspace.
    manager, _ = _build_manager(bus, adapter, memory_factory=MemoryStore)
    session = manager.create_session(workspace_path=str(workspace))
    await manager.submit_turn(session.id, "hi")
    await bus.drain()
    await bus.stop()
    volatile = adapter.requests[0].system_prompt_volatile
    assert volatile is not None
    assert "user is a Go developer" in volatile
    assert "tests live in tests/" in volatile
    # USER.md before MEMORY.md.
    assert volatile.index("USER.md") < volatile.index("MEMORY.md")
    # Stable prefix stays free of memory so the cache breakpoint works.
    assert "user is a Go developer" not in (adapter.requests[0].system_prompt or "")


async def test_memory_tool_emits_memory_updated_event(bus, event_log, workspace):
    """Agent calls memory_add → memory.updated event emitted by SessionManager."""
    adapter = _RecordingAdapter(
        [
            _Scripted(
                content=[
                    ToolUseBlock(
                        id="tu_mem_1",
                        name="memory_add",
                        input={"file": "MEMORY.md", "entry": "remembered fact"},
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            _Scripted(
                content=[TextBlock(text="noted")],
                stop_reason=StopReason.END_TURN,
            ),
        ]
    )
    manager, _ = _build_manager(bus, adapter, memory_factory=MemoryStore)
    session = manager.create_session(workspace_path=str(workspace))
    await manager.submit_turn(session.id, "remember this")
    await bus.drain()
    await bus.stop()

    updates = [e for e in event_log if e.type == "memory.updated"]
    assert len(updates) == 1
    payload = updates[0].payload
    assert payload["file"] == "MEMORY.md"
    assert payload["operation"] == "add"
    assert payload["before_size_bytes"] == 0
    assert payload["after_size_bytes"] > 0
    assert payload["before_hash"] != payload["after_hash"]

    # No eviction since we're well under the cap.
    evictions = [e for e in event_log if e.type == "memory.eviction"]
    assert evictions == []


async def test_memory_eviction_event_when_over_soft_cap(bus, event_log, workspace):
    """A memory_add that pushes file past the soft cap → memory.eviction emitted."""
    # Pre-fill near the soft cap so adding one entry tips it over.
    store = MemoryStore(workspace)
    payload = "x" * 1_900  # MEMORY soft cap is 2048
    store.add_entry(MemoryFile.MEMORY, payload)

    adapter = _RecordingAdapter(
        [
            _Scripted(
                content=[
                    ToolUseBlock(
                        id="tu_mem_2",
                        name="memory_add",
                        input={"file": "MEMORY.md", "entry": "y" * 200},
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            _Scripted(
                content=[TextBlock(text="ok")],
                stop_reason=StopReason.END_TURN,
            ),
        ]
    )
    manager, _ = _build_manager(bus, adapter, memory_factory=MemoryStore)
    session = manager.create_session(workspace_path=str(workspace))
    await manager.submit_turn(session.id, "remember more")
    await bus.drain()
    await bus.stop()

    evictions = [e for e in event_log if e.type == "memory.eviction"]
    assert len(evictions) == 1
    p = evictions[0].payload
    assert p["file"] == "MEMORY.md"
    assert p["trigger"] == "size_cap_exceeded"
    assert p["size_after_bytes"] > p["size_before_bytes"]


async def test_memory_consolidate_after_writes_emits_consolidate_operation(
    bus, event_log, workspace
):
    adapter = _RecordingAdapter(
        [
            _Scripted(
                content=[
                    ToolUseBlock(
                        id="tu_c",
                        name="memory_consolidate",
                        input={"file": "USER.md", "content": "compact\n"},
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            _Scripted(
                content=[TextBlock(text="done")],
                stop_reason=StopReason.END_TURN,
            ),
        ]
    )
    # Pre-populate USER.md so consolidate has a 'before' state.
    store = MemoryStore(workspace)
    store.add_entry(MemoryFile.USER, "line a")
    store.add_entry(MemoryFile.USER, "line b")

    manager, _ = _build_manager(bus, adapter, memory_factory=MemoryStore)
    session = manager.create_session(workspace_path=str(workspace))
    await manager.submit_turn(session.id, "tidy memory")
    await bus.drain()
    await bus.stop()

    updates = [e for e in event_log if e.type == "memory.updated"]
    assert len(updates) == 1
    assert updates[0].payload["operation"] == "consolidate"
    assert updates[0].payload["file"] == "USER.md"


async def test_second_turn_picks_up_memory_written_in_first_turn(bus, event_log, workspace):
    """Memory added by a tool in turn 1 should show up in turn 2's system prompt."""
    adapter = _RecordingAdapter(
        [
            # Turn 1: agent writes to memory, then ends.
            _Scripted(
                content=[
                    ToolUseBlock(
                        id="tu_1",
                        name="memory_add",
                        input={"file": "USER.md", "entry": "user is a Rust dev"},
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            _Scripted(
                content=[TextBlock(text="noted")],
                stop_reason=StopReason.END_TURN,
            ),
            # Turn 2: just answers.
            _Scripted(
                content=[TextBlock(text="hi rust dev")],
                stop_reason=StopReason.END_TURN,
            ),
        ]
    )
    manager, _ = _build_manager(bus, adapter, memory_factory=MemoryStore)
    session = manager.create_session(workspace_path=str(workspace))
    await manager.submit_turn(session.id, "remember I do rust")
    await manager.submit_turn(session.id, "are you sure?")
    await bus.drain()
    await bus.stop()

    # Turn 1's first request: no memory yet.
    assert not (adapter.requests[0].system_prompt_volatile or "").__contains__("user is a Rust dev")
    # Turn 1's second request (after tool dispatch): memory now in the
    # volatile segment, kept out of the cached stable prefix.
    assert "user is a Rust dev" in (adapter.requests[1].system_prompt_volatile or "")
    # Turn 2's request: memory still present in the volatile segment.
    assert "user is a Rust dev" in (adapter.requests[2].system_prompt_volatile or "")
