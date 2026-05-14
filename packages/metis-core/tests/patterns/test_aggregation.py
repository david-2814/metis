"""K-NN aggregation tests per pattern-store §8.3/§8.4."""

from __future__ import annotations

from decimal import Decimal

import pytest
from metis_core.patterns.aggregation import _AggregateInputs, aggregate_recommendation


def _row(
    *,
    model: str,
    success_mean: float,
    success_count: int,
    sample_size: int,
    avg_cost: str,
) -> _AggregateInputs:
    return _AggregateInputs(
        primary_model=model,
        success_score_mean=success_mean,
        success_score_count=success_count,
        sample_size=sample_size,
        avg_cost_usd=Decimal(avg_cost),
    )


def test_empty_inputs_return_no_recommendation() -> None:
    result = aggregate_recommendation((), cost_weight=0.3)
    assert result.chosen_model is None
    assert result.ranked == ()
    assert result.confidence == 0.0


def test_cost_weight_zero_picks_highest_success() -> None:
    rows = (
        _row(model="m_high", success_mean=0.9, success_count=10, sample_size=10, avg_cost="0.10"),
        _row(model="m_cheap", success_mean=0.3, success_count=10, sample_size=10, avg_cost="0.01"),
    )
    result = aggregate_recommendation(rows, cost_weight=0.0)
    assert result.chosen_model == "m_high"


def test_cost_weight_one_picks_cheapest() -> None:
    rows = (
        _row(model="m_high", success_mean=0.9, success_count=10, sample_size=10, avg_cost="0.10"),
        _row(model="m_cheap", success_mean=0.3, success_count=10, sample_size=10, avg_cost="0.01"),
    )
    result = aggregate_recommendation(rows, cost_weight=1.0)
    assert result.chosen_model == "m_cheap"


def test_blend_with_cost_weight_half() -> None:
    rows = (
        _row(model="m_high", success_mean=0.9, success_count=10, sample_size=10, avg_cost="0.10"),
        _row(model="m_cheap", success_mean=0.3, success_count=10, sample_size=10, avg_cost="0.01"),
    )
    result = aggregate_recommendation(rows, cost_weight=0.5)
    # Both contribute. With these numbers, m_high still wins
    # (0.5 * 0.9 + 0.5 * 0 = 0.45 vs 0.5 * 0.3 + 0.5 * 1 = 0.65) — wait
    # let's actually compute: m_cheap is cheaper so its cost_efficiency=1.
    # score_high = 0.5 * 0.9 + 0.5 * 0 = 0.45
    # score_cheap = 0.5 * 0.3 + 0.5 * 1 = 0.65 → m_cheap wins.
    assert result.chosen_model == "m_cheap"
    assert result.confidence > 0.0


def test_degenerate_identical_cost_falls_to_pure_quality() -> None:
    rows = (
        _row(model="m_a", success_mean=0.9, success_count=10, sample_size=10, avg_cost="0.10"),
        _row(model="m_b", success_mean=0.3, success_count=10, sample_size=10, avg_cost="0.10"),
    )
    result = aggregate_recommendation(rows, cost_weight=0.9)
    # cost_efficiency zeros out, so the cost term contributes 0 for both;
    # both reduce to (1 - 0.9) * success which picks m_a (higher success).
    assert result.chosen_model == "m_a"


def test_single_model_in_cluster_gets_confidence_one() -> None:
    rows = (
        _row(model="m_only", success_mean=0.8, success_count=5, sample_size=5, avg_cost="0.05"),
    )
    result = aggregate_recommendation(rows, cost_weight=0.3)
    assert result.chosen_model == "m_only"
    assert result.confidence == pytest.approx(1.0)


def test_sample_size_weighted_mean_dominates_single_shot() -> None:
    # Both rows are model_a — the heavy one (sample=50, mean=0.9) should
    # outweigh the lightweight one (sample=1, mean=0.0) when computing the
    # cluster mean.
    rows = (
        _row(
            model="m_a",
            success_mean=0.9,
            success_count=50,
            sample_size=50,
            avg_cost="0.10",
        ),
        _row(
            model="m_a",
            success_mean=0.0,
            success_count=1,
            sample_size=1,
            avg_cost="0.10",
        ),
    )
    result = aggregate_recommendation(rows, cost_weight=0.0)
    # Weighted mean = (0.9 * 50 + 0.0 * 1) / 51 ≈ 0.882 → close to 0.9.
    assert result.chosen_model == "m_a"
    assert result.ranked[0].success_score_mean == pytest.approx((0.9 * 50) / 51)


def test_no_score_signal_returns_zero_chosen() -> None:
    # No rows carry a score (count=0). Without success signal and with
    # cost_weight=0, every model scores 0 → no chosen.
    rows = (
        _row(model="m_a", success_mean=0.0, success_count=0, sample_size=5, avg_cost="0.10"),
        _row(model="m_b", success_mean=0.0, success_count=0, sample_size=5, avg_cost="0.10"),
    )
    result = aggregate_recommendation(rows, cost_weight=0.0)
    assert result.chosen_model is None
