"""Pure-ASGI token-bucket rate limit middleware (gateway-hardening.md §3).

Two independent buckets compose: a request passes only if both the
**per-key** bucket (keyed on the resolved `GatewayKey.key_id`) and the
**per-IP** bucket (keyed on the client IP parsed from `X-Forwarded-For`
or the ASGI socket peer) admit it. Either bucket exhausted → HTTP 429
with a `Retry-After` header.

This is a defense-in-depth layer alongside the per-key spend quotas in
[`quotas.py`](quotas.py): quotas bound total dollars over a day/month;
rate limits bound *requests per minute* so a leaked key can't drain its
daily cap before the operator can rotate it.

Single-process in-memory state. Multi-instance deployments see roughly
twice the configured limit per key — accepted in v1; the daily spend cap
is the durable backstop (gateway-hardening.md §3.3 / §8).

Pure ASGI (not `BaseHTTPMiddleware`) so SSE response bodies are not
buffered — matches the pattern in `middleware_versioning.py`.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Literal

import msgspec
from starlette.types import ASGIApp, Receive, Scope, Send

from metis_gateway.auth import extract_bearer_token

logger = logging.getLogger(__name__)

# gateway-hardening.md §3.1 — buyer-recommended defaults.
DEFAULT_PER_KEY_RPM = 60
DEFAULT_PER_IP_RPM = 1000

# gateway-hardening.md §3.3 — bounded LRU per bucket type.
DEFAULT_MAX_TRACKED_KEYS = 1000

# Floating-point tolerance for token-refill drift. The lazy refill in
# `_Bucket.take()` accumulates via `elapsed * (capacity / window)`, which
# can land fractionally below 1.0 after an exact-period advance (CI run
# 26067891870 hit `tokens=0.9999999999999716` after a 30-second refill at
# capacity=60 / window=60). The epsilon lets `take()` succeed when the
# shortfall is purely arithmetic noise.
_TOKEN_EPSILON = 1e-9

# Paths the limiter applies to. `/healthz` and future Metis-owned paths
# are exempt (gateway-hardening.md §3.4).
RATE_LIMITED_PREFIXES: tuple[str, ...] = (
    "/v1/chat/completions",
    "/v1/messages",
)

BucketName = Literal["per_key", "per_ip"]


@dataclass(frozen=True)
class RateLimitConfig:
    """Configuration knobs for the rate-limit middleware.

    `enabled=False` makes the middleware a no-op; the gateway uses that
    as the v1 default per gateway-hardening.md §7 (opt-in until Wave 12+
    promotes it to the recommended default).

    `trusted_proxies` is the list of CIDRs the middleware treats as
    forwarders when parsing `X-Forwarded-For` (gateway-hardening.md §3.5).
    Default empty: read only the socket peer.
    """

    enabled: bool = False
    per_key_rpm: int = DEFAULT_PER_KEY_RPM
    per_ip_rpm: int = DEFAULT_PER_IP_RPM
    max_tracked_keys: int = DEFAULT_MAX_TRACKED_KEYS
    trusted_proxies: tuple[str, ...] = ()


@dataclass
class _Bucket:
    """A single token bucket. `capacity == refill_per_window`.

    Lazy refill: the bucket only updates `tokens` when `take()` is called.
    `last_refill` is the wall-clock seconds at which `tokens` was last
    materialized; a call N seconds later credits
    `N * capacity / window_seconds` tokens, capped at `capacity`.
    """

    capacity: float
    window_seconds: float
    tokens: float = field(init=False)
    last_refill: float = field(init=False)

    def __post_init__(self) -> None:
        self.tokens = self.capacity
        self.last_refill = time.monotonic()

    def take(self, *, now: float, cost: float = 1.0) -> bool:
        elapsed = max(0.0, now - self.last_refill)
        if elapsed > 0:
            refill = elapsed * (self.capacity / self.window_seconds)
            self.tokens = min(self.capacity, self.tokens + refill)
            self.last_refill = now
        if self.tokens + _TOKEN_EPSILON >= cost:
            self.tokens = max(0.0, self.tokens - cost)
            return True
        return False

    def retry_after_seconds(self, *, now: float, cost: float = 1.0) -> int:
        """Whole seconds (≥1) until `cost` tokens are available."""
        deficit = max(0.0, cost - self.tokens)
        rate = self.capacity / self.window_seconds
        seconds = deficit / rate if rate > 0 else self.window_seconds
        # Round up; min 1 so clients always back off at least one second
        # even when the deficit is tiny (sub-second).
        return max(1, int(seconds) + (0 if seconds.is_integer() else 1))


class _BucketRegistry:
    """Bounded LRU map of bucket-key → `_Bucket`.

    New entries push older entries out once `max_entries` is exceeded.
    The eviction policy is plain LRU — a sustained flood from one IP
    can't displace another IP's bucket as long as that other IP keeps
    sending traffic.
    """

    def __init__(self, *, capacity: float, window_seconds: float, max_entries: int) -> None:
        self._capacity = capacity
        self._window = window_seconds
        self._max = max_entries
        self._buckets: OrderedDict[str, _Bucket] = OrderedDict()

    def get(self, key: str) -> _Bucket:
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _Bucket(capacity=self._capacity, window_seconds=self._window)
            self._buckets[key] = bucket
            if len(self._buckets) > self._max:
                self._buckets.popitem(last=False)
        else:
            self._buckets.move_to_end(key)
        return bucket

    def __len__(self) -> int:
        return len(self._buckets)


def _is_rate_limited_path(path: str) -> bool:
    return any(path == prefix or path.startswith(prefix + "/") for prefix in RATE_LIMITED_PREFIXES)


def _header(scope: Scope, name: bytes) -> bytes | None:
    for header_name, value in scope.get("headers", []):
        if header_name == name:
            return value
    return None


def _client_ip(scope: Scope, *, trusted_proxies: tuple[str, ...]) -> str | None:
    """Resolve the request's source IP per gateway-hardening.md §3.2 / §3.5.

    When `trusted_proxies` is non-empty and the immediate ASGI peer is in
    one of those CIDRs, walk `X-Forwarded-For` right-to-left and pick the
    first untrusted hop. Otherwise fall back to the ASGI socket peer.
    """
    client = scope.get("client")
    socket_peer = client[0] if client and len(client) > 0 else None

    forwarded = _header(scope, b"x-forwarded-for")
    if forwarded and trusted_proxies and socket_peer and _ip_in_any(socket_peer, trusted_proxies):
        for raw in reversed(forwarded.decode("latin-1").split(",")):
            candidate = raw.strip()
            if not candidate:
                continue
            if _ip_in_any(candidate, trusted_proxies):
                continue
            try:
                ipaddress.ip_address(candidate)
            except ValueError:
                continue
            return candidate
    return socket_peer


def _ip_in_any(ip: str, cidrs: tuple[str, ...]) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for cidr in cidrs:
        try:
            if addr in ipaddress.ip_network(cidr, strict=False):
                return True
        except ValueError:
            continue
    return False


def _bearer_key_fingerprint(scope: Scope) -> str | None:
    """Derive a stable per-key bucket id from the request's auth header.

    The middleware runs **before** auth (so 401s aren't rate-limited
    against the same bucket as authenticated requests), so it cannot
    read `key_id` directly. SHA-256 of the bearer token hex digest is
    stable, lookup-free, and gives the per-key bucket the same identity
    the keystore uses (the keystore stores SHA-256 of the same string).

    Returns `None` when no recognizable bearer / x-api-key is present —
    the request will short-circuit at auth anyway, and we don't want
    pre-auth traffic competing for the per-key bucket.
    """
    auth_header = _header(scope, b"authorization")
    bearer = (
        extract_bearer_token(auth_header.decode("latin-1")) if auth_header is not None else None
    )
    if bearer is None:
        x_api_key = _header(scope, b"x-api-key")
        if x_api_key is not None:
            bearer = x_api_key.decode("latin-1").strip() or None
    if not bearer:
        return None
    return hashlib.sha256(bearer.encode("utf-8")).hexdigest()


def _inbound_shape(path: str) -> Literal["openai", "anthropic"]:
    """The error envelope shape to use on 429.

    Matches `app.py`'s `_openai_error` / `_anthropic_error` envelopes so
    clients see the per-shape framing they expect.
    """
    if path.startswith("/v1/messages"):
        return "anthropic"
    return "openai"


def _build_429_body(
    *,
    bucket: BucketName,
    rpm: int,
    retry_after: int,
    shape: Literal["openai", "anthropic"],
) -> bytes:
    """Construct the inbound-shape-matched 429 response body.

    gateway-hardening.md §3.4 spec'd shape; the OpenAI envelope carries
    the `scope` and `retry_after_seconds` fields so SDK callers can read
    the structured details, while Anthropic's envelope sticks to the
    minimal `{type, message}` pair its parser expects.
    """
    message = f"{bucket.replace('_', '-')} rate limit exceeded ({rpm} rpm); retry in {retry_after}s"
    if shape == "openai":
        body = {
            "error": {
                "code": "rate_limit_exceeded",
                "type": "rate_limit_error",
                "message": message,
                "scope": bucket,
                "retry_after_seconds": retry_after,
            }
        }
    else:
        body = {
            "error": {
                "type": "rate_limit_error",
                "message": message,
            }
        }
    return msgspec.json.encode(body)


class RateLimitMiddleware:
    """Pure-ASGI middleware enforcing per-key + per-IP token buckets.

    A request to a rate-limited path must pass both buckets; either
    deny short-circuits with HTTP 429 and a `Retry-After` header. The
    middleware is **off by default** — construct with
    `RateLimitConfig(enabled=True)` to enable.

    Construction is cheap; the middleware owns two `_BucketRegistry`
    instances backed by `OrderedDict`. The per-key registry is bounded
    by `config.max_tracked_keys`; the per-IP registry by the same.
    """

    def __init__(self, app: ASGIApp, *, config: RateLimitConfig | None = None) -> None:
        self.app = app
        self.config = config or RateLimitConfig()
        self._per_key = _BucketRegistry(
            capacity=float(self.config.per_key_rpm),
            window_seconds=60.0,
            max_entries=self.config.max_tracked_keys,
        )
        self._per_ip = _BucketRegistry(
            capacity=float(self.config.per_ip_rpm),
            window_seconds=60.0,
            max_entries=self.config.max_tracked_keys,
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self.config.enabled or scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if not _is_rate_limited_path(path):
            await self.app(scope, receive, send)
            return

        shape = _inbound_shape(path)
        now = time.monotonic()

        key_fp = _bearer_key_fingerprint(scope)
        if key_fp is not None:
            bucket = self._per_key.get(key_fp)
            if not bucket.take(now=now):
                retry = bucket.retry_after_seconds(now=now)
                logger.warning(
                    "rate-limit deny bucket=per_key rpm=%d retry_after=%ds path=%s key_fp=%s",
                    self.config.per_key_rpm,
                    retry,
                    path,
                    key_fp[:8],
                )
                await self._send_429(
                    send,
                    bucket_name="per_key",
                    rpm=self.config.per_key_rpm,
                    retry_after=retry,
                    shape=shape,
                )
                return

        ip = _client_ip(scope, trusted_proxies=self.config.trusted_proxies)
        if ip is not None:
            bucket = self._per_ip.get(ip)
            if not bucket.take(now=now):
                retry = bucket.retry_after_seconds(now=now)
                logger.warning(
                    "rate-limit deny bucket=per_ip rpm=%d retry_after=%ds path=%s ip=%s",
                    self.config.per_ip_rpm,
                    retry,
                    path,
                    ip,
                )
                await self._send_429(
                    send,
                    bucket_name="per_ip",
                    rpm=self.config.per_ip_rpm,
                    retry_after=retry,
                    shape=shape,
                )
                return

        await self.app(scope, receive, send)

    async def _send_429(
        self,
        send: Send,
        *,
        bucket_name: BucketName,
        rpm: int,
        retry_after: int,
        shape: Literal["openai", "anthropic"],
    ) -> None:
        body = _build_429_body(bucket=bucket_name, rpm=rpm, retry_after=retry_after, shape=shape)
        await send(
            {
                "type": "http.response.start",
                "status": 429,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"retry-after", str(retry_after).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body, "more_body": False})


__all__ = [
    "DEFAULT_MAX_TRACKED_KEYS",
    "DEFAULT_PER_IP_RPM",
    "DEFAULT_PER_KEY_RPM",
    "RATE_LIMITED_PREFIXES",
    "RateLimitConfig",
    "RateLimitMiddleware",
]
