"""Schema-loader tests for `scripts/benchmark.py`.

The script's analytics-projection path is covered by metis-core's
analytics tests; the path that's specific to this script (workload YAML
parsing) is what we exercise here.
"""

from __future__ import annotations

from pathlib import Path

import benchmark  # scripts/benchmark.py is on sys.path via the workspace-root conftest
import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
WORKLOADS_DIR = REPO_ROOT / "benchmarks" / "workloads"


def test_shipped_workloads_load_clean():
    """Every workload under benchmarks/workloads/ parses without error.

    Catches a regression in the YAML or the schema loader.
    """
    # Use include_marginal so we exercise every YAML on disk (post 13a-1
    # the default suite is empty by design — see benchmark.md §4.1).
    workloads = benchmark.discover_workloads(include_marginal=True)
    names = {w.name for w in workloads}
    assert names == {
        "architectural-explanation-without-hallucination",
        "fix-a-bug-small",
        "intentionally-failing-task",
        "multi-file-refactor-with-shared-types",
        "multi-step-with-delegation",
        "multi-turn-refactor",
        "recursive-data-structure-traversal",
        "refactor-with-contract-preservation",
        "regex-with-edge-cases",
        "subtle-bug-fix-with-test",
        "write-a-doc-from-notes",
    }
    for w in workloads:
        assert w.suite_version == 1
        assert w.signal_strength in {"high", "marginal"}, (
            f"{w.name}: unexpected signal_strength {w.signal_strength!r}"
        )
        assert w.turns, f"{w.name}: must have at least one turn"
        for t in w.turns:
            assert t.prompt, f"{w.name}: turn prompt cannot be empty"


def test_signal_strength_default_high(tmp_path: Path):
    """An unannotated workload defaults to signal_strength='high' (the spec
    default, so adding the field doesn't break existing YAMLs)."""
    yaml_path = tmp_path / "workload.yaml"
    yaml_path.write_text("name: x\ndescription: x\nsuite_version: 1\nturns:\n  - prompt: hi\n")
    workload = benchmark.load_workload(yaml_path)
    assert workload.signal_strength == "high"


def test_signal_strength_explicit_marginal(tmp_path: Path):
    yaml_path = tmp_path / "workload.yaml"
    yaml_path.write_text(
        "name: x\ndescription: x\nsuite_version: 1\n"
        "signal_strength: marginal\n"
        "turns:\n  - prompt: hi\n"
    )
    workload = benchmark.load_workload(yaml_path)
    assert workload.signal_strength == "marginal"


def test_signal_strength_rejects_unknown_value(tmp_path: Path):
    yaml_path = tmp_path / "workload.yaml"
    yaml_path.write_text(
        "name: x\ndescription: x\nsuite_version: 1\n"
        "signal_strength: bogus\n"
        "turns:\n  - prompt: hi\n"
    )
    with pytest.raises(benchmark.WorkloadSchemaError, match="signal_strength"):
        benchmark.load_workload(yaml_path)


def test_discover_workloads_filters_marginal_by_default():
    """`discover_workloads()` without include_marginal returns only
    `signal_strength=high` workloads. Post 13a-1 the shipped suite is
    all marginal — assert the default returns an empty list (the
    harness handles this with a helpful message)."""
    default = benchmark.discover_workloads()
    high = {w.name for w in default if w.signal_strength == "high"}
    marginal = {w.name for w in default if w.signal_strength == "marginal"}
    assert marginal == set(), "discover_workloads() default must not include marginal workloads"
    # Equality with `high` confirms the filter passes everything that IS
    # high (it just happens to be empty today).
    assert {w.name for w in default} == high


def test_discover_workloads_include_marginal_returns_everything():
    everything = benchmark.discover_workloads(include_marginal=True)
    high_only = benchmark.discover_workloads(include_marginal=False)
    assert {w.name for w in everything} >= {w.name for w in high_only}
    # And the difference is exactly the marginal-tagged workloads on disk.
    diff = {w.name for w in everything} - {w.name for w in high_only}
    for w in everything:
        if w.name in diff:
            assert w.signal_strength == "marginal"


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


# ---------------------------------------------------------------------------
# `--seed-passes` statistical reporting (benchmark.md §6.4)
# ---------------------------------------------------------------------------


def _result(name: str, quality: float | None, cost: float = 0.01, rep: int = 0):
    return benchmark.WorkloadResult(
        name=name,
        started_us=0,
        ended_us=0,
        turns=1,
        llm_calls=1,
        tool_calls=0,
        actual_repriced_usd=cost,
        baseline_repriced_usd=cost * 3,
        savings_usd=cost * 2,
        savings_pct=0.66,
        actual_stamped_usd=cost,
        rows_total=1,
        rows_missing_from_price_table=0,
        quality_score=quality,
        quality_confidence=0.9,
        seed_pass_index=rep,
    )


def test_compute_workload_stats_n_equals_one_has_no_std():
    """A single sample has a defined mean but no sample-stdev."""
    results = [_result("solo", 0.85, rep=0)]
    stats = benchmark.compute_workload_stats(results)
    assert len(stats) == 1
    s = stats[0]
    assert s.name == "solo"
    assert s.samples == 1
    assert s.quality_mean == pytest.approx(0.85)
    assert s.quality_std is None
    assert s.noisy is False


def test_compute_workload_stats_computes_mean_and_std_for_n_samples():
    """N=3 samples with low variance produce a tight mean ± std."""
    results = [
        _result("steady", 0.80, rep=0),
        _result("steady", 0.85, rep=1),
        _result("steady", 0.83, rep=2),
    ]
    stats = benchmark.compute_workload_stats(results)
    s = next(s for s in stats if s.name == "steady")
    assert s.samples == 3
    assert s.quality_mean == pytest.approx((0.80 + 0.85 + 0.83) / 3)
    # Sample stdev (N-1); should sit ~0.025.
    assert s.quality_std is not None and 0.0 < s.quality_std < 0.05
    assert s.noisy is False


def test_compute_workload_stats_flags_noisy_above_threshold():
    """Std > 0.15 → workload flagged for replacement (signal-strength gate)."""
    # Spread 0.10 → 0.50 → 0.90 has stdev ≈ 0.40 (well above 0.15).
    results = [
        _result("flaky", 0.10, rep=0),
        _result("flaky", 0.50, rep=1),
        _result("flaky", 0.90, rep=2),
    ]
    stats = benchmark.compute_workload_stats(results)
    s = next(s for s in stats if s.name == "flaky")
    assert s.quality_std is not None and s.quality_std > benchmark.NOISY_QUALITY_STD_THRESHOLD
    assert s.noisy is True


def test_compute_workload_stats_groups_by_name():
    """Reps from two different workloads aggregate independently."""
    results = [
        _result("a", 0.9, rep=0),
        _result("b", 0.5, rep=0),
        _result("a", 0.8, rep=1),
        _result("b", 0.6, rep=1),
    ]
    stats = {s.name: s for s in benchmark.compute_workload_stats(results)}
    assert set(stats) == {"a", "b"}
    assert stats["a"].samples == 2
    assert stats["a"].quality_mean == pytest.approx(0.85)
    assert stats["b"].quality_mean == pytest.approx(0.55)


def test_compute_workload_stats_skips_errored_reps():
    """Reps where `error` is set are excluded from the sample population."""
    good = _result("mixed", 0.9, rep=0)
    bad = benchmark.WorkloadResult(
        name="mixed",
        started_us=0,
        ended_us=0,
        turns=0,
        llm_calls=0,
        tool_calls=0,
        actual_repriced_usd=0.0,
        baseline_repriced_usd=0.0,
        savings_usd=0.0,
        savings_pct=0.0,
        actual_stamped_usd=0.0,
        rows_total=0,
        rows_missing_from_price_table=0,
        seed_pass_index=1,
        error="exploded",
    )
    stats = benchmark.compute_workload_stats([good, bad])
    # The errored rep was filtered before grouping, so only one sample remains.
    assert len(stats) == 1
    assert stats[0].samples == 1
    assert stats[0].quality_mean == pytest.approx(0.9)


def test_compute_workload_stats_handles_missing_quality_scores():
    """Reps without a quality verdict still count for sample size but not stats."""
    results = [
        _result("nq", None, rep=0),
        _result("nq", None, rep=1),
    ]
    stats = benchmark.compute_workload_stats(results)
    s = stats[0]
    assert s.samples == 2
    assert s.quality_mean is None
    assert s.quality_std is None
    assert s.noisy is False


# ---------------------------------------------------------------------------
# Seed-passes pattern-store accumulation (the §A3-rev6 fix)
# ---------------------------------------------------------------------------


def test_seed_passes_accumulates_samples_in_shared_patterns_db(tmp_path: Path):
    """Simulate the harness's `--seed-passes N` loop: for the same (workload,
    model) we expect N PatternStore.record() calls to land in one shared
    patterns DB with `sample_size = N` for that fingerprint.

    This is the contract the harness relies on. The shared-patterns-db loop
    is just `seed_path = shared_patterns_db if exists; run_workload writes
    record(); save back`. Accumulation lives in `PatternStore.record()`'s
    upsert, which this test exercises with the same deterministic
    fingerprint across N reps.
    """
    from decimal import Decimal

    from metis.core.patterns.fingerprint import FingerprintInputs, compute_fingerprint
    from metis.core.patterns.store import PatternStore

    # One stable fingerprint stands in for "the same workload, same turn".
    inputs = FingerprintInputs(
        user_message_text="fix the off-by-one bug",
        workspace_path="/tmp/ws",
        estimated_input_tokens=2_000,
        has_images=False,
        has_tool_calls_in_history=False,
        file_extensions=(".py",),
        file_path_buckets=("src",),
        tool_names=("read_file",),
        side_effect_classes=("read",),
    )
    fp = compute_fingerprint(inputs)

    N = 3
    # Each rep opens-closes the store and records once. Mimics N seed-passes
    # against a stable prompt, with the patterns DB persisting between reps.
    for rep in range(N):
        store = PatternStore(tmp_path)
        try:
            result = store.record(
                fingerprint=fp,
                primary_model="anthropic:haiku",
                success_score=0.8 + 0.01 * rep,
                cost_usd=Decimal("0.0100"),
                latency_ms=1000.0,
                pricing_version="v1",
            )
            assert result.sample_size_after == rep + 1
        finally:
            store.close()

    # K-NN reads back exactly N samples for the (fingerprint, model) row.
    final = PatternStore(tmp_path)
    try:
        rec = final.recommend(fp, cost_weight=0.05, min_confidence=0.0, min_sample_size=1)
        assert rec.chosen_model == "anthropic:haiku"
        assert rec.sample_size == N
    finally:
        final.close()


@pytest.mark.asyncio
async def test_seed_passes_loop_invokes_run_workload_n_times(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """End-to-end: `amain --seed-passes 3 --workload x` calls `run_workload`
    3 times against the same patterns-db-path, with a fresh session_id per rep,
    and the K-NN sees `N` accumulated samples for the (fingerprint, model).

    Mocks out the heavy ChatRuntime / AnalyticsStore / evaluator. The
    fake `run_workload` performs a real PatternStore round-trip into the
    shared db so the K-NN recommendation read at the end is honest.

    If the harness loop ever stops calling `run_workload` N times per
    workload — or stops threading the shared patterns DB through reps —
    this test fails. That's the §A3-rev6-path-2 invariant.
    """
    import os
    import sqlite3
    from decimal import Decimal

    from metis.core.patterns.fingerprint import FingerprintInputs, compute_fingerprint
    from metis.core.patterns.store import PatternStore

    # Stand up a fake workloads directory with a single workload.
    workloads_dir = tmp_path / "workloads"
    wl_dir = workloads_dir / "fake-workload"
    (wl_dir / "workspace").mkdir(parents=True)
    (wl_dir / "workload.yaml").write_text(
        "name: fake-workload\ndescription: synthetic\nsuite_version: 1\nturns:\n  - prompt: hello\n"
    )
    monkeypatch.setattr(benchmark, "WORKLOADS_DIR", workloads_dir)

    shared_db = tmp_path / "patterns.db"

    class _FakeRegistry:
        def resolve_alias(self, name):
            return None

        def __contains__(self, key):
            return True

        def provider_of(self, _):
            return "anthropic"

    class _FakePricing:
        version = "2026-05-15"

        def __contains__(self, key):
            return True

    class _FakeRuntime:
        registry = _FakeRegistry()
        pricing = _FakePricing()

    async def _fake_setup_runtime(**_kwargs):
        return _FakeRuntime()

    async def _fake_shutdown_runtime(_runtime):
        return None

    monkeypatch.setattr(benchmark, "setup_runtime", _fake_setup_runtime, raising=False)
    monkeypatch.setattr(benchmark, "shutdown_runtime", _fake_shutdown_runtime, raising=False)

    # `run_workload` stub: copy seed → temp workspace, record one outcome,
    # copy patterns DB back to the shared location. Uses a STABLE workspace
    # path across reps so the structural signature collapses to one row —
    # mirrors the "did the K-NN see N samples?" question directly.
    call_count = {"n": 0}
    next_ts = {"us": 1_700_000_000_000_000}
    seed_path_history: list[Path | None] = []
    save_path_history: list[Path | None] = []
    fake_workspace = tmp_path / "fake-ws"
    fake_workspace.mkdir()

    async def _fake_run_workload(workload, *, pattern_seed_path, pattern_save_path, **_kw):
        seed_path_history.append(pattern_seed_path)
        save_path_history.append(pattern_save_path)
        # Stable workspace path across reps so every record() lands on the
        # same structural signature and the K-NN sees one outcome row with
        # sample_size = N after N reps.
        ws_root = fake_workspace
        metis_dir = ws_root / ".metis"
        metis_dir.mkdir(exist_ok=True)
        target_db = metis_dir / "patterns.db"
        if pattern_seed_path is not None and pattern_seed_path.is_file():
            target_db.write_bytes(pattern_seed_path.read_bytes())
        elif target_db.exists():
            target_db.unlink()

        store = PatternStore(ws_root)
        try:
            fp = compute_fingerprint(
                FingerprintInputs(
                    user_message_text="hello",
                    workspace_path=str(ws_root),
                    estimated_input_tokens=200,
                    has_images=False,
                    has_tool_calls_in_history=False,
                    file_extensions=(),
                    file_path_buckets=(),
                    tool_names=(),
                    side_effect_classes=(),
                )
            )
            store.record(
                fingerprint=fp,
                primary_model="anthropic:claude-haiku-4-5",
                success_score=0.9,
                cost_usd=Decimal("0.001"),
                latency_ms=10.0,
                pricing_version="2026-05-15",
            )
        finally:
            store.close()
        # WAL checkpoint via a separate connection so the copy is durable —
        # matches what `run_workload` does before copying the file out.
        co = sqlite3.connect(str(target_db))
        try:
            co.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            co.close()
        if pattern_save_path is not None:
            pattern_save_path.write_bytes(target_db.read_bytes())

        call_count["n"] += 1
        start = next_ts["us"]
        next_ts["us"] += 1_000_000
        end = next_ts["us"]
        session_id = f"sess-{call_count['n']}"
        per_turn = [
            {
                "tool_calls": 0,
                "llm_calls": 1,
                "assistant_text": "ok",
                "stop_reason": "end_turn",
                "cost_usd": "0.001",
            }
        ]
        return start, end, per_turn, Decimal("0.001"), 1, 0, session_id

    monkeypatch.setattr(benchmark, "run_workload", _fake_run_workload, raising=False)

    async def _fake_evaluate(*_args, **_kwargs):
        return 0.9, 0.8

    monkeypatch.setattr(benchmark, "evaluate_workload_quality", _fake_evaluate, raising=False)

    def _fake_aggregate_savings(_store, _window, _baseline, _pricing):
        return {
            "actual_repriced_usd": 0.001,
            "baseline_repriced_usd": 0.003,
            "savings_usd": 0.002,
            "savings_pct": 0.66,
            "actual_stamped_usd": 0.001,
            "rows_total": 1,
            "rows_missing_from_price_table": 0,
        }

    monkeypatch.setattr(benchmark, "_aggregate_savings", _fake_aggregate_savings)

    # amain refuses an existing trace DB; let it create one via sqlite3.connect.
    db_path = tmp_path / "trace.db"

    os.environ["ANTHROPIC_API_KEY"] = "test-key"

    argv = [
        "benchmark.py",
        "--workload",
        "fake-workload",
        "--seed-passes",
        "3",
        "--db-path",
        str(db_path),
        "--patterns-db-path",
        str(shared_db),
    ]
    monkeypatch.setattr("sys.argv", argv)

    rc = await benchmark.amain()
    # The per-rep stubs don't drive real LLM activity, so soft-assertion gates
    # against per-turn metrics may flag — tolerate exit 1 alongside exit 0.
    assert rc in (0, 1)
    assert call_count["n"] == 3, f"expected 3 run_workload calls, got {call_count['n']}"

    # Rep 0 sees no seed (file didn't exist); reps 1..N-1 inherit the shared db.
    assert seed_path_history[0] is None or not seed_path_history[0].is_file()
    for sp in seed_path_history[1:]:
        assert sp is not None and sp == shared_db
    assert all(sp == shared_db for sp in save_path_history)

    # The K-NN reads N=3 accumulated samples for the (fingerprint, haiku)
    # row in the shared patterns DB. If this assertion ever fails, the
    # recording-path accumulation is broken — the §A3-rev6 path-2 unblock
    # depends on this invariant holding.
    inspect_ws = tmp_path / "inspect"
    inspect_ws.mkdir()
    (inspect_ws / ".metis").mkdir()
    (inspect_ws / ".metis" / "patterns.db").write_bytes(shared_db.read_bytes())
    inspect = PatternStore(inspect_ws)
    try:
        fp = compute_fingerprint(
            FingerprintInputs(
                user_message_text="hello",
                workspace_path=str(fake_workspace),  # match the recorder
                estimated_input_tokens=200,
                has_images=False,
                has_tool_calls_in_history=False,
                file_extensions=(),
                file_path_buckets=(),
                tool_names=(),
                side_effect_classes=(),
            )
        )
        rec = inspect.recommend(fp, cost_weight=0.05, min_confidence=0.0, min_sample_size=1)
        assert rec.chosen_model == "anthropic:claude-haiku-4-5"
        assert rec.sample_size == 3, (
            f"K-NN should see N=3 accumulated samples; got {rec.sample_size}. "
            "If this fails, the harness loop dropped a record() or the "
            "shared-patterns-db round-trip isn't accumulating."
        )
    finally:
        inspect.close()
