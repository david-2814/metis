"""Starlette ASGI app for the Metis gateway.

Endpoints (v1):

  POST /v1/chat/completions   OpenAI-shape, sync + SSE streaming.
  POST /v1/messages           Anthropic-shape, sync + SSE streaming.
  GET  /healthz               Liveness.

Each inbound translator lives next to its handler (`translators.py` for the
OpenAI shape, `endpoints/anthropic.py` for the Anthropic shape) so a
provider quirk in one shape can't bleed into the other.
"""

from __future__ import annotations

import hashlib
import logging
import socket
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

import msgspec
from metis_core.adapters.tool_id_map import ToolIdMap
from metis_core.canonical.ids import new_message_id
from metis_core.events.envelope import Actor
from metis_core.events.payloads import GatewayAuthFailed, make_event
from metis_core.extensions import (
    AnalyticsExtension,
    BillingBackend,
    NoopAnalyticsExtension,
    NoopBillingBackend,
    NoopSignupBackend,
    SignupBackend,
)
from metis_core.observability import METRICS_CONTENT_TYPE, MetricsCollector
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from metis_gateway.auth import extract_bearer_token, identity_from_key
from metis_gateway.billing import (
    BillingClient,
    BillingConfig,
    BillingState,
    billing_cancel_handler,
    billing_pause_handler,
    billing_payment_method_handler,
    billing_plan_handler,
    billing_portal_handler,
    billing_status_handler,
    build_billing_state,
    stripe_webhook_handler,
)
from metis_gateway.billing.routes import (
    billing_error_response,
    billing_resume_handler,
    billing_subscribe_handler,
)
from metis_gateway.billing.subscriptions import BillingError
from metis_gateway.endpoints.anthropic import (
    InboundTranslationError as AnthropicInboundTranslationError,
)
from metis_gateway.endpoints.anthropic import (
    anthropic_error_envelope,
    parse_anthropic_request,
    render_anthropic_response,
)
from metis_gateway.endpoints.anthropic import (
    render_sse_stream as render_anthropic_sse_stream,
)
from metis_gateway.harness import (
    ClientDisconnected,
    GatewayHarness,
    ModelNotAllowedError,
    RoutingFailedError,
    UpstreamProviderError,
    make_disconnect_probe,
)
from metis_gateway.middleware_ratelimit import RateLimitConfig, RateLimitMiddleware
from metis_gateway.middleware_versioning import VersioningMiddleware
from metis_gateway.quotas import (
    QuotaExceeded,
    RequestQuotaCache,
    TierCaps,
    enforce_quotas,
)
from metis_gateway.runtime import GatewayRuntime
from metis_gateway.signup import (
    SignupConfig,
    SignupError,
    SignupState,
    account_keys_create_handler,
    account_keys_list_handler,
    account_keys_revoke_handler,
    build_signup_state,
    signup_error_response,
    signup_handler,
    signup_verify_handler,
)
from metis_gateway.translators import (
    InboundTranslationError,
    parse_openai_request,
    render_openai_response,
    render_openai_sse_stream,
)

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8422
# gateway-hardening.md §2.1 / §3 — connection-rate hardening defaults.
# `limit_concurrency` is what uvicorn calls a per-process cap on in-flight
# requests + connections; gateway-hardening.md §2.1 names "per-IP concurrent
# connections" but the realistic mitigation under in-process uvicorn is the
# per-process ceiling (the per-IP slice is what the buyer's edge LB / WAF
# does). Default 1000 matches the spec recommendation.
DEFAULT_MAX_CONCURRENT_CONNECTIONS = 1000
# uvicorn's default backlog is 2048; we restate it as a config knob so
# graceful-restart tuning (SO_REUSEPORT + larger backlog) is one place.
DEFAULT_BACKLOG = 2048


class GatewayConfigError(ValueError):
    """Raised when a `GatewayConfig` is internally inconsistent.

    Examples: `tls_cert` set without `tls_key`, cert file missing on disk.
    Surfacing this as a typed exception lets the CLI render a clean
    diagnostic instead of an opaque uvicorn stacktrace.
    """


@dataclass
class GatewayConfig:
    """Configuration for `run_gateway`.

    Bind posture (gateway-hardening.md §2.1):

    - `host` defaults to `127.0.0.1` (loopback-only). Setting it to
      `0.0.0.0` exposes the gateway on every interface; the rate-limit
      middleware (§3), audit logging (audit-log.md), and TLS termination
      (either in-process via `tls_cert`/`tls_key` or via an upstream
      terminator per §2) MUST be in place before doing so on the open
      internet. `run_gateway` no longer silently rewrites a non-loopback
      host — it logs a one-time warning summarizing the hardening checklist.

    Connection-rate hardening (gateway-hardening.md §2.1 / §6):

    - `max_concurrent_connections` caps in-flight requests + open
      connections per process (uvicorn's `limit_concurrency`). Excess
      connections are answered with HTTP 503 immediately rather than
      queued, which is the right shape for a transparent proxy: a leaked
      key flooding the gateway hits the cap before exhausting the event
      loop. The buyer's edge layer (CDN/WAF) handles volumetric DDoS;
      this is the in-process backstop.
    - `backlog` sets the listen-socket queue depth; 2048 matches
      uvicorn's default and is plenty for the connection-cap shape above.
    - `reuse_port` opts into `SO_REUSEPORT` on the listen socket so two
      gateway processes can bind the same port for graceful restarts /
      blue-green rollouts. Single-process operation doesn't need it;
      the helm chart's multi-pod / multi-process recipe does.

    TLS-in-process (gateway-hardening.md §2):

    - `tls_cert` + `tls_key` (both or neither). When both are set,
      uvicorn terminates TLS in-process. The recommended posture is
      still a sidecar terminator (nginx-ingress / Caddy / cloud LB) per
      §2 — running TLS in-process is for buyers who don't want a
      sidecar in their topology.
    """

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    # gateway-hardening.md §3 — opt-in until Wave 12+ promotes to default.
    rate_limit: RateLimitConfig = field(default_factory=RateLimitConfig)
    # Wave 13 — connection-rate hardening + in-process TLS.
    max_concurrent_connections: int = DEFAULT_MAX_CONCURRENT_CONNECTIONS
    backlog: int = DEFAULT_BACKLOG
    reuse_port: bool = False
    tls_cert: Path | None = None
    tls_key: Path | None = None
    # Wave 14 — self-serve signup. Off by default; SaaS deployments opt in.
    signup: SignupConfig | None = None
    # Wave 15 — Stripe-backed billing per pricing.md §5.5.4. Off by default;
    # mounting `/account/billing/*` + `/webhooks/stripe` requires the
    # operator to flip `enabled=True` and supply `stripe_api_key` +
    # `stripe_webhook_secret`. Pre-Wave-15 deployments are byte-identical.
    billing: BillingConfig | None = None
    # Wave 17 (planned) — repo-split extension Protocols. metis-pro overlays
    # substitute real implementations via the composition root; OSS-only
    # deployments keep the noop defaults. See docs/operations/repo-split-plan.md
    # §3 and packages/metis-core/src/metis_core/extensions.py. These fields
    # are scaffolding in §4.1 — they accept Protocol implementations but the
    # gateway hot-path still routes through the existing billing/signup
    # modules until the migration steps §4.2 / §4.3 land.
    billing_backend: BillingBackend = field(default_factory=NoopBillingBackend)
    signup_backend: SignupBackend = field(default_factory=NoopSignupBackend)
    analytics_extension: AnalyticsExtension = field(default_factory=NoopAnalyticsExtension)

    def __post_init__(self) -> None:
        if (self.tls_cert is None) != (self.tls_key is None):
            raise GatewayConfigError(
                "tls_cert and tls_key must be set together (got one without the other)"
            )
        if self.tls_cert is not None and not self.tls_cert.exists():
            raise GatewayConfigError(f"tls_cert file not found: {self.tls_cert}")
        if self.tls_key is not None and not self.tls_key.exists():
            raise GatewayConfigError(f"tls_key file not found: {self.tls_key}")
        if self.max_concurrent_connections < 1:
            raise GatewayConfigError(
                f"max_concurrent_connections must be >= 1 (got {self.max_concurrent_connections})"
            )
        if self.backlog < 1:
            raise GatewayConfigError(f"backlog must be >= 1 (got {self.backlog})")

    @property
    def tls_enabled(self) -> bool:
        return self.tls_cert is not None and self.tls_key is not None


@dataclass
class _AppState:
    runtime: GatewayRuntime
    started_at: datetime
    metrics: MetricsCollector
    signup: SignupState | None = None
    billing: BillingState | None = None


def build_app(
    runtime: GatewayRuntime,
    *,
    rate_limit: RateLimitConfig | None = None,
    signup: SignupConfig | None = None,
    billing: BillingConfig | None = None,
    billing_client: BillingClient | None = None,
) -> Starlette:
    """Build the Starlette ASGI app bound to a fully-wired GatewayRuntime.

    `rate_limit` follows gateway-hardening.md §3 — off by default; pass
    `RateLimitConfig(enabled=True, ...)` to engage the per-key / per-IP
    buckets in front of the provider-shape paths.

    `signup` follows gateway.md §"Self-serve signup" (Wave 14) — off by
    default; passing a `SignupConfig(enabled=True, ...)` mounts the
    ``/signup``, ``/signup/verify``, and ``/account/keys`` routes. Magic
    links are logged to stdout in v1 (no real email transport); flipping
    this on in production requires the operator to swap in a real email
    sender before the endpoints face the open internet.

    `billing` follows pricing.md §5.5.4 (Wave 15) — off by default;
    passing a `BillingConfig(enabled=True, ...)` mounts the
    ``/account/billing/*`` routes and the ``/webhooks/stripe``
    listener. `billing_client` is the test-injection seam: pass a
    `FakeBillingClient` to bypass the real Stripe wrapper.
    """
    metrics = MetricsCollector(
        bus=runtime.bus,
        gateway_keys_getter=lambda: _count_gateway_keys(runtime),
        # docs/operations/trace-performance.md §WAL: gauge on the trace
        # DB's WAL file size. The gateway is the highest-throughput
        # writer, so the WAL gauge belongs here above all.
        trace_wal_bytes_getter=lambda: runtime.trace.wal_size_bytes(),
    )
    metrics.attach()
    # Resolve signup paths against the runtime's db_path / keystore so a
    # caller that only passes `enabled=True` still gets the right wiring.
    signup_config = _resolve_signup_config(signup, runtime)
    signup_state = build_signup_state(signup_config)
    billing_state = build_billing_state(billing, runtime.bus, client=billing_client)
    state = _AppState(
        runtime=runtime,
        started_at=datetime.now(UTC),
        metrics=metrics,
        signup=signup_state,
        billing=billing_state,
    )

    async def _err_handler(_request: Request, exc: Exception) -> Response:
        if isinstance(exc, BillingError):
            return billing_error_response(exc)
        if isinstance(exc, SignupError):
            return signup_error_response(exc)
        if isinstance(exc, HTTPException):
            return _error_response(exc.detail or "request rejected", status=exc.status_code)
        logger.exception("unhandled error in gateway endpoint")
        return _error_response("internal server error", status=500, code="internal_error")

    async def _signup_err_handler(_request: Request, exc: Exception) -> Response:
        assert isinstance(exc, SignupError)
        return signup_error_response(exc)

    async def _billing_err_handler(_request: Request, exc: Exception) -> Response:
        assert isinstance(exc, BillingError)
        return billing_error_response(exc)

    routes = [
        Route("/healthz", _health, methods=["GET"]),
        Route("/metrics", _metrics, methods=["GET"]),
        Route("/v1/chat/completions", chat_completions, methods=["POST"]),
        Route("/v1/messages", messages, methods=["POST"]),
    ]
    if signup_state is not None:
        routes.extend(
            [
                Route("/signup", signup_handler, methods=["POST"]),
                Route("/signup/verify", signup_verify_handler, methods=["POST"]),
                Route("/account/keys", account_keys_list_handler, methods=["GET"]),
                Route("/account/keys", account_keys_create_handler, methods=["POST"]),
                Route(
                    "/account/keys/{key_id}",
                    account_keys_revoke_handler,
                    methods=["DELETE"],
                ),
            ]
        )
    if billing_state is not None:
        # Billing routes piggy-back on the signup-session auth — the
        # gateway's stance is "if you're not running signup, you don't
        # have an account model to bill" so we require signup to be on.
        if signup_state is None:
            raise GatewayConfigError(
                "BillingConfig.enabled=True requires SignupConfig.enabled=True"
            )
        routes.extend(
            [
                Route("/account/billing", billing_status_handler, methods=["GET"]),
                Route("/account/billing/portal", billing_portal_handler, methods=["GET"]),
                Route("/account/billing/plan", billing_plan_handler, methods=["POST"]),
                Route(
                    "/account/billing/subscribe",
                    billing_subscribe_handler,
                    methods=["POST"],
                ),
                Route(
                    "/account/billing/payment-method",
                    billing_payment_method_handler,
                    methods=["POST"],
                ),
                Route("/account/billing/cancel", billing_cancel_handler, methods=["POST"]),
                Route("/account/billing/pause", billing_pause_handler, methods=["POST"]),
                Route("/account/billing/resume", billing_resume_handler, methods=["POST"]),
                Route("/webhooks/stripe", stripe_webhook_handler, methods=["POST"]),
            ]
        )
    middleware_stack: list[Middleware] = [Middleware(VersioningMiddleware)]
    if rate_limit is not None and rate_limit.enabled:
        middleware_stack.append(Middleware(RateLimitMiddleware, config=rate_limit))
    app = Starlette(
        routes=routes,
        exception_handlers={
            BillingError: _billing_err_handler,
            SignupError: _signup_err_handler,
            Exception: _err_handler,
        },
        middleware=middleware_stack,
    )
    app.state.app_state = state
    return app


def _resolve_signup_config(
    config: SignupConfig | None, runtime: GatewayRuntime
) -> SignupConfig | None:
    """Fill defaults that depend on the runtime (db path, keystore path).

    The signup module ships with home-relative fallbacks for both, but a
    caller that wires the runtime against a non-default DB / keystore
    expects signup to honor those without restating them.
    """
    if config is None or not config.enabled:
        return config
    if config.db_path is None:
        config.db_path = runtime.db_file
    return config


def _state(request: Request) -> _AppState:
    return request.app.state.app_state


async def _health(request: Request) -> Response:
    st = _state(request)
    uptime = (datetime.now(UTC) - st.started_at).total_seconds()
    return _json({"status": "ok", "uptime_seconds": round(uptime, 3)})


async def _metrics(request: Request) -> Response:
    """GET /metrics — Prometheus exposition for in-cluster scrapers.

    Loopback-only by virtue of the gateway's bind posture (see
    `run_gateway`); production scrape goes through the proxy sidecar
    + ServiceMonitor (helm chart `monitoring.enabled`).
    """
    st = _state(request)
    body = st.metrics.expose()
    return Response(content=body, media_type=METRICS_CONTENT_TYPE)


def _emit_auth_failed(
    runtime: GatewayRuntime,
    *,
    reason: Literal["missing_token", "invalid_token", "key_revoked"],
    inbound_shape: Literal["openai", "anthropic"],
    token: str | None,
    gateway_key_id: str | None = None,
) -> None:
    """Audit + metric every rejected auth attempt.

    Persists a `gateway.auth_failed` event on the bus (audit-flagged per
    `audit-log.md §AUDIT_EVENT_TYPES`) and bumps
    `metis_gateway_auth_failures_total{reason}` via the bus subscriber.
    The token is hashed to an 8-char SHA-256 prefix so SIEM operators
    can correlate repeated attempts of the same leaked credential
    without persisting the credential itself — the full hash is too
    long to bucket usefully and the raw token would defeat the
    purpose. Best-effort: bus emission errors are logged and swallowed
    so an observability glitch can't open a side-channel that bypasses
    the 401 response.
    """
    token_hash_prefix: str | None = None
    if token:
        token_hash_prefix = hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]
    try:
        runtime.bus.emit(
            make_event(
                type="gateway.auth_failed",
                session_id=f"gw_{new_message_id()}",
                actor=Actor.SYSTEM,
                payload=GatewayAuthFailed(
                    reason=reason,
                    inbound_shape=inbound_shape,
                    token_hash_prefix=token_hash_prefix,
                    gateway_key_id=gateway_key_id,
                ),
                timestamp=datetime.now(UTC),
            )
        )
    except Exception:
        logger.warning("failed to emit gateway.auth_failed", exc_info=True)


def _count_gateway_keys(runtime: GatewayRuntime) -> tuple[int, int]:
    """Tally `(active, revoked)` for the keystore at scrape time.

    Grace-period-expired keys are still on disk as `status="active"`
    but `is_active(now=…)` returns False until the next admin sweep
    persists the revocation; the gauge reflects the auth-time view.
    """
    now = datetime.now(UTC)
    active = 0
    revoked = 0
    for key in runtime.keystore.keys():
        if key.is_active(now=now):
            active += 1
        else:
            revoked += 1
    return active, revoked


async def chat_completions(request: Request) -> Response:
    """POST /v1/chat/completions — OpenAI-shape endpoint (sync + SSE).

    Translates the inbound OpenAI request, runs routing + adapter through
    `GatewayHarness`, and returns either an OpenAI-shape JSON body or, when
    `stream: true` was set, a `text/event-stream` of `chat.completion.chunk`
    frames terminated by `data: [DONE]`.
    """
    st = _state(request)
    runtime = st.runtime

    bearer = extract_bearer_token(request.headers.get("authorization"))
    key = runtime.keystore.authenticate(bearer or "")
    if key is None:
        _emit_auth_failed(
            runtime,
            reason="missing_token" if not bearer else "invalid_token",
            inbound_shape="openai",
            token=bearer,
        )
        return _openai_error(
            "invalid or missing API key",
            status=401,
            type_="invalid_request_error",
            code="invalid_api_key",
        )
    now = datetime.now(UTC)
    if not key.is_active(now=now):
        _emit_auth_failed(
            runtime,
            reason="key_revoked",
            inbound_shape="openai",
            token=bearer,
            gateway_key_id=key.key_id,
        )
        return _key_revoked_response(
            key_id=key.key_id,
            revoked_at=key.effective_revoked_at(now=now),
            shape="openai",
        )

    identity = identity_from_key(key)
    quota_cache = _build_quota_cache(runtime)
    if quota_cache is not None:
        verdict = enforce_quotas(
            bus=runtime.bus,
            cache=quota_cache,
            key=key,
            identity=identity,
            inbound_shape="openai",
            tier_caps=_resolve_tier_caps(st, key),
        )
        if verdict is not None:
            return _quota_exceeded_response(verdict, shape="openai")

    try:
        raw = await request.body()
    except Exception as exc:
        return _openai_error(
            f"could not read request body: {exc}",
            status=400,
            type_="invalid_request_error",
        )
    try:
        body = msgspec.json.decode(raw) if raw else {}
    except Exception as exc:
        return _openai_error(
            f"invalid JSON body: {exc}",
            status=400,
            type_="invalid_request_error",
        )

    tool_map = ToolIdMap()
    try:
        parsed = parse_openai_request(body, tool_map=tool_map)
    except InboundTranslationError as exc:
        return _openai_error(str(exc), status=400, type_="invalid_request_error")

    harness = GatewayHarness(
        bus=runtime.bus,
        registry=runtime.registry,
        routing=runtime.routing,
        pricing=runtime.pricing,
        global_default_model=runtime.global_default_model,
        inbound_shape="openai",
    )

    probe = make_disconnect_probe(request.is_disconnected)
    team_budget_remaining = _team_budget_remaining(quota_cache, key)

    if parsed.stream:
        return await _stream_chat_completions(
            harness=harness,
            parsed=parsed,
            key=key,
            identity=identity,
            probe=probe,
            tool_map=tool_map,
            team_budget_remaining_usd=team_budget_remaining,
        )

    try:
        result = await harness.call(
            messages=parsed.messages,
            tools=parsed.tools,
            system_prompt=parsed.system_prompt,
            max_output_tokens=parsed.max_output_tokens,
            temperature=parsed.temperature,
            stop_sequences=parsed.stop_sequences,
            output_schema=parsed.output_schema,
            requested_model=parsed.model,
            identity=identity,
            allowed_models=key.allowed_models,
            is_disconnected=probe,
            team_budget_remaining_usd=team_budget_remaining,
        )
    except RoutingFailedError as exc:
        return _openai_error(
            str(exc),
            status=503,
            type_="api_error",
            code="routing_failed",
        )
    except ModelNotAllowedError as exc:
        return _openai_error(str(exc), status=403, type_="invalid_request_error")
    except UpstreamProviderError as exc:
        return _openai_error_from_adapter(exc)
    except ClientDisconnected:
        # No response body — connection's already gone. Starlette will close
        # the (already-closed) socket. Return a 499-style sentinel just in
        # case the connection actually survived.
        return Response(status_code=499)

    payload = render_openai_response(
        result.response,
        requested_model=parsed.model,
        tool_map=tool_map,
    )
    return _json(payload)


async def _stream_chat_completions(
    *,
    harness: GatewayHarness,
    parsed,
    key,
    identity,
    probe,
    tool_map: ToolIdMap,
    team_budget_remaining_usd,
) -> Response:
    """Drive `GatewayHarness.stream(...)` and return an SSE StreamingResponse.

    Pre-flight errors (routing failure, model-not-allowed) must return a JSON
    error body, not a 200 SSE stream. We prime the async generator once to
    surface any synchronous exception before committing the response code.
    """
    stream_iter = harness.stream(
        messages=parsed.messages,
        tools=parsed.tools,
        system_prompt=parsed.system_prompt,
        max_output_tokens=parsed.max_output_tokens,
        temperature=parsed.temperature,
        stop_sequences=parsed.stop_sequences,
        output_schema=parsed.output_schema,
        requested_model=parsed.model,
        identity=identity,
        allowed_models=key.allowed_models,
        is_disconnected=probe,
        team_budget_remaining_usd=team_budget_remaining_usd,
    )

    try:
        first_event = await stream_iter.__anext__()
    except StopAsyncIteration:
        first_event = None
    except RoutingFailedError as exc:
        return _openai_error(str(exc), status=503, type_="api_error", code="routing_failed")
    except ModelNotAllowedError as exc:
        return _openai_error(str(exc), status=403, type_="invalid_request_error")
    except UpstreamProviderError as exc:
        return _openai_error_from_adapter(exc)
    except ClientDisconnected:
        return Response(status_code=499)

    async def replay():
        if first_event is not None:
            yield first_event
        try:
            async for event in stream_iter:
                yield event
        except ClientDisconnected:
            return
        except UpstreamProviderError:
            logger.exception("upstream provider error during streaming")
            return

    sse_bytes = render_openai_sse_stream(
        replay(),
        requested_model=parsed.model,
        tool_map=tool_map,
        include_usage=parsed.include_usage,
    )
    return StreamingResponse(
        sse_bytes,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


# ---------------------------------------------------------------------------
# Anthropic-shape inbound (`POST /v1/messages`)
# ---------------------------------------------------------------------------


async def messages(request: Request) -> Response:
    """POST /v1/messages — Anthropic Messages API shape (sync + SSE).

    Auth accepts either `x-api-key` (Anthropic SDK convention) or
    `Authorization: Bearer ...` (gateway.md §3.3); Anthropic clients reach
    for `x-api-key` first.
    """
    st = _state(request)
    runtime = st.runtime

    token = request.headers.get("x-api-key") or extract_bearer_token(
        request.headers.get("authorization")
    )
    key = runtime.keystore.authenticate(token or "")
    if key is None:
        _emit_auth_failed(
            runtime,
            reason="missing_token" if not token else "invalid_token",
            inbound_shape="anthropic",
            token=token,
        )
        return _anthropic_error(
            "invalid or missing API key", status=401, type_="authentication_error"
        )
    now = datetime.now(UTC)
    if not key.is_active(now=now):
        _emit_auth_failed(
            runtime,
            reason="key_revoked",
            inbound_shape="anthropic",
            token=token,
            gateway_key_id=key.key_id,
        )
        return _key_revoked_response(
            key_id=key.key_id,
            revoked_at=key.effective_revoked_at(now=now),
            shape="anthropic",
        )

    identity = identity_from_key(key)
    quota_cache = _build_quota_cache(runtime)
    if quota_cache is not None:
        verdict = enforce_quotas(
            bus=runtime.bus,
            cache=quota_cache,
            key=key,
            identity=identity,
            inbound_shape="anthropic",
            tier_caps=_resolve_tier_caps(st, key),
        )
        if verdict is not None:
            return _quota_exceeded_response(verdict, shape="anthropic")

    try:
        raw = await request.body()
    except Exception as exc:
        return _anthropic_error(
            f"could not read request body: {exc}", status=400, type_="invalid_request_error"
        )
    try:
        body = msgspec.json.decode(raw) if raw else {}
    except Exception as exc:
        return _anthropic_error(
            f"invalid JSON body: {exc}", status=400, type_="invalid_request_error"
        )

    try:
        parsed = parse_anthropic_request(body)
    except AnthropicInboundTranslationError as exc:
        return _anthropic_error(str(exc), status=400, type_="invalid_request_error")

    harness = GatewayHarness(
        bus=runtime.bus,
        registry=runtime.registry,
        routing=runtime.routing,
        pricing=runtime.pricing,
        global_default_model=runtime.global_default_model,
        inbound_shape="anthropic",
    )

    probe = make_disconnect_probe(request.is_disconnected)
    team_budget_remaining = _team_budget_remaining(quota_cache, key)

    if parsed.stream:
        return await _stream_messages(
            harness=harness,
            parsed=parsed,
            key=key,
            identity=identity,
            probe=probe,
            team_budget_remaining_usd=team_budget_remaining,
        )

    try:
        result = await harness.call(
            messages=parsed.messages,
            tools=parsed.tools,
            system_prompt=parsed.system_prompt,
            system_prompt_volatile=parsed.system_prompt_volatile,
            max_output_tokens=parsed.max_output_tokens,
            temperature=parsed.temperature,
            stop_sequences=parsed.stop_sequences,
            output_schema=None,
            requested_model=parsed.model,
            identity=identity,
            allowed_models=key.allowed_models,
            is_disconnected=probe,
            team_budget_remaining_usd=team_budget_remaining,
        )
    except RoutingFailedError as exc:
        return _anthropic_error(str(exc), status=503, type_="overloaded_error")
    except ModelNotAllowedError as exc:
        return _anthropic_error(str(exc), status=403, type_="permission_error")
    except UpstreamProviderError as exc:
        return _anthropic_error_from_adapter(exc)
    except ClientDisconnected:
        return Response(status_code=499)

    payload = render_anthropic_response(result.response, requested_model=parsed.model)
    return _json(payload)


async def _stream_messages(
    *,
    harness: GatewayHarness,
    parsed,
    key,
    identity,
    probe,
    team_budget_remaining_usd,
) -> Response:
    """Drive `GatewayHarness.stream(...)` and return Anthropic SSE.

    Same priming pattern as `_stream_chat_completions`: pre-flight errors
    must come back as a JSON error envelope (gateway.md §8), not a 200 SSE
    stream. We pull the first event before committing to a 200.
    """
    stream_iter = harness.stream(
        messages=parsed.messages,
        tools=parsed.tools,
        system_prompt=parsed.system_prompt,
        system_prompt_volatile=parsed.system_prompt_volatile,
        max_output_tokens=parsed.max_output_tokens,
        temperature=parsed.temperature,
        stop_sequences=parsed.stop_sequences,
        output_schema=None,
        requested_model=parsed.model,
        identity=identity,
        allowed_models=key.allowed_models,
        is_disconnected=probe,
        team_budget_remaining_usd=team_budget_remaining_usd,
    )

    try:
        first_event = await stream_iter.__anext__()
    except StopAsyncIteration:
        first_event = None
    except RoutingFailedError as exc:
        return _anthropic_error(str(exc), status=503, type_="overloaded_error")
    except ModelNotAllowedError as exc:
        return _anthropic_error(str(exc), status=403, type_="permission_error")
    except UpstreamProviderError as exc:
        return _anthropic_error_from_adapter(exc)
    except ClientDisconnected:
        return Response(status_code=499)

    async def replay():
        if first_event is not None:
            yield first_event
        try:
            async for event in stream_iter:
                yield event
        except ClientDisconnected:
            return
        except UpstreamProviderError:
            logger.exception("upstream provider error during anthropic streaming")
            return

    sse_bytes = render_anthropic_sse_stream(replay(), requested_model=parsed.model)
    return StreamingResponse(
        sse_bytes,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def _is_loopback_host(host: str) -> bool:
    return host in ("127.0.0.1", "localhost", "::1")


def _log_non_loopback_warning(cfg: GatewayConfig) -> None:
    """Emit a one-time hardening-checklist line when binding non-loopback.

    The check is advisory, not blocking — Wave 13 lifts the loopback-only
    bind in favor of explicit opt-in (gateway-hardening.md §2.1). Whether
    the hardening layers are *actually* in place is the operator's
    responsibility; this log line names them so a quick `grep WARN`
    surfaces the checklist at boot.
    """
    have_tls = cfg.tls_enabled
    have_rl = cfg.rate_limit.enabled
    logger.warning(
        "gateway bound to non-loopback host=%s port=%d — verify perimeter: "
        "tls_in_process=%s rate_limit=%s. "
        "If TLS is terminated upstream (nginx-ingress/Caddy/cloud LB) and "
        "rate limiting is enforced there, this is fine. Otherwise, see "
        "docs/specs/gateway-hardening.md §2.",
        cfg.host,
        cfg.port,
        "on" if have_tls else "off",
        "on" if have_rl else "off",
    )


def _make_listen_socket(cfg: GatewayConfig) -> socket.socket:
    """Create a bound + listening TCP socket with SO_REUSEADDR (+ SO_REUSEPORT
    when `cfg.reuse_port`).

    Returned to uvicorn via `Server.serve(sockets=[sock])` so that two
    gateway processes can hold the same `(host, port)` for graceful
    restart / rolling deploy. Single-process operation never needs this;
    the helm chart multi-pod recipe enables it via the entrypoint.
    """
    family = socket.AF_INET6 if ":" in cfg.host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if cfg.reuse_port:
        # SO_REUSEPORT is Linux 3.9+ / macOS / BSD. The attribute may be
        # absent on Windows; we read via getattr so the import is harmless.
        reuse_port_const = getattr(socket, "SO_REUSEPORT", None)
        if reuse_port_const is None:
            raise GatewayConfigError(
                "reuse_port=True requested but SO_REUSEPORT is not available on this platform"
            )
        sock.setsockopt(socket.SOL_SOCKET, reuse_port_const, 1)
    sock.bind((cfg.host, cfg.port))
    sock.listen(cfg.backlog)
    sock.set_inheritable(True)
    return sock


def _build_uvicorn_config(app: Starlette, cfg: GatewayConfig):
    """Project a `GatewayConfig` onto a `uvicorn.Config`.

    Extracted from `run_gateway` so tests can inspect the projection
    without actually serving.
    """
    import uvicorn

    kwargs: dict = {
        "host": cfg.host,
        "port": cfg.port,
        "log_level": "info",
        "lifespan": "off",
        "limit_concurrency": cfg.max_concurrent_connections,
        "backlog": cfg.backlog,
    }
    if cfg.tls_enabled:
        kwargs["ssl_certfile"] = str(cfg.tls_cert)
        kwargs["ssl_keyfile"] = str(cfg.tls_key)
    return uvicorn.Config(app, **kwargs)


async def run_gateway(runtime: GatewayRuntime, config: GatewayConfig | None = None) -> None:
    """Run the gateway HTTP server until shutdown.

    Bind posture (gateway-hardening.md §2.1):

    - Default `host="127.0.0.1"` (loopback-only). Pre-Wave-13 the gateway
      silently rewrote any non-loopback host to 127.0.0.1; that constraint
      is lifted. The operator opts into a public bind explicitly via
      `--host 0.0.0.0`, which logs a one-time hardening-checklist warning
      and then trusts the operator.
    - In-process TLS engages when `tls_cert` + `tls_key` are both set.
      The recommended posture remains an upstream terminator (see §2);
      in-process TLS is a convenience for buyers who don't want a sidecar.
    - `SO_REUSEPORT` engages when `reuse_port=True`, allowing graceful
      restart by letting an old + new process bind the same port.
    """
    import uvicorn

    cfg = config or GatewayConfig()
    if not _is_loopback_host(cfg.host):
        _log_non_loopback_warning(cfg)

    app = build_app(
        runtime,
        rate_limit=cfg.rate_limit,
        signup=cfg.signup,
        billing=cfg.billing,
    )
    uvicorn_config = _build_uvicorn_config(app, cfg)
    server = uvicorn.Server(uvicorn_config)

    sockets: list[socket.socket] | None = None
    if cfg.reuse_port:
        sockets = [_make_listen_socket(cfg)]

    try:
        await server.serve(sockets=sockets)
    finally:
        if sockets is not None:
            for sock in sockets:
                sock.close()


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------


def _json(body: dict, *, status: int = 200) -> Response:
    return Response(
        content=msgspec.json.encode(body),
        media_type="application/json",
        status_code=status,
    )


def _error_response(message: str, *, status: int, code: str = "validation_error") -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code, "message": message}},
        status_code=status,
    )


def _openai_error(
    message: str,
    *,
    status: int,
    type_: str,
    code: str | None = None,
) -> Response:
    body: dict = {"error": {"message": message, "type": type_}}
    if code is not None:
        body["error"]["code"] = code
    return _json(body, status=status)


def _openai_error_from_adapter(exc: UpstreamProviderError) -> Response:
    """Map a canonical adapter error class onto an OpenAI-shape error body.

    Mirrors gateway.md §8 (the error-class translation table).
    """
    from metis_core.adapters.errors import ErrorClass

    adapter_error = exc.adapter_error
    cls = adapter_error.error_class
    if cls == ErrorClass.AUTH:
        return _openai_error(
            str(adapter_error), status=401, type_="invalid_request_error", code="invalid_api_key"
        )
    if cls == ErrorClass.RATE_LIMIT:
        return _openai_error(
            str(adapter_error),
            status=429,
            type_="rate_limit_error",
            code="rate_limit_exceeded",
        )
    if cls == ErrorClass.CONTEXT_OVERFLOW:
        return _openai_error(
            str(adapter_error),
            status=400,
            type_="invalid_request_error",
            code="context_length_exceeded",
        )
    if cls == ErrorClass.INVALID_REQUEST:
        return _openai_error(str(adapter_error), status=400, type_="invalid_request_error")
    if cls == ErrorClass.NETWORK:
        return _openai_error(str(adapter_error), status=502, type_="api_error")
    if cls == ErrorClass.SERVER_ERROR:
        return _openai_error(str(adapter_error), status=503, type_="api_error")
    return _openai_error(str(adapter_error), status=500, type_="api_error")


def _anthropic_error(
    message: str,
    *,
    status: int,
    type_: str,
) -> Response:
    return _json(
        anthropic_error_envelope(message=message, error_type=type_),
        status=status,
    )


def _anthropic_error_from_adapter(exc: UpstreamProviderError) -> Response:
    """Map a canonical adapter error class onto an Anthropic-shape body."""
    from metis_core.adapters.errors import ErrorClass

    adapter_error = exc.adapter_error
    cls = adapter_error.error_class
    if cls == ErrorClass.AUTH:
        return _anthropic_error(str(adapter_error), status=401, type_="authentication_error")
    if cls == ErrorClass.RATE_LIMIT:
        return _anthropic_error(str(adapter_error), status=429, type_="rate_limit_error")
    if cls == ErrorClass.CONTEXT_OVERFLOW:
        return _anthropic_error(str(adapter_error), status=400, type_="invalid_request_error")
    if cls == ErrorClass.INVALID_REQUEST:
        return _anthropic_error(str(adapter_error), status=400, type_="invalid_request_error")
    if cls == ErrorClass.NETWORK:
        return _anthropic_error(str(adapter_error), status=502, type_="api_error")
    if cls == ErrorClass.SERVER_ERROR:
        return _anthropic_error(str(adapter_error), status=503, type_="overloaded_error")
    return _anthropic_error(str(adapter_error), status=500, type_="api_error")


# ---------------------------------------------------------------------------
# Quota helpers
# ---------------------------------------------------------------------------


def _build_quota_cache(runtime: GatewayRuntime) -> RequestQuotaCache | None:
    """Per-request quota cache; `None` when the runtime has no tracker.

    The agent-loop runtime (`metis serve`) doesn't ship a tracker
    today; this guard lets the gateway HTTP path stay agnostic of
    whether quotas are wired.
    """
    if runtime.quota_tracker is None:
        return None
    return RequestQuotaCache(runtime.quota_tracker)


def _resolve_tier_caps(state: _AppState, key) -> TierCaps | None:
    """Wave 15 — resolve the billing tier cap that composes with key caps.

    Returns `None` when:
      - billing isn't enabled on this deployment;
      - signup isn't enabled (no account model to attach a tier to);
      - the gateway key has no signup account (CLI-issued key);
      - the account is on a tier without a configured cap (pro / enterprise
        default unlimited at the tier layer).

    On the Free tier, returns the configured `free_daily_cap_usd` /
    `free_monthly_cap_usd` aggregated across every key under the account
    so a buyer can't slide past the cap by issuing more keys.
    """
    if state.billing is None or state.signup is None:
        return None
    account = state.signup.store.account_for_key(key.key_id)
    if account is None:
        return None
    state.billing.service.enforce_failed_payment_state(account_id=account.account_id)
    customer = state.billing.store.get_customer(account.account_id)
    tier = customer.tier if customer is not None else "free"
    if tier != "free":
        return None
    cfg = state.billing.config
    daily_cap = cfg.free_daily_cap_usd
    monthly_cap = cfg.free_monthly_cap_usd
    if daily_cap is None and monthly_cap is None:
        return None
    return TierCaps(
        account_id=account.account_id,
        key_ids=tuple(account.key_ids),
        daily_cap_usd=daily_cap,
        monthly_cap_usd=monthly_cap,
    )


def _team_budget_remaining(quota_cache: RequestQuotaCache | None, key) -> Decimal | None:
    """The team's monthly headroom (cap minus team's month spend), or None.

    Drives the `team_budget_remaining_lt` routing predicate. v1 uses the
    key's `monthly_cap_usd` as the team budget proxy because
    `teams.json` (multi-user.md §4.2) is not built yet — every key in
    a team typically shares the same cap. Returns `None` (which
    evaluates to "no constraint") when the key has no team binding or
    no monthly cap.
    """
    if quota_cache is None or key.team_id is None or key.monthly_cap_usd is None:
        return None
    status = quota_cache.status(
        identity_kind="team",
        identity_value=key.team_id,
        window="monthly",
        cap_usd=key.monthly_cap_usd,
    )
    return status.remaining_usd()


def _key_revoked_response(
    *,
    key_id: str,
    revoked_at: datetime | None,
    shape: Literal["openai", "anthropic"],
) -> Response:
    """Render the documented 401 body for a revoked-or-grace-expired key.

    Body shape (gateway.md §11):

        {"error": {"code": "key_revoked", "key_id": "...", "revoked_at": "..."}}

    The same shape is returned for both inbound translators (OpenAI +
    Anthropic clients see identical bodies) so the buyer's runbook is one
    diagnostic instead of two. The shape-specific `type` discriminator is
    set to keep the envelope parseable by each SDK's error parser.
    """
    body: dict = {
        "error": {
            "code": "key_revoked",
            "key_id": key_id,
            "revoked_at": revoked_at.astimezone(UTC).isoformat() if revoked_at else None,
            "message": f"gateway key {key_id} has been revoked",
        }
    }
    if shape == "openai":
        body["error"]["type"] = "invalid_request_error"
    else:
        body["error"]["type"] = "authentication_error"
    return _json(body, status=401)


def _quota_exceeded_response(verdict: QuotaExceeded, *, shape: str) -> Response:
    """Render a `QuotaExceeded` as the documented 429 body.

    multi-user.md §5 / gateway.md §6.4:

        {"error": {"code": "quota_exceeded", "identity": "user|team|key",
                   "limit_usd": ..., "current_usd": ...}}

    Shape-specific framing matches the existing error envelopes — OpenAI
    clients see the canonical `error.code` shape; Anthropic clients see
    the same shape (the body is shared between both inbound shapes per
    gateway.md §6.4).
    """
    body = {
        "error": {
            "code": "quota_exceeded",
            "identity": verdict.identity_kind,
            "scope": verdict.scope,
            "limit_usd": format(verdict.limit_usd, "f"),
            "current_usd": format(verdict.current_usd, "f"),
            "message": (
                f"{verdict.scope} cap of ${verdict.limit_usd} hit (${verdict.current_usd} spent)"
            ),
        }
    }
    if shape == "anthropic":
        # Anthropic clients also expect a `type` discriminator on the error
        # envelope (gateway.md §8). 429 maps to `rate_limit_error`.
        body["error"]["type"] = "rate_limit_error"
    else:
        body["error"]["type"] = "rate_limit_error"
    return _json(body, status=429)


__all__ = [
    "DEFAULT_BACKLOG",
    "DEFAULT_HOST",
    "DEFAULT_MAX_CONCURRENT_CONNECTIONS",
    "DEFAULT_PORT",
    "GatewayConfig",
    "GatewayConfigError",
    "build_app",
    "chat_completions",
    "messages",
    "run_gateway",
]
