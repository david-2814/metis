"""Schema-loader tests for `scripts/benchmark.py`.

The script's analytics-projection path is covered by metis-core's
analytics tests; the path that's specific to this script (workload YAML
parsing) is what we exercise here.
"""

from __future__ import annotations

from pathlib import Path

import benchmark  # scripts/benchmark.py is on sys.path via the workspace-root conftest
import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKLOADS_DIR = REPO_ROOT / "benchmarks" / "workloads"


def test_shipped_workloads_load_clean():
    """Every workload under benchmarks/workloads/ parses without error.

    Catches a regression in the YAML or the schema loader.
    """
    workloads = benchmark.discover_workloads()
    names = {w.name for w in workloads}
    assert names == {"fix-a-bug-small", "write-a-doc-from-notes", "multi-turn-refactor"}
    for w in workloads:
        assert w.suite_version == 1
        assert w.turns, f"{w.name}: must have at least one turn"
        for t in w.turns:
            assert t.prompt, f"{w.name}: turn prompt cannot be empty"


def test_rejects_unknown_top_level_key(tmp_path: Path):
    yaml_path = tmp_path / "workload.yaml"
    yaml_path.write_text(
        "name: x\ndescription: x\nsuite_version: 1\nturns:\n  - prompt: hi\nbogus: 1\n"
    )
    with pytest.raises(benchmark.WorkloadSchemaError, match="bogus"):
        benchmark.load_workload(yaml_path)


def test_rejects_unknown_turn_expect_key(tmp_path: Path):
    yaml_path = tmp_path / "workload.yaml"
    yaml_path.write_text(
        "name: x\n"
        "description: x\n"
        "suite_version: 1\n"
        "turns:\n"
        "  - prompt: hi\n"
        "    expect:\n"
        "      mystery_field: 1\n"
    )
    with pytest.raises(benchmark.WorkloadSchemaError, match="mystery_field"):
        benchmark.load_workload(yaml_path)


def test_rejects_unknown_aggregate_expect_key(tmp_path: Path):
    yaml_path = tmp_path / "workload.yaml"
    yaml_path.write_text(
        "name: x\n"
        "description: x\n"
        "suite_version: 1\n"
        "turns:\n"
        "  - prompt: hi\n"
        "expect:\n"
        "  not_a_real_key: 1\n"
    )
    with pytest.raises(benchmark.WorkloadSchemaError, match="not_a_real_key"):
        benchmark.load_workload(yaml_path)


def test_rejects_unsupported_suite_version(tmp_path: Path):
    yaml_path = tmp_path / "workload.yaml"
    yaml_path.write_text("name: x\ndescription: x\nsuite_version: 99\nturns:\n  - prompt: hi\n")
    with pytest.raises(benchmark.WorkloadSchemaError, match="suite_version"):
        benchmark.load_workload(yaml_path)


def test_rejects_empty_turns(tmp_path: Path):
    yaml_path = tmp_path / "workload.yaml"
    yaml_path.write_text("name: x\ndescription: x\nsuite_version: 1\nturns: []\n")
    with pytest.raises(benchmark.WorkloadSchemaError, match="non-empty"):
        benchmark.load_workload(yaml_path)


def test_accepts_evaluate_block(tmp_path: Path):
    yaml_path = tmp_path / "workload.yaml"
    yaml_path.write_text(
        "name: x\n"
        "description: x\n"
        "suite_version: 1\n"
        "turns:\n"
        "  - prompt: hi\n"
        "evaluate:\n"
        "  rubric: heuristic\n"
        "  expect_substring_in_final_response: off-by-one\n"
        "  weight_per_turn: 2.0\n"
    )
    workload = benchmark.load_workload(yaml_path)
    assert workload.evaluate.rubric == "heuristic"
    assert workload.evaluate.expect_substring_in_final_response == "off-by-one"
    assert workload.evaluate.weight_per_turn == 2.0


def test_rejects_unknown_evaluate_key(tmp_path: Path):
    yaml_path = tmp_path / "workload.yaml"
    yaml_path.write_text(
        "name: x\n"
        "description: x\n"
        "suite_version: 1\n"
        "turns:\n"
        "  - prompt: hi\n"
        "evaluate:\n"
        "  rubric: heuristic\n"
        "  not_a_real_key: 1\n"
    )
    with pytest.raises(benchmark.WorkloadSchemaError, match="not_a_real_key"):
        benchmark.load_workload(yaml_path)


def test_evaluate_block_defaults_when_absent(tmp_path: Path):
    yaml_path = tmp_path / "workload.yaml"
    yaml_path.write_text("name: x\ndescription: x\nsuite_version: 1\nturns:\n  - prompt: hi\n")
    workload = benchmark.load_workload(yaml_path)
    assert workload.evaluate.rubric == "heuristic"
    assert workload.evaluate.expect_substring_in_final_response is None
    assert workload.evaluate.weight_per_turn == 1.0
