"""Closed enum of error codes + helpers to render them as Starlette responses.

See server-api.md §3.6 (error body shape) and §8 (closed code table). All
non-2xx responses go through `error_response` so the wire shape is uniform.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from starlette.responses import JSONResponse


@dataclass(frozen=True)
class APIError(Exception):
    """Raised inside endpoint code to short-circuit with a structured response."""

    code: str
    status: int
    message: str
    details: dict[str, Any] | None = None

    def to_response(self) -> JSONResponse:
        return error_response(self.code, self.status, self.message, self.details)


def error_response(
    code: str,
    status: int,
    message: str,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {"error": {"code": code, "message": message}}
    if details is not None:
        body["error"]["details"] = details
    return JSONResponse(body, status_code=status)


# Curated factories keep the call sites tidy and impossible to typo.


def session_not_found(session_id: str) -> APIError:
    return APIError("session_not_found", 404, f"No session with id {session_id}")


def workspace_not_found(path: str) -> APIError:
    return APIError(
        "workspace_not_found",
        400,
        f"workspace path is missing or not a directory: {path}",
    )


def model_not_configured(model: str) -> APIError:
    return APIError("model_not_configured", 400, f"model {model!r} is not configured")


def turn_in_flight(session_id: str) -> APIError:
    return APIError(
        "turn_in_flight",
        409,
        f"session {session_id} already has a turn in flight",
    )


def turn_not_found(turn_id: str) -> APIError:
    return APIError("turn_not_found", 404, f"No turn with id {turn_id}")


def turn_already_completed(turn_id: str) -> APIError:
    return APIError(
        "turn_already_completed",
        409,
        f"turn {turn_id} has already completed",
    )


def invalid_content(message: str) -> APIError:
    return APIError("invalid_content", 400, message)


def session_already_ended(session_id: str) -> APIError:
    return APIError(
        "session_already_ended",
        409,
        f"session {session_id} has already ended",
    )


def validation_error(message: str) -> APIError:
    return APIError("validation_error", 400, message)


def confirmation_not_found(request_id: str) -> APIError:
    return APIError(
        "confirmation_not_found",
        404,
        f"no pending confirmation with id {request_id}",
    )


def confirmation_already_resolved(request_id: str) -> APIError:
    return APIError(
        "confirmation_already_resolved",
        409,
        f"confirmation {request_id} has already been resolved",
    )


# ---- analytics-api.md §6 ---------------------------------------------------


def invalid_time_window(message: str) -> APIError:
    return APIError("invalid_time_window", 400, message)


def invalid_group_by(message: str) -> APIError:
    return APIError("invalid_group_by", 400, message)


def invalid_order(message: str) -> APIError:
    return APIError("invalid_order", 400, message)


def invalid_limit(message: str) -> APIError:
    return APIError("invalid_limit", 400, message)


def unknown_baseline_model(model: str) -> APIError:
    return APIError(
        "unknown_baseline_model",
        400,
        f"baseline model {model!r} is not in the current price table",
    )


def invalid_gateway_key(message: str) -> APIError:
    return APIError("invalid_gateway_key", 400, message)


def invalid_user(message: str) -> APIError:
    return APIError("invalid_user", 400, message)


def invalid_team(message: str) -> APIError:
    return APIError("invalid_team", 400, message)


# ---- analytics-api.md §4.10 (GDPR portability / forget) -------------------


def invalid_user_id_path(message: str) -> APIError:
    """The path parameter `{user_id}` failed the shape guard."""
    return APIError("invalid_user_id", 400, message)
