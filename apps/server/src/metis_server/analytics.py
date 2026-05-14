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
from metis_core.pricing import PriceTable
from starlette.requests import Request
from starlette.responses import Response

from metis_server.errors import (
    invalid_group_by,
    invalid_limit,
    invalid_order,
    invalid_time_window,
    turn_not_found,
    unknown_baseline_model,
)


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
    try:
        data = _store(request).cost(window, group_by=group_by)
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
