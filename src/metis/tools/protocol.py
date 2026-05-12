"""Tool protocol and per-execution context types.

See tool-dispatcher.md §3.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from metis.canonical.content import ContentBlock
from metis.canonical.tools import ToolDefinition
from metis.tools.workspace import WorkspaceFileAPI

if TYPE_CHECKING:  # pragma: no cover
    pass


@dataclass
class ToolContext:
    """Per-call context handed to a tool's execute()."""

    session_id: str
    turn_id: str
    tool_use_id: str
    workspace_path: str
    workspace_files: WorkspaceFileAPI
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("metis.tool"))
    # Per-session bounded memory. None means memory tools should refuse to run.
    memory: Any = None  # MemoryStore — Any to avoid an import cycle through tools


@dataclass
class ToolOutput:
    """Structured tool result. The dispatcher wraps this into a canonical
    ToolResultBlock for the agent loop."""

    content: list[ContentBlock]
    success: bool = True
    metadata: dict = field(default_factory=dict)
    files_modified: list[str] | None = None
    command_executed: str | None = None


class Tool(Protocol):
    """Implemented by every tool — built-in or MCP-wrapped."""

    definition: ToolDefinition

    async def execute(self, input: dict, context: ToolContext) -> ToolOutput: ...

    async def cancel(self) -> bool: ...


# A factory returns a fresh Tool instance per dispatch (§3.1). The dispatcher
# never holds singletons — each call gets its own instance to avoid shared
# state between concurrent dispatches.
ToolFactory = Callable[[], Tool]
