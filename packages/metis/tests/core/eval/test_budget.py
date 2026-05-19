"""Budget caps (evaluator.md §7).

Heuristic-only v1 is structurally below caps. The tests pin the
contract so the future LLM-as-judge tier can wire in without surprise.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from metis.core.eval import (
    DEFAULT_PER_DAY_MAX_USD,
    DEFAULT_PER_SESSION_MAX_USD,
    BudgetTracker,
)


def test_zero_cost_never_throttles():
    tracker = BudgetTracker()
    assert tracker.throttle_reason(session_id="s", projected_cost_usd=Decimal("0")) is None


def test_session_cap_fires_on_overspend():
    tracker = BudgetTracker(per_session_max_usd=Decimal("0.01"))
    tracker.record(session_id="s", cost_usd=Decimal("0.005"))
    assert (
        tracker.throttle_reason(session_id="s", projected_cost_usd=Decimal("0.006"))
        == "session_cap"
    )


def test_daily_cap_fires_on_overspend():
    tracker = BudgetTracker(per_session_max_usd=Decimal("100"), per_day_max_usd=Decimal("0.05"))
    tracker.record(session_id="s1", cost_usd=Decimal("0.04"))
    assert (
        tracker.throttle_reason(session_id="s2", projected_cost_usd=Decimal("0.02")) == "daily_cap"
    )


def test_session_caps_are_per_session():
    tracker = BudgetTracker(per_session_max_usd=Decimal("0.01"))
    tracker.record(session_id="a", cost_usd=Decimal("0.01"))
    # Session a is at cap but b is fresh.
    assert tracker.throttle_reason(session_id="b", projected_cost_usd=Decimal("0.005")) is None


def test_daily_cap_resets_across_dates():
    tracker = BudgetTracker(per_day_max_usd=Decimal("0.05"))
    yesterday = datetime.now(UTC) - timedelta(days=1)
    tracker.record(session_id="s", cost_usd=Decimal("0.05"), now=yesterday)
    today = datetime.now(UTC)
    # Today's bucket is empty even though yesterday was capped.
    assert (
        tracker.throttle_reason(session_id="s", projected_cost_usd=Decimal("0.01"), now=today)
        is None
    )


def test_defaults_are_reasonable_for_v1():
    """v1 caps should permit a typical session of LLM-judge evaluations.

    Pin the published defaults so the spec and the implementation can't
    silently drift apart.
    """
    assert DEFAULT_PER_SESSION_MAX_USD == Decimal("0.10")
    assert DEFAULT_PER_DAY_MAX_USD == Decimal("1.00")


def test_heuristic_judge_never_runs_into_caps():
    """Property check for v1: heuristic spend is zero → throttle never fires."""
    tracker = BudgetTracker(
        per_session_max_usd=Decimal("0"),
        per_day_max_usd=Decimal("0"),
    )
    # Zero projected cost → no throttle even when caps are zero.
    assert tracker.throttle_reason(session_id="s", projected_cost_usd=Decimal("0")) is None
