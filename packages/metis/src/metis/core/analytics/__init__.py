"""Analytics: read-only projections over the trace store and session store.

Backs the dashboard SPA via the HTTP namespace defined in
`docs/specs/analytics-api.md`. No bus emission, no persistent state, no write
paths — every query is a SQL projection over the existing tables.
"""

from __future__ import annotations

from metis.core.analytics.errors import (
    InvalidGroupByError,
    InvalidOrderError,
    InvalidTimeWindowError,
    TurnNotFoundError,
    UnknownBaselineModelError,
)
from metis.core.analytics.store import AnalyticsStore
from metis.core.analytics.windows import TimeWindow, resolve_window

__all__ = [
    "AnalyticsStore",
    "InvalidGroupByError",
    "InvalidOrderError",
    "InvalidTimeWindowError",
    "TimeWindow",
    "TurnNotFoundError",
    "UnknownBaselineModelError",
    "resolve_window",
]
