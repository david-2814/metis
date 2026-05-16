# Metis — one-pager

> The open-core LLM gateway that makes AI-agent spend visible,
> governable, and cheaper. Drop-in for Claude Code, Cursor, and any
> OpenAI- or Anthropic-shaped SDK; runs on your provider keys.

---

## What it is

A transparent HTTP gateway in front of Anthropic / OpenAI / OpenRouter.
Your devs change one environment variable; their existing tools keep
working. Every turn is cost-stamped per key, per user, per team, per
inbound shape, and per routing decision. The agent upgrade path adds
context shaping, bounded memory, skills, and planner-worker delegation
on the same canonical IR.

---

## The headline numbers

**Delegation is the most reproduced routing-surface lever.** On the
`multi-step-with-delegation` workload, sonnet planner + haiku workers
beats sonnet-only-no-delegation by **8.3% – 26.1% better
cost-per-quality** across three runs, with the A3-rev7 completion landing
at **19.9%**:

| Run | Delegation result |
|---|---:|
| §A3-rev5 | 8.3% better cost-per-quality |
| §A3-rev6 | 26.1% better cost-per-quality |
| §A3-rev7 completion | 19.9% better cost-per-quality |

**Model selection has one canonical end-to-end inversion.** From
[`benchmarks/RESULTS.md §A3-rev3`](../../benchmarks/RESULTS.md), the
three-pass protocol (haiku pinned / sonnet pinned / `--no-active-model`
so slot 4 fires):

| Pass | Strategy | Quality sum | Real-API cost | $ / quality unit |
|------|----------|------------:|--------------:|------------------:|
| A | haiku pinned | 5.16 | $0.198 | **$0.0383** |
| B | sonnet pinned | 5.75 | $0.676 | **$0.1176** |
| C | slot 4 routes per turn | 5.55 | $0.265 | **$0.0477** |

Pass C: **8% more quality than haiku-only at 40% of sonnet-only cost.**
The one sonnet pick was on `regex-with-edge-cases` turn 2 — the hard
"16 edge-case tests" turn where haiku rubric-fails (0.19 in Pass A,
0.74 in Pass C). See [`docs/savings-demo.md`](../savings-demo.md) for
the full mechanism walkthrough.

The A3-rev7 completion is the newest model-selection result: partial
credit worked, but Pass C still picked haiku on every routed turn across
5 workloads (zero sonnet picks across 36 decisions). The N=1 inversion
stands as mechanism proof, not a broad regime.

**Prompt caching:** 100% cache-fire rate on a 49-call benchmark suite
(vs ~33% cold cache), 22.8% same-workload cost reduction
([`benchmarks/RESULTS.md §Run 3`](../../benchmarks/RESULTS.md)).

---

## What this is honest about

- **The model-selection inversion is N=1 in v1.** The mechanism is wired
  end-to-end, but §A3-rev7 completion did not generalize it. Quoting
  "save 62% on your bill" is not what the data supports. Quoting
  "delegation has a reproduced 8.3% – 26.1% cost-per-quality range, and
  model selection has a clear proof-of-mechanism" is.
- **The savings shape depends on your workload.** Three shapes where
  Metis won't move the needle on routing: single-model workloads (every
  turn needs the same model); very short sessions (< 6 turns; cache
  doesn't pay off); no rubric (slot 4 can't learn from outcomes).
  Caching, per-key cost attribution, and audit trail still ship.
- **Local-first means BYO keys, BYO infra, BYO data.** Helm-installable
  in your cluster. No SaaS dependency. Your trace DB never leaves the
  perimeter you draw.

---

## What you change vs what stays

```bash
# Before
export ANTHROPIC_API_KEY="sk-ant-..."
claude   # or cursor, or python -c "import anthropic; ..."

# After
export ANTHROPIC_BASE_URL="http://your-gateway:8422"
export ANTHROPIC_API_KEY="gw_..."        # gateway-issued
claude   # unchanged
```

Per-key / per-user / per-team rollup:

```bash
curl 'http://your-server:8421/analytics/by_team' | jq
curl 'http://your-server:8421/analytics/cost?group_by=user&window=7d' | jq
```

---

## How it composes

| Lever | What it does | Status |
|---|---|---|
| Model selection (slot 4) | K-NN over learned task-fingerprint outcomes; picks cheaper model on easy turns, escalates on hard ones | shipped, N=1 inversion; breadth not generalized in §A3-rev7 |
| Delegation | Sonnet planner spawns haiku workers; cost attributed per role | shipped, 8.3–26.1% cost/quality improvement across 3 runs |
| Prompt caching | Stable system+tools prefix with `cache_control`; padded to provider cache floor | shipped, 100% fire rate |
| Per-key / per-user / per-team cost | Every event stamped with `gateway_key_id` / `user_id` / `team_id` | shipped |
| Audit log + retention + GDPR forget | SOC2-aligned 12-event subset; `metis trace prune`; `metis user forget` | shipped |
| Billing | Open-core gateway + per-seat Pro + Enterprise savings add-on; Stripe-backed, opt-in | shipped |
| Skills | agentskills.io-compatible (35+ implementers including Anthropic, OpenAI Codex, Cursor, Goose) | shipped substrate; agent-side activation in flight |
| Bounded memory (MEMORY.md / USER.md) | 2 KB / 1.5 KB caps; agent-curated; survives session restart | shipped (agent mode only — gateway is stateless per request) |

---

## Pricing shape

Ratified in [`docs/specs/pricing.md §5.5.4`](../specs/pricing.md):

| Tier | Model | Buyer-facing line |
|---|---|---|
| Community | $0 open-core gateway | Self-host gateway + single-user agent surfaces; BYO provider keys |
| Pro | Per active user / month | Team identity, caps, per-user/team analytics, hosted operations, audit export, LLM judge tier, agent upgrade |
| Enterprise | Custom Pro + capped %-of-savings | Outcome-linked add-on for procurement-led buyers |

Metis does **not** resell provider tokens. Anthropic / OpenAI /
OpenRouter still bill the buyer directly; Metis bills for the control
plane and, on Enterprise, a separately contracted savings line.

---

## Why now

- **LiteLLM has a bug-of-the-week problem** on `cache_control`, thinking
  blocks, and tool_use round-trip ([market research §03](../market-research/03-routing-layers.md)).
  Their OpenAI-shape internal IR is lossy on Anthropic-native features
  Metis treats as load-bearing.
- **Per-dev cost attribution is invisible** in most setups. CFOs see a
  $50k/month Anthropic line item with no breakdown. Metis surfaces per
  user, per team, per project on day one.
- **Compliance work is shifting left.** SOC2-aligned audit log, 90-day
  trace retention with audit exemption, and GDPR Article 17 erasure-as-
  pseudonymization ship in v1 — see
  [`docs/operations/soc2-readiness.md`](../operations/soc2-readiness.md).

---

## Deployment shape

- **In-cluster (helm).** Single chart at
  [`infra/gateway/helm/`](../../infra/gateway/helm/); validated on kind
  + production k8s; ServiceMonitor included for Prometheus Operator.
- **Docker compose.** Single-host trial; see
  [`docs/gateway-deployment.md`](../gateway-deployment.md).
- **TLS** terminated upstream (Caddy / nginx-ingress / cloud LB) or
  in-process via `--tls-cert` / `--tls-key` (Wave 13).
- **Billing** opt-in via gateway config; deployments without billing stay
  on the free runtime path.

---

## How to evaluate

| Time budget | Path |
|---|---|
| 1 hour | [`docs/operations/quickstart.md`](../operations/quickstart.md) — kind + helm + pre-baked workload, prints `actual / baseline / savings_pct` |
| 1 day | [`docs/customer-trial-recipe.md`](../customer-trial-recipe.md) Path A — your devs use existing tools through the gateway for a week, read `/analytics/by_team` |
| 1 week | [`docs/customer-trial-recipe.md`](../customer-trial-recipe.md) Path B — 5–10 of your prompts, before/after with a rubric, cost-per-quality column |

---

## What we are not

- Not a SaaS routing service that holds your keys ( ≠ OpenRouter,
  Not Diamond, Martian).
- Not a wrapper around LiteLLM — we wrote our own per-provider adapters
  precisely because LiteLLM's canonical IR loses Anthropic features.
- Not a "replace your IDE / Claude Code / Cursor" play — the gateway
  sits in front of those tools, not in place of them.

---

## Who's behind it

Solo, part-time owner. Specs-first development (every component has a
contract in [`docs/specs/`](../specs/) before code lands). Open about
what works, what doesn't, and what's deferred — see
[`docs/STRATEGY.md`](../STRATEGY.md) §6 for the open questions and
[`docs/KNOWN_ISSUES.md`](../KNOWN_ISSUES.md) for the spec/impl gaps.

---

## One-line follow-ups

- **FAQ:** [`faq.md`](faq.md)
- **vs LiteLLM / Portkey / Helicone:** [`competitive-comparison.md`](competitive-comparison.md)
- **Common objections:** [`objection-handling.md`](objection-handling.md)
- **Pilot template:** [`case-study-template.md`](case-study-template.md)
