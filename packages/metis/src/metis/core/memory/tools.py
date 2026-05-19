"""Memory tools: memory_add, memory_replace, memory_consolidate.

These mutate the workspace's MEMORY.md / USER.md via the per-session
MemoryStore. The dispatcher passes the store in via ToolContext.memory.
"""

from __future__ import annotations

from metis.core.canonical.content import TextBlock
from metis.core.canonical.tools import SideEffects, ToolDefinition
from metis.core.memory.store import MemoryFile, MemoryHardCapExceeded
from metis.core.tools.errors import ToolExecutionError
from metis.core.tools.protocol import ToolContext, ToolOutput

_FILE_ENUM = {f.value for f in MemoryFile}


class _MemoryToolBase:
    async def cancel(self) -> bool:
        return True

    @staticmethod
    def _require_memory(context: ToolContext):
        memory = getattr(context, "memory", None)
        if memory is None:
            raise ToolExecutionError(
                "memory is not configured for this session",
                tool_use_id=context.tool_use_id,
            )
        return memory


class MemoryAddTool(_MemoryToolBase):
    """Append a fact / instruction / note to MEMORY.md or USER.md.

    Treat entries as durable: things the user (or the workspace) will want
    the agent to remember across sessions. Don't use this for one-off
    conversation context — that's already in the message history.
    """

    definition = ToolDefinition(
        name="memory_add",
        description=(
            "Append a single entry to MEMORY.md (workspace facts the agent should "
            "remember) or USER.md (facts about the user). Use sparingly — both "
            "files are byte-budgeted (~2KB / ~1.5KB)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "file": {"type": "string", "enum": ["MEMORY.md", "USER.md"]},
                "entry": {"type": "string", "minLength": 1},
            },
            "required": ["file", "entry"],
            "additionalProperties": False,
        },
        side_effects=SideEffects.WRITE,
        requires_workspace=True,
    )

    async def execute(self, input: dict, context: ToolContext) -> ToolOutput:
        memory = self._require_memory(context)
        file = input["file"]
        if file not in _FILE_ENUM:
            raise ToolExecutionError(
                f"unknown memory file {file!r}", tool_use_id=context.tool_use_id
            )
        try:
            result = memory.add_entry(file, input["entry"])
        except MemoryHardCapExceeded as exc:
            raise ToolExecutionError(str(exc), tool_use_id=context.tool_use_id) from exc
        except ValueError as exc:
            raise ToolExecutionError(str(exc), tool_use_id=context.tool_use_id) from exc
        msg = f"appended to {result.file.value} ({result.after_size_bytes} bytes total)"
        if result.over_soft_cap:
            msg += " — over soft cap; consider memory_consolidate"
        return ToolOutput(
            content=[TextBlock(text=msg)],
            files_modified=[result.file.value],
            metadata={
                "before_hash": result.before_hash,
                "after_hash": result.after_hash,
                "before_size_bytes": result.before_size_bytes,
                "after_size_bytes": result.after_size_bytes,
                "over_soft_cap": result.over_soft_cap,
                "operation": "add",
            },
        )


class MemoryReplaceTool(_MemoryToolBase):
    """str-replace a single occurrence inside MEMORY.md or USER.md."""

    definition = ToolDefinition(
        name="memory_replace",
        description=(
            "Replace a unique substring in MEMORY.md or USER.md. `old` must "
            "appear exactly once in the file."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "file": {"type": "string", "enum": ["MEMORY.md", "USER.md"]},
                "old": {"type": "string"},
                "new": {"type": "string"},
            },
            "required": ["file", "old", "new"],
            "additionalProperties": False,
        },
        side_effects=SideEffects.WRITE,
        requires_workspace=True,
    )

    async def execute(self, input: dict, context: ToolContext) -> ToolOutput:
        memory = self._require_memory(context)
        file = input["file"]
        if file not in _FILE_ENUM:
            raise ToolExecutionError(
                f"unknown memory file {file!r}", tool_use_id=context.tool_use_id
            )
        try:
            result = memory.replace(file, input["old"], input["new"])
        except (ValueError, MemoryHardCapExceeded) as exc:
            raise ToolExecutionError(str(exc), tool_use_id=context.tool_use_id) from exc
        msg = f"replaced in {result.file.value} ({result.after_size_bytes} bytes total)"
        if result.over_soft_cap:
            msg += " — over soft cap; consider memory_consolidate"
        return ToolOutput(
            content=[TextBlock(text=msg)],
            files_modified=[result.file.value],
            metadata={
                "before_hash": result.before_hash,
                "after_hash": result.after_hash,
                "before_size_bytes": result.before_size_bytes,
                "after_size_bytes": result.after_size_bytes,
                "over_soft_cap": result.over_soft_cap,
                "operation": "replace",
            },
        )


class MemoryConsolidateTool(_MemoryToolBase):
    """Rewrite MEMORY.md or USER.md wholesale with a consolidated version.

    Useful when the file has grown organically and the agent recognizes it
    can be expressed more concisely.
    """

    definition = ToolDefinition(
        name="memory_consolidate",
        description=(
            "Replace the entire content of MEMORY.md or USER.md with `content`. "
            "Use to compress the file when it's grown over its soft cap."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "file": {"type": "string", "enum": ["MEMORY.md", "USER.md"]},
                "content": {"type": "string"},
            },
            "required": ["file", "content"],
            "additionalProperties": False,
        },
        side_effects=SideEffects.WRITE,
        requires_workspace=True,
    )

    async def execute(self, input: dict, context: ToolContext) -> ToolOutput:
        memory = self._require_memory(context)
        file = input["file"]
        if file not in _FILE_ENUM:
            raise ToolExecutionError(
                f"unknown memory file {file!r}", tool_use_id=context.tool_use_id
            )
        try:
            result = memory.consolidate(file, input["content"])
        except MemoryHardCapExceeded as exc:
            raise ToolExecutionError(str(exc), tool_use_id=context.tool_use_id) from exc
        msg = f"consolidated {result.file.value}: {result.before_size_bytes} → {result.after_size_bytes} bytes"
        return ToolOutput(
            content=[TextBlock(text=msg)],
            files_modified=[result.file.value],
            metadata={
                "before_hash": result.before_hash,
                "after_hash": result.after_hash,
                "before_size_bytes": result.before_size_bytes,
                "after_size_bytes": result.after_size_bytes,
                "over_soft_cap": result.over_soft_cap,
                "operation": "consolidate",
            },
        )


def register_memory_tools(dispatcher) -> None:
    dispatcher.register(MemoryAddTool)
    dispatcher.register(MemoryReplaceTool)
    dispatcher.register(MemoryConsolidateTool)
