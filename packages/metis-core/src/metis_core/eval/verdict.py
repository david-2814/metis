"""Evaluator verdict.

The single output of a judge invocation. Per evaluator.md §4 the verdict
shape is small and stable: one numeric `score` in [0, 1], a `confidence`
in [0, 1], and a free-form `signals` dict for audit/replay.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

import msgspec

EvalSubjectKind = Literal["turn", "tool_cycle", "session", "workload"]
EvalJudgeKind = Literal["heuristic", "llm", "hybrid"]


class EvalVerdict(msgspec.Struct, frozen=True):
    """A single recorded verdict (evaluator.md §4.1).

    `score` is in [0, 1]; higher is better. `confidence` is in [0, 1] and
    is a gate (consumers filter by threshold), not a score modifier.
    `signals` is judge-internal evidence — never structural to routing.
    """

    eval_id: str
    subject_kind: EvalSubjectKind
    subject_id: str
    score: float
    confidence: float
    judge_kind: EvalJudgeKind
    judge_cost_usd: Decimal
    judge_latency_ms: int
    rubric_id: str
    rubric_version: str
    signals: dict
    created_at: str
    judge_model: str | None = None
    judge_pricing_version: str | None = None
    parent_eval_id: str | None = None


def clamp_unit(value: float) -> float:
    """Clamp into [0.0, 1.0]. Score and confidence invariants depend on this."""
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value
