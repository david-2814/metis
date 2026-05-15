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


def test_parse_workload_rubric_accepts_grounding_lists():
    rubric = parse_workload_rubric(
        {
            "rubric": "hybrid",
            "grounding_tokens": ["RoutingEngine", "policy="],
            "forbidden_grounding": ["PATTERN_LOOKUP", "RouterChain"],
        }
    )
    assert rubric.grounding_tokens == ("RoutingEngine", "policy=")
    assert rubric.forbidden_grounding == ("PATTERN_LOOKUP", "RouterChain")


def test_parse_workload_rubric_grounding_defaults_to_empty():
    rubric = parse_workload_rubric({"rubric": "heuristic"})
    assert rubric.grounding_tokens == ()
    assert rubric.forbidden_grounding == ()


def test_parse_workload_rubric_rejects_non_string_grounding_token():
    with pytest.raises(WorkloadRubricError, match="grounding_tokens must be a list"):
        parse_workload_rubric({"grounding_tokens": ["ok", 42]})


def test_parse_workload_rubric_rejects_empty_grounding_token():
    with pytest.raises(WorkloadRubricError, match="grounding_tokens must be a list"):
        parse_workload_rubric({"grounding_tokens": ["ok", ""]})


def test_parse_workload_rubric_rejects_non_list_forbidden_grounding():
    with pytest.raises(WorkloadRubricError, match="forbidden_grounding must be a list"):
        parse_workload_rubric({"forbidden_grounding": "PATTERN_LOOKUP"})
