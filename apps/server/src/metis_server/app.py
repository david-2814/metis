"""Starlette ASGI app — HTTP REST + WebSocket streaming.

Wires runtime components into Starlette routes. Endpoints follow
server-api.md §4 (subset for v1):

  POST   /sessions
  GET    /sessions
  GET    /sessions/{session_id}                 (issues attach_token)
  PATCH  /sessions/{session_id}
  DELETE /sessions/{session_id}
  POST   /sessions/{session_id}/turns
  POST   /sessions/{session_id}/turns/{turn_id}/cancel
  GET    /sessions/{session_id}/messages
  GET    /models
  GET    /health
  GET    /server/version
  WS     /sessions/{session_id}/stream

The TUI / external clients use these endpoints; the in-process REPL still
talks to SessionManager directly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import msgspec
from metis_cli.models_display import model_dict, resolve_models
from metis_cli.runtime import ChatRuntime
from metis_core.analytics import AnalyticsStore
from metis_core.canonical.ids import new_message_id
from metis_core.canonical.messages import Message
from metis_core.events.envelope import Actor
from metis_core.events.payloads import SessionEnded, make_event
from metis_core.sessions.manager import UnknownAliasError
from metis_core.tools.confirmation import ConfirmationDecision
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket

from metis_server import analytics as analytics_handlers
from metis_server.confirmations import RemoteConfirmationHandler
from metis_server.errors import (
    APIError,
    confirmation_already_resolved,
    confirmation_not_found,
    error_response,
    invalid_content,
    model_not_configured,
    session_not_found,
    turn_in_flight,
    turn_not_found,
    workspace_not_found,
)
from metis_server.hub import StreamingHub
from metis_server.streaming import StreamingConnection
from metis_server.tokens import AttachTokenRegistry
from metis_server.turns import TurnExecutor

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8421


@dataclass
class ServerConfig:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT


@dataclass
class _AppState:
    runtime: ChatRuntime
    tokens: AttachTokenRegistry
    hub: StreamingHub
    executor: TurnExecutor
    confirmation_handler: RemoteConfirmationHandler
    analytics: AnalyticsStore
    started_at: datetime


def build_app(runtime: ChatRuntime) -> Starlette:
    """Build a Starlette app bound to a fully-wired runtime.

    The dispatcher's confirmation handler is swapped to a
    `RemoteConfirmationHandler` so tools that require confirmation block on
    a REST response (per server-api.md §4.2). CLI / TUI runtimes still use
    the original `AutoAllowHandler`.
    """
    hub = StreamingHub()
    confirmation = RemoteConfirmationHandler()
    runtime.dispatcher.set_confirmation_handler(confirmation)
    state = _AppState(
        runtime=runtime,
        tokens=AttachTokenRegistry(),
        hub=hub,
        executor=TurnExecutor(runtime.manager, hub=hub),
        confirmation_handler=confirmation,
        analytics=AnalyticsStore(runtime.db_file),
        started_at=datetime.now(UTC),
    )

    async def _err_handler(_request: Request, exc: Exception) -> Response:
        if isinstance(exc, APIError):
            return exc.to_response()
        if isinstance(exc, HTTPException):
            return error_response(
                "validation_error",
                exc.status_code,
                exc.detail or "request rejected",
            )
        logger.exception("unhandled error in server endpoint")
        return error_response("internal_error", 500, "internal server error")

    routes = [
        Route("/health", _health, methods=["GET"]),
        Route("/server/version", _server_version, methods=["GET"]),
        Route("/sessions", _post_session, methods=["POST"]),
        Route("/sessions", _list_sessions, methods=["GET"]),
        Route("/sessions/{session_id}", _get_session, methods=["GET"]),
        Route("/sessions/{session_id}", _patch_session, methods=["PATCH"]),
        Route("/sessions/{session_id}", _delete_session, methods=["DELETE"]),
        Route("/sessions/{session_id}/turns", _post_turn, methods=["POST"]),
        Route(
            "/sessions/{session_id}/turns/{turn_id}/cancel",
            _cancel_turn,
            methods=["POST"],
        ),
        Route(
            "/sessions/{session_id}/turns/{turn_id}/confirmations/{request_id}",
            _resolve_confirmation,
            methods=["POST"],
        ),
        Route(
            "/sessions/{session_id}/messages",
            _list_messages,
            methods=["GET"],
        ),
        Route("/models", _list_models, methods=["GET"]),
        # /analytics/* — read-only dashboard surface (analytics-api.md)
        Route("/analytics/cost", analytics_handlers.cost, methods=["GET"]),
        Route(
            "/analytics/cache_effectiveness",
            analytics_handlers.cache_effectiveness,
            methods=["GET"],
        ),
        Route("/analytics/routing", analytics_handlers.routing, methods=["GET"]),
        Route("/analytics/reliability", analytics_handlers.reliability, methods=["GET"]),
        Route("/analytics/sessions", analytics_handlers.sessions, methods=["GET"]),
        Route("/analytics/turns/{turn_id}", analytics_handlers.turn, methods=["GET"]),
        Route("/analytics/savings", analytics_handlers.savings, methods=["GET"]),
        WebSocketRoute("/sessions/{session_id}/stream", _stream),
        # Dashboard SPA — vanilla HTML + JS, served as static files from
        # `metis_server/static/`. Mounted last so API/WS routes take priority.
        Route("/", _redirect_to_dashboard, methods=["GET"]),
        Mount("/dashboard", _static_files_app(), name="dashboard"),
    ]

    app = Starlette(
        routes=routes,
        exception_handlers={Exception: _err_handler, APIError: _err_handler},
    )
    app.state.app_state = state
    return app


def _state(request: Request | WebSocket) -> _AppState:
    return request.app.state.app_state


# ---------------------------------------------------------------------------
# HTTP handlers
# ---------------------------------------------------------------------------


async def _health(request: Request) -> Response:
    st = _state(request)
    uptime = (datetime.now(UTC) - st.started_at).total_seconds()
    return _json(
        {
            "status": "ok",
            "started_at": st.started_at.isoformat(),
            "uptime_seconds": round(uptime, 3),
            "active_sessions": len(st.runtime.session_store.list_sessions()),
            "active_turns": sum(
                1
                for sid in (s.id for s in st.runtime.session_store.list_sessions())
                if st.executor.has_in_flight(sid)
            ),
        }
    )


async def _server_version(_request: Request) -> Response:
    return _json(
        {
            "version": "0.1.0",
            "schema_versions": {
                "canonical_message": 1,
                "events": 1,
                "routing_policy": 1,
            },
        }
    )


async def _post_session(request: Request) -> Response:
    st = _state(request)
    body = await _read_json(request)
    workspace_path = body.get("workspace_path")
    if not isinstance(workspace_path, str) or not workspace_path:
        raise invalid_content("workspace_path is required")
    if not Path(workspace_path).expanduser().is_dir():
        raise workspace_not_found(workspace_path)
    initial_model = body.get("initial_active_model")
    try:
        session = st.runtime.manager.create_session(
            workspace_path=workspace_path,
            active_model=initial_model,
        )
    except UnknownAliasError as exc:
        raise model_not_configured(exc.alias) from exc
    return _json(
        {
            "id": session.id,
            "workspace_path": session.workspace_path,
            "active_model": session.active_model,
            "created_at": session.created_at.isoformat(),
            "routing_policy_version": None,
        },
        status=201,
    )


async def _list_sessions(request: Request) -> Response:
    st = _state(request)
    sessions = st.runtime.session_store.list_sessions()
    workspace_filter = request.query_params.get("workspace_path")
    if workspace_filter:
        sessions = [s for s in sessions if s.workspace_path == workspace_filter]
    return _json(
        {
            "sessions": [
                {
                    "id": s.id,
                    "workspace_path": s.workspace_path,
                    "active_model": s.active_model,
                    "created_at": s.created_at.isoformat(),
                    "turn_count": s.turn_count,
                    "cost_so_far_usd": s.cost_so_far_usd,
                }
                for s in sessions
            ],
            "next_cursor": None,
        }
    )


async def _get_session(request: Request) -> Response:
    st = _state(request)
    sid = request.path_params["session_id"]
    try:
        session = st.runtime.session_store.get_session(sid)
    except KeyError:
        raise session_not_found(sid) from None
    token, _expires = st.tokens.mint(sid)
    base = (
        str(request.base_url).rstrip("/").replace("http://", "ws://").replace("https://", "wss://")
    )
    return _json(
        {
            "id": session.id,
            "workspace_path": session.workspace_path,
            "active_model": session.active_model,
            "routing_policy_version": None,
            "cost_so_far_usd": session.cost_so_far_usd,
            "turn_count": session.turn_count,
            "current_turn_id": None,
            "current_turn_status": "in_flight" if st.executor.has_in_flight(sid) else None,
            "attach_token": token,
            "ws_url": f"{base}/sessions/{sid}/stream?attach={token}",
        }
    )


async def _patch_session(request: Request) -> Response:
    st = _state(request)
    sid = request.path_params["session_id"]
    body = await _read_json(request)
    if "active_model" not in body:
        raise invalid_content("body must include active_model")
    new_model = body["active_model"]
    try:
        st.runtime.session_store.get_session(sid)
    except KeyError:
        raise session_not_found(sid) from None
    try:
        st.runtime.manager.set_active_model(sid, new_model)
    except UnknownAliasError as exc:
        raise model_not_configured(exc.alias) from exc
    session = st.runtime.session_store.get_session(sid)
    return _json(
        {
            "id": session.id,
            "active_model": session.active_model,
            "swap_queued": False,
            "swap_queued_until_turn": None,
        }
    )


async def _delete_session(request: Request) -> Response:
    st = _state(request)
    sid = request.path_params["session_id"]
    try:
        session = st.runtime.session_store.get_session(sid)
    except KeyError:
        raise session_not_found(sid) from None
    # Emit session.ended so clients receive the canonical signal.
    st.runtime.bus.emit(
        make_event(
            type="session.ended",
            session_id=sid,
            actor=Actor.USER,
            payload=SessionEnded(
                disposition="completed",
                turn_count=session.turn_count,
                total_cost_usd=session.cost_so_far_usd,
                duration_seconds=(datetime.now(UTC) - session.created_at).total_seconds(),
            ),
            timestamp=datetime.now(UTC),
        )
    )
    # Cancel any in-flight turn for this session.
    st.executor._in_flight.pop(sid, None)
    return _json({"id": sid, "ended_at": datetime.now(UTC).isoformat()})


async def _post_turn(request: Request) -> Response:
    st = _state(request)
    sid = request.path_params["session_id"]
    try:
        st.runtime.session_store.get_session(sid)
    except KeyError:
        raise session_not_found(sid) from None
    if st.executor.has_in_flight(sid):
        raise turn_in_flight(sid)
    body = await _read_json(request)
    content = body.get("content")
    if not isinstance(content, list) or not content:
        raise invalid_content("content must be a non-empty list of canonical blocks")
    user_text = _extract_text(content)
    override = body.get("per_message_override")
    if override:
        user_text = f"@{override} {user_text}".strip()
    user_msg_id = new_message_id()
    turn_id = st.executor.submit(sid, user_text)
    return _json(
        {
            "turn_id": turn_id,
            "session_id": sid,
            "submitted_at": datetime.now(UTC).isoformat(),
            "user_message_id": user_msg_id,
        },
        status=202,
    )


async def _cancel_turn(request: Request) -> Response:
    st = _state(request)
    sid = request.path_params["session_id"]
    turn_id = request.path_params["turn_id"]
    try:
        st.runtime.session_store.get_session(sid)
    except KeyError:
        raise session_not_found(sid) from None
    cancelled = st.executor.cancel(sid, turn_id)
    if not cancelled:
        raise turn_not_found(turn_id)
    return _json(
        {"turn_id": turn_id, "cancellation_initiated": True},
        status=202,
    )


async def _resolve_confirmation(request: Request) -> Response:
    """Server-api.md §4.2 `POST .../confirmations/{request_id}`.

    Body: `{"decision": "allow" | "deny", "scope": "once" | "session"}`.
    First-write-wins; subsequent attempts get 409 confirmation_already_resolved.
    """
    st = _state(request)
    sid = request.path_params["session_id"]
    rid = request.path_params["request_id"]
    try:
        st.runtime.session_store.get_session(sid)
    except KeyError:
        raise session_not_found(sid) from None

    body = await _read_json(request)
    decision_raw = body.get("decision")
    if decision_raw not in ("allow", "deny"):
        raise invalid_content("decision must be 'allow' or 'deny'")
    scope = body.get("scope", "once")
    if scope not in ("once", "session"):
        raise invalid_content("scope must be 'once' or 'session'")

    decision = ConfirmationDecision.ALLOW if decision_raw == "allow" else ConfirmationDecision.DENY
    if not st.confirmation_handler.is_pending(rid):
        # Either the id is unknown or it was already resolved. Distinguish
        # by checking whether it's a known-but-resolved one; in v1 we drop
        # resolved entries, so "unknown" is the only case we can detect.
        # We treat both as not_found unless we add a resolved-history cache.
        raise confirmation_not_found(rid)
    applied = st.confirmation_handler.resolve(rid, decision=decision, scope=scope)
    if not applied:
        raise confirmation_already_resolved(rid)
    return _json(
        {
            "request_id": rid,
            "decision": decision.value,
            "applied": True,
        }
    )


async def _list_messages(request: Request) -> Response:
    st = _state(request)
    sid = request.path_params["session_id"]
    try:
        messages = st.runtime.session_store.get_messages(sid)
    except KeyError:
        raise session_not_found(sid) from None
    before = request.query_params.get("before")
    after = request.query_params.get("after")
    limit = min(int(request.query_params.get("limit", 50)), 200)

    if before is not None:
        messages = [m for m in messages if m.id < before]
    if after is not None:
        messages = [m for m in messages if m.id > after]
    has_more_before = False
    has_more_after = False
    if len(messages) > limit:
        # Default behavior: return most-recent `limit`.
        has_more_before = True
        messages = messages[-limit:]

    return _json(
        {
            "messages": [_message_to_dict(m) for m in messages],
            "has_more_before": has_more_before,
            "has_more_after": has_more_after,
        }
    )


async def _list_models(request: Request) -> Response:
    """GET /models — list registered models with capabilities + pricing.

    Query params:
      - ``primary_only=true``: collapse OpenRouter version siblings to one
        latest entry per family (matches the TUI/CLI ``/models`` default).
        Native ids are always kept. Default is the full list.
      - ``pattern=<substring>``: case-insensitive substring filter applied
        after the primary collapse (if both are set).
    """
    st = _state(request)
    registry = st.runtime.registry
    pricing = st.runtime.pricing

    primary_only = request.query_params.get("primary_only", "").lower() in ("1", "true", "yes")
    pattern = request.query_params.get("pattern") or None

    if primary_only and pattern:
        # Apply primary collapse first, then pattern filter on the result.
        ids, _total = resolve_models(registry=registry, mode="primary")
        ids, _ = resolve_models(
            registry=_StaticRegistryView(registry, ids),
            mode="pattern",
            pattern=pattern,
        )
    elif primary_only:
        ids, _total = resolve_models(registry=registry, mode="primary")
    elif pattern:
        ids, _total = resolve_models(registry=registry, mode="pattern", pattern=pattern)
    else:
        ids, _total = resolve_models(registry=registry, mode="all")

    models = [model_dict(mid, registry=registry, pricing=pricing) for mid in ids]
    return _json({"models": models})


class _StaticRegistryView:
    """Adapter that lets ``resolve_models`` re-filter a pre-computed id set.

    Used when the caller wants to compose `primary_only` + `pattern` — first
    we collapse to primary, then we re-run `resolve_models(mode="pattern")`
    against just that subset. The adapter satisfies the small `list_models`
    interface `resolve_models` uses.
    """

    def __init__(self, base_registry, ids: list[str]) -> None:
        self._base = base_registry
        self._ids = list(ids)

    def list_models(self) -> list[str]:
        return list(self._ids)

    def get(self, model_id: str):  # pragma: no cover — pass-through
        return self._base.get(model_id)

    def provider_of(self, model_id: str) -> str:  # pragma: no cover — pass-through
        return self._base.provider_of(model_id)


async def _stream(websocket: WebSocket) -> None:
    st = _state(websocket)
    sid = websocket.path_params["session_id"]
    token = websocket.query_params.get("attach")
    if not token or not st.tokens.consume(token, session_id=sid):
        await websocket.close(code=1008)
        return
    conn = StreamingConnection(
        websocket,
        session_id=sid,
        bus=st.runtime.bus,
        session_store=st.runtime.session_store,
        hub=st.hub,
    )
    conn.on_cancel(lambda s, t, r: st.executor.cancel(s, t))
    await conn.run()


# ---------------------------------------------------------------------------
# Lifecycle: uvicorn entry point
# ---------------------------------------------------------------------------


async def run_server(runtime: ChatRuntime, config: ServerConfig | None = None) -> None:
    """Run the HTTP server until shutdown. Wires runtime → app → uvicorn.

    Caller is responsible for `shutdown_runtime(runtime)` after exit.
    """
    import uvicorn

    cfg = config or ServerConfig()
    if cfg.host not in ("127.0.0.1", "localhost", "::1"):
        # server-api.md §3.1: refuse non-loopback in v1.
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
# Encoders
# ---------------------------------------------------------------------------


def _static_dir() -> Path:
    """Filesystem path to the dashboard SPA assets shipped with this package."""
    return Path(__file__).parent / "static"


def _static_files_app() -> StaticFiles:
    """ASGI app serving the SPA (index.html, app.js, style.css).

    `html=True` lets bare `/dashboard/` serve `index.html` automatically.
    """
    return StaticFiles(directory=_static_dir(), html=True)


async def _redirect_to_dashboard(_request: Request) -> Response:
    """Redirect bare `/` to `/dashboard/` so loopback users land on the SPA."""
    return RedirectResponse(url="/dashboard/")


def _json(body: dict, *, status: int = 200) -> Response:
    return Response(
        content=msgspec.json.encode(body),
        media_type="application/json",
        status_code=status,
    )


async def _read_json(request: Request) -> dict:
    try:
        raw = await request.body()
    except Exception as exc:
        raise invalid_content(f"could not read request body: {exc}") from exc
    if not raw:
        return {}
    try:
        return msgspec.json.decode(raw)
    except Exception as exc:
        raise invalid_content(f"invalid JSON body: {exc}") from exc


def _message_to_dict(message: Message) -> dict:
    return msgspec.to_builtins(message)


def _extract_text(content: list) -> str:
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            raise invalid_content("each content block must be an object")
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text", "")
            if not isinstance(text, str):
                raise invalid_content("text block `text` must be a string")
            parts.append(text)
        elif block_type == "image":
            # Image blocks are accepted but not turned into text for the
            # `submit_turn` API which still takes a string. Phase 2: thread
            # full ContentBlocks through SessionManager.
            continue
        else:
            raise invalid_content(f"unsupported content block type: {block_type!r}")
    return "\n".join(parts)


# Re-exports for tests
__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "ServerConfig",
    "build_app",
    "run_server",
]
