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

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

import msgspec
from metis_core.adapters.tool_id_map import ToolIdMap
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from metis_gateway.auth import extract_bearer_token, identity_from_key
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
from metis_gateway.runtime import GatewayRuntime
from metis_gateway.translators import (
    InboundTranslationError,
    parse_openai_request,
    render_openai_response,
    render_openai_sse_stream,
)

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8422


@dataclass
class GatewayConfig:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT


@dataclass
class _AppState:
    runtime: GatewayRuntime
    started_at: datetime


def build_app(runtime: GatewayRuntime) -> Starlette:
    """Build the Starlette ASGI app bound to a fully-wired GatewayRuntime."""
    state = _AppState(runtime=runtime, started_at=datetime.now(UTC))

    async def _err_handler(_request: Request, exc: Exception) -> Response:
        if isinstance(exc, HTTPException):
            return _error_response(exc.detail or "request rejected", status=exc.status_code)
        logger.exception("unhandled error in gateway endpoint")
        return _error_response("internal server error", status=500, code="internal_error")

    routes = [
        Route("/healthz", _health, methods=["GET"]),
        Route("/v1/chat/completions", chat_completions, methods=["POST"]),
        Route("/v1/messages", messages, methods=["POST"]),
    ]
    app = Starlette(
        routes=routes,
        exception_handlers={Exception: _err_handler},
    )
    app.state.app_state = state
    return app


def _state(request: Request) -> _AppState:
    return request.app.state.app_state


async def _health(request: Request) -> Response:
    st = _state(request)
    uptime = (datetime.now(UTC) - st.started_at).total_seconds()
    return _json({"status": "ok", "uptime_seconds": round(uptime, 3)})


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
        return _openai_error(
            "invalid or missing API key",
            status=401,
            type_="invalid_request_error",
            code="invalid_api_key",
        )

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

    if parsed.stream:
        return await _stream_chat_completions(
            harness=harness,
            parsed=parsed,
            key=key,
            probe=probe,
            tool_map=tool_map,
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
            identity=identity_from_key(key),
            allowed_models=key.allowed_models,
            is_disconnected=probe,
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
    probe,
    tool_map: ToolIdMap,
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
        identity=identity_from_key(key),
        allowed_models=key.allowed_models,
        is_disconnected=probe,
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
        return _anthropic_error(
            "invalid or missing API key", status=401, type_="authentication_error"
        )

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

    if parsed.stream:
        return await _stream_messages(harness=harness, parsed=parsed, key=key, probe=probe)

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
            identity=identity_from_key(key),
            allowed_models=key.allowed_models,
            is_disconnected=probe,
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
    probe,
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
        identity=identity_from_key(key),
        allowed_models=key.allowed_models,
        is_disconnected=probe,
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


async def run_gateway(runtime: GatewayRuntime, config: GatewayConfig | None = None) -> None:
    """Run the gateway HTTP server until shutdown.

    v1 binds loopback-only, matching `metis serve`'s safety posture. The
    public-facing deployment shape (TLS terminator in front of the gateway)
    is operator responsibility per gateway.md §3.2 — the production-bind
    path is gated behind future hardening (auth/rate limiting/audit) before
    v1 will accept non-loopback binds.
    """
    import uvicorn

    cfg = config or GatewayConfig()
    if cfg.host not in ("127.0.0.1", "localhost", "::1"):
        logger.warning(
            "non-loopback bind %r requested; forcing 127.0.0.1 (v1 safety guarantee)",
            cfg.host,
        )
        cfg.host = "127.0.0.1"
    app = build_app(runtime)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=cfg.host,
            port=cfg.port,
            log_level="info",
            lifespan="off",
        )
    )
    await server.serve()


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


__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "GatewayConfig",
    "build_app",
    "chat_completions",
    "messages",
    "run_gateway",
]
