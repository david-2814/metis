"""Workspace-scoped file API with path-escape protection.

See tool-dispatcher.md §5.1. All paths resolve relative to workspace_root.
`..` segments are resolved during checking, not after — a path of
`subdir/../../etc/passwd` is rejected even though the final string doesn't
contain `..`. Symlinks pointing outside the workspace are rejected.
"""

from __future__ import annotations

import os
from pathlib import Path


class WorkspaceEscapeError(ValueError):
    """Raised when a path resolves outside the workspace root."""

    def __init__(self, path: str, workspace_root: str) -> None:
        super().__init__(f"Path {path!r} escapes workspace boundary {workspace_root!r}")
        self.path = path
        self.workspace_root = workspace_root


class WorkspaceFileAPI:
    """File operations scoped to a workspace root.

    Tools that touch the filesystem MUST use this rather than raw OS calls.
    """

    def __init__(self, workspace_root: str | Path) -> None:
        # Resolve to absolute, follow symlinks to canonical form so symlinked
        # workspace roots themselves work transparently.
        self._root = Path(workspace_root).expanduser().resolve()
        if not self._root.is_dir():
            raise ValueError(f"workspace_root {self._root!r} is not a directory")

    @property
    def workspace_root(self) -> str:
        return str(self._root)

    # ---- Path resolution -------------------------------------------------

    def _resolve(self, path: str | Path) -> Path:
        """Resolve `path` against the workspace root and verify it stays in scope.

        Raises WorkspaceEscapeError on any escape attempt.
        """
        p = Path(path)
        if p.is_absolute():
            candidate = p
        else:
            candidate = self._root / p

        # `resolve(strict=False)` collapses `..` segments and follows symlinks
        # for components that exist. For components that don't exist yet
        # (e.g. write to new file), it returns the normalized path.
        resolved = candidate.resolve(strict=False)

        # Containment check: resolved must equal root or be inside it.
        try:
            resolved.relative_to(self._root)
        except ValueError as exc:
            raise WorkspaceEscapeError(str(path), str(self._root)) from exc
        return resolved

    # ---- Operations ------------------------------------------------------

    def read(self, path: str | Path) -> str:
        return self._resolve(path).read_text()

    def read_bytes(self, path: str | Path) -> bytes:
        return self._resolve(path).read_bytes()

    def write(self, path: str | Path, content: str) -> None:
        resolved = self._resolve(path)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content)

    def write_bytes(self, path: str | Path, content: bytes) -> None:
        resolved = self._resolve(path)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_bytes(content)

    def append(self, path: str | Path, content: str) -> None:
        resolved = self._resolve(path)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        with resolved.open("a") as f:
            f.write(content)

    def exists(self, path: str | Path) -> bool:
        try:
            return self._resolve(path).exists()
        except WorkspaceEscapeError:
            return False

    def list(self, path: str | Path = ".") -> list[str]:
        resolved = self._resolve(path)
        if not resolved.is_dir():
            raise NotADirectoryError(f"{path!r} is not a directory")
        return sorted(os.listdir(resolved))

    def delete(self, path: str | Path) -> None:
        resolved = self._resolve(path)
        if resolved.is_dir():
            raise IsADirectoryError(f"{path!r} is a directory; use a directory-aware delete")
        resolved.unlink()

    def patch(self, path: str | Path, old: str, new: str) -> None:
        """str_replace-style patch. `old` must appear exactly once in the file."""
        resolved = self._resolve(path)
        text = resolved.read_text()
        count = text.count(old)
        if count == 0:
            raise ValueError(f"patch source not found in {path!r}")
        if count > 1:
            raise ValueError(f"patch source appears {count} times in {path!r}; must be unique")
        resolved.write_text(text.replace(old, new, 1))
