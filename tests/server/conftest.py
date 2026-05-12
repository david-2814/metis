"""Shared fixtures for server tests: builds a runtime with a scripted adapter."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from metis.adapters.protocol import CanonicalRequest, StopReason, TokenUsage
from metis.adapters.streaming import (
    MessageComplete,
    MessageStart,
    TextDelta,
    ToolUseEnd,
    ToolUseStart,
)
from metis.canonical.capabilities import AdapterCapabilities
from metis.canonical.content import TextBlock, ToolUseBlock
from metis.canonical.ids import new_message_id
from metis.cli.runtime import ChatRuntime
from metis.events.bus import EventBus
from metis.memory import MemoryStore, register_memory_tools
from metis.pricing import DEFAULT_PRICE_TABLE
from metis.routing import ModelRegistry, RoutingEngine
from metis.sessions import InMemorySessionStore, SessionManager
from metis.tools.dispatcher import ToolDispatcher
from metis.trace.store import TraceStore


@dataclass
class _Scripted:
    content: list
    stop_reason: StopReason


class _RuntimeAdapter:
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
                yield TextDelta(
                    message_id=message_id, content_block_index=idx, text=block.text
                )
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
            latency_ms=42,
        )

    async def cancel(self, request_id: str) -> bool:
        return False

    async def close(self) -> None:
        return

    def estimate_input_tokens(self, messages, tools, system_prompt) -> int:
        return 100


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "README.md").write_text("# project\nhello")
    return tmp_path


@pytest.fixture
def scripted_adapter() -> _RuntimeAdapter:
    return _RuntimeAdapter(
        [
            _Scripted(
                content=[TextBlock(text="hi from server")],
                stop_reason=StopReason.END_TURN,
            )
        ]
    )


@pytest.fixture
async def runtime(tmp_path: Path, workspace: Path, scripted_adapter: _RuntimeAdapter) -> ChatRuntime:
    bus = EventBus()
    bus.start()
    db_file = tmp_path / "server.db"
    trace = TraceStore(db_file)
    trace.attach_to(bus)
    session_store = InMemorySessionStore()

    registry = ModelRegistry()
    registry.register(
        model_id="anthropic:claude-sonnet-4-6",
        adapter=scripted_adapter,
        aliases=["sonnet"],
    )
    registry.register(
        model_id="anthropic:claude-haiku-4-5",
        adapter=scripted_adapter,
        aliases=["haiku"],
    )
    routing = RoutingEngine(registry=registry, bus=bus)
    dispatcher = ToolDispatcher(bus)
    register_memory_tools(dispatcher)
    manager = SessionManager(
        registry=registry,
        routing=routing,
        dispatcher=dispatcher,
        bus=bus,
        store=session_store,
        pricing=DEFAULT_PRICE_TABLE,
        memory_factory=lambda ws: MemoryStore(ws),
    )

    rt = ChatRuntime(
        bus=bus,
        trace=trace,
        session_store=session_store,
        registry=registry,
        routing=routing,
        dispatcher=dispatcher,
        manager=manager,
        adapters=[scripted_adapter],
        db_file=db_file,
        pricing=DEFAULT_PRICE_TABLE,
        global_default_model="anthropic:claude-sonnet-4-6",
    )
    yield rt
    await bus.drain()
    await bus.stop()
    trace.close()
