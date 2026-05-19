"""Tests for the bounded retry policy."""

from __future__ import annotations

import pytest
from metis.core.adapters.errors import (
    AuthError,
    NetworkError,
    RateLimitError,
    ServerError,
)
from metis.core.adapters.retry import RetryPolicy, with_retry


def test_is_retryable_classes():
    p = RetryPolicy()
    assert p.is_retryable(RateLimitError("rl")) is True
    assert p.is_retryable(ServerError("se")) is True
    assert p.is_retryable(NetworkError("ne")) is True
    assert p.is_retryable(AuthError("ae")) is False


def test_backoff_honors_retry_after_when_present():
    p = RetryPolicy(retry_after_cap_seconds=60.0)
    err = RateLimitError("rl", retry_after_seconds=12.5)
    assert p.backoff_for_attempt(0, err) == 12.5
    # Cap at retry_after_cap.
    long = RateLimitError("rl", retry_after_seconds=200.0)
    assert p.backoff_for_attempt(0, long) == 60.0


def test_backoff_exponential_without_hint():
    p = RetryPolicy(base_backoff_seconds=1.0, max_backoff_seconds=30.0, jitter_factor=0.0)
    assert p.backoff_for_attempt(0, ServerError("x")) == 1.0
    assert p.backoff_for_attempt(1, ServerError("x")) == 2.0
    assert p.backoff_for_attempt(2, ServerError("x")) == 4.0
    assert p.backoff_for_attempt(10, ServerError("x")) == 30.0  # capped


def test_backoff_jitter_within_bounds():
    p = RetryPolicy(base_backoff_seconds=1.0, jitter_factor=0.25)
    for _ in range(20):
        v = p.backoff_for_attempt(0, ServerError("x"))
        assert 0.75 <= v <= 1.25


# ---- with_retry --------------------------------------------------------


async def test_with_retry_succeeds_first_try():
    calls = 0

    async def fn():
        nonlocal calls
        calls += 1
        return "ok"

    sleeps: list[float] = []

    async def sleep(d):
        sleeps.append(d)

    result = await with_retry(fn, policy=RetryPolicy(max_retries=2), sleep=sleep)
    assert result == "ok"
    assert calls == 1
    assert sleeps == []


async def test_with_retry_retries_on_transient():
    calls = 0

    async def fn():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise ServerError("transient")
        return "ok"

    sleeps: list[float] = []

    async def sleep(d):
        sleeps.append(d)

    result = await with_retry(fn, policy=RetryPolicy(max_retries=2, jitter_factor=0.0), sleep=sleep)
    assert result == "ok"
    assert calls == 3  # initial + 2 retries
    assert len(sleeps) == 2


async def test_with_retry_raises_on_non_retryable():
    calls = 0

    async def fn():
        nonlocal calls
        calls += 1
        raise AuthError("no key")

    async def sleep(d):
        raise AssertionError("should not sleep on non-retryable")

    with pytest.raises(AuthError):
        await with_retry(fn, policy=RetryPolicy(max_retries=2), sleep=sleep)
    assert calls == 1


async def test_with_retry_raises_after_exhausting():
    calls = 0

    async def fn():
        nonlocal calls
        calls += 1
        raise NetworkError("flaky")

    async def sleep(d):
        pass

    with pytest.raises(NetworkError):
        await with_retry(fn, policy=RetryPolicy(max_retries=2, jitter_factor=0.0), sleep=sleep)
    # 1 + max_retries total attempts
    assert calls == 3


async def test_total_attempts_formula():
    """max_retries=2 means up to 3 total attempts (provider-adapter §6.4)."""
    calls = 0

    async def fn():
        nonlocal calls
        calls += 1
        raise ServerError("x")

    with pytest.raises(ServerError):
        await with_retry(
            fn,
            policy=RetryPolicy(max_retries=2, jitter_factor=0.0),
            sleep=lambda _: _noop(),
        )
    assert calls == 3


async def _noop():
    return None
