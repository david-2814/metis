# Evaluator Specification

**Status:** v1 (heuristic + LLM + hybrid tiers shipped)
**Last updated:** 2026-05-14

> Defines the feedback loop that turns "was this turn successful?" into a
> recorded signal the pattern store and the analytics surface can read. This
> closes the open question in [`STRATEGY.md §6.7`](../STRATEGY.md): without an
> evaluator, "is the system actually saving money vs naive sonnet-everywhere?"
> stays an open question forever — savings without quality is just a smaller
> bill for worse work.
>
> v1 is heuristics-first with an opt-in LLM-as-judge tier. No labeled
> training data, no fine-tuned classifier, no SaaS dependency. The judge
> outputs a numeric `score` in `[0, 1]` with a `confidence`, written as a
> bus event so every consumer (pattern store, `/analytics/*`, the dashboard)
> reads from the same record.

---

## 1. Purpose

The build records *spend* honestly today: `llm.call_completed.cost_usd` is
stamped at write time, `/analytics/savings` re-prices the counterfactual,
[`benchmark.md`](benchmark.md) bounds the workload. None of that proves the
agent's *output* was good — and the savings-vs-naive-sonnet pitch collapses
the moment a buyer asks "how do you know haiku produced an answer worth
keeping?"

The evaluator answers that question. It consumes the events already in the
bus (`turn.completed`, `tool.completed`, `feedback.*`, `route.decided`) and
emits a *verdict* per subject — turn, tool cycle, session, or benchmark
workload. The verdict is a single numeric score with a confidence and the
provenance of the judge that produced it.

This spec depends on:

- [`event-bus-and-trace-catalog.md`](event-bus-and-trace-catalog.md) for the
  events the evaluator subscribes to (`turn.completed`, `tool.completed`,
  `tool.failed`, `feedback.explicit`, `feedback.implicit`, `route.decided`)
  and the catalog the new `eval.*` events join.
- [`canonical-message-format.md §9.1`](canonical-message-format.md) for the
  trace store schema the evaluator reads and writes through.
- [`analytics-api.md`](analytics-api.md) for the projection conventions the
  new `/analytics/quality` endpoint follows.
- [`benchmark.md §2.2`](benchmark.md) for the v1 limitation this spec closes
  (quality scoring deferred to the evaluator).
- [`routing-engine.md §5.5`](routing-engine.md) for the pattern store's
  consumption shape (`success_score` in `[0, 1]`, weighted with cost).

This spec coordinates with the planned [`pattern-store.md`](pattern-store.md)
(drafted in parallel; see §15). Touchpoints are listed there so the two specs
reconcile before either implementation lands.

---

## 2. Goals and non-goals

### 2.1 Goals

1. **Closes STRATEGY.md §6.7.** Every turn (and every workload run, and
   every flagged tool cycle) yields a recorded verdict. "Is the system saving
   money on tasks that succeeded?" becomes a join, not a guess.
2. **Heuristic floor, LLM ceiling.** v1 ships heuristic judges that cost
   $0/turn and a hybrid escalation tier where an LLM-as-judge is invoked only
   when the heuristic's confidence is below a threshold. The expensive judge
   is *opt-in*, not the default.
3. **Re-evaluatable.** Every verdict is a record, not a mutation. Re-running
   the evaluator over an older window produces *new* verdicts joined to the
   same subjects — the dashboard's "evaluator agreement rate over time" tile
   ([§9.2](#92-analytics-quality)) is a query, not a side-table.
4. **Single-user / local-first.** Workspace-scoped by default per
   [`STRATEGY.md §2`](../STRATEGY.md); the verdict store lives next to the
   trace store. No external service, no labeling pipeline.
5. **Budget-explicit.** LLM-as-judge calls cost money. The evaluator's
   own spend is recorded (`eval.completed.judge_cost_usd`), capped per
   session and per day, and surfaced under
   `/analytics/cost?group_by=model&include_eval=true` so an operator can see
   "how much did the judge cost vs how much did it save."
6. **Bus-as-spine.** Output is `eval.started` / `eval.completed` /
   `eval.failed` on the existing bus. No private side-channels; every
   consumer is a normal subscriber.
7. **Verdict shape is small and stable.** The numeric `score` is a single
   field, so the pattern store ([routing-engine.md §5.5](routing-engine.md))
   plugs in without bespoke aggregation. Multi-dimensional rubrics are an
   *additive* extension under `signals` — never a breaking change to the
   score field.

### 2.2 Non-goals

1. **No supervised training, no labeled corpus.** The single-user local
   deployment has no labeling pipeline. Calibration is by self-consistency
   (the agreement-rate view) and by spot-check against
   [`benchmark.md`](benchmark.md) workloads with hand-asserted expectations,
   not by fitting a model to a labeled set.
2. **No quality scoring inside the routing chain.** The evaluator runs
   *after* `turn.completed`. Routing decisions for the *current* turn still
   come from the chain in [`routing-engine.md §4`](routing-engine.md). Verdicts
   feed the **pattern store** (slot 4) which influences *future* turns —
   never the current one. This preserves the turn-locked model invariant
   ([`AGENTS.md` "Gotchas"](../../AGENTS.md)).
3. **No verdict overriding the user.** A low score never silently degrades
   the user's chosen model, never auto-escalates mid-turn, never refuses to
   route. The evaluator is an observer; the user (or a future
   pattern-disagreement banner per
   [`routing-engine.md §5.6`](routing-engine.md)) is the actor.
4. **No real-time LLM-as-judge in v1.** The heuristic judge is fast-enough
   to run inline on the `turn.completed` event. The LLM judge runs out-of-band
   (batch / on-demand), not on the fast path. Promotion to a faster tier is
   a §13 open question.
5. **No multi-judge ensembling.** v1 picks one judge per verdict and records
   it. Ensembling (running two judges and combining their scores) is
   tempting and a v2 question.
6. **No quality scoring of *intermediate model output* (tokens
   mid-stream).** The judge sees the completed turn / cycle / session — the
   atomic unit. Token-level scoring is a different research project.
7. **No remote / multi-tenant rollups.** Per
   [`STRATEGY.md §2`](../STRATEGY.md); deferred until the gateway path
   ([`deployment-shape.md`](deployment-shape.md)) needs it.

---

## 3. What the evaluator evaluates

The evaluator produces a verdict per *subject*. Four subject kinds, ordered
by inclusion (each kind is the next-level aggregation of the prior):

| Subject kind  | Identifier     | Bus trigger                            | Typical judge                    | v1? |
|---------------|----------------|----------------------------------------|----------------------------------|-----|
| `turn`        | `turn_id`      | `turn.completed`                       | heuristic; LLM optional          | yes |
| `tool_cycle`  | `tool_use_id`  | `tool.completed` / `tool.failed`       | heuristic only                   | yes |
| `session`     | `session_id`   | `session.ended`                        | heuristic-over-turns aggregation | yes |
| `workload`    | `workload_run_id` | benchmark harness final report     | heuristic + optional LLM         | yes |

A `tool_cycle` verdict scopes *one* tool invocation — did the dispatch
return a useful result, or did the agent re-call the same tool with different
arguments three calls later (tool thrash)? Tool-cycle verdicts roll up into
the parent turn verdict's `signals` but do *not* arithmetic-average into
the turn `score`; the heuristic rubric ([§5.1](#51-turn-heuristic-rubric))
decides how much weight each signal carries.

A `session` verdict is the per-turn weighted average plus session-scoped
signals (explicit feedback, manual `/model` swaps inside the session, etc.).

A `workload` verdict subsumes [`benchmark.md §2.2.2`](benchmark.md)'s "no
quality scoring in v1" gap. The benchmark harness calls the evaluator with
`subject_kind=workload` after the suite run; the workload-level rubric is
defined per workload in `workload.yaml` ([§5.4](#54-workload-rubric)).

**No mid-turn evaluation.** The evaluator never fires inside a turn. A turn
that's still running has no `turn.completed` event yet, so the subscription
filter never matches it. This is what keeps the evaluator off the fast path
and out of the turn-locked model contract.

---

## 4. The verdict

### 4.1 Shape

`EvalVerdict` is a `msgspec.Struct(frozen=True)` carried as the payload of
`eval.completed`:

```python
class EvalVerdict(msgspec.Struct, frozen=True):
    eval_id: str                                    # monotonic ULID
    subject_kind: Literal["turn", "tool_cycle", "session", "workload"]
    subject_id: str                                 # turn_id / tool_use_id / session_id / workload_run_id
    score: float                                    # in [0.0, 1.0]; 1.0 = clear success, 0.0 = clear failure
    confidence: float                               # in [0.0, 1.0]; judge's confidence in `score`
    judge_kind: Literal["heuristic", "llm", "hybrid"]
    judge_model: str | None                         # canonical id when llm or hybrid used the LLM tier; else None
    judge_cost_usd: Decimal                         # 0 for heuristic; > 0 for llm/hybrid
    judge_pricing_version: str | None               # set when judge_cost_usd > 0
    judge_latency_ms: int                           # wall time for this verdict alone
    rubric_id: str                                  # which rubric produced this (e.g. "turn-heuristic-v1")
    rubric_version: str                             # rubric's own version string
    signals: dict[str, object]                      # judge-specific evidence (see §4.4)
    parent_eval_id: str | None                      # for tool_cycle → turn / turn → session rollups
    created_at: str                                 # ISO 8601 UTC
```

### 4.2 The `score` field

`score` is a single number in `[0.0, 1.0]`. Higher is better. This shape is
deliberate:

- The pattern store's
  ([`routing-engine.md §5.5`](routing-engine.md)) `normalized_success_M`
  formula expects `success_score` in `[0, 1]`. One number, one consumer
  contract.
- Multi-dimensional rubrics (correctness × completeness × verbosity × …)
  are expressible as `signals` and *collapse* to one `score` via the
  rubric's own weights. The rubric is versioned (`rubric_version`) so a
  weight change is observable as a new score series.
- Single numbers compose. Session score = weighted average of turn scores.
  Workload score = weighted average of session scores. Multi-dimensional
  scores require a per-dimension policy at every join — needless complexity
  for v1.

The "score is one number" rule is the **only** structural commitment to the
pattern store consumer. Everything else under `signals` is judge-internal
and may evolve without breaking routing.

### 4.3 The `confidence` field

`confidence` is the judge's stated confidence in the score it produced.
Distinct from the score:

- A heuristic judge with all signals firing cleanly (no `tool.failed`, no
  `manual_swap` follow-up, `stop_reason=end_turn`, low retry similarity)
  emits high `confidence` even when the heuristic's rubric is coarse — it's
  saying "the signals I have are unambiguous."
- A heuristic judge with conflicting signals (stop_reason clean but a
  `feedback.implicit.type=retry` followed) emits low `confidence` — "I see
  the signal but I'm not sure how to read it."
- An LLM judge emits a `confidence` that reflects its own self-reported
  certainty (rubric-prompted; see [§5.2](#52-llm-as-judge-rubric)).

Confidence is a *gate*, not a score modifier. Consumers (pattern store,
analytics) filter by confidence threshold before aggregating. The score
itself is not down-weighted by confidence — that would conflate two distinct
signals.

**Routing-side gate.** The pattern store **ignores** verdicts with
`confidence < pattern.min_eval_confidence` (default `0.5`, configured per
the planned [`pattern-store.md`](pattern-store.md) — see §15). Low-confidence
verdicts still record (for the agreement-rate view) but don't drive routing.

### 4.4 The `signals` dict

Free-form, judge-specific evidence. Stable conventions:

- Keys are snake_case strings.
- Values are JSON-roundtrippable (no Decimal, no objects). Numeric weights
  belong in the rubric, not in signals.
- Required-by-convention keys per judge_kind:
  - **Heuristic:** `flags: list[str]` — the heuristic flags that fired
    (e.g. `["stop_reason_clean", "no_tool_failure"]`). `flags_negative:
    list[str]` for flags that fired *against* the subject.
  - **LLM:** `rationale_hash: str` (SHA-256 of the judge's natural-language
    rationale), `rationale_redacted: str | None` (populated only on opt-in,
    similar to `turn.started.user_message_text_redacted` per
    [`event-bus-and-trace-catalog.md §4.4.1`](event-bus-and-trace-catalog.md)).
  - **Hybrid:** `heuristic_score: float`, `heuristic_confidence: float`,
    `escalated: bool`, plus the LLM keys if `escalated`.

`signals` is opaque to the score; it exists for the audit trail and for
re-evaluation (the next time this subject is judged, the new judge can see
what the prior signals were).

### 4.5 The `judge_cost_usd` field

`Decimal`, computed via the existing `PriceTable.compute_cost`
([pricing/table.py](../../packages/metis-core/src/metis_core/pricing/table.py)),
stamped with `judge_pricing_version`. Same convention as
[`canonical-message-format.md §6.4`](canonical-message-format.md): aggregate
in `Decimal`, serialize as JSON number with 6 decimal places at the wire
boundary per [`analytics-api.md §5.1`](analytics-api.md).

For heuristic judges, `judge_cost_usd` is exactly `Decimal("0")` and
`judge_pricing_version` is `None`. This is deliberate — pricing semantics
don't apply to code that did no inference.

### 4.6 Re-evaluation

A subject may have *many* verdicts over time. Re-running the evaluator on
a past `turn_id` produces a new `EvalVerdict` with a fresh `eval_id`; the
old verdict is **not** replaced or invalidated. The verdict table is
append-only by construction (it's the trace store) and the analytics
projection queries it like any other event ([§9](#9-analytics-surface)).

The `(subject_kind, subject_id, eval_id)` triple is the natural sort key.
"Latest verdict" is `ORDER BY eval_id DESC LIMIT 1` per subject.
"Agreement rate" joins distinct verdicts across runs ([§9.2](#92-analytics-quality)).

This is why the verdict is on the bus, not a column on `turn.completed`:
the latter would make re-evaluation a destructive operation.

---

## 5. The judge

### 5.1 Turn heuristic rubric

`rubric_id = "turn-heuristic-v1"`. Cost: $0. Latency: <1ms.

Inputs (all derived from events already in the trace store for `turn_id`):

| Signal name              | Source                                                                | Direction |
|--------------------------|-----------------------------------------------------------------------|-----------|
| `stop_reason_clean`      | `turn.completed.stop_reason == "end_turn"`                            | positive  |
| `no_llm_failure`         | No `llm.call_failed` in turn                                          | positive  |
| `no_tool_failure`        | No `tool.failed` in turn (uncaught Python exception path)             | positive  |
| `no_tool_exit_failure`   | No `tool.completed` with `success=False` in turn (clean-exit-nonzero path; e.g. shell-tool nonzero return code) | positive (strong; single failure must drop a clean turn's score by ≥0.3) |
| `no_max_tokens_hit`      | No `llm.call_completed.stop_reason == "max_tokens"` in turn           | positive  |
| `tool_cycle_count_reasonable` | `turn.completed.tool_call_count` ≤ a configured threshold (default 20) | positive  |
| `assistant_refusal_detected` | `signals_extra.final_response_text` begins with a refusal phrase (e.g. "I cannot help", "I'm unable to") within the first 160 chars | negative (×0.5) |
| `empty_assistant_response` | `signals_extra.final_response_text` is whitespace-only | negative (×0.4) |
| `no_retry_implicit`      | No `feedback.implicit.type == "retry"` whose `subject_turn_id == turn_id` within next 5 user messages | positive |
| `no_manual_swap_after`   | No `feedback.implicit.type == "manual_swap"` whose `subject_turn_id == turn_id` | positive |
| `no_edit_followup`       | No `feedback.implicit.type == "edit_followup"` whose `subject_turn_id == turn_id` | positive |
| `explicit_thumbs_up`     | A `feedback.explicit.rating == "thumbs_up"` with `subject_turn_id == turn_id` | positive (heavy weight) |
| `explicit_thumbs_down`   | A `feedback.explicit.rating == "thumbs_down"` with `subject_turn_id == turn_id` | negative (heavy weight) |

The score is the rubric-weighted sum of fired signals normalized to
`[0, 1]`. Concrete v1 weights are an implementation detail of the rubric
file (`rubrics/turn-heuristic-v1.yaml`, not specified here); the *contract*
is that the score is bounded and that explicit feedback dominates implicit
signals dominates lifecycle signals.

**Two distinct tool-failure signals.** v1 distinguishes `tool.failed` (an
uncaught Python exception raised inside a `Tool.execute` body — the
dispatcher catches it and emits `tool.failed`) from `tool.completed` with
`success=False` (the tool ran cleanly to completion but returned a
non-success outcome — the canonical case is the shell tool reporting a
non-zero exit code). Both are real failures from the agent's perspective;
the rubric reads them as two independent gates so a shell tool that
prints `"FAIL N/M"` and exits 1 (`success=False`, no exception) lowers
the score by the same shape as an uncaught exception would. The
`no_tool_exit_failure` weight is sized so that a single failed exit
drops a clean turn's score by ≥0.3 and the resulting confidence below
the v1 hybrid escalation threshold (0.7, see
[§5.3](#53-hybrid-escalation)), so `HybridJudge` escalates to the LLM
judge on this class of failure without depending on assistant-text
content signals.

**Content penalty (opt-in).** `assistant_refusal_detected` and
`empty_assistant_response` apply as multiplicative penalties on the
normalized score (×0.5 and ×0.4 respectively), not as weighted lifecycle
signals. They fire only when the caller plumbs `final_response_text` via
`SubjectContext.signals_extra`. The refusal regex is anchored to the
first 160 chars of the stripped response so substantive answers that
incidentally quote a refusal phrase don't false-positive.

**`signals_extra` contract.** The session manager's
`_emit_turn_completed` stamps three text keys onto
`turn.completed.signals_extra` when the underlying string is non-empty
(any missing string is omitted so the judge's "(not available)"
fallback fires honestly):

| Key                        | Source                                             | Reader                                              |
|----------------------------|----------------------------------------------------|-----------------------------------------------------|
| `final_response_text`      | last assistant text block in the turn              | heuristic content-penalty path (this section)       |
| `assistant_response_text`  | alias of `final_response_text`                     | LLM judge `_build_user_message` (see [§5.2](#52-llm-as-judge-rubric)) |
| `user_prompt_text`         | first text block of the persisted user message     | LLM judge `_build_user_message` (see [§5.2](#52-llm-as-judge-rubric)) |

The two assistant-text keys are intentionally aliased to the same
string so producer and consumer evolved independently — the heuristic
content-penalty path was wired before the LLM judge tier landed and
reads the older name; the LLM judge ships with the newer one. A future
migration can drop the alias once the consumer side converges. The
benchmark workload harness (see [§5.4](#54-workload-rubric)) also
populates these keys at the workload subject level, so workload-level
evaluation exercises the same readers.

**Confidence** is high when ≥ N signals fire in the same direction with no
conflict; low when signals contradict (e.g. clean stop reason but implicit
retry detected later). Concrete threshold lives in the rubric file.

**Lookahead window.** Some signals (`no_retry_implicit`,
`no_manual_swap_after`, `no_edit_followup`) require seeing user messages
*after* the turn being judged. The heuristic judge waits until either (a)
5 user messages follow in the same session, (b) the session ends, or (c) 24
hours pass without progress, then commits. v1 ships the heuristic with the
lookahead window configurable per workspace; the trade-off is verdict
latency vs verdict richness, and the dashboard's "pending" tile makes the
backlog visible.

### 5.2 LLM-as-judge rubric

`rubric_id = "turn-llm-v1"`. Cost: typically $0.001–$0.01 per turn,
depending on judge model and turn size. Latency: 500–3000ms.

The LLM judge ingests:

- The user's prompt text and assistant's final response text for the turn
  (from the canonical `messages` table per
  [`canonical-message-format.md §9.1`](canonical-message-format.md)).
- The list of `tool.called` / `tool.completed` events with their hashes
  and side-effect classifications.
- The turn's `route.decided.chosen_model`.

It prompts a small model (default `anthropic:claude-haiku-4-5`,
configurable; the same model the routing pipeline considers cheap) with a
fixed rubric prompt asking for:

1. A success score in `[0, 1]` with one decimal.
2. A self-reported confidence in `[0, 1]` with one decimal.
3. A one-sentence rationale.

The judge response is parsed against a `msgspec` schema; parse failures emit
`eval.failed.failure_mode="judge_output_invalid"` and the verdict is not
written. Retries are bounded (default 1 retry on parse failure) — beyond
that, the heuristic verdict ([§5.1](#51-turn-heuristic-rubric)) stands as
the only record for the subject.

The rubric prompt is shipped as `rubrics/turn-llm-v1.md` and versioned with
the spec — changing the prompt is a `rubric_version` bump that produces a
new score series on the dashboard.

**Why a small model for the judge.** The judge's job is "did this look like
a successful turn" — a classification, not synthesis. Spending opus to
grade haiku's work would invert the cost story. The configuration must
allow a bigger judge (a buyer running benchmarks may opt in), but the
default is cheap.

### 5.3 Hybrid escalation

`rubric_id = "turn-hybrid-v1"`. Default judge for v1 turn evaluations.

Algorithm:

1. Run the heuristic rubric. Get `(h_score, h_confidence)`.
2. If `h_confidence >= hybrid.escalation_threshold` (default `0.7`),
   emit the heuristic verdict and stop. Cost: $0.
3. Otherwise, run the LLM judge. Emit a verdict with
   `judge_kind="hybrid"`, `score=l_score`, `confidence=l_confidence`,
   `signals.heuristic_score=h_score`,
   `signals.heuristic_confidence=h_confidence`,
   `signals.escalated=true`.

The threshold is configurable per workspace. The session- and workload-
level rubrics follow the same pattern; tool-cycle is heuristic-only in v1
(the LLM judge there would cost more than the action it's grading).

This is the cost-vs-truth knob. `escalation_threshold = 0` is "always run
the LLM judge" (maximum cost, maximum signal); `escalation_threshold = 1` is
"never run the LLM judge" (zero cost, heuristic-only). The default lands in
between, with the dashboard's agreement-rate view ([§9.2](#92-analytics-quality))
as the calibration surface.

**Implementation status (2026-05-14).** LLM tier landed at
[`packages/metis-core/src/metis_core/eval/llm_judge.py`](../../packages/metis-core/src/metis_core/eval/llm_judge.py).
Hybrid escalation knob default `0.7` is configurable via
`HybridJudge(..., escalation_threshold=...)`. Budget-exhausted LLM calls
return a `signals.budget_exhausted=True` verdict (confidence=0); HybridJudge
falls back to its heuristic verdict and records
`signals.escalation_skipped="budget_exhausted"`. The LLM judge also delegates
to the heuristic for tool_cycle / session subjects so the v1 heuristic-only
commitment for those kinds holds even when an LLM judge is wired in.

### 5.4 Workload rubric

For benchmark workloads ([`benchmark.md §3`](benchmark.md)), the rubric is
authored per-workload in the existing `workload.yaml` schema as a new
optional `evaluate:` block:

```yaml
name: fix-a-bug-small
...
evaluate:
  rubric: heuristic        # heuristic | llm | hybrid; default heuristic
  expect_substring_in_final_response: "..."   # passthrough to heuristic signals
  llm_judge_model: anthropic:claude-haiku-4-5  # only when rubric != heuristic
  weight_per_turn: 1.0                           # how turns in the workload aggregate
  grounding_tokens: ["RoutingEngine", "policy=", "PolicyEvaluation"]   # v1.1
  forbidden_grounding: ["PATTERN_LOOKUP", "RouterChain", "ModelSelector"] # v1.1
  partial_credit:                                    # v1.2
    enabled: true
    criterion: test_pass_count_ratio
    map: linear
```

The benchmark harness ([`benchmark.md §9`](benchmark.md)) calls the
evaluator with `subject_kind=workload` after the suite run. The resulting
`EvalVerdict` is the workload's quality score; the benchmark's `savings_pct`
multiplied (or filtered) by the workload's `score` becomes the headline
"saved X% **on successful work**" number a buyer can quote without
qualification.

The benchmark v1's `expect.contains_substring` ([`benchmark.md §3.1`](benchmark.md))
is the special-case heuristic for the workload rubric: a present substring
contributes positively to the score; an absent one negatively. New workload
rubric primitives are added as `evaluate.*` fields, not as new schema
versions.

The workload rubric also applies the same content penalty as the turn
rubric — `workload_assistant_refusal_detected` (×0.5) and
`workload_empty_assistant_response` (×0.4) — using the harness-supplied
`final_response_text`. Without this, a workload whose `evaluate:` block
has no `expect_substring_in_final_response` would score 1.0 on a clean
refusal (lifecycle is fine; substring isn't asserted). The
`intentionally-failing-task` workload under `benchmarks/workloads/` is
the control case that exercises this — it scores < 0.8 when the agent
refuses or returns nothing.

#### Grounding-check primitive (v1.1)

`grounding_tokens` and `forbidden_grounding` are the rubric inputs for
workloads that probe **hallucination / source-grounding** rather than
task completion. The motivating case is documented in
[`benchmarks/RESULTS.md §A3-rev`](../../benchmarks/RESULTS.md): the
`architectural-explanation-without-hallucination` workload used a single
`expect_substring_in_final_response="PATTERN_RECOMMENDATION"` assertion;
sonnet's response cited the real `PolicyEvaluation` / `RoutingDecision`
dataclasses and lowercase `policy=` literals — strictly more grounded than
haiku — but scored 0.50 because it didn't parrot the UPPERCASE
`PATTERN_RECOMMENDATION` label from the engine.py module docstring. The
substring check rewarded **stylistic mimicry** over **real grounding**.

Semantics:

- `grounding_tokens`: a list of substrings that **should** appear in the
  final response. Each one is a real symbol the agent must cite to count
  as grounded — class names, function names, real string-literal values
  the source uses. The heuristic awards `present / total` as a positive
  score component.
- `forbidden_grounding`: a list of substrings that **should not** appear.
  Each one is a plausible-but-fabricated name a hallucinating agent would
  invent. The heuristic awards `1 - (present / total)` as a positive
  score component (i.e. it pays for *absence*).
- The two lists are independent. A workload may set either, both, or
  neither. When both are set, the heuristic averages the two components.

The rubric exposes a workload-level signal `workload_grounding_score`
(plus `grounding_tokens_present`, `grounding_tokens_missing`,
`forbidden_grounding_present` for the audit trail). The composed
workload score averages this with the substring/assertion-derived score
when grounding is configured — so a workload that fully grounds in real
symbols and avoids fabricated ones is unaffected, and one that misses
all expected symbols and contains forbidden ones is halved.

LLM tier escalation: when `rubric: llm` or `rubric: hybrid` is set, the
configured `grounding_tokens` and `forbidden_grounding` lists are
surfaced to the judge LLM in the user message (under a "GROUNDING HINTS"
section). The LLM tier can recognize *paraphrased* grounding (citing a
real symbol with different capitalization or via a synonym) and *partial*
fabrications (a real prefix joined to a fake suffix) that the heuristic
substring match would miss. The LLM judge's score remains a single
[0, 1] number; the grounding hints are inputs, not a separate axis.

Cost discipline: heuristic-tier grounding is $0; LLM-tier grounding
escalation is one judge call per workload, governed by the same
`BudgetTracker` caps as the per-turn LLM judge.

#### Partial-credit primitive (v1.2)

`partial_credit` is the rubric input for workloads where the agent's final
response carries a **count** (test pass/fail tallies, sub-task scoreboards)
rather than a single boolean substring. The motivating case is documented
in [`benchmarks/RESULTS.md §A3-rev6 / §13a-1`](../../benchmarks/RESULTS.md):
across six A3 iterations the per-workload haiku-vs-sonnet quality gap on
the v1 suite is below the heuristic judge's resolution. Pass/fail substring
detection collapses partial successes — 12/16 regex cases, 3/4 pytest
tests — to 0; partial-credit surfaces the mid-range gradient haiku and
sonnet actually produce.

Schema:

```yaml
evaluate:
  rubric: heuristic
  partial_credit:
    enabled: true
    criterion: test_pass_count_ratio   # only criterion in v1
    map: linear                        # "linear" | "stepped"
```

Semantics:

- `enabled: false` (default): partial-credit is off; the workload falls
  back to the pre-v1.2 substring path.
- `enabled: true`: the heuristic parses `final_response_text` for the
  configured criterion, applies `map`, and folds the resulting score into
  the composed workload score **in place of** the pass/fail substring
  assertion. `expect_substring_in_final_response` is bypassed when
  partial-credit is active — pick one or the other.
- `criterion: test_pass_count_ratio`: the parser recognizes two shapes,
  preferring whichever produces an explicit total:
    1. `PASS N/M` / `FAIL N/M` runner output (per the `runner.py`
       convention used in this repo's workloads). The last occurrence in
       the text wins, so iterative per-case lines followed by the final
       summary line are graded correctly.
    2. Pytest summary tokens: `N passed`, `M failed`, `K error(s)`. Total
       is `passed + failed + errors`; skipped tests are excluded from
       the denominator (a skip isn't a pass or a fail).
   The ratio is `passed / total`. When the response contains no parseable
   test signal (e.g. the agent never reached the test step), the ratio is
   `0.0` and the `partial_credit_no_test_signal` negative flag fires — a
   missing signal is treated as a failure.
- `map: linear` (default): pass the ratio through unchanged. Mid-ratios
  produce mid-scores; perfect-pass produces 1.0 (recovering the same
  composed score as the prior `substring_present=True` path); zero-pass
  produces 0.0 (matching the prior `substring_present=False` halving).
- `map: stepped`: round the ratio to the nearest 0.25 (so the only
  possible mapped scores are 0.0, 0.25, 0.5, 0.75, 1.0). Useful when the
  caller wants a stable bucketed score rather than the continuous version.
  Endpoints (0/N and N/N) stay exact.

Composition: when partial-credit is active, the composed score is
`(base + partial_credit_score) / 2.0`, parallel to how the grounding
score folds in. The rubric exposes workload-level signals
`partial_credit_score`, `partial_credit_ratio`, `partial_credit_passed`,
`partial_credit_total`, `partial_credit_criterion`, `partial_credit_map`,
and `partial_credit_test_signal_found` for the audit trail. Pre-v1.2
workloads (no `partial_credit` block) are unaffected.

Cost discipline: heuristic-tier partial-credit is $0 (pure regex over
the response text). The LLM tier does not consult partial-credit — it
forms its own [0, 1] judgment.

### 5.5 Tool-cycle rubric

`rubric_id = "tool-cycle-heuristic-v1"`. Heuristic only in v1.

Signals:

- `tool_succeeded`: `tool.completed` with `success=true`.
- `output_size_in_window`: `tool.completed.output_size_bytes` within a
  per-tool reasonable range (the range is rubric-internal).
- `no_immediate_re_call_same_input`: the next `tool.called` in the turn
  doesn't have the same `input_hash` for the same `tool_name`.
- `no_thrash_in_window`: within the next 3 tool calls, the same
  `tool_name` is not called with `input_hash` differing by a small
  Hamming-style threshold (catches "re-call with one arg tweaked").

Tool-cycle verdicts attach to their parent turn via `parent_eval_id`. They
do not arithmetic-average into the turn score; the turn rubric reads
`signals.tool_cycles_with_score_below_threshold` and applies its own weight.

### 5.6 Session rubric

`rubric_id = "session-aggregate-v1"`. Heuristic-only (LLM at session scale
is expensive and the per-turn signal is already in the bus).

Algorithm:

1. Aggregate child turn verdicts: `mean_turn_score`,
   `min_turn_score`, `turns_with_explicit_thumbs_down`.
2. Add session-scoped signals: `feedback.explicit` with `scope="session"`,
   number of distinct models swapped via `/model`, session ended with
   `disposition="abandoned"`.
3. Emit a session-level verdict with `signals` carrying the child
   `eval_id`s.

---

## 6. When the evaluator runs

### 6.1 Bus subscriber (online)

The evaluator registers a **non-fast-path** subscriber per
[`event-bus-and-trace-catalog.md §3.4`](event-bus-and-trace-catalog.md):

| Filter event       | Action                                                                                |
|--------------------|---------------------------------------------------------------------------------------|
| `turn.completed`   | Queue a `turn`-subject evaluation (delayed until the lookahead window resolves; §5.1) |
| `tool.completed`   | Queue a `tool_cycle`-subject evaluation                                               |
| `tool.failed`      | Queue a `tool_cycle`-subject evaluation                                               |
| `session.ended`    | Queue a `session`-subject evaluation                                                  |
| `feedback.explicit` | Re-queue the matching turn / session for re-evaluation (the verdict gains a thumb signal) |

Non-fast-path is the right choice: the heuristic judge is fast (~ms), the
LLM judge is slow (seconds), and neither is on a critical user-facing path.
A backlog is observable as `eval.started - eval.completed` over time on
`/analytics/quality.backlog`.

### 6.2 Batch re-evaluation (offline)

The dashboard's agreement-rate view ([§9.2](#92-analytics-quality))
depends on re-evaluating subjects on demand. The evaluator exposes a
`re_evaluate(window, subject_kind, rubric_id)` entry point (CLI subcommand
in v1: `metis evaluate --since <ts> --subject turn --rubric turn-hybrid-v1`)
that runs the judge across the trace store window and emits fresh
`eval.completed` events.

Re-evaluation is **bounded** by the same per-day and per-session
`judge_cost_usd` caps ([§7](#7-budget-and-safety)) as online evaluation —
the cap is across both modes.

### 6.3 No mid-turn evaluation

Per [§2.2.2](#22-non-goals). The evaluator never reads in-flight state.
This is what keeps it off the turn-locked-model path
([`AGENTS.md` "Gotchas"](../../AGENTS.md)) and out of the routing chain.

---

## 7. Budget and safety

The LLM judge spends real money. Three caps apply:

| Cap                          | Default                       | Configurable | Effect                                              |
|------------------------------|-------------------------------|--------------|------------------------------------------------------|
| `eval.per_session_max_usd`   | `Decimal("0.10")`             | yes          | After spend, eval queue drops `judge_kind in (llm, hybrid)` for the session — heuristic still runs. |
| `eval.per_day_max_usd`       | `Decimal("1.00")`             | yes          | After spend, eval queue drops `judge_kind in (llm, hybrid)` workspace-wide for the day. |
| `eval.escalation_threshold`  | `0.7`                         | yes          | Hybrid judge escalates to LLM only when heuristic confidence is below this.            |

When a cap throttles a verdict, an `eval.completed` is still emitted with
`judge_kind="heuristic"` (the heuristic always runs) and
`signals.throttled_reason: Literal["session_cap", "daily_cap"]`. No verdict
is silently dropped.

Kill switch: workspace config `eval.disabled = true` skips *both* judges. No
`eval.*` events emitted for that workspace; the bus subscriber unregisters.
Useful for benchmarking the cost story without the evaluator's own cost
confounding it.

**Why both per-session and per-day caps.** A single chatty session can
exhaust the daily budget alone (LLM judge on every escalated turn × tens of
turns × $0.005 ≈ pennies-fast). The per-session cap prevents one session
from starving the rest of the day's evaluation; the per-day cap is the
hard ceiling the operator sees on their bill.

---

## 8. Events

Three new bus catalog events. All payloads are `msgspec.Struct(frozen=True)`
defined in `packages/metis-core/src/metis_core/events/payloads.py` when the
implementation lands (this spec describes the contract only).

### 8.1 `eval.started`

> **Sensitivity:** `pseudonymous`
> **Phase:** 3
> **Actor:** SYSTEM
> **Parent:** `turn.completed` / `tool.completed` / `tool.failed` / `session.ended` / `feedback.explicit`

```python
{
    "eval_id": str,                                   # monotonic ULID
    "subject_kind": Literal["turn", "tool_cycle", "session", "workload"],
    "subject_id": str,
    "rubric_id": str,
    "rubric_version": str,
    "judge_kind_planned": Literal["heuristic", "llm", "hybrid"],
    "trigger": Literal["bus", "batch", "feedback_arrived", "benchmark"],
}
```

### 8.2 `eval.completed`

> **Sensitivity:** `user_controlled` (floor; downgrades to `pseudonymous`
>   per [`event-bus-and-trace-catalog.md §4.4.1`](event-bus-and-trace-catalog.md)
>   when `signals.rationale_redacted` is absent)
> **Phase:** 3
> **Actor:** SYSTEM
> **Parent:** `eval.started`

```python
{
    "eval_id": str,
    "subject_kind": Literal["turn", "tool_cycle", "session", "workload"],
    "subject_id": str,
    "score": float,                                   # in [0.0, 1.0]
    "confidence": float,                              # in [0.0, 1.0]
    "judge_kind": Literal["heuristic", "llm", "hybrid"],
    "judge_model": str | None,
    "judge_cost_usd": str,                            # Decimal serialized as string (canonical: same as Usage.cost_usd)
    "judge_pricing_version": str | None,
    "judge_latency_ms": int,
    "rubric_id": str,
    "rubric_version": str,
    "signals": dict,                                  # see §4.4
    "parent_eval_id": str | None,
}
```

Cost is serialized as a string (mirrors `Usage.cost_usd` in
[`canonical-message-format.md §6.4`](canonical-message-format.md)) so the
JSON envelope round-trips through the trace store without `Decimal` loss.

**Sensitivity floor.** The catalog floor is `user_controlled` — the worst
case, when `signals.rationale_redacted` is populated (the user opted into
capturing LLM judge rationales) and the event carries user-derived text.
When the rationale field is absent (heuristic verdict, or LLM verdict
without rationale opt-in), the subscriber passes `pseudonymous` to
`make_event` — a downgrade toward less private, which the dynamic-sensitivity
rule in [`event-bus-and-trace-catalog.md §4.4.1`](event-bus-and-trace-catalog.md)
allows.

### 8.3 `eval.failed`

> **Sensitivity:** `pseudonymous`
> **Phase:** 3
> **Actor:** SYSTEM
> **Parent:** `eval.started`

```python
{
    "eval_id": str,
    "subject_kind": Literal["turn", "tool_cycle", "session", "workload"],
    "subject_id": str,
    "failure_mode": Literal[
        "judge_output_invalid",       # LLM response didn't parse against the rubric schema
        "judge_call_failed",          # LLM call hit a hard error (provider down, auth, etc.)
        "throttled_no_heuristic",     # caps fired AND heuristic also unavailable (shouldn't happen in v1; defensive)
        "subject_not_found",          # subject_id resolved to no events
        "rubric_invalid",             # rubric file failed to load
    ],
    "error_message": str,
    "judge_latency_ms": int,
}
```

`throttled_no_heuristic` is defensive — v1 always has a heuristic fallback,
so the live path never emits it. Reserved for future configurations where a
rubric is LLM-only.

### 8.4 Sensitivity classifications

Summary of the three new events in the
[`event-bus-and-trace-catalog.md §4.4`](event-bus-and-trace-catalog.md) frame:

| Event             | Floor sensitivity | Downgrade pathway    |
|-------------------|-------------------|----------------------|
| `eval.started`    | `pseudonymous`    | (no opt-in fields)   |
| `eval.completed`  | `user_controlled` | `pseudonymous` when `signals.rationale_redacted` is absent |
| `eval.failed`     | `pseudonymous`    | (no opt-in fields)   |

The `eval` domain joins the closed domain list in
[`event-bus-and-trace-catalog.md §4.5`](event-bus-and-trace-catalog.md).
This is an additive domain (no collision with `feedback`, which describes
*user-supplied* signal; the evaluator describes the *system's* assessment).

**Payload registry.** The three payloads land in `PAYLOAD_REGISTRY`
([`event-bus-and-trace-catalog.md §6`](event-bus-and-trace-catalog.md))
*when the implementation lands*, per the same convention used for the
gateway's pending payload-field additions
([`CHANGES.md` 2026-05-13 gateway entry](CHANGES.md)). The catalog spec
gains §6.11 (the `eval` domain) at implementation time.

---

## 9. Analytics surface

The evaluator's data is consumable from `/analytics/*` per
[`analytics-api.md §2.1`](analytics-api.md) ("read-only and derived"). One
new endpoint, plus an additive field on the existing cost view.

### 9.1 `GET /analytics/cost` — additive `include_eval` parameter

```
GET /analytics/cost?group_by=model&include_eval=false   # default: only llm.call_completed rows
GET /analytics/cost?group_by=model&include_eval=true    # add eval.completed.judge_cost_usd to model totals
```

Default `false` keeps the savings story honest (eval spend is metis's
overhead, not the buyer's agent workload). The dashboard's overhead tile
sets `include_eval=true` and renders the eval cost as a separate column.
SPA-side: subtract for "agent-only spend," include for "total Metis spend."

### 9.2 `GET /analytics/quality`

New endpoint. Aggregates `eval.completed` events over a time window.

**Query parameters:**

| Parameter      | Type                                                | Required | Default |
|----------------|-----------------------------------------------------|----------|---------|
| `from`,`to`    | ISO 8601 UTC                                        | no       | last 7d |
| `subject_kind` | `turn` \| `tool_cycle` \| `session` \| `workload`  | no       | `turn`  |
| `group_by`     | `model` \| `judge_kind` \| `rubric_id` \| `none`   | no       | `model` |
| `min_confidence` | float                                              | no       | `0.0`   |

**Response shape (`group_by=model`):**

```json
{
  "window": {...},
  "current_pricing_version": "...",
  "data": [
    {
      "chosen_model": "anthropic:claude-haiku-4-5",
      "verdict_count": 142,
      "mean_score": 0.82,
      "p50_score": 0.85,
      "p10_score": 0.50,
      "mean_confidence": 0.71,
      "judge_cost_usd_total": 0.0823,
      "thumbs_down_count": 3
    }
  ]
}
```

`chosen_model` is joined from the `route.decided` event of the subject
turn — the model whose work is being judged, not the judge's model. The
join walks `subject_id (turn_id) → route.decided.chosen_model`; per
[`analytics-api.md §4.3`](analytics-api.md) the routing event is one
per turn so this is a 1:1.

**Agreement rate tile (computed from the same source).** When two distinct
verdicts exist for the same `subject_id` (one online, one from a batch
re-eval; or two with different `rubric_id`s), the dashboard computes
"verdict agreement" as the fraction whose `|score_a - score_b| <=
agreement_window` (default 0.15, configurable client-side). This is a SPA-
side computation over the verdict rows; the API surface is *just* the
`eval.completed` event projection. No new endpoint needed.

**Backlog tile.** `verdict_count` versus the count of `eval.started`
without a matching `eval.completed` / `eval.failed` in the window. SPA
queries `/analytics/quality?subject_kind=turn` and counts open `eval_id`s
client-side.

### 9.3 Drill-down: turn analytics gains an `evaluations` field

[`analytics-api.md §4.6`](analytics-api.md) (`GET /analytics/turns/{id}`)
gets an additive `evaluations` array in `data`. Each entry is the
`EvalVerdict` shape from [§4.1](#41-shape). No breaking change; existing
consumers ignoring the field continue to work.

This is the only place a `signals.rationale_redacted` value can surface to
the UI (under the opt-in sensitivity uplift in [§8.4](#84-sensitivity-classifications)).

### 9.4 Negative space

Not in v1:

- `/analytics/quality_trend` (time series of mean score by week). Cheap
  to add later via `group_by=day`; not worth the SPA tile yet.
- Cross-workload quality dashboards (the benchmark harness emits its own
  workload-level verdict; the SPA can fetch via `subject_kind=workload`).
- Per-rubric comparison views. Add when there are multiple rubrics
  shipping (v1 ships exactly one per subject_kind).

---

## 10. Storage

Verdicts are bus events, persisted in the existing trace store
([`canonical-message-format.md §9.1`](canonical-message-format.md),
`events` table). No new table.

Indexes:

- The existing `(type, timestamp_us)` index covers the time-windowed
  projection in `/analytics/quality`.
- A new index `idx_events_eval_subject` on
  `(json_extract(payload_json, '$.subject_kind'),
    json_extract(payload_json, '$.subject_id'), id)`
  is *optional* — at single-user scale (≤10K verdicts) the full scan over
  `type='eval.completed'` rows is fast enough. The index lands as a Phase
  3 follow-up if the agreement-rate query measurably slows the dashboard.

Following [`analytics-api.md §2.1`](analytics-api.md): the source of truth
is the bus + trace store; analytics is a projection. No rollup, no
materialized verdict table.

---

## 11. The feedback loop

The verdict feeds *future* turn routing through the pattern store. Three
consumers:

### 11.1 Pattern store (slot 4 of the routing chain)

The pattern store ([planned spec: pattern-store.md](pattern-store.md); see
§15 for coordination) reads `eval.completed.score` as the `success_score`
in `outcome.primary_model` clusters. Per
[`routing-engine.md §5.5`](routing-engine.md):

```
normalized_success_M = mean(success_score) for neighbors with primary_model = M
```

The pattern store's K-nearest aggregation reads from `eval.completed`
events directly (not from a separate outcome table). Verdicts with
`confidence < pattern.min_eval_confidence` (default `0.5`) are excluded
from the aggregation but stay in the trace store (they're still useful for
the agreement-rate view).

**Which verdict wins when there are multiple.** The pattern store reads
the *latest* `eval.completed` per `(subject_kind, subject_id)` —
`MAX(eval_id)` per subject. Re-evaluation, by construction, supersedes older
verdicts for routing purposes.

### 11.2 Analytics-driven escalation (Phase 3+, surface only)

When a model accumulates `mean_score < quality_floor` (default `0.6`) over
a configurable window on `/analytics/quality?group_by=model`, the dashboard
surfaces it ("This task type is underperforming on haiku — consider
escalating"). The surface is a *banner*, not an automatic rule change.
Auto-escalation belongs to the pattern store, not the analytics view.

### 11.3 Benchmark headline

The benchmark harness ([`benchmark.md §8`](benchmark.md)) gains a quality
column in its report: each workload's `score`, the suite-level mean, and
the gating note "savings on successful work: $X of $Y total." This is what
turns "saved 67%" into "saved 67% on work the evaluator judged as
successful at confidence ≥ 0.5." The exact format lands in a follow-up
benchmark.md amendment when the evaluator implementation does.

---

## 12. Invariants

1. **Append-only.** `eval.completed` events are never updated or deleted.
   Re-evaluation produces new events.
2. **One score field.** The `score` is one number in `[0, 1]`. Multi-
   dimensional rubrics collapse into `signals`, never into the score field.
3. **Heuristic always available.** Every subject for which the heuristic
   has all required input events produces a verdict. Throttling never
   silently drops; it downgrades the planned `judge_kind`.
4. **Eval cost is recorded.** `judge_cost_usd` is always set (zero for
   heuristic; positive for LLM / hybrid that escalated). Pricing version is
   stamped when cost > 0.
5. **No routing-chain involvement.** The evaluator is not a chain slot. It
   does not appear in `route.decided.chain`. It feeds the pattern store
   (which *is* a slot) via the bus, not via direct call.
6. **No mid-turn execution.** Subscribers fire on terminal events
   (`turn.completed`, etc.), never inside a turn.
7. **Rubric is versioned.** Every verdict carries `rubric_id` and
   `rubric_version`. Changing the rubric is a version bump, not a silent
   recalibration.
8. **Confidence gates routing, not display.** The dashboard shows all
   verdicts; the pattern store filters by confidence threshold.

---

## 13. Open questions

These are **live**. Do not unilaterally close them.

1. **Hybrid escalation threshold default.** `0.7` is a guess. The right
   value is whatever makes the agreement-rate view show heuristic and LLM
   agreeing ≥ 95% of the time at that threshold — observable only after
   the dashboard ships and data accumulates.
2. **Should explicit `feedback.explicit` overwrite a prior verdict's
   score?** Today a thumbs-down arriving after a verdict triggers a
   re-evaluation that produces a new verdict (the old verdict stays
   recorded). Alternative: the new feedback updates the *latest* verdict's
   `signals` without producing a new verdict. The current shape is cleaner
   for the agreement-rate view; the alternative is cheaper in event count.
3. **LLM judge prompt-cache discipline.** Per
   [`context-assembler.md`](context-assembler.md), the LLM judge's rubric
   prompt is stable and a perfect cache candidate. The implementation
   should mark it for caching; the spec doesn't yet pin the cache contract
   for the judge's request shape.
4. **Workload-rubric primitives.** v1 leans on `expect_substring_in_final_response`
   plus the LLM judge. The benchmark workloads may want richer
   primitives (assert tests pass, assert a file's contents match a
   pattern). Wait for the v1 suite to settle before adding noise.
5. **Tool-cycle LLM judging.** v1 ships heuristic-only at the tool-cycle
   level. The thrash detector catches the obvious case; subtle cases
   (tool succeeded but the answer was wrong) require the LLM judge. The
   cost calculus is unfavorable at v1 scale — revisit when there's
   evidence of missed signal.
6. **Cross-rubric agreement as a calibration signal.** Two heuristic
   rubrics scoring the same subjects (e.g. "lenient" and "strict")
   produce an agreement series independent of the LLM. This is a v2
   tool for rubric maintenance; not on the v1 surface.
7. **Per-user calibration.** A future signal: "user X consistently
   thumbs-down turns the heuristic scored ≥ 0.8." The eval rubric could
   learn a per-user offset. Defer until multi-user / multi-profile
   lands per [`STRATEGY.md §2`](../STRATEGY.md).
8. **Evaluating the evaluator.** Spot-checking via benchmark workloads
   with hand-asserted expectations is the v1 calibration plan. A more
   rigorous "golden verdict" corpus (a sampled set of turns the operator
   manually scores) is plausible but expensive — and would itself need a
   labeling UI. Open until evidence shows the v1 plan is insufficient.
9. **Cost of re-evaluation at scale.** Batch re-evaluation under the
   per-day cap is bounded, but the cap could throttle an honest
   investigation. Caps are workspace config; a one-off `metis evaluate
   --override-cap` may be needed. Wait for actual friction.

---

## 14. Testing strategy

V1's tests cover the contract, not the rubric weights (those live in the
rubric files and may evolve without the spec needing to change).

### 14.1 Required tests

1. **Heuristic verdict shape.** Seed a clean `turn.completed` + supporting
   events; assert `eval.completed` carries `judge_kind="heuristic"`,
   `judge_cost_usd == Decimal("0")`, `judge_pricing_version is None`,
   and `score` is in `[0, 1]`.
2. **Hybrid escalation threshold.** A turn whose heuristic produces
   `confidence < escalation_threshold` triggers the LLM judge (mocked via
   the scripted adapter, per `conftest.py` / `tests_shared/`); the verdict
   carries `judge_kind="hybrid"`, `signals.escalated=true`, and
   `judge_cost_usd > 0`.
3. **Hybrid no-escalation.** A turn whose heuristic produces
   `confidence >= escalation_threshold` produces a verdict with
   `judge_kind="heuristic"`, no LLM call, `judge_cost_usd == 0`.
4. **Per-session cap downgrade.** A configured `eval.per_session_max_usd
   = Decimal("0")` causes a `judge_kind="hybrid"` plan to emit a
   heuristic verdict with `signals.throttled_reason="session_cap"`.
5. **Per-day cap downgrade.** Same shape, `signals.throttled_reason
   ="daily_cap"`. Caps are independent.
6. **Re-evaluation produces a new event.** Re-running the evaluator on a
   prior `turn_id` produces a second `eval.completed` with a fresh
   `eval_id`; both are queryable; pattern-store-style "latest" query
   returns the newer.
7. **Confidence gate filters analytics aggregation.** Verdicts below
   `min_confidence` are excluded from `mean_score` on `/analytics/quality`
   but still present in `verdict_count` (or vice versa, per the response
   contract; pin this in implementation tests).
8. **Subject not found emits `eval.failed`.** Triggering an eval against a
   nonexistent `turn_id` produces `eval.failed.failure_mode
   ="subject_not_found"` and no `eval.completed`.
9. **Invalid LLM judge response.** A scripted LLM judge whose response
   fails the rubric schema produces `eval.failed.failure_mode
   ="judge_output_invalid"` after the bounded retry; no `eval.completed`.
10. **Rubric versioning.** Two verdicts on the same subject with different
    `rubric_version` strings both persist; `/analytics/quality?group_by
    =rubric_id` shows two rows.
11. **Tool-cycle attaches to parent turn.** A `tool.completed` triggers a
    `tool_cycle`-subject verdict whose `parent_eval_id` references the
    turn's eval (when the turn has been evaluated; null otherwise).
12. **Session aggregation reads child turn verdicts.** A
    `session.ended` with three child turn verdicts produces a
    session-subject verdict whose `signals.child_eval_ids` lists all three.
13. **Sensitivity uplift.** Setting `signals.rationale_redacted` produces
    a recorded event whose `sensitivity == "user_controlled"`; omitting
    the field keeps it at `"pseudonymous"`.
14. **Eval cost folds into `/analytics/cost?include_eval=true`.** Sum of
    `eval.completed.judge_cost_usd` matches the delta between the two
    endpoint calls.
15. **No mid-turn fire.** The subscriber filter doesn't match
    `llm.call_completed` events; verifying via subscription registration
    introspection.

### 14.2 Property tests

- **Score boundedness.** All emitted `eval.completed.score` are in
  `[0.0, 1.0]`.
- **Heuristic determinism.** Same input events → same heuristic verdict
  (`score`, `confidence`, `signals.flags`) byte-equal. LLM judges are
  excluded (provider variance).
- **Cost monotonicity in re-evaluation.** Re-evaluating a turn under a
  more-expensive judge never reduces total eval spend recorded.

---

## 15. Coordinates with `pattern-store.md`

> *Authored 2026-05-13 in parallel with Agent 3A's draft of
> `pattern-store.md`. Reconciled 2026-05-14 — see CHANGES.md
> "Pattern-store ↔ evaluator reconciliation sweep." The table below
> reflects the reconciled contract; the open coordination items
> originally listed have been closed (see §15.1).*

The pattern-store spec ([`pattern-store.md §15`](pattern-store.md))
imports the verdict shape and consumption semantics from this spec
verbatim. This section lists the load-bearing touchpoints and their
reconciled status.

| Touchpoint                                               | Reconciled outcome (2026-05-14) | Where it lives in this spec |
|----------------------------------------------------------|---------------------------------|----------------------------|
| Verdict shape (`EvalVerdict`) ownership                  | Evaluator owns it; pattern store consumes verbatim and does **not** re-specify. | [§4.1](#41-shape) |
| Score timing (sync vs async)                             | **Async.** Pattern-store writes outcome immediately on `session.ended` with `success_score=None`; the `eval.completed` subscriber later calls `PatternStore.update_score(turn_id, ...)`. Join key: `turn_id`. | [§6.1](#61-bus-subscriber-online), pattern-store §10.4, §15.3 |
| Confidence-gate filter home + default                    | Lives in **pattern-store config** (`routing.yaml::pattern.min_eval_confidence`); default `0.5`. Evaluator emits all verdicts; pattern store filters at K-cluster aggregation time. | [§4.3](#43-the-confidence-field), pattern-store §15.4 |
| Sample-size weighting in K-cluster aggregation           | Pinned in [`routing-engine.md §5.5`](routing-engine.md) (2026-05-14 clarification): `Σ(success_score_i × sample_size_i) / Σ(sample_size_i)`. | routing-engine §5.5 |
| Latest-verdict rule when multiple verdicts exist         | `MAX(eval_id)` per `(subject_kind, subject_id)` — re-evaluation supersedes. Pattern store rolls back prior contribution to its outcome accumulator before applying the new score (pattern-store §10.4). | [§4.6](#46-re-evaluation), [§11.1](#111-pattern-store-slot-4-of-the-routing-chain), pattern-store §10.4 |
| `outcome.primary_model` join                             | Joined client-side from `route.decided.chosen_model` of the subject turn; not embedded in `eval.completed` payload. | [§9.2](#92-analytics-quality) |
| Pattern domain vs eval domain                            | Distinct domains. Pattern store does **not** emit `eval.*` events; evaluator does **not** emit `pattern.*` events. | [§2.2.2](#22-non-goals), [§12](#12-invariants) |
| Fingerprint computation independence                     | Fingerprint = task shape (pattern-store concern); verdict = outcome (evaluator concern). No overlap. | (no overlap) |
| Cost source for K-cluster `avg_cost_M`                   | Sourced from `llm.call_completed.cost_usd` summed over the turn — **not** from `eval.completed.judge_cost_usd` (that's the *judge's* cost, surfaced separately under `/analytics/cost?include_eval=true`). | [§4.5](#45-the-judge_cost_usd-field), [§9.1](#91-get-analyticscost---additive-include_eval-parameter) |
| Session-level vs turn-level verdicts                     | Pattern-store K-nearest is **turn-level only** in v1. Session verdicts are not a pattern-store input; they surface on `/analytics/quality?subject_kind=session` for dashboard use. | [§5.6](#56-session-rubric) |

### 15.1 Closed coordination items

The three open items listed in the original draft are closed:

- **Confidence-gate filter as pattern-store override.** Resolved: the
  filter is a pattern-store config knob (`pattern.min_eval_confidence`),
  not an evaluator-side concern. The evaluator emits unfiltered;
  consumers (pattern store, analytics) decide their own thresholds.
- **Pattern-store outcome rollup losslessness.** Resolved: the
  `eval.completed` payload ([§8.2](#82-eval-completed)) carries
  `subject_id` (the turn_id), `score`, `confidence`, `eval_id`, and
  `judge_pricing_version` — sufficient for the pattern store's
  `update_score()` flow (pattern-store §10.4). No additional fields
  needed in v1.
- **Session verdicts as pattern-store input.** Resolved: turn-level
  only in v1. Pattern-store §10.4 reads `eval.completed` events
  filtered to `subject_kind=turn`.

---

## 16. Decision log

| Date       | Decision                                                                | Rationale                                                                                              |
|------------|-------------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------|
| 2026-05-13 | Numeric `score` in `[0, 1]` as the only structural commitment           | Pattern store consumes one number; multi-dim rubrics expressible via `signals` collapsed by the rubric. |
| 2026-05-13 | `confidence` is a gate, not a score modifier                            | Conflating confidence and score loses signal; downstream consumers (pattern store, analytics) can filter or weight independently. |
| 2026-05-13 | Heuristic-first, LLM-as-judge gated by hybrid escalation                | Default-cheap; LLM judge is opt-in via a single threshold knob the operator can tune from the dashboard. |
| 2026-05-13 | Verdicts are append-only bus events, not a mutable verdict table        | Re-evaluation must not destroy the old verdict (it's the source data for the agreement-rate view).      |
| 2026-05-13 | Three new bus events (`eval.started/completed/failed`)                  | Bus-as-spine consistency; trace store gets re-eval data for free; consumers (pattern store, analytics) subscribe normally. |
| 2026-05-13 | Non-fast-path subscriber                                                | Heuristic is ms-fast; LLM judge is seconds; neither belongs on a user-facing path.                       |
| 2026-05-13 | Per-session AND per-day cost caps                                       | One chatty session can exhaust a daily budget alone; both caps are needed.                              |
| 2026-05-13 | LLM judge defaults to a small model (haiku-class)                       | Spending opus to grade haiku inverts the cost story; small-model classification is the right tier.       |
| 2026-05-13 | No mid-turn evaluation                                                  | Preserves turn-locked-model invariant; the routing chain stays out of the evaluator's path.              |
| 2026-05-13 | Single-user / local-first / per-workspace by default                    | Per [`STRATEGY.md §2`](../STRATEGY.md); multi-user is downstream of the gateway / replacement-agent fork. |
| 2026-05-13 | Re-evaluation produces new verdict, doesn't mutate old                  | Agreement-rate-over-time is a query, not a side-table; preserves audit trail.                            |
| 2026-05-13 | `judge_cost_usd` is `Decimal`, serialized as string in event payload    | Matches `Usage.cost_usd` convention from [`canonical-message-format.md §6.4`](canonical-message-format.md). |
| 2026-05-13 | Rubrics versioned via `rubric_id` + `rubric_version`                    | Changing the rubric is a version bump that produces a new score series; old verdicts remain comparable.   |
| 2026-05-13 | One new analytics endpoint (`/analytics/quality`), additive `include_eval` on `/cost` | Minimal surface increase; SPA composition (agreement-rate, backlog) computed client-side from one event projection. |
| 2026-05-13 | Workload rubric is per-workload in `workload.yaml.evaluate`             | The benchmark harness already owns the workload contract; the evaluator extends it rather than building a parallel surface. |

---

## 17. References

- [`event-bus-and-trace-catalog.md`](event-bus-and-trace-catalog.md) — the
  catalog these events join; non-fast-path subscriber contract; sensitivity
  classifications; dynamic sensitivity for opt-in payloads.
- [`canonical-message-format.md`](canonical-message-format.md) — `Decimal`
  cost convention, trace store schema, ULID generation.
- [`routing-engine.md §5.5`](routing-engine.md) — `success_score`
  consumption shape (the pattern-store side of the contract).
- [`analytics-api.md`](analytics-api.md) — projection conventions, response
  envelope, SQL-injection-safe parameter whitelisting, Decimal serialization
  at the wire boundary.
- [`benchmark.md`](benchmark.md) — v1 limitation closed by this spec
  (quality scoring deferred to the evaluator); workload rubric extension.
- [`context-assembler.md`](context-assembler.md) — prompt-cache discipline
  the LLM judge should follow (open question 13.3).
- [`memory-store.md`](memory-store.md) — sibling spec for shape reference.
- [`../STRATEGY.md §6.7`](../STRATEGY.md) — the open question this spec
  closes.
- [`../project-overview.md`](../project-overview.md) — Evaluator's role in
  the architecture diagram; Phase 3 ("full evaluator") deliverable.
- [`../../AGENTS.md`](../../AGENTS.md) — turn-locked-model invariant the
  evaluator preserves by never running mid-turn.
- [`pattern-store.md`](pattern-store.md) (planned, drafted in parallel) —
  the primary consumer; see §15 for coordination touchpoints.
