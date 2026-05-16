# Customer trial recipe

> For a buyer who wants to validate Metis against their own workload
> in under a day. Deploy the gateway, point your existing tool at it,
> run your prompts, read the cost-per-quality column.

> **Want a "first savings number in &lt; 1 hour" with no workload of
> your own?** Start at [`operations/quickstart.md`](operations/quickstart.md)
> — `infra/gateway/scripts/quickstart.sh` automates the helm install
> end-to-end and `metis trial` runs a pre-baked workload through the
> gateway. Come back here when you want to swap our workload for yours.

Pairs with [`savings-demo.md`](savings-demo.md) — that doc is the
evidence we collected on our benchmark; this doc is how to collect
the same evidence on your workload. Time budget: 60–90 minutes for
setup, plus however long you want to run real workload through it.
Smoke-phase real-API spend: well under $1.

## 1. Setup — pick one

### Docker (fastest)

Per [README "Try it — transparent gateway in Docker"](../README.md):

```bash
cp .env.example .env && $EDITOR .env               # set ANTHROPIC_API_KEY
docker compose run --rm gateway issue-key \
  --name "alice-laptop" --workspace /workspace
docker compose up -d && curl http://127.0.0.1:8422/healthz
```

Save the printed `gw_…` token; it prints once.

### Kubernetes via helm (one-command, kind cluster)

```bash
infra/gateway/scripts/quickstart.sh   # build, kind, install, key, port-forward
source .metis-trial/state.env         # exports METIS_TRIAL_GATEWAY_URL / KEY
```

This automates the recipe in
[`docs/operations/quickstart.md`](operations/quickstart.md). For the
detailed kind-cluster walkthrough (image build, kind load, helm install,
port-forward), see
[`docs/gateway-deployment.md §"First production smoke"`](gateway-deployment.md)
(validated 2026-05-15 at $0.00012 for 4 haiku calls).

For a multi-user trial, issue per-user / per-team keys so the
identity rollups in §4 are populated:

```bash
metis gateway issue-key --keystore ./keys.json \
  --name "alice" --workspace /workspace --user alice --team eng
```

The `user_id` / `team_id` fields are tags — no quotas in v1, but
every `llm.call_completed` and `turn.completed` carries them and
analytics rolls up by either.

## 2. Workload — use your prompts

Two paths, depending on rigor:

**Path 0 — "the pre-baked workload."** Run `metis trial --gateway-url
… --gateway-key …`. This is the smoke test from
[`operations/quickstart.md`](operations/quickstart.md) — one workload,
one model, takes &lt; 2 minutes, costs &lt; $0.10. Output is the
`actual / baseline / savings_pct` block, suitable for an internal
"this thing works end-to-end" demo before you write your own rubric.
Not a substitute for Path A or B.

**Path A — "watch and read."** Devs use their existing tools through
the gateway for a week. After a week, read `/analytics/by_user` and
`/analytics/by_team` and see what the distribution looks like.
Adequate for a "is this saving money" sniff test.

**Path B — "before / after with a rubric."** Pick 5–10 prompts your
team runs hundreds of times per week. For each, write a 3–5 question
pass/fail rubric. Run through your current setup and through Metis;
compare cost-per-quality, not just cost. This is what landed
`regex-with-edge-cases` as the demo workload in `savings-demo.md`.

### Caveats while you build the workload

- **Temperature ≠ 0 adds noise.** Three runs at temp 0 → median;
  three runs at temp 0.7 → variance. Don't conflate them.
- **Baseline matters.** "Saves 60%" against opus-pinned is different
  from sonnet-pinned or your existing routed baseline. State which.
- **Judge tier matters.** Binary pass/fail rubric → `HeuristicJudge`.
  "Rate 1-5" → `LLMJudge`. Mixed → `HybridJudge(0.7)` (our default
  in `savings-demo.md`). Configure via `--judge` on
  `scripts/benchmark.py` if you adapt our harness.

## 3. Running through the gateway

Point your tool at the gateway URL (full Claude Code / Cursor /
raw-SDK matrix at
[`gateway-client-quickstart.md`](gateway-client-quickstart.md)):

```bash
export ANTHROPIC_BASE_URL="http://your-gateway:8422"  # Claude Code, anthropic-python
export OPENAI_BASE_URL="http://your-gateway:8422/v1"  # Cursor, openai-python
export ANTHROPIC_API_KEY="gw_…"   # OR OPENAI_API_KEY — same gateway token
```

Every request produces `route.decided` + `llm.call_started/completed`
+ `turn.completed` events, stamped with `gateway_key_id`, `user_id`,
`team_id`, `inbound_shape`, `cost_usd`, token counts, cache fields.

## 4. Reading the result — cost first

```bash
curl 'http://your-server:8421/analytics/cost?group_by=user&window=7d' | jq
curl 'http://your-server:8421/analytics/by_team' | jq
curl 'http://your-server:8421/analytics/savings?window=7d' | jq
```

`/analytics/savings` reports `actual_repriced_usd` (your spend at
current prices) and `baseline_repriced_usd` (same turns if every
call had gone to the configured naive baseline). `savings_pct =
1 - actual/baseline`. Both are recomputed against the same price
table, so it's apples-to-apples to your provider invoice.

## 5. Reading the result — quality second

If you ran with an evaluator subscribed (default for `metis serve`;
see [`evaluator.md`](specs/evaluator.md)):

```bash
curl 'http://your-server:8421/analytics/quality?group_by=model&window=7d' | jq
```

This endpoint joins each verdict to the `route.decided` for the same
turn, so the model attributed is the one *judged*, not the judge's
own model.

The buyer report headline is three numbers:

1. Total spend (from `/analytics/cost`)
2. Total quality score (sum from `/analytics/quality`)
3. Cost-per-quality = 1 ÷ 2

State both Metis and pinned-baseline columns side by side.

## 6. When this trial won't show savings

Be honest about the shapes where Metis's wedge doesn't fire:

- **Single-model workloads.** Every turn needs the same model →
  slot 4 won't find a cheaper alternative. Caching still saves
  ~22-25% on long sessions; routing saves ~0%.
- **Very short sessions.** Sessions under ~6 turns rarely cross
  the cache-write break-even. `RESULTS.md §Run 3` quantifies it:
  caching fires on 49 of 49 calls *when sessions are long enough*.
- **No quality signal.** No rubric / test suite / feedback → slot 4
  can't learn from outcomes. Pattern store still accumulates cost +
  latency; success-aware routing needs an evaluator wiring
  `eval.completed` back. (And see the outcome-update bug in
  `RESULTS.md §A3-rev3 caveats` for one tool-heavy pattern where
  this currently drops.)

If any of the three match, run the trial anyway — you get clean
per-user cost attribution and audit trail, which is half the buyer
story. Quote the savings as "policy enforcement + visibility, not
routing-driven" so you don't promise a number Metis can't deliver.

## 7. After the trial — carry away

1. Cost-per-quality on your real workload.
2. Per-user / per-team rollup — usually 2-3× outliers on day 1.
3. List of prompts where slot 4 fired and why; the
   `route.decided.chain` field carries the workload_id and the K-NN
   cluster means
   ([`RESULTS.md §A3-rev3 "the KEY TABLE"`](../benchmarks/RESULTS.md)
   shows the shape).

Those three are everything you need for the internal "do we adopt
this" review.
