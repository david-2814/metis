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
TURN_HEURISTIC_RUBRIC_VERSION = "1.1.0"

TOOL_CYCLE_HEURISTIC_RUBRIC_ID = "tool-cycle-heuristic-v1"
TOOL_CYCLE_HEURISTIC_RUBRIC_VERSION = "1.0.0"

SESSION_AGGREGATE_RUBRIC_ID = "session-aggregate-v1"
SESSION_AGGREGATE_RUBRIC_VERSION = "1.0.0"

# Bumped to 1.2.0 with the addition of the `partial_credit` primitive
# (evaluator.md §5.4 v1.2, §A3-rev6 / 13a-1 follow-up — finer-grained outcome
# scoring so partial-test-pass counts surface haiku-vs-sonnet differentiation
# the pass/fail substring check erases). Prior bump to 1.1.0 added the
# `grounding_tokens` / `forbidden_grounding` primitive. New score series on
# the dashboard rather than silent recalibration of prior verdicts.
WORKLOAD_HEURISTIC_RUBRIC_ID = "workload-heuristic-v1"
WORKLOAD_HEURISTIC_RUBRIC_VERSION = "1.2.0"


PartialCreditCriterion = Literal["test_pass_count_ratio"]
PartialCreditMap = Literal["linear", "stepped"]


@dataclass(frozen=True)
class PartialCreditConfig:
    """Partial-credit scoring config for the workload rubric (evaluator.md §5.4 v1.2).

    When `enabled=True` the heuristic computes a ratio in [0, 1] from the
    final response text (per `criterion`), applies `map`, and folds the
    result into the composed score in place of the pass/fail substring
    assertion. The `expect_substring_in_final_response` check is bypassed
    when partial-credit is active — the criterion is the new signal source.

    Criteria (v1):
    - `test_pass_count_ratio`: parses pytest summary lines (`N passed`,
      `M failed`, `K errors`) and `PASS N/M` / `FAIL N/M` runner output;
      ratio = passed / total. When no test signal is found, the ratio is
      0.0 and the negative flag `partial_credit_no_test_signal` fires.

    Maps (v1):
    - `linear`: score = ratio.
    - `stepped`: round ratio to nearest 0.25 (0.0, 0.25, 0.5, 0.75, 1.0).
      Useful when judges want a stable bucketed score rather than a
      continuous one.
    """

    enabled: bool = False
    criterion: PartialCreditCriterion = "test_pass_count_ratio"
    map: PartialCreditMap = "linear"


@dataclass(frozen=True)
class WorkloadRubric:
    """Parsed `workload.yaml.evaluate` block (evaluator.md §5.4 / benchmark.md §3.1).

    `rubric` is the planned judge tier; only `heuristic` is implemented in
    v1, but the field is accepted so workloads written today don't churn
    when LLM/hybrid land.

    `grounding_tokens` and `forbidden_grounding` are the v1.1 primitive for
    workloads that probe hallucination / source-grounding (§A3-rev: the
    original `expect_substring_in_final_response` rewards stylistic mimicry
    over actual grounding). Each list is a small set of substrings; the
    heuristic awards positive credit for grounding tokens present and
    negative credit for forbidden ones present. The lists are **independent**
    of `expect_substring_in_final_response` — workloads can use either, both,
    or neither.
    """

    rubric: Literal["heuristic", "llm", "hybrid"] = "heuristic"
    expect_substring_in_final_response: str | None = None
    llm_judge_model: str | None = None
    weight_per_turn: float = 1.0
    grounding_tokens: tuple[str, ...] = ()
    forbidden_grounding: tuple[str, ...] = ()
    partial_credit: PartialCreditConfig | None = None


_ALLOWED_EVALUATE_KEYS = {
    "rubric",
    "expect_substring_in_final_response",
    "llm_judge_model",
    "weight_per_turn",
    "grounding_tokens",
    "forbidden_grounding",
    "partial_credit",
}

_ALLOWED_PARTIAL_CREDIT_KEYS = {"enabled", "criterion", "map"}
_ALLOWED_PARTIAL_CREDIT_CRITERIA = {"test_pass_count_ratio"}
_ALLOWED_PARTIAL_CREDIT_MAPS = {"linear", "stepped"}


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
    grounding_tokens = _parse_string_list(raw.get("grounding_tokens"), key="grounding_tokens")
    forbidden_grounding = _parse_string_list(
        raw.get("forbidden_grounding"), key="forbidden_grounding"
    )
    partial_credit = _parse_partial_credit(raw.get("partial_credit"))
    return WorkloadRubric(
        rubric=rubric_kind,
        expect_substring_in_final_response=substring,
        llm_judge_model=llm_model,
        weight_per_turn=float(weight),
        grounding_tokens=grounding_tokens,
        forbidden_grounding=forbidden_grounding,
        partial_credit=partial_credit,
    )


def _parse_partial_credit(raw: Any) -> PartialCreditConfig | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise WorkloadRubricError("evaluate.partial_credit must be a mapping")
    unknown = set(raw) - _ALLOWED_PARTIAL_CREDIT_KEYS
    if unknown:
        raise WorkloadRubricError(f"unknown partial_credit keys: {sorted(unknown)}")
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise WorkloadRubricError("evaluate.partial_credit.enabled must be a boolean")
    criterion = raw.get("criterion", "test_pass_count_ratio")
    if criterion not in _ALLOWED_PARTIAL_CREDIT_CRITERIA:
        raise WorkloadRubricError(
            f"evaluate.partial_credit.criterion must be one of "
            f"{sorted(_ALLOWED_PARTIAL_CREDIT_CRITERIA)}; got {criterion!r}"
        )
    map_kind = raw.get("map", "linear")
    if map_kind not in _ALLOWED_PARTIAL_CREDIT_MAPS:
        raise WorkloadRubricError(
            f"evaluate.partial_credit.map must be one of "
            f"{sorted(_ALLOWED_PARTIAL_CREDIT_MAPS)}; got {map_kind!r}"
        )
    return PartialCreditConfig(enabled=enabled, criterion=criterion, map=map_kind)


def _parse_string_list(raw: Any, *, key: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(s, str) and s for s in raw):
        raise WorkloadRubricError(f"evaluate.{key} must be a list of non-empty strings")
    return tuple(raw)


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
    # `no_tool_exit_failure` distinguishes `tool.completed.success=False`
    # (clean exit, nonzero return — e.g. shell tool's nonzero exit code) from
    # `tool.failed` (uncaught Python exception). Sized so a single shell-tool
    # failure drops a clean turn's score by >0.3 (1.0 → 1.0/1.5 ≈ 0.667),
    # taking confidence below the v1 hybrid escalation threshold of 0.7.
    weight_no_tool_exit_failure: float = 0.5
    weight_no_max_tokens_hit: float = 0.15
    weight_tool_cycle_reasonable: float = 0.10
    tool_cycle_threshold: int = 20
    high_confidence_min_signals: int = 4

    def total_weight(self) -> float:
        return (
            self.weight_stop_reason_clean
            + self.weight_no_llm_failure
            + self.weight_no_tool_failure
            + self.weight_no_tool_exit_failure
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
