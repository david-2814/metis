"""Bounded retry policy with exponential backoff.

See provider-adapter-contract.md §6.4. Total attempts = 1 + max_retries.
Backoff is 1s, 2s, 4s, 8s, ... ±25% jitter, capped at 30s. RATE_LIMIT
responses with a `retry_after` hint honor that duration (capped at 60s).
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass

from metis.core.adapters.errors import (
    AdapterError,
    ErrorClass,
    RateLimitError,
)

_RETRYABLE_CLASSES: frozenset[ErrorClass] = frozenset(
    {ErrorClass.RATE_LIMIT, ErrorClass.SERVER_ERROR, ErrorClass.NETWORK}
)


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 2  # additional attempts after the first; total = 1 + max_retries
    base_backoff_seconds: float = 1.0
    max_backoff_seconds: float = 30.0
    jitter_factor: float = 0.25
    retry_after_cap_seconds: float = 60.0

    def is_retryable(self, exc: AdapterError) -> bool:
        return exc.error_class in _RETRYABLE_CLASSES

    def backoff_for_attempt(self, attempt: int, exc: AdapterError) -> float:
        """Compute the sleep duration before the next retry.

        `attempt` is the 0-indexed retry count (0 = first retry).
        """
        if isinstance(exc, RateLimitError) and exc.retry_after_seconds is not None:
            return min(exc.retry_after_seconds, self.retry_after_cap_seconds)
        base = self.base_backoff_seconds * (2**attempt)
        capped = min(base, self.max_backoff_seconds)
        jitter = capped * self.jitter_factor
        return capped + random.uniform(-jitter, jitter)


async def with_retry(
    fn,
    *,
    policy: RetryPolicy,
    sleep=asyncio.sleep,
):
    """Run an async fn with bounded retry. Returns fn's result on success,
    raises the final AdapterError on failure."""
    last_exc: AdapterError | None = None
    for attempt in range(policy.max_retries + 1):
        try:
            return await fn()
        except AdapterError as exc:
            last_exc = exc
            if not policy.is_retryable(exc):
                raise
            if attempt >= policy.max_retries:
                # Exhausted; raise the last error.
                raise
            await sleep(policy.backoff_for_attempt(attempt, exc))
    # Unreachable but keeps type-checkers happy.
    assert last_exc is not None
    raise last_exc
