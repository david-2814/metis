"""Output-quality scoring (heuristic, LLM, hybrid tiers).

Per `docs/specs/evaluator.md`. The evaluator subscribes to terminal
events (`turn.completed`, `tool.completed`, `tool.failed`,
`session.ended`), runs a judge against the relevant subject context,
and emits `eval.started` / `eval.completed` / `eval.failed` on the bus.
Re-evaluation is available via `metis evaluate` and the `reevaluate()`
function.

Ships:
- `HeuristicJudge` — zero-cost rule-based scoring per subject kind.
- `LLMJudge` — prompts a configurable judge model (default haiku) and
  parses a JSON verdict; budget-gated via the shared `BudgetTracker`.
- `HybridJudge` — heuristic floor + LLM escalation when heuristic
  confidence is below `escalation_threshold` (default 0.7).
- `BudgetTracker` — per-session and per-day caps shared across judges.
- `register_evaluator()` — bus-wiring helper for the runtime.

Tool-cycle and session subjects remain heuristic-only in v1
(evaluator.md §5.5 / §5.6); LLM-eligible subjects are `turn` and
`workload`.
"""

from metis_core.eval.budget import (
    DEFAULT_PER_DAY_MAX_USD,
    DEFAULT_PER_SESSION_MAX_USD,
    BudgetTracker,
    ThrottleReason,
)
from metis_core.eval.cli import main as evaluate_main
from metis_core.eval.cli import reevaluate
from metis_core.eval.judge import (
    HeuristicJudge,
    Judge,
    SubjectContext,
)
from metis_core.eval.llm_judge import (
    DEFAULT_ESCALATION_THRESHOLD,
    DEFAULT_JUDGE_MAX_OUTPUT_TOKENS,
    TURN_HYBRID_RUBRIC_ID,
    TURN_HYBRID_RUBRIC_VERSION,
    TURN_LLM_RUBRIC_ID,
    TURN_LLM_RUBRIC_VERSION,
    WORKLOAD_HYBRID_RUBRIC_ID,
    WORKLOAD_HYBRID_RUBRIC_VERSION,
    WORKLOAD_LLM_RUBRIC_ID,
    WORKLOAD_LLM_RUBRIC_VERSION,
    HybridJudge,
    LLMJudge,
    LLMJudgeConfig,
    LLMJudgeError,
)
from metis_core.eval.rubric import (
    DEFAULT_RUBRICS,
    SESSION_AGGREGATE_RUBRIC_ID,
    SESSION_AGGREGATE_RUBRIC_VERSION,
    TOOL_CYCLE_HEURISTIC_RUBRIC_ID,
    TOOL_CYCLE_HEURISTIC_RUBRIC_VERSION,
    TURN_HEURISTIC_RUBRIC_ID,
    TURN_HEURISTIC_RUBRIC_VERSION,
    WORKLOAD_HEURISTIC_RUBRIC_ID,
    WORKLOAD_HEURISTIC_RUBRIC_VERSION,
    PartialCreditConfig,
    PartialCreditCriterion,
    PartialCreditMap,
    RubricSet,
    SessionAggregateConfig,
    ToolCycleHeuristicConfig,
    TurnHeuristicConfig,
    WorkloadRubric,
    WorkloadRubricError,
    parse_workload_rubric,
)
from metis_core.eval.subscriber import Evaluator, register_evaluator
from metis_core.eval.verdict import (
    EvalJudgeKind,
    EvalSubjectKind,
    EvalVerdict,
    clamp_unit,
)

__all__ = [
    "DEFAULT_ESCALATION_THRESHOLD",
    "DEFAULT_JUDGE_MAX_OUTPUT_TOKENS",
    "DEFAULT_PER_DAY_MAX_USD",
    "DEFAULT_PER_SESSION_MAX_USD",
    "DEFAULT_RUBRICS",
    "SESSION_AGGREGATE_RUBRIC_ID",
    "SESSION_AGGREGATE_RUBRIC_VERSION",
    "TOOL_CYCLE_HEURISTIC_RUBRIC_ID",
    "TOOL_CYCLE_HEURISTIC_RUBRIC_VERSION",
    "TURN_HEURISTIC_RUBRIC_ID",
    "TURN_HEURISTIC_RUBRIC_VERSION",
    "TURN_HYBRID_RUBRIC_ID",
    "TURN_HYBRID_RUBRIC_VERSION",
    "TURN_LLM_RUBRIC_ID",
    "TURN_LLM_RUBRIC_VERSION",
    "WORKLOAD_HEURISTIC_RUBRIC_ID",
    "WORKLOAD_HEURISTIC_RUBRIC_VERSION",
    "WORKLOAD_HYBRID_RUBRIC_ID",
    "WORKLOAD_HYBRID_RUBRIC_VERSION",
    "WORKLOAD_LLM_RUBRIC_ID",
    "WORKLOAD_LLM_RUBRIC_VERSION",
    "BudgetTracker",
    "EvalJudgeKind",
    "EvalSubjectKind",
    "EvalVerdict",
    "Evaluator",
    "HeuristicJudge",
    "HybridJudge",
    "Judge",
    "LLMJudge",
    "LLMJudgeConfig",
    "LLMJudgeError",
    "PartialCreditConfig",
    "PartialCreditCriterion",
    "PartialCreditMap",
    "RubricSet",
    "SessionAggregateConfig",
    "SubjectContext",
    "ThrottleReason",
    "ToolCycleHeuristicConfig",
    "TurnHeuristicConfig",
    "WorkloadRubric",
    "WorkloadRubricError",
    "clamp_unit",
    "evaluate_main",
    "parse_workload_rubric",
    "reevaluate",
    "register_evaluator",
]
