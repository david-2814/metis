# Metis vs LiteLLM / Portkey / Helicone

> The three closest gateway-shaped competitors. All three intercept HTTP
> and route across providers; the meaningful differences are in the
> internal message representation, learning vs static rules, and cost
> attribution granularity.

Public GitHub counts spot-checked on 2026-05-16; issue analysis still
comes from the 2026-05-09 market-research sweep (see
[`docs/market-research/03-routing-layers.md`](../market-research/03-routing-layers.md)
for the full survey). Re-verify competitor issue lists before quoting
externally.

---

## At a glance

| Capability | Metis | LiteLLM | Portkey | Helicone |
|---|---|---|---|---|
| **Stars / license** | (launch) / TBD | 47.2k / MIT-ish | 11.7k / MIT (OSS gateway) + SaaS | 5.7k / Apache-2.0 + SaaS |
| **Internal IR** | Canonical-first; Anthropic blocks load-bearing | OpenAI-shape lossy bridge | OpenAI-shape | Mostly pass-through |
| **`cache_control` round-trip** | Lossless (universal placement) | Broken: #26625, #20418, #20485 (Bedrock + Vertex) | Limited; OpenAI-shape constrains it | Pass-through (no placement logic) |
| **Thinking blocks across providers** | Lossless | Broken: #27512, #26916, #24985, #15601 | Limited; not the focus | Pass-through |
| **Tool-use round-trip** | Tested via cross-provider conformance suite | Broken: #27469 (regression in v1.83.7) | Mostly normalized | Pass-through |
| **Routing** | Manual / YAML rules / **learned K-NN over task fingerprints** | User rules, fallback, load-balance | Conditional rules, fallback, A/B, LB | Caching, fallback (light) |
| **Cost attribution** | Per request / **per dev / per team / per project / per inbound shape** | Per request / provider / key / team | Per request / team | Per request / user / session |
| **Prompt-cache discipline** | Stable prefix + padding to provider floor (100% fire rate in v1 benchmark) | None (placement bugs above) | Limited | None |
| **Per-key / per-user / per-team rollup** | Native (multi-user.md) | Per-key + per-team | Per-key + per-team | Per-user + per-session |
| **Audit log** | 12-event subset, JSONL/CSV deterministic export | Logs, not an audit subset | Logs, SaaS surface | Logs, SaaS surface |
| **Trace retention + audit-exempt sweep** | `metis trace prune` + helm CronJob; audit events exempt | SaaS-managed | SaaS-managed | SaaS-managed |
| **GDPR Article 17 (erasure-as-pseudonymization)** | `metis user forget` ships v1 | Buyer-implements | Buyer-implements | Buyer-implements |
| **Pricing shape** | Open-core gateway + per-seat Pro + Enterprise savings add-on | OSS gateway + enterprise | OSS gateway + SaaS / enterprise | OSS + SaaS |
| **Self-host story** | Helm chart + Docker compose; loopback-default; explicit `--host 0.0.0.0` + TLS | Self-host gateway; SaaS dashboards optional | Self-host OSS gateway; SaaS for dashboards | Self-host via base URL |
| **Cloud-required path** | None (BYO keys, BYO infra always) | No | Dashboards SaaS-only | Dashboards SaaS-only |
| **`/metrics` (Prometheus)** | Shipped, 10 series, ServiceMonitor template | Some metrics | Yes (SaaS) | Yes (SaaS) |
| **Replay surviving provider API changes** | Yes (canonical-IR-backed) | No (wire-format logs) | No | No |

---

## What the canonical-IR difference actually means

**LiteLLM's open issues, as of 2026-05-09:**

- `#27512` (2026-05-09) — Anthropic Messages retry **drops thinking blocks**.
- `#27469` (2026-05-08) — `tool_call.function.arguments` lost in
  OpenAI→Anthropic conversion (regression in v1.83.7).
- `#26916`, `#24985` — Anthropic↔OpenAI bridge **collapses thinking
  blocks to text** in multi-turn.
- `#15601` — Anthropic thinking blocks missing on requests with tool calls.
- `#26625`, `#20418`, `#20485` — Bedrock + Vertex `cache_control` placement broken.
- `#26937` — Citations on Bedrock Converse not supported.

These are not edge cases. They are the surfaces a serious Claude
workload touches every turn. LiteLLM fixes them via tickets, not by
design — the OpenAI-shape internal IR can't represent these blocks
losslessly to begin with.

Metis treats Anthropic's content blocks as the **authoritative internal
shape**. Each provider adapter is a translator from canonical →
provider-native and back, tested via a cross-provider conformance suite
that mid-session switches Anthropic → OpenAI → OpenRouter with tool-use
round-trip. The same canonical IR is what powers replay-surviving-
provider-changes (re-pricing historical traces against today's price
table, re-running an evaluator against archived `turn.completed` events).

---

## What the learned-routing difference actually means

| Layer | LiteLLM / Portkey / Helicone | Metis |
|---|---|---|
| Manual selection | `model=...` in the request | `@alias` override / `/model` sticky / request-body `model` |
| Configured rules | YAML / JSON; user authors | YAML at `<workspace>/.metis/routing.yaml`; user authors |
| **Learned** | None | **Slot 4: K-NN over task fingerprints stored per-workspace** |
| Delegation | None | **`delegate(tier, task, context)` tool spawns workers; cost attributed per role** |

The slot 4 mechanism: every turn's outcome (cost, latency, success
score from the evaluator) writes a row into a per-workspace SQLite
pattern store keyed by a structural fingerprint (file extensions / tool
names / side-effect classes / token bucket / intent tags, optionally
augmented with an OpenAI-text-embedding-3-small embedding in v2). At
routing time, the chain consults the K nearest neighbors and aggregates
their `(cost, success)` per model. The cheapest model whose aggregate
success clears the confidence gate wins.

The first model-selection end-to-end demonstration is in
[`benchmarks/RESULTS.md §A3-rev3`](../../benchmarks/RESULTS.md): Pass C
picked sonnet on the one hard turn of `regex-with-edge-cases` and haiku
on every other turn, recovering 99% of sonnet-only's quality at roughly
25% above haiku-only's cost. N=1 inversion; §A3-rev7 completion did not
generalize it (zero sonnet picks across 36 routing decisions on 5
partial-credit workloads), so this is proof-of-mechanism, not a broad
regime. The more reproduced routing-surface claim is delegation:
sonnet planner + haiku workers produce 8.3% – 26.1% better
cost-per-quality across three A3 runs, with a 19.9% midpoint in
§A3-rev7 completion.

**No commodity router ships this.** RouteLLM was an offline classifier
research project (stalled since August 2024). Not Diamond's Python SDK
was archived December 2025. Martian and Unify are managed routing
services that don't see agent structure. LiteLLM and Portkey route by
user-authored rules and fallback chains; neither learns from outcomes.

---

## What the cost-attribution difference actually means

LiteLLM, Portkey, and Helicone all roll up cost per API key. That's
useful for budgets. It doesn't answer:

- "Which dev is spending the most?"
- "Which project / repo is driving cost growth?"
- "Is the marketing team using Opus on tasks haiku could handle?"

Metis stamps `user_id` and `team_id` on every key. Every
`llm.call_completed` and `turn.completed` event carries both as typed
fields. `/analytics/cost?group_by=user|team`, `/analytics/by_user`,
`/analytics/by_team` roll up per identity. See
[`docs/specs/multi-user.md`](../specs/multi-user.md).

---

## What each competitor does better than Metis

**LiteLLM:**
- 47k stars / huge adoption. 100+ provider list out of the box.
- Mature org-level cost dashboards.
- Active commercial team behind it.

**Portkey:**
- Polished SaaS dashboard; observability UX is ahead.
- Conditional routing rule language is more expressive (A/B + LB).
- Has been at this longer.

**Helicone:**
- Newest of the three; cleaner architecture (YC W23, Apache-2.0 gateway).
- Strong observability framing; sessions / users / cost in one view.
- SaaS path is more polished than ours.

If the buyer needs polished SaaS dashboards today, none of these is
wrong. The choice is between "SaaS dashboards now, canonical-IR + learned
routing later" vs "canonical-IR + learned routing now, dashboards in
progress."

---

## What Metis does that none of the three does

1. **Canonical IR that round-trips Anthropic-native blocks.** Not an
   OpenAI-shape bridge with a bug backlog.
2. **Learned routing over task fingerprints.** Pass C of `§A3-rev3` is
   the demo.
3. **`delegate(tier, task, context)` as a tool.** Planner spawns workers
   inside the agent loop; cost attributed per role. Closest analog is
   Aider's `architect+editor` (CLI-only, two tiers); nobody else ships
   delegation as a first-class agent tool. Metis has three benchmark
   datapoints at 8.3% / 19.9% / 26.1% better cost-per-quality on the
   fan-out workload.
4. **Replays survive provider API changes.** Canonical-IR-backed; refeed
   old traces through today's price table or today's evaluator and the
   numbers move correctly.
5. **Local-first, BYO-infra, BYO-keys by default.** No SaaS dependency
   anywhere in the path. Your trace DB never leaves the perimeter you
   draw.
6. **Bounded memory + skills substrate** (gateway is per-request
   stateless; the substrate is in the agent surface — `metis chat` /
   `metis serve` / future "Metis Pro" tier).

---

## What about a "router-becomes-agent" risk?

The lunch-eat risk in this lane is not a router becoming an agent. It's
an *agent SDK* (Vercel AI SDK) becoming a better abstraction. Vercel
has 24k stars on the cleanest typed message abstraction in TypeScript,
and they've been visibly pushing into agents. If they ship a typed
`Agent` with delegation primitives, they compete on the SDK side. Their
own thinking-block bugs (#13430, #13703, both open as of Mar–Apr 2026)
suggest the canonical-IR work is harder than it looks; Metis's head
start on lossless Anthropic-block round-trip is a real moat for now.

LiteLLM adding a managed agent runtime on top of the proxy is the
medium-risk scenario; their codebase is a serialization minefield, and
an agent layer there would inherit the bugs.

OpenRouter, Portkey, Helicone, Not Diamond, Martian, Unify are unlikely
to ship an opinionated agent. Per-token margin and routing fees
discourage it.

---

## Comparison table — picking a path

| If you want… | Pick |
|---|---|
| Polished SaaS dashboards today | Portkey or Helicone |
| Provider catalog with light routing | LiteLLM |
| Learned routing that picks the model per task | Metis |
| `delegate()` planner/worker inside the agent loop | Metis (eventually Aider for CLI two-tier) |
| Per-user / per-team cost attribution out of the box | Metis (others roll up per key) |
| Canonical-IR-backed replay across provider changes | Metis |
| BYO infra, no SaaS dependency | LiteLLM (self-host) or Metis |

---

## When to disqualify Metis honestly

- The buyer wants a turnkey SaaS dashboard with SSO, multi-org, and
  on-call white-glove — Portkey or Helicone, not us.
- The buyer wants 100+ provider list out of the box today — LiteLLM,
  not us. (Metis ships Anthropic / OpenAI / OpenRouter; OpenRouter
  brings the long tail, but it's one hop.)
- The buyer doesn't care about Anthropic-native features and lives in
  pure OpenAI-shape traffic — the canonical-IR moat doesn't help them.
  LiteLLM's bug list doesn't bite either.
- The buyer needs SOC2 Type 2 *today*. Metis's SOC2 readiness audit is
  shipped ([`docs/operations/soc2-readiness.md`](../operations/soc2-readiness.md));
  Type 1 target is Q3 2026 contingent on an audit-fee underwriter.
