"""Tests for `AnalyticsStore.quality` (evaluator.md §9.2)."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from metis.core.analytics import AnalyticsStore, InvalidGroupByError, TimeWindow

from .conftest import DBSeeder

SESSION = "sess_quality"


def _window_around(t: datetime, hours: int = 1) -> TimeWindow:
    return TimeWindow(start=t - timedelta(hours=hours), end=t + timedelta(hours=hours))


def _seed_verdict(
    seeder: DBSeeder,
    *,
    t: datetime,
    subject_kind: str = "turn",
    subject_id: str,
    score: float,
    confidence: float,
    judge_kind: str = "heuristic",
    judge_model: str | None = None,
    judge_cost_usd: str = "0",
    rubric_id: str = "turn-heuristic-v1",
    signals: dict | None = None,
) -> None:
    payload = {
        "eval_id": f"eval_{subject_id}",
        "subject_kind": subject_kind,
        "subject_id": subject_id,
        "score": score,
        "confidence": confidence,
        "judge_kind": judge_kind,
        "judge_model": judge_model,
        "judge_cost_usd": judge_cost_usd,
        "judge_latency_ms": 5,
        "rubric_id": rubric_id,
        "rubric_version": "1.0.0",
        "signals": signals or {},
        "parent_eval_id": None,
        "judge_pricing_version": "2026-05-08" if judge_cost_usd != "0" else None,
    }
    seeder.insert_event(
        event_type="eval.completed",
        timestamp=t,
        session_id=SESSION,
        payload=payload,
    )


def test_quality_empty_window_returns_empty_list(
    seeded_db: tuple[Path, DBSeeder], now: datetime
) -> None:
    db_path, _ = seeded_db
    with AnalyticsStore(db_path) as store:
        result = store.quality(_window_around(now), group_by="model")
    assert result == []


def test_quality_group_by_model_joins_route_decided(
    seeded_db: tuple[Path, DBSeeder], now: datetime
) -> None:
    """group_by=model joins verdict.subject_id → route.decided.chosen_model.

    This is the load-bearing join in evaluator.md §9.2: the dashboard
    must show which *judged* model performed best, not which judge model
    produced the verdict.
    """
    db_path, seeder = seeded_db
    # Two turns judged on different models; each has its own route.decided.
    for i, model in enumerate(["anthropic:claude-haiku-4-5", "anthropic:claude-sonnet-4-6"]):
        turn_id = f"turn_{i}"
        seeder.insert_route_decided(
            timestamp=now,
            chosen_model=model,
            winner_index=0,
            chain=[{"policy": "workspace_default", "verdict": "selected"}],
            turn_id=turn_id,
        )
        _seed_verdict(
            seeder,
            t=now,
            subject_id=turn_id,
            score=0.9 if "haiku" in model else 0.6,
            confidence=0.8,
        )
    with AnalyticsStore(db_path) as store:
        result = store.quality(_window_around(now), group_by="model")
    assert isinstance(result, list)
    # Each model appears exactly once with a one-verdict bucket.
    by_model = {row["chosen_model"]: row for row in result}
    assert set(by_model) == {
        "anthropic:claude-haiku-4-5",
        "anthropic:claude-sonnet-4-6",
    }
    assert by_model["anthropic:claude-haiku-4-5"]["mean_score"] == 0.9
    assert by_model["anthropic:claude-sonnet-4-6"]["mean_score"] == 0.6
    assert by_model["anthropic:claude-haiku-4-5"]["verdict_count"] == 1


def test_quality_min_confidence_excludes_low_confidence_from_score_stats(
    seeded_db: tuple[Path, DBSeeder], now: datetime
) -> None:
    """min_confidence filters scores out of `mean_score` etc. but rows still
    counted in `verdict_count` and `judge_cost_usd_total`."""
    db_path, seeder = seeded_db
    seeder.insert_route_decided(
        timestamp=now,
        chosen_model="anthropic:claude-haiku-4-5",
        winner_index=0,
        chain=[{"policy": "workspace_default", "verdict": "selected"}],
        turn_id="t1",
    )
    seeder.insert_route_decided(
        timestamp=now,
        chosen_model="anthropic:claude-haiku-4-5",
        winner_index=0,
        chain=[{"policy": "workspace_default", "verdict": "selected"}],
        turn_id="t2",
    )
    _seed_verdict(seeder, t=now, subject_id="t1", score=0.2, confidence=0.3)  # filtered
    _seed_verdict(seeder, t=now, subject_id="t2", score=0.9, confidence=0.8)
    with AnalyticsStore(db_path) as store:
        result = store.quality(_window_around(now), group_by="model", min_confidence=0.5)
    assert len(result) == 1
    row = result[0]
    assert row["verdict_count"] == 2  # both counted
    # Only the high-confidence row contributed to mean_score.
    assert row["mean_score"] == 0.9


def test_quality_group_by_judge_kind(seeded_db: tuple[Path, DBSeeder], now: datetime) -> None:
    db_path, seeder = seeded_db
    _seed_verdict(seeder, t=now, subject_id="t1", score=0.9, confidence=0.8, judge_kind="heuristic")
    _seed_verdict(
        seeder,
        t=now,
        subject_id="t2",
        score=0.7,
        confidence=0.6,
        judge_kind="hybrid",
        judge_cost_usd="0.005",
    )
    _seed_verdict(
        seeder,
        t=now,
        subject_id="t3",
        score=0.5,
        confidence=0.7,
        judge_kind="llm",
        judge_cost_usd="0.003",
    )
    with AnalyticsStore(db_path) as store:
        result = store.quality(_window_around(now), group_by="judge_kind")
    assert isinstance(result, list)
    by_kind = {row["judge_kind"]: row for row in result}
    assert set(by_kind) == {"heuristic", "hybrid", "llm"}
    # Hybrid + LLM total should equal 0.008 within float epsilon.
    total = by_kind["hybrid"]["judge_cost_usd_total"] + by_kind["llm"]["judge_cost_usd_total"]
    assert total == pytest.approx(0.008)
    assert by_kind["heuristic"]["judge_cost_usd_total"] == 0.0


def test_quality_group_by_rubric_id(seeded_db: tuple[Path, DBSeeder], now: datetime) -> None:
    db_path, seeder = seeded_db
    _seed_verdict(
        seeder,
        t=now,
        subject_id="t1",
        score=0.9,
        confidence=0.8,
        rubric_id="turn-heuristic-v1",
    )
    _seed_verdict(
        seeder,
        t=now,
        subject_id="t2",
        score=0.5,
        confidence=0.6,
        rubric_id="turn-hybrid-v1",
    )
    with AnalyticsStore(db_path) as store:
        result = store.quality(_window_around(now), group_by="rubric_id")
    rubrics = {row["rubric_id"] for row in result}
    assert rubrics == {"turn-heuristic-v1", "turn-hybrid-v1"}


def test_quality_group_by_none_collapses_to_single_dict(
    seeded_db: tuple[Path, DBSeeder], now: datetime
) -> None:
    db_path, seeder = seeded_db
    _seed_verdict(seeder, t=now, subject_id="t1", score=0.9, confidence=0.8)
    _seed_verdict(seeder, t=now, subject_id="t2", score=0.5, confidence=0.6)
    with AnalyticsStore(db_path) as store:
        result = store.quality(_window_around(now), group_by="none")
    assert isinstance(result, dict)
    assert result["verdict_count"] == 2
    assert result["mean_score"] == pytest.approx(0.7)


def test_quality_subject_kind_filters_rows(seeded_db: tuple[Path, DBSeeder], now: datetime) -> None:
    """A verdict for `tool_cycle` shouldn't appear under `subject_kind=turn`."""
    db_path, seeder = seeded_db
    _seed_verdict(
        seeder,
        t=now,
        subject_kind="turn",
        subject_id="turn_a",
        score=0.9,
        confidence=0.8,
    )
    _seed_verdict(
        seeder,
        t=now,
        subject_kind="tool_cycle",
        subject_id="tu_a",
        score=0.4,
        confidence=0.6,
        rubric_id="tool-cycle-heuristic-v1",
    )
    with AnalyticsStore(db_path) as store:
        turn = store.quality(_window_around(now), subject_kind="turn", group_by="none")
        tool = store.quality(_window_around(now), subject_kind="tool_cycle", group_by="none")
    assert turn["verdict_count"] == 1
    assert turn["mean_score"] == 0.9
    assert tool["verdict_count"] == 1
    assert tool["mean_score"] == 0.4


def test_quality_invalid_group_by_raises(seeded_db: tuple[Path, DBSeeder], now: datetime) -> None:
    db_path, _ = seeded_db
    with AnalyticsStore(db_path) as store:
        with pytest.raises(InvalidGroupByError):
            store.quality(_window_around(now), group_by="invalid_dimension")


def test_quality_invalid_subject_kind_raises(
    seeded_db: tuple[Path, DBSeeder], now: datetime
) -> None:
    db_path, _ = seeded_db
    with AnalyticsStore(db_path) as store:
        with pytest.raises(InvalidGroupByError):
            store.quality(_window_around(now), subject_kind="bogus")


def test_quality_budget_exhausted_count_surfaces_in_aggregate(
    seeded_db: tuple[Path, DBSeeder], now: datetime
) -> None:
    """LLM judge budget-exhausted verdicts → counted under
    `budget_exhausted_count` so the dashboard can show throttling pressure."""
    db_path, seeder = seeded_db
    _seed_verdict(
        seeder,
        t=now,
        subject_id="t1",
        score=0.5,
        confidence=0.0,
        judge_kind="llm",
        signals={"budget_exhausted": True, "throttled_reason": "session_cap"},
    )
    with AnalyticsStore(db_path) as store:
        result = store.quality(_window_around(now), group_by="judge_kind")
    assert len(result) == 1
    assert result[0]["budget_exhausted_count"] == 1
