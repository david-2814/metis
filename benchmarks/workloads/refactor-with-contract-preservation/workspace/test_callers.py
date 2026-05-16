"""Verification harness for the keyword-only refactor.

These tests assert two things:

1. ``api.fetch`` rejects every positional call shape (i.e. the
   refactor actually made the parameters keyword-only).
2. Every caller in this workspace still works post-refactor: each one
   invokes ``api.fetch`` with the same effective arguments it did
   before.

If a caller is left calling ``fetch`` positionally, its test fails
with a ``TypeError`` raised from inside the call. The refactor is
not done until ``pytest`` reports ``9 passed``.
"""

from __future__ import annotations

import inspect

import pytest

import api
import cli
import client
import tasks


# ---------------------------------------------------------------------------
# Signature gate — proves the refactor made fetch keyword-only.
# ---------------------------------------------------------------------------


def test_fetch_signature_is_keyword_only() -> None:
    """Every named parameter on api.fetch must be KEYWORD_ONLY."""
    sig = inspect.signature(api.fetch)
    kinds = [p.kind for p in sig.parameters.values()]
    assert kinds, "api.fetch must have at least one parameter"
    for name, p in sig.parameters.items():
        assert p.kind == inspect.Parameter.KEYWORD_ONLY, (
            f"parameter {name!r} must be keyword-only after the refactor; "
            f"got kind={p.kind.description}"
        )


def test_fetch_rejects_positional_call() -> None:
    """A naive positional invocation must raise TypeError."""
    with pytest.raises(TypeError):
        api.fetch("/some-endpoint")  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Caller behaviour gate — every existing caller must still work.
# ---------------------------------------------------------------------------


def test_client_get_users() -> None:
    api.LAST_CALL = None
    result = client.get_users()
    assert result["endpoint"] == "/users"
    assert result["method"] == "GET"
    assert result["retries"] == 3
    assert api.LAST_CALL == result


def test_client_post_user() -> None:
    api.LAST_CALL = None
    result = client.post_user({"name": "x"})
    assert result["endpoint"] == "/users"
    assert result["method"] == "POST"
    assert result["retries"] == 3


def test_client_slow_get() -> None:
    api.LAST_CALL = None
    result = client.slow_get("/things")
    assert result["endpoint"] == "/things"
    assert result["method"] == "GET"
    assert result["retries"] == 5


def test_tasks_get_admin() -> None:
    api.LAST_CALL = None
    result = tasks.get_admin()
    assert result["endpoint"] == "/admins"
    assert result["method"] == "GET"
    assert result["retries"] == 3


def test_tasks_poll_health() -> None:
    api.LAST_CALL = None
    result = tasks.poll_health()
    assert result["endpoint"] == "/health"
    assert result["retries"] == 1


def test_cli_run_command_passes_options() -> None:
    api.LAST_CALL = None
    result = cli.run_command("/widgets", {"method": "PUT", "retries": 2})
    assert result["endpoint"] == "/widgets"
    assert result["method"] == "PUT"
    assert result["retries"] == 2


def test_cli_run_command_with_empty_options() -> None:
    api.LAST_CALL = None
    result = cli.run_command("/widgets", {})
    assert result["endpoint"] == "/widgets"
    assert result["method"] == "GET"
    assert result["retries"] == 3
