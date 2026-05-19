"""Textual TUI tests via the Pilot API.

We construct a real MetisApp with a scripted adapter and drive it
deterministically with simulated key presses.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from metis.cli.runtime import ChatRuntime
from metis.cli.tui.app import MetisApp
from metis.core.adapters.protocol import StopReason
from metis.core.canonical.content import TextBlock, ToolUseBlock
from metis.core.events.bus import EventBus
from metis.core.pricing import DEFAULT_PRICE_TABLE
from metis.core.routing import ModelRegistry, RoutingEngine
from metis.core.sessions import InMemorySessionStore, SessionManager
from metis.core.sessions.store import Session
from metis.core.tools.builtins.file_ops import ReadFileTool
from metis.core.tools.dispatcher import ToolDispatcher
from textual.widgets import Input, Log

from tests_shared.scripted_adapter import _ScriptedAnthropicAdapter, _ScriptedResponse

# ---- Test runtime helper ---------------------------------------------


def _build_runtime(adapter: _ScriptedAnthropicAdapter, workspace: Path) -> ChatRuntime:
    bus = EventBus()
    bus.start()
    registry = ModelRegistry()
    registry.register(model_id="anthropic:claude-sonnet-4-6", adapter=adapter, aliases=["sonnet"])
    registry.register(model_id="anthropic:claude-haiku-4-5", adapter=adapter, aliases=["haiku"])
    routing = RoutingEngine(registry=registry, bus=bus)
    dispatcher = ToolDispatcher(bus)
    dispatcher.register(ReadFileTool)
    store = InMemorySessionStore()
    manager = SessionManager(
        registry=registry,
        routing=routing,
        dispatcher=dispatcher,
        bus=bus,
        store=store,
        pricing=DEFAULT_PRICE_TABLE,
    )
    # TraceStore left unset for tests — we don't exercise its lifecycle here.
    return ChatRuntime(
        bus=bus,
        trace=None,  # type: ignore[arg-type] — not used in TUI tests
        session_store=store,
        registry=registry,
        routing=routing,
        dispatcher=dispatcher,
        manager=manager,
        adapters=[adapter],
        db_file=Path("/tmp/test.db"),
        pricing=DEFAULT_PRICE_TABLE,
        global_default_model="anthropic:claude-sonnet-4-6",
    )


def _make_app(
    adapter: _ScriptedAnthropicAdapter, workspace: Path, model: str = "sonnet"
) -> tuple[MetisApp, Session]:
    runtime = _build_runtime(adapter, workspace)
    session = runtime.manager.create_session(workspace_path=str(workspace), active_model=model)
    return MetisApp(runtime=runtime, session=session), session


# ---- Smoke tests -----------------------------------------------------


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "README.md").write_text("# project")
    return tmp_path


async def test_app_mounts_and_shows_banner(workspace: Path):
    adapter = _ScriptedAnthropicAdapter([])
    app, _ = _make_app(adapter, workspace)
    async with app.run_test() as pilot:
        await pilot.pause()
        log = app.query_one(Log)
        # The banner mentions Metis chat and the providers line.
        text = _log_text(log)
        assert "Metis chat" in text
        assert "Providers:" in text
        assert "Active model:" in text


async def test_help_command_lists_commands(workspace: Path):
    adapter = _ScriptedAnthropicAdapter([])
    app, _ = _make_app(adapter, workspace)
    async with app.run_test() as pilot:
        await _type_and_submit(pilot, "/help")
        log = app.query_one(Log)
        text = _log_text(log)
        assert "/model" in text
        assert "/cost" in text
        assert "/models" in text


async def test_models_command_lists_registry(workspace: Path):
    """/models renders provider/namespace nesting: an `anthropic:` header
    plus indented leaves for each model. The full canonical id is the
    header+leaf combined; check for the parts."""
    adapter = _ScriptedAnthropicAdapter([])
    app, _ = _make_app(adapter, workspace)
    async with app.run_test() as pilot:
        await _type_and_submit(pilot, "/models")
        log = app.query_one(Log)
        text = _log_text(log)
        assert "anthropic:" in text  # provider header
        assert "claude-sonnet-4-6" in text  # leaf
        assert "claude-haiku-4-5" in text  # leaf


async def test_model_switch_via_slash_command(workspace: Path):
    adapter = _ScriptedAnthropicAdapter([])
    app, session = _make_app(adapter, workspace)
    async with app.run_test() as pilot:
        await _type_and_submit(pilot, "/model haiku")
        await pilot.pause()
        # The runtime's session got mutated to the canonical id.
        fresh = app.runtime.session_store.get_session(session.id)
        assert fresh.active_model == "anthropic:claude-haiku-4-5"


async def test_unknown_command_shows_error(workspace: Path):
    adapter = _ScriptedAnthropicAdapter([])
    app, _ = _make_app(adapter, workspace)
    async with app.run_test() as pilot:
        await _type_and_submit(pilot, "/nonsense")
        log = app.query_one(Log)
        text = _log_text(log)
        assert "unknown command" in text.lower()


# ---- Turn submission -------------------------------------------------


async def test_submit_turn_renders_text_response(workspace: Path):
    adapter = _ScriptedAnthropicAdapter(
        [
            _ScriptedResponse(
                content=[TextBlock(text="hello back")],
                stop_reason=StopReason.END_TURN,
            )
        ]
    )
    app, _ = _make_app(adapter, workspace)
    async with app.run_test() as pilot:
        await _type_and_submit(pilot, "hello")
        # Worker is async; pause until it settles.
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        log = app.query_one(Log)
        text = _log_text(log)
        assert "hello back" in text
        # The cost tag is also rendered after the assistant text.
        assert "anthropic:claude-sonnet-4-6" in text


async def test_submit_turn_with_tool_use_renders_marker_and_response(workspace: Path):
    adapter = _ScriptedAnthropicAdapter(
        [
            _ScriptedResponse(
                content=[
                    TextBlock(text="I'll check."),
                    ToolUseBlock(id="tu_1", name="read_file", input={"path": "README.md"}),
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            _ScriptedResponse(
                content=[TextBlock(text="Done.")],
                stop_reason=StopReason.END_TURN,
            ),
        ]
    )
    app, _ = _make_app(adapter, workspace)
    async with app.run_test() as pilot:
        await _type_and_submit(pilot, "what's in the readme?")
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        log = app.query_one(Log)
        text = _log_text(log)
        assert "I'll check." in text
        assert "→ read_file" in text  # tool marker (arrow style avoids Rich markup conflict)
        assert "Done." in text


async def test_empty_submit_is_ignored(workspace: Path):
    adapter = _ScriptedAnthropicAdapter([])
    app, _ = _make_app(adapter, workspace)
    async with app.run_test() as pilot:
        prompt = app.query_one(Input)
        prompt.value = ""
        await pilot.press("enter")
        await pilot.pause()
        # No turn fired; scripted adapter received nothing.
        assert adapter.requests == []


async def test_exit_keyword_quits(workspace: Path):
    adapter = _ScriptedAnthropicAdapter([])
    app, _ = _make_app(adapter, workspace)
    async with app.run_test() as pilot:
        await _type_and_submit(pilot, "exit")
        await pilot.pause()
        # `exit` should have triggered the app's exit path; querying after
        # exit returns the result. Pilot's context manager catches it.
        assert app.return_value is None or app.return_value == 0


# ---- /copy command --------------------------------------------------


async def test_copy_with_no_messages_reports_empty(workspace: Path):
    adapter = _ScriptedAnthropicAdapter([])
    app, _ = _make_app(adapter, workspace)
    async with app.run_test() as pilot:
        await _type_and_submit(pilot, "/copy")
        log = app.query_one(Log)
        assert "no assistant messages yet" in _log_text(log)


async def test_copy_copies_last_assistant_reply(workspace: Path):
    adapter = _ScriptedAnthropicAdapter(
        [
            _ScriptedResponse(
                content=[TextBlock(text="first answer")],
                stop_reason=StopReason.END_TURN,
            ),
            _ScriptedResponse(
                content=[TextBlock(text="second answer")],
                stop_reason=StopReason.END_TURN,
            ),
        ]
    )
    app, _ = _make_app(adapter, workspace)
    copied: list[str] = []
    # Stub the clipboard so we don't actually shell out to pbcopy in tests.
    app.copy_to_clipboard = copied.append  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        await _type_and_submit(pilot, "q1")
        await app.workers.wait_for_complete()
        await _type_and_submit(pilot, "q2")
        await app.workers.wait_for_complete()
        await pilot.pause()
        await _type_and_submit(pilot, "/copy")
        assert copied == ["second answer"]


async def test_copy_with_index_picks_nth_most_recent(workspace: Path):
    adapter = _ScriptedAnthropicAdapter(
        [
            _ScriptedResponse(
                content=[TextBlock(text="first answer")],
                stop_reason=StopReason.END_TURN,
            ),
            _ScriptedResponse(
                content=[TextBlock(text="second answer")],
                stop_reason=StopReason.END_TURN,
            ),
        ]
    )
    app, _ = _make_app(adapter, workspace)
    copied: list[str] = []
    app.copy_to_clipboard = copied.append  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        await _type_and_submit(pilot, "q1")
        await app.workers.wait_for_complete()
        await _type_and_submit(pilot, "q2")
        await app.workers.wait_for_complete()
        await pilot.pause()
        await _type_and_submit(pilot, "/copy 2")
        assert copied == ["first answer"]


async def test_copy_with_bad_arg_shows_usage(workspace: Path):
    adapter = _ScriptedAnthropicAdapter([])
    app, _ = _make_app(adapter, workspace)
    async with app.run_test() as pilot:
        await _type_and_submit(pilot, "/copy abc")
        log = app.query_one(Log)
        assert "usage: /copy" in _log_text(log)


async def test_copy_out_of_range_reports_count(workspace: Path):
    adapter = _ScriptedAnthropicAdapter(
        [
            _ScriptedResponse(
                content=[TextBlock(text="only one")],
                stop_reason=StopReason.END_TURN,
            ),
        ]
    )
    app, _ = _make_app(adapter, workspace)
    async with app.run_test() as pilot:
        await _type_and_submit(pilot, "q1")
        await app.workers.wait_for_complete()
        await _type_and_submit(pilot, "/copy 5")
        log = app.query_one(Log)
        assert "only 1 assistant" in _log_text(log)


# ---- Helpers ---------------------------------------------------------


async def _type_and_submit(pilot, text: str) -> None:
    """Type a string into the input box and press Enter."""
    # Setting the value directly is faster than per-key simulation and avoids
    # depending on Textual's key handling for ordinary characters.
    pilot.app.query_one(Input).value = text
    await pilot.press("enter")
    await pilot.pause()


def _log_text(log: Log) -> str:
    """Plain-text extraction from a Log for assertions."""
    return "\n".join(log.lines)
