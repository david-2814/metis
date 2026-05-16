"""Direct callers of api.fetch — currently use positional args."""

from __future__ import annotations

from api import fetch


def get_users():
    """Single positional arg."""
    return fetch("/users")


def post_user(payload):  # noqa: ARG001 — payload is illustrative, the call shape is what matters
    """Two positional args (endpoint and method)."""
    return fetch("/users", "POST")


def slow_get(endpoint):
    """Mix of positional + keyword."""
    return fetch(endpoint, "GET", retries=5)
