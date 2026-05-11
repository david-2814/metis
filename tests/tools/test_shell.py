"""Tests for the shell tool."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from metis.canonical.content import ToolUseBlock
from metis.canonical.tools import SideEffects
from metis.events.bus import EventBus
from metis.tools.builtins.shell import ShellTool
from metis.tools.confirmation import AutoAllowHandler
from metis.tools.dispatcher import ToolDispatcher


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
async def dispatcher() -> ToolDispatcher:
    bus = EventBus()
    bus.start()
    # Shell defaults to EXECUTE → PROMPT; AutoAllow keeps tests deterministic.
    d = ToolDispatcher(bus, confirmation_handler=AutoAllowHandler())
    d.register(ShellTool)
    return d


def _shell(command: str) -> ToolUseBlock:
    return ToolUseBlock(id="tu_shell", name="shell", input={"command": command})


async def test_shell_runs_command(dispatcher: ToolDispatcher, workspace: Path):
    result = await dispatcher.dispatch(
        _shell("echo hi"),
        session_id="s",
        turn_id="t",
        workspace_path=str(workspace),
    )
    assert result.is_error is False
    assert "hi" in result.content[0].text
    assert "exit_code=0" in result.content[0].text


async def test_shell_nonzero_exit_marks_failure(dispatcher: ToolDispatcher, workspace: Path):
    result = await dispatcher.dispatch(
        _shell("exit 7"),
        session_id="s",
        turn_id="t",
        workspace_path=str(workspace),
    )
    # is_error reflects the tool's success flag, which is False for non-zero
    # exit. The agent still gets the output for context.
    assert result.is_error is True
    assert "exit_code=7" in result.content[0].text


async def test_shell_runs_in_workspace_cwd(dispatcher: ToolDispatcher, workspace: Path):
    (workspace / "marker.txt").write_text("found")
    result = await dispatcher.dispatch(
        _shell("ls"),
        session_id="s",
        turn_id="t",
        workspace_path=str(workspace),
    )
    assert "marker.txt" in result.content[0].text


async def test_shell_timeout(workspace: Path):
    bus = EventBus()
    bus.start()
    d = ToolDispatcher(
        bus,
        confirmation_handler=AutoAllowHandler(),
        timeouts={SideEffects.EXECUTE: 0.2},
    )
    d.register(ShellTool)
    result = await d.dispatch(
        _shell("sleep 5"),
        session_id="s",
        turn_id="t",
        workspace_path=str(workspace),
    )
    await bus.drain()
    await bus.stop()
    assert result.is_error is True


async def test_shell_cancel_via_session(workspace: Path):
    bus = EventBus()
    bus.start()
    d = ToolDispatcher(bus, confirmation_handler=AutoAllowHandler())
    d.register(ShellTool)
    task = asyncio.create_task(
        d.dispatch(
            _shell("sleep 30"),
            session_id="cancel_me",
            turn_id="t",
            workspace_path=str(workspace),
        )
    )
    await asyncio.sleep(0.1)  # let the subprocess start
    await d.cancel_session_tools("cancel_me")
    # The shell tool's cancel() returns once SIGTERM/SIGKILL kills the
    # subprocess; dispatch() then completes with the partial output.
    result = await task
    await bus.drain()
    await bus.stop()
    # Killed processes return non-zero exit; we don't assert the exact code
    # since SIGTERM vs SIGKILL vary by platform.
    assert "exit_code=" in result.content[0].text
