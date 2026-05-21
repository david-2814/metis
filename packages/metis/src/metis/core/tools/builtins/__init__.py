"""Phase 1 + Phase 2.5 built-in tools."""

from metis.core.tools.builtins.delegate import DelegateTool
from metis.core.tools.builtins.file_ops import (
    ListDirTool,
    PatchFileTool,
    ReadFileTool,
    WriteFileTool,
)
from metis.core.tools.builtins.shell import ShellTool
from metis.core.tools.builtins.skill_save import SkillSaveTool

__all__ = [
    "DelegateTool",
    "ListDirTool",
    "PatchFileTool",
    "ReadFileTool",
    "ShellTool",
    "SkillSaveTool",
    "WriteFileTool",
]


def register_builtins(dispatcher, *, with_delegate: bool = True) -> None:
    """Convenience: register all built-ins on a dispatcher.

    `with_delegate` lets callers opt out of registering the `delegate` tool
    on dispatchers that will never run a planner — e.g. test dispatchers
    that drive the worker side directly. Per-session visibility is still
    enforced by the session manager's tool filter (delegation.md §5.6);
    `with_delegate=False` is the broader knob for dispatcher-wide opt-out.

    `skill_save` is registered unconditionally; the session manager filters
    it out of worker tool dispatch (manager.py `_WORKER_FORBIDDEN_TOOLS`).
    """
    dispatcher.register(ReadFileTool)
    dispatcher.register(WriteFileTool)
    dispatcher.register(PatchFileTool)
    dispatcher.register(ListDirTool)
    dispatcher.register(ShellTool)
    dispatcher.register(SkillSaveTool)
    if with_delegate:
        dispatcher.register(DelegateTool)
