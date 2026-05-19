"""Bus subscriber wiring for the evaluator.

Per evaluator.md §6.1 the subscriber is non-fast-path. The heuristic
judge is ms-fast but neither it nor the future LLM judge belongs on a
critical user-facing path. The subscriber:

1. Filters to terminal events (`turn.completed`, `tool.completed`,
   `tool.failed`, `session.ended`).
2. Builds a SubjectContext from the trace store (events for the turn,
   for the surrounding tool sequence, etc.).
3. Emits `eval.started`, runs the judge, then emits `eval.completed` or
   `eval.failed`.
4. Records cost against the BudgetTracker for any non-zero spend.

The subscriber is intentionally tolerant: if the trace store can't
resolve a subject's events (subject not found), it emits `eval.failed`
with the relevant `failure_mode` rather than propagating the exception.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal

from metis.core.canonical.ids import next_monotonic_ulid
from metis.core.eval.budget import BudgetTracker, ThrottleReason
from metis.core.eval.judge import HeuristicJudge, Judge, SubjectContext
from metis.core.eval.verdict import EvalSubjectKind, EvalVerdict
from metis.core.events.bus import EventBus, EventFilter, Subscription, SubscriptionHandle
from metis.core.events.envelope import Actor, Event, Sensitivity
from metis.core.events.payloads import EvalCompleted, EvalFailed, EvalStarted, make_event
from metis.core.trace.store import TraceStore

logger = logging.getLogger(__name__)


_TRIGGER_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "turn.completed",
        "tool.completed",
        "tool.failed",
        "session.ended",
    }
)


class Evaluator:
    """Owns the judge + budget tracker + bus glue.

    Lives for the lifetime of a `ChatRuntime`; one instance per bus.
    """

    def __init__(
        self,
        *,
        bus: EventBus,
        trace: TraceStore,
        judge: Judge | None = None,
        budget: BudgetTracker | None = None,
    ) -> None:
        self._bus = bus
        self._trace = trace
        self._judge: Judge = judge or HeuristicJudge()
        self._budget = budget or BudgetTracker()
        self._handle: SubscriptionHandle | None = None

    @property
    def judge(self) -> Judge:
        return self._judge

    @property
    def budget(self) -> BudgetTracker:
        return self._budget

    # ---- registration --------------------------------------------------

    def register(self, name: str = "evaluator") -> SubscriptionHandle:
        """Subscribe to terminal events; return the handle for tear-down."""
        if self._handle is not None:
            return self._handle
        handle = self._bus.subscribe(
            Subscription(
                filter=EventFilter(event_types=frozenset(_TRIGGER_EVENT_TYPES)),
                handler=self._on_event,
                name=name,
                fast_path=False,
            )
        )
        self._handle = handle
        return handle

    def unregister(self) -> None:
        if self._handle is not None:
            self._bus.unsubscribe(self._handle)
            self._handle = None

    # ---- dispatch ------------------------------------------------------

    async def _on_event(self, event: Event) -> None:
        if event.type == "turn.completed":
            await self.evaluate_turn(
                session_id=event.session_id,
                turn_id=event.turn_id or event.payload.get("turn_id"),
                parent_event_id=event.id,
                trigger="bus",
            )
        elif event.type in ("tool.completed", "tool.failed"):
            tool_use_id = event.payload.get("tool_use_id")
            if not tool_use_id:
                return
            await self.evaluate_tool_cycle(
                session_id=event.session_id,
                turn_id=event.turn_id,
                tool_use_id=tool_use_id,
                parent_event_id=event.id,
                trigger="bus",
            )
        elif event.type == "session.ended":
            await self.evaluate_session(
                session_id=event.session_id,
                parent_event_id=event.id,
                trigger="bus",
            )

    # ---- public per-subject entry points (also used by `metis evaluate`) -

    async def evaluate_turn(
        self,
        *,
        session_id: str,
        turn_id: str | None,
        parent_event_id: str | None = None,
        trigger: str = "batch",
    ) -> EvalVerdict | None:
        if not turn_id:
            return await self._emit_failed(
                subject_kind="turn",
                subject_id="(missing)",
                session_id=session_id,
                parent_event_id=parent_event_id,
                failure_mode="subject_not_found",
                error_message="turn_id missing on turn.completed event",
            )
        events = self._trace.events_for_turn(turn_id)
        turn_completed_event = next((e for e in events if e.type == "turn.completed"), None)
        if turn_completed_event is None:
            return await self._emit_failed(
                subject_kind="turn",
                subject_id=turn_id,
                session_id=session_id,
                parent_event_id=parent_event_id,
                failure_mode="subject_not_found",
                error_message=f"no turn.completed found for turn_id={turn_id}",
            )
        # Forward `signals_extra` (carries `final_response_text` from the
        # session manager) into the SubjectContext so the heuristic judge's
        # content-penalty path — refusal / empty-response — fires on the
        # online subscriber path, not just the workload harness. See
        # `evaluator.md §5.1` "Content penalty (opt-in)".
        signals_extra = turn_completed_event.payload.get("signals_extra") or None
        ctx = SubjectContext(
            subject_kind="turn",
            subject_id=turn_id,
            events=events,
            session_id=session_id,
            signals_extra=signals_extra,
        )
        return await self._run_judge(
            ctx,
            session_id=session_id,
            turn_id=turn_id,
            parent_event_id=parent_event_id,
            trigger=trigger,
        )

    async def evaluate_tool_cycle(
        self,
        *,
        session_id: str,
        turn_id: str | None,
        tool_use_id: str,
        parent_event_id: str | None = None,
        trigger: str = "batch",
    ) -> EvalVerdict | None:
        # Scope events to the turn the tool ran in.
        events = self._trace.events_for_turn(turn_id) if turn_id else []
        if not events:
            return await self._emit_failed(
                subject_kind="tool_cycle",
                subject_id=tool_use_id,
                session_id=session_id,
                parent_event_id=parent_event_id,
                failure_mode="subject_not_found",
                error_message=f"no events found for turn_id={turn_id}",
            )
        ctx = SubjectContext(
            subject_kind="tool_cycle",
            subject_id=tool_use_id,
            events=events,
            session_id=session_id,
        )
        return await self._run_judge(
            ctx,
            session_id=session_id,
            turn_id=turn_id,
            parent_event_id=parent_event_id,
            trigger=trigger,
        )

    async def evaluate_workload(
        self,
        *,
        workload_run_id: str,
        session_id: str,
        per_turn_scores: list[float],
        final_response_text: str,
        assertion_failures: list[str],
        workload_rubric=None,
        workload_name: str | None = None,
        parent_event_id: str | None = None,
        trigger: str = "benchmark",
    ) -> EvalVerdict | None:
        """Workload-subject entry point (evaluator.md §5.4).

        Called by the benchmark harness after a workload run completes.
        Builds a SubjectContext with the harness-supplied signals and
        invokes the judge. The resulting verdict is emitted on the bus
        like any other; the harness reads `eval.completed.score` for the
        "savings on successful work" headline column.
        """
        ctx = SubjectContext(
            subject_kind="workload",
            subject_id=workload_run_id,
            events=[],
            workload_rubric=workload_rubric,
            session_id=session_id,
            signals_extra={
                "per_turn_scores": per_turn_scores,
                "final_response_text": final_response_text,
                "assertion_failures": assertion_failures,
                "assertions_checked": True,
                "workload_name": workload_name,
            },
        )
        return await self._run_judge(
            ctx,
            session_id=session_id,
            turn_id=None,
            parent_event_id=parent_event_id,
            trigger=trigger,
        )

    async def evaluate_session(
        self,
        *,
        session_id: str,
        parent_event_id: str | None = None,
        trigger: str = "batch",
    ) -> EvalVerdict | None:
        session_events = self._trace.events_for_session(session_id)
        if not any(e.type == "session.ended" for e in session_events):
            return await self._emit_failed(
                subject_kind="session",
                subject_id=session_id,
                session_id=session_id,
                parent_event_id=parent_event_id,
                failure_mode="subject_not_found",
                error_message=f"no session.ended for session_id={session_id}",
            )
        # Pull the latest turn-verdicts to aggregate. The session subscriber
        # only fires on session.ended, by which time any online turn
        # verdicts have already landed.
        child_scores: list[float] = []
        child_eval_ids: list[str] = []
        seen_subjects: set[str] = set()
        for e in reversed(session_events):
            if e.type != "eval.completed":
                continue
            if e.payload.get("subject_kind") != "turn":
                continue
            subject_id = e.payload.get("subject_id")
            if subject_id in seen_subjects:
                continue
            seen_subjects.add(subject_id)
            child_scores.append(float(e.payload.get("score") or 0.0))
            child_eval_ids.append(str(e.payload.get("eval_id")))
        ctx = SubjectContext(
            subject_kind="session",
            subject_id=session_id,
            events=session_events,
            session_id=session_id,
            signals_extra={
                "child_turn_scores": list(reversed(child_scores)),
                "child_eval_ids": list(reversed(child_eval_ids)),
            },
        )
        return await self._run_judge(
            ctx,
            session_id=session_id,
            turn_id=None,
            parent_event_id=parent_event_id,
            trigger=trigger,
        )

    # ---- core driver ---------------------------------------------------

    async def _run_judge(
        self,
        ctx: SubjectContext,
        *,
        session_id: str,
        turn_id: str | None,
        parent_event_id: str | None,
        trigger: str,
    ) -> EvalVerdict:
        eval_id_planned = str(next_monotonic_ulid())
        rubric_id, rubric_version = self._planned_rubric_for(ctx)
        # eval.started uses the *planned* eval id; the judge mints its own
        # id for the eventual verdict — they may diverge if the verdict
        # eval_id is allocated fresh inside the judge. We carry both ids in
        # the corresponding completed/failed events for traceability.
        self._emit(
            type="eval.started",
            session_id=session_id,
            turn_id=turn_id,
            parent_event_id=parent_event_id,
            payload=EvalStarted(
                eval_id=eval_id_planned,
                subject_kind=ctx.subject_kind,
                subject_id=ctx.subject_id,
                rubric_id=rubric_id,
                rubric_version=rubric_version,
                judge_kind_planned=self._judge.judge_kind,  # type: ignore[arg-type]
                trigger=trigger,  # type: ignore[arg-type]
            ),
        )
        throttle: ThrottleReason | None = self._budget.throttle_reason(
            session_id=session_id, projected_cost_usd=self._projected_cost(ctx)
        )
        try:
            verdict = await self._judge.evaluate(ctx)
        except Exception as exc:
            logger.warning(
                "evaluator: judge failed subject=%s/%s err=%s",
                ctx.subject_kind,
                ctx.subject_id,
                exc,
                exc_info=True,
            )
            return await self._emit_failed(
                subject_kind=ctx.subject_kind,
                subject_id=ctx.subject_id,
                session_id=session_id,
                parent_event_id=parent_event_id,
                failure_mode="judge_call_failed",
                error_message=f"{type(exc).__name__}: {exc}",
            )

        if throttle is not None:
            verdict_signals = dict(verdict.signals)
            verdict_signals["throttled_reason"] = throttle
            verdict = EvalVerdict(
                eval_id=verdict.eval_id,
                subject_kind=verdict.subject_kind,
                subject_id=verdict.subject_id,
                score=verdict.score,
                confidence=verdict.confidence,
                judge_kind="heuristic",  # downgrade
                judge_cost_usd=Decimal("0"),
                judge_latency_ms=verdict.judge_latency_ms,
                rubric_id=verdict.rubric_id,
                rubric_version=verdict.rubric_version,
                signals=verdict_signals,
                judge_model=None,
                judge_pricing_version=None,
                parent_eval_id=verdict.parent_eval_id,
                created_at=verdict.created_at,
            )

        self._budget.record(session_id=session_id, cost_usd=verdict.judge_cost_usd)
        self._emit(
            type="eval.completed",
            session_id=session_id,
            turn_id=turn_id,
            parent_event_id=parent_event_id,
            sensitivity=self._sensitivity_for(verdict),
            payload=EvalCompleted(
                eval_id=verdict.eval_id,
                subject_kind=verdict.subject_kind,
                subject_id=verdict.subject_id,
                score=verdict.score,
                confidence=verdict.confidence,
                judge_kind=verdict.judge_kind,  # type: ignore[arg-type]
                judge_cost_usd=verdict.judge_cost_usd,
                judge_latency_ms=verdict.judge_latency_ms,
                rubric_id=verdict.rubric_id,
                rubric_version=verdict.rubric_version,
                signals=verdict.signals,
                judge_model=verdict.judge_model,
                judge_pricing_version=verdict.judge_pricing_version,
                parent_eval_id=verdict.parent_eval_id,
            ),
        )
        return verdict

    # ---- helpers -------------------------------------------------------

    def _emit(
        self,
        *,
        type: str,
        session_id: str,
        turn_id: str | None,
        parent_event_id: str | None,
        payload,
        sensitivity: Sensitivity | None = None,
    ) -> None:
        try:
            event = make_event(
                type=type,
                session_id=session_id,
                actor=Actor.SYSTEM,
                payload=payload,
                timestamp=datetime.now(UTC),
                turn_id=turn_id,
                parent_event_id=parent_event_id,
                sensitivity=sensitivity,
            )
            self._bus.emit(event)
        except Exception:
            logger.warning("evaluator: failed to emit %s", type, exc_info=True)

    async def _emit_failed(
        self,
        *,
        subject_kind: EvalSubjectKind,
        subject_id: str,
        session_id: str,
        parent_event_id: str | None,
        failure_mode: str,
        error_message: str,
    ) -> None:
        self._emit(
            type="eval.failed",
            session_id=session_id,
            turn_id=None,
            parent_event_id=parent_event_id,
            payload=EvalFailed(
                eval_id=str(next_monotonic_ulid()),
                subject_kind=subject_kind,
                subject_id=subject_id,
                failure_mode=failure_mode,  # type: ignore[arg-type]
                error_message=error_message,
                judge_latency_ms=0,
            ),
        )
        return None

    def _projected_cost(self, ctx: SubjectContext) -> Decimal:
        # v1: heuristic only → zero cost. The LLM-as-judge tier will
        # estimate a per-call cost here based on input-token estimation.
        return Decimal("0")

    def _planned_rubric_for(self, ctx: SubjectContext) -> tuple[str, str]:
        from metis.core.eval.rubric import (
            SESSION_AGGREGATE_RUBRIC_ID,
            SESSION_AGGREGATE_RUBRIC_VERSION,
            TOOL_CYCLE_HEURISTIC_RUBRIC_ID,
            TOOL_CYCLE_HEURISTIC_RUBRIC_VERSION,
            TURN_HEURISTIC_RUBRIC_ID,
            TURN_HEURISTIC_RUBRIC_VERSION,
            WORKLOAD_HEURISTIC_RUBRIC_ID,
            WORKLOAD_HEURISTIC_RUBRIC_VERSION,
        )

        if ctx.subject_kind == "turn":
            return TURN_HEURISTIC_RUBRIC_ID, TURN_HEURISTIC_RUBRIC_VERSION
        if ctx.subject_kind == "tool_cycle":
            return TOOL_CYCLE_HEURISTIC_RUBRIC_ID, TOOL_CYCLE_HEURISTIC_RUBRIC_VERSION
        if ctx.subject_kind == "session":
            return SESSION_AGGREGATE_RUBRIC_ID, SESSION_AGGREGATE_RUBRIC_VERSION
        return WORKLOAD_HEURISTIC_RUBRIC_ID, WORKLOAD_HEURISTIC_RUBRIC_VERSION

    def _sensitivity_for(self, verdict: EvalVerdict) -> Sensitivity | None:
        # §4.4.1: catalog floor is `user_controlled` (the worst case, when
        # rationale_redacted is populated). When rationale is absent the event
        # carries structural metadata only, so we downgrade to `pseudonymous`
        # — a move toward less private, which §4.4.1 explicitly allows.
        if verdict.signals.get("rationale_redacted"):
            return None  # keep the floor
        return Sensitivity.PSEUDONYMOUS


def register_evaluator(
    bus: EventBus,
    trace: TraceStore,
    *,
    judge: Judge | None = None,
    budget: BudgetTracker | None = None,
    name: str = "evaluator",
) -> tuple[Evaluator, SubscriptionHandle]:
    """Build + register an Evaluator. Returns (evaluator, subscription handle).

    The handle is unsubscribed by `Evaluator.unregister()`; the tuple
    return is so callers can keep both references for tear-down.
    """
    evaluator = Evaluator(bus=bus, trace=trace, judge=judge, budget=budget)
    handle = evaluator.register(name=name)
    return evaluator, handle
