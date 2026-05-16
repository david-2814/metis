"""`metis trial-status` — quick conversion-readiness indicator for a trial.

Reads the workspace's trace DB and reports: how far the buyer is into a
7-day trial (or whatever window the caller picks), what they've spent,
what quality verdicts have landed, and a 0-100 conversion-readiness
score derived from a small fixed weighting of those signals.

This is the day-7 stand-up tool — not a billing surface. The score is
deliberately coarse (three weighted bands: signal-of-spend, signal-of-
quality, signal-of-trend) so the concierge-onboarding flow has
something to quote during the day-7 close conversation without
over-claiming precision.

The CLI is read-only; it does not write to the trace DB.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from metis_core.analytics import AnalyticsStore, TimeWindow
from metis_core.analytics.errors import UnknownBaselineModelError
from metis_core.pricing import DEFAULT_PRICE_TABLE

DEFAULT_TRIAL_LENGTH_DAYS = 7
DEFAULT_BASELINE_MODEL = "anthropic:claude-sonnet-4-6"

# Conversion-readiness thresholds. These are owner-tunable — the values
# below match the concierge-onboarding doc's day-7 close criteria.
MIN_SPEND_FOR_SIGNAL_USD = 0.50  # below this, "did they actually use it?"
MIN_LLM_CALLS_FOR_SIGNAL = 20  # below this, sample is too small
MIN_QUALITY_VERDICTS = 5  # below this, quality signal is noisy
HEALTHY_QUALITY_FLOOR = 0.70  # below this, the trial isn't converting


@dataclass(frozen=True)
class TrialStatus:
    """Snapshot of a single workspace's trial state.

    Every field is derived from the trace DB; no remembered state. Re-
    running against the same DB at the same moment produces identical
    output (modulo `generated_at`, which we accept as wall-clock drift).
    """

    workspace_path: str
    trial_start: datetime
    trial_end: datetime
    generated_at: datetime
    days_in: int
    days_remaining: int
    trial_length_days: int

    total_spend_usd: float
    baseline_repriced_usd: float
    savings_usd: float
    savings_pct: float
    llm_calls: int

    quality_mean: float | None
    quality_count: int
    cost_per_quality_usd: float | None

    readiness_score: int
    readiness_band: str  # "ready" | "warm" | "not_yet" | "no_signal"
    readiness_reasons: list[str]


def compute_trial_status(
    *,
    db_path: Path,
    workspace_path: str,
    trial_start: datetime,
    trial_length_days: int = DEFAULT_TRIAL_LENGTH_DAYS,
    baseline_model: str = DEFAULT_BASELINE_MODEL,
    now: datetime | None = None,
) -> TrialStatus:
    """Read the trace DB once and project the status snapshot."""
    generated_at = now or datetime.now(UTC)
    trial_end = trial_start + timedelta(days=trial_length_days)
    # Compare against now to find day-N; clamp `days_in` ≥ 0.
    delta = generated_at - trial_start
    days_in = max(int(delta.total_seconds() // 86_400), 0)
    days_remaining = max(trial_length_days - days_in, 0)

    window = TimeWindow(
        start=trial_start,
        end=min(generated_at, trial_end) + timedelta(microseconds=1),
    )

    store = AnalyticsStore(db_path)
    try:
        savings = store.savings(
            window,
            baseline=baseline_model,
            price_table=DEFAULT_PRICE_TABLE,
        )
        quality_rollup = store.quality(
            window,
            subject_kind="turn",
            group_by="none",
        )
    finally:
        store.close()

    total_spend = float(savings["actual_repriced_usd"])
    llm_calls = int(savings["rows_total"])
    quality_mean = (
        float(quality_rollup["mean_score"])
        if isinstance(quality_rollup, dict) and quality_rollup.get("mean_score") is not None
        else None
    )
    quality_count = (
        int(quality_rollup["verdict_count"])
        if isinstance(quality_rollup, dict) and quality_rollup.get("verdict_count")
        else 0
    )
    cost_per_quality = (
        total_spend / quality_mean
        if quality_mean is not None and quality_mean > 0 and total_spend > 0
        else None
    )

    score, band, reasons = _readiness_band(
        spend=total_spend,
        llm_calls=llm_calls,
        quality_mean=quality_mean,
        quality_count=quality_count,
        days_in=days_in,
        trial_length_days=trial_length_days,
    )

    return TrialStatus(
        workspace_path=workspace_path,
        trial_start=trial_start,
        trial_end=trial_end,
        generated_at=generated_at,
        days_in=days_in,
        days_remaining=days_remaining,
        trial_length_days=trial_length_days,
        total_spend_usd=total_spend,
        baseline_repriced_usd=float(savings["baseline_repriced_usd"]),
        savings_usd=float(savings["savings_usd"]),
        savings_pct=float(savings["savings_pct"]),
        llm_calls=llm_calls,
        quality_mean=quality_mean,
        quality_count=quality_count,
        cost_per_quality_usd=cost_per_quality,
        readiness_score=score,
        readiness_band=band,
        readiness_reasons=reasons,
    )


def _readiness_band(
    *,
    spend: float,
    llm_calls: int,
    quality_mean: float | None,
    quality_count: int,
    days_in: int,
    trial_length_days: int,
) -> tuple[int, str, list[str]]:
    """Coarse 3-axis score: usage signal + quality signal + trial progress.

    Each axis contributes up to 33 points; the score is clamped to [0, 100]
    so the headline fits on a status line. Returned `reasons` is the short
    list of factors driving the band — surfaced verbatim in the CLI output
    so the concierge sees *why* the score landed where it did.
    """
    reasons: list[str] = []

    usage_score = 0
    if spend >= MIN_SPEND_FOR_SIGNAL_USD and llm_calls >= MIN_LLM_CALLS_FOR_SIGNAL:
        usage_score = 33
    elif spend > 0 and llm_calls > 0:
        # Partial usage: scale linearly between zero and the floor.
        spend_ratio = min(spend / MIN_SPEND_FOR_SIGNAL_USD, 1.0)
        calls_ratio = min(llm_calls / MIN_LLM_CALLS_FOR_SIGNAL, 1.0)
        usage_score = int(33 * min(spend_ratio, calls_ratio))
        reasons.append(
            f"low usage signal: ${spend:.2f} / {llm_calls} calls "
            f"(floor ${MIN_SPEND_FOR_SIGNAL_USD:.2f} / {MIN_LLM_CALLS_FOR_SIGNAL} calls)"
        )
    else:
        reasons.append("no usage in the trial window")

    quality_score = 0
    if quality_count >= MIN_QUALITY_VERDICTS and quality_mean is not None:
        if quality_mean >= HEALTHY_QUALITY_FLOOR:
            quality_score = 34  # split the leftover point here
        else:
            quality_score = int(34 * (quality_mean / HEALTHY_QUALITY_FLOOR))
            reasons.append(f"quality below floor: {quality_mean:.2f} < {HEALTHY_QUALITY_FLOOR:.2f}")
    elif quality_count > 0 and quality_mean is not None:
        quality_score = int(34 * (quality_count / MIN_QUALITY_VERDICTS))
        reasons.append(
            f"quality sample small: {quality_count} verdicts (floor {MIN_QUALITY_VERDICTS})"
        )
    else:
        reasons.append("no quality verdicts in window — wire an evaluator")

    # Progress: more days through the trial = more confidence in the
    # numbers. Capped at full points once we've passed the midpoint.
    progress_score = 0
    if trial_length_days > 0:
        progress_ratio = min(days_in / max(trial_length_days / 2, 1), 1.0)
        progress_score = int(33 * progress_ratio)
        if days_in == 0:
            reasons.append("trial just started — give it 24h before scoring")

    # No usage = no signal, regardless of how many days the trial has run.
    # The "progress" axis is a confidence multiplier on usage, not a
    # standalone contributor.
    if spend == 0 and llm_calls == 0:
        return 0, "no_signal", reasons

    total = max(0, min(usage_score + quality_score + progress_score, 100))

    if total >= 80:
        band = "ready"
    elif total >= 50:
        band = "warm"
    else:
        band = "not_yet"

    return total, band, reasons


# ---------------------------------------------------------------------------
# CLI shim
# ---------------------------------------------------------------------------


def _default_db_path() -> Path:
    return Path.home() / ".metis" / "metis.db"


def _format_optional_float(value: float | None, fmt: str) -> str:
    if value is None:
        return "—"
    return format(value, fmt)


def _print_status(status: TrialStatus) -> None:
    print("=== Metis trial status ===")
    print(f"workspace:              {status.workspace_path}")
    print(
        f"trial window:           {status.trial_start.isoformat()} → {status.trial_end.isoformat()}"
    )
    print(
        f"days:                   day {status.days_in} of "
        f"{status.trial_length_days} ({status.days_remaining} remaining)"
    )
    print()
    print(f"spend (USD):            ${status.total_spend_usd:.4f}")
    print(f"baseline (USD):         ${status.baseline_repriced_usd:.4f}")
    print(f"savings_pct:            {status.savings_pct * 100:.1f}%")
    print(f"llm calls:              {status.llm_calls}")
    quality_line = (
        f"{_format_optional_float(status.quality_mean, '.2f')} "
        f"across {status.quality_count} verdicts"
    )
    print(f"quality:                {quality_line}")
    cpq = _format_optional_float(status.cost_per_quality_usd, ".4f")
    print(f"cost-per-quality (USD): ${cpq}" if cpq != "—" else "cost-per-quality (USD): —")
    print()
    print(f"readiness:              {status.readiness_band} ({status.readiness_score}/100)")
    for reason in status.readiness_reasons:
        print(f"  - {reason}")
    print()
    print("Run `metis customer-report --workspace <path> --since <trial-start>`")
    print("to produce the buyer-facing usage report companion to this status.")


def run_trial_status_command(
    *,
    workspace: str,
    db_path: str | None,
    since: str | None,
    trial_length_days: int,
    baseline: str,
) -> int:
    source_db = Path(db_path).expanduser() if db_path else _default_db_path()
    if not source_db.exists():
        print(f"trial-status failed: trace DB not found: {source_db}", file=sys.stderr)
        return 1

    if trial_length_days <= 0:
        print(
            f"trial-status failed: --trial-length-days must be positive, got {trial_length_days}",
            file=sys.stderr,
        )
        return 2

    if since is not None:
        try:
            trial_start = datetime.fromisoformat(since)
        except ValueError as exc:
            print(f"trial-status failed: could not parse --since {since!r}: {exc}", file=sys.stderr)
            return 2
        if trial_start.tzinfo is None:
            print(
                f"trial-status failed: --since must be timezone-aware (got {since!r})",
                file=sys.stderr,
            )
            return 2
    else:
        trial_start = datetime.now(UTC) - timedelta(days=trial_length_days)

    workspace_path = str(Path(workspace).expanduser().resolve())
    try:
        status = compute_trial_status(
            db_path=source_db,
            workspace_path=workspace_path,
            trial_start=trial_start,
            trial_length_days=trial_length_days,
            baseline_model=baseline,
        )
    except UnknownBaselineModelError as exc:
        print(f"trial-status failed: {exc}", file=sys.stderr)
        return 2

    _print_status(status)
    return 0


__all__ = [
    "DEFAULT_BASELINE_MODEL",
    "DEFAULT_TRIAL_LENGTH_DAYS",
    "HEALTHY_QUALITY_FLOOR",
    "MIN_LLM_CALLS_FOR_SIGNAL",
    "MIN_QUALITY_VERDICTS",
    "MIN_SPEND_FOR_SIGNAL_USD",
    "TrialStatus",
    "compute_trial_status",
    "run_trial_status_command",
]
