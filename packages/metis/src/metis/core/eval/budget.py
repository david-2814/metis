"""Evaluator budget caps (evaluator.md §7).

Two caps apply to LLM-as-judge spend: per-session and per-day. The v1
heuristic tier always reports zero cost, so these caps are structural —
they exist so future LLM/hybrid judges can be wired in without touching
the contract. A throttled judge downgrades the planned `judge_kind` to
`heuristic`; verdicts are never silently dropped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Literal

DEFAULT_PER_SESSION_MAX_USD = Decimal("0.10")
DEFAULT_PER_DAY_MAX_USD = Decimal("1.00")

ThrottleReason = Literal["session_cap", "daily_cap"]


@dataclass
class BudgetTracker:
    """Tracks judge spend by session and by UTC calendar day.

    The tracker is in-memory only — it persists across a single
    long-running process (subscriber's lifetime). Re-evaluation runs
    through the CLI instantiate a fresh tracker. This matches the v1
    posture: caps are runtime safety, not durable accounting (the
    durable record is the `eval.completed.judge_cost_usd` field).
    """

    per_session_max_usd: Decimal = DEFAULT_PER_SESSION_MAX_USD
    per_day_max_usd: Decimal = DEFAULT_PER_DAY_MAX_USD
    _session_spend: dict[str, Decimal] = field(default_factory=dict)
    _daily_spend: dict[date, Decimal] = field(default_factory=dict)

    def throttle_reason(
        self,
        *,
        session_id: str,
        projected_cost_usd: Decimal,
        now: datetime | None = None,
    ) -> ThrottleReason | None:
        """Return the cap that would fire, or None if allowed.

        `projected_cost_usd` is the estimated spend for the planned call.
        Heuristic judges pass `Decimal("0")` — they never throttle, since
        the cap is on inference cost, not the existence of an evaluation.
        """
        if projected_cost_usd <= 0:
            return None
        today = (now or datetime.now(UTC)).date()
        if (
            self._session_spend.get(session_id, Decimal("0")) + projected_cost_usd
            > self.per_session_max_usd
        ):
            return "session_cap"
        if self._daily_spend.get(today, Decimal("0")) + projected_cost_usd > self.per_day_max_usd:
            return "daily_cap"
        return None

    def record(
        self,
        *,
        session_id: str,
        cost_usd: Decimal,
        now: datetime | None = None,
    ) -> None:
        """Charge a successful judge call against both caps."""
        if cost_usd <= 0:
            return
        today = (now or datetime.now(UTC)).date()
        self._session_spend[session_id] = (
            self._session_spend.get(session_id, Decimal("0")) + cost_usd
        )
        self._daily_spend[today] = self._daily_spend.get(today, Decimal("0")) + cost_usd

    def session_spend(self, session_id: str) -> Decimal:
        return self._session_spend.get(session_id, Decimal("0"))

    def daily_spend(self, day: date | None = None) -> Decimal:
        day = day or datetime.now(UTC).date()
        return self._daily_spend.get(day, Decimal("0"))
