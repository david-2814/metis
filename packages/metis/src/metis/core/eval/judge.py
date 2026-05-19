"""Judge protocol + v1 heuristic implementations.

Per evaluator.md §5: each subject kind gets a rubric; the heuristic tier
reads events from the trace and applies a weighted-sum scoring function.
LLM-as-judge and hybrid escalation are deferred to a later wave — the
heuristic floor is enough to validate the contract end-to-end.

The judge produces an `EvalVerdict` carrying the *signals* it observed
and the rubric metadata. The verdict is the same shape regardless of
which judge produced it; the routing pattern store and analytics
endpoints read one number — `score` — and ignore the rest unless they
opt into the audit trail (`signals`).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from metis.core.canonical.ids import next_monotonic_ulid
from metis.core.eval.rubric import (
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
    RubricSet,
    WorkloadRubric,
)
from metis.core.eval.verdict import EvalSubjectKind, EvalVerdict, clamp_unit
from metis.core.events.envelope import Event


@dataclass(frozen=True)
class SubjectContext:
    """Events and metadata the judge consults to produce a verdict.

    Built by the subscriber (online path) or the CLI (batch path) by
    walking the trace store for the subject. `signals_extra` lets the
    benchmark harness pass workload-rubric inputs (assistant text,
    workload-name etc.) the bus events don't carry directly.

    `session_id` is the session that owns this subject — required by the
    LLM/Hybrid judges so they can charge their inference cost against the
    shared BudgetTracker's per-session cap. The heuristic tier ignores it.
    """

    subject_kind: EvalSubjectKind
    subject_id: str
    events: list[Event]
    parent_eval_id: str | None = None
    workload_rubric: WorkloadRubric | None = None
    signals_extra: dict | None = None
    session_id: str | None = None


@runtime_checkable
class Judge(Protocol):
    """Pluggable judge surface. v1 ships HeuristicJudge."""

    judge_kind: str  # "heuristic" | "llm" | "hybrid"

    async def evaluate(self, ctx: SubjectContext) -> EvalVerdict: ...


class HeuristicJudge:
    """Zero-cost, rule-based judge.

    All scoring is deterministic — same events → same verdict. This is
    the property tested by `tests/eval/test_judge.py::test_heuristic_determinism`.
    """

    judge_kind = "heuristic"

    def __init__(self, rubrics: RubricSet | None = None) -> None:
        self._rubrics = rubrics or DEFAULT_RUBRICS

    async def evaluate(self, ctx: SubjectContext) -> EvalVerdict:
        started_ns = time.perf_counter_ns()
        if ctx.subject_kind == "turn":
            score, confidence, signals = self._evaluate_turn(ctx)
            rubric_id, rubric_version = (
                TURN_HEURISTIC_RUBRIC_ID,
                TURN_HEURISTIC_RUBRIC_VERSION,
            )
        elif ctx.subject_kind == "tool_cycle":
            score, confidence, signals = self._evaluate_tool_cycle(ctx)
            rubric_id, rubric_version = (
                TOOL_CYCLE_HEURISTIC_RUBRIC_ID,
                TOOL_CYCLE_HEURISTIC_RUBRIC_VERSION,
            )
        elif ctx.subject_kind == "session":
            score, confidence, signals = self._evaluate_session(ctx)
            rubric_id, rubric_version = (
                SESSION_AGGREGATE_RUBRIC_ID,
                SESSION_AGGREGATE_RUBRIC_VERSION,
            )
        elif ctx.subject_kind == "workload":
            score, confidence, signals = self._evaluate_workload(ctx)
            rubric_id, rubric_version = (
                WORKLOAD_HEURISTIC_RUBRIC_ID,
                WORKLOAD_HEURISTIC_RUBRIC_VERSION,
            )
        else:  # pragma: no cover — Literal exhausts at type-check time
            raise ValueError(f"unknown subject_kind: {ctx.subject_kind}")
        latency_ms = max(0, (time.perf_counter_ns() - started_ns) // 1_000_000)
        return EvalVerdict(
            eval_id=str(next_monotonic_ulid()),
            subject_kind=ctx.subject_kind,
            subject_id=ctx.subject_id,
            score=clamp_unit(score),
            confidence=clamp_unit(confidence),
            judge_kind="heuristic",
            judge_cost_usd=Decimal("0"),
            judge_latency_ms=int(latency_ms),
            rubric_id=rubric_id,
            rubric_version=rubric_version,
            signals=signals,
            parent_eval_id=ctx.parent_eval_id,
            created_at=datetime.now(UTC).isoformat(),
        )

    # ---- per-subject scoring ------------------------------------------

    def _evaluate_turn(self, ctx: SubjectContext) -> tuple[float, float, dict]:
        cfg = self._rubrics.turn
        turn_completed = _find(ctx.events, "turn.completed")
        # Signals fire positive (clean) or negative (broken). Weights collapse
        # them into the score; flags get the audit-trail signal names.
        flags: list[str] = []
        flags_negative: list[str] = []
        weighted = 0.0

        stop_reason = turn_completed.payload.get("stop_reason") if turn_completed else None
        if stop_reason == "end_turn":
            flags.append("stop_reason_clean")
            weighted += cfg.weight_stop_reason_clean
        else:
            flags_negative.append("stop_reason_unclean")

        if not any(e.type == "llm.call_failed" for e in ctx.events):
            flags.append("no_llm_failure")
            weighted += cfg.weight_no_llm_failure
        else:
            flags_negative.append("llm_call_failed")

        if not any(e.type == "tool.failed" for e in ctx.events):
            flags.append("no_tool_failure")
            weighted += cfg.weight_no_tool_failure
        else:
            flags_negative.append("tool_failed")

        # `tool.failed` only fires on uncaught Python exceptions; a shell tool
        # returning a non-zero exit code emits `tool.completed` with
        # `success=False`. Without this gate the heuristic awards a clean
        # lifecycle score to runs that printed "FAIL N/M" and exited non-zero
        # (see benchmarks/RESULTS.md §A3 for the case this closes).
        tool_exit_failure = any(
            e.type == "tool.completed" and e.payload.get("success") is False for e in ctx.events
        )
        if not tool_exit_failure:
            flags.append("no_tool_exit_failure")
            weighted += cfg.weight_no_tool_exit_failure
        else:
            flags_negative.append("tool_returned_failure")

        # max_tokens: any nested llm.call_completed with stop_reason=max_tokens
        # within the turn signals an over-long response.
        max_tokens_hit = any(
            e.type == "llm.call_completed" and e.payload.get("stop_reason") == "max_tokens"
            for e in ctx.events
        )
        if not max_tokens_hit:
            flags.append("no_max_tokens_hit")
            weighted += cfg.weight_no_max_tokens_hit
        else:
            flags_negative.append("max_tokens_hit")

        tool_call_count = (
            turn_completed.payload.get("tool_call_count") if turn_completed else 0
        ) or 0
        if tool_call_count <= cfg.tool_cycle_threshold:
            flags.append("tool_cycle_count_reasonable")
            weighted += cfg.weight_tool_cycle_reasonable
        else:
            flags_negative.append("tool_cycle_count_excessive")

        base_score = weighted / cfg.total_weight() if cfg.total_weight() > 0 else 0.0

        # Opt-in content check: when the caller plumbs assistant text via
        # signals_extra, detect empty / refusal patterns that the event-based
        # lifecycle signals can't see. A turn with stop_reason=end_turn and
        # no failures otherwise scores 1.0 even on a clean refusal. Spec §5.1
        # describes inputs as event-derived; this fires only when callers
        # explicitly opt in (workload harness today; future message-store-
        # aware subscriber).
        content_penalty, content_flags = _content_penalty(ctx.signals_extra or {}, prefix="")
        flags_negative.extend(content_flags)
        score = base_score * content_penalty

        # Confidence: high when no contradictions, lower when negative flags
        # fire alongside positive ones. The spec's §4.3 "low confidence when
        # signals contradict" rule maps to: contradictions := negative flags
        # that contradict explicit-clean.
        positives = len(flags)
        negatives = len(flags_negative)
        if negatives == 0 and positives >= cfg.high_confidence_min_signals:
            confidence = 0.9
        elif negatives == 0:
            confidence = 0.7
        elif negatives == 1:
            confidence = 0.55
        else:
            confidence = 0.35

        signals: dict[str, Any] = {
            "flags": flags,
            "flags_negative": flags_negative,
            "tool_call_count": int(tool_call_count),
        }
        if turn_completed is not None:
            signals["stop_reason"] = stop_reason
        return score, confidence, signals

    def _evaluate_tool_cycle(self, ctx: SubjectContext) -> tuple[float, float, dict]:
        cfg = self._rubrics.tool_cycle
        # The subject id is the tool_use_id. We look at the matching
        # tool.completed/tool.failed and at any later tool.called events
        # that share the same tool_name.
        completed = _find(
            ctx.events, "tool.completed", lambda e: _payload_get(e, "tool_use_id") == ctx.subject_id
        )
        failed = _find(
            ctx.events, "tool.failed", lambda e: _payload_get(e, "tool_use_id") == ctx.subject_id
        )
        called = _find(
            ctx.events, "tool.called", lambda e: _payload_get(e, "tool_use_id") == ctx.subject_id
        )
        flags: list[str] = []
        flags_negative: list[str] = []
        weighted = 0.0

        succeeded = completed is not None and bool(completed.payload.get("success", False))
        if succeeded:
            flags.append("tool_succeeded")
            weighted += cfg.weight_succeeded
        else:
            flags_negative.append("tool_failed" if failed is not None else "tool_did_not_complete")

        # Find this call's position in the turn's tool.called sequence so we
        # can read the next few siblings.
        tool_called_events = [e for e in ctx.events if e.type == "tool.called"]
        my_index = -1
        for i, e in enumerate(tool_called_events):
            if _payload_get(e, "tool_use_id") == ctx.subject_id:
                my_index = i
                break

        my_input_hash = _payload_get(called, "input_hash") if called else None
        my_tool_name = _payload_get(called, "tool_name") if called else None

        immediate = (
            tool_called_events[my_index + 1]
            if 0 <= my_index < len(tool_called_events) - 1
            else None
        )
        immediate_re_call = (
            immediate is not None
            and _payload_get(immediate, "tool_name") == my_tool_name
            and _payload_get(immediate, "input_hash") == my_input_hash
        )
        if not immediate_re_call:
            flags.append("no_immediate_re_call_same_input")
            weighted += cfg.weight_no_immediate_re_call
        else:
            flags_negative.append("immediate_re_call_same_input")

        # Thrash: same tool_name called within the next thrash_window_calls
        # siblings (regardless of input_hash). The spec defines thrash more
        # precisely as "small hamming distance"; v1 uses the simpler "same
        # tool repeated" heuristic and lets the audit trail show the detail.
        window_start = my_index + 1 if my_index >= 0 else 0
        window = tool_called_events[window_start : window_start + cfg.thrash_window_calls]
        thrash = any(_payload_get(e, "tool_name") == my_tool_name for e in window)
        if not thrash:
            flags.append("no_thrash_in_window")
            weighted += cfg.weight_no_thrash_in_window
        else:
            flags_negative.append("thrash_in_window")

        total = (
            cfg.weight_succeeded + cfg.weight_no_immediate_re_call + cfg.weight_no_thrash_in_window
        )
        score = weighted / total if total > 0 else 0.0

        if len(flags_negative) == 0:
            confidence = 0.85
        elif len(flags_negative) == 1:
            confidence = 0.6
        else:
            confidence = 0.4

        signals: dict[str, Any] = {
            "flags": flags,
            "flags_negative": flags_negative,
            "tool_name": my_tool_name,
            "succeeded": succeeded,
        }
        return score, confidence, signals

    def _evaluate_session(self, ctx: SubjectContext) -> tuple[float, float, dict]:
        cfg = self._rubrics.session
        # Child turn verdicts are passed through signals_extra. The
        # subscriber/CLI populates them after looking up `eval.completed`
        # events with subject_kind=turn for this session_id.
        extra = ctx.signals_extra or {}
        child_scores: list[float] = list(extra.get("child_turn_scores") or [])
        child_eval_ids: list[str] = list(extra.get("child_eval_ids") or [])
        session_ended = _find(ctx.events, "session.ended")
        disposition = session_ended.payload.get("disposition") if session_ended else None

        flags: list[str] = []
        flags_negative: list[str] = []
        if child_scores:
            mean_score = sum(child_scores) / len(child_scores)
            min_score = min(child_scores)
        else:
            mean_score = 0.5  # No turns → no signal; pin neutral.
            min_score = 0.5

        weighted = cfg.weight_mean_turn_score * mean_score + cfg.weight_min_turn_score * min_score
        if disposition == "completed":
            flags.append("disposition_completed")
            weighted += cfg.weight_completed_disposition * 1.0
        elif disposition == "abandoned":
            flags_negative.append("disposition_abandoned")
        elif disposition == "error":
            flags_negative.append("disposition_error")
        total = (
            cfg.weight_mean_turn_score
            + cfg.weight_min_turn_score
            + cfg.weight_completed_disposition
        )
        score = weighted / total if total > 0 else 0.0
        confidence = 0.7 if child_scores else 0.4
        signals: dict[str, Any] = {
            "flags": flags,
            "flags_negative": flags_negative,
            "child_eval_ids": child_eval_ids,
            "mean_turn_score": mean_score,
            "min_turn_score": min_score,
            "turn_count": len(child_scores),
            "disposition": disposition,
        }
        return score, confidence, signals

    def _evaluate_workload(self, ctx: SubjectContext) -> tuple[float, float, dict]:
        # Workload-rubric ingestion (evaluator.md §5.4): pull the per-turn
        # outcomes from signals_extra and apply the workload rubric.
        extra = ctx.signals_extra or {}
        per_turn_scores: list[float] = list(extra.get("per_turn_scores") or [])
        assertion_failures: list[str] = list(extra.get("assertion_failures") or [])
        final_response_text: str = str(extra.get("final_response_text") or "")
        rubric = ctx.workload_rubric or WorkloadRubric()
        weight_per_turn = max(0.0, rubric.weight_per_turn)

        flags: list[str] = []
        flags_negative: list[str] = []

        if per_turn_scores:
            # Uniform weight_per_turn → unweighted mean. Future versions can
            # accept per-turn overrides; current schema uses one number.
            base = sum(per_turn_scores) / len(per_turn_scores)
        else:
            base = 0.5

        # Partial-credit primitive (evaluator.md §5.4 v1.2, §A3-rev6 / 13a-1
        # follow-up): when configured, parse a mid-score from the final
        # response text (e.g. `passed/total` for pytest runs) and bypass the
        # pass/fail substring assertion entirely. Substring-only rubrics
        # collapse partial successes (3/4 tests pass) to 0; partial-credit
        # surfaces the gradient haiku and sonnet actually produce.
        partial_credit_active = rubric.partial_credit is not None and rubric.partial_credit.enabled
        partial_credit_score: float | None = None
        partial_credit_signal: dict[str, Any] | None = None
        if partial_credit_active:
            (
                partial_credit_score,
                partial_credit_signal,
                pc_flags,
                pc_neg_flags,
            ) = _partial_credit_score(rubric.partial_credit, final_response_text)
            flags.extend(pc_flags)
            flags_negative.extend(pc_neg_flags)

        # Substring assertion (evaluator.md §5.4 special-case heuristic).
        # Bypassed when partial_credit is active — the criterion is the new
        # signal source. Workloads should pick one or the other.
        substring_present: bool | None = None
        if rubric.expect_substring_in_final_response and not partial_credit_active:
            substring_present = (
                rubric.expect_substring_in_final_response.lower() in final_response_text.lower()
            )
            if substring_present:
                flags.append("expected_substring_present")
            else:
                flags_negative.append("expected_substring_missing")

        # Grounding-check primitive (evaluator.md §5.4, added v1.1 after
        # benchmarks/RESULTS.md §A3-rev exposed the substring check rewarding
        # stylistic mimicry over real source-code grounding). Returns a score
        # in [0, 1] and a separate flag set; folded into the composed score
        # alongside the substring assertion.
        grounding_score, grounding_flags, grounding_neg_flags, grounding_signal = _grounding_score(
            rubric, final_response_text
        )
        flags.extend(grounding_flags)
        flags_negative.extend(grounding_neg_flags)

        # Assertion-set pass = no assertion failures and substring matches if
        # required. Heavy weight when explicit assertions exist.
        if assertion_failures:
            flags_negative.append("workload_assertions_failed")
        else:
            flags.append("workload_assertions_passed")

        # Compose: base score is the mean turn score; assertions and the
        # substring assertion (or partial-credit ratio) shift it.
        score = base
        if assertion_failures:
            score = score * 0.5
        if substring_present is True:
            score = (score + 1.0) / 2.0
        elif substring_present is False:
            score = score * 0.5
        elif partial_credit_score is not None:
            # Average the partial-credit ratio into the base. A perfect
            # partial-credit ratio (1.0) recovers the same boost as a
            # passing substring assertion ((base + 1.0)/2); a zero ratio
            # produces the same penalty as a failing one (base/2 ≡
            # (base + 0)/2). Mid-ratios produce mid-scores — the whole
            # point of this primitive.
            score = (score + partial_credit_score) / 2.0

        # Grounding score (when configured) is averaged into the composed
        # score with equal weight — a workload that fully grounds in real
        # symbols and avoids fabricated ones receives an unchanged score; a
        # workload with no grounding tokens present and forbidden ones
        # present is halved. Workloads that don't configure grounding pass
        # `grounding_score is None` and the multiplier is a no-op.
        if grounding_score is not None:
            score = (score + grounding_score) / 2.0

        # Same opt-in content check as the turn rubric. Workloads without a
        # substring expectation would otherwise score 1.0 even on a refusal.
        content_penalty, content_flags = _content_penalty(extra, prefix="workload_")
        flags_negative.extend(content_flags)
        score = score * content_penalty

        # Workload-level confidence: high when we have per-turn signal AND
        # at least one explicit assertion (substring, assertion-set,
        # grounding tokens, or a partial-credit signal with a real test
        # parse).
        partial_credit_has_signal = partial_credit_signal is not None and bool(
            partial_credit_signal.get("test_signal_found")
        )
        has_explicit = (
            substring_present is not None
            or bool(extra.get("assertions_checked"))
            or grounding_score is not None
            or partial_credit_has_signal
        )
        if per_turn_scores and has_explicit:
            confidence = 0.8
        elif per_turn_scores:
            confidence = 0.6
        else:
            confidence = 0.4

        signals: dict[str, Any] = {
            "flags": flags,
            "flags_negative": flags_negative,
            "per_turn_count": len(per_turn_scores),
            "weight_per_turn": weight_per_turn,
            "substring_present": substring_present,
            "assertion_failures": assertion_failures,
            "workload_name": extra.get("workload_name"),
        }
        if grounding_signal is not None:
            signals["workload_grounding_score"] = grounding_signal["score"]
            signals["grounding_tokens_present"] = grounding_signal["tokens_present"]
            signals["grounding_tokens_missing"] = grounding_signal["tokens_missing"]
            signals["forbidden_grounding_present"] = grounding_signal["forbidden_present"]
        if partial_credit_signal is not None:
            signals["partial_credit_score"] = partial_credit_signal["score"]
            signals["partial_credit_ratio"] = partial_credit_signal["ratio"]
            signals["partial_credit_criterion"] = partial_credit_signal["criterion"]
            signals["partial_credit_map"] = partial_credit_signal["map"]
            signals["partial_credit_passed"] = partial_credit_signal["passed"]
            signals["partial_credit_total"] = partial_credit_signal["total"]
            signals["partial_credit_test_signal_found"] = partial_credit_signal["test_signal_found"]
        return score, confidence, signals


def _find(events: list[Event], event_type: str, predicate=None) -> Event | None:
    for e in events:
        if e.type != event_type:
            continue
        if predicate is None or predicate(e):
            return e
    return None


def _payload_get(event: Event | None, key: str) -> Any:
    if event is None:
        return None
    return event.payload.get(key)


# Refusal patterns anchored to the response head (first ~160 chars) so
# substantive answers that incidentally quote these phrases don't false-
# positive. Real refusals almost always lead with one of these.
_REFUSAL_PATTERNS: tuple[str, ...] = (
    "i cannot help",
    "i can't help",
    "i cannot assist",
    "i can't assist",
    "i cannot do that",
    "i can't do that",
    "i'm unable to",
    "i am unable to",
    "i'm not able to",
    "i am not able to",
    "i won't help",
    "i refuse to",
    "sorry, i can't",
    "sorry, i cannot",
)
_REFUSAL_HEAD_CHARS = 160


def _content_penalty(extra: dict, *, prefix: str) -> tuple[float, list[str]]:
    """Detect refusal / empty response when text is plumbed via signals_extra.

    Returns (penalty_multiplier, negative_flags). Penalty is 1.0 (no-op) when
    the caller didn't plumb `final_response_text` — keeps the bus-subscriber
    path unchanged until a future task wires assistant text through it.
    """
    if "final_response_text" not in extra:
        return 1.0, []
    text = str(extra.get("final_response_text") or "")
    stripped = text.strip()
    if not stripped:
        return 0.4, [f"{prefix}empty_assistant_response"]
    head = stripped[:_REFUSAL_HEAD_CHARS].lower()
    if any(pat in head for pat in _REFUSAL_PATTERNS):
        return 0.5, [f"{prefix}assistant_refusal_detected"]
    return 1.0, []


def _grounding_score(
    rubric: WorkloadRubric, final_response_text: str
) -> tuple[float | None, list[str], list[str], dict[str, Any] | None]:
    """Score how well a final response grounds in expected source-code symbols.

    Workload-rubric primitive added in v1.1 after benchmarks/RESULTS.md
    §A3-rev showed the original substring assertion rewarded stylistic
    mimicry over real grounding (sonnet cited the real `PolicyEvaluation`
    dataclass and lowercase `policy=` literals — strictly more grounded —
    but scored 0.50 because it didn't parrot the UPPERCASE
    `PATTERN_RECOMMENDATION` label from the module docstring).

    Heuristic algorithm (cost: $0):

    - For each `grounding_tokens` substring present in the response, count
      a positive hit. Score component = `present / total`.
    - For each `forbidden_grounding` substring present, count a negative
      hit. Score component = `1 - (present / total)`.
    - Combined score = `(positive + forbidden_clean) / 2` when both lists
      are non-empty; otherwise the configured side alone.

    Returns (score | None, positive_flags, negative_flags, signal_dict).
    Returns `None` for the score when the rubric configures neither list —
    callers treat None as a no-op multiplier.
    """
    grounding_tokens = rubric.grounding_tokens
    forbidden_grounding = rubric.forbidden_grounding
    if not grounding_tokens and not forbidden_grounding:
        return None, [], [], None

    text_lower = final_response_text.lower()

    tokens_present = [t for t in grounding_tokens if t.lower() in text_lower]
    tokens_missing = [t for t in grounding_tokens if t.lower() not in text_lower]
    forbidden_present = [t for t in forbidden_grounding if t.lower() in text_lower]

    pos_flags: list[str] = []
    neg_flags: list[str] = []
    components: list[float] = []

    if grounding_tokens:
        pos_score = len(tokens_present) / len(grounding_tokens)
        components.append(pos_score)
        if tokens_present:
            pos_flags.append("workload_grounding_tokens_present")
        if tokens_missing:
            neg_flags.append("workload_grounding_tokens_missing")
    if forbidden_grounding:
        clean_score = 1.0 - (len(forbidden_present) / len(forbidden_grounding))
        components.append(clean_score)
        if forbidden_present:
            neg_flags.append("workload_forbidden_grounding_present")
        else:
            pos_flags.append("workload_forbidden_grounding_clean")

    score = sum(components) / len(components)
    signal: dict[str, Any] = {
        "score": score,
        "tokens_present": tokens_present,
        "tokens_missing": tokens_missing,
        "forbidden_present": forbidden_present,
    }
    return score, pos_flags, neg_flags, signal


# Pytest summary line: matches `N passed`, `M failed`, `K error(s)`. These
# appear independently across the final line; the parser picks the LAST
# occurrence of each, since iterative agents often print intermediate
# summaries before the final one.
_PYTEST_PASSED_RE = re.compile(r"(\d+)\s+passed", re.IGNORECASE)
_PYTEST_FAILED_RE = re.compile(r"(\d+)\s+failed", re.IGNORECASE)
_PYTEST_ERROR_RE = re.compile(r"(\d+)\s+errors?", re.IGNORECASE)

# `PASS N/M` / `FAIL N/M` runner output. N is the count that passed (or that
# the line is reporting); M is the total. Both PASS and FAIL forms carry the
# same ratio shape — for PASS the count up front is the pass count, for FAIL
# it's also the pass count (the runner.py convention in this repo prints
# "FAIL N/M" where N is how many passed). We pick the LAST occurrence in the
# text because iterative outputs often emit per-case lines followed by the
# final summary.
_RUNNER_PASS_FAIL_RE = re.compile(r"\b(?:PASS|FAIL)\s+(\d+)\s*/\s*(\d+)\b", re.IGNORECASE)


def _partial_credit_score(
    config: PartialCreditConfig, final_response_text: str
) -> tuple[float, dict[str, Any], list[str], list[str]]:
    """Compute the partial-credit score for the configured criterion.

    Returns (score, signal_dict, positive_flags, negative_flags). The score
    is in [0, 1]; the signal carries the parse details for the audit trail.
    When no test signal is found in the response, returns score=0 with the
    `partial_credit_no_test_signal` negative flag — a missing signal is
    treated as a failure (the agent was asked to run tests and report).
    """
    if config.criterion == "test_pass_count_ratio":
        ratio, passed, total, signal_found = _parse_test_pass_count(final_response_text)
    else:  # pragma: no cover — Literal exhausts at type-check time
        raise ValueError(f"unknown partial-credit criterion: {config.criterion}")

    pos_flags: list[str] = []
    neg_flags: list[str] = []
    if not signal_found:
        neg_flags.append("partial_credit_no_test_signal")
    elif ratio >= 1.0:
        pos_flags.append("partial_credit_full")
    elif ratio <= 0.0:
        neg_flags.append("partial_credit_zero")
    else:
        pos_flags.append("partial_credit_partial")

    mapped = _apply_partial_credit_map(ratio, config.map)
    signal: dict[str, Any] = {
        "score": mapped,
        "ratio": ratio,
        "passed": passed,
        "total": total,
        "criterion": config.criterion,
        "map": config.map,
        "test_signal_found": signal_found,
    }
    return mapped, signal, pos_flags, neg_flags


def _apply_partial_credit_map(ratio: float, map_kind: str) -> float:
    if map_kind == "stepped":
        # Round to the nearest quarter (0.0, 0.25, 0.5, 0.75, 1.0).
        # Useful when callers want a stable bucketed score rather than the
        # continuous linear version. The boundary values stay exact at 0 and 1.
        clamped = 0.0 if ratio < 0 else (1.0 if ratio > 1 else ratio)
        return round(clamped * 4) / 4
    # `linear` (default): pass the ratio through, clamped to [0, 1].
    return 0.0 if ratio < 0 else (1.0 if ratio > 1 else ratio)


def _parse_test_pass_count(text: str) -> tuple[float, int, int, bool]:
    """Extract (ratio, passed, total, signal_found) from response text.

    Recognized shapes:
    - `PASS N/M` or `FAIL N/M` runner output (per `runner.py` convention in
      this repo's workloads).
    - Pytest summary tokens: `N passed`, `M failed`, `K error(s)`. Total is
      passed + failed + errors. Skipped tests are excluded from the total
      (they don't represent successes or failures).

    When both shapes appear, the `PASS N/M` runner line wins — it's the
    most explicit total. Multiple matches of the same shape: the LAST
    match wins (iterative agents often print intermediate summaries).
    """
    runner_matches = list(_RUNNER_PASS_FAIL_RE.finditer(text))
    if runner_matches:
        last = runner_matches[-1]
        passed = int(last.group(1))
        total = int(last.group(2))
        if total <= 0:
            return 0.0, passed, total, False
        ratio = passed / total
        return _clamp01(ratio), passed, total, True

    passed_matches = list(_PYTEST_PASSED_RE.finditer(text))
    failed_matches = list(_PYTEST_FAILED_RE.finditer(text))
    error_matches = list(_PYTEST_ERROR_RE.finditer(text))
    passed = int(passed_matches[-1].group(1)) if passed_matches else 0
    failed = int(failed_matches[-1].group(1)) if failed_matches else 0
    errors = int(error_matches[-1].group(1)) if error_matches else 0
    total = passed + failed + errors
    if total <= 0:
        return 0.0, 0, 0, False
    ratio = passed / total
    return _clamp01(ratio), passed, total, True


def _clamp01(x: float) -> float:
    if x < 0:
        return 0.0
    if x > 1:
        return 1.0
    return x
