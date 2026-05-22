"""File-system tools: read_file, write_file, patch_file, list_dir."""

from __future__ import annotations

from metis.core.canonical.content import TextBlock
from metis.core.canonical.tools import SideEffects, ToolDefinition
from metis.core.tools.errors import ToolExecutionError, ToolPermissionDeniedError
from metis.core.tools.protocol import ToolContext, ToolOutput
from metis.core.tools.workspace import WorkspaceEscapeError


class _BaseFileTool:
    """Shared cancellation behavior. File ops are fast; we just check the
    flag before starting per tool-dispatcher.md §8."""

    async def cancel(self) -> bool:
        return True


class ReadFileTool(_BaseFileTool):
    definition = ToolDefinition(
        name="read_file",
        description=(
            "Read a file from the workspace and return its text content. "
            "Optional `offset` (1-indexed start line) and `limit` (line count) "
            "read only a slice — use these when a file is large and you only "
            "need a specific section. Omitting both returns the full file."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "offset": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "1-indexed line number to start reading from.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Maximum number of lines to return.",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        side_effects=SideEffects.READ,
    )

    async def execute(self, input: dict, context: ToolContext) -> ToolOutput:
        path = input["path"]
        offset = input.get("offset")
        limit = input.get("limit")
        try:
            text = context.workspace_files.read(path)
        except WorkspaceEscapeError as exc:
            raise ToolPermissionDeniedError(str(exc), tool_use_id=context.tool_use_id) from exc
        except FileNotFoundError as exc:
            raise ToolExecutionError(
                f"file not found: {path}", tool_use_id=context.tool_use_id, underlying=exc
            ) from exc

        # Back-compat: no slicing params → unchanged behavior.
        if offset is None and limit is None:
            return ToolOutput(content=[TextBlock(text=text)])

        # `keepends=True` preserves newline characters so the rejoined slice
        # is byte-faithful for the lines it covers.
        lines = text.splitlines(keepends=True)
        total = len(lines)
        start = (offset - 1) if offset is not None else 0
        if start >= total:
            return ToolOutput(
                content=[TextBlock(text=f"(file has {total} line(s); offset {offset} is past end)")]
            )
        end = total if limit is None else min(start + limit, total)
        header = f"(showing lines {start + 1}-{end} of {total})\n"
        return ToolOutput(content=[TextBlock(text=header + "".join(lines[start:end]))])


class WriteFileTool(_BaseFileTool):
    definition = ToolDefinition(
        name="write_file",
        description="Create or overwrite a file in the workspace.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
        side_effects=SideEffects.WRITE,
    )

    async def execute(self, input: dict, context: ToolContext) -> ToolOutput:
        path = input["path"]
        content = input["content"]
        try:
            context.workspace_files.write(path, content)
        except WorkspaceEscapeError as exc:
            raise ToolPermissionDeniedError(str(exc), tool_use_id=context.tool_use_id) from exc
        return ToolOutput(
            content=[TextBlock(text=f"Wrote {len(content)} bytes to {path}")],
            files_modified=[path],
        )


class PatchFileTool(_BaseFileTool):
    definition = ToolDefinition(
        name="patch_file",
        description="Replace a unique string in a workspace file (str_replace style).",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old": {"type": "string"},
                "new": {"type": "string"},
            },
            "required": ["path", "old", "new"],
            "additionalProperties": False,
        },
        side_effects=SideEffects.WRITE,
    )

    async def execute(self, input: dict, context: ToolContext) -> ToolOutput:
        path = input["path"]
        old = input["old"]
        new = input["new"]
        try:
            context.workspace_files.patch(path, old, new)
        except WorkspaceEscapeError as exc:
            raise ToolPermissionDeniedError(str(exc), tool_use_id=context.tool_use_id) from exc
        except ValueError as exc:
            # Unique-match failure surfaces to the agent as a result error.
            raise ToolExecutionError(
                str(exc), tool_use_id=context.tool_use_id, underlying=exc
            ) from exc
        return ToolOutput(
            content=[TextBlock(text=f"Patched {path}")],
            files_modified=[path],
        )


class ListDirTool(_BaseFileTool):
    definition = ToolDefinition(
        name="list_dir",
        description="List the contents of a workspace directory.",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string", "default": "."}},
            "additionalProperties": False,
        },
        side_effects=SideEffects.READ,
    )

    async def execute(self, input: dict, context: ToolContext) -> ToolOutput:
        path = input.get("path", ".")
        try:
            entries = context.workspace_files.list(path)
        except WorkspaceEscapeError as exc:
            raise ToolPermissionDeniedError(str(exc), tool_use_id=context.tool_use_id) from exc
        except (FileNotFoundError, NotADirectoryError) as exc:
            raise ToolExecutionError(
                str(exc), tool_use_id=context.tool_use_id, underlying=exc
            ) from exc
        text = "\n".join(entries) if entries else "(empty)"
        return ToolOutput(content=[TextBlock(text=text)])
