"""Time window resolution.

The API speaks UTC end-to-end (analytics-api.md §3.1). The SPA computes UTC
bounds from local-TZ buttons before calling. This module parses ISO 8601
strings and applies the "last 7 days" default.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from metis_core.analytics.errors import InvalidTimeWindowError


@dataclass(frozen=True)
class TimeWindow:
    """Inclusive-start, exclusive-end UTC bounds.

    `start_us` and `end_us` are microseconds since the Unix epoch (matches the
    `events.timestamp_us` column type for direct SQL parameter binding).
    """

    start: datetime
    end: datetime

    @property
    def start_us(self) -> int:
        return _to_micros(self.start)

    @property
    def end_us(self) -> int:
        return _to_micros(self.end)

    def to_envelope(self) -> dict:
        return {"start": self.start.isoformat(), "end": self.end.isoformat()}


def resolve_window(
    from_str: str | None,
    to_str: str | None,
    *,
    now: datetime | None = None,
    default_lookback: timedelta = timedelta(days=7),
) -> TimeWindow:
    """Parse `from`/`to` strings into a UTC TimeWindow.

    - Both omitted: last `default_lookback` (default 7 days).
    - Only `to` omitted: `from..now`.
    - Only `from` omitted: `now - default_lookback..to`.
    - Both present: `from..to`.

    Raises `InvalidTimeWindowError` if either string is malformed or `from >= to`.
    """
    current = now or datetime.now(UTC)
    if from_str is None and to_str is None:
        return TimeWindow(start=current - default_lookback, end=current)
    start = _parse_iso(from_str) if from_str is not None else current - default_lookback
    end = _parse_iso(to_str) if to_str is not None else current
    if start >= end:
        raise InvalidTimeWindowError(
            f"from ({start.isoformat()}) must be strictly less than to ({end.isoformat()})"
        )
    return TimeWindow(start=start, end=end)


def _parse_iso(s: str) -> datetime:
    """Parse an ISO 8601 string; require timezone-aware (UTC), normalize to UTC."""
    try:
        # `datetime.fromisoformat` in 3.11+ handles 'Z' as UTC.
        dt = datetime.fromisoformat(s)
    except ValueError as exc:
        raise InvalidTimeWindowError(f"could not parse {s!r} as ISO 8601") from exc
    if dt.tzinfo is None:
        raise InvalidTimeWindowError(f"timestamp {s!r} is timezone-naive; expected UTC offset")
    return dt.astimezone(UTC)


def _to_micros(dt: datetime) -> int:
    epoch = datetime(1970, 1, 1, tzinfo=dt.tzinfo)
    delta = dt - epoch
    return delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds
