"""K-NN cluster aggregation per `pattern-store.md §8.3-§8.4`.

Implements the routing-engine §5.5 scoring formula:

    score_M = (1 - cost_weight) * normalized_success_M
            + cost_weight       * normalized_cost_efficiency_M

with sample-size-weighted means per §8.4. Pure functions — they take
materialized neighbor tuples and return a `PatternRecommendation`, so the
SQLite layer and the math are independently testable.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class _AggregateInputs:
    """Per-neighbor inputs the aggregator works on. Decoupled from the store
    so unit tests can supply plain tuples without touching SQLite."""

    primary_model: str
    success_score_mean: float
    success_score_count: int
    sample_size: int
    avg_cost_usd: Decimal


def _per_model_groups(
    rows: tuple[_AggregateInputs, ...],
) -> dict[str, list[_AggregateInputs]]:
    groups: dict[str, list[_AggregateInputs]] = {}
    for row in rows:
        groups.setdefault(row.primary_model, []).append(row)
    return groups


def _weighted_mean_success(rows: list[_AggregateInputs]) -> tuple[float, int]:
    """Sample-size weighted mean over rows with `success_score_count > 0`.

    Returns `(weighted_mean, total_score_count)`. When no row carries a
    score, returns `(0.0, 0)` and the caller treats success as unknown.
    """
    total_weight = 0
    weighted_sum = 0.0
    score_count = 0
    for row in rows:
        if row.success_score_count <= 0:
            continue
        weight = row.sample_size
        weighted_sum += row.success_score_mean * weight
        total_weight += weight
        score_count += row.success_score_count
    if total_weight == 0:
        return 0.0, 0
    return weighted_sum / total_weight, score_count


def _weighted_mean_cost(rows: list[_AggregateInputs]) -> Decimal:
    """Sample-size-weighted mean of `avg_cost_usd`."""
    total_weight = 0
    weighted_sum = Decimal("0")
    for row in rows:
        weight = row.sample_size
        weighted_sum += row.avg_cost_usd * weight
        total_weight += weight
    if total_weight == 0:
        return Decimal("0")
    return weighted_sum / Decimal(total_weight)


@dataclass(frozen=True)
class ScoredModel:
    model: str
    score: float
    sample_size: int
    avg_cost_usd: Decimal
    success_score_mean: float
    success_score_count: int


@dataclass(frozen=True)
class AggregationResult:
    """Result of aggregating K-NN neighbors into scored models.

    `chosen_model` is None when the cluster is empty or all candidates score
    zero. The caller (PatternStore.recommend) applies the confidence and
    sample-size thresholds.
    """

    chosen_model: str | None
    confidence: float
    ranked: tuple[ScoredModel, ...]
    chosen_sample_size: int


def aggregate_recommendation(
    rows: tuple[_AggregateInputs, ...],
    *,
    cost_weight: float,
) -> AggregationResult:
    """K-NN cluster → ranked scored models.

    Implements `routing-engine.md §5.5`. `cost_weight` is clamped to
    `[0.0, 1.0]`. Empty inputs return an empty result. See also `§10.1.25`:
    a degenerate cluster where all candidate models have identical
    `avg_cost_usd` zeroes the cost-efficiency term so scoring falls to pure
    quality.
    """
    cost_weight = max(0.0, min(1.0, cost_weight))
    if not rows:
        return AggregationResult(
            chosen_model=None,
            confidence=0.0,
            ranked=(),
            chosen_sample_size=0,
        )

    groups = _per_model_groups(rows)

    # Per-model means.
    per_model: dict[str, tuple[float, int, Decimal, int]] = {}
    for model, group in groups.items():
        success_mean, score_count = _weighted_mean_success(group)
        cost_mean = _weighted_mean_cost(group)
        sample_size_total = sum(r.sample_size for r in group)
        per_model[model] = (success_mean, score_count, cost_mean, sample_size_total)

    costs = [v[2] for v in per_model.values()]
    min_cost = min(costs)
    max_cost = max(costs)
    cost_span = max_cost - min_cost

    scored: list[ScoredModel] = []
    for model, (success_mean, score_count, cost_mean, sample_size_total) in per_model.items():
        if cost_span == 0:
            cost_efficiency = 0.0
        else:
            cost_efficiency = float((max_cost - cost_mean) / cost_span)
        score = (1.0 - cost_weight) * success_mean + cost_weight * cost_efficiency
        scored.append(
            ScoredModel(
                model=model,
                score=score,
                sample_size=sample_size_total,
                avg_cost_usd=cost_mean,
                success_score_mean=success_mean,
                success_score_count=score_count,
            )
        )

    # Stable sort: by score desc, then model id asc for determinism.
    scored.sort(key=lambda s: (-s.score, s.model))

    if not scored or scored[0].score <= 0.0:
        return AggregationResult(
            chosen_model=None,
            confidence=0.0,
            ranked=tuple(scored),
            chosen_sample_size=scored[0].sample_size if scored else 0,
        )

    top = scored[0]
    if len(scored) == 1:
        confidence = 1.0
    else:
        runner_up = scored[1].score
        if top.score <= 0:
            confidence = 0.0
        else:
            confidence = max(0.0, (top.score - runner_up) / top.score)
    return AggregationResult(
        chosen_model=top.model,
        confidence=confidence,
        ranked=tuple(scored),
        chosen_sample_size=top.sample_size,
    )


def now_ms() -> float:
    """Monotonic wall-clock in milliseconds; isolated so tests can patch."""
    return time.monotonic() * 1000
