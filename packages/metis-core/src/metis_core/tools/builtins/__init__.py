"""Phase 1 built-in tools."""

from metis_core.tools.builtins.file_ops import (
    ListDirTool,
    PatchFileTool,
    ReadFileTool,
    WriteFileTool,
)
from metis_core.tools.builtins.shell import ShellTool

__all__ = [
    "ListDirTool",
    "PatchFileTool",
    "ReadFileTool",
    "ShellTool",
    "WriteFileTool",
]


def register_builtins(dispatcher) -> None:
    """Convenience: register all v1 built-ins on a dispatcher."""
    dispatcher.register(ReadFileTool)
    dispatcher.register(WriteFileTool)
    dispatcher.register(PatchFileTool)
    dispatcher.register(ListDirTool)
    dispatcher.register(ShellTool)
