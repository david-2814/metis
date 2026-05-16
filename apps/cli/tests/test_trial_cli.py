"""Smoke tests for the `metis trial` CLI subcommand wiring.

End-to-end run is exercised manually against the real Anthropic API
(see docs/operations/quickstart.md "Pitfalls"). These tests confirm
the parser shape and the workload-discovery error path so a regression
to either is caught before live-API time.
"""

from __future__ import annotations

import pytest
from metis_cli.main import build_parser
from metis_cli.trial import (
    DEFAULT_BASELINE_MODEL,
    DEFAULT_TRIAL_MODEL,
    DEFAULT_TRIAL_WORKLOAD,
    _discover_trial_workload,
    run_trial_command,
)


def test_trial_subcommand_defaults():
    parser = build_parser()
    args = parser.parse_args(["trial"])
    assert args.command == "trial"
    assert args.workload == DEFAULT_TRIAL_WORKLOAD
    assert args.model == DEFAULT_TRIAL_MODEL
    assert args.baseline == DEFAULT_BASELINE_MODEL
    assert args.gateway_url is None
    assert args.gateway_key is None
    assert args.db_path is None


def test_trial_subcommand_accepts_gateway_flags():
    parser = build_parser()
    args = parser.parse_args(
        [
            "trial",
            "--workload",
            "refactor-extract-helper",
            "--gateway-url",
            "http://127.0.0.1:8422",
            "--gateway-key",
            "gw_xyz",
            "--db-path",
            "/tmp/foo.db",
        ]
    )
    assert args.gateway_url == "http://127.0.0.1:8422"
    assert args.gateway_key == "gw_xyz"
    assert args.db_path == "/tmp/foo.db"


def test_trial_subcommand_accepts_baseline_alias():
    parser = build_parser()
    args = parser.parse_args(["trial", "--baseline", "anthropic:claude-opus-4-7"])
    assert args.baseline == "anthropic:claude-opus-4-7"


def test_discover_trial_workload_finds_default():
    """The default trial workload ships in-tree."""
    yaml_path = _discover_trial_workload(DEFAULT_TRIAL_WORKLOAD)
    assert yaml_path.is_file()
    assert (yaml_path.parent / "workspace").is_dir()


def test_default_trial_workload_parses_with_benchmark_loader():
    """The trial workload obeys benchmark.md §3.1 — same schema as the
    project benchmark suite, so tooling can swap between them."""
    import benchmark  # scripts/benchmark.py is on sys.path via the workspace-root conftest

    yaml_path = _discover_trial_workload(DEFAULT_TRIAL_WORKLOAD)
    workload = benchmark.load_workload(yaml_path)
    assert workload.name == DEFAULT_TRIAL_WORKLOAD
    assert workload.suite_version == 1
    assert workload.turns, "trial workload must have at least one turn"
    # Trial-specific contract: <2 minutes, <$0.10. The cost ceiling is
    # the assertion that catches drift on either constraint.
    assert workload.expect.get("max_total_cost_usd", 1.0) <= 0.10
    # Hybrid evaluator with grounding tokens — see workloads-trial/README.md.
    assert workload.evaluate.rubric == "hybrid"
    assert workload.evaluate.grounding_tokens, "trial workload must populate grounding_tokens"


def test_discover_trial_workload_rejects_unknown():
    with pytest.raises(FileNotFoundError) as exc:
        _discover_trial_workload("does-not-exist")
    msg = str(exc.value)
    assert "does-not-exist" in msg
    assert "Available" in msg


def test_run_trial_command_rejects_partial_gateway_args(capsys):
    """Both gateway flags must be passed together (else env-var injection
    is half-done — unsafe)."""
    rc = run_trial_command(
        workload=DEFAULT_TRIAL_WORKLOAD,
        model=DEFAULT_TRIAL_MODEL,
        baseline=DEFAULT_BASELINE_MODEL,
        db_path=None,
        gateway_url="http://127.0.0.1:8422",
        gateway_key=None,
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "gateway" in err.lower()


def test_run_trial_command_rejects_unknown_workload(capsys):
    rc = run_trial_command(
        workload="this-workload-does-not-exist",
        model=DEFAULT_TRIAL_MODEL,
        baseline=DEFAULT_BASELINE_MODEL,
        db_path=None,
        gateway_url=None,
        gateway_key=None,
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "this-workload-does-not-exist" in err
