# Benchmark Specification

**Status:** Draft v1
**Last updated:** 2026-05-13

> Defines the workload suite, baseline, and measurement methodology that turn
> `analytics-api.md /analytics/savings.actual_repriced_usd` and
> `baseline_repriced_usd` into a credible "Metis saved you X%" number — the
> artifact [`STRATEGY.md §6.4`](../STRATEGY.md) names as "currently the biggest
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
   task correctly?" is the evaluator's job ([`STRATEGY.md §6.7`](../STRATEGY.md));
   benchmark v1 asserts that turns *completed* and *exercised expected events*,
   not that the answers were good. A workload whose outputs are wrong but cost
   the right amount still scores. This is a known v1 limitation.
3. **No multi-tenant / multi-user simulation.** Single workspace, single
   session per workload. Multi-user pricing is downstream of the
   [`STRATEGY.md §3`](../STRATEGY.md) replacement-agent-vs-gateway fork.
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
```

**Schema enforcement.** The harness validates the YAML against this shape at
load time using `msgspec.yaml.decode` against a `Workload` struct. Unknown
top-level keys are rejected; unknown `expect` keys are rejected. This forces
schema migrations to flow through this spec.

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

---

## 4. The suite

V1 ships **three workloads** covering the dev-task shapes most representative
of buyer use:

| Workload                 | Shape                              | Turns | Notes |
|--------------------------|------------------------------------|-------|-------|
| `fix-a-bug-small`        | Find + fix a bug in a tiny python module | 2-3   | Exercises read/edit tools; multi-turn so the model has to use prior context. |
| `write-a-doc-from-notes` | Read raw notes, produce a structured doc | 2     | Exercises read + write tools; mid-length output. |
| `multi-turn-refactor`    | Rename a function across 3 files in a small repo | 4-6   | Long-context, repeated tool calls, multi-turn dependence — the workload most sensitive to context / cache discipline. |

V2 may add `code-explanation` (read + summarize, no edits) and
`shell-driven-debug` (exercise `run_shell` heavily). Out of scope for v1 to
keep the per-run cost bounded ([§5](#5-cost-budget)).

The choice to ship three is deliberate: enough variation that the aggregate
isn't a single workload's accident, few enough that the full run stays under
the [§5](#5-cost-budget) cost ceiling.

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
| 2026-05-13 | Quality scoring deferred to the evaluator                     | Benchmark v1 measures spend, not correctness — evaluator's job per STRATEGY.md §6.7.       |

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
- [`../STRATEGY.md §6.4`](../STRATEGY.md) — the open question this spec
  closes.
- [`../KNOWN_ISSUES.md`](../KNOWN_ISSUES.md) — prompt-caching gap; the
  benchmark's `cache_hit_rate` column doubles as a forcing function.
- [`scripts/smoke.py`](../../scripts/smoke.py),
  [`scripts/smoke_cross_provider.py`](../../scripts/smoke_cross_provider.py) —
  shape reference for the real-API harness pattern this spec extends.
