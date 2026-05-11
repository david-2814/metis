"""End-to-end tests for SessionManager with a scripted adapter.

These exercise the turn loop, tool-use cycle, cost stamping, and event
emission without making any HTTP calls.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pytest

from metis.adapters.protocol import (
    CanonicalRequest,
    CanonicalResponse,
    StopReason,
    TokenUsage,
)
from metis.adapters.streaming import (
    MessageComplete,
    MessageStart,
    TextDelta,
    ToolUseEnd,
    ToolUseInputDelta,
    ToolUseStart,
)
from metis.canonical.capabilities import AdapterCapabilities
from metis.canonical.content import TextBlock, ToolUseBlock
from metis.canonical.ids import new_message_id
from metis.canonical.messages import Role
from metis.events.bus import EventBus, EventFilter, Subscription
from metis.events.envelope import Event
from metis.pricing import DEFAULT_PRICE_TABLE
from metis.routing import ModelRegistry, RoutingEngine
from metis.routing.engine import RoutingError
from metis.sessions import InMemorySessionStore, SessionManager, UnknownAliasError
from metis.tools.dispatcher import ToolDispatcher

# ---- Fake adapter ------------------------------------------------------


@dataclass
class _ScriptedResponse:
    content: list
    stop_reason: StopReason
    input_tokens: int = 10
    output_tokens: int = 5


class _ScriptedAnthropicAdapter:
    """Returns scripted responses in order. Records every request."""

    name = "anthropic"

    def __init__(
        self,
        responses: list[_ScriptedResponse],
        *,
        capability_overrides: dict[str, AdapterCapabilities] | None = None,
    ) -> None:
        self._responses = list(responses)
        self.requests: list[CanonicalRequest] = []
        self._caps_overrides = capability_overrides or {}

    def capabilities_for(self, model: str) -> AdapterCapabilities:
        if model in self._caps_overrides:
            return self._caps_overrides[model]
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

    async def complete(self, request: CanonicalRequest) -> CanonicalResponse:
        self.requests.append(request)
        if not self._responses:
            raise AssertionError("scripted adapter ran out of responses")
        scripted = self._responses.pop(0)
        return CanonicalResponse(
            request_id=request.request_id,
            model=request.model,
            provider=self.name,
            content=scripted.content,
            stop_reason=scripted.stop_reason,
            usage=TokenUsage(
                input_tokens=scripted.input_tokens,
                output_tokens=scripted.output_tokens,
            ),
            latency_ms=42,
        )

    async def stream(self, request: CanonicalRequest):
        """Synthesize streaming events from a scripted response.

        Yields a MessageStart, then text/tool deltas + ends matching the
        scripted content, then a MessageComplete with the final state. This
        is enough for the SessionManager to drive its streaming-based loop
        in tests without needing real SDK streaming chunks.
        """
        self.requests.append(request)
        if not self._responses:
            raise AssertionError("scripted adapter ran out of responses")
        scripted = self._responses.pop(0)
        message_id = new_message_id()
        import json as _json

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
                json_str = _json.dumps(block.input)
                yield ToolUseInputDelta(
                    message_id=message_id,
                    content_block_index=idx,
                    tool_use_id=block.id,
                    partial_json=json_str,
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
            usage=TokenUsage(
                input_tokens=scripted.input_tokens,
                output_tokens=scripted.output_tokens,
            ),
            latency_ms=42,
        )

    async def cancel(self, request_id: str) -> bool:
        return False

    async def close(self) -> None:
        return

    def estimate_input_tokens(self, messages, tools, system_prompt) -> int:
        return 100


# ---- Fixtures ----------------------------------------------------------


@pytest.fixture
async def bus() -> EventBus:
    bus = EventBus()
    bus.start()
    return bus


@pytest.fixture
async def event_log(bus: EventBus) -> list[Event]:
    log: list[Event] = []

    async def handler(e: Event) -> None:
        log.append(e)

    bus.subscribe(Subscription(filter=EventFilter(), handler=handler, name="log", fast_path=True))
    return log


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "README.md").write_text("# project\nA repo.")
    return tmp_path


def _build_manager(
    bus: EventBus,
    adapter: _ScriptedAnthropicAdapter,
    *,
    workspace_default: str | None = None,
) -> tuple[SessionManager, ToolDispatcher]:
    registry = ModelRegistry()
    registry.register(
        model_id="anthropic:claude-sonnet-4-6",
        adapter=adapter,
        aliases=["sonnet"],
    )
    registry.register(
        model_id="anthropic:claude-haiku-4-5",
        adapter=adapter,
        aliases=["haiku"],
    )
    routing = RoutingEngine(registry=registry, bus=bus)
    dispatcher = ToolDispatcher(bus)
    from metis.tools.builtins.file_ops import ListDirTool, ReadFileTool

    dispatcher.register(ReadFileTool)
    dispatcher.register(ListDirTool)
    manager = SessionManager(
        registry=registry,
        routing=routing,
        dispatcher=dispatcher,
        bus=bus,
        store=InMemorySessionStore(),
        pricing=DEFAULT_PRICE_TABLE,
        workspace_default_model=workspace_default,
    )
    return manager, dispatcher


# ---- Tests -------------------------------------------------------------


async def test_simple_text_turn(bus, event_log, workspace):
    """User asks → assistant answers with text → end of turn."""
    adapter = _ScriptedAnthropicAdapter(
        [
            _ScriptedResponse(
                content=[TextBlock(text="hi there")],
                stop_reason=StopReason.END_TURN,
            )
        ]
    )
    manager, _ = _build_manager(bus, adapter)
    session = manager.create_session(workspace_path=str(workspace))

    result = await manager.submit_turn(session.id, "hello")
    await bus.drain()
    await bus.stop()

    assert result.assistant_text == "hi there"
    assert result.stop_reason == StopReason.END_TURN
    assert result.llm_call_count == 1
    assert result.tool_call_count == 0
    # Cost: 10 input + 5 output @ sonnet = (10*3 + 5*15) / 1M = $0.000105
    assert result.cost_usd == Decimal("0.000105")

    event_types = [e.type for e in event_log]
    assert event_types == [
        "turn.started",
        "route.decided",
        "llm.call_started",
        "llm.call_completed",
        "turn.completed",
    ]


async def test_tool_use_cycle_completes_in_two_llm_calls(bus, event_log, workspace):
    """assistant emits tool_use → dispatcher runs it → assistant emits final text."""
    adapter = _ScriptedAnthropicAdapter(
        [
            _ScriptedResponse(
                content=[
                    TextBlock(text="I'll read the README."),
                    ToolUseBlock(id="tu_001", name="read_file", input={"path": "README.md"}),
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            _ScriptedResponse(
                content=[TextBlock(text="The README describes the project.")],
                stop_reason=StopReason.END_TURN,
            ),
        ]
    )
    manager, _ = _build_manager(bus, adapter)
    session = manager.create_session(workspace_path=str(workspace))

    result = await manager.submit_turn(session.id, "summarize the readme")
    await bus.drain()
    await bus.stop()

    assert result.llm_call_count == 2
    assert result.tool_call_count == 1
    assert result.stop_reason == StopReason.END_TURN
    assert result.assistant_text == "The README describes the project."

    # Second LLM call should have seen the tool_result in history.
    second_request = adapter.requests[1]
    tool_roles = [m.role for m in second_request.messages]
    assert Role.TOOL in tool_roles


async def test_parallel_tool_uses_dispatch_concurrently(bus, event_log, workspace):
    """Multiple tool_use blocks in one assistant message → all dispatch."""
    adapter = _ScriptedAnthropicAdapter(
        [
            _ScriptedResponse(
                content=[
                    ToolUseBlock(id="tu_a", name="read_file", input={"path": "README.md"}),
                    ToolUseBlock(id="tu_b", name="list_dir", input={"path": "."}),
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            _ScriptedResponse(
                content=[TextBlock(text="done")],
                stop_reason=StopReason.END_TURN,
            ),
        ]
    )
    manager, _ = _build_manager(bus, adapter)
    session = manager.create_session(workspace_path=str(workspace))
    result = await manager.submit_turn(session.id, "look at the repo")
    await bus.drain()
    await bus.stop()
    assert result.tool_call_count == 2


async def test_per_message_override_wins(bus, event_log, workspace):
    """@haiku at start of message picks haiku regardless of sticky."""
    adapter = _ScriptedAnthropicAdapter(
        [
            _ScriptedResponse(
                content=[TextBlock(text="quick answer")],
                stop_reason=StopReason.END_TURN,
            )
        ]
    )
    manager, _ = _build_manager(bus, adapter)
    session = manager.create_session(
        workspace_path=str(workspace),
        active_model="anthropic:claude-sonnet-4-6",
    )
    result = await manager.submit_turn(session.id, "@haiku what is 2+2?")
    await bus.drain()
    await bus.stop()
    assert result.chosen_model == "anthropic:claude-haiku-4-5"
    # The user message stored should have the @haiku prefix stripped.
    msgs = manager._store.get_messages(session.id)
    user_msgs = [m for m in msgs if m.role == Role.USER]
    assert user_msgs[-1].content[0].text == "what is 2+2?"


async def test_unknown_alias_raises_before_llm_call(bus, event_log, workspace):
    adapter = _ScriptedAnthropicAdapter([])
    manager, _ = _build_manager(bus, adapter)
    session = manager.create_session(workspace_path=str(workspace))
    with pytest.raises(UnknownAliasError):
        await manager.submit_turn(session.id, "@nope question")
    await bus.drain()
    await bus.stop()
    # No LLM call should have been attempted.
    assert adapter.requests == []


async def test_set_active_model_changes_sticky(bus, event_log, workspace):
    adapter = _ScriptedAnthropicAdapter(
        [
            _ScriptedResponse(content=[TextBlock(text="ok")], stop_reason=StopReason.END_TURN),
            _ScriptedResponse(content=[TextBlock(text="ok2")], stop_reason=StopReason.END_TURN),
        ]
    )
    manager, _ = _build_manager(bus, adapter)
    session = manager.create_session(workspace_path=str(workspace))
    manager.set_active_model(session.id, "haiku")
    r1 = await manager.submit_turn(session.id, "first")
    manager.set_active_model(session.id, "sonnet")
    r2 = await manager.submit_turn(session.id, "second")
    await bus.drain()
    await bus.stop()
    assert r1.chosen_model == "anthropic:claude-haiku-4-5"
    assert r2.chosen_model == "anthropic:claude-sonnet-4-6"


async def test_set_active_model_to_unknown_raises(bus, event_log, workspace):
    adapter = _ScriptedAnthropicAdapter([])
    manager, _ = _build_manager(bus, adapter)
    session = manager.create_session(workspace_path=str(workspace))
    with pytest.raises(UnknownAliasError):
        manager.set_active_model(session.id, "wildebeest")


async def test_cost_accumulates_across_turns(bus, event_log, workspace):
    adapter = _ScriptedAnthropicAdapter(
        [
            _ScriptedResponse(
                content=[TextBlock(text="a")],
                stop_reason=StopReason.END_TURN,
                input_tokens=1000,
                output_tokens=500,
            ),
            _ScriptedResponse(
                content=[TextBlock(text="b")],
                stop_reason=StopReason.END_TURN,
                input_tokens=2000,
                output_tokens=300,
            ),
        ]
    )
    manager, _ = _build_manager(bus, adapter)
    session = manager.create_session(workspace_path=str(workspace))
    r1 = await manager.submit_turn(session.id, "first")
    r2 = await manager.submit_turn(session.id, "second")
    await bus.drain()
    await bus.stop()
    expected_total = r1.cost_usd + r2.cost_usd
    fresh = manager._store.get_session(session.id)
    assert Decimal(str(fresh.cost_so_far_usd)).quantize(
        Decimal("0.0001")
    ) == expected_total.quantize(Decimal("0.0001"))
    assert fresh.turn_count == 2


async def test_assistant_message_metadata_includes_cost_and_routing(bus, event_log, workspace):
    adapter = _ScriptedAnthropicAdapter(
        [_ScriptedResponse(content=[TextBlock(text="answer")], stop_reason=StopReason.END_TURN)]
    )
    manager, _ = _build_manager(bus, adapter)
    session = manager.create_session(
        workspace_path=str(workspace), active_model="anthropic:claude-haiku-4-5"
    )
    await manager.submit_turn(session.id, "ask")
    await bus.drain()
    await bus.stop()
    msgs = manager._store.get_messages(session.id)
    assistant = next(m for m in msgs if m.role == Role.ASSISTANT)
    assert assistant.metadata.model == "anthropic:claude-haiku-4-5"
    assert assistant.metadata.provider == "anthropic"
    assert assistant.metadata.routing is not None
    assert assistant.metadata.routing.chosen_model == "anthropic:claude-haiku-4-5"
    assert assistant.metadata.usage is not None
    assert assistant.metadata.usage.cost_usd > 0
    assert assistant.metadata.usage.pricing_version == DEFAULT_PRICE_TABLE.version


async def test_hard_routing_failure_does_not_call_adapter(bus, event_log, workspace):
    adapter = _ScriptedAnthropicAdapter([])
    manager, _ = _build_manager(bus, adapter)
    # No default configured → routing chain has no candidates → RoutingError.
    manager._global_default_model = None  # type: ignore[assignment]
    session = manager.create_session(workspace_path=str(workspace))
    with pytest.raises(RoutingError):
        await manager.submit_turn(session.id, "hello")
    await bus.drain()
    await bus.stop()
    assert adapter.requests == []
    # route.decided is still emitted even on hard failure.
    assert any(e.type == "route.decided" for e in event_log)


async def test_assistant_text_returned_for_display(bus, event_log, workspace):
    """Smoke test: result.assistant_text contains the concatenated final text."""
    adapter = _ScriptedAnthropicAdapter(
        [
            _ScriptedResponse(
                content=[
                    TextBlock(text="line one"),
                    TextBlock(text="line two"),
                ],
                stop_reason=StopReason.END_TURN,
            )
        ]
    )
    manager, _ = _build_manager(bus, adapter)
    session = manager.create_session(workspace_path=str(workspace))
    result = await manager.submit_turn(session.id, "hi")
    await bus.drain()
    await bus.stop()
    assert result.assistant_text == "line one\nline two"
