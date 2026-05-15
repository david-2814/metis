"""User factory + indexing. Built on the shared UserId / UserMap types."""

from __future__ import annotations

from domain import UserId, UserMap


def make_user(name: str) -> UserId:
    """Return a UserId for the given login name."""
    return UserId(value=name)


def index_users(names: list[str]) -> UserMap:
    """Map each name's UserId back to the raw name (round-trip helper)."""
    return {make_user(n): n for n in names}
