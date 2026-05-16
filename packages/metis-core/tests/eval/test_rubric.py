"""Workload rubric parsing tests."""

from __future__ import annotations

import pytest
from metis_core.eval import (
    PartialCreditConfig,
    WorkloadRubric,
    WorkloadRubricError,
    parse_workload_rubric,
)


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


def test_parse_workload_rubric_partial_credit_defaults_to_none():
    rubric = parse_workload_rubric({"rubric": "heuristic"})
    assert rubric.partial_credit is None


def test_parse_workload_rubric_accepts_partial_credit_block():
    rubric = parse_workload_rubric(
        {
            "rubric": "heuristic",
            "partial_credit": {
                "enabled": True,
                "criterion": "test_pass_count_ratio",
                "map": "linear",
            },
        }
    )
    assert rubric.partial_credit == PartialCreditConfig(
        enabled=True, criterion="test_pass_count_ratio", map="linear"
    )


def test_parse_workload_rubric_accepts_stepped_map():
    rubric = parse_workload_rubric({"partial_credit": {"enabled": True, "map": "stepped"}})
    assert rubric.partial_credit is not None
    assert rubric.partial_credit.map == "stepped"
    assert rubric.partial_credit.criterion == "test_pass_count_ratio"


def test_parse_workload_rubric_partial_credit_defaults_enabled_false():
    rubric = parse_workload_rubric({"partial_credit": {}})
    assert rubric.partial_credit is not None
    assert rubric.partial_credit.enabled is False


def test_parse_workload_rubric_rejects_unknown_partial_credit_keys():
    with pytest.raises(WorkloadRubricError, match="unknown partial_credit keys"):
        parse_workload_rubric({"partial_credit": {"enabled": True, "foo": "bar"}})


def test_parse_workload_rubric_rejects_non_bool_partial_credit_enabled():
    with pytest.raises(WorkloadRubricError, match="enabled must be a boolean"):
        parse_workload_rubric({"partial_credit": {"enabled": "yes"}})


def test_parse_workload_rubric_rejects_unknown_partial_credit_criterion():
    with pytest.raises(WorkloadRubricError, match="criterion must be one of"):
        parse_workload_rubric({"partial_credit": {"criterion": "magic_ratio"}})


def test_parse_workload_rubric_rejects_unknown_partial_credit_map():
    with pytest.raises(WorkloadRubricError, match="map must be one of"):
        parse_workload_rubric({"partial_credit": {"map": "exponential"}})


def test_parse_workload_rubric_rejects_non_mapping_partial_credit():
    with pytest.raises(WorkloadRubricError, match="partial_credit must be a mapping"):
        parse_workload_rubric({"partial_credit": "enabled"})
