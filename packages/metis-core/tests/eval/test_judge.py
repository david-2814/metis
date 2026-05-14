"""HeuristicJudge per-subject scoring."""

from __future__ import annotations

from decimal import Decimal

from metis_core.eval import (
    TURN_HEURISTIC_RUBRIC_ID,
    HeuristicJudge,
    SubjectContext,
    WorkloadRubric,
)

from .helpers import (
    build_llm_completed,
    build_session_ended,
    build_tool_called,
    build_tool_completed,
    build_tool_failed,
    build_turn_completed,
    new_tool_use_id,
    new_turn_id,
)


async def test_turn_heuristic_clean_turn_scores_one():
    session_id = "sess_test"
    turn_id = new_turn_id()
    events = [
        build_turn_completed(session_id=session_id, turn_id=turn_id),
        build_llm_completed(session_id=session_id, turn_id=turn_id),
    ]
    judge = HeuristicJudge()
    verdict = await judge.evaluate(
        SubjectContext(subject_kind="turn", subject_id=turn_id, events=events)
    )
    assert verdict.score == 1.0
    assert verdict.confidence >= 0.7
    assert verdict.judge_kind == "heuristic"
    assert verdict.judge_cost_usd == Decimal("0")
    assert verdict.judge_pricing_version is None
    assert verdict.rubric_id == TURN_HEURISTIC_RUBRIC_ID
    assert "stop_reason_clean" in verdict.signals["flags"]
    assert "no_tool_failure" in verdict.signals["flags"]


async def test_turn_heuristic_max_tokens_lowers_score():
    session_id = "sess_test"
    turn_id = new_turn_id()
    events = [
        build_turn_completed(session_id=session_id, turn_id=turn_id, stop_reason="max_tokens"),
        build_llm_completed(session_id=session_id, turn_id=turn_id, stop_reason="max_tokens"),
    ]
    judge = HeuristicJudge()
    verdict = await judge.evaluate(
        SubjectContext(subject_kind="turn", subject_id=turn_id, events=events)
    )
    assert verdict.score < 1.0
    assert "stop_reason_unclean" in verdict.signals["flags_negative"]
    assert "max_tokens_hit" in verdict.signals["flags_negative"]


async def test_turn_heuristic_tool_failure_lowers_score_and_confidence():
    session_id = "sess_test"
    turn_id = new_turn_id()
    tool_use_id = new_tool_use_id()
    events = [
        build_turn_completed(session_id=session_id, turn_id=turn_id, tool_call_count=1),
        build_tool_called(
            session_id=session_id,
            turn_id=turn_id,
            tool_use_id=tool_use_id,
            tool_name="read_file",
        ),
        build_tool_failed(session_id=session_id, turn_id=turn_id, tool_use_id=tool_use_id),
    ]
    judge = HeuristicJudge()
    verdict = await judge.evaluate(
        SubjectContext(subject_kind="turn", subject_id=turn_id, events=events)
    )
    assert verdict.score < 1.0
    assert "tool_failed" in verdict.signals["flags_negative"]


async def test_turn_heuristic_score_bounded_in_unit_interval():
    """Property check: any input → score in [0, 1]."""
    session_id = "sess_test"
    judge = HeuristicJudge()
    for stop_reason in ("end_turn", "max_tokens", "stop_sequence", "tool_use"):
        turn_id = new_turn_id()
        events = [
            build_turn_completed(
                session_id=session_id,
                turn_id=turn_id,
                stop_reason=stop_reason,
                tool_call_count=50,
            )
        ]
        verdict = await judge.evaluate(
            SubjectContext(subject_kind="turn", subject_id=turn_id, events=events)
        )
        assert 0.0 <= verdict.score <= 1.0
        assert 0.0 <= verdict.confidence <= 1.0


async def test_turn_heuristic_determinism():
    """Same input events → identical score, confidence, flags."""
    session_id = "sess_det"
    turn_id = new_turn_id()
    events = [
        build_turn_completed(session_id=session_id, turn_id=turn_id),
        build_llm_completed(session_id=session_id, turn_id=turn_id),
    ]
    judge = HeuristicJudge()
    v1 = await judge.evaluate(
        SubjectContext(subject_kind="turn", subject_id=turn_id, events=events)
    )
    v2 = await judge.evaluate(
        SubjectContext(subject_kind="turn", subject_id=turn_id, events=events)
    )
    assert v1.score == v2.score
    assert v1.confidence == v2.confidence
    assert v1.signals == v2.signals
    # eval_ids and created_at differ (monotonic ulid + wall clock); excluded.


async def test_tool_cycle_heuristic_success():
    session_id = "sess_test"
    turn_id = new_turn_id()
    tool_use_id = new_tool_use_id()
    events = [
        build_tool_called(
            session_id=session_id,
            turn_id=turn_id,
            tool_use_id=tool_use_id,
            tool_name="read_file",
            input_hash="h1",
        ),
        build_tool_completed(
            session_id=session_id, turn_id=turn_id, tool_use_id=tool_use_id, success=True
        ),
    ]
    judge = HeuristicJudge()
    verdict = await judge.evaluate(
        SubjectContext(subject_kind="tool_cycle", subject_id=tool_use_id, events=events)
    )
    assert verdict.score >= 0.9
    assert verdict.signals["succeeded"] is True
    assert "tool_succeeded" in verdict.signals["flags"]


async def test_tool_cycle_heuristic_failure_and_thrash():
    session_id = "sess_test"
    turn_id = new_turn_id()
    a = new_tool_use_id()
    b = new_tool_use_id()
    events = [
        build_tool_called(
            session_id=session_id,
            turn_id=turn_id,
            tool_use_id=a,
            tool_name="read_file",
            input_hash="h1",
        ),
        build_tool_failed(session_id=session_id, turn_id=turn_id, tool_use_id=a),
        build_tool_called(
            session_id=session_id,
            turn_id=turn_id,
            tool_use_id=b,
            tool_name="read_file",
            input_hash="h1",
        ),
    ]
    judge = HeuristicJudge()
    verdict = await judge.evaluate(
        SubjectContext(subject_kind="tool_cycle", subject_id=a, events=events)
    )
    assert verdict.score < 1.0
    assert "thrash_in_window" in verdict.signals["flags_negative"]


async def test_session_heuristic_aggregates_child_scores():
    session_id = "sess_aggr"
    events = [build_session_ended(session_id=session_id)]
    judge = HeuristicJudge()
    verdict = await judge.evaluate(
        SubjectContext(
            subject_kind="session",
            subject_id=session_id,
            events=events,
            signals_extra={
                "child_turn_scores": [0.9, 0.8, 1.0],
                "child_eval_ids": ["e1", "e2", "e3"],
            },
        )
    )
    assert 0.0 <= verdict.score <= 1.0
    assert verdict.score > 0.7  # weighted toward the child scores
    assert verdict.signals["child_eval_ids"] == ["e1", "e2", "e3"]
    assert verdict.signals["turn_count"] == 3
    assert verdict.signals["disposition"] == "completed"


async def test_session_heuristic_abandoned_disposition_negative_flag():
    session_id = "sess_abandoned"
    events = [build_session_ended(session_id=session_id, disposition="abandoned")]
    judge = HeuristicJudge()
    verdict = await judge.evaluate(
        SubjectContext(
            subject_kind="session",
            subject_id=session_id,
            events=events,
            signals_extra={"child_turn_scores": [0.4], "child_eval_ids": ["e1"]},
        )
    )
    assert "disposition_abandoned" in verdict.signals["flags_negative"]


async def test_workload_heuristic_substring_present():
    judge = HeuristicJudge()
    verdict = await judge.evaluate(
        SubjectContext(
            subject_kind="workload",
            subject_id="wl1",
            events=[],
            workload_rubric=WorkloadRubric(
                rubric="heuristic",
                expect_substring_in_final_response="off-by-one",
            ),
            signals_extra={
                "per_turn_scores": [0.9, 0.85],
                "final_response_text": "I found the off-by-one bug in slug.py.",
                "assertion_failures": [],
                "assertions_checked": True,
                "workload_name": "fix-a-bug-small",
            },
        )
    )
    assert verdict.score > 0.8
    assert verdict.signals["substring_present"] is True
    assert "expected_substring_present" in verdict.signals["flags"]


async def test_workload_heuristic_assertion_failure_penalty():
    judge = HeuristicJudge()
    verdict = await judge.evaluate(
        SubjectContext(
            subject_kind="workload",
            subject_id="wl2",
            events=[],
            workload_rubric=WorkloadRubric(rubric="heuristic"),
            signals_extra={
                "per_turn_scores": [1.0],
                "final_response_text": "",
                "assertion_failures": ["turn 0: tool_calls=7 > max_tool_calls=6"],
                "assertions_checked": True,
            },
        )
    )
    assert verdict.score < 1.0
    assert "workload_assertions_failed" in verdict.signals["flags_negative"]
