"""Tests for built-in file_ops tools through the dispatcher."""

from __future__ import annotations

from pathlib import Path

import pytest

from metis.canonical.content import TextBlock, ToolUseBlock
from metis.events.bus import EventBus
from metis.tools.builtins.file_ops import (
    ListDirTool,
    PatchFileTool,
    ReadFileTool,
    WriteFileTool,
)
from metis.tools.dispatcher import ToolDispatcher


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "hello.txt").write_text("hello world")
    (tmp_path / "subdir").mkdir()
    (tmp_path / "subdir" / "nested.txt").write_text("inner")
    return tmp_path


@pytest.fixture
async def dispatcher() -> ToolDispatcher:
    bus = EventBus()
    bus.start()
    d = ToolDispatcher(bus)
    d.register(ReadFileTool)
    d.register(WriteFileTool)
    d.register(PatchFileTool)
    d.register(ListDirTool)
    return d


def _tu(name: str, **input: object) -> ToolUseBlock:
    return ToolUseBlock(id=f"tu_{name}", name=name, input=input)


async def test_read_file(dispatcher: ToolDispatcher, workspace: Path):
    result = await dispatcher.dispatch(
        _tu("read_file", path="hello.txt"),
        session_id="s",
        turn_id="t",
        workspace_path=str(workspace),
    )
    assert result.is_error is False
    assert isinstance(result.content[0], TextBlock)
    assert result.content[0].text == "hello world"


async def test_read_file_outside_workspace_rejected(dispatcher: ToolDispatcher, workspace: Path):
    result = await dispatcher.dispatch(
        _tu("read_file", path="../../etc/passwd"),
        session_id="s",
        turn_id="t",
        workspace_path=str(workspace),
    )
    assert result.is_error is True
    assert "escape" in result.content[0].text.lower()


async def test_write_file(dispatcher: ToolDispatcher, workspace: Path):
    result = await dispatcher.dispatch(
        _tu("write_file", path="new.txt", content="written"),
        session_id="s",
        turn_id="t",
        workspace_path=str(workspace),
    )
    assert result.is_error is False
    assert (workspace / "new.txt").read_text() == "written"


async def test_patch_file(dispatcher: ToolDispatcher, workspace: Path):
    result = await dispatcher.dispatch(
        _tu("patch_file", path="hello.txt", old="world", new="metis"),
        session_id="s",
        turn_id="t",
        workspace_path=str(workspace),
    )
    assert result.is_error is False
    assert (workspace / "hello.txt").read_text() == "hello metis"


async def test_patch_file_no_match(dispatcher: ToolDispatcher, workspace: Path):
    result = await dispatcher.dispatch(
        _tu("patch_file", path="hello.txt", old="missing", new="x"),
        session_id="s",
        turn_id="t",
        workspace_path=str(workspace),
    )
    assert result.is_error is True


async def test_list_dir(dispatcher: ToolDispatcher, workspace: Path):
    result = await dispatcher.dispatch(
        _tu("list_dir", path="."),
        session_id="s",
        turn_id="t",
        workspace_path=str(workspace),
    )
    assert result.is_error is False
    lines = result.content[0].text.split("\n")
    assert "hello.txt" in lines
    assert "subdir" in lines
