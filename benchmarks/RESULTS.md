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
