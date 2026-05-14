"""Session list handler. Reads from cache; falls back to DB on miss."""

from __future__ import annotations

from cache import get_cached, put_cached


def list_sessions(user_id: str) -> list[dict]:
    cached = get_cached(f"sessions:{user_id}")
    if cached is not None:
        return cached  # type: ignore[return-value]
    rows = _load_from_db(user_id)
    put_cached(f"sessions:{user_id}", rows)
    return rows


def _load_from_db(user_id: str) -> list[dict]:
    # Stub: in real life this hits postgres.
    return [{"id": f"sess_{user_id}_{i}", "ts": i} for i in range(3)]
