"""Time-window resolution tests (analytics-api.md §3.1)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from metis_core.analytics import InvalidTimeWindowError, resolve_window


def test_both_omitted_defaults_to_last_7_days():
    now = datetime(2026, 5, 12, 12, 0, 0, tzinfo=UTC)
    w = resolve_window(None, None, now=now)
    assert w.end == now
    assert w.start == now - timedelta(days=7)


def test_only_to_omitted_uses_now():
    now = datetime(2026, 5, 12, 12, 0, 0, tzinfo=UTC)
    w = resolve_window("2026-05-10T00:00:00+00:00", None, now=now)
    assert w.end == now


def test_explicit_both():
    w = resolve_window(
        "2026-05-01T00:00:00+00:00",
        "2026-05-12T00:00:00+00:00",
    )
    assert w.start.day == 1
    assert w.end.day == 12


def test_naive_timestamp_rejected():
    with pytest.raises(InvalidTimeWindowError):
        resolve_window("2026-05-12T12:00:00", None)


def test_malformed_iso_rejected():
    with pytest.raises(InvalidTimeWindowError):
        resolve_window("not-a-date", None)


def test_from_after_to_rejected():
    with pytest.raises(InvalidTimeWindowError):
        resolve_window(
            "2026-05-12T00:00:00+00:00",
            "2026-05-01T00:00:00+00:00",
        )


def test_equal_from_to_rejected():
    with pytest.raises(InvalidTimeWindowError):
        resolve_window(
            "2026-05-12T00:00:00+00:00",
            "2026-05-12T00:00:00+00:00",
        )


def test_z_suffix_accepted():
    # 3.11+ fromisoformat handles 'Z' as UTC.
    w = resolve_window("2026-05-12T00:00:00Z", None)
    assert w.start.tzinfo is UTC or w.start.utcoffset().total_seconds() == 0


def test_envelope_serialization():
    w = resolve_window(
        "2026-05-01T00:00:00+00:00",
        "2026-05-12T00:00:00+00:00",
    )
    env = w.to_envelope()
    assert env["start"].startswith("2026-05-01")
    assert env["end"].startswith("2026-05-12")
