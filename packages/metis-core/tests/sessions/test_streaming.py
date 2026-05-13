"""Session-level streaming test: SessionManager forwards events live."""

from __future__ import annotations

from pathlib import Path

import pytest
from metis_core.adapters.protocol import StopReason
from metis_core.adapters.streaming import (
    MessageComplete,
    MessageStart,
    StreamingEvent,
    TextDelta,
    ToolUseEnd,
    ToolUseStart,
)
from metis_core.canonical.content import TextBlock, ToolUseBlock
from metis_core.events.bus import EventBus
from metis_core.pricing import DEFAULT_PRICE_TABLE
from metis_core.routing import ModelRegistry, RoutingEngine
from metis_core.sessions import InMemorySessionStore, SessionManager
from metis_core.tools.builtins.file_ops import ReadFileTool
from metis_core.tools.dispatcher import ToolDispatcher

from tests_shared.scripted_adapter import _ScriptedAnthropicAdapter, _ScriptedResponse


@pytest.fixture
async def bus() -> EventBus:
    bus = EventBus()
    bus.start()
    return bus


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "README.md").write_text("# project\nA repo.")
    return tmp_path


def _build_manager(bus: EventBus, adapter: _ScriptedAnthropicAdapter) -> SessionManager:
    registry = ModelRegistry()
    registry.register(model_id="anthropic:claude-sonnet-4-6", adapter=adapter, aliases=["sonnet"])
    routing = RoutingEngine(registry=registry, bus=bus)
    dispatcher = ToolDispatcher(bus)
    dispatcher.register(ReadFileTool)
    return SessionManager(
        registry=registry,
        routing=routing,
        dispatcher=dispatcher,
        bus=bus,
        store=InMemorySessionStore(),
        pricing=DEFAULT_PRICE_TABLE,
    )


# ---- Tests --------------------------------------------------------------


async def test_stream_handler_receives_events_in_order(bus, workspace):
    """Single text turn: handler sees MessageStart → TextDelta → MessageComplete."""
    adapter = _ScriptedAnthropicAdapter(
        [_ScriptedResponse(content=[TextBlock(text="hello")], stop_reason=StopReason.END_TURN)]
    )
    manager = _build_manager(bus, adapter)
    session = manager.create_session(workspace_path=str(workspace), active_model="sonnet")

    collected: list[StreamingEvent] = []

    def handler(event: StreamingEvent) -> None:
        collected.append(event)

    await manager.submit_turn(session.id, "hi", on_streaming_event=handler)
    await bus.drain()
    await bus.stop()

    types = [type(e).__name__ for e in collected]
    assert types == ["MessageStart", "TextDelta", "MessageComplete"]


async def test_stream_handler_async_callback_is_awaited(bus, workspace):
    adapter = _ScriptedAnthropicAdapter(
        [_ScriptedResponse(content=[TextBlock(text="hi")], stop_reason=StopReason.END_TURN)]
    )
    manager = _build_manager(bus, adapter)
    session = manager.create_session(workspace_path=str(workspace), active_model="sonnet")
    collected: list[StreamingEvent] = []

    async def async_handler(event: StreamingEvent) -> None:
        collected.append(event)

    await manager.submit_turn(session.id, "hi", on_streaming_event=async_handler)
    await bus.drain()
    await bus.stop()
    types = [type(e).__name__ for e in collected]
    assert types == ["MessageStart", "TextDelta", "MessageComplete"]


async def test_streaming_through_tool_use_cycle(bus, workspace):
    """Two LLM calls in one turn: handler sees two MessageStart events with
    a tool-use cycle in between."""
    adapter = _ScriptedAnthropicAdapter(
        [
            _ScriptedResponse(
                content=[
                    TextBlock(text="I'll read it."),
                    ToolUseBlock(id="tu_001", name="read_file", input={"path": "README.md"}),
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            _ScriptedResponse(
                content=[TextBlock(text="The file says ...")],
                stop_reason=StopReason.END_TURN,
            ),
        ]
    )
    manager = _build_manager(bus, adapter)
    session = manager.create_session(workspace_path=str(workspace), active_model="sonnet")
    collected: list[StreamingEvent] = []

    def handler(event: StreamingEvent) -> None:
        collected.append(event)

    result = await manager.submit_turn(session.id, "summarize", on_streaming_event=handler)
    await bus.drain()
    await bus.stop()

    # Two LLM calls → two MessageStart, two MessageComplete events.
    starts = [e for e in collected if isinstance(e, MessageStart)]
    completes = [e for e in collected if isinstance(e, MessageComplete)]
    assert len(starts) == 2
    assert len(completes) == 2

    # First MessageComplete has stop_reason=tool_use; second is end_turn.
    assert completes[0].stop_reason == StopReason.TOOL_USE
    assert completes[1].stop_reason == StopReason.END_TURN

    # The tool_use markers were forwarded.
    tool_starts = [e for e in collected if isinstance(e, ToolUseStart)]
    tool_ends = [e for e in collected if isinstance(e, ToolUseEnd)]
    assert len(tool_starts) == 1
    assert len(tool_ends) == 1
    assert tool_starts[0].tool_name == "read_file"

    # The result still reports correct turn-level stats.
    assert result.llm_call_count == 2
    assert result.tool_call_count == 1
    assert result.stop_reason == StopReason.END_TURN


async def test_no_handler_runs_silently(bus, workspace):
    """submit_turn without a handler still works."""
    adapter = _ScriptedAnthropicAdapter(
        [_ScriptedResponse(content=[TextBlock(text="ok")], stop_reason=StopReason.END_TURN)]
    )
    manager = _build_manager(bus, adapter)
    session = manager.create_session(workspace_path=str(workspace), active_model="sonnet")
    result = await manager.submit_turn(session.id, "hi")
    await bus.drain()
    await bus.stop()
    assert result.assistant_text == "ok"


async def test_text_deltas_arrive_with_block_index(bus, workspace):
    adapter = _ScriptedAnthropicAdapter(
        [_ScriptedResponse(content=[TextBlock(text="hello")], stop_reason=StopReason.END_TURN)]
    )
    manager = _build_manager(bus, adapter)
    session = manager.create_session(workspace_path=str(workspace), active_model="sonnet")
    collected: list[StreamingEvent] = []

    def handler(event: StreamingEvent) -> None:
        collected.append(event)

    await manager.submit_turn(session.id, "hi", on_streaming_event=handler)
    await bus.drain()
    await bus.stop()

    text_deltas = [e for e in collected if isinstance(e, TextDelta)]
    assert len(text_deltas) == 1
    assert text_deltas[0].text == "hello"
    assert text_deltas[0].content_block_index == 0
