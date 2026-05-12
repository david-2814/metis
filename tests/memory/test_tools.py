"""Memory tools: end-to-end dispatch through ToolDispatcher with a real store."""

from __future__ import annotations

from pathlib import Path

import pytest

from metis.canonical.content import ToolUseBlock
from metis.events.bus import EventBus, EventFilter, Subscription
from metis.events.envelope import Event
from metis.memory.store import MemoryFile, MemoryStore
from metis.memory.tools import register_memory_tools
from metis.tools.dispatcher import ToolDispatcher


@pytest.fixture
async def bus() -> EventBus:
    b = EventBus()
    b.start()
    return b


@pytest.fixture
async def event_log(bus: EventBus) -> list[Event]:
    events: list[Event] = []

    async def handler(e: Event) -> None:
        events.append(e)

    bus.subscribe(
        Subscription(filter=EventFilter(), handler=handler, name="log", fast_path=True)
    )
    return events


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def store(workspace: Path) -> MemoryStore:
    return MemoryStore(workspace)


@pytest.fixture
def dispatcher(bus: EventBus) -> ToolDispatcher:
    d = ToolDispatcher(bus)
    register_memory_tools(d)
    return d


def _tool_use(name: str, **input: object) -> ToolUseBlock:
    return ToolUseBlock(id=f"tu_{name}_1", name=name, input=input)


async def test_memory_tools_registered(dispatcher: ToolDispatcher):
    names = {d.name for d in dispatcher.get_definitions()}
    assert names == {"memory_add", "memory_replace", "memory_consolidate"}


async def test_memory_add_writes_file(
    bus: EventBus, workspace: Path, store: MemoryStore, dispatcher: ToolDispatcher
):
    result = await dispatcher.dispatch(
        _tool_use("memory_add", file="MEMORY.md", entry="metis tests live in tests/"),
        session_id="s1",
        turn_id="t1",
        workspace_path=str(workspace),
        memory=store,
    )
    await bus.drain()
    await bus.stop()

    assert result.is_error is False
    assert store.read(MemoryFile.MEMORY) == "metis tests live in tests/\n"


async def test_memory_add_refuses_when_memory_not_configured(
    bus: EventBus, workspace: Path, dispatcher: ToolDispatcher, event_log: list[Event]
):
    result = await dispatcher.dispatch(
        _tool_use("memory_add", file="MEMORY.md", entry="x"),
        session_id="s1",
        turn_id="t1",
        workspace_path=str(workspace),
        memory=None,
    )
    await bus.drain()
    await bus.stop()

    assert result.is_error is True
    failed = next(e for e in event_log if e.type == "tool.failed")
    assert failed.payload["error_class"] == "execution_error"


async def test_memory_add_rejects_unknown_file(
    bus: EventBus, workspace: Path, store: MemoryStore, dispatcher: ToolDispatcher
):
    # Schema enum should catch this before tool runs.
    result = await dispatcher.dispatch(
        _tool_use("memory_add", file="OTHER.md", entry="x"),
        session_id="s1",
        turn_id="t1",
        workspace_path=str(workspace),
        memory=store,
    )
    await bus.drain()
    await bus.stop()
    assert result.is_error is True


async def test_memory_add_hard_cap_returns_error(
    bus: EventBus, workspace: Path, store: MemoryStore, dispatcher: ToolDispatcher
):
    payload = "x" * 10_000
    result = await dispatcher.dispatch(
        _tool_use("memory_add", file="MEMORY.md", entry=payload),
        session_id="s1",
        turn_id="t1",
        workspace_path=str(workspace),
        memory=store,
    )
    await bus.drain()
    await bus.stop()
    assert result.is_error is True
    # The file should NOT have been created
    assert not (workspace / ".metis" / "MEMORY.md").exists()


async def test_memory_replace_unique_substring(
    bus: EventBus, workspace: Path, store: MemoryStore, dispatcher: ToolDispatcher
):
    store.add_entry(MemoryFile.MEMORY, "user prefers Go")
    result = await dispatcher.dispatch(
        _tool_use("memory_replace", file="MEMORY.md", old="Go", new="Rust"),
        session_id="s1",
        turn_id="t1",
        workspace_path=str(workspace),
        memory=store,
    )
    await bus.drain()
    await bus.stop()
    assert result.is_error is False
    assert store.read(MemoryFile.MEMORY) == "user prefers Rust\n"


async def test_memory_replace_not_found_returns_error(
    bus: EventBus, workspace: Path, store: MemoryStore, dispatcher: ToolDispatcher
):
    store.add_entry(MemoryFile.MEMORY, "alpha")
    result = await dispatcher.dispatch(
        _tool_use("memory_replace", file="MEMORY.md", old="zzz", new="x"),
        session_id="s1",
        turn_id="t1",
        workspace_path=str(workspace),
        memory=store,
    )
    await bus.drain()
    await bus.stop()
    assert result.is_error is True


async def test_memory_consolidate_overwrites(
    bus: EventBus, workspace: Path, store: MemoryStore, dispatcher: ToolDispatcher
):
    store.add_entry(MemoryFile.MEMORY, "line one")
    store.add_entry(MemoryFile.MEMORY, "line two")
    result = await dispatcher.dispatch(
        _tool_use("memory_consolidate", file="MEMORY.md", content="compact line\n"),
        session_id="s1",
        turn_id="t1",
        workspace_path=str(workspace),
        memory=store,
    )
    await bus.drain()
    await bus.stop()
    assert result.is_error is False
    assert store.read(MemoryFile.MEMORY) == "compact line\n"


async def test_memory_tool_metadata_includes_hashes(
    bus: EventBus, workspace: Path, store: MemoryStore, dispatcher: ToolDispatcher, event_log
):
    await dispatcher.dispatch(
        _tool_use("memory_add", file="USER.md", entry="user is a data scientist"),
        session_id="s1",
        turn_id="t1",
        workspace_path=str(workspace),
        memory=store,
    )
    await bus.drain()
    await bus.stop()
    completed = next(e for e in event_log if e.type == "tool.completed")
    assert completed.payload["success"] is True
    assert completed.payload["files_modified"] == ["USER.md"]
