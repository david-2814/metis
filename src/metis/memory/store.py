"""MemoryStore: per-workspace bounded MEMORY.md / USER.md.

Files live at `<workspace>/.metis/MEMORY.md` and `<workspace>/.metis/USER.md`.
Both are agent-curated markdown — small, byte-budgeted, file-on-disk, and
git-syncable. The agent reads them via the system prompt (composed by
SessionManager); writes go through the three memory tools (add/replace/
consolidate).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

MEMORY_FILE = "MEMORY.md"
USER_FILE = "USER.md"

MEMORY_SOFT_CAP_BYTES = 2_048
MEMORY_HARD_CAP_BYTES = 4_096
USER_SOFT_CAP_BYTES = 1_536
USER_HARD_CAP_BYTES = 3_072


class MemoryFile(StrEnum):
    """Closed enum matching the event bus catalog's memory.updated.file field."""

    MEMORY = "MEMORY.md"
    USER = "USER.md"


@dataclass(frozen=True)
class WriteResult:
    """Returned by MemoryStore writes; carries hashes for memory.updated events."""

    file: MemoryFile
    before_hash: str
    after_hash: str
    before_size_bytes: int
    after_size_bytes: int
    over_soft_cap: bool
    over_hard_cap: bool


class MemoryHardCapExceeded(ValueError):
    """Raised when a write would push a file past its hard cap.

    Soft-cap overflows are tolerated (with a memory.eviction event); hard-cap
    overflows are rejected so the agent has to consolidate first.
    """

    def __init__(self, file: MemoryFile, attempted_size: int, hard_cap: int) -> None:
        super().__init__(
            f"{file.value} write rejected: {attempted_size} bytes exceeds "
            f"hard cap {hard_cap}; consolidate before adding more"
        )
        self.file = file
        self.attempted_size = attempted_size
        self.hard_cap = hard_cap


class MemoryStore:
    """Workspace-scoped memory. One instance per session.

    All paths are inside `<workspace>/.metis/`. Directory created lazily on
    first write. Files default to empty when missing.
    """

    def __init__(self, workspace_path: str | Path) -> None:
        self._workspace = Path(workspace_path).expanduser().resolve()
        self._dir = self._workspace / ".metis"

    @property
    def workspace_path(self) -> str:
        return str(self._workspace)

    def _path(self, file: MemoryFile | str) -> Path:
        file_enum = MemoryFile(file) if isinstance(file, str) else file
        return self._dir / file_enum.value

    @staticmethod
    def soft_cap(file: MemoryFile) -> int:
        return MEMORY_SOFT_CAP_BYTES if file == MemoryFile.MEMORY else USER_SOFT_CAP_BYTES

    @staticmethod
    def hard_cap(file: MemoryFile) -> int:
        return MEMORY_HARD_CAP_BYTES if file == MemoryFile.MEMORY else USER_HARD_CAP_BYTES

    # ---- Reads ---------------------------------------------------------

    def read(self, file: MemoryFile | str) -> str:
        path = self._path(file)
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")

    def exists(self, file: MemoryFile | str) -> bool:
        return self._path(file).exists()

    def size_bytes(self, file: MemoryFile | str) -> int:
        path = self._path(file)
        if not path.exists():
            return 0
        return path.stat().st_size

    # ---- Writes --------------------------------------------------------

    def add_entry(self, file: MemoryFile | str, entry: str) -> WriteResult:
        """Append `entry` as a new line to `file`.

        Empty entries are rejected. Hard-cap overflow raises. Soft-cap
        overflow is allowed but reflected in `WriteResult.over_soft_cap`.
        """
        file = MemoryFile(file) if isinstance(file, str) else file
        if not entry.strip():
            raise ValueError("entry must be non-empty")
        old = self.read(file)
        if old and not old.endswith("\n"):
            old = old + "\n"
        new_content = old + entry.strip() + "\n"
        return self._write(file, new_content)

    def replace(self, file: MemoryFile | str, old_text: str, new_text: str) -> WriteResult:
        """Replace `old_text` with `new_text` in `file`.

        `old_text` must appear exactly once.
        """
        file = MemoryFile(file) if isinstance(file, str) else file
        current = self.read(file)
        if not current:
            raise ValueError(f"{file.value} is empty; nothing to replace")
        count = current.count(old_text)
        if count == 0:
            raise ValueError(f"replace source not found in {file.value}")
        if count > 1:
            raise ValueError(f"replace source appears {count} times in {file.value}; must be unique")
        new_content = current.replace(old_text, new_text, 1)
        return self._write(file, new_content)

    def consolidate(self, file: MemoryFile | str, new_content: str) -> WriteResult:
        """Replace `file` wholesale with `new_content`."""
        file = MemoryFile(file) if isinstance(file, str) else file
        return self._write(file, new_content)

    def _write(self, file: MemoryFile, new_content: str) -> WriteResult:
        before_text = self.read(file)
        before_bytes = before_text.encode("utf-8")
        after_bytes = new_content.encode("utf-8")
        after_size = len(after_bytes)
        if after_size > self.hard_cap(file):
            raise MemoryHardCapExceeded(file, after_size, self.hard_cap(file))
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._path(file)
        path.write_text(new_content, encoding="utf-8")
        return WriteResult(
            file=file,
            before_hash=hashlib.sha256(before_bytes).hexdigest(),
            after_hash=hashlib.sha256(after_bytes).hexdigest(),
            before_size_bytes=len(before_bytes),
            after_size_bytes=after_size,
            over_soft_cap=after_size > self.soft_cap(file),
            over_hard_cap=False,  # would have raised
        )

    # ---- Context assembly ---------------------------------------------

    def assemble_system_prompt(self, base: str) -> str:
        """Compose the final system prompt: base + USER.md + MEMORY.md.

        Empty memory files are omitted. The result is what gets sent to the
        adapter for every turn — so the agent sees memory as part of its
        operating context, not as a tool result it has to ask for.
        """
        sections: list[str] = [base.rstrip()]
        user = self.read(MemoryFile.USER).strip()
        memory = self.read(MemoryFile.MEMORY).strip()
        if user:
            sections.append(_section("User context (USER.md)", user))
        if memory:
            sections.append(_section("Workspace memory (MEMORY.md)", memory))
        return "\n\n".join(s for s in sections if s)


def _section(title: str, body: str) -> str:
    return f"## {title}\n{body}"
