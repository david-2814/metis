"""Cost tile handler. Hits the cache for repeated dashboard renders."""

from __future__ import annotations

from cache import get_cached, put_cached


def cost_summary(user_id: str, window: str) -> dict:
    key = f"cost:{user_id}:{window}"
    cached = get_cached(key)
    if cached is not None:
        return cached  # type: ignore[return-value]
    summary = {"total_usd": 0.42, "rows": 12}
    put_cached(key, summary)
    return summary
