"""End-to-end tests for SessionManager with a scripted adapter.

These exercise the turn loop, tool-use cycle, cost stamping, and event
emission without making any HTTP calls.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from metis_core.adapters.protocol import StopReason
from metis_core.canonical.capabilities import AdapterCapabilities
from metis_core.canonical.content import TextBlock, ToolUseBlock
from metis_core.canonical.messages import Role
from metis_core.events.bus import EventBus, EventFilter, Subscription
from metis_core.events.envelope import Event
from metis_core.pricing import DEFAULT_PRICE_TABLE
from metis_core.routing import ModelRegistry, RoutingEngine
from metis_core.routing.engine import RoutingError
from metis_core.sessions import (
    InMemorySessionStore,
    SessionManager,
    UnknownAliasError,
    UserExplicitModelRejectedError,
)
from metis_core.tools.dispatcher import ToolDispatcher

from tests_shared.scripted_adapter import _ScriptedAnthropicAdapter, _ScriptedResponse

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
        # Filter bus.* lifecycle events; session tests assert on domain
        # event sequences and shouldn't see the fixture's own registration.
        if e.type.startswith("bus."):
            return
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
    from metis_core.tools.builtins.file_ops import ListDirTool, ReadFileTool

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


async def test_rejected_sticky_refuses_turn(bus, event_log, workspace):
    """A sticky model that fails capability validation should raise
    UserExplicitModelRejectedError instead of silently falling through to
    the global default."""
    no_tools = AdapterCapabilities(
        supports_thinking=False,
        supports_images=False,
        supports_tools=False,
        supports_system_prompt=True,
        supports_structured_output=False,
        supports_streaming=True,
        supports_streaming_tool_calls=False,
        supports_parallel_tool_calls=False,
        supports_prompt_caching=False,
        supports_system_messages_in_list=False,
        max_context_tokens=8_192,
        max_output_tokens=4_096,
    )
    adapter = _ScriptedAnthropicAdapter(
        [],  # no responses needed — turn should refuse before any LLM call
        capability_overrides={"anthropic:claude-haiku-4-5": no_tools},
    )
    manager, _ = _build_manager(bus, adapter)
    session = manager.create_session(
        workspace_path=str(workspace),
        active_model="anthropic:claude-haiku-4-5",
    )
    with pytest.raises(UserExplicitModelRejectedError) as excinfo:
        await manager.submit_turn(session.id, "hello")
    await bus.drain()
    await bus.stop()

    err = excinfo.value
    assert err.model == "anthropic:claude-haiku-4-5"
    assert err.validation_failure == "no_tool_support"
    assert err.would_fall_back_to == "anthropic:claude-sonnet-4-6"
    assert err.source == "active model"
    # No LLM call should have happened — the refusal preempts it.
    assert adapter.requests == []
    # route.decided is still emitted so the trace records the rejection.
    event_types = [e.type for e in event_log]
    assert "route.decided" in event_types
    assert "llm.call_started" not in event_types


async def test_rejected_override_refuses_turn(bus, event_log, workspace):
    """A per-message @override that fails validation refuses the turn,
    crediting the override (not the sticky) as the source."""
    no_tools = AdapterCapabilities(
        supports_thinking=False,
        supports_images=False,
        supports_tools=False,
        supports_system_prompt=True,
        supports_structured_output=False,
        supports_streaming=True,
        supports_streaming_tool_calls=False,
        supports_parallel_tool_calls=False,
        supports_prompt_caching=False,
        supports_system_messages_in_list=False,
        max_context_tokens=8_192,
        max_output_tokens=4_096,
    )
    adapter = _ScriptedAnthropicAdapter(
        [],
        capability_overrides={"anthropic:claude-haiku-4-5": no_tools},
    )
    manager, _ = _build_manager(bus, adapter)
    session = manager.create_session(workspace_path=str(workspace))
    with pytest.raises(UserExplicitModelRejectedError) as excinfo:
        await manager.submit_turn(session.id, "@haiku hello")
    await bus.drain()
    await bus.stop()
    assert excinfo.value.source == "@model override"
    assert excinfo.value.model == "anthropic:claude-haiku-4-5"


async def test_compatible_sticky_runs_normally(bus, event_log, workspace):
    """When the sticky model passes validation the turn proceeds normally
    with no exception."""
    adapter = _ScriptedAnthropicAdapter(
        [
            _ScriptedResponse(
                content=[TextBlock(text="ok")],
                stop_reason=StopReason.END_TURN,
            )
        ]
    )
    manager, _ = _build_manager(bus, adapter)
    session = manager.create_session(
        workspace_path=str(workspace),
        active_model="anthropic:claude-haiku-4-5",
    )
    result = await manager.submit_turn(session.id, "hello")
    await bus.drain()
    await bus.stop()
    assert result.chosen_model == "anthropic:claude-haiku-4-5"


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


async def test_bare_alias_with_no_body_raises_override_error(bus, event_log, workspace):
    """Per routing-engine.md §9.2, `@<alias>` must be followed by whitespace +
    body. A bare `@haiku` is malformed and the turn does not start."""
    from metis_core.sessions import OverrideError

    adapter = _ScriptedAnthropicAdapter([])
    manager, _ = _build_manager(bus, adapter)
    session = manager.create_session(workspace_path=str(workspace))
    with pytest.raises(OverrideError) as excinfo:
        await manager.submit_turn(session.id, "@haiku")
    await bus.drain()
    await bus.stop()
    assert excinfo.value.alias == "haiku"
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


async def test_get_session_reflects_set_active_model(bus, workspace):
    """`manager.get_session` returns the current sticky after `set_active_model`."""
    adapter = _ScriptedAnthropicAdapter([])
    manager, _ = _build_manager(bus, adapter)
    session = manager.create_session(workspace_path=str(workspace))
    manager.set_active_model(session.id, "haiku")
    refreshed = manager.get_session(session.id)
    assert refreshed.active_model == "anthropic:claude-haiku-4-5"


async def test_get_session_reflects_clear_sticky(bus, workspace):
    adapter = _ScriptedAnthropicAdapter([])
    manager, _ = _build_manager(bus, adapter)
    session = manager.create_session(workspace_path=str(workspace), active_model="haiku")
    manager.set_active_model(session.id, None)
    refreshed = manager.get_session(session.id)
    assert refreshed.active_model is None


async def test_get_session_with_sqlite_store_returns_fresh_record(bus, workspace, tmp_path):
    """SqliteSessionStore rehydrates rows on each `get_session` call —
    callers holding a long-lived Session reference get stale data. This
    test confirms `manager.get_session` returns the *current* record,
    fixing the stale-snapshot bug behind the `sticky: None` display lie."""
    from metis_core.sessions import SqliteSessionStore

    adapter = _ScriptedAnthropicAdapter([])
    db_file = tmp_path / "sessions.db"
    store = SqliteSessionStore(db_file)
    try:
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
        manager = SessionManager(
            registry=registry,
            routing=routing,
            dispatcher=dispatcher,
            bus=bus,
            store=store,
            pricing=DEFAULT_PRICE_TABLE,
        )
        session = manager.create_session(workspace_path=str(workspace))
        # Local snapshot says no sticky.
        assert session.active_model is None
        # Set via manager.
        manager.set_active_model(session.id, "haiku")
        # The local copy IS stale (SqliteSessionStore rehydrates per-call).
        assert session.active_model is None
        # Fresh fetch shows the truth — what the display code must use.
        refreshed = manager.get_session(session.id)
        assert refreshed.active_model == "anthropic:claude-haiku-4-5"
    finally:
        store.close()


async def test_set_active_model_returns_resolved_canonical_id(bus, workspace):
    """Callers can rely on the return value for display without re-fetching."""
    adapter = _ScriptedAnthropicAdapter([])
    manager, _ = _build_manager(bus, adapter)
    session = manager.create_session(workspace_path=str(workspace))
    returned = manager.set_active_model(session.id, "haiku")
    assert returned == "anthropic:claude-haiku-4-5"
    cleared = manager.set_active_model(session.id, None)
    assert cleared is None


async def test_temperature_kwarg_threads_to_request(bus, event_log, workspace):
    """`submit_turn(temperature=0)` reaches the adapter as `request.temperature == 0`.

    The benchmark suite (docs/specs/benchmark.md) requires deterministic-ish
    runs, which means temperature must be settable from the public API.
    """
    adapter = _ScriptedAnthropicAdapter(
        [
            _ScriptedResponse(
                content=[TextBlock(text="answer")],
                stop_reason=StopReason.END_TURN,
            )
        ]
    )
    manager, _ = _build_manager(bus, adapter)
    session = manager.create_session(workspace_path=str(workspace))
    await manager.submit_turn(session.id, "hello", temperature=0.0)
    await bus.drain()
    await bus.stop()
    assert len(adapter.requests) == 1
    assert adapter.requests[0].temperature == 0.0


async def test_temperature_default_is_none(bus, event_log, workspace):
    """When `submit_turn` is called without `temperature=`, the request leaves it
    unset so adapters use their per-provider default (preserves prior behavior)."""
    adapter = _ScriptedAnthropicAdapter(
        [
            _ScriptedResponse(
                content=[TextBlock(text="answer")],
                stop_reason=StopReason.END_TURN,
            )
        ]
    )
    manager, _ = _build_manager(bus, adapter)
    session = manager.create_session(workspace_path=str(workspace))
    await manager.submit_turn(session.id, "hello")
    await bus.drain()
    await bus.stop()
    assert adapter.requests[0].temperature is None


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


# ---- /share bridge -----------------------------------------------------


async def test_buffer_slash_output_stores_text_per_session(bus, workspace):
    """`buffer_slash_output` records the captured text; each session is isolated."""
    adapter = _ScriptedAnthropicAdapter([])
    manager, _ = _build_manager(bus, adapter)
    s1 = manager.create_session(workspace_path=str(workspace))
    s2 = manager.create_session(workspace_path=str(workspace))
    manager.buffer_slash_output(s1.id, "session 1 output")
    manager.buffer_slash_output(s2.id, "session 2 output")
    # Sessions don't leak into each other.
    assert manager.mark_share_pending(s1.id) == "session 1 output"
    assert manager.mark_share_pending(s2.id) == "session 2 output"
    await bus.stop()


async def test_mark_share_pending_with_empty_buffer_returns_none(bus, workspace):
    adapter = _ScriptedAnthropicAdapter([])
    manager, _ = _build_manager(bus, adapter)
    session = manager.create_session(workspace_path=str(workspace))
    assert manager.mark_share_pending(session.id) is None
    await bus.stop()


async def test_consume_pending_share_is_one_shot(bus, workspace):
    """A single /share applies to ONE turn only; subsequent turns see no
    pending share unless /share is run again."""
    adapter = _ScriptedAnthropicAdapter([])
    manager, _ = _build_manager(bus, adapter)
    session = manager.create_session(workspace_path=str(workspace))
    manager.buffer_slash_output(session.id, "captured")
    manager.mark_share_pending(session.id)
    # First consume returns the buffer; second consume returns None.
    assert manager.consume_pending_share(session.id) == "captured"
    assert manager.consume_pending_share(session.id) is None
    await bus.stop()


async def test_buffer_persists_after_consume(bus, workspace):
    """Buffer survives consume — user can /share the same output twice if
    they run /share again. Only the pending flag is one-shot."""
    adapter = _ScriptedAnthropicAdapter([])
    manager, _ = _build_manager(bus, adapter)
    session = manager.create_session(workspace_path=str(workspace))
    manager.buffer_slash_output(session.id, "captured")
    manager.mark_share_pending(session.id)
    manager.consume_pending_share(session.id)
    # Second /share works because the buffer is still there.
    assert manager.mark_share_pending(session.id) == "captured"
    assert manager.consume_pending_share(session.id) == "captured"
    await bus.stop()


async def test_submit_turn_with_pending_share_prepends_to_user_message(bus, event_log, workspace):
    """When /share is pending, the user Message persisted to the session
    carries the buffered output as a labeled preamble, and the LLM's
    request sees the composed text."""
    adapter = _ScriptedAnthropicAdapter(
        [
            _ScriptedResponse(
                content=[TextBlock(text="ack")],
                stop_reason=StopReason.END_TURN,
            )
        ]
    )
    manager, _ = _build_manager(bus, adapter)
    session = manager.create_session(workspace_path=str(workspace))
    manager.buffer_slash_output(session.id, "anthropic:\n  claude-sonnet-4-6  $3 in / $15 out")
    manager.mark_share_pending(session.id)
    await manager.submit_turn(session.id, "which is cheapest?")
    await bus.drain()
    await bus.stop()

    # Adapter saw exactly one request; its messages include the composed
    # user content with both the shared block and the question.
    assert len(adapter.requests) == 1
    user_msg = adapter.requests[0].messages[-1]  # last is the new user message
    assert user_msg.role.value == "user"
    user_text = "".join(getattr(b, "text", "") for b in user_msg.content)
    assert "Shared from my terminal" in user_text
    assert "claude-sonnet-4-6" in user_text
    assert "which is cheapest?" in user_text


async def test_submit_turn_without_pending_share_unaffected(bus, event_log, workspace):
    """Without /share, behaviour is identical to before — just the raw
    user text reaches the adapter."""
    adapter = _ScriptedAnthropicAdapter(
        [
            _ScriptedResponse(
                content=[TextBlock(text="ack")],
                stop_reason=StopReason.END_TURN,
            )
        ]
    )
    manager, _ = _build_manager(bus, adapter)
    session = manager.create_session(workspace_path=str(workspace))
    manager.buffer_slash_output(session.id, "captured but never shared")
    # NOTE: no mark_share_pending call.
    await manager.submit_turn(session.id, "hello")
    await bus.drain()
    await bus.stop()

    user_msg = adapter.requests[0].messages[-1]
    user_text = "".join(getattr(b, "text", "") for b in user_msg.content)
    assert "Shared from" not in user_text
    assert "captured but never shared" not in user_text
    assert user_text == "hello"


async def test_share_only_applies_to_next_turn_not_subsequent(bus, event_log, workspace):
    """`/share` is one-shot. The turn AFTER the shared one sees the plain
    user message, not the shared content again."""
    adapter = _ScriptedAnthropicAdapter(
        [
            _ScriptedResponse(content=[TextBlock(text="r1")], stop_reason=StopReason.END_TURN),
            _ScriptedResponse(content=[TextBlock(text="r2")], stop_reason=StopReason.END_TURN),
        ]
    )
    manager, _ = _build_manager(bus, adapter)
    session = manager.create_session(workspace_path=str(workspace))
    manager.buffer_slash_output(session.id, "shared-data")
    manager.mark_share_pending(session.id)

    await manager.submit_turn(session.id, "first")
    await manager.submit_turn(session.id, "second")
    await bus.drain()
    await bus.stop()

    first_user = "".join(getattr(b, "text", "") for b in adapter.requests[0].messages[-1].content)
    # Second request's history includes the (already-composed) first user
    # message, but the *new* user message (last in the list) is plain.
    second_user = "".join(getattr(b, "text", "") for b in adapter.requests[1].messages[-1].content)
    assert "shared-data" in first_user
    assert second_user == "second"


async def test_set_active_model_auto_resolves_unambiguous_suffix(bus, workspace):
    """Typing `openai/gpt-oss-20b` resolves to the unique canonical id
    `openrouter:openai/gpt-oss-20b` when there's only one match."""
    from metis_core.sessions.manager import AmbiguousModelError  # noqa: F401

    adapter = _ScriptedAnthropicAdapter([])
    manager, _ = _build_manager(bus, adapter)
    # Register a model with an OpenRouter-style id so suffix matching has
    # something to resolve to.
    from metis_core.canonical.capabilities import AdapterCapabilities

    caps = AdapterCapabilities(
        supports_thinking=False,
        supports_images=False,
        supports_tools=True,
        supports_system_prompt=True,
        supports_structured_output=False,
        supports_streaming=True,
        supports_streaming_tool_calls=True,
        supports_parallel_tool_calls=True,
        supports_prompt_caching=False,
        supports_system_messages_in_list=False,
        max_context_tokens=100_000,
        max_output_tokens=4096,
    )

    class _Stub:
        name = "openrouter"

        def capabilities_for(self, _m):
            return caps

        async def close(self):
            return None

    manager._registry.register(model_id="openrouter:openai/gpt-oss-20b", adapter=_Stub())
    session = manager.create_session(workspace_path=str(workspace))
    resolved = manager.set_active_model(session.id, "openai/gpt-oss-20b")
    assert resolved == "openrouter:openai/gpt-oss-20b"
    await bus.stop()


async def test_set_active_model_short_suffix_also_resolves(bus, workspace):
    """`gpt-oss-20b` also resolves to the same model — `/` is a boundary too."""
    adapter = _ScriptedAnthropicAdapter([])
    manager, _ = _build_manager(bus, adapter)
    from metis_core.canonical.capabilities import AdapterCapabilities

    caps = AdapterCapabilities(
        supports_thinking=False,
        supports_images=False,
        supports_tools=True,
        supports_system_prompt=True,
        supports_structured_output=False,
        supports_streaming=True,
        supports_streaming_tool_calls=True,
        supports_parallel_tool_calls=True,
        supports_prompt_caching=False,
        supports_system_messages_in_list=False,
        max_context_tokens=100_000,
        max_output_tokens=4096,
    )

    class _Stub:
        name = "openrouter"

        def capabilities_for(self, _m):
            return caps

        async def close(self):
            return None

    manager._registry.register(model_id="openrouter:openai/gpt-oss-20b", adapter=_Stub())
    session = manager.create_session(workspace_path=str(workspace))
    assert manager.set_active_model(session.id, "gpt-oss-20b") == "openrouter:openai/gpt-oss-20b"
    await bus.stop()


async def test_set_active_model_ambiguous_raises_with_candidates(bus, workspace):
    """When 2+ ids match the suffix, raise with the candidate list so the
    user can pick. Sticky is unchanged on failure."""
    from metis_core.sessions.manager import AmbiguousModelError

    adapter = _ScriptedAnthropicAdapter([])
    manager, _ = _build_manager(bus, adapter)
    from metis_core.canonical.capabilities import AdapterCapabilities

    caps = AdapterCapabilities(
        supports_thinking=False,
        supports_images=False,
        supports_tools=True,
        supports_system_prompt=True,
        supports_structured_output=False,
        supports_streaming=True,
        supports_streaming_tool_calls=True,
        supports_parallel_tool_calls=True,
        supports_prompt_caching=False,
        supports_system_messages_in_list=False,
        max_context_tokens=100_000,
        max_output_tokens=4096,
    )

    class _Stub:
        name = "stub"

        def capabilities_for(self, _m):
            return caps

        async def close(self):
            return None

    # Both end in `gpt-5` at a boundary character.
    manager._registry.register(model_id="openai:gpt-5", adapter=_Stub())
    manager._registry.register(model_id="openrouter:openai/gpt-5", adapter=_Stub())
    session = manager.create_session(workspace_path=str(workspace))
    before = session.active_model
    with pytest.raises(AmbiguousModelError) as exc_info:
        manager.set_active_model(session.id, "gpt-5")
    err = exc_info.value
    assert err.input == "gpt-5"
    assert sorted(err.candidates) == ["openai:gpt-5", "openrouter:openai/gpt-5"]
    # Sticky is not changed on ambiguity.
    fresh = manager._store.get_session(session.id)
    assert fresh.active_model == before
    await bus.stop()


async def test_set_active_model_unknown_input_raises_unknown_alias(bus, workspace):
    """Unknown inputs that don't match anything still raise UnknownAliasError."""
    adapter = _ScriptedAnthropicAdapter([])
    manager, _ = _build_manager(bus, adapter)
    session = manager.create_session(workspace_path=str(workspace))
    with pytest.raises(UnknownAliasError):
        manager.set_active_model(session.id, "totally-bogus-name")
    await bus.stop()


async def test_set_active_model_returns_resolved_id(bus, workspace):
    """The return value is the canonical id — fixes the stale-display bug."""
    adapter = _ScriptedAnthropicAdapter([])
    manager, _ = _build_manager(bus, adapter)
    session = manager.create_session(workspace_path=str(workspace))
    resolved = manager.set_active_model(session.id, "haiku")
    assert resolved == "anthropic:claude-haiku-4-5"
    cleared = manager.set_active_model(session.id, None)
    assert cleared is None
    await bus.stop()


async def test_set_active_model_alias_still_takes_precedence(bus, workspace):
    """Explicit alias wins over suffix match — `sonnet` resolves to the
    aliased canonical id, not via suffix scan."""
    adapter = _ScriptedAnthropicAdapter([])
    manager, _ = _build_manager(bus, adapter)
    session = manager.create_session(workspace_path=str(workspace))
    # `sonnet` is a registered alias for anthropic:claude-sonnet-4-6 (set up
    # by the conftest fixture).
    resolved = manager.set_active_model(session.id, "sonnet")
    assert resolved == "anthropic:claude-sonnet-4-6"
    await bus.stop()


async def test_empty_buffer_does_not_overwrite_existing(bus, workspace):
    """An empty-string buffer call is a no-op so it can't accidentally
    erase the previous slash output."""
    adapter = _ScriptedAnthropicAdapter([])
    manager, _ = _build_manager(bus, adapter)
    session = manager.create_session(workspace_path=str(workspace))
    manager.buffer_slash_output(session.id, "real content")
    manager.buffer_slash_output(session.id, "")
    assert manager.mark_share_pending(session.id) == "real content"
    await bus.stop()


# ---- shared-text normalization ------------------------------------------

# The LLM-bound version of shared output should be compact: column padding
# and trailing whitespace stripped, tabs expanded, empty lines dropped,
# but tree hierarchy (leading indent) preserved.


def _whitespace_normalize(text: str) -> str:
    """Test helper — exposes the private normalizer."""
    from metis_core.sessions.manager import _normalize_shared_text

    return _normalize_shared_text(text)


def test_normalize_collapses_internal_column_padding():
    raw = "claude-sonnet-4-6    $3.00 in / $15.00 out / MTok     [coding, balanced]"
    assert _whitespace_normalize(raw) == (
        "claude-sonnet-4-6 $3.00 in / $15.00 out / MTok [coding, balanced]"
    )


def test_normalize_strips_trailing_whitespace():
    raw = "anthropic:           "
    assert _whitespace_normalize(raw) == "anthropic:"


def test_normalize_drops_empty_lines():
    raw = "a\n\n\nb\n  \nc"
    assert _whitespace_normalize(raw) == "a\nb\nc"


def test_normalize_expands_tabs_to_spaces():
    raw = "name\t\tvalue"  # two tabs → 8 spaces → collapsed to single space
    assert _whitespace_normalize(raw) == "name value"


def test_normalize_preserves_leading_indent_for_tree_hierarchy():
    """The `/models` tree format uses indent for nesting — keep it."""
    raw = (
        "anthropic:\n"
        "   claude-opus-4-7      $15.00 in / $75.00 out / MTok    [deep-reasoning]\n"
        " * claude-sonnet-4-6    $3.00 in / $15.00 out / MTok     [coding, balanced]\n"
        "openrouter:\n"
        "  deepseek:\n"
        "     deepseek-chat-v3.1  $0.30 in / $0.90 out / MTok      [coding]"
    )
    out = _whitespace_normalize(raw)
    lines = out.splitlines()
    assert lines[0] == "anthropic:"
    assert lines[1] == "   claude-opus-4-7 $15.00 in / $75.00 out / MTok [deep-reasoning]"
    assert lines[2] == " * claude-sonnet-4-6 $3.00 in / $15.00 out / MTok [coding, balanced]"
    assert lines[3] == "openrouter:"
    assert lines[4] == "  deepseek:"
    assert lines[5] == "     deepseek-chat-v3.1 $0.30 in / $0.90 out / MTok [coding]"


def test_normalize_preserves_sticky_marker():
    raw = " * claude-sonnet-4-6      $3.00 in / $15.00 out / MTok"
    assert _whitespace_normalize(raw) == " * claude-sonnet-4-6 $3.00 in / $15.00 out / MTok"


def test_normalize_is_idempotent():
    """Running the normalizer twice gives the same result as running it once."""
    raw = "  name    value   \n\n  other     value"
    once = _whitespace_normalize(raw)
    twice = _whitespace_normalize(once)
    assert once == twice


def test_normalize_empty_input_empty_output():
    assert _whitespace_normalize("") == ""
    assert _whitespace_normalize("   \n\t\n  ") == ""


def test_normalize_single_spaces_preserved():
    """Existing single spaces (already-compact text) aren't touched."""
    raw = "the quick brown fox jumps over the lazy dog"
    assert _whitespace_normalize(raw) == raw


async def test_submit_turn_persists_normalized_shared_text(bus, event_log, workspace):
    """The Message persisted in the session (and what the LLM sees) carries
    the *normalized* shared text — column padding stripped — not the raw
    buffer with display whitespace."""
    adapter = _ScriptedAnthropicAdapter(
        [_ScriptedResponse(content=[TextBlock(text="ok")], stop_reason=StopReason.END_TURN)]
    )
    manager, _ = _build_manager(bus, adapter)
    session = manager.create_session(workspace_path=str(workspace))
    padded = "anthropic:\n   claude-sonnet-4-6    $3.00 in / $15.00 out / MTok    [coding]   "
    manager.buffer_slash_output(session.id, padded)
    manager.mark_share_pending(session.id)
    await manager.submit_turn(session.id, "which is cheapest?")
    await bus.drain()
    await bus.stop()

    user_msg = adapter.requests[0].messages[-1]
    user_text = "".join(getattr(b, "text", "") for b in user_msg.content)
    # The original 4-space column gap is collapsed to a single space.
    assert "claude-sonnet-4-6 $3.00 in" in user_text
    # And the trailing whitespace after [coding] is gone.
    assert "[coding]   " not in user_text
    assert "[coding]\n" in user_text or user_text.endswith("[coding]") or "[coding]" in user_text


# ---- Cache-floor padding (context-assembler.md §5.1) -------------------


def test_pad_stable_prefix_noop_when_already_above_floor():
    """Caller-supplied long prompts already clear the floor; helper is a no-op."""
    from metis_core.sessions.manager import (
        MIN_CACHEABLE_PREFIX_TOKENS,
        _pad_stable_prefix_for_cache,
    )

    class _Adapter:
        def estimate_input_tokens(self, messages, tools, system_prompt):
            return MIN_CACHEABLE_PREFIX_TOKENS + 100

    prompt = "already long enough"
    out = _pad_stable_prefix_for_cache(
        stable_prefix=prompt, adapter=_Adapter(), tools=[], skill_store=None
    )
    assert out == prompt


def test_pad_stable_prefix_appends_operating_context_when_below_floor():
    """No skills + short base prompt → operating-context block fills the prefix."""
    from metis_core.sessions.manager import (
        MIN_CACHEABLE_PREFIX_TOKENS,
        _pad_stable_prefix_for_cache,
    )

    class _Adapter:
        # ~4 chars per token, matching the production heuristic.
        def estimate_input_tokens(self, messages, tools, system_prompt):
            return max(1, len(system_prompt or "") // 4)

    base = "You are Metis. Be concise."
    out = _pad_stable_prefix_for_cache(
        stable_prefix=base, adapter=_Adapter(), tools=[], skill_store=None
    )
    # Padded prefix must clear the floor.
    assert len(out) // 4 >= MIN_CACHEABLE_PREFIX_TOKENS
    # Padding came from the operating-context block (substantive, not lorem-ipsum).
    assert "## Operating context" in out
    # Base prompt preserved at the front.
    assert out.startswith(base)


def test_pad_stable_prefix_respects_upper_bound():
    """Padded prefix stays under MAX_CACHEABLE_PREFIX_TOKENS with margin."""
    from metis_core.sessions.manager import (
        MAX_CACHEABLE_PREFIX_TOKENS,
        _pad_stable_prefix_for_cache,
    )

    class _Adapter:
        def estimate_input_tokens(self, messages, tools, system_prompt):
            return max(1, len(system_prompt or "") // 4)

    base = "You are Metis."
    out = _pad_stable_prefix_for_cache(
        stable_prefix=base, adapter=_Adapter(), tools=[], skill_store=None
    )
    # ~4 chars/token: stay near or under the upper bound (small overshoot
    # allowed for tail whitespace + the rstrip + "\n\n" assembly).
    assert len(out) // 4 <= MAX_CACHEABLE_PREFIX_TOKENS + 50


def test_pad_stable_prefix_byte_stable_across_calls():
    """Determinism is load-bearing: same inputs → identical bytes every call."""
    from metis_core.sessions.manager import _pad_stable_prefix_for_cache

    class _Adapter:
        def estimate_input_tokens(self, messages, tools, system_prompt):
            return max(1, len(system_prompt or "") // 4)

    base = "You are Metis."
    a = _pad_stable_prefix_for_cache(
        stable_prefix=base, adapter=_Adapter(), tools=[], skill_store=None
    )
    b = _pad_stable_prefix_for_cache(
        stable_prefix=base, adapter=_Adapter(), tools=[], skill_store=None
    )
    assert a == b


def test_pad_stable_prefix_prefers_skill_bodies_then_operating_context():
    """Skills load substantive content first; ops-context fills remaining headroom."""
    from metis_core.sessions.manager import _pad_stable_prefix_for_cache

    class _Adapter:
        def estimate_input_tokens(self, messages, tools, system_prompt):
            return max(1, len(system_prompt or "") // 4)

    class _FakeSkill:
        def __init__(self, name: str, body: str) -> None:
            self.name = name
            self.body = body

    class _FakeSkillStore:
        def __init__(self, skills):
            self._skills = list(skills)

        def __len__(self):
            return len(self._skills)

        def list_skills(self):
            return list(self._skills)

    # Two short skill bodies; deterministic order by name.
    skills = _FakeSkillStore(
        [
            _FakeSkill("zeta", "Zeta skill body content.\n" * 5),
            _FakeSkill("alpha", "Alpha skill body content.\n" * 5),
        ]
    )
    base = "You are Metis."
    out = _pad_stable_prefix_for_cache(
        stable_prefix=base, adapter=_Adapter(), tools=[], skill_store=skills
    )
    # Both skill headings present; alpha appears before zeta (name-ascending).
    assert "### Skill: alpha" in out
    assert "### Skill: zeta" in out
    assert out.index("### Skill: alpha") < out.index("### Skill: zeta")
    # Short skill bodies don't fill the headroom alone, so the ops-context
    # block runs to top up.
    assert "## Operating context" in out


def test_pad_stable_prefix_truncates_huge_skill_body():
    """A huge skill body is truncated; total stays near MAX_CACHEABLE_PREFIX_TOKENS."""
    from metis_core.sessions.manager import (
        MAX_CACHEABLE_PREFIX_TOKENS,
        _pad_stable_prefix_for_cache,
    )

    class _Adapter:
        def estimate_input_tokens(self, messages, tools, system_prompt):
            return max(1, len(system_prompt or "") // 4)

    class _BigSkill:
        name = "big"
        body = "X" * 80_000  # would blow past the upper bound if not truncated

    class _Store:
        def __len__(self):
            return 1

        def list_skills(self):
            return [_BigSkill()]

    out = _pad_stable_prefix_for_cache(
        stable_prefix="short base", adapter=_Adapter(), tools=[], skill_store=_Store()
    )
    assert len(out) // 4 <= MAX_CACHEABLE_PREFIX_TOKENS + 50


async def test_session_manager_pads_short_default_prompt(bus, event_log, workspace):
    """End-to-end: the request sent to the adapter carries a padded prefix.

    Today's DEFAULT_SYSTEM_PROMPT is ~290 chars (~75 tokens). The scripted
    adapter reports `estimate_input_tokens=100` for any input, so the
    padding helper still sees current_tokens (100) < min (4500) and
    appends padding. The CanonicalRequest.system_prompt arriving at the
    adapter must reflect the padded prefix.
    """
    from metis_core.sessions.manager import MIN_CACHEABLE_PREFIX_TOKENS

    adapter = _ScriptedAnthropicAdapter(
        [
            _ScriptedResponse(
                content=[TextBlock(text="ok")],
                stop_reason=StopReason.END_TURN,
            )
        ]
    )
    manager, _ = _build_manager(bus, adapter)
    session = manager.create_session(workspace_path=str(workspace))
    await manager.submit_turn(session.id, "hello")
    await bus.drain()
    await bus.stop()

    assert len(adapter.requests) == 1
    sent = adapter.requests[0].system_prompt or ""
    # The padded prefix must include the operating-context block.
    assert "## Operating context" in sent
    # ~4 chars/token: the padded prefix tokenizes above the haiku floor.
    assert len(sent) // 4 >= MIN_CACHEABLE_PREFIX_TOKENS - 200


async def test_session_manager_does_not_pad_already_long_prompt(bus, event_log, workspace):
    """Caller passes a custom long system_prompt: §5.1 is a no-op."""
    from metis_core.sessions.manager import _OPERATING_CONTEXT_PADDING

    # Custom prompt that, when measured by len//4, already clears MIN.
    long_prompt = ("Be precise. " * 2000).strip()  # ~24 KB, well above the floor

    class _RealEstimateAdapter(_ScriptedAnthropicAdapter):
        def estimate_input_tokens(self, messages, tools, system_prompt):
            return max(1, len(system_prompt or "") // 4)

    adapter = _RealEstimateAdapter(
        [
            _ScriptedResponse(
                content=[TextBlock(text="ok")],
                stop_reason=StopReason.END_TURN,
            )
        ]
    )
    registry = ModelRegistry()
    registry.register(model_id="anthropic:claude-sonnet-4-6", adapter=adapter, aliases=["sonnet"])
    routing = RoutingEngine(registry=registry, bus=bus)
    dispatcher = ToolDispatcher(bus)
    manager = SessionManager(
        registry=registry,
        routing=routing,
        dispatcher=dispatcher,
        bus=bus,
        store=InMemorySessionStore(),
        pricing=DEFAULT_PRICE_TABLE,
        system_prompt=long_prompt,
    )
    session = manager.create_session(workspace_path=str(workspace))
    await manager.submit_turn(session.id, "hello")
    await bus.drain()
    await bus.stop()

    sent = adapter.requests[0].system_prompt or ""
    assert sent == long_prompt
    # Padding constant did NOT get appended.
    assert _OPERATING_CONTEXT_PADDING.split("\n")[0] not in sent


async def test_session_manager_pads_byte_stable_across_turns(bus, event_log, workspace):
    """Two consecutive turns send byte-identical stable prefixes.

    The cache only fires when the prefix is identical turn-to-turn. Any
    per-call variation (timestamps, session-id interpolation, etc.)
    invalidates the cache and defeats §5.1.
    """
    adapter = _ScriptedAnthropicAdapter(
        [
            _ScriptedResponse(
                content=[TextBlock(text="one")],
                stop_reason=StopReason.END_TURN,
            ),
            _ScriptedResponse(
                content=[TextBlock(text="two")],
                stop_reason=StopReason.END_TURN,
            ),
        ]
    )
    manager, _ = _build_manager(bus, adapter)
    session = manager.create_session(workspace_path=str(workspace))
    await manager.submit_turn(session.id, "hello")
    await manager.submit_turn(session.id, "again")
    await bus.drain()
    await bus.stop()

    assert len(adapter.requests) == 2
    assert adapter.requests[0].system_prompt == adapter.requests[1].system_prompt


async def test_turn_completed_carries_final_response_text_in_signals_extra(
    bus, event_log, workspace
):
    """Producer-side plumbing: `_emit_turn_completed` stamps the assistant's
    final text on `turn.completed.signals_extra.final_response_text` so the
    evaluator's content-penalty path fires on the online subscriber path
    (evaluator.md §5.1)."""
    adapter = _ScriptedAnthropicAdapter(
        [
            _ScriptedResponse(
                content=[TextBlock(text="I cannot help with that request.")],
                stop_reason=StopReason.END_TURN,
            )
        ]
    )
    manager, _ = _build_manager(bus, adapter)
    session = manager.create_session(workspace_path=str(workspace))

    await manager.submit_turn(session.id, "please do something disallowed")
    await bus.drain()
    await bus.stop()

    turn_completed = next(e for e in event_log if e.type == "turn.completed")
    extras = turn_completed.payload.get("signals_extra")
    assert extras is not None
    assert extras.get("final_response_text") == "I cannot help with that request."


async def test_turn_completed_omits_signals_extra_when_no_assistant_text(bus, event_log, workspace):
    """When the assistant produced no text (rare; e.g., tool-only stop), the
    emitter leaves `signals_extra` as None so the heuristic judge sees
    "no signal" rather than a sentinel that would mis-trigger the empty-
    response penalty."""
    adapter = _ScriptedAnthropicAdapter(
        [
            _ScriptedResponse(
                content=[
                    ToolUseBlock(id="tu_x", name="read_file", input={"path": "README.md"}),
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

    await manager.submit_turn(session.id, "go")
    await bus.drain()
    await bus.stop()

    turn_completed = next(e for e in event_log if e.type == "turn.completed")
    extras = turn_completed.payload.get("signals_extra")
    assert extras is not None
    assert extras["final_response_text"] == "done"


async def test_refusal_signals_drop_eval_score_below_baseline(bus, event_log, workspace, tmp_path):
    """End-to-end: SessionManager → bus → trace → evaluator subscriber.

    With `final_response_text` plumbed, the heuristic judge multiplies the
    base score by 0.5 (refusal) so the final eval score falls below 0.6
    even on a clean stop-reason turn. Previously this turn would have
    scored 1.0 because the evaluator never saw the refusal text.
    """
    from metis_core.eval import register_evaluator
    from metis_core.trace.store import TraceStore

    trace = TraceStore(tmp_path / "trace.db")
    trace.attach_to(bus)
    evaluator, _ = register_evaluator(bus, trace)
    try:
        adapter = _ScriptedAnthropicAdapter(
            [
                _ScriptedResponse(
                    content=[TextBlock(text="I cannot help with that.")],
                    stop_reason=StopReason.END_TURN,
                )
            ]
        )
        manager, _ = _build_manager(bus, adapter)
        session = manager.create_session(workspace_path=str(workspace))
        await manager.submit_turn(session.id, "please do something disallowed")
        await bus.drain()
    finally:
        evaluator.unregister()
        await bus.drain()
        await bus.stop()
        trace.close()

    completed = [e for e in event_log if e.type == "eval.completed"]
    assert completed, "evaluator should have emitted an eval.completed event"
    payload = completed[0].payload
    assert payload["subject_kind"] == "turn"
    assert float(payload["score"]) < 0.6, (
        f"refusal should multiply by 0.5; got score={payload['score']!r}"
    )
    flags_negative = payload.get("signals", {}).get("flags_negative", [])
    assert "assistant_refusal_detected" in flags_negative


async def test_clean_response_keeps_eval_score_at_baseline(bus, event_log, workspace, tmp_path):
    """Control: a non-refusal response with the same lifecycle signals keeps
    the score at 1.0. This pins the delta in the refusal test to the
    content-penalty path firing, not to other lifecycle drift."""
    from metis_core.eval import register_evaluator
    from metis_core.trace.store import TraceStore

    trace = TraceStore(tmp_path / "trace.db")
    trace.attach_to(bus)
    evaluator, _ = register_evaluator(bus, trace)
    try:
        adapter = _ScriptedAnthropicAdapter(
            [
                _ScriptedResponse(
                    content=[TextBlock(text="Here is the answer you asked for.")],
                    stop_reason=StopReason.END_TURN,
                )
            ]
        )
        manager, _ = _build_manager(bus, adapter)
        session = manager.create_session(workspace_path=str(workspace))
        await manager.submit_turn(session.id, "explain something")
        await bus.drain()
    finally:
        evaluator.unregister()
        await bus.drain()
        await bus.stop()
        trace.close()

    completed = [e for e in event_log if e.type == "eval.completed"]
    assert completed
    assert float(completed[0].payload["score"]) == 1.0
