"""Tests for the workspace-scoped file API.

These are the most security-critical tests in the codebase. Any path that
resolves outside the workspace root MUST be rejected.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from metis.core.tools.workspace import WorkspaceEscapeError, WorkspaceFileAPI


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "subdir").mkdir()
    (tmp_path / "subdir" / "nested.txt").write_text("inner")
    (tmp_path / "top.txt").write_text("hello")
    return tmp_path


def test_resolve_relative_path(workspace: Path):
    api = WorkspaceFileAPI(workspace)
    assert api.read("top.txt") == "hello"


def test_resolve_nested_path(workspace: Path):
    api = WorkspaceFileAPI(workspace)
    assert api.read("subdir/nested.txt") == "inner"


def test_resolve_absolute_path_inside_workspace(workspace: Path):
    api = WorkspaceFileAPI(workspace)
    abs_path = str(workspace / "top.txt")
    assert api.read(abs_path) == "hello"


def test_resolve_double_dot_escape_rejected(workspace: Path):
    api = WorkspaceFileAPI(workspace)
    with pytest.raises(WorkspaceEscapeError):
        api.read("../../etc/passwd")


def test_resolve_double_dot_within_then_escape_rejected(workspace: Path):
    """`subdir/../../escape` must be rejected even though it has a valid
    prefix — `..` resolution happens before the check."""
    api = WorkspaceFileAPI(workspace)
    with pytest.raises(WorkspaceEscapeError):
        api.read("subdir/../../escape.txt")


def test_resolve_absolute_path_outside_workspace_rejected(workspace: Path, tmp_path: Path):
    api = WorkspaceFileAPI(workspace)
    # Create a file in tmp_path that's a sibling of (not under) workspace.
    outside = tmp_path.parent / "outside.txt"
    with pytest.raises(WorkspaceEscapeError):
        api.read(str(outside))


def test_symlink_pointing_outside_workspace_rejected(workspace: Path, tmp_path: Path):
    target = tmp_path.parent / "target.txt"
    target.write_text("oops")
    link = workspace / "evil_link.txt"
    os.symlink(target, link)
    api = WorkspaceFileAPI(workspace)
    with pytest.raises(WorkspaceEscapeError):
        api.read("evil_link.txt")


def test_write_creates_parent_directories(workspace: Path):
    api = WorkspaceFileAPI(workspace)
    api.write("new/dir/file.txt", "content")
    assert (workspace / "new" / "dir" / "file.txt").read_text() == "content"


def test_write_outside_workspace_rejected(workspace: Path):
    api = WorkspaceFileAPI(workspace)
    with pytest.raises(WorkspaceEscapeError):
        api.write("../escape.txt", "evil")


def test_list_returns_sorted_entries(workspace: Path):
    api = WorkspaceFileAPI(workspace)
    entries = api.list(".")
    assert entries == sorted(entries)
    assert "subdir" in entries
    assert "top.txt" in entries


def test_list_not_a_directory(workspace: Path):
    api = WorkspaceFileAPI(workspace)
    with pytest.raises(NotADirectoryError):
        api.list("top.txt")


def test_exists_returns_false_for_escape(workspace: Path):
    """exists() does not raise — it returns False for out-of-scope paths."""
    api = WorkspaceFileAPI(workspace)
    assert api.exists("../etc/passwd") is False


def test_patch_unique_match(workspace: Path):
    api = WorkspaceFileAPI(workspace)
    api.write("file.txt", "alpha beta gamma")
    api.patch("file.txt", "beta", "delta")
    assert api.read("file.txt") == "alpha delta gamma"


def test_patch_no_match_fails(workspace: Path):
    api = WorkspaceFileAPI(workspace)
    api.write("file.txt", "hello")
    with pytest.raises(ValueError, match="not found"):
        api.patch("file.txt", "missing", "x")


def test_patch_multiple_matches_fails(workspace: Path):
    api = WorkspaceFileAPI(workspace)
    api.write("file.txt", "foo foo")
    with pytest.raises(ValueError, match="appears 2 times"):
        api.patch("file.txt", "foo", "bar")


def test_delete_file(workspace: Path):
    api = WorkspaceFileAPI(workspace)
    api.delete("top.txt")
    assert not (workspace / "top.txt").exists()


def test_delete_directory_rejects(workspace: Path):
    api = WorkspaceFileAPI(workspace)
    with pytest.raises(IsADirectoryError):
        api.delete("subdir")
