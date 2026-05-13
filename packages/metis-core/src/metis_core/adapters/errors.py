"""Adapter error hierarchy and HTTP/body classification.

See provider-adapter-contract.md §6.
"""

from __future__ import annotations

from enum import StrEnum


class ErrorClass(StrEnum):
    """Closed set matching event-bus llm.call_failed.error_class (8 values)."""

    RATE_LIMIT = "rate_limit"
    AUTH = "auth"
    SERVER_ERROR = "server_error"
    NETWORK = "network"
    CONTEXT_OVERFLOW = "context_overflow"
    INVALID_REQUEST = "invalid_request"
    CANCELLED = "cancelled"
    OTHER = "other"


class AdapterError(Exception):
    """Base adapter exception. All subclasses carry `error_class`."""

    error_class: ErrorClass = ErrorClass.OTHER

    def __init__(
        self,
        message: str,
        *,
        provider_status: int | None = None,
        provider_message: str = "",
        retryable: bool = False,
        request_id: str = "",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.provider_status = provider_status
        self.provider_message = provider_message
        self.retryable = retryable
        self.request_id = request_id


class RateLimitError(AdapterError):
    error_class = ErrorClass.RATE_LIMIT

    def __init__(
        self,
        message: str,
        *,
        retry_after_seconds: float | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(message, retryable=True, **kwargs)  # type: ignore[arg-type]
        self.retry_after_seconds = retry_after_seconds


class AuthError(AdapterError):
    error_class = ErrorClass.AUTH


class ServerError(AdapterError):
    error_class = ErrorClass.SERVER_ERROR

    def __init__(self, message: str, **kwargs: object) -> None:
        super().__init__(message, retryable=True, **kwargs)  # type: ignore[arg-type]


class NetworkError(AdapterError):
    error_class = ErrorClass.NETWORK

    def __init__(self, message: str, **kwargs: object) -> None:
        super().__init__(message, retryable=True, **kwargs)  # type: ignore[arg-type]


class ContextOverflowError(AdapterError):
    error_class = ErrorClass.CONTEXT_OVERFLOW


class InvalidRequestError(AdapterError):
    error_class = ErrorClass.INVALID_REQUEST


class CancelledError(AdapterError):
    error_class = ErrorClass.CANCELLED


# ---- HTTP status → ErrorClass classifier ---------------------------------


def classify_http_status(status: int, body: dict | None = None) -> ErrorClass:
    """Default HTTP status mapping per §6.2.

    Per-provider overrides (e.g. Anthropic's `overloaded_error` body on 529)
    should call this for the default then adjust based on the body. The
    Anthropic adapter does this in its own classifier.
    """
    if status in (401, 403):
        return ErrorClass.AUTH
    if status == 408:
        return ErrorClass.NETWORK
    if status == 413:
        return ErrorClass.CONTEXT_OVERFLOW
    if status == 429:
        return ErrorClass.RATE_LIMIT
    if status >= 500:
        return ErrorClass.SERVER_ERROR
    if 400 <= status < 500:
        return ErrorClass.INVALID_REQUEST
    return ErrorClass.OTHER


def classify_anthropic_response(status: int, body: dict | None) -> ErrorClass:
    """Anthropic-specific classifier (§6.2).

    Reads `error.type` from the body to refine the HTTP status mapping:
    - overloaded_error → RATE_LIMIT (often surfaces as 529)
    - rate_limit_error → RATE_LIMIT
    - authentication_error / permission_error → AUTH
    - invalid_request_error with 'context' or 'tokens exceeds' → CONTEXT_OVERFLOW
    - api_error → SERVER_ERROR
    """
    default = classify_http_status(status, body)
    if not body or "error" not in body:
        return default
    err = body["error"]
    err_type = err.get("type", "") if isinstance(err, dict) else ""
    err_msg = err.get("message", "") if isinstance(err, dict) else ""

    if err_type == "overloaded_error":
        return ErrorClass.RATE_LIMIT
    if err_type == "rate_limit_error":
        return ErrorClass.RATE_LIMIT
    if err_type in ("authentication_error", "permission_error"):
        return ErrorClass.AUTH
    if err_type == "invalid_request_error":
        msg_lower = err_msg.lower() if isinstance(err_msg, str) else ""
        if "context" in msg_lower or "tokens exceeds" in msg_lower or "too large" in msg_lower:
            return ErrorClass.CONTEXT_OVERFLOW
        return ErrorClass.INVALID_REQUEST
    if err_type == "api_error":
        return ErrorClass.SERVER_ERROR
    return default


def error_for_class(
    error_class: ErrorClass,
    message: str,
    *,
    provider_status: int | None = None,
    provider_message: str = "",
    request_id: str = "",
    retry_after_seconds: float | None = None,
) -> AdapterError:
    """Construct the right AdapterError subclass for a classification."""
    common = dict(
        provider_status=provider_status,
        provider_message=provider_message,
        request_id=request_id,
    )
    if error_class == ErrorClass.RATE_LIMIT:
        return RateLimitError(message, retry_after_seconds=retry_after_seconds, **common)
    if error_class == ErrorClass.AUTH:
        return AuthError(message, **common)
    if error_class == ErrorClass.SERVER_ERROR:
        return ServerError(message, **common)
    if error_class == ErrorClass.NETWORK:
        return NetworkError(message, **common)
    if error_class == ErrorClass.CONTEXT_OVERFLOW:
        return ContextOverflowError(message, **common)
    if error_class == ErrorClass.INVALID_REQUEST:
        return InvalidRequestError(message, **common)
    if error_class == ErrorClass.CANCELLED:
        return CancelledError(message, **common)
    return AdapterError(message, **common)
