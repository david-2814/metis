"""MemoryStore: byte budgets, write semantics, system prompt assembly."""

from __future__ import annotations

from pathlib import Path

import pytest
from metis_core.memory.store import (
    MEMORY_HARD_CAP_BYTES,
    MEMORY_SOFT_CAP_BYTES,
    USER_HARD_CAP_BYTES,
    MemoryFile,
    MemoryHardCapExceeded,
    MemoryStore,
)


@pytest.fixture
def store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(tmp_path)


def test_read_missing_returns_empty(store: MemoryStore):
    assert store.read(MemoryFile.MEMORY) == ""
    assert store.read(MemoryFile.USER) == ""
    assert store.size_bytes(MemoryFile.MEMORY) == 0
    assert store.exists(MemoryFile.MEMORY) is False


def test_add_entry_creates_directory_and_file(tmp_path: Path):
    store = MemoryStore(tmp_path)
    assert not (tmp_path / ".metis").exists()
    result = store.add_entry(MemoryFile.MEMORY, "first fact")
    assert (tmp_path / ".metis" / "MEMORY.md").exists()
    assert store.read(MemoryFile.MEMORY) == "first fact\n"
    assert result.file == MemoryFile.MEMORY
    assert result.before_size_bytes == 0
    assert result.after_size_bytes == len("first fact\n")
    assert result.over_soft_cap is False


def test_add_entry_appends_newline_between_entries(store: MemoryStore):
    store.add_entry(MemoryFile.MEMORY, "one")
    store.add_entry(MemoryFile.MEMORY, "two")
    assert store.read(MemoryFile.MEMORY) == "one\ntwo\n"


def test_add_entry_strips_whitespace(store: MemoryStore):
    store.add_entry(MemoryFile.MEMORY, "  padded  \n")
    assert store.read(MemoryFile.MEMORY) == "padded\n"


def test_add_entry_rejects_empty(store: MemoryStore):
    with pytest.raises(ValueError, match="non-empty"):
        store.add_entry(MemoryFile.MEMORY, "   ")


def test_add_entry_string_file_accepted(store: MemoryStore):
    store.add_entry("MEMORY.md", "fact")
    assert store.read("MEMORY.md") == "fact\n"


def test_add_entry_soft_cap_flagged_not_rejected(store: MemoryStore):
    payload = "x" * (MEMORY_SOFT_CAP_BYTES + 100)
    result = store.add_entry(MemoryFile.MEMORY, payload)
    assert result.over_soft_cap is True
    assert result.over_hard_cap is False
    assert store.size_bytes(MemoryFile.MEMORY) == len(payload) + 1  # trailing \n


def test_add_entry_hard_cap_rejected(store: MemoryStore):
    payload = "x" * (MEMORY_HARD_CAP_BYTES + 1)
    with pytest.raises(MemoryHardCapExceeded) as exc:
        store.add_entry(MemoryFile.MEMORY, payload)
    assert exc.value.file == MemoryFile.MEMORY
    assert exc.value.attempted_size > MEMORY_HARD_CAP_BYTES
    # File should not exist after a rejected write
    assert store.exists(MemoryFile.MEMORY) is False


def test_replace_unique_substring(store: MemoryStore):
    store.add_entry(MemoryFile.MEMORY, "user prefers tabs")
    result = store.replace(MemoryFile.MEMORY, "tabs", "spaces")
    assert store.read(MemoryFile.MEMORY) == "user prefers spaces\n"
    assert result.before_hash != result.after_hash


def test_replace_missing_raises(store: MemoryStore):
    store.add_entry(MemoryFile.MEMORY, "fact")
    with pytest.raises(ValueError, match="not found"):
        store.replace(MemoryFile.MEMORY, "nope", "x")


def test_replace_nonunique_raises(store: MemoryStore):
    store.add_entry(MemoryFile.MEMORY, "alpha alpha")
    with pytest.raises(ValueError, match="must be unique"):
        store.replace(MemoryFile.MEMORY, "alpha", "beta")


def test_replace_on_empty_file_raises(store: MemoryStore):
    with pytest.raises(ValueError, match="empty"):
        store.replace(MemoryFile.MEMORY, "x", "y")


def test_consolidate_wholesale_overwrite(store: MemoryStore):
    store.add_entry(MemoryFile.MEMORY, "old line one")
    store.add_entry(MemoryFile.MEMORY, "old line two")
    result = store.consolidate(MemoryFile.MEMORY, "single consolidated line\n")
    assert store.read(MemoryFile.MEMORY) == "single consolidated line\n"
    assert result.before_size_bytes > result.after_size_bytes


def test_consolidate_hard_cap_rejected(store: MemoryStore):
    payload = "x" * (USER_HARD_CAP_BYTES + 1)
    with pytest.raises(MemoryHardCapExceeded):
        store.consolidate(MemoryFile.USER, payload)


def test_user_file_has_smaller_caps(store: MemoryStore):
    # Right at user hard cap but below memory hard cap → user rejected.
    payload = "x" * (USER_HARD_CAP_BYTES + 1)
    with pytest.raises(MemoryHardCapExceeded):
        store.add_entry(MemoryFile.USER, payload)
    # The same content fits in MEMORY.md (larger cap).
    assert USER_HARD_CAP_BYTES + 1 < MEMORY_HARD_CAP_BYTES
    store.add_entry(MemoryFile.MEMORY, payload)


def test_hashes_change_after_write(store: MemoryStore):
    r1 = store.add_entry(MemoryFile.MEMORY, "alpha")
    r2 = store.add_entry(MemoryFile.MEMORY, "beta")
    assert r1.after_hash == r2.before_hash
    assert r1.after_hash != r2.after_hash


def test_assemble_system_prompt_empty(store: MemoryStore):
    assert store.assemble_system_prompt("base prompt") == "base prompt"


def test_assemble_system_prompt_with_user_only(store: MemoryStore):
    store.add_entry(MemoryFile.USER, "user is a Go developer")
    composed = store.assemble_system_prompt("base prompt")
    assert composed.startswith("base prompt\n\n")
    assert "User context (USER.md)" in composed
    assert "user is a Go developer" in composed
    assert "Workspace memory" not in composed


def test_assemble_system_prompt_with_both_files(store: MemoryStore):
    store.add_entry(MemoryFile.USER, "user prefers Go")
    store.add_entry(MemoryFile.MEMORY, "tests live under tests/")
    composed = store.assemble_system_prompt("base")
    # USER.md comes before MEMORY.md in the composed prompt.
    user_idx = composed.index("USER.md")
    mem_idx = composed.index("MEMORY.md")
    assert user_idx < mem_idx
    assert "user prefers Go" in composed
    assert "tests live under tests/" in composed


def test_workspace_path_expanded_and_resolved(tmp_path: Path):
    store = MemoryStore(str(tmp_path))
    assert Path(store.workspace_path).is_absolute()


def test_soft_and_hard_cap_helpers():
    assert MemoryStore.soft_cap(MemoryFile.MEMORY) == MEMORY_SOFT_CAP_BYTES
    assert MemoryStore.hard_cap(MemoryFile.MEMORY) == MEMORY_HARD_CAP_BYTES
    assert MemoryStore.soft_cap(MemoryFile.USER) < MemoryStore.soft_cap(MemoryFile.MEMORY)
    assert MemoryStore.hard_cap(MemoryFile.USER) < MemoryStore.hard_cap(MemoryFile.MEMORY)
