"""Output-quality scoring (v1: heuristic tier only).

Per `docs/specs/evaluator.md`. The evaluator subscribes to terminal
events (`turn.completed`, `tool.completed`, `tool.failed`,
`session.ended`), runs a judge against the relevant subject context,
and emits `eval.started` / `eval.completed` / `eval.failed` on the bus.
Re-evaluation is available via `metis evaluate` and the `reevaluate()`
function.

v1 ships:
- `HeuristicJudge` — zero-cost rule-based scoring per subject kind.
- `BudgetTracker` — per-session and per-day caps (structural for the
  future LLM-as-judge tier; heuristic never spends).
- `register_evaluator()` — bus-wiring helper for the runtime.

LLM-as-judge and hybrid escalation are deferred to a later wave —
they require provider availability the heuristic tier doesn't have.
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
    "DEFAULT_PER_DAY_MAX_USD",
    "DEFAULT_PER_SESSION_MAX_USD",
    "DEFAULT_RUBRICS",
    "SESSION_AGGREGATE_RUBRIC_ID",
    "SESSION_AGGREGATE_RUBRIC_VERSION",
    "TOOL_CYCLE_HEURISTIC_RUBRIC_ID",
    "TOOL_CYCLE_HEURISTIC_RUBRIC_VERSION",
    "TURN_HEURISTIC_RUBRIC_ID",
    "TURN_HEURISTIC_RUBRIC_VERSION",
    "WORKLOAD_HEURISTIC_RUBRIC_ID",
    "WORKLOAD_HEURISTIC_RUBRIC_VERSION",
    "BudgetTracker",
    "EvalJudgeKind",
    "EvalSubjectKind",
    "EvalVerdict",
    "Evaluator",
    "HeuristicJudge",
    "Judge",
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
