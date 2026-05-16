"""HTTP handlers for the /analytics/* namespace.

Implements the endpoints defined in `docs/specs/analytics-api.md`. All
handlers are read-only `GET`s; every response carries the standard envelope
`{window, current_pricing_version, data}` so the SPA can label and compose
without a second round-trip.

The AnalyticsStore lives on app state — one read connection per process,
shared across requests (sqlite3 connections are not thread-safe by default,
but the SPA's expected concurrency is well within the loopback v1 envelope).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import msgspec
from metis_core.analytics import (
    AnalyticsStore,
    InvalidGroupByError,
    InvalidOrderError,
    InvalidTimeWindowError,
    TimeWindow,
    TurnNotFoundError,
    UnknownBaselineModelError,
    resolve_window,
)
from metis_core.events.envelope import Actor
from metis_core.events.payloads import (
    AnalyticsUserExported,
    AnalyticsUserForgotten,
    make_event,
)
from metis_core.pricing import PriceTable
from metis_core.redaction import PseudonymizingRedactor, Redactor, pseudonym_for
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

from metis_server.errors import (
    invalid_gateway_key,
    invalid_group_by,
    invalid_limit,
    invalid_order,
    invalid_team,
    invalid_time_window,
    invalid_user,
    invalid_user_id_path,
    turn_not_found,
    unknown_baseline_model,
)

# Gateway key ids are `gk_<ULID>` (issue_key.py); Crockford-base32 ULIDs are
# 26 chars of `[0-9A-HJKMNP-TV-Z]`. We accept a slightly looser charset so
# tests and ad-hoc tooling can use synthetic ids without lying about the
# format. Anything outside this character set or longer than the cap is
# rejected at the HTTP boundary — even though the SQL is parameterized,
# pre-validating keeps the surface defensive in depth.
_GATEWAY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,200}$")
# Same defense-in-depth shape guard for `user` / `team` filters. The
# canonical id forms are `usr_<ulid>` / `team_<ulid>` (multi-user.md §3.1)
# but the spec §5.3 also accepts a human alias, so the pattern matches
# both. Whitespace, semicolons, quotes, etc. are rejected.
_PRINCIPAL_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,200}$")


def _store(request: Request) -> AnalyticsStore:
    return request.app.state.app_state.analytics


def _pricing(request: Request) -> PriceTable:
    return request.app.state.app_state.runtime.pricing


def _resolve_window_from_query(request: Request) -> TimeWindow:
    try:
        return resolve_window(
            request.query_params.get("from"),
            request.query_params.get("to"),
        )
    except InvalidTimeWindowError as exc:
        raise invalid_time_window(exc.message) from exc


def _envelope(window: TimeWindow | None, pricing_version: str, data: Any) -> dict:
    return {
        "window": window.to_envelope() if window is not None else {"start": None, "end": None},
        "current_pricing_version": pricing_version,
        "data": data,
    }


def _json(body: dict, *, status: int = 200) -> Response:
    return Response(
        content=msgspec.json.encode(body),
        media_type="application/json",
        status_code=status,
    )


async def cost(request: Request) -> Response:
    window = _resolve_window_from_query(request)
    group_by = request.query_params.get("group_by", "model")
    gateway_key = request.query_params.get("gateway_key")
    user = request.query_params.get("user")
    team = request.query_params.get("team")
    include_workers_raw = request.query_params.get("include_workers", "true")
    include_workers = include_workers_raw.lower() not in ("false", "0", "no")
    if gateway_key is not None and not _GATEWAY_KEY_PATTERN.match(gateway_key):
        raise invalid_gateway_key(f"gateway_key={gateway_key!r} does not look like a valid key id")
    if user is not None and not _PRINCIPAL_ID_PATTERN.match(user):
        raise invalid_user(f"user={user!r} does not look like a valid user id")
    if team is not None and not _PRINCIPAL_ID_PATTERN.match(team):
        raise invalid_team(f"team={team!r} does not look like a valid team id")
    try:
        data = _store(request).cost(
            window,
            group_by=group_by,
            gateway_key=gateway_key,
            user=user,
            team=team,
            include_workers=include_workers,
        )
    except InvalidGroupByError as exc:
        raise invalid_group_by(str(exc)) from exc
    return _json(_envelope(window, _pricing(request).version, data))


async def cache_effectiveness(request: Request) -> Response:
    window = _resolve_window_from_query(request)
    data = _store(request).cache_effectiveness(window)
    return _json(_envelope(window, _pricing(request).version, data))


async def routing(request: Request) -> Response:
    window = _resolve_window_from_query(request)
    data = _store(request).routing(window)
    return _json(_envelope(window, _pricing(request).version, data))


async def reliability(request: Request) -> Response:
    window = _resolve_window_from_query(request)
    data = _store(request).reliability(window)
    return _json(_envelope(window, _pricing(request).version, data))


async def sessions(request: Request) -> Response:
    limit_raw = request.query_params.get("limit", "25")
    try:
        limit_int = int(limit_raw)
    except ValueError as exc:
        raise invalid_limit(f"limit={limit_raw!r} is not an integer") from exc
    if limit_int < 1:
        raise invalid_limit(f"limit must be >= 1; got {limit_int}")
    limit = min(limit_int, 500)
    order = request.query_params.get("order", "recency")
    try:
        data = _store(request).sessions(limit=limit, order=order)
    except InvalidOrderError as exc:
        raise invalid_order(str(exc)) from exc
    return _json(_envelope(None, _pricing(request).version, data))


async def turn(request: Request) -> Response:
    turn_id = request.path_params["turn_id"]
    try:
        data = _store(request).turn(turn_id)
    except TurnNotFoundError as exc:
        raise turn_not_found(exc.turn_id) from exc
    # Turn drill-down isn't time-windowed at the API level; the SPA filters by
    # turn_id directly. Echo null/null for envelope shape consistency.
    return _json(_envelope(None, _pricing(request).version, data))


async def savings(request: Request) -> Response:
    window = _resolve_window_from_query(request)
    pricing = _pricing(request)
    baseline = request.query_params.get("baseline", "anthropic:claude-sonnet-4-6")
    try:
        data = _store(request).savings(window, baseline=baseline, price_table=pricing)
    except UnknownBaselineModelError as exc:
        raise unknown_baseline_model(exc.model_id) from exc
    return _json(_envelope(window, pricing.version, data))


async def by_key(request: Request) -> Response:
    """GET /analytics/by_key (gateway.md §6 / analytics-api.md §4.8).

    Per-(gateway_key_id) cost + tokens + call_count rollup, with an
    `by_inbound_shape` sub-array per row. Rows for in-process agent traffic
    (no `gateway_key_id` stamp) appear with `gateway_key_id: null`. Sorted
    by `cost_usd` DESC.
    """
    window = _resolve_window_from_query(request)
    gateway_key = request.query_params.get("gateway_key")
    if gateway_key is not None and not _GATEWAY_KEY_PATTERN.match(gateway_key):
        raise invalid_gateway_key(f"gateway_key={gateway_key!r} does not look like a valid key id")
    data = _store(request).by_key(window, gateway_key=gateway_key)
    return _json(_envelope(window, _pricing(request).version, data))


async def by_team(request: Request) -> Response:
    """GET /analytics/by_team (multi-user.md §5.2).

    Per-(team_id) cost + tokens + call_count rollup, with a `by_user`
    sub-array per row. Rows with `team_id: null` cover agent-loop traffic
    and pre-v1 gateway keys issued without `--team`. Sorted by
    `cost_usd` DESC; the `by_user` sub-array is also `cost_usd` DESC.
    """
    window = _resolve_window_from_query(request)
    team = request.query_params.get("team")
    if team is not None and not _PRINCIPAL_ID_PATTERN.match(team):
        raise invalid_team(f"team={team!r} does not look like a valid team id")
    data = _store(request).by_team(window, team=team)
    return _json(_envelope(window, _pricing(request).version, data))


def _resolve_user_id_path(request: Request) -> str:
    """Pull `{user_id}` off the path, apply the same shape guard as the filter param.

    The portability / forget endpoints take the subject id in the URL path
    rather than a query parameter; the shape guard keeps the surface
    defense-in-depth even though every SQL/JSON write goes through a
    placeholder.
    """
    raw = request.path_params["user_id"]
    if not isinstance(raw, str) or not _PRINCIPAL_ID_PATTERN.match(raw):
        raise invalid_user_id_path(f"user_id={raw!r} does not look like a valid user id")
    return raw


async def user_export(request: Request) -> Response:
    """GET /analytics/user/{user_id}/export (analytics-api.md §4.10.1).

    Streams every event stamped with `user_id` as JSONL (one event per
    line). Optional `from` / `to` window narrows the export. Loopback-
    only inherits from the rest of `/analytics/*` (analytics-api.md
    §2.1.4); per-user authentication is downstream of the deployment-
    shape fork in STRATEGY.md §3.

    The body is a streaming response — the full result is never
    materialized in memory, so 10k+ event exports cost O(1) RAM. PRIVATE-
    tier fields are included verbatim (this is the subject's own data).

    On successful stream completion, an `analytics.user_exported` audit
    event is emitted onto the bus with the byte count + row count so a
    later compliance review can reconcile the export artifact.
    """
    user_id = _resolve_user_id_path(request)
    # Empty / mismatched window — return 400 the same way the other
    # endpoints do.
    has_window = (
        request.query_params.get("from") is not None
        or request.query_params.get("to") is not None
    )
    window: TimeWindow | None = _resolve_window_from_query(request) if has_window else None

    store = _store(request)
    state = request.app.state.app_state
    bus = state.runtime.bus
    row_count_at_start = store.user_event_count(user_id, window=window)

    async def _stream():
        byte_count = 0
        row_count = 0
        for line in store.user_export(user_id, window=window):
            byte_count += len(line)
            row_count += 1
            yield line
        # Audit event fires after the body has been fully drained. If the
        # client disconnects mid-stream the generator stops here and the
        # event reflects what was actually delivered.
        bus.emit(
            make_event(
                type="analytics.user_exported",
                session_id="analytics",
                actor=Actor.SYSTEM,
                payload=AnalyticsUserExported(
                    subject_user_id=user_id,
                    requested_by=None,
                    row_count=row_count,
                    byte_count=byte_count,
                    window_start=window.start if window is not None else None,
                    window_end=window.end if window is not None else None,
                ),
                timestamp=datetime.now(UTC),
            )
        )

    headers = {
        # Suggest a filename for `curl -O` / browser saves. The id is
        # already shape-checked above so no header injection risk.
        "Content-Disposition": f'attachment; filename="{user_id}.jsonl"',
        "X-Metis-Row-Count": str(row_count_at_start),
    }
    return StreamingResponse(
        _stream(),
        media_type="application/jsonl",
        headers=headers,
    )


async def user_forget(request: Request) -> Response:
    """POST /analytics/user/{user_id}/forget (analytics-api.md §4.10.2).

    Triggers redaction.md's pseudonymization flow against every event
    stamped with `user_id`. Idempotent: a second call returns 0
    pseudonymized rows; the audit event fires either way. Loud audit
    event (`analytics.user_forgotten`) lands on the bus regardless of
    the row count.

    Returns: `{user_id, pseudonym, pseudonymized_rows, timestamp}`.
    """
    user_id = _resolve_user_id_path(request)
    state = request.app.state.app_state
    redactor: Redactor = getattr(state, "redactor", None) or PseudonymizingRedactor(
        state.runtime.db_file
    )
    bus = state.runtime.bus

    rows = _store(request).forget_user(user_id, redactor=redactor)
    pseudonym = pseudonym_for(user_id)
    bus.emit(
        make_event(
            type="analytics.user_forgotten",
            session_id="analytics",
            actor=Actor.SYSTEM,
            payload=AnalyticsUserForgotten(
                subject_user_id=user_id,
                pseudonym=pseudonym,
                requested_by=None,
                pseudonymized_rows=rows,
            ),
            timestamp=datetime.now(UTC),
        )
    )
    return _json(
        {
            "user_id": user_id,
            "pseudonym": pseudonym,
            "pseudonymized_rows": rows,
            "completed_at": datetime.now(UTC).isoformat(),
        }
    )


async def quality(request: Request) -> Response:
    """GET /analytics/quality (evaluator.md §9.2).

    Read-only projection over `eval.completed` events. Backs the
    dashboard's quality tile: score histograms, mean / p50 / p10,
    judge_kind breakdown, and per-model quality rollup (which model
    scored best on which subject kind).
    """
    window = _resolve_window_from_query(request)
    subject_kind = request.query_params.get("subject_kind", "turn")
    group_by = request.query_params.get("group_by", "model")
    min_confidence_raw = request.query_params.get("min_confidence", "0.0")
    try:
        min_confidence = float(min_confidence_raw)
    except ValueError as exc:
        raise invalid_group_by(f"min_confidence={min_confidence_raw!r} is not a float") from exc
    if not 0.0 <= min_confidence <= 1.0:
        raise invalid_group_by(f"min_confidence must be in [0, 1]; got {min_confidence}")
    try:
        data = _store(request).quality(
            window,
            subject_kind=subject_kind,
            group_by=group_by,
            min_confidence=min_confidence,
        )
    except InvalidGroupByError as exc:
        raise invalid_group_by(str(exc)) from exc
    return _json(_envelope(window, _pricing(request).version, data))
