"""Tests for the rate-limit middleware (gateway-hardening.md §3).

Coverage:

1. Disabled-by-default config doesn't break existing behavior — `/v1/*` and
   `/healthz` pass through with no rate-limit headers.
2. Per-key bucket fires HTTP 429 at threshold and resets after the window.
3. Per-IP bucket fires HTTP 429 independent of the per-key bucket.
4. Both buckets must allow (compose AND, not OR).
5. 429 response shape: OpenAI vs Anthropic envelope, `Retry-After` header.
6. Unit tests for the `_Bucket` class (refill math, retry-after rounding).
7. `_client_ip` respects `trusted_proxies` and falls back to socket peer.
"""

from __future__ import annotations

import time

import httpx
import pytest
from metis_gateway.app import build_app
from metis_gateway.middleware_ratelimit import (
    DEFAULT_PER_IP_RPM,
    DEFAULT_PER_KEY_RPM,
    RateLimitConfig,
    _Bucket,
    _client_ip,
)

# ---------------------------------------------------------------------------
# `_Bucket` unit tests — guard refill math in isolation.
# ---------------------------------------------------------------------------


def test_bucket_starts_full() -> None:
    b = _Bucket(capacity=10, window_seconds=60.0)
    assert b.tokens == 10


def test_bucket_take_decrements() -> None:
    b = _Bucket(capacity=3, window_seconds=60.0)
    now = time.monotonic()
    assert b.take(now=now) is True
    assert b.take(now=now) is True
    assert b.take(now=now) is True
    assert b.take(now=now) is False


def test_bucket_refills_over_time() -> None:
    b = _Bucket(capacity=60, window_seconds=60.0)  # 1 token / sec
    start = time.monotonic()
    for _ in range(60):
        assert b.take(now=start) is True
    assert b.take(now=start) is False
    # After 30 seconds we should have 30 tokens again.
    assert b.take(now=start + 30.0) is True
    # And we can take 29 more (we already took 1).
    for _ in range(29):
        assert b.take(now=start + 30.0) is True
    assert b.take(now=start + 30.0) is False


def test_bucket_retry_after_rounds_up_and_min_one() -> None:
    b = _Bucket(capacity=60, window_seconds=60.0)
    now = time.monotonic()
    for _ in range(60):
        b.take(now=now)
    # Bucket is empty; deficit is 1 token, rate is 1 tok/sec → 1 second.
    assert b.retry_after_seconds(now=now) == 1
    # Sub-second deficit still rounds up to 1.
    b.tokens = 0.1
    assert b.retry_after_seconds(now=now) == 1


# ---------------------------------------------------------------------------
# `_client_ip` unit tests — header parsing + trusted_proxies.
# ---------------------------------------------------------------------------


def _scope_with_headers(
    headers: list[tuple[bytes, bytes]], *, client: tuple[str, int] | None
) -> dict:
    return {"type": "http", "headers": headers, "client": client}


def test_client_ip_falls_back_to_socket_peer_when_no_xff() -> None:
    scope = _scope_with_headers([], client=("203.0.113.7", 50000))
    assert _client_ip(scope, trusted_proxies=()) == "203.0.113.7"


def test_client_ip_ignores_xff_when_no_trusted_proxies() -> None:
    """Peer is the source of truth unless we explicitly trust a forwarder."""
    scope = _scope_with_headers(
        [(b"x-forwarded-for", b"198.51.100.42, 10.0.0.1")],
        client=("10.0.0.1", 50000),
    )
    assert _client_ip(scope, trusted_proxies=()) == "10.0.0.1"


def test_client_ip_reads_xff_when_peer_is_trusted_proxy() -> None:
    scope = _scope_with_headers(
        [(b"x-forwarded-for", b"198.51.100.42, 10.0.0.1")],
        client=("10.0.0.1", 50000),
    )
    assert _client_ip(scope, trusted_proxies=("10.0.0.0/8",)) == "198.51.100.42"


def test_client_ip_skips_unparseable_xff_entries() -> None:
    scope = _scope_with_headers(
        [(b"x-forwarded-for", b"not-an-ip, 198.51.100.42")],
        client=("10.0.0.1", 50000),
    )
    assert _client_ip(scope, trusted_proxies=("10.0.0.0/8",)) == "198.51.100.42"


def test_client_ip_returns_none_when_no_peer() -> None:
    scope = _scope_with_headers([], client=None)
    assert _client_ip(scope, trusted_proxies=()) is None


# ---------------------------------------------------------------------------
# HTTP-level tests — drive the middleware via the gateway app.
# ---------------------------------------------------------------------------


@pytest.fixture
async def disabled_client(runtime):
    """Default config — rate limit OFF. Existing behavior must be unchanged."""
    app = build_app(runtime)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


@pytest.fixture
async def strict_client(runtime):
    """Tiny limits so a handful of requests trips both buckets in-test."""
    config = RateLimitConfig(enabled=True, per_key_rpm=3, per_ip_rpm=2)
    app = build_app(runtime, rate_limit=config)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


@pytest.fixture
async def per_key_strict_client(runtime):
    """Per-key bucket strict; per-IP wide open so we can isolate per-key."""
    config = RateLimitConfig(enabled=True, per_key_rpm=2, per_ip_rpm=10_000)
    app = build_app(runtime, rate_limit=config)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


@pytest.fixture
async def per_ip_strict_client(runtime):
    """Per-IP bucket strict; per-key wide open so we can isolate per-IP."""
    config = RateLimitConfig(enabled=True, per_key_rpm=10_000, per_ip_rpm=2)
    app = build_app(runtime, rate_limit=config)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


# --- Disabled by default ---------------------------------------------------


async def test_disabled_config_does_not_break_chat_completions(
    disabled_client, bearer_token, scripted_adapter
) -> None:
    for _ in range(5):
        scripted_adapter.push_response(text="hi")
    for _ in range(5):
        r = await disabled_client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {bearer_token}"},
            json={"model": "haiku", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r.status_code == 200
        assert "retry-after" not in r.headers


async def test_disabled_config_does_not_break_healthz(disabled_client) -> None:
    r = await disabled_client.get("/healthz")
    assert r.status_code == 200
    assert "retry-after" not in r.headers


async def test_disabled_default_construction_uses_disabled_config() -> None:
    """`RateLimitConfig()` with no kwargs MUST default to disabled.

    The middleware is opt-in until Wave 12+ promotes it — a buyer who
    upgrades the gateway shouldn't have rate limiting silently switch on.
    """
    config = RateLimitConfig()
    assert config.enabled is False
    assert config.per_key_rpm == DEFAULT_PER_KEY_RPM
    assert config.per_ip_rpm == DEFAULT_PER_IP_RPM


# --- Per-key bucket --------------------------------------------------------


async def test_per_key_bucket_fires_429_at_threshold(
    per_key_strict_client, bearer_token, scripted_adapter
) -> None:
    """First N requests succeed; the (N+1)th fires 429 with `Retry-After`."""
    scripted_adapter.push_response(text="hi")
    scripted_adapter.push_response(text="hi")

    headers = {"Authorization": f"Bearer {bearer_token}"}
    body = {"model": "haiku", "messages": [{"role": "user", "content": "hi"}]}

    r1 = await per_key_strict_client.post("/v1/chat/completions", headers=headers, json=body)
    assert r1.status_code == 200
    r2 = await per_key_strict_client.post("/v1/chat/completions", headers=headers, json=body)
    assert r2.status_code == 200
    r3 = await per_key_strict_client.post("/v1/chat/completions", headers=headers, json=body)
    assert r3.status_code == 429
    assert r3.headers["retry-after"].isdigit()
    body3 = r3.json()
    assert body3["error"]["code"] == "rate_limit_exceeded"
    assert body3["error"]["type"] == "rate_limit_error"
    assert body3["error"]["scope"] == "per_key"
    assert body3["error"]["retry_after_seconds"] >= 1


async def test_per_key_bucket_anthropic_shape_envelope(
    per_key_strict_client, bearer_token, scripted_adapter
) -> None:
    """Anthropic-shape clients see the minimal `{type, message}` envelope."""
    scripted_adapter.push_response(text="hi")
    scripted_adapter.push_response(text="hi")

    headers = {"x-api-key": bearer_token}
    body = {"model": "haiku", "max_tokens": 32, "messages": [{"role": "user", "content": "hi"}]}

    for _ in range(2):
        r = await per_key_strict_client.post("/v1/messages", headers=headers, json=body)
        assert r.status_code == 200

    r_blocked = await per_key_strict_client.post("/v1/messages", headers=headers, json=body)
    assert r_blocked.status_code == 429
    assert "retry-after" in r_blocked.headers
    payload = r_blocked.json()
    assert payload["error"]["type"] == "rate_limit_error"
    # Anthropic envelope: no `code`, no `scope`.
    assert "code" not in payload["error"]
    assert "scope" not in payload["error"]


# --- Per-IP bucket ---------------------------------------------------------


async def test_per_ip_bucket_fires_429_independent_of_key(
    per_ip_strict_client, bearer_token, scripted_adapter
) -> None:
    """Even when each request uses the same key, the per-IP cap also fires.

    Tests the compose-by-AND contract: the per-IP bucket is checked even
    after the per-key bucket has admitted the request.
    """
    scripted_adapter.push_response(text="hi")
    scripted_adapter.push_response(text="hi")

    headers = {"Authorization": f"Bearer {bearer_token}"}
    body = {"model": "haiku", "messages": [{"role": "user", "content": "hi"}]}

    r1 = await per_ip_strict_client.post("/v1/chat/completions", headers=headers, json=body)
    r2 = await per_ip_strict_client.post("/v1/chat/completions", headers=headers, json=body)
    assert r1.status_code == 200
    assert r2.status_code == 200

    r3 = await per_ip_strict_client.post("/v1/chat/completions", headers=headers, json=body)
    assert r3.status_code == 429
    assert r3.json()["error"]["scope"] == "per_ip"


# --- Compose: both must allow ---------------------------------------------


async def test_both_buckets_compose(strict_client, bearer_token, scripted_adapter) -> None:
    """Per-key=3 / per-IP=2 — per-IP fires first because it's tighter."""
    scripted_adapter.push_response(text="hi")
    scripted_adapter.push_response(text="hi")

    headers = {"Authorization": f"Bearer {bearer_token}"}
    body = {"model": "haiku", "messages": [{"role": "user", "content": "hi"}]}

    assert (
        await strict_client.post("/v1/chat/completions", headers=headers, json=body)
    ).status_code == 200
    assert (
        await strict_client.post("/v1/chat/completions", headers=headers, json=body)
    ).status_code == 200
    r = await strict_client.post("/v1/chat/completions", headers=headers, json=body)
    assert r.status_code == 429
    # per-IP=2 hits before per-key=3.
    assert r.json()["error"]["scope"] == "per_ip"


# --- Exempt paths ----------------------------------------------------------


async def test_healthz_is_not_rate_limited(strict_client) -> None:
    """Even with strict limits, `/healthz` is uncounted."""
    for _ in range(20):
        r = await strict_client.get("/healthz")
        assert r.status_code == 200
        assert "retry-after" not in r.headers


# --- Bucket reset / refill -------------------------------------------------


async def test_bucket_refills_via_unit_test() -> None:
    """The HTTP-level "wait 60 seconds" test would be flaky / slow; verify
    the refill at the unit level instead so the property is still asserted
    in the suite.

    gateway-hardening.md §3.1: capacity equals the refill amount over the
    window, so after a full window the bucket recovers.
    """
    b = _Bucket(capacity=2, window_seconds=60.0)
    start = time.monotonic()
    assert b.take(now=start) is True
    assert b.take(now=start) is True
    assert b.take(now=start) is False
    # 60 seconds later → bucket is full again.
    assert b.take(now=start + 60.0) is True
    assert b.take(now=start + 60.0) is True
    assert b.take(now=start + 60.0) is False
