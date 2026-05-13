"""Bounded per-workspace memory.

`MEMORY.md` (~2 KB) and `USER.md` (~1.5 KB) live under `<workspace>/.metis/`.
They are agent-curated through the memory tools (memory_add, memory_replace,
memory_consolidate). On every turn, the session manager loads both files
into the system prompt so the agent has continuity across sessions.

The byte budgets are soft. When a file exceeds its budget, a memory.eviction
event is emitted as a signal that the agent should consolidate; v1 does NOT
auto-truncate (the eviction is the spec's prescribed user-visible action).
"""

from metis_core.memory.store import (
    MEMORY_FILE,
    MEMORY_HARD_CAP_BYTES,
    MEMORY_SOFT_CAP_BYTES,
    USER_FILE,
    USER_HARD_CAP_BYTES,
    USER_SOFT_CAP_BYTES,
    MemoryFile,
    MemoryStore,
)
from metis_core.memory.tools import (
    MemoryAddTool,
    MemoryConsolidateTool,
    MemoryReplaceTool,
    register_memory_tools,
)

__all__ = [
    "MEMORY_FILE",
    "MEMORY_HARD_CAP_BYTES",
    "MEMORY_SOFT_CAP_BYTES",
    "USER_FILE",
    "USER_HARD_CAP_BYTES",
    "USER_SOFT_CAP_BYTES",
    "MemoryAddTool",
    "MemoryConsolidateTool",
    "MemoryFile",
    "MemoryReplaceTool",
    "MemoryStore",
    "register_memory_tools",
]
