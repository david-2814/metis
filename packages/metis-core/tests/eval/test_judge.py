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
    build_llm_failed,
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


async def test_turn_heuristic_llm_failure_lowers_score():
    """A failed upstream LLM call must not score as a clean turn."""
    session_id = "sess_test"
    turn_id = new_turn_id()
    events = [
        build_turn_completed(session_id=session_id, turn_id=turn_id),
        build_llm_failed(session_id=session_id, turn_id=turn_id),
        build_llm_completed(session_id=session_id, turn_id=turn_id),
    ]
    judge = HeuristicJudge()
    verdict = await judge.evaluate(
        SubjectContext(subject_kind="turn", subject_id=turn_id, events=events)
    )
    assert verdict.score < 1.0
    assert "llm_call_failed" in verdict.signals["flags_negative"]


async def test_turn_heuristic_excessive_tool_calls_lowers_score():
    """tool_call_count above the threshold (default 20) fires negative."""
    session_id = "sess_test"
    turn_id = new_turn_id()
    events = [
        build_turn_completed(session_id=session_id, turn_id=turn_id, tool_call_count=50),
        build_llm_completed(session_id=session_id, turn_id=turn_id),
    ]
    judge = HeuristicJudge()
    verdict = await judge.evaluate(
        SubjectContext(subject_kind="turn", subject_id=turn_id, events=events)
    )
    assert verdict.score < 1.0
    assert "tool_cycle_count_excessive" in verdict.signals["flags_negative"]


async def test_turn_heuristic_empty_final_response_lowers_score():
    """When the caller plumbs an empty final_response_text, score must drop.

    The lifecycle signals (stop_reason=end_turn, no failures) would otherwise
    score this 1.0. This is the gap hypothesis (b) — content-blind heuristic
    can't distinguish a successful answer from an empty response.
    """
    session_id = "sess_test"
    turn_id = new_turn_id()
    events = [
        build_turn_completed(session_id=session_id, turn_id=turn_id),
        build_llm_completed(session_id=session_id, turn_id=turn_id),
    ]
    judge = HeuristicJudge()
    verdict = await judge.evaluate(
        SubjectContext(
            subject_kind="turn",
            subject_id=turn_id,
            events=events,
            signals_extra={"final_response_text": "   "},
        )
    )
    assert verdict.score < 0.9
    assert "empty_assistant_response" in verdict.signals["flags_negative"]


async def test_turn_heuristic_refusal_text_lowers_score():
    """Assistant refusal at the start of the response triggers the penalty."""
    session_id = "sess_test"
    turn_id = new_turn_id()
    events = [
        build_turn_completed(session_id=session_id, turn_id=turn_id),
        build_llm_completed(session_id=session_id, turn_id=turn_id),
    ]
    judge = HeuristicJudge()
    verdict = await judge.evaluate(
        SubjectContext(
            subject_kind="turn",
            subject_id=turn_id,
            events=events,
            signals_extra={
                "final_response_text": "I cannot help with that request. Please ask something else.",
            },
        )
    )
    assert verdict.score < 0.9
    assert "assistant_refusal_detected" in verdict.signals["flags_negative"]


async def test_turn_heuristic_refusal_pattern_in_body_does_not_false_positive():
    """A substantive response that quotes 'I cannot' deep in the body is NOT a refusal."""
    session_id = "sess_test"
    turn_id = new_turn_id()
    events = [
        build_turn_completed(session_id=session_id, turn_id=turn_id),
        build_llm_completed(session_id=session_id, turn_id=turn_id),
    ]
    judge = HeuristicJudge()
    # 200+ chars of substantive text before the quoted refusal phrase.
    long_text = (
        "Here is the analysis you asked for. The function processes input in three stages, "
        "validates the schema, then writes the result. The docstring claims it 'cannot' handle "
        "empty inputs but the code actually does. I cannot find any other issues."
    )
    verdict = await judge.evaluate(
        SubjectContext(
            subject_kind="turn",
            subject_id=turn_id,
            events=events,
            signals_extra={"final_response_text": long_text},
        )
    )
    assert verdict.score == 1.0
    assert "assistant_refusal_detected" not in verdict.signals["flags_negative"]


async def test_tool_cycle_heuristic_immediate_re_call_same_input_lowers_score():
    """A second tool.called with identical (name, input_hash) right after fires negative."""
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
        build_tool_completed(session_id=session_id, turn_id=turn_id, tool_use_id=a, success=True),
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
    assert "immediate_re_call_same_input" in verdict.signals["flags_negative"]


async def test_session_heuristic_error_disposition_negative_flag():
    """session.ended with disposition=error fires the error negative flag."""
    session_id = "sess_err"
    events = [build_session_ended(session_id=session_id, disposition="error")]
    judge = HeuristicJudge()
    verdict = await judge.evaluate(
        SubjectContext(
            subject_kind="session",
            subject_id=session_id,
            events=events,
            signals_extra={"child_turn_scores": [0.3], "child_eval_ids": ["e1"]},
        )
    )
    assert verdict.score < 0.5
    assert "disposition_error" in verdict.signals["flags_negative"]


async def test_session_heuristic_low_child_scores_drag_aggregate_low():
    """A session whose turns all scored poorly aggregates to a low session score."""
    session_id = "sess_bad"
    events = [build_session_ended(session_id=session_id, disposition="completed")]
    judge = HeuristicJudge()
    verdict = await judge.evaluate(
        SubjectContext(
            subject_kind="session",
            subject_id=session_id,
            events=events,
            signals_extra={
                "child_turn_scores": [0.1, 0.2, 0.15],
                "child_eval_ids": ["e1", "e2", "e3"],
            },
        )
    )
    assert verdict.score < 0.5


async def test_workload_heuristic_combined_failure_scores_below_0_8():
    """Per-turn 0.3 + assertion fail + substring missing + empty text → clearly failing."""
    judge = HeuristicJudge()
    verdict = await judge.evaluate(
        SubjectContext(
            subject_kind="workload",
            subject_id="wl_intentional",
            events=[],
            workload_rubric=WorkloadRubric(
                rubric="heuristic",
                expect_substring_in_final_response="completed-fix",
            ),
            signals_extra={
                "per_turn_scores": [0.3, 0.3],
                "final_response_text": "",
                "assertion_failures": ["turn 0: tool_calls=0 < min_tool_calls=1"],
                "assertions_checked": True,
                "workload_name": "intentionally-failing-task",
            },
        )
    )
    assert verdict.score < 0.8
    assert "workload_assertions_failed" in verdict.signals["flags_negative"]
    assert "expected_substring_missing" in verdict.signals["flags_negative"]
    assert "workload_empty_assistant_response" in verdict.signals["flags_negative"]


async def test_workload_heuristic_refusal_text_lowers_score():
    """Refusal in final_response_text drops score even without substring assertion."""
    judge = HeuristicJudge()
    verdict = await judge.evaluate(
        SubjectContext(
            subject_kind="workload",
            subject_id="wl_refusal",
            events=[],
            workload_rubric=WorkloadRubric(rubric="heuristic"),
            signals_extra={
                "per_turn_scores": [1.0],
                "final_response_text": "I'm unable to do that.",
                "assertion_failures": [],
                "assertions_checked": True,
            },
        )
    )
    assert verdict.score < 0.9
    assert "workload_assistant_refusal_detected" in verdict.signals["flags_negative"]


async def test_intentionally_failing_workload_fixture_scores_below_0_8():
    """Load the benchmarks/workloads/intentionally-failing-task fixture and
    simulate the most plausible failure shapes — refusal or empty response,
    sentinel substring missing — and verify the workload heuristic returns
    score < 0.8. This is the 'testing the test' check.
    """
    import pathlib

    import yaml
    from metis_core.eval.rubric import parse_workload_rubric

    repo_root = pathlib.Path(__file__).resolve().parents[4]
    workload_path = repo_root / "benchmarks/workloads/intentionally-failing-task/workload.yaml"
    raw = yaml.safe_load(workload_path.read_text())
    rubric = parse_workload_rubric(raw.get("evaluate"))

    judge = HeuristicJudge()
    for failure_text in ("I cannot help with that.", "   ", ""):
        verdict = await judge.evaluate(
            SubjectContext(
                subject_kind="workload",
                subject_id="wl_intentional",
                events=[],
                workload_rubric=rubric,
                signals_extra={
                    "per_turn_scores": [0.5],
                    "final_response_text": failure_text,
                    "assertion_failures": [],
                    "assertions_checked": True,
                    "workload_name": "intentionally-failing-task",
                },
            )
        )
        assert verdict.score < 0.8, f"text={failure_text!r} → score={verdict.score}"
        assert "expected_substring_missing" in verdict.signals["flags_negative"]


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
