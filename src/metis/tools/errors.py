"""Tool dispatcher exception hierarchy.

See tool-dispatcher.md §6. ToolError subclasses carry a closed
ToolErrorClass enum that matches the `tool.failed.error_class` catalog.
"""

from __future__ import annotations

from enum import StrEnum


class ToolErrorClass(StrEnum):
    NOT_FOUND = "not_found"
    VALIDATION_ERROR = "validation_error"
    PERMISSION_DENIED = "permission_denied"
    USER_DENIED = "user_denied"
    TIMEOUT = "timeout"
    EXECUTION_ERROR = "execution_error"
    CANCELLED = "cancelled"
    CONFIRMATION_TIMEOUT = "confirmation_timeout"


class ToolError(Exception):
    error_class: ToolErrorClass
    is_user_visible: bool = True

    def __init__(self, message: str, *, tool_use_id: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.tool_use_id = tool_use_id


class ToolNotFoundError(ToolError):
    error_class = ToolErrorClass.NOT_FOUND


class ToolValidationError(ToolError):
    error_class = ToolErrorClass.VALIDATION_ERROR

    def __init__(
        self, message: str, *, tool_use_id: str = "", validation_errors: list[str] | None = None
    ) -> None:
        super().__init__(message, tool_use_id=tool_use_id)
        self.validation_errors = validation_errors or []


class ToolPermissionDeniedError(ToolError):
    error_class = ToolErrorClass.PERMISSION_DENIED


class ToolUserDeniedError(ToolError):
    error_class = ToolErrorClass.USER_DENIED


class ToolTimeoutError(ToolError):
    error_class = ToolErrorClass.TIMEOUT


class ToolExecutionError(ToolError):
    error_class = ToolErrorClass.EXECUTION_ERROR
    # Underlying exception (if any) is for logs; not surfaced to the agent.
    is_user_visible = False

    def __init__(
        self,
        message: str,
        *,
        tool_use_id: str = "",
        underlying: BaseException | None = None,
    ) -> None:
        super().__init__(message, tool_use_id=tool_use_id)
        self.underlying = underlying


class ToolCancelledError(ToolError):
    error_class = ToolErrorClass.CANCELLED


class ConfirmationTimeoutError(ToolError):
    error_class = ToolErrorClass.CONFIRMATION_TIMEOUT


class ToolRegistrationError(ValueError):
    """Raised at register() time when a tool's definition is invalid."""

    def __init__(self, tool_name: str, reason: str) -> None:
        super().__init__(f"{tool_name}: {reason}")
        self.tool_name = tool_name
        self.reason = reason
