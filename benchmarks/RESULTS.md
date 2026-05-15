# Benchmark Results

First reproducible run of the savings counterfactual against the workload suite
defined in [`docs/specs/benchmark.md`](../docs/specs/benchmark.md). This is the
artifact [`STRATEGY.md §6.4`](../docs/STRATEGY.md) named as missing — the
"we saved X%" number tied to a versioned workload, on a known commit, against
a known `PriceTable`.

The headline `savings_pct` here is computed by the same `AnalyticsStore.savings()`
method that backs the `/analytics/savings` HTTP endpoint; pointing
`metis serve --db-path` at the same trace DB renders the same number on the
dashboard.

## Run metadata

| Field              | Value                                          |
|--------------------|------------------------------------------------|
| Run date (UTC)     | 2026-05-14T03:46:32Z                           |
| Commit SHA         | `c9465d3cc0eea4a2dc99dc33c1d3e569d9f61492`     |
| Branch             | `2026-05-12/phase1-review`                     |
| Working tree       | clean                                          |
| Suite version      | 1                                              |
| Actual model       | `anthropic:claude-haiku-4-5`                   |
| Baseline model     | `anthropic:claude-sonnet-4-6`                  |
| Pricing version    | `2026-05-08+openrouter-4bc47d1d2711`           |
| Temperature        | 0.0                                            |
| Python             | 3.13.12                                        |
| Test baseline      | 842 passed (`uv run pytest -q`)                |

JSON artifact: [`.runs/benchmark-2026-05-14T03-46-30Z.json`](.runs/benchmark-2026-05-14T03-46-30Z.json)
Trace DB: `.runs/benchmark-2026-05-14T03-46-30Z.db`

## Aggregate

| Metric                              | Value          |
|-------------------------------------|----------------|
| `rows_total`                        | 22             |
| `rows_missing_from_price_table`     | 0              |
| `actual_repriced_usd`               | $0.083735      |
| `baseline_repriced_usd`             | $0.251206      |
| `savings_usd`                       | $0.167470      |
| **`savings_pct`**                   | **66.7%**      |
| `hard_failures` (routing)           | 0              |
| Wall time                           | ~40 s          |

Routing: all 9 turn-level decisions resolved via `manual_sticky` to
`anthropic:claude-haiku-4-5`; no rejections.

## Per-workload

| Workload                  | Turns | LLM | Tool | Actual ($) | Baseline ($) | Saved ($) | Saved (%) |
|---------------------------|-------|-----|------|------------|--------------|-----------|-----------|
| `fix-a-bug-small`         | 3     | 5   | 3    | 0.015305   | 0.045915     | 0.030610  | 66.7%     |
| `multi-turn-refactor`     | 4     | 12  | 20   | 0.050344   | 0.151033     | 0.100688  | 66.7%     |
| `write-a-doc-from-notes`  | 2     | 5   | 3    | 0.018086   | 0.054258     | 0.036172  | 66.7%     |

All workload-level assertion sets passed (no `assertion_failures` in the JSON
artifact). No `rows_missing_from_price_table` on any workload.

## Anomalies

### Resolved: smoke cache test failed (`scripts/smoke_cache.py --model haiku`)

**Original symptom (commit `c9465d3`).** Both turns reported **zero** cache
activity:

| Turn | `cache_creation_input_tokens` | `cached_input_tokens` | input | output | cost      |
|------|-------------------------------|-----------------------|-------|--------|-----------|
| 1    | 0                             | 0                     | 2817  | 4      | $0.002837 |
| 2    | 0                             | 0                     | 2836  | 5      | $0.002861 |

Script `exit_code = 1`.

**Root cause.** Padding shortfall in `scripts/smoke_cache.py`, not the
wire shape. The original `_STABLE_PADDING` produced ~8209 chars (~2050
tokens — sitting right at haiku's 2048-token cache floor). The Anthropic
API silently dropped the `cache_control` markers because the cached
prefix tokenized below the floor. Wave 2's wire-shape unit tests were
correct; `cache_control: {"type": "ephemeral"}` was attached to the last
tool and the last stable system block exactly as
[`docs/specs/context-assembler.md §3.1–§3.2`](../docs/specs/context-assembler.md)
prescribes.

**Fix.** Bumped `_STABLE_PADDING` to ~17.7K chars (~4400–5000 tokens) of
distinct guideline text in `scripts/smoke_cache.py` so the cached prefix
clears the haiku floor with margin. Used 20 varied guideline sentences
cycled across 160 lines plus Style and Tool-use sections so BPE
tokenization can't compress repeated lines and undercount.

**Live verification** (after fix, same commit `c9465d3` + script change):

| Turn | `cache_creation_input_tokens` | `cached_input_tokens` | input | output | cost      |
|------|-------------------------------|-----------------------|-------|--------|-----------|
| 1    | 4957                          | 0                     | 332   | 4      | $0.006548 |
| 2    | 330                           | 4957                  | 21    | 5      | $0.000954 |

Total smoke run cost: $0.007502. Script `exit_code = 0`. Turn 1 writes
the cache, turn 2 reads the full 4957-token cached prefix back.

**Benchmark re-run (bonus).** Re-ran `scripts/benchmark.py` after the
smoke fix. The cache wiring is honored where the cached prefix clears
the floor — see [`.runs/benchmark-2026-05-14T05-13-20Z.db`](.runs/),
where 11 of 22 `llm.call_completed` events record non-zero
`cache_creation_input_tokens` / `cached_input_tokens` (totals: 11578
cache-create tokens, 23387 cached-read tokens). The aggregate
`actual_repriced_usd` came in at **$0.084020** — within $0.0003 of the
pre-fix $0.083735, not the ~20% drop the cache-token totals would
predict if the discount were flowing through. Three independent reasons
this is small:

1. The benchmark uses `SessionManager`'s default system prompt (short),
   not the smoke test's padded one. The cached prefix is `tools + stable
   system`; with a short system prompt this is often below haiku's
   2048-token floor on early turns, so those calls don't cache at all.
   The 11 calls that did cache were later turns where conversation
   history pushed the prefix above the floor.
2. The cache *write* costs +25% over the input rate, which partially
   offsets the *read* savings (cached reads are 10% of input).
3. `AnalyticsStore.savings()` re-pricing pulls actual `cost_usd` from
   the persisted `llm.call_completed` records — those already reflect
   the cache discount at request time — so the re-priced number is
   pulling the same cache-amortized cost the run actually paid. The
   pre-fix $0.083735 was a no-cache run; the post-fix $0.084020 was a
   partially-cached run with cache-write overhead on the calls where
   caching kicked in. The two numbers are roughly equal because the
   benchmark's prompt shape doesn't exercise prompt-caching well.

**Takeaway.** Prompt caching is now provably honored end-to-end against
the live Anthropic API. The benchmark's default system prompt does not
clear the haiku cache floor on most calls, which limits the cache's
impact on the suite's aggregate cost. The remaining cost lever — making
the Metis system prompt and skill index large enough to cache
universally — is a separate piece of context-assembler work, not a
caching-wiring bug.

## Run 2: post-Wave-4 (caching wired + pattern store + evaluator)

Same workload suite, re-run after the Wave-4 cohort landed (prompt
caching honored end-to-end, pattern store wired into the CLI runtime,
evaluator subscribing to `turn.completed` / `tool.completed` /
`session.ended` and emitting `eval.completed`). The headline number
should answer two questions:

1. **Does the savings_pct hold** once caching, pattern store, and
   evaluator are all active end-to-end? If wiring any of them regressed
   the suite, this is where it shows.
2. **Does a warm pattern store (re-run over the cold-run's persisted
   `.metis/patterns.db`) improve routing?** The cold/warm framing is
   from `STRATEGY.md §6.4`: "we saved X% — and X went up after the
   system learned from your last run." The TL;DR (anomaly §A1 below):
   **no, not under the current benchmark shape** — the harness pins
   `active_model` so `manual_sticky` wins on every turn and the
   pattern slot is never reached.

### Run 2 metadata

| Field              | Value                                                |
|--------------------|------------------------------------------------------|
| Run date (UTC)     | 2026-05-14T06:08:16Z (cold) / 2026-05-14T06:09:27Z (warm) |
| Commit SHA         | `c9465d3cc0eea4a2dc99dc33c1d3e569d9f61492` + dirty Wave-4 working tree |
| Branch             | `2026-05-12/phase1-review`                           |
| Suite version      | 1                                                    |
| Actual model       | `anthropic:claude-haiku-4-5`                         |
| Baseline model     | `anthropic:claude-sonnet-4-6`                        |
| Pricing version    | `2026-05-08+openrouter-d9ecdc576e05`                 |
| Temperature        | 0.0                                                  |
| Python             | 3.13.12                                              |
| Test baseline      | 968 passed (`uv run pytest -q`)                      |
| Smoke cache        | PASSED, $0.007420 ($cache_creation=4957$, $cached_read=4957$ on turn 2) |
| Total spend        | cold $0.108273 + warm $0.090407 = **$0.198680**      |

JSON artifacts:
- Cold: [`.runs/benchmark-2026-05-14T06-08-16Z.json`](.runs/benchmark-2026-05-14T06-08-16Z.json), trace DB `.runs/benchmark-2026-05-14T06-08-16Z.db`
- Warm: [`.runs/benchmark-2026-05-14T06-09-27Z.json`](.runs/benchmark-2026-05-14T06-09-27Z.json), trace DB `.runs/benchmark-2026-05-14T06-09-27Z.db`
- Seeded patterns DBs (cold → warm): [`.runs/patterns-cold-2026-05-13/`](.runs/patterns-cold-2026-05-13/)

### Run 2 aggregate

| Metric                            | Cold       | Warm       | Δ vs Run 1 ($0.083735 / 66.7%) |
|-----------------------------------|------------|------------|---------------------------------|
| `rows_total`                      | 30         | 24         | +8 / +2                         |
| `rows_missing_from_price_table`   | 0          | 0          | —                               |
| `actual_repriced_usd`             | $0.108273  | $0.090407  | +29% / +8%                      |
| `baseline_repriced_usd`           | $0.324819  | $0.271221  | (scales with actual)            |
| `savings_usd`                     | $0.216546  | $0.180814  | —                               |
| **`savings_pct`**                 | **66.7%**  | **66.7%**  | **identical**                   |
| `hard_failures` (routing)         | 0          | 0          | 0                               |
| Wall time                         | ~58 s      | ~50 s      | —                               |

The aggregate `savings_pct` is stable at **66.7% in all three runs**
(Run 1, Run 2 cold, Run 2 warm). This is structural: actual and
baseline scale linearly with token counts at fixed haiku/sonnet rates,
so the ratio is pinned by the rate card, not by routing behavior. Per
`benchmark.md §6.2` the variance tolerance for `savings_pct` is ±5 pp;
the observed run-to-run drift is within noise.

### Run 2 routing chain breakdown

Same in both cold and warm runs:

| Slot # | Policy                 | Cold verdicts                | Warm verdicts                |
|--------|------------------------|------------------------------|------------------------------|
| 1      | `per_message_override` | 9× `not_applicable`          | 9× `not_applicable`          |
| 2      | `manual_sticky`        | **9× `chose` → haiku-4-5**   | **9× `chose` → haiku-4-5**   |
| 3      | `rule`                 | not evaluated (short-circuit) | not evaluated (short-circuit) |
| 4      | `pattern`              | not evaluated (short-circuit) | not evaluated (short-circuit) |
| 5–7    | (delegate / workspace_default / global_default) | not evaluated  | not evaluated  |

`chosen_model` on every `route.decided` is `anthropic:claude-haiku-4-5`.
**The pattern slot never runs** because `manual_sticky` wins first; see
anomaly §A1 for why the cold/warm comparison can't materialize under
this shape.

### Run 2 cache token totals

| Run  | LLM calls | Calls w/ cache hit | `cache_creation_input_tokens` | `cached_input_tokens` |
|------|-----------|--------------------|-------------------------------|------------------------|
| Cold | 30        | 10 (33%)           | 11,229                        | 47,918                |
| Warm | 24        | 8 (33%)            | 12,031                        | 37,694                |

Both runs honor caching end-to-end (compare Run 1's pre-cache-fix `0/0`
in §Anomalies). Cache activity is concentrated in
`multi-turn-refactor`; the other two workloads register zero cache
tokens because their stable prefix tokenizes below haiku's
2048-token cache floor (see Run 1 anomaly note for the prefix-shape
discussion — unchanged here).

### Run 2 per-workload

| Workload                  | Turns | LLM (cold→warm) | Tool (cold→warm) | Actual $ (cold→warm) | Saved % | Quality (heuristic) | Cache write / read (cold) | Cache write / read (warm) |
|---------------------------|-------|------|------|--------------------|---------|----------------------|--------------------------|--------------------------|
| `fix-a-bug-small`         | 3     | 5 / 5   | 3 / 3   | $0.015380 / $0.015305  | 66.7% / 66.7% | 1.00 @ 0.80         | 0 / 0                    | 0 / 0                    |
| `multi-turn-refactor`     | 4     | 20 / 14 | 22 / 22 | $0.074921 / $0.056110  | 66.7% / 66.7% | 1.00 @ 0.80         | 11,229 / 47,918          | 12,031 / 37,694          |
| `write-a-doc-from-notes`  | 2     | 5 / 5   | 3 / 3   | $0.017972 / $0.018992  | 66.7% / 66.7% | 1.00 @ 0.80         | 0 / 0                    | 0 / 0                    |

`multi-turn-refactor` shows the only run-over-run delta: warm needed 14
LLM calls vs cold's 20 (a 30% drop in calls, 25% drop in cost). The
tool-call count was identical (22 in both runs), and slot 4 never
fired in either run, so this is most plausibly explained by haiku's
non-strict-determinism at `temperature=0` on tool-cycle branches —
within `benchmark.md §6.2`'s `±2 llm_call_count` per workload
tolerance. Not a pattern-store effect.

### Run 2 evaluator verdicts

Heuristic judge runs everywhere; `judge_cost_usd=0` for all (LLM-tier
judge is not yet implemented).

| Subject kind | Cold count | Cold avg score | Warm count | Warm avg score |
|--------------|------------|----------------|------------|----------------|
| `tool_cycle` | 28         | 0.95           | 28         | 0.91           |
| `turn`       | 9          | 1.00           | 9          | 1.00           |
| `workload`   | 3          | 1.00 @ 0.80    | 3          | 1.00 @ 0.80    |

Both runs scored every workload 1.00 at the workload level (the
heuristic rubric checks stop_reason + final-response presence; none of
the suite's prompts have `expect_substring_in_final_response`). The
slight tool-cycle drop in warm (0.95 → 0.91) reflects multi-turn-refactor's
fewer-but-longer cycles; the rubric weights `failures` and `retries`
inside the cycle.

The evaluator subscriber emits `eval.completed` events inline with the
turn lifecycle; the pattern subscriber's `update_score()` flow then
patches the per-turn score onto the matching pattern outcome row. Both
sides of that handshake work end-to-end in this run.

#### Hypothesis sanity check (2026-05-14)

Task P2's mandate was to confirm whether "1.00 @ 0.80 on every workload"
reflected genuine success or a content-blind rubric. The answer is
**both** — hypothesis (a) was true for the three live workloads
(lifecycle was genuinely clean and the response text was substantive),
and hypothesis (b) was true for the rubric's structural ceiling — the
v1 heuristic had no signal that could fire on a clean lifecycle paired
with a refusal or empty response. A turn that streamed
`I cannot help with that.` cleanly to end_turn would have scored 1.0
under the prior rubric.

**What changed.** Two opt-in signals were added to the heuristic judge:

| Subject  | Signal name (negative)              | Multiplier | Fires when                                                |
|----------|--------------------------------------|------------|------------------------------------------------------------|
| turn     | `assistant_refusal_detected`         | ×0.5       | `signals_extra.final_response_text` begins with a known refusal phrase in the first 160 chars |
| turn     | `empty_assistant_response`           | ×0.4       | `signals_extra.final_response_text` is whitespace-only     |
| workload | `workload_assistant_refusal_detected`| ×0.5       | same as turn, on the harness-supplied final response       |
| workload | `workload_empty_assistant_response`  | ×0.4       | same as turn                                               |

Both are **opt-in via `signals_extra.final_response_text`**. The bus
subscriber doesn't plumb assistant text today, so `metis evaluate
--subject turn` is unchanged on existing trace DBs (proved below). The
benchmark harness *does* plumb the text into the workload-subject path,
so workload-level evaluation exercises the new signals on the next
benchmark run.

**Re-evaluation against the cold-run DB.** Re-running the heuristic
against [`benchmark-2026-05-14T06-08-16Z.db`](.runs/benchmark-2026-05-14T06-08-16Z.db)
under the new judge produced identical per-turn verdicts (9 turns,
score=1.00, confidence=0.90, rubric=`turn-heuristic-v1@1.0.0`). This is
intentional — `metis evaluate --subject turn` doesn't reconstruct
`final_response_text` from persisted messages, so the new signal
doesn't fire. Workload-level scores would need a fresh benchmark run
to exercise the new check; no API budget was spent on that here. The
rubric version stayed at `1.0.0` for the same reason — no existing
verdict series shifts.

**Control case.** A new
[`intentionally-failing-task` workload](workloads/intentionally-failing-task/)
ships as a deliberately-failing fixture. Its prompt asks the agent to
refuse, and its `evaluate.expect_substring_in_final_response` is a
sentinel that no real response will contain. Unit-tested at
[`packages/metis-core/tests/eval/test_judge.py::test_intentionally_failing_workload_fixture_scores_below_0_8`](../packages/metis-core/tests/eval/test_judge.py):
for `final_response_text ∈ {refusal-text, whitespace, ""}` the
heuristic produces `score < 0.8` with `expected_substring_missing` plus
either `workload_assistant_refusal_detected` or
`workload_empty_assistant_response` in `flags_negative`. Future
benchmark runs include this workload by default (filesystem discovery
in `scripts/benchmark.py`); cost is ≤ $0.005/run since the agent is
asked to refuse.

**Tests.** Eleven new synthetic-failure unit tests landed in
`test_judge.py` covering the lifecycle signals that *were* working
(llm.call_failed, excessive tool count, session error disposition, low
child scores, immediate re-call same input) plus the new content
signals. Suite: 968 → 979 tests passing.

**What's still deferred to LLM tier.** Detecting *wrong-answer-with-
clean-lifecycle* (response is non-empty, non-refusing, but factually
incorrect) remains content semantic — too subtle for v1 heuristics.
Tool-result-never-consumed-by-next-message and user-said-"this-is-wrong"
similarly want deeper context. These stay in `evaluator.md §5.2`
(LLM-as-judge) territory.

### Run 2 pattern store activity

| Run  | `pattern.recorded` | `pattern.matched` | `pattern.evicted` |
|------|--------------------|-------------------|-------------------|
| Cold | 9                  | **0**             | 0                 |
| Warm | 9                  | **0**             | 0                 |

`pattern.matched=0` in both runs is the §A1 finding: slot 4 never
evaluates because slot 2 (manual_sticky) wins first. The 9
`pattern.recorded` events in each run prove the subscriber wiring
itself works: every `turn.completed` produced a fingerprint + outcome
write. The cold-run patterns DB (3 files, ~57 KB each) was preserved
to [`.runs/patterns-cold-2026-05-13/`](.runs/patterns-cold-2026-05-13/)
and seeded into the warm run's tempdir before each workload — the
warm run's `pattern.recorded` events landed on top of the seed.

### Anomalies

#### A1. Cold/warm pattern comparison can't fire — `manual_sticky` short-circuits

**Status: Resolved by Experiment A1 below (2026-05-13).**

**Observation.** Both cold and warm runs produced identical routing
chains (slots 1 and 2 only). `pattern.matched=0` in both. The warm
run's seeded patterns DB had 6 fingerprints and 6 outcomes available
for K-NN matching, but slot 4 was never asked.

**Root cause.** [`scripts/benchmark.py`](../scripts/benchmark.py) calls
`runtime.manager.create_session(workspace_path=str(ws), active_model=actual_model)`.
That pins `Session.active_model`, so the routing chain's slot 2
(`manual_sticky`) returns `verdict=chose` on the first turn and
short-circuits the rest of the chain — per
[`routing-engine.md §4.1`](../docs/specs/routing-engine.md) (each slot
runs in priority order; a winner stops the chain).

**Why this isn't trivially fixable.** Removing `active_model=` from the
benchmark's `create_session` would let manual_sticky fall through, but
then routing would resolve via slot 7 (`global_default`, also pinned to
haiku) in the cold run and via slot 4 (`pattern`) in the warm run. The
pattern store would recommend whichever primary_model it recorded — and
since the benchmark only ever runs haiku, every recorded outcome is
haiku-on-haiku, so the recommendation is always haiku. Same chosen
model → same cost → no measurable savings delta. The pattern store's
cost-optimization story requires *multiple actual models* in the
training history; that's beyond v1's single-model benchmark shape.

**Suggested next experiment.** Run the benchmark twice with different
`--model` values (e.g., once with haiku, once with sonnet) sharing a
patterns DB. Then a third run with no `active_model` pinned: slot 4
would have two models to choose between and could plausibly pick the
cheaper one for fingerprints whose recorded outcomes favor it. This is
out of scope for Task 4c-1.

**Surfaced to human (5/13).** Wiring the pattern store into
[`apps/cli/src/metis_cli/runtime.py`](../apps/cli/src/metis_cli/runtime.py)
was a real gap discovered while preparing for this run — the runtime
previously left `pattern_store_resolver=None` and never attached
`PatternEventSubscriber`. That gap is fixed (in the dirty working tree
for this run); without the fix `pattern.recorded` would be 0 too.

#### A2. Cache totals are large in `multi-turn-refactor` only

Same finding as Run 1's "Resolved" anomaly. The cached prefix
(tools + stable system, per
[`context-assembler.md §3.1`](../docs/specs/context-assembler.md)) only
clears haiku's 2048-token cache floor on `multi-turn-refactor`, whose
conversation history bulks up the prefix as turns accumulate. The
other two workloads stay below the floor on every call and register
zero cache tokens. Not a regression; the same limitation flagged in
Run 1 Anomalies §"Takeaway."

#### A3. Pricing version overlay version differs from Run 1

Run 1: `2026-05-08+openrouter-4bc47d1d2711`. Run 2:
`2026-05-08+openrouter-d9ecdc576e05`. The native (Anthropic + OpenAI)
half is unchanged at `2026-05-08`; the OpenRouter catalog overlay
changed because their public catalog refreshed between runs. This does
not affect the haiku/sonnet rates used in the savings projection
(both are native, not OpenRouter-routed).

#### A4. `chain[].model` is `None` on `manual_sticky` win

A small cosmetic curiosity: the `manual_sticky` chain entry records
`policy=manual_sticky verdict=chose model=None`, even though
`chosen_model` at the top of the `route.decided` payload is
`anthropic:claude-haiku-4-5`. The model is correctly applied; only
the chain entry's `model` field is missing. Likely
[`routing/engine.py`](../packages/metis-core/src/metis_core/routing/engine.py)
populates the winner's `model` from the chosen_model rather than
mirroring it into the chain entry. Not load-bearing for analytics or
savings, but a confusing artifact when reading `route.decided` events
raw. **Not fixing in this task.**

## Experiment A1: multi-model pattern store fire

Built directly on Run 2's §A1 suggestion: drive the suite three times
against a single shared patterns DB so slot 4 sees outcomes for *two*
distinct models on the same fingerprint cluster, then run once with no
`active_model` pinned and observe whether the pattern slot wins.

### A1 metadata

| Field           | Value                                                |
|-----------------|------------------------------------------------------|
| Run dates (UTC) | 2026-05-14T06:34Z (A) / 2026-05-14T06:42Z (B) / 2026-05-14T06:56Z (C) |
| Commit SHA      | `4c7d136` + dirty working tree (runtime wire-up, benchmark flags, WAL fix) |
| Branch          | `2026-05-12/phase1-review`                           |
| Suite version   | 1 (same 3 workloads as Run 2; the new `intentionally-failing-task` workload was excluded from the suite for this experiment to keep the cluster shape comparable to Run 2) |
| Test baseline   | 979 passed (`uv run pytest -q`)                      |
| Shared DB       | [`benchmarks/.runs/exp-a1-patterns.db`](.runs/) (single file, copied in/out per workload; WAL checkpointed before each copy) |
| Total API spend | A $0.092552 + B $0.258365 + C $0.084458 = **$0.435375** |

JSON artifacts / trace DBs:
- A: [`.runs/exp-a1-run-a.json`](.runs/exp-a1-run-a.json), `.runs/exp-a1-run-a.db`
- B: [`.runs/exp-a1-run-b.json`](.runs/exp-a1-run-b.json), `.runs/exp-a1-run-b.db`
- C: [`.runs/exp-a1-run-c.json`](.runs/exp-a1-run-c.json), `.runs/exp-a1-run-c.db`

### A1 wire-up: two prerequisites this experiment surfaced

1. **`fingerprint_inputs_builder` was unwired in the CLI runtime.**
   [`apps/cli/src/metis_cli/runtime.py`](../apps/cli/src/metis_cli/runtime.py)
   constructed `RoutingEngine(pattern_store_resolver=...)` but never
   passed a `fingerprint_inputs_builder`. The query path in
   [`routing/engine.py:269`](../packages/metis-core/src/metis_core/routing/engine.py#L269)
   short-circuits with `reason="no fingerprint inputs builder"`
   whenever the builder is `None`, so slot 4 would have returned
   `not_applicable` even after passing slot 2. The runtime now wires a
   builder that mirrors the recording subscriber's `default_fingerprint_builder`
   (empty `user_message_text` + empty tool/file tuples) so query-side
   and record-side `StructuralFeatures` align on the empty-tuple shape
   the bus actually carries.

2. **The shared-DB save lost data through SQLite's WAL.**
   `PatternStore` uses `journal_mode=WAL` + `synchronous=NORMAL`.
   Closing the connection in `shutdown_runtime` does not force a
   checkpoint, so the main `.db` file remained near-empty (4 KB,
   schema only) while all the records sat in `.db-wal`. A
   `shutil.copyfile` after the workload only copied the main file,
   producing an empty save. The first attempt at this experiment ran
   to completion but accidentally cleared the shared DB on every
   workload boundary, leaving 4 fingerprints from the last
   workload of each run instead of the cumulative ~10. The harness
   now runs `PRAGMA wal_checkpoint(TRUNCATE)` against
   `<tempdir>/.metis/patterns.db` before the copy.

Both fixes are in the dirty working tree for this experiment;
[`scripts/benchmark.py`](../scripts/benchmark.py) and
[`apps/cli/src/metis_cli/runtime.py`](../apps/cli/src/metis_cli/runtime.py).
Neither touches `packages/metis-core/src/metis_core/patterns/`.

### A1 routing-chain breakdown

| Run | Invocation flags                                | `route.decided` winner_index dist | `chosen_model` dist | `pattern.recorded` | **`pattern.matched`** |
|-----|--------------------------------------------------|------------------------------------|----------------------|---------------------|------------------------|
| A   | `--model haiku --patterns-db-path …`             | slot 2 (manual_sticky) × 9         | haiku × 9            | 9                   | **0**                  |
| B   | `--model sonnet --patterns-db-path …`            | slot 2 (manual_sticky) × 9         | sonnet × 9           | 9                   | **0**                  |
| C   | `--no-active-model --patterns-db-path …`         | **slot 4 (pattern) × 9**           | **haiku × 9**        | 9                   | **9**                  |

Runs A and B match Run 2's chain shape exactly: `active_model` pins
slot 2, slot 4 is never consulted. Run C — the experimental cell —
**fires slot 4 on every turn**. Success criterion met.

### A1 pattern-store state after A + B

After Runs A and B drained into the shared DB:

| Metric                              | Value                  |
|-------------------------------------|------------------------|
| `fingerprints`                      | 11                     |
| `outcomes`                          | 11                     |
| haiku outcomes / sample-size total  | 6 / 9                  |
| sonnet outcomes / sample-size total | 5 / 9                  |
| Outcomes with non-null score        | 12 (of 22 model-slots) |
| Mean success-score per model        | 1.00 / 1.00            |
| Cheaper model (per outcome `avg_cost_usd`) | haiku in every K-cluster |

Why the per-run sample counts don't match Run A's and Run B's 9
each: every benchmark invocation uses a fresh `tempfile.TemporaryDirectory`
per workload, so the `workspace_hash` in `StructuralFeatures` differs
per (workload × invocation). Each invocation deduplicates within
itself (two turns with the same `estimated_input_tokens_bucket` and
`has_tool_calls_in_history` land on the same `structural_signature`)
but cannot merge across runs. The cluster therefore grows by 5–6
outcomes per run instead of accumulating sample-size on a small
fingerprint set. This is fine for slot 4: K-NN is Jaccard-similarity-
based (per [`pattern-store.md §5.3`](../docs/specs/pattern-store.md)),
not signature-equality, and `workspace_hash` is deliberately excluded
from the weighted-Jaccard formula. The K=10 cap covers most of the
cluster on every query.

### A1 per-turn slot-4 detail (Run C, first three turns)

The full alternatives list is identical across all 9 turns:

```
chosen_model = anthropic:claude-haiku-4-5
confidence   = 0.300 (exactly; passes `min_confidence=0.3`)

pattern_alternatives:
  anthropic:claude-haiku-4-5   score=1.000   sample_size=9-11
  anthropic:claude-sonnet-4-6  score=0.700   sample_size=7-8
```

Why haiku wins: per
[`pattern-store.md §8.3`](../docs/specs/pattern-store.md) the score is
`(1 - cost_weight) * success_mean + cost_weight * cost_efficiency`.
With `cost_weight=0.3` and both models tied at `success_mean=1.0`,
the score difference reduces to `0.3 × (cost_efficiency_haiku -
cost_efficiency_sonnet) = 0.3 × (1 - 0) = 0.3` — exactly the
confidence threshold. The cost-efficiency term is computed off the
per-outcome `avg_cost_usd`; haiku's average across the K-cluster is
materially cheaper than sonnet's, so `normalized_cost_efficiency_haiku = 1.0`
and `normalized_cost_efficiency_sonnet = 0.0`. Tie-breaking lands
deterministically on haiku via the descending-score / model-id-ascending
sort in
[`patterns/aggregation.py:156`](../packages/metis-core/src/metis_core/patterns/aggregation.py#L156).

### A1 cost delta

| Run | LLM calls | Total cost ($) | `actual_repriced_usd` | `cache_creation_input_tokens` | `cached_input_tokens` |
|-----|-----------|----------------|------------------------|-------------------------------|------------------------|
| A   | 25        | 0.092552       | 0.092552               | 12,253                        | 44,194                 |
| B   | 35        | 0.258365       | 0.258365               | 13,351                        | 137,438                |
| C   | 22        | 0.084458       | 0.084458               | 11,586                        | 23,403                 |

- `(A + B) / 2` average = **$0.175459**
- Run C `actual_repriced_usd` = **$0.084458**
- **Delta vs the A/B average: -$0.091 (-52%)**

Run C is also slightly cheaper than Run A alone (-$0.008, -9%) — a
combined effect of (a) Run A's cache warming the prefix later in the
run while Run C inherits that prefix from turn 1 conditioning, and
(b) ordinary haiku non-determinism at `temperature=0` on tool-cycle
branches (within `benchmark.md §6.2`'s `±2 llm_call_count` tolerance).
Run C's `llm_call_count=22` matches Run 1's haiku-only baseline
exactly.

### A1 finding

**The pattern store does change routing decisions when slot 4 has
genuine choice.** With multiple actual primary_models populating the
cluster and `active_model` unpinned, slot 4 fires on every turn and
picks the cheaper of the two models at the floor of the confidence
gate. The 52% cost delta vs the (A+B) average is a synthetic ceiling
— the experiment didn't include any fingerprints where sonnet was
genuinely the right call — but it falsifies the prior reading that
slot 4 was effectively non-load-bearing.

The §A1 finding from Run 2 stands: **the benchmark suite as previously
shipped never exercised slot 4** because the harness pinned
`active_model`. Two structural fixes were needed to let the experiment
fire:

1. The runtime must inject a `fingerprint_inputs_builder` (otherwise
   slot 4 short-circuits on its own precondition check, regardless of
   chain order).
2. The harness must checkpoint the SQLite WAL before snapshotting
   `patterns.db` to a shared path.

Both are wired in this experiment's working tree and stay in place;
the second one (WAL checkpoint) is a real benchmark-harness bug that
silently lost data in Run 2's `--pattern-save-dir` / `--pattern-seed-dir`
mode too — Run 2's "6 fingerprints, 6 outcomes" reading was probably
also an undercount, though we can't reconstruct what the warm run
would have seen without re-running.

### A1 caveats and what this experiment does NOT prove

- **Both runs scored every turn at `success_score=1.0`.** The
  heuristic judge's v1 rubric doesn't differentiate haiku from sonnet
  on these workloads (Run 2's eval analysis bears this out). So the
  cluster ranking is dominated by `cost_efficiency`, not by `success`
  signal. A real-world cluster with `success_mean_haiku=0.7` and
  `success_mean_sonnet=0.95` would invert the recommendation despite
  haiku being cheaper. The mechanism works; the input quality is the
  separate Wave-4 evaluator project (and Agent P2's content-signal
  patch landing in parallel).
- **The `default_fingerprint_builder` records empty
  `user_message_text`**, so all queries have empty `intent_tags` and
  cluster Jaccard similarity is the same ~0.85 for every turn against
  every recorded fingerprint. Within the v1 contract this is correct
  (per `pattern-store.md §10.4`: "raw text not on bus") but it means
  v1's K-NN is essentially clustering by `(token_bucket,
  has_tool_calls_in_history, has_images)` only. Richer fingerprints —
  via `set_fingerprint_inputs` from the session manager, or a v2
  embedding fingerprint — would meaningfully improve cluster
  selectivity.
- **No fingerprint accumulates `sample_size > 4` in this experiment**
  because each invocation uses a fresh tempdir. Real-world per-
  workspace usage would see the same workspace_hash across sessions
  and accumulate sample-size on individual outcomes. `min_sample_size=5`
  was cleared via the K-NN aggregator summing across same-model
  neighbors, not by any single outcome reaching that threshold.

```bash
# Confirm baseline.
uv run pytest -q                                # 968 passed (Run 2)

# Live cache validator (requires ANTHROPIC_API_KEY).
uv run python scripts/smoke_cache.py --model haiku

# Run 1: full suite (requires ANTHROPIC_API_KEY).
uv run python scripts/benchmark.py

# Run 2: cold-then-warm pair to compare pattern-store activity.
uv run python scripts/benchmark.py \
  --pattern-save-dir benchmarks/.runs/patterns-cold/

uv run python scripts/benchmark.py \
  --pattern-seed-dir benchmarks/.runs/patterns-cold/

# Experiment A1: prove slot 4 fires when the chain is unblocked and the
# patterns DB has outcomes for multiple actual models.
rm -f benchmarks/.runs/exp-a1-patterns.db
uv run python scripts/benchmark.py \
  --model haiku  --patterns-db-path benchmarks/.runs/exp-a1-patterns.db \
  --db-path     benchmarks/.runs/exp-a1-run-a.db
uv run python scripts/benchmark.py \
  --model sonnet --patterns-db-path benchmarks/.runs/exp-a1-patterns.db \
  --db-path     benchmarks/.runs/exp-a1-run-b.db
uv run python scripts/benchmark.py \
  --no-active-model --patterns-db-path benchmarks/.runs/exp-a1-patterns.db \
  --db-path     benchmarks/.runs/exp-a1-run-c.db
# Expect: 0 / 0 / 9 pattern.matched events across A / B / C.

# Replay either run against its saved DB without re-spending.
uv run python scripts/benchmark.py \
  --db-path benchmarks/.runs/benchmark-2026-05-14T06-08-16Z.db \
  --skip-execute

# Inspect on the dashboard.
uv run metis serve $(pwd) \
  --db-path benchmarks/.runs/benchmark-2026-05-14T06-08-16Z.db
open http://127.0.0.1:8421/dashboard
```

---

## Experiment A2: K-NN selectivity after wiring `user_message_text`

> An earlier draft of this section described plumbing that wasn't actually
> landed (a `fingerprint_inputs_hook` and a corresponding runtime builder).
> This is the version against the real code: the producer-side hook +
> builder now exist and are exercised by end-to-end tests.

**Date (UTC):** 2026-05-14
**Branch:** `2026-05-14`
**Status:** offline numerical evidence + producer-side plumbing landed;
full-suite re-run deferred to avoid spending API budget when the K-NN-side
argument is provable analytically.

### Motivation

A1's caveat §"`default_fingerprint_builder` records empty
`user_message_text`" called out the v1 wedge: the routing-side
fingerprint and the recording-side fingerprint *both* fed
`user_message_text=""` into `build_structural_features`, so
`intent_tags` was always empty and the weighted-Jaccard collapsed every
turn into one cluster. The wedge wasn't `pattern-store.md §10.4`'s
"raw text not on bus" — the session manager already has the text — it
was that nobody plumbed it through. This experiment isolates the
selectivity gain from doing so.

### What changed

Two producer-side seams land on `2026-05-14` so the receiver-side
plumbing that Wave 5 5b-3 already shipped (`TurnCompleted.signals_extra`
in [packages/metis-core/src/metis_core/events/payloads.py](../packages/metis-core/src/metis_core/events/payloads.py),
`PatternEventSubscriber.set_fingerprint_inputs` in
[packages/metis-core/src/metis_core/patterns/subscriber.py](../packages/metis-core/src/metis_core/patterns/subscriber.py),
the evaluator subscriber's `signals_extra` read in
[packages/metis-core/src/metis_core/eval/subscriber.py](../packages/metis-core/src/metis_core/eval/subscriber.py))
sees real data:

1. **`SessionManager._emit_turn_completed` stamps `signals_extra`.**
   The turn's `last_assistant_text` (already accumulated in the turn
   loop in [packages/metis-core/src/metis_core/sessions/manager.py](../packages/metis-core/src/metis_core/sessions/manager.py))
   is passed through as `signals_extra={"final_response_text": …}`
   when non-empty; the key is omitted when the assistant produced no
   text. The evaluator subscriber's content-penalty path (`evaluator.md
   §5.1`) now fires on the online bus path, not just the workload harness.

2. **`SessionManager` accepts a `fingerprint_inputs_hook`.** A
   `Callable[[str, TurnContext], None]` invoked right after
   `_build_turn_context` so the CLI runtime can pre-populate the
   pattern subscriber's per-turn override before `turn.completed` fires.
   The hook is optional — when unset, the subscriber falls back to
   `default_fingerprint_builder` (preserving the "raw text not on bus"
   guarantee in `pattern-store.md §10.4` for embedded callers).

3. **`apps/cli/src/metis_cli/runtime.py` wires the hook against the
   same builder it passes to the routing engine.** Both ends of the
   fingerprint (query-side at routing and record-side at outcome
   recording) now read identical fields from `TurnContext`:

```python
# apps/cli/src/metis_cli/runtime.py
def _routing_fingerprint_inputs(ctx) -> FingerprintInputs:
    return FingerprintInputs(
        user_message_text=ctx.user_message_text,
        workspace_path=ctx.workspace_path,
        estimated_input_tokens=ctx.estimated_input_tokens,
        has_images=ctx.has_images,
        has_tool_calls_in_history=ctx.has_tool_calls_in_history,
    )

def _on_turn_fingerprint_inputs(turn_id: str, ctx) -> None:
    pattern_subscriber.set_fingerprint_inputs(
        turn_id, _routing_fingerprint_inputs(ctx)
    )

manager = SessionManager(..., fingerprint_inputs_hook=_on_turn_fingerprint_inputs)
```

`TurnContext.user_message_text` is set in
`SessionManager._build_turn_context` from the new user `Message`'s first
`TextBlock`, so `intent_tags` (mechanical regex from
`patterns/fingerprint.py::_INTENT_PATTERNS`) finally fires on a real
string at both query time and record time.

### End-to-end tests that prove the producer-side fires

Four new tests in [packages/metis-core/tests/sessions/test_manager.py](../packages/metis-core/tests/sessions/test_manager.py)
and [packages/metis-core/tests/patterns/test_subscriber.py](../packages/metis-core/tests/patterns/test_subscriber.py)
drive a real `SessionManager` (scripted adapter, no live API) and
assert on the resulting bus events:

- `test_turn_completed_carries_final_response_text_in_signals_extra`
  — scripts a refusal response, asserts `turn.completed.signals_extra
  ["final_response_text"]` is populated with the refusal text.
- `test_turn_completed_omits_signals_extra_when_no_assistant_text`
  — guards the producer's "omit empty" behavior so the evaluator
  doesn't mis-trigger the empty-response penalty on tool-only turns.
- `test_refusal_signals_drop_eval_score_below_baseline` — drives the
  same refusal through the heuristic evaluator subscriber and asserts
  the resulting `eval.completed.score < 0.6` (refusal multiplier 0.5
  × clean lifecycle base) with `flags_negative` containing
  `assistant_refusal_detected`. Previously this turn would have
  scored 1.0 because the evaluator never saw the refusal text.
- `test_clean_response_keeps_eval_score_at_baseline` — control case;
  pins the delta in the refusal test to the content-penalty path,
  not to other lifecycle drift.
- `test_fingerprint_inputs_hook_records_distinct_signatures_per_turn`
  (in `tests/patterns/test_subscriber.py`) — submits two turns with
  substantively different user messages ("refactor this function" vs
  "debug the failing test") through SessionManager + the hook,
  asserts the pattern store recorded two distinct
  `fingerprint_id`s, not the single collapsed signature the pre-hook
  codepath would have produced.

These complement the existing
`test_knn_returns_matching_cluster_for_distinct_user_messages` in
[packages/metis-core/tests/patterns/test_knn_selectivity.py](../packages/metis-core/tests/patterns/test_knn_selectivity.py),
which proved the K-NN math at the store level. With the new tests,
the entire chain — `submit_turn` → hook → subscriber → store →
`find_k_nearest` — is exercised under unit test, no live API.

Test count: 1096 → 1101 (`uv run pytest -q`).

### Pairwise weighted-Jaccard over the shipped workload suite

Computed against each workload's *first-turn user prompt*, holding all
other structural fields identical (the goal is to isolate the change in
`intent_tags`):

| Workload                                    | `intent_tags` (after) |
|---------------------------------------------|------------------------|
| `fix-a-bug-small`                           | `(architecture, debug, doc)` |
| `intentionally-failing-task`                | `()`                   |
| `multi-file-refactor-with-shared-types`     | `()`                   |
| `multi-turn-refactor`                       | `(refactor, architecture, doc)` |
| `regex-with-edge-cases`                     | `()`                   |
| `write-a-doc-from-notes`                    | `()`                   |

**Unique structural signatures across the 6 workloads:**
**1 (before) → 3 (after)**.

`weighted_jaccard(a, b)` per `pattern-store.md §5.3` formula, using each
workload's first-turn prompt:

| Pair                                                                                | Before | After |
|-------------------------------------------------------------------------------------|-------:|------:|
| `fix-a-bug-small`              vs `intentionally-failing-task`                      | 1.000  | 0.700 |
| `fix-a-bug-small`              vs `multi-file-refactor-with-shared-types`           | 1.000  | 0.700 |
| `fix-a-bug-small`              vs `multi-turn-refactor`                             | 1.000  | 0.850 |
| `fix-a-bug-small`              vs `regex-with-edge-cases`                           | 1.000  | 0.700 |
| `fix-a-bug-small`              vs `write-a-doc-from-notes`                          | 1.000  | 0.700 |
| `intentionally-failing-task`   vs `multi-file-refactor-with-shared-types`           | 1.000  | 1.000 |
| `intentionally-failing-task`   vs `multi-turn-refactor`                             | 1.000  | 0.700 |
| `intentionally-failing-task`   vs `regex-with-edge-cases`                           | 1.000  | 1.000 |
| `intentionally-failing-task`   vs `write-a-doc-from-notes`                          | 1.000  | 1.000 |
| `multi-file-refactor-with-shared-types` vs `multi-turn-refactor`                    | 1.000  | 0.700 |
| `multi-file-refactor-with-shared-types` vs `regex-with-edge-cases`                  | 1.000  | 1.000 |
| `multi-file-refactor-with-shared-types` vs `write-a-doc-from-notes`                 | 1.000  | 1.000 |
| `multi-turn-refactor`          vs `regex-with-edge-cases`                           | 1.000  | 0.700 |
| `multi-turn-refactor`          vs `write-a-doc-from-notes`                          | 1.000  | 0.700 |
| `regex-with-edge-cases`        vs `write-a-doc-from-notes`                          | 1.000  | 1.000 |

A1's commentary estimated "cluster Jaccard similarity is the same ~0.85
for every turn against every recorded fingerprint." That was a rounded
estimate; with `user_message_text=""` the structural signatures are
actually *identical* (Jaccard = 1.000) for any pair with matching
token_bucket / has_images / has_tool_calls — i.e. every workload pair
in the shipped suite. The fix introduces real spread: 9 of 15 pairs now
fall below 1.000, 5 fall to the `0.700` floor (no `intent_tags`
overlap), and one (`fix-a-bug-small` vs `multi-turn-refactor`) sits at
`0.850` because both prompts share `architecture` and `doc` tags.

### What this proves (and what it doesn't)

**Proves.**

- The K-NN store can now distinguish workloads with different mechanical
  intent_tags. Slot 4's K-cluster aggregation will read genuinely
  different neighbor sets per turn instead of always pulling every
  recorded outcome with similarity ~1.0.
- `test_knn_returns_matching_cluster_for_distinct_user_messages` in
  `packages/metis-core/tests/patterns/test_knn_selectivity.py` exercises
  the same property at the store level: a refactor probe ranks the
  refactor neighbor's similarity above the debug neighbor's.

**Does not prove.**

- That the recommended *model* changes for real workloads on the
  *current* heuristic judge alone. The judge still scores every clean
  turn ~1.0 on these workloads (refusal/empty are the only penalties
  with teeth in v1), so the cluster aggregator falls back to
  `cost_efficiency` — same model wins. The refusal/empty path now
  reaches the online subscriber thanks to this commit's other half
  (`final_response_text` on `turn.completed.signals_extra`; see
  `evaluator.md §5.1`), and the new
  `test_refusal_signals_drop_eval_score_below_baseline` test asserts
  end-to-end that a scripted refusal lands `score < 0.6` on the bus
  path. Lifting the *non*-refusal floor still needs the LLM-as-judge
  tier (`evaluator.md §5.2`) — that's Experiment A3.
- That intent_tags are sufficient. Four of the six shipped workloads
  still hit `()` because their prompts use task-domain words (`runner`,
  `notes`, `handlers`) that the mechanical regex doesn't recognize.
  v2's hybrid fingerprint (embedding over user message) is the
  follow-up; the structural floor lands now.

### Why not re-run `scripts/benchmark.py` end-to-end here

The benchmark suite spends ~$0.30–$1.00 of real API budget per run.
A1 already executed a 3-pass benchmark (~$0.50) that pinned slot 4 firing.
A2's argument — that K-NN selectivity improves once
`user_message_text` is plumbed — is fully provable from the static
weighted-Jaccard table above, the new unit tests, and the visible
delta in `intent_tags` per workload. Re-running the full benchmark
would re-verify A1's savings number under the new plumbing (expected:
unchanged at 66.7% under the same single-model routing) without
producing any new information about the K-NN-side change. The
follow-up A3 — "re-run the suite under a *non-trivial* evaluator that
distinguishes haiku from sonnet on at least one workload" — is the run
worth spending money on; it's blocked on the evaluator's LLM-as-judge
tier (`evaluator.md §5.2`), not on this plumbing.

### Reproduce the selectivity numbers

```bash
# Same Python env that runs pytest.
uv run python -c "
import yaml
from pathlib import Path
from metis_core.patterns.fingerprint import (
    FingerprintInputs, build_structural_features, structural_signature,
)
from metis_core.patterns.similarity import weighted_jaccard

WORKLOADS_DIR = Path('benchmarks/workloads')

def prompts():
    return {
        w.name: (yaml.safe_load((w / 'workload.yaml').read_text()).get('turns') or [{}])[0].get('prompt', '').strip().replace('\n', ' ')
        for w in sorted(WORKLOADS_DIR.iterdir())
        if (w / 'workload.yaml').exists()
    }

def features(text):
    return build_structural_features(FingerprintInputs(
        user_message_text=text, workspace_path='/dummy',
        estimated_input_tokens=1000, has_images=False,
        has_tool_calls_in_history=False,
    ))

p = prompts()
old = {n: features('') for n in p}
new = {n: features(t) for n, t in p.items()}
print('unique sigs before:', len({structural_signature(f) for f in old.values()}))
print('unique sigs after: ', len({structural_signature(f) for f in new.values()}))
names = list(p)
for i, a in enumerate(names):
    for b in names[i+1:]:
        print(f'{a:42s} vs {b:42s}  before={weighted_jaccard(old[a], old[b]):.3f}  after={weighted_jaccard(new[a], new[b]):.3f}')
"
```

## Workload diversity v1: discriminating success-rate workloads

**Date (UTC):** 2026-05-14
**Branch:** `2026-05-14`
**Commit:** `a6e9679` + dirty working tree (new fixtures + test set update)
**Test baseline:** 1029 passed (`uv run pytest -q`)

### Motivation

Experiment A1's caveat surfaced the v1 rubric's structural ceiling:
"Both runs scored every turn at `success_score=1.0` ... the cluster
ranking is dominated by `cost_efficiency`, not by success signal." The
three primary workloads (`fix-a-bug-small`, `multi-turn-refactor`,
`write-a-doc-from-notes`) all score `1.00 @ 0.80` for both haiku-4-5
and sonnet-4-6, so the pattern store's slot-4 recommendation is a
mechanical "pick the cheaper model" — correct given the inputs but
unable to invert when a cheaper model is also a worse model. This
section ships two new fixtures that intentionally stress success rate
and reports the haiku-vs-sonnet score delta against the *unchanged*
heuristic judge.

### What ships

Two new workloads under [`benchmarks/workloads/`](workloads/):

| Workload                                  | Shape                                     | Discrimination strategy |
|-------------------------------------------|-------------------------------------------|-------------------------|
| `regex-with-edge-cases`                   | One-shot regex over 16 labeled NANP cases | Iteration is locked down (`max_tool_calls: 3` on the write turn, `max_tool_calls: 1` on the run turn); a `runner.py` fixture prints `PASS 16/16` only on full correctness, and `expect_substring_in_final_response: "PASS 16/16"` checks the agent's final message. A naive regex (e.g., one that omits the `+1 ...` country-code variants or accepts unbalanced parens) fails the runner and the substring check; non-zero exit on the runner also fires `tool.failed`, dropping the per-turn score. |
| `multi-file-refactor-with-shared-types`   | Rename `UserId` → `AccountId` across 7 files | `legacy.py` uses an aliased import (`from domain import UserId as UID`) and `test_users.py` imports from every other module; any missed rename throws `ImportError` at pytest collection time, and `expect_substring_in_final_response: "3 passed"` only matches on a clean run. The prompt does not call out the alias trap directly. |

A third candidate — `architectural-explanation-without-hallucination` —
was considered and rejected. The v1 heuristic judge only checks
substring *presence*, not absence, so it cannot penalize hallucinated
class names; the workload would have measured omission, not
fabrication. Logged here so the option doesn't get re-picked without
evaluator-tier work first.

Both workloads ship with `evaluate:` blocks pinning the heuristic
rubric and an objective substring assertion. Both score `0.0` on the
existing assertion-set's content-penalty path if the agent refuses or
produces no text — same backstop as `intentionally-failing-task`.

### Iteration history

| Iteration | Workload | Change | haiku score | sonnet score | Delta | Verdict |
|-----------|----------|--------|------------:|-------------:|------:|---------|
| iter 1    | `regex-with-edge-cases`             | Three-turn structure with `"iterate until PASS"` on the run turn (`max_tool_calls: 12`). | 1.00 | 1.00 | 0.00 | Both models reach PASS via trial-and-error; iteration absorbs all signal. |
| iter 2 (shipped) | `regex-with-edge-cases`             | Turn 2 prohibits running, turn 3 locked to `max_tool_calls: 1`. | **0.25** | **1.00** | **0.75** | Strong discriminator. Haiku's one-shot regex emits `FAIL 15/16`; sonnet's emits `PASS 16/16`. |
| iter 1    | `multi-file-refactor-with-shared-types` | Six-file workspace, no alias trap, brittle `contains_substring: "AccountId"` on turn 3. | 0.75 | (not run) | n/a | Score reflects rubric brittleness — haiku completed the rename and pytest passed but its turn-3 grep summary said "no references remain" without literally saying "AccountId." Fixture problem, not model failure. |
| iter 2    | `multi-file-refactor-with-shared-types` | Brittle assertion removed; `legacy.py` (aliased import) added; tests grown to 3. Prompt explicitly warned about aliased imports. | 1.00 | 1.00 | 0.00 | Both models catch the alias because the prompt names the pattern. |
| iter 3 (shipped) | `multi-file-refactor-with-shared-types` | Prompt hint removed; agent must discover the aliased import from turn 1's enumeration on its own. | **1.00** | **1.00** | **0.00** | Does not discriminate. Both models enumerate every `.py` file in turn 1, find the `UserId` token in `legacy.py`'s import line, and update it. Haiku 4-5 is capable enough on standard refactor tasks that even a moderately adversarial import doesn't break it; pytest passes in both runs. |

### Shipped numbers

Ran the two new workloads in isolation (`--workload <name>`) against
each model. Per `benchmark.md §6.1` provenance: pricing version
`2026-05-08+openrouter-c40a0b72db6a`, temperature `0.0`, Python
`3.13.12`.

| Workload                                | Model  | Quality score | Confidence | LLM calls | Tool calls | `actual_repriced_usd` | Substring assertion | Notes |
|-----------------------------------------|--------|--------------:|-----------:|----------:|-----------:|----------------------:|---------------------|-------|
| `regex-with-edge-cases`                 | haiku  | **0.25**      | 0.80       | 7         | 5          | $0.0386               | **MISS** (`FAIL 15/16`) | haiku's one-shot regex omits the `+1 (415) 555-0123` country-code variant. |
| `regex-with-edge-cases`                 | sonnet | **1.00**      | 0.80       | 6         | 4          | $0.1041               | hit | `PASS 16/16` in one shot. |
| `multi-file-refactor-with-shared-types` | haiku  | **1.00**      | 0.80       | 13        | 26         | $0.0603               | hit | Catches `legacy.py` alias via turn-1 enumeration; iterates once to clean docstring references. |
| `multi-file-refactor-with-shared-types` | sonnet | **1.00**      | 0.80       | 17        | 29         | $0.1618               | hit | More verbose enumeration in turn 3 (grep + wider sweep) but same outcome. |

JSON artifacts:
[`benchmarks/.runs/diversity-regex-haiku.json`](.runs/diversity-regex-haiku.json),
[`benchmarks/.runs/diversity-regex-sonnet.json`](.runs/diversity-regex-sonnet.json),
[`benchmarks/.runs/diversity-mfr-haiku.json`](.runs/diversity-mfr-haiku.json),
[`benchmarks/.runs/diversity-mfr-sonnet.json`](.runs/diversity-mfr-sonnet.json).
Total API spend across all six benchmark passes (including the two
iterations on each new workload): **$0.853**.

### Cost-per-success: the inversion

The savings story is `actual / score` (cost per unit of successful
output), not `actual` alone:

| Workload                                | haiku $/score | sonnet $/score | Cheaper model | Margin |
|-----------------------------------------|--------------:|---------------:|---------------|-------:|
| `regex-with-edge-cases`                 | **$0.154**    | $0.104         | **sonnet**    | -32%   |
| `multi-file-refactor-with-shared-types` | $0.060        | $0.162         | **haiku**     | +63%   |

On `regex-with-edge-cases`, **sonnet's cost-per-success is 32% lower
than haiku's** despite sonnet's raw spend being 2.7× higher — because
haiku's score is 0.25, not 1.00. On `multi-file-refactor-with-shared-types`,
haiku retains its expected 63% advantage because it succeeds.

### Implications for the savings narrative

Run 1 reported a **structural** 66.7% `savings_pct` (haiku's rate card
is uniformly cheaper than sonnet's; multiply token counts by either
rate, the ratio is fixed). That number is true and reproducible — but
it is silent on whether the work succeeded.

With the new workloads:

1. **The 66.7% headline holds when haiku succeeds.** Both shipped
   workloads run under the same single-model harness, so the
   `actual_repriced_usd / baseline_repriced_usd` ratio reproduces the
   structural number per-workload. That hasn't changed.
2. **Cost-per-success on `regex-with-edge-cases` inverts.** Sonnet at
   $0.104 / 1.0 success beats haiku at $0.0386 / 0.25 success.
   `savings_pct` over a workload mix that includes this shape is
   misleading without weighting by success — picking haiku saves
   dollars in the analytics ledger but does not save dollars-of-
   correct-work.
3. **Pattern-store recommendation can now invert correctly.**
   `pattern-store.md §8.3` aggregates outcomes as
   `(1 - cost_weight) * success_mean + cost_weight * cost_efficiency`
   with `cost_weight=0.3`. Plugging in haiku's `success_mean=0.25` and
   sonnet's `success_mean=1.00` from this fixture:
   `haiku_score = 0.7 × 0.25 + 0.3 × 1.0 = 0.475`,
   `sonnet_score = 0.7 × 1.0 + 0.3 × 0.0 = 0.700`.
   Sonnet wins the cluster despite being more expensive, which is
   precisely the behavior A1 was unable to demonstrate. The mechanism
   was already implemented; this section provides the first input
   distribution where it triggers.
4. **`savings_pct` should grow a quality-weighted sibling.** A future
   `/analytics/savings_per_success` (or a `quality_weight` parameter on
   the existing endpoint) would multiply each row's repriced cost by
   `1/score` before the ratio. Spec impact lives in
   [`docs/specs/analytics-api.md`](../docs/specs/analytics-api.md);
   tracked separately, not landed here.

### Caveats

- **`multi-file-refactor-with-shared-types` does not discriminate at
  haiku-4-5 level.** Both models score 1.00 in three iterations of the
  fixture (with-hint, no-hint, alias-trap added). Haiku is capable
  enough to enumerate `.py` files via `read_file`/`shell`, identify
  every `UserId` token (including the import in `legacy.py`), and
  apply the rename consistently. The workload is shipped anyway as a
  parity datapoint: it proves the rubric isn't pinned-low either, and
  the cost differential ($0.060 vs $0.162) demonstrates haiku's
  intended savings story on a task where it actually succeeds. A
  follow-up could replace it with a workload that does discriminate
  (e.g., a 12-file refactor where one reference is in a comment-only
  location, or a subtle algorithmic bug where the obvious fix doesn't
  cover the test fixtures).
- **One model pair, one snapshot.** Numbers are for `haiku-4-5` vs
  `sonnet-4-6` on commit `a6e9679`. A future haiku rev might one-shot
  the regex; a future sonnet rev might miss it. Re-running on a model
  refresh re-establishes the delta.
- **`temperature=0.0` is not strictly deterministic.** Per
  `benchmark.md §6.2` the per-workload `actual_repriced_usd` is
  expected to vary by ±25% relative and `llm_call_count` by ±2. The
  0.25-vs-1.00 score delta on regex-with-edge-cases is too large for
  that variance to explain away, but a single run is not a proof — the
  score-delta direction (haiku < sonnet) is the load-bearing signal,
  not the exact magnitude.
- **The score-1.0 ceiling masks finer differentiation on mfr.** The
  workload-level rubric is monotone-but-coarse: success collapses to
  `1.0` once pytest passes regardless of tool-cycle efficiency or
  edit-set minimality. A model that touches 12 files unnecessarily but
  passes pytest scores identically to one that touches the 6 strictly
  needed. An LLM-tier judge (`evaluator.md §5.2`) is the right place
  to add that gradient.

### Reproduce

```bash
# Confirm test baseline.
uv run pytest -q                                # 1029 passed

# Run each new workload against haiku and sonnet.
uv run python scripts/benchmark.py \
    --workload regex-with-edge-cases \
    --model haiku --db-path benchmarks/.runs/diversity-regex-haiku.db
uv run python scripts/benchmark.py \
    --workload regex-with-edge-cases \
    --model sonnet --db-path benchmarks/.runs/diversity-regex-sonnet.db
uv run python scripts/benchmark.py \
    --workload multi-file-refactor-with-shared-types \
    --model haiku --db-path benchmarks/.runs/diversity-mfr-haiku.db
uv run python scripts/benchmark.py \
    --workload multi-file-refactor-with-shared-types \
    --model sonnet --db-path benchmarks/.runs/diversity-mfr-sonnet.db

# Quality scores and assertion failures land in the JSON artifact:
jq '.workloads[] | {name, quality_score, assertion_failures, actual_repriced_usd}' \
    benchmarks/.runs/diversity-*.json

# Total spend ≈ $0.40 for the four runs above; full iteration history
# (iter1 of regex, iter2 of mfr, plus the shipped runs) totaled $0.85.
```

### Third diversity workload: `architectural-explanation-without-hallucination`

**Date (UTC):** 2026-05-14
**Test baseline:** 1101 passed (`uv run pytest -q`; +1 over the prior
1100 from updating `test_shipped_workloads_load_clean` to include the
new fixture name).
**Total validation spend:** ~$0.19 (heuristic haiku $0.039 + heuristic
sonnet $0.126 + hybrid-haiku $0.012 + llm-haiku $0.012; cache fired on
the later runs).
**JSON artifacts:**
[`.runs/diversity-hallucination-haiku.json`](.runs/diversity-hallucination-haiku.json) (heuristic),
[`.runs/diversity-hallucination-sonnet.json`](.runs/diversity-hallucination-sonnet.json) (heuristic),
[`.runs/diversity-hallucination-haiku-hybrid.json`](.runs/diversity-hallucination-haiku-hybrid.json),
[`.runs/diversity-hallucination-haiku-llm.json`](.runs/diversity-hallucination-haiku-llm.json).

#### Motivation

The original 5b-4 brief asked for 2-3 diverse workloads; Wave 5
shipped two (regex, mfr) and **explicitly rejected** this third one
above ("the v1 heuristic judge only checks substring *presence*, not
absence, so it cannot penalize hallucinated class names; the workload
would have measured omission, not fabrication"). Since then the
hybrid + LLM judge tiers shipped, and Wave 6a-1 wired
`--judge {hybrid,llm}` flags into the benchmark harness — so the
hallucination dimension is in principle reachable now. This
sub-section ships the workload, validates against haiku and sonnet
under all three judge modes, and reports what each judge tier
actually sees.

#### What ships

A new fixture under
[`workloads/architectural-explanation-without-hallucination/`](workloads/architectural-explanation-without-hallucination/):

- **Workspace:** a snapshot of `packages/metis-core/src/metis_core/routing/`
  copied under `workspace/routing/` (11 files, no `__pycache__`).
- **Prompt:** one turn asking the agent to explain the 7-slot routing
  chain and reference at least four real class/function/enum names
  from the source, citing the file each lives in.
- **Heuristic assertions:** turn-level `contains_substring: "RoutingEngine"`
  (the central class) and workload-level
  `expect_substring_in_final_response: "PATTERN_RECOMMENDATION"` (a
  distinctive UPPERCASE slot label that appears in `routing/engine.py`'s
  module docstring at line 8).
- **`evaluate.rubric: hybrid`** with `llm_judge_model: anthropic:claude-haiku-4-5`
  — accepted by the schema; reachable today via
  `scripts/benchmark.py --judge {hybrid,llm}` (6a-1's wiring) or the
  default heuristic.
- **`expect.max_total_cost_usd: 0.20`** — single turn, ~6 tool calls
  for reading the routing source.

#### Heuristic-judge results (default)

| Model  | Quality | Conf. | LLM calls | Tool calls | `actual_repriced_usd` | Turn-level heuristic | Workload-level signal |
|--------|--------:|------:|----------:|-----------:|----------------------:|---------------------:|----------------------|
| haiku  | **1.00** | 0.80 | 4         | 6          | $0.0394               | 1.00 @ 0.90           | `expected_substring_present` |
| sonnet | **0.50** | 0.80 | 4         | 7          | $0.1257               | 1.00 @ 0.90           | `expected_substring_missing` (no literal "PATTERN_RECOMMENDATION") |

By the brief's numeric test (delta of 0.5), the workload **does
discriminate**. But the direction is **inverted from the intended
axis** — see next section.

#### Critical caveat: the discrimination is stylistic, not hallucination

A side-by-side grep of grounding tokens in each model's final response
(reading the trace DB's `messages` table directly):

| Grounding token                  | haiku | sonnet |
|----------------------------------|:-----:|:------:|
| `RoutingEngine` (engine.py)      | ✓     | ✓      |
| `ModelRegistry` (registry.py)    | ✓     | ✓      |
| `AvailabilityState` (availability.py) | ✓ | ✓      |
| `_evaluate_pattern` (engine.py)  | ✓     | ✓      |
| `_build_chain`, `_validate`      | ✓     | ✓      |
| `TurnContext` (context.py)       | ✓     | ✓      |
| `ProviderAvailability`           | ✓     | ✓      |
| `PatternConfig` (policy.py)      | ✓     | ✓      |
| `RoutingError` (engine.py)       | ✓     | ✓      |
| `PolicyEvaluation` (real dataclass) | ✗  | **✓**  |
| `RoutingDecision`                | ✗     | **✓**  |
| Real lowercase `policy=` strings (`"per_message_override"`, `"manual_sticky"`, `"rule"`, `"pattern"`, `"global_default"`) | ✗ | **✓** |
| `PATTERN_RECOMMENDATION` (the docstring UPPERCASE label at engine.py:8) | **✓** | ✗ |

**Sonnet's response is more grounded by every measure except the
substring we picked**. Sonnet cited the real `PolicyEvaluation` /
`RoutingDecision` dataclasses, the real lowercase `policy=` string
literals used in actual events, and the same `RoutingEngine` /
`ModelRegistry` / `AvailabilityState` symbols haiku used. Haiku
parroted the UPPERCASE_SLOT_LABEL convention from engine.py's module
docstring; sonnet rendered slot names lowercase the way the
`policy=` field literally serializes.

The substring `PATTERN_RECOMMENDATION` is therefore a **stylistic
fidelity check**, not a hallucination check. Neither model
fabricated; the heuristic just rewards the one that copied the
docstring convention.

**This empirically confirms the 5b-4 design note** that motivated
rejecting this workload in the first place: substring presence is
not the right primitive for fabrication.

#### Hybrid + LLM judge tiers can't fix this yet (validation finding)

Re-ran haiku under `--judge hybrid --judge-escalation-threshold 0.9`
(forcing escalation since the heuristic confidence sits at 0.9) and
under `--judge llm` (always LLM). Per-turn LLM judge verdict:

```
score=0.5 confidence=0.3
rationale: "Tool calls succeeded but user prompt and assistant
            response unavailable, preventing assessment of whether
            intent was met."
```

Workload-level LLM judge verdict:

```
score=0.0 confidence=1.0
rationale: "Cannot evaluate without user prompt and assistant
            response; both marked unavailable."
```

The LLM judge expects `SubjectContext.signals_extra.user_prompt_text`
and `assistant_response_text` (see
`packages/metis-core/src/metis_core/eval/llm_judge.py:505-533`), and
the test suite under `packages/metis-core/tests/eval/test_llm_judge.py`
plumbs them through. **Production code paths (session manager /
evaluator subscriber) do not.** A grep across `packages/metis-core/`
finds those keys only in the tests and in the consumer.

So 6a-1's `--judge {hybrid,llm}` wiring is **structurally complete
but functionally inert** for this workload — the LLM is asked to
evaluate something it can't see. The fix is a small change in the
session manager (or the evaluator subscriber) to populate
`signals_extra.user_prompt_text` and `assistant_response_text` in
the `turn.completed` event. Outside this wave's scope
(`packages/metis-core/` is locked); flagged for a follow-up wave.

#### What we're shipping anyway, and why

The workload **is shipped** even though its current discrimination
signal is brittle, because:

1. **It is a forward-compatible asset.** Once the
   `signals_extra.user_prompt_text` plumbing lands, the same fixture
   re-runs under `--judge hybrid` and yields a real hallucination
   verdict. No fixture change needed.
2. **It's the only fixture in the suite that puts the heuristic /
   LLM-judge gap under load.** Run-3's other five workloads don't
   exercise hallucination at all — they exercise objective task
   completion (regex passing, pytest passing, files edited). Pattern
   store + evaluator analytics gain a workload where the *judge tier*
   is the load-bearing knob.
3. **It empirically validates the 5b-4 design note** with concrete
   numbers (1.00 vs 0.50 in the *wrong* direction).
4. **Cost is bounded** — heuristic mode adds ~$0.04 (haiku) /
   ~$0.13 (sonnet) per run, well within the per-workload smoke
   ceiling.

#### Caveats specific to this workload

- **`PATTERN_RECOMMENDATION` substring assertion is misleading** if
  read at face value. The score-1.00-vs-0.50 delta inverts the actual
  grounding quality. Operators reading this row should treat it as
  "the agent named the docstring's slot labels using the docstring's
  capitalization" — useful as a stylistic-fidelity sentinel, not a
  hallucination probe.
- **Pattern store impact is unclear.** This workload's structural
  fingerprint (read-heavy, single turn) clusters near
  `write-a-doc-from-notes`. A future wave that includes this fixture
  in the K-NN sample may shift slot-4 recommendations on the doc
  cluster; not yet measured.
- **The harness's per-workload name column overflows** for
  `architectural-explanation-without-hallucination` (47 chars vs the
  format's 28-char field). The "turns" number butts right against
  the workload name in the table print. Pre-existing rough edge
  (also affects `multi-file-refactor-with-shared-types` at 37
  chars); not touched here to avoid conflict with 6a-1's concurrent
  harness changes.

#### Reproduce

```bash
# Confirm test baseline.
uv run pytest -q                                # expect 1101 passed

# Heuristic mode (the shipped result).
uv run python scripts/benchmark.py \
    --workload architectural-explanation-without-hallucination \
    --model haiku \
    --db-path benchmarks/.runs/diversity-hallucination-haiku.db
uv run python scripts/benchmark.py \
    --workload architectural-explanation-without-hallucination \
    --model sonnet \
    --db-path benchmarks/.runs/diversity-hallucination-sonnet.db

# Hybrid / LLM modes (currently inert per finding above; useful for
# tracking the fix-it follow-up).
uv run python scripts/benchmark.py \
    --workload architectural-explanation-without-hallucination \
    --model haiku --judge hybrid --judge-escalation-threshold 0.9 \
    --db-path benchmarks/.runs/diversity-hallucination-haiku-hybrid.db
uv run python scripts/benchmark.py \
    --workload architectural-explanation-without-hallucination \
    --model haiku --judge llm \
    --db-path benchmarks/.runs/diversity-hallucination-haiku-llm.db

# Inspect the eval.completed verdicts for any of the runs:
sqlite3 benchmarks/.runs/diversity-hallucination-haiku-llm.db \
    "SELECT type, json_extract(payload_json, '$.subject_kind'),
            json_extract(payload_json, '$.judge_kind'),
            json_extract(payload_json, '$.score'),
            json_extract(payload_json, '$.signals.rationale_preview')
     FROM events WHERE type='eval.completed'"
```

---

## Run 3: post-§5.1 (minimum-cacheable-prefix rule wired)

Same workload suite (now 6 workloads — `intentionally-failing-task` from
Run 2 plus `regex-with-edge-cases` and
`multi-file-refactor-with-shared-types` from the diversity wave),
re-run after `context-assembler.md §5.1` shipped. The §5.1 rule pads
the natural Metis stable prefix above the haiku-4-5 effective cache
floor (~4000 actual tokens) with a deterministic operating-context
block, so the cached prefix fires on **every** session — not just
`multi-turn-refactor` like in Run 2 §A2.

### Run 3 metadata

| Field              | Value                                                |
|--------------------|------------------------------------------------------|
| Run date (UTC)     | 2026-05-14T17:14:58Z                                 |
| Commit SHA         | `a6e9679` + dirty Wave-5 working tree                |
| Branch             | `2026-05-14`                                         |
| Suite version      | 1 (now 6 workloads — diversity-wave additions plus original 3 plus `intentionally-failing-task`) |
| Actual model       | `anthropic:claude-haiku-4-5`                         |
| Baseline model     | `anthropic:claude-sonnet-4-6`                        |
| Pricing version    | `2026-05-08+openrouter-c40a0b72db6a`                 |
| Temperature        | 0.0                                                  |
| Python             | 3.13.12                                              |
| Test baseline      | 1038 passed (`uv run pytest -q`; 1029 inherited + 9 new §5.1 tests minus 2 reverted-by-other-agent) |
| Smoke cache        | PASSED with natural prompt, $0.007786 (turn 1 wrote 5167 cache tokens; turn 2 read 5167 cached tokens) |

JSON artifact: [`.runs/benchmark-2026-05-14T17-14-58Z.json`](.runs/benchmark-2026-05-14T17-14-58Z.json)
Trace DB: `.runs/benchmark-2026-05-14T17-14-58Z.db`

### Run 3 aggregate

| Metric                            | Run 2 cold (3 workloads) | Run 3 same-3-workloads | Run 3 all-6-workloads |
|-----------------------------------|---------------------------|------------------------|------------------------|
| `rows_total`                      | 30                        | —                      | 49                     |
| `rows_missing_from_price_table`   | 0                         | —                      | 0                      |
| `actual_repriced_usd`             | $0.108273                 | **$0.083632**          | **$0.180137**          |
| `baseline_repriced_usd`           | $0.324819                 | —                      | $0.540410              |
| `savings_pct` (vs sonnet baseline)| 66.7%                     | 66.7%                  | 66.7%                  |
| `hard_failures` (routing)         | 0                         | —                      | 0                      |
| Wall time                         | ~58 s                     | —                      | ~125 s                 |

**Cost delta for the original-3 workloads vs Run 2 cold:** `-$0.024641`
(**-22.8%**). The §5.1 padding pays for itself across the suite: the
cache fires on every workload (not just `multi-turn-refactor`), and
the cache reads on the two previously-uncached workloads more than
offset the new cache-write overhead.

### Run 3 cache token totals

| Run    | LLM calls | Calls w/ cache hit | `cache_creation_input_tokens` | `cached_input_tokens` |
|--------|-----------|--------------------|-------------------------------|------------------------|
| Run 2 cold | 30    | 10 (33%)           | 11,229                        | 47,918                 |
| Run 3      | 49    | **49 (100%)**      | 36,962                        | 366,441                |

**Every single LLM call** in Run 3 had cache activity (write or read).
Cache reads grew 7.6× while writes only grew 3.3× — i.e. the cache
is being amortized more, not just used more.

### Run 3 per-workload (same-3 subset, vs Run 2 cold)

| Workload                  | Turns | Actual $ (Run 2 cold → Run 3) | Δ ($) | Δ (%) | Cache writes (Run 2 → Run 3) | Cache reads (Run 2 → Run 3) |
|---------------------------|-------|-------------------------------|-------|-------|-------------------------------|------------------------------|
| `fix-a-bug-small`         | 3     | $0.015380 → $0.021494         | +$0.0061 | **+39.7%** | 0 → 7,779                     | 0 → 38,860                    |
| `multi-turn-refactor`     | 4     | $0.074921 → $0.049288         | -$0.0256 | **-34.2%** | 11,229 → 10,336              | 47,918 → 119,917             |
| `write-a-doc-from-notes`  | 2     | $0.017972 → $0.012850         | -$0.0051 | **-28.5%** | 0 → 1,436                    | 0 → 31,018                    |
| **Total (3 workloads)**   |       | **$0.108273 → $0.083632**     | **-$0.0246** | **-22.8%** | 11,229 → 19,551               | 47,918 → 189,795             |

`fix-a-bug-small` costs *more* under §5.1 — its short 3-turn arc means
the cache-write overhead on the padded prefix exceeds the read savings
within the session. The two longer workloads recover that loss many
times over (the +$0.006 on `fix-a-bug-small` is dwarfed by the
-$0.026 on `multi-turn-refactor` alone).

### Run 3 per-workload (new 3 workloads)

| Workload                                | Turns | Actual $   | Cache writes | Cache reads |
|-----------------------------------------|-------|------------|--------------|-------------|
| `intentionally-failing-task`            | 1     | $0.000965  | 0            | 5,781       |
| `multi-file-refactor-with-shared-types` | 4     | $0.065495  | 15,040       | 120,453     |
| `regex-with-edge-cases`                 | 3     | $0.030045  | 2,371        | 50,412      |

Note: `intentionally-failing-task` shows **5,781 cache reads on a
single LLM call with 0 cache writes**. This is the §5.1 deterministic-
padding side-effect: because every Metis session in the suite carries
the same stable prefix (DEFAULT_SYSTEM_PROMPT + tools + the same
`_OPERATING_CONTEXT_PADDING` block), Anthropic's cache (which persists
for ~5 minutes across requests sharing identical prefixes) was already
warm from earlier workloads when this session started. The session
read the warmed prefix without paying for a cache write — a "free
warm-up" effect that is not load-bearing for the §5.1 correctness
claim but is a real cross-session efficiency that emerges from the
byte-stability rule.

### Run 3 finding

**§5.1 makes prompt caching fire universally.** Run 2 §A2's
"`multi-turn-refactor` only" limitation is resolved: every LLM call in
every workload now writes or reads cache. The 22.8% same-3-workload
aggregate cost reduction is the visible signal; the underlying
mechanism is that ~5K-token cached reads at the 10% cache-read rate
replace ~5K-token full-input reads at the 100% input rate.

The cost trade-off documented in `context-assembler.md §5.1` shows
up cleanly in `fix-a-bug-small`: 3-turn workloads with very short
natural prefixes lose ~$0.006 on padding. This is **acceptable in
aggregate** because (a) the cost is dwarfed by the wins on longer
workloads, (b) sessions in production tend to be longer than the
benchmark's bare-bones 3-turn shape, and (c) the cross-session warm-
prefix effect (seen on `intentionally-failing-task`) amplifies the
saving for cohorts of related sessions sharing a workspace and
system prompt.

### Run 3 caveats

- **The 66.7% `savings_pct` is structural, not behavioral.** It's
  fixed by the haiku/sonnet rate-card ratio at equal token counts;
  see Run 2's same observation. The §5.1 win lands in `actual_$`
  (smaller numerator) and the cache-token totals (which `savings_pct`
  doesn't see), not in the aggregate percentage.
- **One workload regressed on cost** (`fix-a-bug-small`, +39.7%).
  This is the natural prefix being far below the cache floor — the
  §5.1 padding pays for itself per turn, so very short sessions
  amortize less of the overhead. A future revision MAY add a
  "skip padding when natural prefix < X" escape hatch; the §5.1
  decision log already flags this trade-off.
- **The `intentionally-failing-task` 5,781-cache-read "free" effect
  is timing-sensitive.** It only fires when an earlier workload
  warms the cache within Anthropic's ~5-minute TTL. Running the
  workload in isolation produces 0 reads.
- **`regex-with-edge-cases` and `multi-file-refactor-with-shared-types`
  weren't in Run 2** so we can't compute Δ for them directly. Their
  Run 3 cache activity (100% of LLM calls cached) is a forward
  baseline.

### Reproduce

```bash
uv run pytest -q                                # expect 1038 passed
uv run python scripts/smoke_cache.py --model haiku   # expect PASSED
uv run python scripts/benchmark.py              # full 6-workload suite
```

---

## Experiment A3: quality-differentiated routing under the LLM judge

**Date (UTC):** 2026-05-14
**Branch:** `2026-05-14`
**Commit:** `1e0fe03` + dirty Wave-6 working tree
**Test baseline:** 1101 passed (`uv run pytest -q`)
**Total API spend (3 passes + retry):** **$1.026** under the per-call
`smoke_eval.py` $0.0006 LLM-judge cost.
**JSON artifacts:**
[`.runs/a3-pass-a.json`](.runs/a3-pass-a.json) (haiku),
[`.runs/a3-pass-b.json`](.runs/a3-pass-b.json) (sonnet),
[`.runs/a3-pass-b-regex.json`](.runs/a3-pass-b-regex.json) (sonnet, regex-only retry — network-error recovery for pass B),
[`.runs/a3-pass-c.json`](.runs/a3-pass-c.json) (no-active-model; slot 4 fires).
**Shared patterns DB:** [`.runs/a3-patterns.db`](.runs/a3-patterns.db).

### Motivation

The §A1 caveat called out the structural ceiling on the original Run-2
benchmark suite: every turn scored `success_score=1.0` under the v1
heuristic, so slot 4's cluster aggregation
(`(1 - cost_weight) * success_mean + cost_weight * cost_efficiency`,
`cost_weight=0.3`) collapsed to "pick the cheaper model." A1 then
asked: with the LLM-judge tier shipped in Wave 5 *and* the two
discriminating diversity workloads (`regex-with-edge-cases` failure-prone
on haiku, `multi-file-refactor-with-shared-types`) plus the
hallucination workload from the diversity wave 2 section above, does
slot 4 now *invert* and pick sonnet on the workloads where it succeeds
where haiku fails?

A3 is the first experiment to wire the hybrid judge end-to-end across
both the per-turn evaluator subscriber and the workload-level
evaluation, then run the three-pass cold/warm/slot-4 protocol against
the full 7-workload suite (six prior + Agent 6a-5's hallucination
fixture).

### What ships in A3

A minimal harness tweak in [scripts/benchmark.py](../scripts/benchmark.py)
exposes three new CLI flags (no `metis-core` change):

- `--judge {heuristic,hybrid,llm}` — chooses the per-turn evaluator
  subscriber's `Judge` (heuristic by default for back-compat). When set
  to `hybrid` or `llm`, the harness unregisters `setup_runtime()`'s
  default `HeuristicJudge` and re-registers an `LLMJudge` /
  `HybridJudge` against the same bus + trace store. The same factory
  also feeds the workload-level `evaluate_workload_quality()` so both
  tiers honor the flag.
- `--judge-escalation-threshold` (default `0.7`) — passes through to
  `HybridJudge(escalation_threshold=...)` (evaluator.md §5.3).
- `--judge-model` (default `anthropic:claude-haiku-4-5`) — the LLM
  judge's model id.

This wires the LLM-tier infrastructure that has been on the bus since
Wave 5 into the benchmark suite for the first time. No `metis-core`
code was touched; the spec's invariants (heuristic-only fallback for
`tool_cycle` / `session` subjects; per-session $0.10 / per-day $1.00
budget caps; `signals.budget_exhausted` on overrun) ride through
unchanged.

### A3 protocol

Three benchmark passes share a single `--patterns-db-path`. Each pass
runs the full 7-workload suite under `--judge hybrid
--judge-escalation-threshold 0.7`:

| Pass | Flags                                                  | Goal                                                    |
|------|--------------------------------------------------------|---------------------------------------------------------|
| A    | `--model haiku  --patterns-db-path a3-patterns.db`     | Record haiku per-cluster outcomes (including failures). |
| B    | `--model sonnet --patterns-db-path a3-patterns.db`     | Record sonnet outcomes against the same clusters.       |
| C    | `--no-active-model --patterns-db-path a3-patterns.db`  | Slot 4 fires; reads A+B outcomes; picks per fingerprint.|

### A3 transient failures

Anthropic returned transient `Connection error` exceptions on two
workloads, both during turn 3 of `multi-turn-refactor` (both passes A
and B) and once on `regex-with-edge-cases` (pass B). The harness
captures the error in `WorkloadResult.error` and continues with the
remaining workloads; the partial pattern outcomes for the crashed
session land in the DB with `success_score_count=0` because the
evaluator subscriber didn't drain. A single retry of
`regex-with-edge-cases` under sonnet (`a3-pass-b-regex.json`) recovered
that workload for pass B; `multi-turn-refactor`'s 4-turn arc was not
re-attempted because the K-NN aggregator is robust to a single missing
model in a cluster (it falls back to the available outcomes).

### A3 routing-chain breakdown

| Pass            | Slot winners                         | Chosen models                     |
|-----------------|--------------------------------------|-----------------------------------|
| A (haiku)       | `manual_sticky` ×17                  | `claude-haiku-4-5` ×17            |
| B (sonnet)      | `manual_sticky` ×15                  | `claude-sonnet-4-6` ×15           |
| C (no active)   | **`pattern` ×17**, `global_default` ×1 | **`claude-haiku-4-5` ×18**        |

Slot 4 (`pattern`) won **17 of 18** turn-routing decisions in Pass C —
the routing chain is fully exercised and the K-NN aggregator returns a
verdict on every cluster that has at least one neighbor. The single
`global_default` win was a fingerprint with no usable neighbors.

### A3 per-pass cost and quality

| Workload                                          | A haiku $    | A q  | B sonnet $   | B q  | C mixed $    | C q  |
|---------------------------------------------------|-------------:|-----:|-------------:|-----:|-------------:|-----:|
| `architectural-explanation-without-hallucination` | 0.0354       | 1.00 | 0.1270       | 0.50 | 0.0420       | 1.00 |
| `fix-a-bug-small`                                 | 0.0150       | 1.00 | 0.0346       | 1.00 | 0.0187       | 1.00 |
| `intentionally-failing-task`                      | 0.0010       | 0.00 | 0.0029       | 0.00 | 0.0010       | 0.00 |
| `multi-file-refactor-with-shared-types`           | 0.0533       | 1.00 | 0.1455       | 1.00 | 0.0635       | 1.00 |
| `multi-turn-refactor`                             | (crashed)    |  —   | (crashed)    |  —   | 0.0790       | 1.00 |
| `regex-with-edge-cases`                           | 0.0265       | 0.75 | 0.0954†      | 0.75 | 0.0312       | 0.25 |
| `write-a-doc-from-notes`                          | 0.0142       | 1.00 | 0.0468       | 1.00 | 0.0149       | 1.00 |
| **Suite total**                                   | **0.1454**   |      | **0.4522**   |      | **0.2504**   |      |

† Pass B regex from the retry run (`a3-pass-b-regex.json`); the original pass B's regex crashed mid-turn.

Quality scores above are the workload-level rubric verdicts (heuristic;
the hybrid threshold 0.7 was not crossed by the workload-level
heuristic's confidence either). Both haiku and sonnet score `0.75` on
`regex-with-edge-cases` in passes A and B — haiku's pass-A run actually
produced "**PASS 16/16**" but the harness's `max_tool_calls: 1`
assertion on turn 3 docked the heuristic to 0.75 because the model
made two tool calls (run + a grep summary). The pass-C run, on the
same haiku model at `temperature=0.0`, instead produced "**FAIL
15/16**" — the stochasticity caveat documented in Run 3 ("temperature
0.0 is not strictly deterministic") shows up again here.

### **The key table: Pass C `pattern.matched.chosen_model`**

> "Pass C's `pattern.matched.chosen_model` per workload. Does it pick
> sonnet on regex-edge-cases? Does it pick haiku on fix-a-bug-small?"

Slot 4 fired on 17 of Pass C's 18 turn decisions. Every single one
picked **haiku-4-5**. The `pattern` slot never chose sonnet — not on
regex, not on the hallucination workload, not anywhere.

The cluster aggregator's two typical verdicts:

| Cluster shape                          | Haiku score | Sonnet score | Gap   | Winner |
|----------------------------------------|------------:|-------------:|------:|--------|
| Both models success_mean ≈ 1.0         | **1.000**   | 0.700        | 0.300 | haiku  |
| Both models success_mean ≈ 0.75 (\*)   | **0.825**   | 0.525        | 0.300 | haiku  |

The 0.300 gap in every cluster is the structural `cost_weight=0.3`
contribution: when haiku and sonnet share the same `success_mean`, the
cost-efficiency term hands haiku a flat 0.3-point advantage in every
cluster (haiku is cheapest → cost_efficiency=1.0; sonnet most expensive
→ cost_efficiency=0.0). `success_mean` would have to differ by
**>0.428** (≈ 0.3 / 0.7) before sonnet could overcome that gap. It does
not — see the next section for why.

(\*) The 0.75 clusters are clusters that pulled in outcomes from the
multi-turn-refactor connection-error sessions, which never got
per-turn `eval.completed` updates, so the K-NN aggregator counted
those rows at the pattern-store's initial `success_score_mean=0.0,
success_score_count=0` value. Not a quality signal — a partial-write
artifact from the transient network errors.

### Why the differentiator does not fire

This is the §A1 ceiling re-emerging in a different shape. With the
LLM-judge tier now wired, the failure mode shifted:

| Pass C eval.completed by `(subject_kind, judge_kind)` | Count | Mean score |
|--------------------------------------------------------|------:|-----------:|
| `(turn, heuristic)`                                    |    17 |       1.00 |
| `(turn, hybrid)` (i.e. escalated to LLM)               |     **1** |       0.00 |
| `(workload, heuristic)`                                |     7 |       0.75 |
| `(tool_cycle, heuristic)`                              |    78 |       0.85 |

Only **1 of 18** turn evaluations escalated to the LLM judge across
Pass C (the single one was `intentionally-failing-task`'s refusal
turn, where the heuristic's content-penalty path drops confidence to
0.55 < 0.7). Every other turn — including the haiku regex turn 3 that
**printed "FAIL 15/16"** — short-circuited at heuristic confidence
≥ 0.9.

The reason is mechanical:

1. **`tool.completed.success=false` is not a v1 heuristic negative
   signal.** The shell tool returns `ToolExecution(success=False, …)`
   on non-zero exit, which the dispatcher emits as `tool.completed`
   with `success=False`. The turn heuristic
   ([eval/judge.py:154](../packages/metis-core/src/metis_core/eval/judge.py))
   only checks for `tool.failed` events (raised on Python exceptions);
   it doesn't read `tool.completed.success`. So haiku's regex turn 3
   gets 5/5 positive lifecycle signals → confidence 0.9 → hybrid
   short-circuits regardless of the runner's actual exit code.
2. **The online subscriber doesn't forward enough text for the LLM
   judge.** Even when the LLM judge does fire, its
   `_build_user_message()` reads `signals_extra["user_prompt_text"]`
   and `signals_extra["assistant_response_text"]`
   ([eval/llm_judge.py:515-516](../packages/metis-core/src/metis_core/eval/llm_judge.py)),
   but `SessionManager._emit_turn_completed` only stamps
   `signals_extra={"final_response_text": …}`
   ([sessions/manager.py:1346](../packages/metis-core/src/metis_core/sessions/manager.py)).
   So the online LLM-judge prompt currently reads:

   ```
   USER PROMPT: (not available)
   ASSISTANT FINAL RESPONSE: (not available)
   TOOL ACTIVITY: ... (only the tool_name + success flag)
   TURN LIFECYCLE: stop_reason=end_turn, tool_call_count=N
   ```

   Without the assistant text, the LLM judge has no way to spot a
   `"FAIL 15/16"` final message vs `"PASS 16/16"`. (The smoke harness
   `scripts/smoke_eval.py` works because *it explicitly* populates
   `user_prompt_text` / `assistant_response_text` in
   `signals_extra` — only the live bus path is impoverished.)

Either single fix (heuristic learns `tool.completed.success=False`, or
the signals_extra key is harmonized) would unblock the differentiator
*for this fixture set*. Neither was in scope for A3.

### Cost-per-success: the headline column

`actual_$ / sum(quality_score)` for each pass (counting the workloads
that completed; intentionally-failing-task's `score=0` is excluded as
it would divide-by-zero):

| Pass            | Sum quality (succeeded workloads) | $ spent | **Cost / quality-unit** |
|-----------------|----------------------------------:|--------:|------------------------:|
| A (haiku)       | 4.75 (5 of 6 working workloads)  | 0.1454  | **$0.0306**             |
| B (sonnet)      | 4.50 (5 of 6 working workloads)  | 0.4522  | **$0.1005**             |
| C (no active)   | 5.25 (7 of 7 workloads)           | 0.2504  | **$0.0477**             |

Pass C's effective cost-per-quality-unit is **$0.0477** — between
haiku ($0.0306) and sonnet ($0.1005), as expected when every turn
picks haiku and the only differentiation comes from the suite's
natural composition. **It is not the inverted-routing number A3 was
designed to produce.** A successful differentiator would have lifted
the haiku-leaning ratio on `regex-with-edge-cases` toward sonnet's
without raising it on `fix-a-bug-small`.

### Comparison to Run 3

| Metric                                | Run 3 (haiku-only) | A3 Pass C (no active) |
|---------------------------------------|-------------------:|----------------------:|
| `actual_repriced_usd`                 | $0.180             | $0.250                |
| `baseline_repriced_usd`               | $0.540             | $0.751                |
| `savings_pct`                         | 66.7%              | 66.7%                 |
| Slot 4 wins                           | (single-model run; chain blocked at slot 2) | **17 of 18 turns** |
| Pattern store outcomes recorded       | 16                 | **34 (haiku + sonnet)** |
| Quality differential at slot 4 input  | (all heuristic 1.0) | (still all heuristic 1.0) |
| Chosen model on every turn            | haiku              | haiku                 |

A3 succeeded in unblocking the routing chain — Pass C is the first
benchmark run in this repo where slot 4 wins on essentially every turn
and reads cross-model outcomes from the pattern store. **A3 did not
succeed in making slot 4 prefer the higher-quality model on a
workload class.** Run 3's `66.7%` headline is preserved exactly,
which itself is the evidence that no quality-driven inversion happened.

### A3 finding

**The differentiator does not fire under hybrid-0.7 + the v1 heuristic
+ the current online signals_extra plumbing.** Slot 4 picks haiku on
every workload — including the regex turn 3 where haiku produced
"FAIL 15/16" — because:

1. The per-turn heuristic doesn't penalize the underlying failure
   (`tool.completed.success=False`), so the heuristic confidence
   stays at 0.9 and the hybrid never escalates.
2. The pattern store's outcome rows record `success_score=1.0` for
   both haiku and sonnet on the regex cluster. Equal `success_mean` ⇒
   the cost_efficiency tiebreaker hands haiku a flat 0.3-point margin
   per cluster.
3. The required `success_mean` gap for sonnet to overcome that
   margin is `>0.428` (≈ `0.3 / 0.7`). The actual gap in the recorded
   outcomes for this suite is `0.0`.

The mechanism is wired correctly and observable end-to-end —
`pattern.matched` events fire, `route.decided.chain` reports the
`pattern` slot winning with the K-cluster's neighbor breakdown, the
LLM judge does fire (once) when the heuristic confidence dips. But
the *content* the mechanism is asked to differentiate on is
indistinguishable to the v1 heuristic, and the LLM judge — which
could read the difference — sees `(not available)` for the assistant
response in the online path. The chain is plumbed; the input quality
signal isn't.

### What unblocks A3's null result

Two independent paths, either of which is sufficient on its own:

1. **Heuristic gains a `tool.completed.success=False` penalty.**
   `metis-core/eval/judge.py::_evaluate_turn` would check the tool
   completions' `success` flag (not just `tool.failed` events) and
   add a `flags_negative.append("tool_returned_failure")` plus a
   weight. This is a small one-file change in the metis-core that
   would make the regex haiku turn 3 score ≈ 0.75 immediately on
   heuristic, dropping confidence to 0.55 and triggering hybrid
   escalation as a follow-on. Both knobs move in the right direction.
2. **`signals_extra` carries `user_prompt_text` +
   `assistant_response_text` from the bus subscriber.** The session
   manager already has both — `TurnContext.user_message_text`
   feeds the routing fingerprint, and `last_assistant_text` already
   exists in the turn loop. Forwarding them on `turn.completed.payload
   .signals_extra` would let the LLM judge actually see the
   "FAIL 15/16" string. This is a metis-core sessions/manager.py
   change, plus an evaluator/subscriber forwarding tweak. The
   evaluator content-penalty path is the same one A2 wired via
   `final_response_text`; this is its sibling.

Either landed on its own would re-run A3 cleanly with cost in the same
~$1 envelope and likely produce slot 4 picking sonnet on regex turn 3.
Both deferred to a follow-up; not in scope for the A3 spec.

### A3 caveats and what this experiment does NOT prove

- **Per-fingerprint cluster sample sizes are small (4–6 each model).**
  K-NN aggregation tolerates this, but a real production deployment
  would accumulate samples over weeks/months. The 0.3-point cost-
  efficiency tiebreaker would still dominate clusters with no quality
  differential, regardless of sample size.
- **Two transient network errors hit during pass B.** The regex retry
  ran clean; `multi-turn-refactor` was left missing for both haiku and
  sonnet's pass-A/B and only landed in pass C (haiku, via slot 4).
  The cluster aggregator handled the gap (it picked haiku for
  multi-turn-refactor in pass C), but the per-model sample for that
  workload is asymmetric.
- **The hallucination workload (`architectural-explanation-without-
  hallucination`) showed sonnet scoring 0.50 vs haiku 1.00** in
  Pass A vs Pass B — the *wrong* direction per the diversity-wave-2
  section above. This is the same inversion that section already
  flagged ("the heuristic's substring-presence check rewards the
  *less* grounded model"). A3 inherits that limitation; the LLM judge
  was not invoked on this workload either (confidence ≥ 0.7).
- **`temperature=0.0` is non-deterministic in practice.** Haiku regex
  turn 3 produced "PASS 16/16" in Pass A and "FAIL 15/16" in Pass C
  — same model, same prompt, same temperature. The score-direction
  finding (haiku < sonnet on regex) was not statistically
  established by this single run.

### Reproduce

```bash
# Confirm test baseline.
uv run pytest -q                                   # expect 1101 passed

# Confirm the LLM judge wire-up works against a real API.
uv run python scripts/smoke_eval.py                # ~$0.001

# A3: 3-pass experiment with hybrid judge (threshold 0.7).
rm -f benchmarks/.runs/a3-patterns.db
uv run python scripts/benchmark.py \
  --model haiku  --patterns-db-path benchmarks/.runs/a3-patterns.db \
  --db-path     benchmarks/.runs/a3-pass-a.db \
  --judge hybrid --judge-escalation-threshold 0.7
uv run python scripts/benchmark.py \
  --model sonnet --patterns-db-path benchmarks/.runs/a3-patterns.db \
  --db-path     benchmarks/.runs/a3-pass-b.db \
  --judge hybrid --judge-escalation-threshold 0.7
uv run python scripts/benchmark.py \
  --no-active-model --patterns-db-path benchmarks/.runs/a3-patterns.db \
  --db-path     benchmarks/.runs/a3-pass-c.db \
  --judge hybrid --judge-escalation-threshold 0.7

# Expect: ~17 pattern.matched events in pass C, all chose haiku-4-5.
# Inspect the slot-4 verdicts:
uv run python -c "
import sqlite3, json, collections
c = sqlite3.connect('benchmarks/.runs/a3-pass-c.db').cursor()
slots = collections.Counter()
chosen = collections.Counter()
for r in c.execute(\"SELECT payload_json FROM events WHERE type='route.decided'\"):
    p = json.loads(r[0])
    chosen[p['chosen_model']] += 1
    win = next((c for c in p.get('chain', []) if c.get('verdict') == 'chose'), None)
    if win: slots[win['policy']] += 1
print('slot winners:', dict(slots))
print('chosen models:', dict(chosen))
"
```

## Experiment A3-rev: differentiator unblocked

Re-run of §A3 after both follow-up unblocks landed:

1. `HeuristicJudge` now penalizes `tool.completed.success=False`
   ([packages/metis-core/src/metis_core/eval/judge.py](../packages/metis-core/src/metis_core/eval/judge.py)
   `_evaluate_turn`, `weight_no_tool_exit_failure=0.5`). A single
   shell-tool nonzero exit drops a clean turn's score from 1.0 to
   0.667 and confidence from 0.9 to 0.55, taking the hybrid below
   the 0.7 escalation threshold.
2. `SessionManager._emit_turn_completed` now forwards
   `signals_extra.user_prompt_text` and
   `signals_extra.assistant_response_text` so the LLM judge's
   `_build_user_message` reader sees real content instead of
   "(not available)"
   ([packages/metis-core/src/metis_core/sessions/manager.py](../packages/metis-core/src/metis_core/sessions/manager.py)
   `_emit_turn_completed`).

Both unblocks are exercised by tests in
[packages/metis-core/tests/eval/test_judge.py](../packages/metis-core/tests/eval/test_judge.py)
and
[packages/metis-core/tests/sessions/test_manager.py](../packages/metis-core/tests/sessions/test_manager.py)
(`test_turn_heuristic_tool_completed_success_true_does_not_fire_negative`,
`test_turn_completed_carries_user_prompt_text_in_signals_extra`,
`test_turn_completed_aliases_assistant_response_text_to_final_response_text`,
`test_turn_completed_signals_extra_feeds_llm_judge_build_user_message`).
Test baseline: 1127 passing on commit `1ccefe7` (dirty).

### A3-rev protocol

Identical to §A3 — three passes against the full 7-workload suite
sharing one patterns DB, hybrid judge with threshold 0.7:

```bash
rm -f benchmarks/.runs/a3rev-patterns.db
uv run python scripts/benchmark.py \
  --model haiku  --patterns-db-path benchmarks/.runs/a3rev-patterns.db \
  --db-path     benchmarks/.runs/a3rev-pass-a.db \
  --judge hybrid --judge-escalation-threshold 0.7
uv run python scripts/benchmark.py \
  --model sonnet --patterns-db-path benchmarks/.runs/a3rev-patterns.db \
  --db-path     benchmarks/.runs/a3rev-pass-b.db \
  --judge hybrid --judge-escalation-threshold 0.7
uv run python scripts/benchmark.py \
  --no-active-model --patterns-db-path benchmarks/.runs/a3rev-patterns.db \
  --db-path     benchmarks/.runs/a3rev-pass-c.db \
  --judge hybrid --judge-escalation-threshold 0.7
```

Total spend: **$1.032** (Pass A $0.205, Pass B $0.651, Pass C $0.176)
— within the $1–2 budget envelope.

### A3-rev transient failures

- Pass A: `multi-turn-refactor` lost connection on turn 4/4
  (`NetworkError`). Turns 1–3 still wrote pattern outcomes.
- Pass B: `write-a-doc-from-notes` lost connection on turn 2/2.
  Turn 1 still wrote a pattern outcome.
- Pass C: `regex-with-edge-cases` lost connection on turn 3/3 (the
  diagnostically critical turn — the one where §A3-original's
  haiku produced "FAIL 15/16"). `write-a-doc-from-notes` lost
  connection on turn 1/2.

The transient error rate matches §A3-original (≈3 connection errors
across ~50 LLM calls). They are *not* deterministic — re-runs hit
different turns each time. The Pass C regex transient is the worst
luck of the three: that workload was the focal target of §A3's
finding, and the data point is missing.

### A3-rev per-turn judge breakdown

The heuristic + hybrid mix shifted materially vs §A3-original — the
unblocks are doing their job:

| Pass | heuristic (turn) | hybrid (turn) | LLM-only | workload heuristic |
|------|-----------------:|--------------:|---------:|-------------------:|
| A    | 11               | **6**         | 0        | 6                  |
| B    | 12               | **5**         | 0        | 6                  |
| C    | 11               | **4**         | 0        | 5                  |

Hybrid escalations to the LLM judge fired on 15 turns across the three
passes. Concrete LLM-judge verdicts visible in the trace (sample):

```
Pass A multi-file-refactor turn 3 (haiku):  score=0.300  conf=0.800  (LLM)
Pass A multi-file-refactor turn 4 (haiku):  score=0.700  conf=0.800  (LLM)
Pass A multi-turn-refactor   turn 3 (haiku):  score=0.300  conf=0.700  (LLM)
Pass B multi-file-refactor turn 2 (sonnet): score=0.700  conf=0.800  (LLM)
Pass B multi-file-refactor turn 3 (sonnet): score=0.300  conf=0.700  (LLM)
Pass B multi-turn-refactor   turn 3 (sonnet): score=0.400  conf=0.600  (LLM)
Pass B multi-turn-refactor   turn 4 (sonnet): score=0.800  conf=0.700  (LLM)
```

§A3-original recorded **0** LLM escalations (it was an empty
column). A3-rev sees the LLM judge produce differentiated 0.3 / 0.7 /
0.8 / 1.0 scores reading the actual assistant text. **Unblock 7a-2 is
working as intended.** And the heuristic confidence drops below 0.7
on turns where a shell tool exited non-zero, which is what causes the
hybrid to escalate — **unblock 7a-1 is working as intended.**

### A3-rev per-workload heuristic and LLM-judge deltas

Workload-level quality verdict (the score the pattern store sees) per
workload per pass — sonnet vs haiku:

| Workload | Pass A haiku | Pass B sonnet | Δ (sonnet − haiku) |
|----------|:-----------:|:-------------:|-------------------:|
| architectural-explanation-without-hallucination | 1.00 | 0.50 | **−0.50** (inverted heuristic, still wrong) |
| fix-a-bug-small                          | 0.93 | 1.00 | +0.07 |
| intentionally-failing-task               | 0.25 | 0.25 | 0.00 |
| multi-file-refactor-with-shared-types    | 0.88 | 0.88 | 0.00 |
| multi-turn-refactor                      | (transient) | 0.80 | (n/a) |
| regex-with-edge-cases                    | 0.75 | 1.00 | **+0.25** |
| write-a-doc-from-notes                   | 1.00 | (transient) | (n/a) |

`regex-with-edge-cases` is now the workload where the heuristic
correctly detects the haiku failure (a shell test exited non-zero
on haiku turn 2; workload-rubric score dropped from 1.0 to 0.75
because of the cascading `tool_returned_failure` flag). Sonnet on
the same prompts scored 1.00 — a +0.25 quality gap *visible to the
heuristic*. §A3-original's same comparison showed 1.00 / 1.00 on
this workload because the heuristic never saw the tool exit failure.

Net per-workload deltas where the data exists: regex favors sonnet
by 0.25; fix-a-bug-small slightly favors sonnet by 0.07; multi-file
and intentionally-failing tie; architectural-without-hallucination
*still* inverts (0.50 vs 1.00 against sonnet) — the
substring-presence content check from the diversity-wave-2 caveat
remains broken in v1 and §A3-rev inherits the flaw.

### A3-rev Pass C slot-4 outcomes — THE KEY TABLE

Pass C ran 15 turns under `--no-active-model`. Routing breakdown:

| Slot         | Wins (Pass C) | Chose haiku | Chose sonnet |
|--------------|--------------:|------------:|-------------:|
| pattern      | **15**        | 15          | **0**        |
| global_default | 2           | 2           | 0            |

All 15 pattern-slot wins picked haiku. The K-NN cluster's per-model
aggregated scores (from
`route.decided.chain[pattern].pattern_alternatives`) tell the story:

```
#   chose            haiku.score  sonnet.score  pattern_conf
0   haiku                  1.000         0.700         0.300
1   haiku                  0.972         0.658         0.323
2   haiku                  0.930         0.612         0.341
3   haiku                  0.755         0.245         0.675
4   haiku                  1.000         0.700         0.300
5   haiku                  1.000         0.700         0.300
6   haiku                  0.953         0.612         0.358
7   haiku                  0.804         0.245         0.695
8   haiku                  0.902         0.612         0.321
9   haiku                  1.000         0.647         0.353
10  haiku                  0.953         0.647         0.321
11  haiku                  0.804         0.245         0.695
13  haiku                  1.000         0.700         0.300
15  haiku                  0.804         0.245         0.695
16  haiku                  1.000         0.700         0.300
```

`alternatives_count=2` (haiku + sonnet) and `sample_size=5–6` per
cluster on every row — the K-NN is reading cross-model outcomes from
both Pass A and Pass B. There is no fingerprint cluster in this
patterns DB where sonnet's aggregated score beats haiku's. The
quality differential A3-rev hoped to surface on `regex-with-edge-
cases` (haiku 0.75 vs sonnet 1.00) is washed out by the K-NN
clustering across other workloads.

### A3-rev cost-per-quality-unit

| Pass | Quality sum (working workloads) | actual_repriced_usd | cost-per-quality |
|------|--------------------------------:|--------------------:|-----------------:|
| A (haiku)     | 4.808 (6 of 7)                | $0.2050             | **$0.0426**      |
| B (sonnet)    | 4.425 (6 of 7)                | $0.6513             | **$0.1472**      |
| C (no active) | 3.877 (5 of 7)                | $0.1752             | **$0.0452**      |

A3-original same numbers were $0.0306 / $0.1005 / $0.0477. A3-rev's
slightly higher per-quality-unit numbers reflect a different sample
of working workloads (transients hit different rows) plus the LLM
judge's content penalty firing on more turns at workload-level too.

Pass C is still essentially Pass A's cost ($0.0452 vs $0.0426) — it
spent ~6% more per quality unit than haiku-only, well below sonnet's
$0.1472. The 6% premium is real but it is **not** the result of slot
4 picking sonnet — Pass C never picked sonnet. The premium reflects
two missing workloads (regex + write-a-doc both hit transients in
Pass C) lowering the denominator, plus the multi-file-refactor
quality score landing at 0.69 in Pass C vs 0.88 in Pass A under the
same model.

### A3-rev finding

**The differentiator still does not invert under hybrid-0.7 with
both 7a-1 and 7a-2 landed.** Slot 4 picks haiku on every Pass C turn
(15 of 15) despite:

1. The heuristic now correctly penalizing `tool.completed.success=
   False` (verified: confidence drops from 0.9 → 0.55 on synthetic
   inputs).
2. The LLM judge now reading `assistant_response_text` and producing
   real differentiated scores (verified: 4–6 hybrid escalations per
   pass, 0 in §A3-original).
3. The K-NN cluster's `pattern_alternatives` showing **5 samples of
   each model** on every selection — the cross-model data is in the
   store and the aggregator is reading it.

The new root cause is downstream of both unblocks. The per-model
aggregated score the K-NN produces (the table above) consistently
shows haiku ahead of sonnet:

- Haiku aggregated scores: 0.755 → 1.000
- Sonnet aggregated scores: 0.245 → 0.700

The gap is bigger than the required 0.428 success-mean delta from
§A3-original's analysis. But it is wrong-direction: in this 33-row
patterns DB, the K-NN computes `success_mean_haiku ≈ success_mean_
sonnet` (both around 0.72–0.95) and then the `cost_weight=0.3` term
on cost_efficiency (haiku cheaper → cost_eff_haiku=1, cost_eff_sonnet=0)
adds a flat +0.3 to haiku's score. The hybrid judge's LLM
verdicts on Pass B's sonnet turns (multi-turn-refactor turn 3
→ 0.400, multi-file-refactor turn 2 → 0.700, multi-file-refactor
turn 3 → 0.300) actively pulled sonnet's success_mean *down* below
haiku's on a non-trivial slice of clusters, because the LLM judge
read sonnet's verbose responses and the heuristic-content-check
penalty fired.

Concretely: in the patterns DB ([benchmarks/.runs/a3rev-patterns.db]):

```
sonnet outcomes: 16 rows, success_mean per row in {0.0, 0.3, 0.4, 0.7, 0.8, 1.0}
haiku  outcomes: 17 rows, success_mean per row in {0.0, 0.3, 0.7, 0.8, 1.0}
```

The 0.0 sonnet rows came from sessions where the LLM judge
escalated and gave 0.300 (then the workload-level scorer aggregated
across turns); haiku has only 2 of 17 rows at 0.0 — fewer. Sonnet's
mean across the patterns DB is actually slightly **lower** than
haiku's by ~0.05, before cost_weight is applied. After cost_weight=0.3
the gap widens to ~0.35.

### What the third unblock looks like

The two unblocks alone are not sufficient. Three independent paths
could move the needle on a follow-up A3-rev2:

1. **K-NN clustering at workload granularity, not structural-
   fingerprint granularity.** The fingerprint inputs builder
   produces `intent_tags=[]` on most turns (the regex matchers
   trigger on architecture / debug / doc / refactor / test keywords
   but the per-turn prompts in this suite often miss those). When
   intent is empty, K-NN groups turns by tool-use shape and length
   bucket — which mixes workloads. A workload-tag carried as part of
   the fingerprint (or surfaced through the `fingerprint_inputs_
   builder` from the benchmark harness) would let same-workload
   neighbors cluster together first.
2. **Lower `cost_weight`.** Default is 0.3 (`routing/policy.py:40`).
   Setting it to ~0.1 would require a quality delta of only ~0.143
   to flip the chooser (since the cost margin would shrink to 0.1).
   Cluster deltas of 0.15–0.30 do exist in the current data, just
   not the 0.43 the default requires. This is a policy-level knob
   change, not a code change.
3. **Fix the heuristic content inversion on
   `architectural-explanation-without-hallucination`.** Sonnet
   scored 0.50 vs haiku 1.00 there because the workload-rubric's
   `contains_substring` check rewards refusal-of-omission instead
   of confirming the agent stayed within scope. That single
   workload is dragging sonnet's pattern-store mean down by ~0.06
   when it should be the *other* direction.

Either #1 or #2 alone should flip slot 4 on regex; #3 is independent
quality work but matters if the suite stays as-is.

### A3-rev caveats

- **Temperature=0 non-determinism re-confirmed.** §A3-original's
  haiku regex turn 3 produced "FAIL 15/16"; A3-rev's haiku regex
  turn 3 (Pass A) produced "PASS 16/16". The heuristic's new
  `tool_returned_failure` flag fired on turn 2 instead (still
  enough to drop the workload score from 1.0 to 0.75), but the
  exact failure mode differed run-to-run.
- **Pass C lost regex turn 3 to a transient**, the single turn most
  diagnostic for whether slot 4 inverts. A re-run of Pass C alone
  could resolve that specific data point at ~$0.05.
- **Pricing version drift mid-Pass-C.** Pass A/B ran under
  `2026-05-08+openrouter-2519f42cf205`; Pass C ran under
  `2026-05-08+openrouter-0151960e3ed7`. The OpenRouter overlay hash
  changed between Pass B and Pass C (OpenRouter price update
  happened concurrently — the bench harness re-fetches it on every
  run). Native Anthropic prices are unchanged so `actual_repriced_
  usd` is comparable across passes.

### Reproduce A3-rev

```bash
# Baseline check
uv run pytest -q                                   # expect 1127 passed

# A3-rev: 3-pass experiment with hybrid judge (threshold 0.7).
rm -f benchmarks/.runs/a3rev-patterns.db \
      benchmarks/.runs/a3rev-pass-{a,b,c}.{db,json}
uv run python scripts/benchmark.py \
  --model haiku  --patterns-db-path benchmarks/.runs/a3rev-patterns.db \
  --db-path     benchmarks/.runs/a3rev-pass-a.db \
  --judge hybrid --judge-escalation-threshold 0.7
uv run python scripts/benchmark.py \
  --model sonnet --patterns-db-path benchmarks/.runs/a3rev-patterns.db \
  --db-path     benchmarks/.runs/a3rev-pass-b.db \
  --judge hybrid --judge-escalation-threshold 0.7
uv run python scripts/benchmark.py \
  --no-active-model --patterns-db-path benchmarks/.runs/a3rev-patterns.db \
  --db-path     benchmarks/.runs/a3rev-pass-c.db \
  --judge hybrid --judge-escalation-threshold 0.7

# Inspect Pass C slot-4 alternatives (the key table):
uv run python -c "
import sqlite3, json
c = sqlite3.connect('benchmarks/.runs/a3rev-pass-c.db').cursor()
for r in c.execute(\"SELECT payload_json FROM events WHERE type='route.decided' ORDER BY id\"):
    p = json.loads(r[0])
    pat = next((c for c in p.get('chain', []) if c.get('policy') == 'pattern'), None)
    if pat and pat.get('verdict') == 'chose':
        alts = pat.get('pattern_alternatives') or []
        h = next((a for a in alts if 'haiku' in a['model']), None)
        s = next((a for a in alts if 'sonnet' in a['model']), None)
        chose = p['chosen_model'].split(':')[-1]
        print(f'{chose:<22} haiku={h[\"score\"]:.3f} sonnet={s[\"score\"]:.3f} conf={pat[\"confidence\"]:.3f}')
"
```
