"""Core HTTP-fetch API. To be refactored to keyword-only parameters."""

from __future__ import annotations

# Sentinel set by tests so they can observe how fetch was invoked.
LAST_CALL: dict[str, object] | None = None


def fetch(endpoint, method="GET", retries=3, timeout=10):
    """Make an HTTP request. Returns a dict describing what was called.

    The refactor target. v2 of this function will move every parameter
    behind the keyword-only barrier so callers cannot pass them by
    position. Adjust callers accordingly.
    """
    global LAST_CALL
    LAST_CALL = {
        "endpoint": endpoint,
        "method": method,
        "retries": retries,
        "timeout": timeout,
    }
    return LAST_CALL
