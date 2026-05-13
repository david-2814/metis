"""File-system tools: read_file, write_file, patch_file, list_dir."""

from __future__ import annotations

from metis_core.canonical.content import TextBlock
from metis_core.canonical.tools import SideEffects, ToolDefinition
from metis_core.tools.errors import ToolExecutionError, ToolPermissionDeniedError
from metis_core.tools.protocol import ToolContext, ToolOutput
from metis_core.tools.workspace import WorkspaceEscapeError


class _BaseFileTool:
    """Shared cancellation behavior. File ops are fast; we just check the
    flag before starting per tool-dispatcher.md §8."""

    async def cancel(self) -> bool:
        return True


class ReadFileTool(_BaseFileTool):
    definition = ToolDefinition(
        name="read_file",
        description="Read a file from the workspace and return its text content.",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
        side_effects=SideEffects.READ,
    )

    async def execute(self, input: dict, context: ToolContext) -> ToolOutput:
        path = input["path"]
        try:
            text = context.workspace_files.read(path)
        except WorkspaceEscapeError as exc:
            raise ToolPermissionDeniedError(str(exc), tool_use_id=context.tool_use_id) from exc
        except FileNotFoundError as exc:
            raise ToolExecutionError(
                f"file not found: {path}", tool_use_id=context.tool_use_id, underlying=exc
            ) from exc
        return ToolOutput(content=[TextBlock(text=text)])


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
