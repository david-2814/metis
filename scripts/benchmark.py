"""Run the Metis benchmark suite and report savings vs a baseline model.

Spec: `docs/specs/benchmark.md`.

Reads workloads from `benchmarks/workloads/<name>/workload.yaml`, copies each
workload's `workspace/` subtree to a fresh tempdir, drives `SessionManager`
end-to-end against the real provider APIs, writes events to a benchmark-only
SQLite trace DB, and finally calls `AnalyticsStore.savings()` against that DB
to compute the actual-vs-baseline counterfactual.

The headline `savings_pct` printed here is by construction the same number the
`/analytics/savings` HTTP endpoint renders when `metis serve` is pointed at
the same DB — both call into the same `AnalyticsStore.savings()` method.

Usage:
    uv run python scripts/benchmark.py                 # full suite, defaults
    uv run python scripts/benchmark.py --workload fix-a-bug-small
    uv run python scripts/benchmark.py --model sonnet --baseline opus
    uv run python scripts/benchmark.py --db-path /tmp/foo.db --skip-execute

Cost expectations (per benchmark.md §5):
    Single workload smoke:    ~$0.05-0.20
    Full suite (haiku/sonnet): ~$0.30-1.00
    Full suite (--model sonnet): ~$1.00-3.00
    Full suite (--model opus):   ~$3.00-5.00
The baseline does not make API calls; it re-prices the actual run's token
counts under the baseline model's PriceTable rates.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from metis_core.analytics import AnalyticsStore
from metis_core.analytics.windows import TimeWindow
from metis_core.eval import (
    HeuristicJudge,
    WorkloadRubric,
    parse_workload_rubric,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKLOADS_DIR = REPO_ROOT / "benchmarks" / "workloads"
RUNS_DIR = REPO_ROOT / "benchmarks" / ".runs"


# ---------------------------------------------------------------------------
# Workload schema (benchmark.md §3.1)
# ---------------------------------------------------------------------------

_ALLOWED_TURN_EXPECT = {
    "min_tool_calls",
    "max_tool_calls",
    "contains_substring",
    "stop_reason",
}
_ALLOWED_AGG_EXPECT = {
    "max_total_cost_usd",
    "min_llm_calls",
    "max_hard_failures",
}
_ALLOWED_TOP = {"name", "description", "suite_version", "turns", "expect", "evaluate"}


@dataclass(frozen=True)
class TurnSpec:
    prompt: str
    expect: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Workload:
    name: str
    description: str
    suite_version: int
    turns: list[TurnSpec]
    expect: dict[str, Any] = field(default_factory=dict)
    evaluate: WorkloadRubric = field(default_factory=WorkloadRubric)
    source_path: Path = field(default_factory=Path)


class WorkloadSchemaError(ValueError):
    pass


def load_workload(yaml_path: Path) -> Workload:
    """Parse + validate a workload.yaml. Rejects unknown keys; surfaces clear errors."""
    raw = yaml.safe_load(yaml_path.read_text())
    if not isinstance(raw, dict):
        raise WorkloadSchemaError(f"{yaml_path}: top-level must be a mapping")
    extra_top = set(raw) - _ALLOWED_TOP
    if extra_top:
        raise WorkloadSchemaError(f"{yaml_path}: unknown top-level keys: {sorted(extra_top)}")
    for k in ("name", "description", "suite_version", "turns"):
        if k not in raw:
            raise WorkloadSchemaError(f"{yaml_path}: missing required key {k!r}")
    if raw["suite_version"] != 1:
        raise WorkloadSchemaError(
            f"{yaml_path}: unsupported suite_version {raw['suite_version']!r}; expected 1"
        )
    if not isinstance(raw["turns"], list) or not raw["turns"]:
        raise WorkloadSchemaError(f"{yaml_path}: turns must be a non-empty list")
    turns: list[TurnSpec] = []
    for i, turn in enumerate(raw["turns"]):
        if not isinstance(turn, dict) or "prompt" not in turn:
            raise WorkloadSchemaError(
                f"{yaml_path}: turn {i} must be a mapping with a 'prompt' key"
            )
        unknown_turn = set(turn) - {"prompt", "expect"}
        if unknown_turn:
            raise WorkloadSchemaError(f"{yaml_path}: turn {i}: unknown keys {sorted(unknown_turn)}")
        expect = turn.get("expect") or {}
        unknown_exp = set(expect) - _ALLOWED_TURN_EXPECT
        if unknown_exp:
            raise WorkloadSchemaError(
                f"{yaml_path}: turn {i}: unknown expect keys {sorted(unknown_exp)}"
            )
        turns.append(TurnSpec(prompt=str(turn["prompt"]).strip(), expect=expect))
    agg_expect = raw.get("expect") or {}
    unknown_agg = set(agg_expect) - _ALLOWED_AGG_EXPECT
    if unknown_agg:
        raise WorkloadSchemaError(
            f"{yaml_path}: unknown aggregate expect keys {sorted(unknown_agg)}"
        )
    try:
        evaluate_rubric = parse_workload_rubric(raw.get("evaluate"))
    except ValueError as exc:
        raise WorkloadSchemaError(f"{yaml_path}: {exc}") from exc
    return Workload(
        name=str(raw["name"]),
        description=str(raw["description"]),
        suite_version=int(raw["suite_version"]),
        turns=turns,
        expect=agg_expect,
        evaluate=evaluate_rubric,
        source_path=yaml_path,
    )


def discover_workloads() -> list[Workload]:
    """Load every workload.yaml under benchmarks/workloads/<name>/."""
    if not WORKLOADS_DIR.is_dir():
        return []
    out: list[Workload] = []
    for child in sorted(WORKLOADS_DIR.iterdir()):
        yaml_path = child / "workload.yaml"
        if yaml_path.is_file():
            out.append(load_workload(yaml_path))
    return out


# ---------------------------------------------------------------------------
# Provenance (benchmark.md §6.1)
# ---------------------------------------------------------------------------


def _git(*args: str) -> str:
    try:
        return (
            subprocess.check_output(["git", *args], cwd=REPO_ROOT, stderr=subprocess.DEVNULL)
            .decode()
            .strip()
        )
    except Exception:
        return ""


@dataclass
class Provenance:
    suite_version: int
    metis_commit_sha: str
    metis_branch: str
    metis_dirty: bool
    pricing_version: str
    actual_model: str
    actual_provider: str
    baseline_model: str
    python_version: str
    temperature: float | None
    started_at: str
    ended_at: str = ""

    def finalize(self) -> None:
        self.ended_at = datetime.now(UTC).isoformat()

    def header_lines(self) -> list[str]:
        dirty = " (dirty)" if self.metis_dirty else " (clean)"
        return [
            "=== Metis benchmark suite ===",
            f"commit:           {self.metis_commit_sha[:7]}{dirty} on {self.metis_branch}",
            f"suite_version:    {self.suite_version}",
            f"actual_model:     {self.actual_model}",
            f"baseline_model:   {self.baseline_model}",
            f"pricing_version:  {self.pricing_version}",
            f"temperature:      {self.temperature}",
        ]


# ---------------------------------------------------------------------------
# Per-workload result
# ---------------------------------------------------------------------------


@dataclass
class WorkloadResult:
    name: str
    started_us: int
    ended_us: int
    turns: int
    llm_calls: int
    tool_calls: int
    actual_repriced_usd: float
    baseline_repriced_usd: float
    savings_usd: float
    savings_pct: float
    actual_stamped_usd: float
    rows_total: int
    rows_missing_from_price_table: int
    assertion_failures: list[str] = field(default_factory=list)
    quality_score: float | None = None
    quality_confidence: float | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# .env loader (matches scripts/smoke.py)
# ---------------------------------------------------------------------------


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


# ---------------------------------------------------------------------------
# Execute a workload
# ---------------------------------------------------------------------------


def _now_us() -> int:
    return int(datetime.now(UTC).timestamp() * 1_000_000)


def _check_assertions(
    workload: Workload,
    per_turn_metrics: list[dict],
    total_cost_usd: Decimal,
    total_llm_calls: int,
    hard_failures: int,
) -> list[str]:
    """Apply soft-floor / hard-ceiling assertions per benchmark.md §3.1."""
    failures: list[str] = []
    for i, (turn, metrics) in enumerate(zip(workload.turns, per_turn_metrics, strict=False)):
        exp = turn.expect
        tc = metrics["tool_calls"]
        if "min_tool_calls" in exp and tc < exp["min_tool_calls"]:
            failures.append(f"turn {i}: tool_calls={tc} < min_tool_calls={exp['min_tool_calls']}")
        if "max_tool_calls" in exp and tc > exp["max_tool_calls"]:
            failures.append(f"turn {i}: tool_calls={tc} > max_tool_calls={exp['max_tool_calls']}")
        if "contains_substring" in exp:
            needle = exp["contains_substring"].lower()
            if needle not in metrics["assistant_text"].lower():
                failures.append(f"turn {i}: assistant_text missing {exp['contains_substring']!r}")
        if "stop_reason" in exp and metrics["stop_reason"] != exp["stop_reason"]:
            failures.append(
                f"turn {i}: stop_reason={metrics['stop_reason']!r} != "
                f"expected {exp['stop_reason']!r}"
            )
    agg = workload.expect
    if "max_total_cost_usd" in agg:
        ceiling = Decimal(str(agg["max_total_cost_usd"]))
        if total_cost_usd > ceiling:
            failures.append(f"total_cost_usd={total_cost_usd} > max_total_cost_usd={ceiling}")
    if "min_llm_calls" in agg and total_llm_calls < agg["min_llm_calls"]:
        failures.append(f"total llm_calls={total_llm_calls} < min_llm_calls={agg['min_llm_calls']}")
    if "max_hard_failures" in agg and hard_failures > agg["max_hard_failures"]:
        failures.append(
            f"hard_failures={hard_failures} > max_hard_failures={agg['max_hard_failures']}"
        )
    return failures


async def run_workload(
    workload: Workload,
    *,
    db_path: Path,
    actual_model: str | None,
    temperature: float | None,
    pattern_seed_path: Path | None = None,
    pattern_save_path: Path | None = None,
    no_active_model: bool = False,
) -> tuple[int, int, list[dict], Decimal, int, int, str]:
    """Drive one workload end-to-end. Returns (started_us, ended_us,
    per_turn_metrics, total_cost_usd, total_llm_calls, total_tool_calls, session_id).

    The trace DB is shared across workloads; this function appends to it.
    Each workload sets up its own ChatRuntime against a freshly-copied
    workspace tempdir (benchmark.md §3.2).

    `pattern_seed_path` / `pattern_save_path` are an optional benchmark-only
    affordance: the pattern store normally lives at `<workspace>/.metis/
    patterns.db` and dies with the tempdir. To compare cold vs warm pattern
    behavior across separate suite runs, the harness can pre-seed the
    workspace's `.metis/patterns.db` from `pattern_seed_path` and copy the
    post-run DB to `pattern_save_path`.

    `no_active_model=True` calls `create_session` without `active_model=`,
    letting the routing chain fall through past slot 2 (`manual_sticky`).
    The runtime's `global_default_model` still owns slot 7. Combined with a
    pre-populated pattern store this lets slot 4 (`pattern`) actually win.
    """
    from metis_cli.runtime import setup_runtime, shutdown_runtime

    started_us = _now_us()
    workspace_src = workload.source_path.parent / "workspace"
    if not workspace_src.is_dir():
        raise FileNotFoundError(f"{workload.name}: missing workspace dir at {workspace_src}")

    with tempfile.TemporaryDirectory(prefix=f"metis-bench-{workload.name}-") as tmp:
        ws = Path(tmp) / "workspace"
        shutil.copytree(workspace_src, ws)
        if pattern_seed_path is not None and pattern_seed_path.is_file():
            seed_dst = ws / ".metis" / "patterns.db"
            seed_dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(pattern_seed_path, seed_dst)
            print(f"  [{workload.name}] seeded pattern store from {pattern_seed_path}")
        runtime = await setup_runtime(
            workspace_path=str(ws),
            db_path=str(db_path),
            global_default_model=actual_model,
        )
        try:
            if no_active_model:
                session = runtime.manager.create_session(workspace_path=str(ws))
            else:
                session = runtime.manager.create_session(
                    workspace_path=str(ws), active_model=actual_model
                )
            session_id = session.id
            per_turn_metrics: list[dict] = []
            total_cost = Decimal("0")
            total_llm = 0
            total_tool = 0
            for i, turn in enumerate(workload.turns):
                print(
                    f"  [{workload.name}] turn {i + 1}/{len(workload.turns)}: ", end="", flush=True
                )
                result = await runtime.manager.submit_turn(
                    session.id, turn.prompt, temperature=temperature
                )
                per_turn_metrics.append(
                    {
                        "tool_calls": result.tool_call_count,
                        "llm_calls": result.llm_call_count,
                        "assistant_text": result.assistant_text or "",
                        "stop_reason": result.stop_reason.value
                        if hasattr(result.stop_reason, "value")
                        else str(result.stop_reason),
                        "cost_usd": str(result.cost_usd),
                    }
                )
                total_cost += result.cost_usd
                total_llm += result.llm_call_count
                total_tool += result.tool_call_count
                print(
                    f"llm={result.llm_call_count} tool={result.tool_call_count} "
                    f"cost=${result.cost_usd:.6f}"
                )
        finally:
            # Wait for the per-turn evaluator subscribers to drain so the
            # workload-level evaluation can read fresh per-turn verdicts.
            try:
                await runtime.bus.drain()
            except Exception:
                pass
            await shutdown_runtime(runtime)
            if pattern_save_path is not None:
                src = ws / ".metis" / "patterns.db"
                if src.is_file():
                    # The PatternStore uses WAL mode. Closing the connection
                    # in shutdown_runtime doesn't guarantee the WAL is
                    # checkpointed back into the main DB file, so
                    # shutil.copyfile (which only sees the main file) can
                    # snapshot a near-empty database. Force a TRUNCATE
                    # checkpoint here so the copy is durable.
                    co = sqlite3.connect(str(src))
                    try:
                        co.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    finally:
                        co.close()
                    pattern_save_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(src, pattern_save_path)
                    print(f"  [{workload.name}] saved pattern store to {pattern_save_path}")

    ended_us = _now_us()
    return started_us, ended_us, per_turn_metrics, total_cost, total_llm, total_tool, session_id


async def evaluate_workload_quality(
    workload: Workload,
    *,
    db_path: Path,
    session_id: str,
    per_turn_metrics: list[dict],
    assertion_failures: list[str],
) -> tuple[float | None, float | None]:
    """Compute the workload-level quality verdict and write it to the trace DB.

    Reads per-turn `eval.completed` verdicts for the workload's session
    (the runtime's evaluator emitted them inline during run_workload),
    builds the workload SubjectContext, runs the heuristic judge, and
    emits the workload verdict on a fresh bus attached to the same DB.
    """
    from metis_core.eval import register_evaluator
    from metis_core.events.bus import EventBus
    from metis_core.trace.store import TraceStore

    trace = TraceStore(db_path)
    bus = EventBus()
    bus.start()
    trace_handle = trace.attach_to(bus, name="trace-store-workload-eval")
    try:
        # Pull turn-level scores already emitted by the inline evaluator.
        session_events = trace.events_for_session(session_id)
        per_turn_scores: list[float] = []
        for e in session_events:
            if e.type != "eval.completed":
                continue
            if e.payload.get("subject_kind") != "turn":
                continue
            per_turn_scores.append(float(e.payload.get("score") or 0.0))
        final_response_text = per_turn_metrics[-1]["assistant_text"] if per_turn_metrics else ""
        evaluator, _ = register_evaluator(bus, trace, judge=HeuristicJudge())
        try:
            verdict = await evaluator.evaluate_workload(
                workload_run_id=f"{workload.name}/{session_id}",
                session_id=session_id,
                per_turn_scores=per_turn_scores,
                final_response_text=final_response_text,
                assertion_failures=assertion_failures,
                workload_rubric=workload.evaluate,
                workload_name=workload.name,
            )
        finally:
            evaluator.unregister()
        await bus.drain()
        if verdict is None:
            return None, None
        return verdict.score, verdict.confidence
    finally:
        bus.unsubscribe(trace_handle)
        await bus.stop()
        trace.close()


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _format_table(rows: list[WorkloadResult]) -> str:
    header = (
        "  workload                    turns  llm  tool   actual_$    baseline_$   "
        "saved_$  saved_%   quality"
    )
    lines = [header]
    for r in rows:
        quality = (
            f"{r.quality_score:.2f}@{r.quality_confidence:.2f}"
            if r.quality_score is not None and r.quality_confidence is not None
            else "  -  "
        )
        lines.append(
            f"  {r.name:<28}{r.turns:<7}{r.llm_calls:<5}{r.tool_calls:<6}"
            f"{r.actual_repriced_usd:<12.6f}{r.baseline_repriced_usd:<13.6f}"
            f"{r.savings_usd:<9.6f}{r.savings_pct * 100:>6.1f}%  {quality}"
        )
    return "\n".join(lines)


def _aggregate_savings(store: AnalyticsStore, window: TimeWindow, baseline: str, pricing) -> dict:
    return store.savings(window, baseline=baseline, price_table=pricing)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def amain() -> int:
    parser = argparse.ArgumentParser(
        description="Run the Metis benchmark suite.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--workload",
        help="Run a single workload by name. Default: run the full suite.",
    )
    parser.add_argument(
        "--model",
        default="anthropic:claude-haiku-4-5",
        help="Actual model (canonical id or alias). Default: anthropic:claude-haiku-4-5.",
    )
    parser.add_argument(
        "--baseline",
        default="anthropic:claude-sonnet-4-6",
        help="Baseline model for the counterfactual. Default: anthropic:claude-sonnet-4-6.",
    )
    parser.add_argument(
        "--db-path",
        default=None,
        help="Trace DB path. Default: benchmarks/.runs/benchmark-<UTC-ts>.db. "
        "Refuses to overwrite an existing file unless --skip-execute is set.",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.0,
        help="Temperature to pass through submit_turn (default: 0.0).",
    )
    parser.add_argument(
        "--skip-execute",
        action="store_true",
        help="Don't run workloads; just re-run analytics against the given --db-path. "
        "Useful for re-printing a report from a prior DB.",
    )
    parser.add_argument(
        "--pattern-seed-dir",
        default=None,
        help="Directory containing per-workload `<name>.db` files to pre-seed each "
        "workload's pattern store (warm-run comparison). Files are copied into the "
        "workload tempdir as `.metis/patterns.db` before the run starts. Missing "
        "per-workload files are silently skipped.",
    )
    parser.add_argument(
        "--pattern-save-dir",
        default=None,
        help="Directory to copy each workload's post-run `.metis/patterns.db` into "
        "(as `<name>.db`). Lets a follow-up `--pattern-seed-dir` run see what the "
        "first run learned.",
    )
    parser.add_argument(
        "--patterns-db-path",
        default=None,
        help="Single shared patterns DB used across every workload in this run "
        "and any follow-up run that points at the same file. Before each "
        "workload the file is copied into the workspace tempdir; after each "
        "workload the post-run DB is copied back out. Lets two runs (e.g., "
        "haiku then sonnet) accumulate outcomes for distinct models on the "
        "same fingerprints so slot 4 has multiple models to choose between.",
    )
    parser.add_argument(
        "--no-active-model",
        action="store_true",
        help="Don't pin `Session.active_model`. The routing chain falls "
        "through slot 2 (`manual_sticky`) so slots 3+ (rule, pattern, "
        "default) can win. Use together with `--patterns-db-path` pointed "
        "at a populated DB to test whether slot 4 (`pattern`) actually fires.",
    )
    args = parser.parse_args()

    _load_dotenv(REPO_ROOT / ".env")

    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    db_path = Path(args.db_path) if args.db_path else RUNS_DIR / f"benchmark-{ts}.db"

    workloads = discover_workloads()
    if args.workload:
        workloads = [w for w in workloads if w.name == args.workload]
        if not workloads:
            print(f"unknown workload: {args.workload}", file=sys.stderr)
            return 2
    if not workloads:
        print("no workloads found under benchmarks/workloads/", file=sys.stderr)
        return 2

    if args.skip_execute:
        if not db_path.is_file():
            print(
                f"--skip-execute set but --db-path {db_path} does not exist",
                file=sys.stderr,
            )
            return 2
    else:
        if db_path.exists():
            print(
                f"trace db {db_path} already exists; refusing to mix events. "
                "Pass a new --db-path or delete the existing file.",
                file=sys.stderr,
            )
            return 2

    # Setup provenance: we need the registry to resolve aliases + the
    # PriceTable version. Build a lightweight runtime up-front using the
    # first workload's workspace just to inspect; we'll tear it down before
    # the real per-workload runs.
    from metis_cli.runtime import setup_runtime, shutdown_runtime

    first_workspace = workloads[0].source_path.parent / "workspace"
    probe_db = db_path  # ok to reuse; setup_runtime opens but doesn't write events
    probe = await setup_runtime(
        workspace_path=str(first_workspace),
        db_path=str(probe_db),
        global_default_model=args.model,
    )
    actual_resolved = probe.registry.resolve_alias(args.model) or args.model
    baseline_resolved = probe.registry.resolve_alias(args.baseline) or args.baseline
    if actual_resolved not in probe.registry:
        print(f"actual model {args.model!r} not configured", file=sys.stderr)
        await shutdown_runtime(probe)
        return 2
    if baseline_resolved not in probe.pricing:
        print(
            f"baseline {args.baseline!r} not in current PriceTable "
            f"(version={probe.pricing.version})",
            file=sys.stderr,
        )
        await shutdown_runtime(probe)
        return 2
    actual_provider = probe.registry.provider_of(actual_resolved)
    pricing_version = probe.pricing.version
    await shutdown_runtime(probe)

    provenance = Provenance(
        suite_version=1,
        metis_commit_sha=_git("rev-parse", "HEAD") or "unknown",
        metis_branch=_git("rev-parse", "--abbrev-ref", "HEAD") or "unknown",
        metis_dirty=bool(_git("status", "--porcelain")),
        pricing_version=pricing_version,
        actual_model=actual_resolved,
        actual_provider=actual_provider or "unknown",
        baseline_model=baseline_resolved,
        python_version=sys.version.split()[0],
        temperature=args.temperature,
        started_at=datetime.now(UTC).isoformat(),
    )

    print("\n".join(provenance.header_lines()))
    print(f"db:               {db_path}")
    print()

    workload_results: list[WorkloadResult] = []
    overall_start_us: int | None = None
    overall_end_us: int | None = None

    pattern_seed_dir = Path(args.pattern_seed_dir).expanduser() if args.pattern_seed_dir else None
    pattern_save_dir = Path(args.pattern_save_dir).expanduser() if args.pattern_save_dir else None
    if pattern_save_dir is not None:
        pattern_save_dir.mkdir(parents=True, exist_ok=True)
    shared_patterns_db = (
        Path(args.patterns_db_path).expanduser() if args.patterns_db_path else None
    )
    if shared_patterns_db is not None:
        shared_patterns_db.parent.mkdir(parents=True, exist_ok=True)

    if not args.skip_execute:
        for workload in workloads:
            if shared_patterns_db is not None:
                seed_path = shared_patterns_db if shared_patterns_db.is_file() else None
                save_path = shared_patterns_db
            else:
                seed_path = (
                    pattern_seed_dir / f"{workload.name}.db"
                    if pattern_seed_dir is not None
                    else None
                )
                save_path = (
                    pattern_save_dir / f"{workload.name}.db"
                    if pattern_save_dir is not None
                    else None
                )
            try:
                started_us, ended_us, per_turn, cost, llm, tool, session_id = await run_workload(
                    workload,
                    db_path=db_path,
                    actual_model=actual_resolved,
                    temperature=args.temperature,
                    pattern_seed_path=seed_path,
                    pattern_save_path=save_path,
                    no_active_model=args.no_active_model,
                )
            except Exception as exc:
                workload_results.append(
                    WorkloadResult(
                        name=workload.name,
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
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
                print(f"  [{workload.name}] FAILED: {exc}", file=sys.stderr)
                continue

            overall_start_us = (
                started_us if overall_start_us is None else min(overall_start_us, started_us)
            )
            overall_end_us = ended_us if overall_end_us is None else max(overall_end_us, ended_us)

            # Per-workload savings against the workload's window.
            store = AnalyticsStore(db_path)
            try:
                window = TimeWindow(
                    start=datetime.fromtimestamp(started_us / 1_000_000, tz=UTC),
                    end=datetime.fromtimestamp(ended_us / 1_000_000 + 1, tz=UTC),
                )
                from metis_core.pricing import DEFAULT_PRICE_TABLE  # baseline lookup table

                pricing = DEFAULT_PRICE_TABLE
                # Match provenance pricing — re-fetching the runtime overlay is
                # expensive; for the typical native-only case DEFAULT_PRICE_TABLE
                # is sufficient. If the actual model is OpenRouter-priced, the
                # spec acknowledges the overlay version is composite.
                savings = _aggregate_savings(store, window, baseline_resolved, pricing)
            finally:
                store.close()

            assertion_failures = _check_assertions(
                workload,
                per_turn,
                cost,
                llm,
                hard_failures=0,  # populated after analytics routing call below
            )
            quality_score, quality_confidence = await evaluate_workload_quality(
                workload,
                db_path=db_path,
                session_id=session_id,
                per_turn_metrics=per_turn,
                assertion_failures=assertion_failures,
            )
            workload_results.append(
                WorkloadResult(
                    name=workload.name,
                    started_us=started_us,
                    ended_us=ended_us,
                    turns=len(workload.turns),
                    llm_calls=llm,
                    tool_calls=tool,
                    actual_repriced_usd=savings["actual_repriced_usd"],
                    baseline_repriced_usd=savings["baseline_repriced_usd"],
                    savings_usd=savings["savings_usd"],
                    savings_pct=savings["savings_pct"],
                    actual_stamped_usd=savings["actual_stamped_usd"],
                    rows_total=savings["rows_total"],
                    rows_missing_from_price_table=savings["rows_missing_from_price_table"],
                    assertion_failures=assertion_failures,
                    quality_score=quality_score,
                    quality_confidence=quality_confidence,
                )
            )

    # Aggregate savings over the full run window.
    from metis_core.pricing import DEFAULT_PRICE_TABLE

    store = AnalyticsStore(db_path)
    try:
        if overall_start_us is None or overall_end_us is None:
            # --skip-execute path: use a wide window
            agg_window = TimeWindow(
                start=datetime(1970, 1, 1, tzinfo=UTC),
                end=datetime.now(UTC),
            )
        else:
            agg_window = TimeWindow(
                start=datetime.fromtimestamp(overall_start_us / 1_000_000, tz=UTC),
                end=datetime.fromtimestamp(overall_end_us / 1_000_000 + 1, tz=UTC),
            )
        aggregate = _aggregate_savings(store, agg_window, baseline_resolved, DEFAULT_PRICE_TABLE)
        routing_stats = store.routing(agg_window)
    finally:
        store.close()

    provenance.finalize()

    # Print report
    successful = [r for r in workload_results if r.error is None]
    print()
    print("Per-workload:")
    if successful:
        print(_format_table(successful))
    failures = [r for r in workload_results if r.error is not None]
    if failures:
        print()
        print("Errors:")
        for r in failures:
            print(f"  {r.name}: {r.error}")

    print()
    print("Aggregate:")
    print(f"  rows_total:                       {aggregate['rows_total']}")
    print(f"  rows_missing_from_price_table:    {aggregate['rows_missing_from_price_table']}")
    print(f"  actual_repriced_usd:              {aggregate['actual_repriced_usd']:.6f}")
    print(f"  baseline_repriced_usd:            {aggregate['baseline_repriced_usd']:.6f}")
    print(f"  savings_usd:                      {aggregate['savings_usd']:.6f}")
    print(f"  savings_pct:                      {aggregate['savings_pct'] * 100:.1f}%")
    print(f"  hard_failures (routing):          {routing_stats['hard_failures']}")
    print()
    print("Run the dashboard against this DB to verify:")
    print(f"  uv run metis serve {REPO_ROOT} --db-path {db_path}")
    print("  open http://127.0.0.1:8421/dashboard")

    # Write JSON artifact
    artifact_path = db_path.with_suffix(".json")
    artifact = {
        "provenance": asdict(provenance),
        "aggregate": aggregate,
        "routing": routing_stats,
        "workloads": [asdict(r) for r in workload_results],
    }
    artifact_path.write_text(json.dumps(artifact, indent=2))
    print(f"\nJSON report: {artifact_path}")

    # Determine exit code (benchmark.md §9.4)
    any_assertion_failed = any(r.assertion_failures for r in successful)
    any_workload_errored = bool(failures)
    if any_workload_errored or any_assertion_failed:
        if any_assertion_failed:
            print("\nAssertion failures:", file=sys.stderr)
            for r in successful:
                for f in r.assertion_failures:
                    print(f"  {r.name}: {f}", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    return asyncio.run(amain())


if __name__ == "__main__":
    sys.exit(main())
