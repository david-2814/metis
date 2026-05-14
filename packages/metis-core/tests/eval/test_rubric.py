"""Workload rubric parsing tests."""

from __future__ import annotations

import pytest
from metis_core.eval import WorkloadRubric, WorkloadRubricError, parse_workload_rubric


def test_parse_workload_rubric_defaults_when_absent():
    rubric = parse_workload_rubric(None)
    assert rubric == WorkloadRubric()


def test_parse_workload_rubric_accepts_full_block():
    rubric = parse_workload_rubric(
        {
            "rubric": "heuristic",
            "expect_substring_in_final_response": "hello",
            "llm_judge_model": "anthropic:claude-haiku-4-5",
            "weight_per_turn": 1.5,
        }
    )
    assert rubric.rubric == "heuristic"
    assert rubric.expect_substring_in_final_response == "hello"
    assert rubric.llm_judge_model == "anthropic:claude-haiku-4-5"
    assert rubric.weight_per_turn == 1.5


def test_parse_workload_rubric_rejects_unknown_keys():
    with pytest.raises(WorkloadRubricError, match="unknown evaluate keys"):
        parse_workload_rubric({"rubric": "heuristic", "foo": "bar"})


def test_parse_workload_rubric_rejects_bad_rubric_kind():
    with pytest.raises(WorkloadRubricError, match="must be one of heuristic"):
        parse_workload_rubric({"rubric": "magic"})


def test_parse_workload_rubric_rejects_negative_weight():
    with pytest.raises(WorkloadRubricError, match="non-negative"):
        parse_workload_rubric({"weight_per_turn": -0.5})


def test_parse_workload_rubric_rejects_non_string_substring():
    with pytest.raises(WorkloadRubricError, match="must be a string"):
        parse_workload_rubric({"expect_substring_in_final_response": 42})
