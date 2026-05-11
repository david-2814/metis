"""Tool dispatcher and built-in tools.

See docs/specs/tool-dispatcher.md for the full specification.
"""

from metis.tools.confirmation import (
    DEFAULT_POLICY,
    AutoAllowHandler,
    ConfirmationDecision,
    ConfirmationHandler,
    ConfirmationPolicy,
)
from metis.tools.dispatcher import ToolDispatcher
from metis.tools.errors import (
    ConfirmationTimeoutError,
    ToolCancelledError,
    ToolError,
    ToolErrorClass,
    ToolExecutionError,
    ToolNotFoundError,
    ToolPermissionDeniedError,
    ToolRegistrationError,
    ToolTimeoutError,
    ToolUserDeniedError,
    ToolValidationError,
)
from metis.tools.protocol import Tool, ToolContext, ToolFactory, ToolOutput
from metis.tools.workspace import WorkspaceEscapeError, WorkspaceFileAPI

__all__ = [
    "DEFAULT_POLICY",
    "AutoAllowHandler",
    "ConfirmationDecision",
    "ConfirmationHandler",
    "ConfirmationPolicy",
    "ConfirmationTimeoutError",
    "Tool",
    "ToolCancelledError",
    "ToolContext",
    "ToolDispatcher",
    "ToolError",
    "ToolErrorClass",
    "ToolExecutionError",
    "ToolFactory",
    "ToolNotFoundError",
    "ToolOutput",
    "ToolPermissionDeniedError",
    "ToolRegistrationError",
    "ToolTimeoutError",
    "ToolUserDeniedError",
    "ToolValidationError",
    "WorkspaceEscapeError",
    "WorkspaceFileAPI",
]
