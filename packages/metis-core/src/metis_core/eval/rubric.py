"""Built-in rubrics and workload-rubric parsing.

The rubric defines how signals collapse into a `score` and what counts
as `confidence`. Each rubric has an `id` and a `version`; bumping the
version produces a new score series on the dashboard rather than silently
recalibrating prior verdicts (evaluator.md §12 invariant 7).

v1 ships four heuristic rubrics (one per subject kind) plus a parsed
workload-rubric object derived from `workload.yaml.evaluate`. The
weights are intentionally simple and live in code — when they grow, a
follow-up wave can move them to versioned yaml files alongside the rest.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# Bump when the heuristic weights or signal set changes. The number lands in
# every emitted EvalVerdict.rubric_version so consumers can detect a change.
TURN_HEURISTIC_RUBRIC_ID = "turn-heuristic-v1"
TURN_HEURISTIC_RUBRIC_VERSION = "1.0.0"

TOOL_CYCLE_HEURISTIC_RUBRIC_ID = "tool-cycle-heuristic-v1"
TOOL_CYCLE_HEURISTIC_RUBRIC_VERSION = "1.0.0"

SESSION_AGGREGATE_RUBRIC_ID = "session-aggregate-v1"
SESSION_AGGREGATE_RUBRIC_VERSION = "1.0.0"

WORKLOAD_HEURISTIC_RUBRIC_ID = "workload-heuristic-v1"
WORKLOAD_HEURISTIC_RUBRIC_VERSION = "1.0.0"


@dataclass(frozen=True)
class WorkloadRubric:
    """Parsed `workload.yaml.evaluate` block (evaluator.md §5.4 / benchmark.md §3.1).

    `rubric` is the planned judge tier; only `heuristic` is implemented in
    v1, but the field is accepted so workloads written today don't churn
    when LLM/hybrid land.
    """

    rubric: Literal["heuristic", "llm", "hybrid"] = "heuristic"
    expect_substring_in_final_response: str | None = None
    llm_judge_model: str | None = None
    weight_per_turn: float = 1.0


_ALLOWED_EVALUATE_KEYS = {
    "rubric",
    "expect_substring_in_final_response",
    "llm_judge_model",
    "weight_per_turn",
}


class WorkloadRubricError(ValueError):
    """Raised when `workload.yaml.evaluate` fails schema validation."""


def parse_workload_rubric(raw: Any) -> WorkloadRubric:
    """Validate a raw `evaluate` mapping and return a WorkloadRubric.

    Unknown keys are rejected so schema migrations have to flow through
    the spec; missing keys take their defaults. Empty/None input returns
    the default rubric — the absence of an `evaluate:` block means
    "heuristic with no substring assertion."
    """
    if raw is None:
        return WorkloadRubric()
    if not isinstance(raw, dict):
        raise WorkloadRubricError("evaluate block must be a mapping")
    unknown = set(raw) - _ALLOWED_EVALUATE_KEYS
    if unknown:
        raise WorkloadRubricError(f"unknown evaluate keys: {sorted(unknown)}")
    rubric_kind = raw.get("rubric", "heuristic")
    if rubric_kind not in ("heuristic", "llm", "hybrid"):
        raise WorkloadRubricError(
            f"evaluate.rubric must be one of heuristic|llm|hybrid; got {rubric_kind!r}"
        )
    weight = raw.get("weight_per_turn", 1.0)
    if not isinstance(weight, (int, float)) or weight < 0:
        raise WorkloadRubricError("evaluate.weight_per_turn must be a non-negative number")
    substring = raw.get("expect_substring_in_final_response")
    if substring is not None and not isinstance(substring, str):
        raise WorkloadRubricError("evaluate.expect_substring_in_final_response must be a string")
    llm_model = raw.get("llm_judge_model")
    if llm_model is not None and not isinstance(llm_model, str):
        raise WorkloadRubricError("evaluate.llm_judge_model must be a string")
    return WorkloadRubric(
        rubric=rubric_kind,
        expect_substring_in_final_response=substring,
        llm_judge_model=llm_model,
        weight_per_turn=float(weight),
    )


@dataclass(frozen=True)
class TurnHeuristicConfig:
    """Weights for the v1 turn heuristic.

    Concrete weights are an implementation detail (evaluator.md §5.1):
    the contract is that explicit feedback dominates implicit signals
    dominates lifecycle signals, and the score is bounded. `tool_cycle_threshold`
    is the configurable maximum tool calls in a turn before the
    `tool_cycle_count_reasonable` signal fires negative.
    """

    weight_stop_reason_clean: float = 0.25
    weight_no_llm_failure: float = 0.25
    weight_no_tool_failure: float = 0.25
    weight_no_max_tokens_hit: float = 0.15
    weight_tool_cycle_reasonable: float = 0.10
    tool_cycle_threshold: int = 20
    high_confidence_min_signals: int = 4

    def total_weight(self) -> float:
        return (
            self.weight_stop_reason_clean
            + self.weight_no_llm_failure
            + self.weight_no_tool_failure
            + self.weight_no_max_tokens_hit
            + self.weight_tool_cycle_reasonable
        )


@dataclass(frozen=True)
class ToolCycleHeuristicConfig:
    """Weights for the tool-cycle heuristic (evaluator.md §5.5)."""

    weight_succeeded: float = 0.6
    weight_no_immediate_re_call: float = 0.2
    weight_no_thrash_in_window: float = 0.2
    thrash_window_calls: int = 3


@dataclass(frozen=True)
class SessionAggregateConfig:
    """Weights for the session-aggregate heuristic (evaluator.md §5.6)."""

    weight_min_turn_score: float = 0.3
    weight_mean_turn_score: float = 0.5
    weight_completed_disposition: float = 0.2


@dataclass(frozen=True)
class RubricSet:
    """Bundle of rubric configs the judge consults."""

    turn: TurnHeuristicConfig = field(default_factory=TurnHeuristicConfig)
    tool_cycle: ToolCycleHeuristicConfig = field(default_factory=ToolCycleHeuristicConfig)
    session: SessionAggregateConfig = field(default_factory=SessionAggregateConfig)


DEFAULT_RUBRICS = RubricSet()
