"""`metis trial` — pre-baked buyer-trial workload runner.

Drives a single workload from `benchmarks/workloads-trial/<name>/` through
either (a) the local provider adapters using the API keys in env, or
(b) a running gateway via `--gateway-url` + `--gateway-key`.

In gateway mode the AnthropicAdapter picks up `ANTHROPIC_BASE_URL` from env
(SDK auto-detect) and the gateway key replaces the upstream Anthropic key.
The trial's provider call flows through the gateway so cost lands in the
gateway's trace DB (visible at `/analytics/by_key`); the local trace DB
this command writes is for the savings counterfactual only.

Output is buyer-facing: cost, baseline-cost-counterfactual, savings_pct,
and (when an evaluator was wired) cost-per-quality. Designed for the
operations/quickstart.md flow — one command after `helm install`.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[4]
TRIAL_WORKLOADS_DIR = REPO_ROOT / "benchmarks" / "workloads-trial"
DEFAULT_TRIAL_WORKLOAD = "refactor-extract-helper"
DEFAULT_BASELINE_MODEL = "anthropic:claude-sonnet-4-6"
DEFAULT_TRIAL_MODEL = "anthropic:claude-haiku-4-5"


@dataclass(frozen=True)
class TrialResult:
    workload: str
    actual_model: str
    baseline_model: str
    turns: int
    llm_calls: int
    tool_calls: int
    actual_repriced_usd: float
    baseline_repriced_usd: float
    savings_usd: float
    savings_pct: float
    quality_score: float | None
    quality_confidence: float | None
    db_path: Path
    gateway_mode: bool


def _discover_trial_workload(name: str) -> Path:
    yaml_path = TRIAL_WORKLOADS_DIR / name / "workload.yaml"
    if not yaml_path.is_file():
        available = []
        if TRIAL_WORKLOADS_DIR.is_dir():
            available = sorted(
                p.name for p in TRIAL_WORKLOADS_DIR.iterdir() if (p / "workload.yaml").is_file()
            )
        raise FileNotFoundError(
            f"unknown trial workload {name!r}. Available: {available or '[none]'}. "
            f"Looked under {TRIAL_WORKLOADS_DIR}."
        )
    return yaml_path


def _print_header(workload_name: str, actual_model: str, gateway_url: str | None) -> None:
    print("=== Metis trial ===")
    print(f"workload:     {workload_name}")
    print(f"model:        {actual_model}")
    if gateway_url:
        print(f"gateway:      {gateway_url}")
    else:
        print("gateway:      (none — local provider adapters)")
    print()


def _print_result(r: TrialResult) -> None:
    print()
    print("=== Trial result ===")
    print(f"workload:               {r.workload}")
    print(f"actual model:           {r.actual_model}")
    print(f"baseline model:         {r.baseline_model}")
    print(f"turns / llm / tool:     {r.turns} / {r.llm_calls} / {r.tool_calls}")
    print(f"actual cost (USD):      {r.actual_repriced_usd:.6f}")
    print(f"baseline cost (USD):    {r.baseline_repriced_usd:.6f}")
    print(f"savings (USD):          {r.savings_usd:.6f}")
    print(f"savings_pct:            {r.savings_pct * 100:.1f}%")
    if r.quality_score is not None and r.quality_confidence is not None:
        print(f"quality:                {r.quality_score:.2f}@{r.quality_confidence:.2f}")
        if r.actual_repriced_usd > 0 and r.quality_score > 0:
            cost_per_q = r.actual_repriced_usd / r.quality_score
            print(f"cost-per-quality (USD): {cost_per_q:.6f}")
    else:
        print("quality:                — (set workload.evaluate.rubric to opt in)")
    print(f"trace db:               {r.db_path}")
    if r.gateway_mode:
        print()
        print("Gateway-mode note: per-key cost lands in the gateway's trace DB.")
        print("Read it with `curl http://<gateway>:8421/analytics/by_key`")
        print("(point `metis serve` at the gateway's metis.db; see")
        print("docs/operations/quickstart.md).")


async def _run(
    *,
    workload_name: str,
    actual_model: str,
    baseline_model: str,
    db_path: Path | None,
    gateway_url: str | None,
    gateway_key: str | None,
) -> TrialResult:
    # Lazy imports so `metis --help` stays cheap.
    from metis_core.analytics import AnalyticsStore
    from metis_core.analytics.windows import TimeWindow
    from metis_core.eval import HeuristicJudge, register_evaluator
    from metis_core.events.bus import EventBus
    from metis_core.pricing import DEFAULT_PRICE_TABLE
    from metis_core.trace.store import TraceStore

    from metis_cli.runtime import setup_runtime, shutdown_runtime

    yaml_path = _discover_trial_workload(workload_name)
    raw = yaml.safe_load(yaml_path.read_text())
    if not isinstance(raw, dict) or "turns" not in raw:
        raise ValueError(f"{yaml_path}: invalid workload (no turns)")
    turns: list[dict[str, Any]] = raw["turns"]
    workspace_src = yaml_path.parent / "workspace"
    if not workspace_src.is_dir():
        raise FileNotFoundError(f"{yaml_path}: missing workspace dir at {workspace_src}")

    if gateway_url and gateway_key:
        # AnthropicAdapter (and the openai SDK) auto-pick base_url from env.
        os.environ["ANTHROPIC_BASE_URL"] = gateway_url
        os.environ["ANTHROPIC_API_KEY"] = gateway_key
    elif bool(gateway_url) != bool(gateway_key):
        raise ValueError("--gateway-url and --gateway-key must be passed together")

    if db_path is None:
        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        db_path = Path(tempfile.gettempdir()) / f"metis-trial-{ts}.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    started_us = int(datetime.now(UTC).timestamp() * 1_000_000)
    quality_score: float | None = None
    quality_confidence: float | None = None
    total_cost = Decimal("0")
    total_llm = 0
    total_tool = 0
    final_assistant_text = ""

    with tempfile.TemporaryDirectory(prefix=f"metis-trial-{workload_name}-") as tmp:
        ws = Path(tmp) / "workspace"
        shutil.copytree(workspace_src, ws)
        runtime = await setup_runtime(
            workspace_path=str(ws),
            db_path=str(db_path),
            global_default_model=actual_model,
        )
        try:
            session = runtime.manager.create_session(
                workspace_path=str(ws), active_model=actual_model
            )
            for i, turn in enumerate(turns):
                prompt = str(turn["prompt"]).strip()
                print(f"  turn {i + 1}/{len(turns)}: ", end="", flush=True)
                result = await runtime.manager.submit_turn(
                    session.id, prompt, workload_id=workload_name
                )
                total_cost += result.cost_usd
                total_llm += result.llm_call_count
                total_tool += result.tool_call_count
                final_assistant_text = result.assistant_text or final_assistant_text
                print(
                    f"llm={result.llm_call_count} tool={result.tool_call_count} "
                    f"cost=${result.cost_usd:.6f}"
                )
            session_id = session.id
        finally:
            try:
                await runtime.bus.drain()
            except Exception:
                pass
            await shutdown_runtime(runtime)

    ended_us = int(datetime.now(UTC).timestamp() * 1_000_000)
    window = TimeWindow(
        start=datetime.fromtimestamp(started_us / 1_000_000, tz=UTC),
        end=datetime.fromtimestamp(ended_us / 1_000_000 + 1, tz=UTC),
    )
    store = AnalyticsStore(db_path)
    try:
        savings = store.savings(window, baseline=baseline_model, price_table=DEFAULT_PRICE_TABLE)
    finally:
        store.close()

    # Workload-level quality verdict on a fresh bus + reused trace DB.
    evaluate_block = raw.get("evaluate") or {}
    if evaluate_block:
        from metis_core.eval.rubric import parse_workload_rubric

        try:
            workload_rubric = parse_workload_rubric(evaluate_block)
        except Exception:
            workload_rubric = None
        if workload_rubric is not None:
            trace = TraceStore(db_path)
            bus = EventBus()
            bus.start()
            handle = trace.attach_to(bus, name="trace-store-trial-eval")
            try:
                session_events = trace.events_for_session(session_id)
                per_turn_scores = [
                    float(e.payload.get("score") or 0.0)
                    for e in session_events
                    if e.type == "eval.completed" and e.payload.get("subject_kind") == "turn"
                ]
                evaluator, _ = register_evaluator(bus, trace, judge=HeuristicJudge())
                try:
                    verdict = await evaluator.evaluate_workload(
                        workload_run_id=f"{workload_name}/{session_id}",
                        session_id=session_id,
                        per_turn_scores=per_turn_scores,
                        final_response_text=final_assistant_text,
                        assertion_failures=[],
                        workload_rubric=workload_rubric,
                        workload_name=workload_name,
                    )
                finally:
                    evaluator.unregister()
                await bus.drain()
                if verdict is not None:
                    quality_score = verdict.score
                    quality_confidence = verdict.confidence
            finally:
                bus.unsubscribe(handle)
                await bus.stop()
                trace.close()

    return TrialResult(
        workload=workload_name,
        actual_model=actual_model,
        baseline_model=baseline_model,
        turns=len(turns),
        llm_calls=total_llm,
        tool_calls=total_tool,
        actual_repriced_usd=savings["actual_repriced_usd"],
        baseline_repriced_usd=savings["baseline_repriced_usd"],
        savings_usd=savings["savings_usd"],
        savings_pct=savings["savings_pct"],
        quality_score=quality_score,
        quality_confidence=quality_confidence,
        db_path=db_path,
        gateway_mode=bool(gateway_url and gateway_key),
    )


def run_trial_command(
    *,
    workload: str,
    model: str,
    baseline: str,
    db_path: str | None,
    gateway_url: str | None,
    gateway_key: str | None,
) -> int:
    actual_model = model
    baseline_model = baseline
    target_db = Path(db_path).expanduser() if db_path else None

    _print_header(workload, actual_model, gateway_url)
    try:
        result = asyncio.run(
            _run(
                workload_name=workload,
                actual_model=actual_model,
                baseline_model=baseline_model,
                db_path=target_db,
                gateway_url=gateway_url,
                gateway_key=gateway_key,
            )
        )
    except FileNotFoundError as exc:
        print(f"trial failed: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"trial failed: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"trial failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    _print_result(result)
    return 0
