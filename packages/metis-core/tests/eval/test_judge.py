"""HeuristicJudge per-subject scoring."""

from __future__ import annotations

from decimal import Decimal

from metis_core.eval import (
    TURN_HEURISTIC_RUBRIC_ID,
    HeuristicJudge,
    PartialCreditConfig,
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


async def test_turn_heuristic_tool_exit_failure_lowers_score_materially():
    """A `tool.completed` with `success=False` (clean-exit-nonzero — e.g. the
    shell tool's non-zero exit path) must drop the score by ≥0.3 from a
    clean baseline. This is the headline assertion that refutes the §A3
    null result: a regex-fail turn that exited 1 used to score 1.0 because
    the heuristic only watched `tool.failed` (exception path).
    """
    session_id = "sess_test"
    turn_id = new_turn_id()
    tool_use_id = new_tool_use_id()
    judge = HeuristicJudge()

    clean_events = [
        build_turn_completed(session_id=session_id, turn_id=turn_id, tool_call_count=1),
        build_tool_called(
            session_id=session_id,
            turn_id=turn_id,
            tool_use_id=tool_use_id,
            tool_name="shell",
        ),
        build_tool_completed(
            session_id=session_id, turn_id=turn_id, tool_use_id=tool_use_id, success=True
        ),
    ]
    clean = await judge.evaluate(
        SubjectContext(subject_kind="turn", subject_id=turn_id, events=clean_events)
    )

    failed_events = [
        build_turn_completed(session_id=session_id, turn_id=turn_id, tool_call_count=1),
        build_tool_called(
            session_id=session_id,
            turn_id=turn_id,
            tool_use_id=tool_use_id,
            tool_name="shell",
        ),
        build_tool_completed(
            session_id=session_id, turn_id=turn_id, tool_use_id=tool_use_id, success=False
        ),
    ]
    failed = await judge.evaluate(
        SubjectContext(subject_kind="turn", subject_id=turn_id, events=failed_events)
    )

    assert clean.score == 1.0
    assert "no_tool_exit_failure" in clean.signals["flags"]
    assert failed.score <= clean.score - 0.3, (
        f"clean={clean.score}, failed={failed.score}; drop must be ≥0.3"
    )
    assert "tool_returned_failure" in failed.signals["flags_negative"]
    assert "no_tool_exit_failure" not in failed.signals["flags"]


async def test_turn_heuristic_tool_exit_failure_drops_confidence_below_hybrid_threshold():
    """The new gate must drop heuristic confidence under 0.7 so HybridJudge
    (escalation_threshold=0.7 in v1, evaluator.md §5.3) escalates to the
    LLM judge. Without this property, the rubric extension wouldn't
    unblock §A3 on its own — see CHANGES.md note."""
    session_id = "sess_test"
    turn_id = new_turn_id()
    tool_use_id = new_tool_use_id()
    events = [
        build_turn_completed(session_id=session_id, turn_id=turn_id, tool_call_count=1),
        build_tool_called(
            session_id=session_id,
            turn_id=turn_id,
            tool_use_id=tool_use_id,
            tool_name="shell",
        ),
        build_tool_completed(
            session_id=session_id, turn_id=turn_id, tool_use_id=tool_use_id, success=False
        ),
    ]
    judge = HeuristicJudge()
    verdict = await judge.evaluate(
        SubjectContext(subject_kind="turn", subject_id=turn_id, events=events)
    )
    assert verdict.confidence < 0.7, (
        f"confidence={verdict.confidence} must be < hybrid escalation threshold 0.7"
    )


async def test_turn_heuristic_tool_completed_success_true_does_not_fire_negative():
    """A clean `tool.completed` with `success=True` is the common case and
    must not fire the new negative flag — extension is additive against
    the existing clean-turn fixture set."""
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
        build_tool_completed(
            session_id=session_id, turn_id=turn_id, tool_use_id=tool_use_id, success=True
        ),
    ]
    judge = HeuristicJudge()
    verdict = await judge.evaluate(
        SubjectContext(subject_kind="turn", subject_id=turn_id, events=events)
    )
    assert verdict.score == 1.0
    assert "no_tool_exit_failure" in verdict.signals["flags"]
    assert "tool_returned_failure" not in verdict.signals["flags_negative"]


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


# ---------------------------------------------------------------------------
# Grounding-check primitive (evaluator.md §5.4 v1.1; benchmarks/RESULTS.md §A3-rev)
# ---------------------------------------------------------------------------


_GROUNDING_RUBRIC = WorkloadRubric(
    rubric="heuristic",
    grounding_tokens=("RoutingEngine", "ModelRegistry", "PolicyEvaluation", "policy="),
    forbidden_grounding=("PATTERN_LOOKUP", "RouterChain", "ModelSelector", "PolicyChain"),
)


async def _score_with_response(text: str, rubric: WorkloadRubric = _GROUNDING_RUBRIC):
    judge = HeuristicJudge()
    return await judge.evaluate(
        SubjectContext(
            subject_kind="workload",
            subject_id="wl_grounding",
            events=[],
            workload_rubric=rubric,
            signals_extra={
                "per_turn_scores": [1.0],
                "final_response_text": text,
                "assertion_failures": [],
                "assertions_checked": True,
            },
        )
    )


async def test_workload_grounding_present_scores_higher_than_absent():
    """A response that names real symbols scores materially higher than one
    that names none — the heuristic floor must reward grounding, not just
    stylistic keyword matches.
    """
    grounded = (
        "The RoutingEngine class delegates to ModelRegistry; the chain emits "
        "a PolicyEvaluation per slot and the events serialize policy=pattern."
    )
    ungrounded = (
        "The router consults a chain of policies in order, picking the first "
        "that returns a model. Each policy can short-circuit."
    )
    grounded_v = await _score_with_response(grounded)
    ungrounded_v = await _score_with_response(ungrounded)
    assert grounded_v.score - ungrounded_v.score >= 0.2, (
        f"grounded {grounded_v.score} vs ungrounded {ungrounded_v.score}"
    )
    assert "workload_grounding_tokens_present" in grounded_v.signals["flags"]
    assert "workload_grounding_tokens_missing" in ungrounded_v.signals["flags_negative"]
    assert (
        grounded_v.signals["workload_grounding_score"]
        > ungrounded_v.signals["workload_grounding_score"]
    )


async def test_workload_forbidden_grounding_present_lowers_score():
    """A response that names fabricated symbols scores lower than one that
    avoids them, even when both are otherwise comparable.
    """
    fabricated = (
        "The RoutingEngine consults a RouterChain via PATTERN_LOOKUP; the "
        "ModelSelector picks the winning policy."
    )
    clean = (
        "The RoutingEngine consults policies in slot order; the ModelRegistry "
        "validates the chosen model."
    )
    fabricated_v = await _score_with_response(fabricated)
    clean_v = await _score_with_response(clean)
    assert clean_v.score > fabricated_v.score
    assert "workload_forbidden_grounding_present" in fabricated_v.signals["flags_negative"]
    assert "workload_forbidden_grounding_clean" in clean_v.signals["flags"]
    # Audit trail captures which forbidden tokens fired.
    assert set(fabricated_v.signals["forbidden_grounding_present"]) >= {
        "RouterChain",
        "PATTERN_LOOKUP",
        "ModelSelector",
    }


async def test_workload_grounding_paraphrase_beats_uppercase_label_parroting():
    """End-to-end §A3-rev fix: a response citing real symbols (PolicyEvaluation,
    lowercase policy=) scores ≥ a response that only parrots the
    UPPERCASE_LABEL convention from a docstring without other grounding.

    This is the literal regression case the §A3-rev finding documented:
    sonnet was strictly more grounded but the old substring rubric scored
    it 0.50 vs haiku's 1.00. The new primitive must reverse that.
    """
    sonnet_style = (
        "Routing slot 4 reads the PolicyEvaluation dataclass produced by the "
        "RoutingEngine; on the wire the slot serializes as policy=pattern. "
        "The ModelRegistry validates the candidate before commit."
    )
    haiku_style_uppercase_only = (
        "The slots are MANUAL_OVERRIDE, MANUAL_STICKY, RULE_MATCH, "
        "PATTERN_RECOMMENDATION, DELEGATE_REQUEST, WORKSPACE_DEFAULT, and "
        "GLOBAL_DEFAULT. The router walks them in order."
    )
    sonnet_v = await _score_with_response(sonnet_style)
    haiku_v = await _score_with_response(haiku_style_uppercase_only)
    assert sonnet_v.score >= haiku_v.score, (
        f"sonnet {sonnet_v.score} vs haiku-uppercase {haiku_v.score}"
    )
    assert (
        sonnet_v.signals["workload_grounding_score"] > haiku_v.signals["workload_grounding_score"]
    )


async def test_workload_grounding_absent_when_lists_empty():
    """No grounding lists configured → no grounding signal in the verdict."""
    rubric = WorkloadRubric(rubric="heuristic")
    verdict = await _score_with_response("anything", rubric=rubric)
    assert "workload_grounding_score" not in verdict.signals
    assert "grounding_tokens_present" not in verdict.signals


async def test_workload_grounding_only_forbidden_list_scored():
    """A rubric that configures only forbidden_grounding still produces a
    grounding score (clean-on-absence)."""
    rubric = WorkloadRubric(
        rubric="heuristic",
        forbidden_grounding=("PATTERN_LOOKUP", "RouterChain"),
    )
    clean = await _score_with_response("All is grounded.", rubric=rubric)
    assert clean.signals["workload_grounding_score"] == 1.0
    assert "workload_forbidden_grounding_clean" in clean.signals["flags"]
    fabricated = await _score_with_response("PATTERN_LOOKUP and RouterChain.", rubric=rubric)
    assert fabricated.signals["workload_grounding_score"] == 0.0


async def test_architectural_explanation_workload_fixture_uses_grounding_primitive():
    """Load the real workload.yaml and confirm the v1.1 fixture parses with
    the grounding lists wired."""
    import pathlib

    import yaml
    from metis_core.eval.rubric import parse_workload_rubric

    repo_root = pathlib.Path(__file__).resolve().parents[4]
    workload_path = (
        repo_root
        / "benchmarks/workloads/architectural-explanation-without-hallucination/workload.yaml"
    )
    raw = yaml.safe_load(workload_path.read_text())
    rubric = parse_workload_rubric(raw.get("evaluate"))
    assert rubric.rubric == "hybrid"
    assert "RoutingEngine" in rubric.grounding_tokens
    assert "PolicyEvaluation" in rubric.grounding_tokens
    assert "policy=" in rubric.grounding_tokens
    assert "PATTERN_LOOKUP" in rubric.forbidden_grounding
    # The misleading substring assertion is dropped per §A3-rev fix.
    assert rubric.expect_substring_in_final_response is None


# ---------------------------------------------------------------------------
# Partial-credit primitive (evaluator.md §5.4 v1.2; §A3-rev6 / 13a-1 follow-up)
# ---------------------------------------------------------------------------


_PC_LINEAR_RUBRIC = WorkloadRubric(
    rubric="heuristic",
    expect_substring_in_final_response="PASS 4/4",
    partial_credit=PartialCreditConfig(
        enabled=True, criterion="test_pass_count_ratio", map="linear"
    ),
)


async def _score_with_partial_credit(text: str, rubric: WorkloadRubric = _PC_LINEAR_RUBRIC):
    judge = HeuristicJudge()
    return await judge.evaluate(
        SubjectContext(
            subject_kind="workload",
            subject_id="wl_pc",
            events=[],
            workload_rubric=rubric,
            signals_extra={
                "per_turn_scores": [1.0],
                "final_response_text": text,
                "assertion_failures": [],
                "assertions_checked": True,
                "workload_name": "pc-test",
            },
        )
    )


async def test_partial_credit_half_tests_pass_scores_near_0_5():
    """Half tests passing → partial-credit ratio = 0.5; composed score
    lands between the full-pass and full-fail values (specifically the
    midpoint between base and 0.5)."""
    verdict = await _score_with_partial_credit("Final summary: PASS 2/4")
    # base=1.0 → (1.0 + 0.5) / 2 = 0.75. Substring check is bypassed.
    assert verdict.score == 0.75
    assert verdict.signals["partial_credit_ratio"] == 0.5
    assert verdict.signals["partial_credit_passed"] == 2
    assert verdict.signals["partial_credit_total"] == 4
    assert verdict.signals["partial_credit_test_signal_found"] is True
    assert verdict.signals["substring_present"] is None  # substring path bypassed
    # The boolean expected_substring_present/missing flags are NOT in the
    # flag lists when partial-credit is active.
    assert "expected_substring_present" not in verdict.signals["flags"]
    assert "expected_substring_missing" not in verdict.signals["flags_negative"]


async def test_partial_credit_full_pass_recovers_pass_substring_score():
    """All tests pass → ratio = 1.0; composed score equals the value the
    pass/fail substring path produces on a clean pass."""
    verdict = await _score_with_partial_credit("PASS 4/4")
    # base=1.0 → (1.0 + 1.0) / 2 = 1.0 — same as old substring-present=True.
    assert verdict.score == 1.0
    assert verdict.signals["partial_credit_ratio"] == 1.0
    assert "partial_credit_full" in verdict.signals["flags"]


async def test_partial_credit_zero_pass_recovers_fail_substring_score():
    """No tests pass → ratio = 0.0; composed score equals the value the
    pass/fail substring path produces on a missing substring."""
    verdict = await _score_with_partial_credit("Final: FAIL 0/4")
    # base=1.0 → (1.0 + 0.0) / 2 = 0.5 — same as old substring-present=False.
    assert verdict.score == 0.5
    assert verdict.signals["partial_credit_ratio"] == 0.0
    assert "partial_credit_zero" in verdict.signals["flags_negative"]


async def test_partial_credit_pytest_summary_parses_mixed_result():
    """Pytest summary `1 passed, 3 failed` → 1/4 = 0.25 ratio."""
    verdict = await _score_with_partial_credit(
        "============= 1 passed, 3 failed in 0.45s ============="
    )
    # base=1.0 → (1.0 + 0.25) / 2 = 0.625
    assert verdict.score == 0.625
    assert verdict.signals["partial_credit_ratio"] == 0.25
    assert verdict.signals["partial_credit_passed"] == 1
    assert verdict.signals["partial_credit_total"] == 4


async def test_partial_credit_pytest_summary_with_errors_counts_total():
    """Pytest summary `2 passed, 1 failed, 1 error` → total = 4, ratio 0.5."""
    verdict = await _score_with_partial_credit(
        "============ 2 passed, 1 failed, 1 error in 0.10s ============"
    )
    assert verdict.signals["partial_credit_ratio"] == 0.5
    assert verdict.signals["partial_credit_total"] == 4


async def test_partial_credit_runner_format_takes_priority_over_pytest_pattern():
    """When both `PASS N/M` and pytest summary appear, the runner shape wins
    (it carries the explicit total). Iterative agents that print interim
    pytest lines followed by a runner.py PASS line still grade correctly."""
    verdict = await _score_with_partial_credit(
        "earlier intermediate: 1 passed, 3 failed\nfinal summary: PASS 4/4"
    )
    assert verdict.signals["partial_credit_ratio"] == 1.0
    assert verdict.signals["partial_credit_total"] == 4


async def test_partial_credit_no_test_signal_treated_as_failure():
    """A response with no parseable test output scores 0 from the partial-
    credit path; combined with base=1.0 the composed score is 0.5."""
    verdict = await _score_with_partial_credit(
        "I think the code is correct but I did not run the tests."
    )
    assert verdict.score == 0.5
    assert verdict.signals["partial_credit_test_signal_found"] is False
    assert "partial_credit_no_test_signal" in verdict.signals["flags_negative"]


async def test_partial_credit_last_runner_line_wins():
    """Multiple `PASS N/M` lines (per-case + final summary) → the LAST one
    is the grade."""
    verdict = await _score_with_partial_credit("PASS 1/1\nPASS 1/1\nfinal: FAIL 14/16")
    # 14/16 = 0.875
    assert verdict.signals["partial_credit_passed"] == 14
    assert verdict.signals["partial_credit_total"] == 16
    assert abs(verdict.signals["partial_credit_ratio"] - 0.875) < 1e-9


async def test_partial_credit_stepped_map_rounds_to_quarters():
    """Stepped map rounds the ratio to the nearest 0.25."""
    rubric = WorkloadRubric(
        rubric="heuristic",
        partial_credit=PartialCreditConfig(
            enabled=True, criterion="test_pass_count_ratio", map="stepped"
        ),
    )
    # 3/8 = 0.375 → stepped to 0.5 (nearest quarter).
    verdict = await _score_with_partial_credit("PASS 3/8", rubric=rubric)
    assert verdict.signals["partial_credit_ratio"] == 0.375
    assert verdict.signals["partial_credit_score"] == 0.5
    # base=1.0 → (1.0 + 0.5) / 2 = 0.75
    assert verdict.score == 0.75


async def test_partial_credit_stepped_map_preserves_endpoints():
    """Stepped map at 0/N and N/N preserves 0.0 and 1.0 exactly."""
    rubric = WorkloadRubric(
        rubric="heuristic",
        partial_credit=PartialCreditConfig(
            enabled=True, criterion="test_pass_count_ratio", map="stepped"
        ),
    )
    full = await _score_with_partial_credit("PASS 4/4", rubric=rubric)
    assert full.signals["partial_credit_score"] == 1.0
    zero = await _score_with_partial_credit("FAIL 0/4", rubric=rubric)
    assert zero.signals["partial_credit_score"] == 0.0


async def test_partial_credit_linear_vs_stepped_disagree_in_mid_range():
    """Linear and stepped maps produce different verdicts at 5/8."""
    linear_rubric = WorkloadRubric(
        rubric="heuristic",
        partial_credit=PartialCreditConfig(
            enabled=True, criterion="test_pass_count_ratio", map="linear"
        ),
    )
    stepped_rubric = WorkloadRubric(
        rubric="heuristic",
        partial_credit=PartialCreditConfig(
            enabled=True, criterion="test_pass_count_ratio", map="stepped"
        ),
    )
    # 5/8 = 0.625 → stepped rounds to 0.5 (closer than 0.75).
    linear_v = await _score_with_partial_credit("PASS 5/8", rubric=linear_rubric)
    stepped_v = await _score_with_partial_credit("PASS 5/8", rubric=stepped_rubric)
    assert linear_v.signals["partial_credit_score"] == 0.625
    assert stepped_v.signals["partial_credit_score"] == 0.5


async def test_partial_credit_disabled_falls_back_to_substring_check():
    """When partial_credit.enabled=False, the rubric behaves like the pre-v1.2
    substring path — existing pass/fail rubrics unchanged."""
    rubric = WorkloadRubric(
        rubric="heuristic",
        expect_substring_in_final_response="PASS 4/4",
        partial_credit=PartialCreditConfig(enabled=False),
    )
    pass_v = await _score_with_partial_credit("Final: PASS 4/4", rubric=rubric)
    fail_v = await _score_with_partial_credit("Final: PASS 2/4", rubric=rubric)
    # Boolean check fires: pass = (1.0 + 1.0)/2 = 1.0; fail = 1.0 * 0.5 = 0.5.
    assert pass_v.score == 1.0
    assert fail_v.score == 0.5
    assert pass_v.signals["substring_present"] is True
    assert fail_v.signals["substring_present"] is False


async def test_partial_credit_no_block_is_pre_v1_2_compatible():
    """When no partial_credit block is set, behavior is exactly the pre-v1.2
    workload rubric (existing pass/fail rubrics unchanged)."""
    rubric = WorkloadRubric(rubric="heuristic", expect_substring_in_final_response="PASS 4/4")
    pass_v = await _score_with_partial_credit("Result: PASS 4/4", rubric=rubric)
    fail_v = await _score_with_partial_credit("Result: PASS 2/4", rubric=rubric)
    assert pass_v.score == 1.0
    assert fail_v.score == 0.5
    # Partial-credit signals are absent.
    assert "partial_credit_ratio" not in pass_v.signals


async def test_partial_credit_pytest_skipped_excluded_from_total():
    """Pytest `1 passed, 0 failed, 5 skipped` → total excludes skipped; ratio = 1.0.

    Skipped tests aren't a pass or a fail — counting them in the denominator
    would penalize workloads with conditional skips. The runner's own
    `PASS N/M` line is the authoritative shape for explicit totals; pytest
    summaries fall back to passed+failed+errors only.
    """
    verdict = await _score_with_partial_credit(
        "============= 1 passed, 5 skipped in 0.10s ============="
    )
    assert verdict.signals["partial_credit_ratio"] == 1.0
    assert verdict.signals["partial_credit_passed"] == 1
    assert verdict.signals["partial_credit_total"] == 1


async def test_partial_credit_smoke_against_regex_workload_fixture():
    """Load the regex-with-edge-cases fixture and verify partial-credit
    is wired: a `PASS 12/16` runner line scores 0.75 ratio (vs the prior
    boolean `PASS 16/16` substring check that would score this 0)."""
    import pathlib

    import yaml
    from metis_core.eval.rubric import parse_workload_rubric

    repo_root = pathlib.Path(__file__).resolve().parents[4]
    workload_path = repo_root / "benchmarks/workloads/regex-with-edge-cases/workload.yaml"
    raw = yaml.safe_load(workload_path.read_text())
    rubric = parse_workload_rubric(raw.get("evaluate"))
    assert rubric.partial_credit is not None
    assert rubric.partial_credit.enabled is True
    assert rubric.partial_credit.criterion == "test_pass_count_ratio"
    assert rubric.partial_credit.map == "linear"
    # Workload no longer carries a pass/fail substring assertion — the
    # partial-credit path is the only signal source.
    assert rubric.expect_substring_in_final_response is None

    judge = HeuristicJudge()
    verdict = await judge.evaluate(
        SubjectContext(
            subject_kind="workload",
            subject_id="wl_regex",
            events=[],
            workload_rubric=rubric,
            signals_extra={
                "per_turn_scores": [1.0, 1.0, 1.0],
                "final_response_text": "FAIL 12/16",
                "assertion_failures": [],
                "assertions_checked": True,
                "workload_name": "regex-with-edge-cases",
            },
        )
    )
    assert verdict.signals["partial_credit_ratio"] == 0.75
    # 12/16 surfaces between 0 and 1; the pre-v1.2 substring check would
    # have collapsed this to score=0.5 (substring missing branch).
    assert verdict.score > 0.5
