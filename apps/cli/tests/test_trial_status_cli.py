"""Tests for the `metis trial-status` CLI subcommand.

Covers: parser shape, the readiness-band scoring (warm vs ready vs
not_yet vs no_signal), days-in / days-remaining math, and the
expected-shape assertion the concierge-onboarding flow depends on at
day 3 / day 7.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from metis_cli.main import build_parser, main
from metis_cli.trial_status import (
    DEFAULT_TRIAL_LENGTH_DAYS,
    HEALTHY_QUALITY_FLOOR,
    MIN_LLM_CALLS_FOR_SIGNAL,
    MIN_QUALITY_VERDICTS,
    MIN_SPEND_FOR_SIGNAL_USD,
    TrialStatus,
    compute_trial_status,
)
from metis_core.events.envelope import Actor
from metis_core.events.payloads import (
    EvalCompleted,
    LLMCallCompleted,
    RouteDecided,
    make_event,
)
from metis_core.trace.store import TraceStore


def _seed_llm_calls(
    path: Path,
    *,
    base_ts: datetime,
    count: int,
    model: str = "anthropic:claude-haiku-4-5",
    cost_each: float = 0.05,
) -> None:
    store = TraceStore(path)
    try:
        for i in range(count):
            store.write(
                make_event(
                    type="llm.call_completed",
                    session_id=f"sess_{i}",
                    actor=Actor.AGENT,
                    timestamp=base_ts + timedelta(minutes=i),
                    payload=LLMCallCompleted(
                        model=model,
                        provider=model.split(":", 1)[0],
                        input_tokens=200,
                        output_tokens=100,
                        cached_input_tokens=0,
                        cache_creation_input_tokens=0,
                        cost_usd=cost_each,
                        pricing_version="v1",
                        latency_ms=100,
                        stop_reason="end_turn",
                        produced_tool_calls=0,
                        produced_thinking_blocks=0,
                        gateway_key_id="gk_trial",
                        inbound_shape="anthropic",
                        user_id="alice",
                        team_id="eng",
                    ),
                )
            )
    finally:
        store.close()


def _seed_quality_verdicts(
    path: Path,
    *,
    base_ts: datetime,
    count: int,
    score: float = 0.85,
    model: str = "anthropic:claude-haiku-4-5",
) -> None:
    """Seed `count` eval.completed turn verdicts plus the route.decided rows
    they join against (so quality group_by=model maps cleanly)."""
    store = TraceStore(path)
    try:
        for i in range(count):
            turn_id = f"turn_{i}"
            store.write(
                make_event(
                    type="route.decided",
                    session_id=f"sess_{i}",
                    actor=Actor.SYSTEM,
                    timestamp=base_ts + timedelta(minutes=i),
                    turn_id=turn_id,
                    payload=RouteDecided(
                        chosen_model=model,
                        winner_index=0,
                        elapsed_ms=1.0,
                        chain=[],
                    ),
                )
            )
            store.write(
                make_event(
                    type="eval.completed",
                    session_id=f"sess_{i}",
                    actor=Actor.SYSTEM,
                    timestamp=base_ts + timedelta(minutes=i, seconds=30),
                    payload=EvalCompleted(
                        eval_id=f"ev_{i}",
                        subject_kind="turn",
                        subject_id=turn_id,
                        score=score,
                        confidence=0.9,
                        judge_kind="heuristic",
                        judge_cost_usd=Decimal("0"),
                        judge_latency_ms=1,
                        rubric_id="workload-heuristic",
                        rubric_version="1.2.0",
                        signals={},
                    ),
                )
            )
    finally:
        store.close()


def test_trial_status_subcommand_parses() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "trial-status",
            "/srv/customer",
            "--db-path",
            "/tmp/metis.db",
            "--since",
            "2026-05-10T00:00:00+00:00",
            "--trial-length-days",
            "14",
            "--baseline",
            "anthropic:claude-sonnet-4-6",
        ]
    )
    assert args.command == "trial-status"
    assert args.workspace == "/srv/customer"
    assert args.trial_length_days == 14
    assert args.baseline == "anthropic:claude-sonnet-4-6"


def test_trial_status_subcommand_defaults() -> None:
    parser = build_parser()
    args = parser.parse_args(["trial-status", "/srv/customer"])
    assert args.trial_length_days == 7
    assert args.since is None
    assert args.db_path is None


def test_compute_trial_status_shape(tmp_path: Path) -> None:
    """The returned dataclass has every expected field populated."""
    db = tmp_path / "metis.db"
    trial_start = datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC)
    _seed_llm_calls(db, base_ts=trial_start + timedelta(hours=1), count=25)
    _seed_quality_verdicts(db, base_ts=trial_start + timedelta(hours=1, minutes=30), count=8)

    now = trial_start + timedelta(days=3, hours=4)
    status = compute_trial_status(
        db_path=db,
        workspace_path="/srv/acme",
        trial_start=trial_start,
        trial_length_days=7,
        now=now,
    )

    assert isinstance(status, TrialStatus)
    assert status.workspace_path == "/srv/acme"
    assert status.trial_length_days == 7
    assert status.days_in == 3
    assert status.days_remaining == 4
    assert status.llm_calls == 25
    assert status.total_spend_usd > 0
    assert status.savings_pct > 0  # haiku vs sonnet
    assert status.quality_count == 8
    assert status.quality_mean is not None
    assert 0.0 <= status.quality_mean <= 1.0
    assert status.cost_per_quality_usd is not None
    # 25 calls + 8 quality verdicts at score 0.85 + day 3 of 7 → ready/warm
    assert status.readiness_band in ("ready", "warm")
    assert 0 <= status.readiness_score <= 100


def test_no_traffic_yields_no_signal(tmp_path: Path) -> None:
    db = tmp_path / "metis.db"
    store = TraceStore(db)
    store.close()
    trial_start = datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC)
    status = compute_trial_status(
        db_path=db,
        workspace_path="/srv/quiet",
        trial_start=trial_start,
        trial_length_days=7,
        now=trial_start + timedelta(days=1),
    )
    assert status.llm_calls == 0
    assert status.readiness_band == "no_signal"
    assert status.readiness_score == 0
    assert any("no usage" in r for r in status.readiness_reasons)


def test_low_usage_lowers_readiness(tmp_path: Path) -> None:
    """A trickle of traffic — below the floor — produces a partial usage
    score and surfaces the reason."""
    db = tmp_path / "metis.db"
    trial_start = datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC)
    # Only 5 calls at $0.01 each → way below 20-call / $0.50 floor
    _seed_llm_calls(
        db,
        base_ts=trial_start + timedelta(hours=1),
        count=5,
        cost_each=0.01,
    )
    status = compute_trial_status(
        db_path=db,
        workspace_path="/srv/light",
        trial_start=trial_start,
        trial_length_days=7,
        now=trial_start + timedelta(days=2),
    )
    assert status.llm_calls == 5
    assert any("low usage" in r for r in status.readiness_reasons)
    # Partial credit still lands in [0, 100]
    assert 0 < status.readiness_score < 100


def test_no_quality_signal_surfaces_in_reasons(tmp_path: Path) -> None:
    """LLM calls but no quality verdicts → readiness reasons mention it."""
    db = tmp_path / "metis.db"
    trial_start = datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC)
    _seed_llm_calls(db, base_ts=trial_start + timedelta(hours=1), count=30)
    status = compute_trial_status(
        db_path=db,
        workspace_path="/srv/no-evaluator",
        trial_start=trial_start,
        trial_length_days=7,
        now=trial_start + timedelta(days=4),
    )
    assert status.quality_count == 0
    assert any("no quality verdicts" in r for r in status.readiness_reasons)


def test_days_in_clamps_at_zero(tmp_path: Path) -> None:
    """now < trial_start (clock skew or future-dated trial) doesn't go
    negative."""
    db = tmp_path / "metis.db"
    store = TraceStore(db)
    store.close()
    trial_start = datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC)
    status = compute_trial_status(
        db_path=db,
        workspace_path="/srv/x",
        trial_start=trial_start,
        trial_length_days=7,
        now=trial_start - timedelta(hours=1),
    )
    assert status.days_in == 0
    assert status.days_remaining == 7


def test_trial_status_end_to_end_main(tmp_path: Path, capsys) -> None:
    db = tmp_path / "metis.db"
    trial_start = datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC)
    _seed_llm_calls(db, base_ts=trial_start + timedelta(hours=1), count=25)
    rc = main(
        [
            "trial-status",
            str(tmp_path),
            "--db-path",
            str(db),
            "--since",
            trial_start.isoformat(),
            "--trial-length-days",
            "7",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "=== Metis trial status ===" in out
    assert "llm calls:              25" in out
    assert "readiness:" in out
    assert "Run `metis customer-report" in out


def test_trial_status_missing_db_returns_nonzero(tmp_path: Path, capsys) -> None:
    rc = main(
        [
            "trial-status",
            str(tmp_path),
            "--db-path",
            str(tmp_path / "missing.db"),
        ]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "trace DB not found" in err


def test_trial_status_rejects_naive_since(tmp_path: Path, capsys) -> None:
    db = tmp_path / "metis.db"
    store = TraceStore(db)
    store.close()
    rc = main(
        [
            "trial-status",
            str(tmp_path),
            "--db-path",
            str(db),
            "--since",
            "2026-05-10T00:00:00",  # no timezone
        ]
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "timezone-aware" in err


def test_trial_status_rejects_zero_trial_length(tmp_path: Path, capsys) -> None:
    db = tmp_path / "metis.db"
    store = TraceStore(db)
    store.close()
    rc = main(
        [
            "trial-status",
            str(tmp_path),
            "--db-path",
            str(db),
            "--trial-length-days",
            "0",
        ]
    )
    assert rc == 2


def test_readiness_thresholds_match_concierge_doc() -> None:
    """The concierge-onboarding.md doc quotes these thresholds; if anyone
    changes them, the doc must change too."""
    assert MIN_SPEND_FOR_SIGNAL_USD == 0.50
    assert MIN_LLM_CALLS_FOR_SIGNAL == 20
    assert MIN_QUALITY_VERDICTS == 5
    assert HEALTHY_QUALITY_FLOOR == 0.70
    assert DEFAULT_TRIAL_LENGTH_DAYS == 7
