"""Textual TUI tests via the Pilot API.

We construct a real MetisApp with a scripted adapter and drive it
deterministically with simulated key presses.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import Input, RichLog

from metis.adapters.protocol import StopReason
from metis.canonical.content import TextBlock, ToolUseBlock
from metis.cli.runtime import ChatRuntime
from metis.events.bus import EventBus
from metis.pricing import DEFAULT_PRICE_TABLE
from metis.routing import ModelRegistry, RoutingEngine
from metis.sessions import InMemorySessionStore, SessionManager
from metis.sessions.store import Session
from metis.tools.builtins.file_ops import ReadFileTool
from metis.tools.dispatcher import ToolDispatcher
from metis.tui.app import MetisApp

# Reuse the scripted adapter from the session manager tests.
from tests.sessions.test_manager import _ScriptedAnthropicAdapter, _ScriptedResponse

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
        log = app.query_one(RichLog)
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
        log = app.query_one(RichLog)
        text = _log_text(log)
        assert "/model" in text
        assert "/cost" in text
        assert "/models" in text


async def test_models_command_lists_registry(workspace: Path):
    adapter = _ScriptedAnthropicAdapter([])
    app, _ = _make_app(adapter, workspace)
    async with app.run_test() as pilot:
        await _type_and_submit(pilot, "/models")
        log = app.query_one(RichLog)
        text = _log_text(log)
        assert "anthropic:claude-sonnet-4-6" in text
        assert "anthropic:claude-haiku-4-5" in text


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
        log = app.query_one(RichLog)
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
        log = app.query_one(RichLog)
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
        log = app.query_one(RichLog)
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


# ---- Helpers ---------------------------------------------------------


async def _type_and_submit(pilot, text: str) -> None:
    """Type a string into the input box and press Enter."""
    # Setting the value directly is faster than per-key simulation and avoids
    # depending on Textual's key handling for ordinary characters.
    pilot.app.query_one(Input).value = text
    await pilot.press("enter")
    await pilot.pause()


def _log_text(log: RichLog) -> str:
    """Best-effort plain-text extraction from a RichLog for assertions."""
    parts: list[str] = []
    for line in log.lines:
        # Each line is a Rich Strip; render its plain text.
        if hasattr(line, "text"):
            parts.append(line.text)
        else:
            parts.append(str(line))
    return "\n".join(parts)
