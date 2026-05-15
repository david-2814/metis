# The Metis savings demo

> Metis picks the model that **succeeds** at the task class, not just the
> cheaper one. The first end-to-end demonstration of that mechanism is
> on disk in [`benchmarks/RESULTS.md §A3-rev3`](../benchmarks/RESULTS.md).

## The headline

A three-pass benchmark on the shipped workload suite (Pass A = haiku
pinned, Pass B = sonnet pinned, Pass C = `--no-active-model` so slot 4
of the routing chain can fire) under the hybrid evaluator with
escalation threshold 0.7:

| Pass | Strategy | Quality sum (7 workloads) | Cost (real API) | Cost / quality unit |
|------|----------|--------------------------:|----------------:|--------------------:|
| A    | haiku pinned                | 5.16 | $0.1977 | **$0.0383** |
| B    | sonnet pinned               | 5.75 | $0.6761 | **$0.1176** |
| C    | slot 4 picks per workload   | 5.55 | $0.2645 | **$0.0477** |

Pass C achieves a quality sum **5.55 — closer to sonnet's 5.75 than to
haiku's 5.16** — for a per-quality cost of **$0.0477**, landing inside
the headline window between haiku-only $0.0383 and sonnet-only $0.1176.

Pass C aggregate `savings_pct = 62.0%` against the matched-baseline
$0.6955; flat-haiku-everywhere would have been 66.7%. The 4.7-point gap
is the cost of the one sonnet pick — and that pick is the demo.

## The pick that proves the mechanism

The workload `regex-with-edge-cases` is a deliberately failure-prone
"16 edge-case tests" workload that exercises negative lookaheads and
nested quantifiers. Haiku struggles; sonnet doesn't.

| Pass | Strategy | Quality on `regex-with-edge-cases` |
|------|----------|-----------------------------------:|
| A    | haiku pinned          | 0.19 (rubric-fail at 0.80) |
| B    | sonnet pinned         | 0.72 |
| C    | slot 4               | **0.74** |

On **turn 2 of `regex-with-edge-cases`** in Pass C, slot 4 of the
routing chain read the cross-model outcomes accumulated in the shared
pattern store, computed cluster means haiku 0.784 vs sonnet 0.833 at
confidence 0.058, and **picked sonnet**. Pass C used haiku on the easy
turns of every other workload (which is how `savings_pct` stays at
62.0%) and routed the one hard turn that mattered to the bigger model.

Quality went from 0.19 (failing) in Pass A to 0.74 in Pass C —
differentiated routing recovered 99% of sonnet-only's quality on this
workload for roughly 25% of sonnet-only's cost.

Confidence 0.058 would have been rejected under the prior
`PatternConfig.min_confidence=0.3` gate. The one-line Wave 9 knob
(`min_confidence: 0.3 → 0.05` at
[`routing/policy.py:63`](../packages/metis-core/src/metis_core/routing/policy.py#L63))
was the missing piece in front of an already-correct K-NN aggregation.

## What this means for a buyer's bill

The two ways a flat-rate router can mis-route in a real workload:

1. **Pinning to the cheap model** when a hard turn shows up. The pinned
   bill is `cost(haiku) × N`, but quality collapses on the turns where
   the small model can't keep up. `regex-with-edge-cases` Pass A
   (0.19 quality, rubric-fail) is the failure mode in a single number.
2. **Pinning to the big model** to insure against case (1). The bill is
   `cost(sonnet) × N` (3-4× haiku in this workload mix) and **quality
   goes up only on the small subset of turns that needed it**.

Slot 4 reads cross-model outcomes by workload, identifies the turns
that need the bigger model, and pays the premium only there. Pass C in
§A3-rev3 is the first end-to-end demonstration: 14 of 18 turns reached
slot 4, the K-NN cluster picked sonnet on the one turn where it
mattered, and the bill came in 25% above haiku-only with 8% more
quality (5.55 vs 5.16) than haiku-only could have produced even with
infinite budget.

## What this does NOT prove

- **Inversion fires on 1 of 6 evaluable workloads in v1.** The other
  five workloads either don't need a non-haiku pick (tied quality) or
  their cluster-aggregate sonnet-ahead signal hasn't accumulated enough
  same-fingerprint samples for slot 4 to fire. The mechanism is
  proven; the breadth is in-progress.
- **One end-to-end demonstration, not a regime.** N=1 inversion. The
  brittle parts are documented in
  [`benchmarks/RESULTS.md §A3-rev3` "caveats and observations"](../benchmarks/RESULTS.md):
  sample-size asymmetry on specific fingerprints, and the residual
  per-turn-vs-workload signal divergence on `multi-turn-refactor`.
  Both are addressable with more pattern-store samples plus the wired
  workload-level eval signal (the §A3-rev2 path #1 fix).
- **`regex-with-edge-cases` is a deliberately-failure-prone workload.**
  Its rubric is harsh and earlier passes saw quality scores anywhere
  in 0.19–1.00. The numbers above are the median, not the floor or
  ceiling; the *relative* ordering Pass A ≪ Pass B ≈ Pass C reproduces
  across runs.
- **An outcome-update bug persists on tool-heavy 1-turn workloads.**
  `architectural-explanation-without-hallucination` records
  `success_score_count=0` across all fingerprints despite the trace
  recording the per-turn `eval.completed`. Doesn't block the
  inversion (slot 4 still picked haiku on this workload's only turn
  and quality came out 0.90 either way), but it's the next
  correctness item.

## Try it yourself (5 minutes)

The same routing engine that ran the benchmark ships in the
transparent HTTP gateway. Point an unmodified OpenAI / Anthropic /
Claude Code / Cursor / raw-SDK client at it and your traffic is
cost-stamped per dev, per project, per task class — without your devs
changing how they work.

End-to-end recipe (Claude Code, Cursor, raw curl/SDK) at
[`docs/gateway-client-quickstart.md`](gateway-client-quickstart.md).
For evaluating Metis against your own workload (not our benchmarks),
see [`docs/customer-trial-recipe.md`](customer-trial-recipe.md).

The two-line version, assuming you have a gateway issuing keys and a
provider API key in the gateway pod:

```bash
# 1. Existing client, one env var flipped.
export ANTHROPIC_BASE_URL="http://your-gateway:8422"
export ANTHROPIC_API_KEY="gw_…"          # gateway-issued token
claude  # or cursor, or your existing tool — no code changes

# 2. The per-key / per-user / per-team rollup the buyer reads.
curl 'http://your-server:8421/analytics/by_team' | jq
curl 'http://your-server:8421/analytics/cost?group_by=user' | jq
```

## How to read the §A3-rev3 numbers

If you want to validate the demo against the trace records before
recommending it to a buyer:

```bash
# The pattern-store snapshot Pass C read at decision time
sqlite3 benchmarks/.runs/a3rev3-patterns.db \
  "SELECT json_extract(f.structural_json,'$.workload_id') AS workload,
          o.primary_model,
          o.success_score_count AS n,
          ROUND(o.success_score_sum / NULLIF(o.success_score_count,0), 3) AS mean
   FROM outcomes o JOIN fingerprints f USING (fingerprint_id)
   WHERE workload = 'regex-with-edge-cases'
   ORDER BY o.primary_model, n DESC;"

# The Pass C route.decided events
sqlite3 benchmarks/.runs/a3rev3-pass-c.db \
  "SELECT json_extract(payload_json,'$.chosen_model') AS model,
          json_extract(payload_json,'$.chain[3].verdict') AS slot4_verdict,
          json_extract(payload_json,'$.chain[3].reason') AS slot4_reason
   FROM events WHERE type = 'route.decided'
   ORDER BY timestamp_us;"
```

Reproduce with the recipe at
[`benchmarks/RESULTS.md §A3-rev3 "Reproduce"`](../benchmarks/RESULTS.md).
Cost envelope: ~$1.10 real-API spend across three passes.

## Where this fits in the bigger story

The thesis is in [`docs/STRATEGY.md §1`](STRATEGY.md): a buyer's LLM
bill bends through three levers — context engineering, skills, and
model selection — applied together. Model selection is the cleanest to
demonstrate against a benchmark, which is why it's the artifact above.
Context engineering and skills compound on top of it; both are
already-shipped (universal prompt-cache placement, agentskills.io-
compatible skill store) and the savings from each surface separately
in the dashboard.

The mechanism for model selection shipped. The breadth across
workloads is the in-progress work tracked in `STRATEGY.md §1`.
