"""Single-use, time-bounded attach tokens for WebSocket subscription.

Per server-api.md §4.1 (`GET /sessions/{id}`) + streaming-protocol.md §3.1:
each `GET /sessions/{id}` mints a fresh nonce. Tokens are single-use and
expire after `ttl_seconds`. The WebSocket handler consumes a token before
upgrading.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class _Entry:
    token: str
    session_id: str
    expires_at: float


class AttachTokenRegistry:
    """In-memory token store. Single-process — fine for v1 (loopback only)."""

    def __init__(self, *, ttl_seconds: float = 60.0) -> None:
        self._ttl = ttl_seconds
        self._tokens: dict[str, _Entry] = {}

    def mint(self, session_id: str) -> tuple[str, float]:
        """Mint a fresh token for `session_id`; returns (token, expires_at_epoch)."""
        token = "atk_" + secrets.token_urlsafe(20)
        expires_at = time.time() + self._ttl
        self._tokens[token] = _Entry(token, session_id, expires_at)
        return token, expires_at

    def consume(self, token: str, *, session_id: str) -> bool:
        """Consume a token. Returns True iff it was valid for `session_id`
        and not yet expired. The token is removed regardless."""
        entry = self._tokens.pop(token, None)
        if entry is None:
            return False
        if entry.session_id != session_id:
            return False
        if time.time() > entry.expires_at:
            return False
        return True

    def prune_expired(self) -> int:
        """Drop expired entries; returns the number removed. Cheap O(n)."""
        now = time.time()
        expired = [t for t, e in self._tokens.items() if e.expires_at <= now]
        for t in expired:
            self._tokens.pop(t, None)
        return len(expired)
