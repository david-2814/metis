"""HTTP-ish handler for a single-user request."""

from __future__ import annotations

from domain import UserId


def handle_user_request(uid: UserId, body: dict) -> str:
    """Echo the request with the UserId stamped on it."""
    return f"handling {uid.value}: {len(body)} fields"
