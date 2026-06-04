# Benchmark Specification

**Status:** Draft v1.1 (signal_strength partition added 2026-05-15)
**Last updated:** 2026-05-15

> Defines the workload suite, baseline, and measurement methodology that turn
> `analytics-api.md /analytics/savings.actual_repriced_usd` and
> `baseline_repriced_usd` into a credible "Metis saved you X%" number — the
> artifact the project strategy (private) names as "currently the biggest
> gap between 'the architecture should work' and 'we can show it works.'"

---

## 1. Purpose

`/analytics/savings` returns the right shape: `actual_repriced_usd`,
`baseline_repriced_usd`, `savings_usd`, `savings_pct`. But the numbers are only
as meaningful as the workload they sum over. With no defined workload, an
operator pointing at a personal trace DB asks reasonable questions the dashboard
can't answer:

- "Is this 30% savings because Metis is good, or because I happened to type
  short prompts to haiku this week?"
- "Will the savings hold if I switch to a longer-running session?"
- "Did the routing improve last release, or did my workload just change?"

This spec closes that gap by pinning the workload. A **benchmark suite** is a
versioned set of scripted user-turn scripts ("workloads") and the harness that
runs them. Re-running the suite on a clean trace DB produces:

1. A trace store populated only with the suite's calls.
2. A per-workload + aggregate report computed by calling `/analytics/savings`
   directly against that trace store.
3. A printed comparison table and a JSON artifact suitable for paste-into-deck.

The same trace DB, served by `metis serve <workspace> --db-path <db>`, renders
the same numbers on the dashboard — by construction, since `scripts/benchmark.py`
calls the in-process `AnalyticsStore.savings()` method that backs the HTTP
handler.

This spec depends on:

- [`analytics-api.md §4.7`](analytics-api.md) for the savings response shape and
  re-pricing semantics.
- [`provider-adapter-contract.md`](provider-adapter-contract.md) (planned) for
  `CanonicalRequest.temperature`.
- [`event-bus-and-trace-catalog.md`](event-bus-and-trace-catalog.md) for the
  `llm.call_completed` / `turn.completed` events whose presence we assert.
- [`canonical-message-format.md §9.1`](canonical-message-format.md) for the
  on-disk trace + session schema.

---

## 2. Goals and non-goals

### 2.1 Goals

1. **Reproducible across runs.** Same suite version + same model versions +
   same `PriceTable` version → same per-workload report within a documented
   tolerance. Determinism is approximate, not absolute (LLMs are not strictly
   deterministic even at `temperature=0`); see §6 for the tolerance.
2. **Self-contained.** Each workload bundles its own fixture workspace. No
   dependency on the host's filesystem state or the metis repo's commit
   history. Running the suite in a fresh clone produces the same numbers.
3. **Real prompts, not toy.** Workloads exercise the tool dispatcher (file
   reads, edits, shell), include multi-turn context, and reach into the
   adapter's tool-cycle path. A workload whose only behavior is a text
   completion isn't a benchmark of Metis — it's a benchmark of the model.
4. **Same number on the dashboard.** The script's printed number equals
   the dashboard's `/analytics/savings.savings_pct` over the same window.
5. **Cheap enough to run weekly.** Per [§5](#5-cost-budget): full-suite run
   targets ≤ $5 against haiku/mini-class actual models with a sonnet/opus
   baseline. Smoke runs on a single workload target ≤ $0.50.

### 2.2 Non-goals

1. **No latency benchmarking.** The headline number is cost; latency is a
   secondary metric ([§7](#7-secondary-metrics)) but not the value prop.
2. **No quality scoring of model outputs in v1.** "Did the agent solve the
   task correctly?" is the evaluator's job (strategic context, private);
   benchmark v1 asserts that turns *completed* and *exercised expected events*,
   not that the answers were good. A workload whose outputs are wrong but cost
   the right amount still scores. This is a known v1 limitation.
3. **No multi-tenant / multi-user simulation.** Single workspace, single
   session per workload. Multi-user pricing is downstream of the
   the project strategy (private) replacement-agent-vs-gateway fork.
4. **No CI integration in v1.** The suite runs against real APIs and costs
   real money. Promotion to CI is a separate decision; v1 ships the harness
   so a human can run it on demand.
5. **No mocked-adapter mode.** A mocked benchmark proves nothing about the
   savings story — the cost numbers are precisely the value being measured.
   The unit tests under [`packages/metis-core/tests/analytics/`](../../packages/metis-core/tests/analytics/)
   already cover the SQL projection logic; benchmark v1 is the end-to-end
   path.

---

## 3. The workload model

A **workload** is a fixture directory under [`benchmarks/workloads/`](../../benchmarks/workloads/)
containing:

```
benchmarks/workloads/<name>/
  workload.yaml         # script + assertions
  workspace/            # files the agent will operate on (the workspace_path)
    ...
```

### 3.1 `workload.yaml` schema

```yaml
name: <slug, matches the directory name>
description: <one-line human summary>
suite_version: 1                     # benchmark suite schema version
signal_strength: high                 # high | marginal; default high (§4.1)
turns:                                # ordered list of user turns
  - prompt: "..."
    expect:                           # optional, per-turn
      min_tool_calls: 1
      max_tool_calls: 20
      contains_substring: "..."       # optional text assertion on assistant_text
      stop_reason: end_turn           # optional, defaults to end_turn
expect:                                # optional, aggregate across the workload
  max_total_cost_usd: 0.50
  min_llm_calls: 1
  max_hard_failures: 0                # /analytics/routing.hard_failures over the window
  min_delegate_calls: 0               # optional, planner-scoped delegate.started count
evaluate:                              # optional, workload-level quality rubric
  rubric: heuristic                    # heuristic | llm | hybrid; default heuristic
  expect_substring_in_final_response: "..."   # passthrough to heuristic signals
  llm_judge_model: anthropic:claude-haiku-4-5   # required when rubric != heuristic
  weight_per_turn: 1.0                 # how turns aggregate (default 1.0)
  grounding_tokens: ["..."]            # optional, see evaluator.md §5.4
  forbidden_grounding: ["..."]         # optional, hallucination-detection list
```

**The `signal_strength` field.** Optional; defaults to `"high"`. Pins
whether the workload produces a stable haiku-vs-sonnet quality gap large
enough for the routing K-NN to learn from (§4.1). Values:

- `high` — smoke-validated gap ≥ 0.4 between the cheap-model mean and
  the strong-model mean across ≥ 4 runs per (workload, model) pair at
  `temperature=0`. Methodology: [`RESULTS.md §13a-1`](../../benchmarks/RESULTS.md).
- `marginal` — gap is within run-to-run variance (typically < 0.15 in
  the §A3-rev6 audit; the smoke gate is "not validated as high"). Kept
  on disk for §A3 reruns and as a regression-sentinel suite, but
  excluded from the default `scripts/benchmark.py` run.

`scripts/benchmark.py` defaults to `signal_strength=high` workloads
only; `--include-marginal` opts the marginal-signal workloads back in.
An explicit `--workload <name>` bypasses the filter so any workload on
disk can be run by name (preserves §A3-rev reproducibility).

**Schema enforcement.** The harness validates the YAML against this shape at
load time. Unknown top-level keys, unknown `expect` keys, and unknown
`evaluate` keys are rejected. This forces schema migrations to flow through
this spec.

**The `evaluate:` block.** Optional; omitting it is equivalent to
`rubric: heuristic` with no substring assertion. The harness passes the
parsed `evaluate` to the evaluator with `subject_kind=workload` after each
workload run; the resulting `eval.completed.score` shows up in the
benchmark report's quality column and feeds the "savings on successful
work" headline ([`evaluator.md §5.4`](evaluator.md)). v1 ships
`rubric: heuristic` only — `llm` / `hybrid` parse but defer to the
heuristic until the LLM-as-judge tier lands.

**Assertions are soft floors / hard ceilings.** `min_*` and `max_*` bound a
window of acceptable behavior — if the model gets cheaper at the same task,
`max_total_cost_usd` does not break the run. The intent is to catch
*regressions* (cost ballooned, tool calls went wild), not to pin behavior so
tightly that an unrelated model release breaks the suite.

### 3.2 `workspace/` directory

A real workspace tree the agent treats as its working directory. The fixture
ships with the files in place; the agent reads / edits / runs them as it would
any project. The harness:

- Copies the `workspace/` subtree to a fresh tempdir at run start. The
  in-tree fixture is **never** mutated by a run.
- Sets that tempdir as `workspace_path` on `SessionManager.create_session()`.
- Removes the tempdir at run end (success or failure).

This is what makes workloads hermetic. The agent can `edit_file` freely;
nothing persists outside the run.

### 3.3 Why YAML, not JSON

YAML files diff cleanly in PRs, support multi-line strings (the prompts are
prose), and read better at PR-review time than JSON. The trade-off is
indentation sensitivity, which the schema validator catches.

### 3.4 Batch-mode (Wave 18a-3)

`scripts/benchmark.py` accepts two batch-flavored flags that opt the run
into the provider's asynchronous batch endpoint (per
[`provider-adapter-contract.md §4.6`](provider-adapter-contract.md)) at
the documented 50% input + output discount. Anthropic-only in v1; the
only adapter declaring `supports_batch_api=True` is the Anthropic
adapter.

#### Two-pass workflow

Batch submission is "best-effort 24h" turnaround, so the harness uses a
two-pass workflow — submit and exit, then collect later:

```bash
# Pass 1: submit. Prints the run id, persists handles, exits.
uv run python scripts/benchmark.py \
    --batch-mode \
    --workload fix-a-bug-small

# (provider takes minutes to hours; run pass 2 when ready)

# Pass 2: collect. Polls handles, ingests results, writes results.json + trace.db.
uv run python scripts/benchmark.py --collect-batch batch-<UTC-ts>
```

The `<run_id>` is `batch-<UTC-timestamp>` — printed by pass 1 and
echoed in pass 2. It also doubles as the directory name under
`benchmarks/.runs/` (gitignored).

#### Persistence

Each invocation of `--batch-mode` writes a single `batch-handles.jsonl`
file under `benchmarks/.runs/<run_id>/`. One JSONL row per submitted
batch (= one batch per workload in the selected suite):

```json
{
  "custom_ids": ["fix-a-bug-small/t0-…", "fix-a-bug-small/t1-…", "fix-a-bug-small/t2-…"],
  "batch_id": "batch_abc123",
  "provider": "anthropic",
  "submitted_at_ms": 1717000000000,
  "workload": "fix-a-bug-small",
  "model": "anthropic:claude-haiku-4-5",
  "status": "submitted",
  "request_count": 3,
  "ingested_at_ms": null
}
```

`status` transitions `submitted -> ingested` (or `failed`) when
`--collect-batch` finishes a row. The JSONL is rewritten atomically
(write-temp + rename) on every transition so an interrupted collect
never produces a half-written handles file.

#### Idempotency

`--collect-batch <run_id>` is idempotent: a row at `status=ingested` is
skipped on re-runs. A network blip during the first collect therefore
cannot produce duplicate `llm.call_completed` events in the trace DB on
retry.

#### Trade-off vs sync mode

Each turn prompt becomes one independent batch entry, with no tools
and no prior-turn assistant text fed forward. Tool-using workloads
therefore land in batch mode as **cost-comparison probes**, not
end-to-end agent runs. The pricing-discount measurement (~50% drop in
`actual_repriced_usd` for the same prompts) is the point. Sync mode
remains the canonical "did the agent solve the task" surface (per-turn
quality scores, tool-cycle correctness, multi-turn coherence).

#### Acceptance gate

Per [§5](#5-cost-budget), a single workload batch run targets ≤ $0.50
on `fix-a-bug-small`. The Wave-18 entry in
[`RESULTS.md`](../../benchmarks/RESULTS.md) documents the first
measurement.

### 3.5 Long-session compaction workload (Wave 19 prep)

[`benchmarks/workloads/long-session-compaction/`](../../benchmarks/workloads/long-session-compaction/)
is a 30-turn workload designed to measure
[`session-compaction.md`](session-compaction.md)'s value claim end-to-end:
**30-60% input-token reduction on sessions past the default
`compaction_threshold_tokens = 30_000`** per the umbrella's Tier-1
ranking ([`token-reduction-strategy.md §4`](../design/token-reduction-strategy.md)).
The workload is opt-in by name (`--workload long-session-compaction`);
its `signal_strength: marginal` keeps it out of the default
model-discrimination suite per §4.1 — it isn't a routing-discrimination
workload, it's a feature-validation workload.

**Shape.** A user iteratively builds a small Python task-tracker library
turn-by-turn: starts with a `Task` dataclass + minimal `TaskStore` + 9
passing tests, ends with priority + due-date + tags + search + JSON
persistence + ~27 passing tests. Context grows organically as the agent
reads files, sees test output, and references earlier decisions. By
turn 30 the message history is ~35-45k tokens — past the compaction
threshold, well into the regime where the lever actually fires.

**Compaction stress design.** Three design rules are pinned in turn 3
("dataclasses only, no Pydantic"; "errors raise, never return `None`";
"all datetimes UTC, use `datetime.now(UTC)`") and load-bearing in
turns 9, 11, 21, 24, and 27. A correct compaction summary preserves
these rules; a lossy summary lets the agent slip Pydantic in at turn
24, or `utcnow()` at turn 11, or `return None` on file-not-found at
turn 27 — all of which fail the final pytest run. Quality regression
shows up in the partial-credit rubric, not just in the diff.

**Measurement methodology.** Wave 19a-5 runs the workload twice on the
same model (default haiku):

1. **Compaction OFF** (`--compaction-disabled` once that flag lands in
   `scripts/benchmark.py`): baseline input-token totals + cost per turn,
   pytest pass count, total cost.
2. **Compaction ON** (default once Wave 19a-2 wires `SessionManager`):
   same prompts, compaction fires at the threshold. Capture per-turn
   input tokens (expect 30-60% reduction past turn ~20), total cost,
   pytest pass count.

The comparison logs to `RESULTS.md §Wave-19a-5` with:
- Per-turn input-token series (a chart-able pair of curves)
- Total cost delta + percentage
- Quality delta (pytest pass count) — should be within ±0.10 of the
  baseline per `session-compaction.md §10`
- One-time cache-write cost at compaction (visible as a turn-N spike in
  the ON curve) per `session-compaction.md §7`

**Final-turn assertion.** Turn 30 prints `PASS N/M` (or `FAIL N/M` on
unfixed failures) for the `test_pass_count_ratio` partial-credit
primitive ([`evaluator.md §5.4`](evaluator.md) v1.2). A clean run
produces `PASS 27/27` (score 1.0); compaction-induced rule violations
produce a lower fraction (e.g. `PASS 24/27` → 0.89).

**Cost budget.** `max_total_cost_usd: 1.50` initially; tighten after
the Wave 19a-5 measurement lands.

---

## 4. The suite

### 4.1 v2 partition — high-signal vs marginal

After §A3-rev6 ([`RESULTS.md`](../../benchmarks/RESULTS.md)), the suite is
partitioned by `signal_strength`. The §A3-rev6 Q1 finding is that across
six end-to-end §A3-series runs the per-workload haiku-vs-sonnet quality
gap stayed within run-to-run variance — the K-NN cannot learn signal
that isn't there. The 13a-1 follow-up tested whether purpose-designed
"haiku-fail" workloads could clear a ≥ 0.4 gap; the result was no (see
[`RESULTS.md §13a-1`](../../benchmarks/RESULTS.md)).

| Workload | Signal | Shape | §A3-rev6 cross-pass gap |
|----------|--------|-------|------------------------:|
| `fix-a-bug-small` | marginal | Find + fix a bug in a tiny python module | +0.070 |
| `write-a-doc-from-notes` | marginal | Read raw notes, produce a structured doc | +0.000 |
| `multi-turn-refactor` | marginal | Rename a function across 4 files | **-0.079** (reverse) |
| `regex-with-edge-cases` | marginal | One-shot NANP regex over 16 cases | +0.119 |
| `multi-file-refactor-with-shared-types` | marginal | Rename a dataclass across 7 files | +0.043 |
| `architectural-explanation-without-hallucination` | marginal | Grounded explanation control case (hallucination detector) | +0.000 |
| `intentionally-failing-task` | marginal | Evaluator-control low-score case | +0.000 |
| `multi-step-with-delegation` | marginal | Planner-driven delegation exercise (Q2-shaped, not Q1) | — |
| `subtle-bug-fix-with-test` (13a-1 candidate) | marginal | Root-cause vs symptom config-loader bug | -0.029 (13a-1) |
| `recursive-data-structure-traversal` (13a-1 candidate) | marginal | Shortest-chain tree walk with tombstoned pruning | +0.083 (13a-1) |
| `refactor-with-contract-preservation` (13a-1 candidate) | marginal | Keyword-only signature refactor preserving 7 callers | +0.000 (13a-1) |

**No workload is currently `signal_strength: high`.** The default
`scripts/benchmark.py` run with no flags emits a helpful error
pointing to `--include-marginal`. This is intentional: shipping a
workload as "high-signal" requires smoke-validating a ≥ 0.4 gap, and
no such workload has emerged from the candidate set as of 2026-05-15.

### 4.2 Adding a high-signal workload

The candidate path is:

1. Design a workload where haiku reliably fails at a measurable rate
   (target: heuristic-judge mean 0.3-0.5 across ≥ 4 runs at
   `temperature=0`) and sonnet reliably passes (target: 0.9+).
2. Ship it at `signal_strength: marginal` initially. Open a PR with
   the workload files + the 4×2 smoke run scoreboard.
3. If the smoke validates a gap ≥ 0.4, the PR also flips the field
   to `signal_strength: high` and updates the §4.1 table.
4. If the smoke does not, document the result in [`RESULTS.md`](../../benchmarks/RESULTS.md)
   and keep the workload at `marginal`. The §A3-rev series may still
   find it useful as a regression sentinel.

The bar for "high" comes from the §A3-rev6 finding: the K-NN's
cluster math needs a stable per-workload delta well above the
heuristic judge's resolution (which clusters at 0.833 / 0.917 /
1.000 in the 13a-1 smoke). 0.4 is the rough minimum a K-NN of 5–10
neighbors can preserve through aggregation while leaving room for
the `min_confidence` gate to fire.

---

## 5. Cost budget

Real-API costs apply. Approximate per-run figures, all in USD, with the
default actual=haiku / baseline=sonnet configuration:

| Run mode                          | Actual cost | Baseline cost (counterfactual, not billed) |
|-----------------------------------|-------------|--------------------------------------------|
| Single workload (smoke)           | ~$0.05–0.20 | n/a (no API call) |
| Full suite (3 workloads)          | ~$0.30–1.00 | n/a |
| Full suite at `--model sonnet` actuals | ~$1.00–3.00 | n/a |
| Full suite at `--model opus` actuals | ~$3.00–5.00 | n/a |

**The baseline does not make API calls.** `/analytics/savings` re-prices each
recorded row's token counts under the baseline model's `PriceTable` rates;
no second LLM run happens. This is what makes the suite cheap enough to run
weekly.

Document these numbers in the harness's `--help`. If a run blows past 2× the
upper bound for its mode, that's a signal to investigate (regressed prompt,
exploded tool-cycle count, etc.).

---

## 6. Reproducibility rules

The harness records and prints provenance so reports are comparable across
runs and machines.

### 6.1 Pinned per run

| Field                  | Where it comes from                                   | Why it matters |
|------------------------|-------------------------------------------------------|----------------|
| `suite_version`        | `workload.yaml.suite_version` (must be `1` in v1)     | Schema migration gate |
| `metis_commit_sha`     | `git rev-parse HEAD` at run start                     | Identifies the agent's behavior |
| `metis_branch`         | `git rev-parse --abbrev-ref HEAD`                     | Context for the SHA |
| `metis_dirty`          | `git status --porcelain` non-empty → `true`           | Flags "ran against uncommitted code" |
| `pricing_version`      | `PriceTable.version` at report time                   | The number on the report |
| `actual_model`         | Canonical id resolved at run start (alias → id)       | Pinned model version |
| `baseline_model`       | Canonical id resolved at run start                    | Pinned baseline |
| `actual_provider`      | Resolved from the registry                            | Sanity-check column |
| `python_version`       | `sys.version`                                         | Reproducibility nicety |
| `started_at`           | UTC ISO timestamp                                     | Report header |
| `ended_at`             | UTC ISO timestamp                                     | Total wall time |
| `temperature`          | Configured per run (default `0.0`)                    | Determinism control |
| `seed_passes`          | `--seed-passes N` (default `1`)                       | N reps per (workload, model); see §6.4 |

### 6.2 Determinism contract

The harness sets `temperature=0` by default (overridable via `--temperature`).
`SessionManager.submit_turn(...)` accepts a `temperature` kwarg that threads
through to `CanonicalRequest.temperature`; adapters that support the parameter
honor it. **This is not strict determinism** — providers reserve the right to
vary outputs even at `temperature=0` (especially on tool-call branches and
under load) — but it's the strongest reproducibility lever available
without going to a recorded-fixture playback model.

Documented expected variance, run-over-run with all pins held:

| Metric                              | Tolerance |
|-------------------------------------|-----------|
| `savings_pct` aggregate             | ±5 absolute pp |
| Per-workload `actual_repriced_usd`  | ±25% relative |
| `llm_call_count` per workload       | ±2 calls (tool-cycle branching) |

If two consecutive clean-DB runs disagree by more than these, suspect a real
behavior change, not noise.

### 6.3 Trace DB isolation

The harness defaults `--db-path` to `benchmarks/.runs/benchmark-<UTC-ts>.db`
and rejects existing files (so the savings projection never mixes in unrelated
events). The default location is git-ignored to keep large trace files out of
commits.

### 6.4 Seed-passes (`--seed-passes N`)

When seeding a shared patterns DB for cross-pass K-NN comparisons (§A3
series), single-shot per-(workload, model) sampling produces 1–2 outcomes
per fingerprint cluster — small enough that one stochastic agent failure
can flip the cluster mean by 0.1–0.3, exceeding the `min_confidence=0.05`
gate that drives slot-4 inversions (see [`benchmarks/RESULTS.md §A3-rev6 Q1
finding`](../../benchmarks/RESULTS.md)).

`--seed-passes N` (default 1) loops each workload N times in the current
pass against the same `--patterns-db-path`. Each rep:

- Gets a fresh ULID-stamped `session_id` (new `runtime.manager.create_session`).
- Copies the workspace src into a fresh tempdir.
- Seeds the workspace's `.metis/patterns.db` from the shared file (after
  rep 1) so the K-NN at rep N sees the prior reps' outcomes.
- Records its outcome and copies the patterns DB back to the shared path.

After the loop, the harness reports per-workload `quality_mean ± quality_std`
across the N reps. Workloads with `std > 0.15` are flagged "NOISY" in the
report and surfaced as candidates for replacement under the 13a-1
signal-strength gate (the workload is too noisy for the K-NN to ever learn
a stable preference — fix the workload, not the routing knob).

**Cost trade-off.** N seed-passes multiplies the actual cost of each
seed-only pass (Pass A / Pass B in the typical §A3 protocol) by N. Pass C
(routing test on the now-richer patterns DB) and Pass D (delegation) are
unaffected. For the typical §A3 four-pass run:

| N | Pass-A+B cost (haiku+sonnet seed) | Pass-C+D cost | Total | Use case |
|---|----------------------------------:|--------------:|------:|----------|
| 1 (default) | ~$0.50 | ~$0.50 | ~$1.00 | single-shot, fastest signal |
| 3 (recommended for A3) | ~$1.50 | ~$0.50 | ~$2.00 | noise-reduced cluster means |
| 5 (cluster-tightening A/B) | ~$2.50 | ~$0.50 | ~$3.00 | std reporting + signal-strength gate |

Per-workload sample size after seeding scales with N: each rep adds one
record per turn fingerprint, so a 3-turn workload at N=3 contributes 3
records to the patterns DB per model — enough for the K-NN cluster mean
to absorb a single stochastic failure without flipping the chooser.

**Validating accumulation.** The harness verifies the shared DB grows: if
after the loop the K-NN's cluster sample size for the test fingerprint
is less than N, that's a recording-path bug (silently dropped record)
and the operator should investigate. The unit test
`test_seed_passes_loop_invokes_run_workload_n_times` exercises this
invariant against the scripted-adapter substrate.

To rerun against a previously-captured DB, pass it explicitly with
`--db-path <existing>` and `--skip-execute` (a no-API mode that only runs the
analytics projection on the existing DB). This is the "did I lose the print"
escape hatch; it doesn't make a new API call.

---

## 7. Secondary metrics

Beyond the headline `savings_pct`, the report includes per-workload:

- `llm_call_count` — from the `turn.completed` events.
- `tool_call_count` — same.
- `total_wall_time_seconds` — clock time from `started_at` to `ended_at` of
  the run, not summed turn latencies (intentionally — wall time is the user's
  experience).
- `cache_hit_rate` — read from `/analytics/cache_effectiveness` if the
  adapter emits cache metadata (currently none do — see
  [`KNOWN_ISSUES.md`](../KNOWN_ISSUES.md) "No prompt-caching strategy").

These are diagnostic, not pass/fail. The benchmark is a savings benchmark;
the other columns answer "why did savings move?"

---

## 8. Report shape

The harness prints a per-workload table and an aggregate summary to stdout,
and writes the full report to `benchmarks/.runs/benchmark-<UTC-ts>.json`.

**Stdout (example):**

```
=== Metis benchmark suite ===
commit:           d79564b (clean)
suite_version:    1
actual_model:     anthropic:claude-haiku-4-5
baseline_model:   anthropic:claude-sonnet-4-6
pricing_version:  2026-05-08
temperature:      0.0
db:               benchmarks/.runs/benchmark-2026-05-13T10-22-08Z.db

Per-workload:
  workload                    turns  llm  tool   actual_$    baseline_$   saved_$  saved_%
  fix-a-bug-small              3     5    4     0.0142       0.0421       0.0279   66.2%
  write-a-doc-from-notes       2     2    1     0.0081       0.0238       0.0157   65.9%
  multi-turn-refactor          5     9    11    0.0418       0.1320       0.0902   68.3%

Aggregate:
  rows_total:                       16
  rows_missing_from_price_table:    0
  actual_repriced_usd:              0.0641
  baseline_repriced_usd:            0.1979
  savings_usd:                      0.1338
  savings_pct:                      67.6%

Run the dashboard against this DB to verify:
  uv run metis serve $(pwd) --db-path benchmarks/.runs/benchmark-2026-05-13T10-22-08Z.db
  open http://127.0.0.1:8421/dashboard
```

**JSON artifact** carries the provenance from [§6.1](#61-pinned-per-run) plus
the per-workload and aggregate fields above plus the raw
`/analytics/savings` response per workload.

---

## 9. Implementation notes

1. **Run analytics in-process.** [`scripts/benchmark.py`](../../scripts/benchmark.py)
   instantiates `AnalyticsStore` directly against the trace DB; it does not
   start the HTTP server. This avoids spinning up uvicorn for a one-shot
   report. The dashboard agreement [§2.1.4](#21-goals) is by construction
   because the HTTP handler delegates to the same `AnalyticsStore.savings()`.
2. **Per-workload window.** Each workload run captures its
   `started_at` / `ended_at` micros; the aggregate report calls
   `AnalyticsStore.savings(window=(min(start), max(end)), baseline=...)` to
   cover the full suite, and per-workload it calls with the workload's own
   window. SQLite's `BETWEEN` over the indexed `(type, timestamp_us)` covers
   this in a single scan.
3. **Workspace isolation.** Each workload tree is copied with
   `shutil.copytree(...)` into a `TemporaryDirectory` and the
   `workspace_path` passed to `SessionManager.create_session()` points at the
   copy. The copy is removed in a `try / finally` regardless of run outcome.
4. **Exit codes.** Harness exits non-zero if any soft assertion fails (per-turn
   substring miss, aggregate `max_total_cost_usd` exceeded) **or** if any turn
   raised. Exit codes:
   - `0` — every workload ran clean, assertions held.
   - `1` — one or more assertions failed; the report still printed.
   - `2` — setup error (missing API key, bad workload file).

---

## 10. Testing strategy

V1's "tests" for the spec are the harness and its workloads. Unit tests cover
the schema validator (see [§3.1](#31-workloadyaml-schema)) — accept the three
shipped workloads, reject malformed YAML, reject unknown keys. End-to-end is
a real-API smoke (one workload at `--model haiku`) on demand, not in CI, per
[§2.2](#22-non-goals).

### 10.1 Required unit tests

1. **Schema accepts shipped workloads.** Each of the three workloads under
   [`benchmarks/workloads/`](../../benchmarks/workloads/) loads without error.
2. **Schema rejects unknown top-level keys.** Loading a workload with an
   extra `foo: bar` field at the top level raises a clear error.
3. **Schema rejects unknown `expect` keys.** Same for per-turn and aggregate
   `expect` fields.
4. **Workspace copy is hermetic.** A workload whose script edits a fixture
   file leaves the in-tree fixture unchanged.
5. **Report sums match per-workload sum.** Aggregate `actual_repriced_usd`
   equals the sum of per-workload `actual_repriced_usd` (exact `Decimal`).

---

## 11. Open questions

These are **live**. Do not unilaterally close them.

1. **Should the harness commit a "golden" report file?** Tempting (catches
   regressions instantly), risky (LLM variance breaks goldens within a few
   weeks, churns PRs). Lean: no — tolerances per [§6.2](#62-determinism-contract)
   are the regression contract.
2. **Should `multi-turn-refactor` use the real `edit_file` tool?** Currently
   yes; the alternative is to mock the tool for determinism. Picking yes
   because the savings story includes the cost of tool-cycle iterations, and
   mocking would erase that.
3. **Should `metis serve --db-path` be advertised?** Today the dashboard
   path requires knowing the DB. Adding `metis benchmark` as a CLI subcommand
   ([`apps/cli/src/metis_cli/main.py`](../../apps/cli/src/metis_cli/main.py))
   would close the loop. Out of scope for v1.
4. **Workload v2 candidates.** `code-explanation`, `shell-driven-debug`,
   `long-multi-doc-summarize` are all plausible. Wait for the v1 suite's
   numbers to settle before adding noise.
5. **Cross-provider workloads.** Run the suite once per provider
   (anthropic / openai / openrouter), each scored against its own
   provider-mate baseline (e.g. haiku vs sonnet within anthropic;
   gpt-5-mini vs gpt-5 within openai). Cleaner story per provider.
   Defer until single-provider runs are stable.

---

## 12. Decision log

| Date       | Decision                                                       | Rationale                                                                                  |
|------------|---------------------------------------------------------------|--------------------------------------------------------------------------------------------|
| 2026-05-13 | YAML for workload files                                       | Multi-line prose prompts and PR diffs read better than JSON; msgspec.yaml validates shape. |
| 2026-05-13 | Bundled fixture workspaces, not the metis repo                | Hermetic; results don't drift with repo state.                                             |
| 2026-05-13 | Baseline is re-priced, not re-executed                        | A re-run baseline doubles cost; analytics-api.md §4.7 already re-prices honestly.          |
| 2026-05-13 | `temperature=0` by default, plumbed via `submit_turn`         | Strongest reproducibility lever without recorded-playback.                                 |
| 2026-05-13 | Three workloads in v1                                         | Enough variation to avoid single-workload accident; few enough to stay under the cost ceiling. |
| 2026-05-13 | Soft floors / hard ceilings, no goldens                       | LLM variance breaks goldens; tolerance windows catch real regressions without churn.       |
| 2026-05-13 | Run analytics in-process (not via HTTP)                       | Avoids uvicorn lifecycle in a one-shot script; dashboard agreement is by construction.     |
| 2026-05-13 | Quality scoring deferred to the evaluator                     | Benchmark v1 measures spend, not correctness — evaluator's job per the project strategy (private)       |
| 2026-05-15 | `signal_strength: high \| marginal` partition + `--include-marginal` flag | §A3-rev6 Q1 finding ([`RESULTS.md`](../../benchmarks/RESULTS.md)): the per-workload haiku-vs-sonnet quality gap in the v1 suite is within run-to-run variance. v2 splits the suite by smoke-validated gap so the default run trains the K-NN only on high-signal workloads. 13a-1 smoke (2026-05-15) tested 3 candidate workloads; none cleared the 0.4 gate, so the default suite ships empty pending future candidates. |
| 2026-06-03 | `--batch-mode` + `--collect-batch` two-pass workflow (§3.4) | Captures the documented 50% Anthropic Batches API discount on benchmark re-runs per [provider-adapter-contract.md §4.6](provider-adapter-contract.md). Two-pass workflow (submit and exit, then poll + ingest) matches the provider's "best-effort 24h" turnaround. Each turn prompt becomes one independent batch entry — tool-using workloads land as cost-comparison probes rather than end-to-end agent runs; sync mode remains canonical for quality scoring. Wave 18a-3. |

---

## 13. References

- [`analytics-api.md`](analytics-api.md) — `/analytics/savings` response shape;
  this spec's headline number is one field of one of its responses.
- [`event-bus-and-trace-catalog.md`](event-bus-and-trace-catalog.md) —
  `llm.call_completed`, `turn.completed` are the rows the savings projection
  sums.
- [`canonical-message-format.md`](canonical-message-format.md) — on-disk
  schema for `events`, `messages`, `sessions`.
- [`provider-adapter-contract.md`](provider-adapter-contract.md) (planned) —
  the contract for `CanonicalRequest.temperature` honored across adapters.
- `../the project strategy (private)` — the open question this spec
  closes.
- [`../KNOWN_ISSUES.md`](../KNOWN_ISSUES.md) — prompt-caching gap; the
  benchmark's `cache_hit_rate` column doubles as a forcing function.
- [`scripts/smoke.py`](../../scripts/smoke.py),
  [`scripts/smoke_cross_provider.py`](../../scripts/smoke_cross_provider.py) —
  shape reference for the real-API harness pattern this spec extends.
