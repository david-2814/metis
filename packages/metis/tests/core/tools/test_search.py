"""Tests for the grep_files search tool (tool-dispatcher.md §5.5)."""

from __future__ import annotations

from pathlib import Path

import pytest
from metis.core.canonical.content import ToolUseBlock
from metis.core.events.bus import EventBus
from metis.core.tools.builtins.search import GrepFilesTool
from metis.core.tools.dispatcher import ToolDispatcher


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "a.py").write_text("def hello():\n    return 'world'\n")
    (tmp_path / "b.py").write_text("def goodbye():\n    return 'world'\n")
    (tmp_path / "notes.md").write_text("Hello, World!\nA second line.\n")
    sub = tmp_path / "src"
    sub.mkdir()
    (sub / "deep.py").write_text("# hello deep\n")
    return tmp_path


@pytest.fixture
async def dispatcher() -> ToolDispatcher:
    bus = EventBus()
    bus.start()
    d = ToolDispatcher(bus)
    d.register(GrepFilesTool)
    return d


def _tu(**input: object) -> ToolUseBlock:
    return ToolUseBlock(id="tu_grep", name="grep_files", input=input)


async def test_grep_files_simple_match(dispatcher: ToolDispatcher, workspace: Path):
    result = await dispatcher.dispatch(
        _tu(pattern="goodbye"),
        session_id="s",
        turn_id="t",
        workspace_path=str(workspace),
    )
    assert result.is_error is False
    text = result.content[0].text
    assert "b.py:1: def goodbye():" in text
    assert "a.py" not in text


async def test_grep_files_multiple_files_and_lines(dispatcher: ToolDispatcher, workspace: Path):
    result = await dispatcher.dispatch(
        _tu(pattern="world"),
        session_id="s",
        turn_id="t",
        workspace_path=str(workspace),
    )
    assert result.is_error is False
    text = result.content[0].text
    # Default is case-insensitive, so notes.md's "Hello, World!" matches.
    assert "a.py:2:" in text
    assert "b.py:2:" in text
    assert "notes.md:1:" in text


async def test_grep_files_path_glob_filters(dispatcher: ToolDispatcher, workspace: Path):
    """`path_glob` is fnmatch-style and applies to the workspace-relative path."""
    result = await dispatcher.dispatch(
        _tu(pattern="hello", path_glob="*.md"),
        session_id="s",
        turn_id="t",
        workspace_path=str(workspace),
    )
    assert result.is_error is False
    text = result.content[0].text
    assert "notes.md:1:" in text
    # .py files are excluded by the glob.
    assert "a.py" not in text
    assert "src/deep.py" not in text


async def test_grep_files_case_sensitive(dispatcher: ToolDispatcher, workspace: Path):
    result = await dispatcher.dispatch(
        _tu(pattern="Hello", case_sensitive=True),
        session_id="s",
        turn_id="t",
        workspace_path=str(workspace),
    )
    assert result.is_error is False
    text = result.content[0].text
    # notes.md has "Hello" (capitalized) — matches.
    assert "notes.md:1:" in text
    # a.py has "hello" lowercase — must NOT match case-sensitive.
    assert "a.py:1:" not in text


async def test_grep_files_case_insensitive_default(dispatcher: ToolDispatcher, workspace: Path):
    result = await dispatcher.dispatch(
        _tu(pattern="HELLO"),
        session_id="s",
        turn_id="t",
        workspace_path=str(workspace),
    )
    assert result.is_error is False
    text = result.content[0].text
    assert "a.py:1:" in text
    assert "notes.md:1:" in text


async def test_grep_files_no_matches_reports_scan_count(
    dispatcher: ToolDispatcher, workspace: Path
):
    result = await dispatcher.dispatch(
        _tu(pattern="nonexistent_xyzzy"),
        session_id="s",
        turn_id="t",
        workspace_path=str(workspace),
    )
    assert result.is_error is False
    text = result.content[0].text
    assert "no matches" in text
    assert "scanned" in text


async def test_grep_files_max_results_truncates(dispatcher: ToolDispatcher, workspace: Path):
    (workspace / "many.txt").write_text("\n".join(f"line {i} marker_xyz" for i in range(20)) + "\n")
    result = await dispatcher.dispatch(
        _tu(pattern="marker_xyz", max_results=5),
        session_id="s",
        turn_id="t",
        workspace_path=str(workspace),
    )
    assert result.is_error is False
    text = result.content[0].text
    assert "truncated" in text
    match_lines = [line for line in text.split("\n") if line.startswith("many.txt:")]
    assert len(match_lines) == 5


async def test_grep_files_excludes_common_noise_dirs(dispatcher: ToolDispatcher, workspace: Path):
    """Common build / cache dirs are pruned at walk time and must not appear."""
    for dirname in (".git", "__pycache__", ".venv", "node_modules"):
        d = workspace / dirname
        d.mkdir()
        (d / "x.txt").write_text("matching_marker\n")

    result = await dispatcher.dispatch(
        _tu(pattern="matching_marker"),
        session_id="s",
        turn_id="t",
        workspace_path=str(workspace),
    )
    assert result.is_error is False
    text = result.content[0].text
    assert ".git/" not in text
    assert "__pycache__/" not in text
    assert ".venv/" not in text
    assert "node_modules/" not in text
    assert "no matches" in text


async def test_grep_files_skips_binary_files(dispatcher: ToolDispatcher, workspace: Path):
    """A file with NUL bytes raises UnicodeDecodeError on read; skip it silently."""
    (workspace / "binary.bin").write_bytes(b"matching_marker\x00\xff\xfe")
    (workspace / "text.txt").write_text("matching_marker\n")
    result = await dispatcher.dispatch(
        _tu(pattern="matching_marker"),
        session_id="s",
        turn_id="t",
        workspace_path=str(workspace),
    )
    assert result.is_error is False
    text = result.content[0].text
    assert "text.txt:1:" in text
    assert "binary.bin" not in text


async def test_grep_files_invalid_regex_errors(dispatcher: ToolDispatcher, workspace: Path):
    """Invalid regex must surface as is_error=True (the dispatcher
    genericizes the message in the content block; the detailed reason
    rides on the bus event, not the tool_result)."""
    result = await dispatcher.dispatch(
        _tu(pattern="["),
        session_id="s",
        turn_id="t",
        workspace_path=str(workspace),
    )
    assert result.is_error is True


async def test_grep_files_long_line_snippet_is_truncated(
    dispatcher: ToolDispatcher, workspace: Path
):
    """Snippets longer than _MAX_SNIPPET_CHARS get an ellipsis tail so a
    minified-js / one-line-blob match doesn't blow up output size."""
    long = "x" * 500 + " hit_marker " + "y" * 500
    (workspace / "long.txt").write_text(long + "\n")
    result = await dispatcher.dispatch(
        _tu(pattern="hit_marker"),
        session_id="s",
        turn_id="t",
        workspace_path=str(workspace),
    )
    assert result.is_error is False
    text = result.content[0].text
    assert "long.txt:1:" in text
    assert "..." in text
    # The full 1000+ char line must NOT appear verbatim.
    assert long not in text
