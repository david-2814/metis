"""Tests for built-in file_ops tools through the dispatcher."""

from __future__ import annotations

from pathlib import Path

import pytest
from metis.core.canonical.content import TextBlock, ToolUseBlock
from metis.core.events.bus import EventBus
from metis.core.tools.builtins.file_ops import (
    ListDirTool,
    PatchFileTool,
    ReadFileTool,
    WriteFileTool,
)
from metis.core.tools.dispatcher import ToolDispatcher


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


# ---- read_file slicing (tool-dispatcher.md §5.5) ------------------------


async def test_read_file_with_offset(dispatcher: ToolDispatcher, workspace: Path):
    (workspace / "multi.txt").write_text("a\nb\nc\nd\ne\n")
    result = await dispatcher.dispatch(
        _tu("read_file", path="multi.txt", offset=3),
        session_id="s",
        turn_id="t",
        workspace_path=str(workspace),
    )
    assert result.is_error is False
    text = result.content[0].text
    assert "(showing lines 3-5 of 5)" in text
    assert text.endswith("c\nd\ne\n")


async def test_read_file_with_limit(dispatcher: ToolDispatcher, workspace: Path):
    (workspace / "multi.txt").write_text("a\nb\nc\nd\ne\n")
    result = await dispatcher.dispatch(
        _tu("read_file", path="multi.txt", limit=2),
        session_id="s",
        turn_id="t",
        workspace_path=str(workspace),
    )
    assert result.is_error is False
    text = result.content[0].text
    assert "(showing lines 1-2 of 5)" in text
    # Third line must not have leaked into the slice.
    assert text.endswith("a\nb\n")


async def test_read_file_with_offset_and_limit(dispatcher: ToolDispatcher, workspace: Path):
    (workspace / "multi.txt").write_text("a\nb\nc\nd\ne\n")
    result = await dispatcher.dispatch(
        _tu("read_file", path="multi.txt", offset=2, limit=2),
        session_id="s",
        turn_id="t",
        workspace_path=str(workspace),
    )
    assert result.is_error is False
    text = result.content[0].text
    assert "(showing lines 2-3 of 5)" in text
    assert text.endswith("b\nc\n")


async def test_read_file_offset_past_end(dispatcher: ToolDispatcher, workspace: Path):
    (workspace / "multi.txt").write_text("a\nb\nc\n")
    result = await dispatcher.dispatch(
        _tu("read_file", path="multi.txt", offset=99),
        session_id="s",
        turn_id="t",
        workspace_path=str(workspace),
    )
    assert result.is_error is False
    text = result.content[0].text
    assert "past end" in text
    assert "3 line" in text


async def test_read_file_no_slicing_args_is_back_compat(
    dispatcher: ToolDispatcher, workspace: Path
):
    """Calling read_file without offset/limit returns the raw file text —
    no header, byte-identical to the pre-slicing behavior."""
    (workspace / "multi.txt").write_text("a\nb\nc\n")
    result = await dispatcher.dispatch(
        _tu("read_file", path="multi.txt"),
        session_id="s",
        turn_id="t",
        workspace_path=str(workspace),
    )
    assert result.is_error is False
    assert result.content[0].text == "a\nb\nc\n"
