# Deployment Shape

**Status:** Draft v1 — recommendation, awaiting owner sign-off
**Last updated:** 2026-05-13

> Resolves the architectural fork in [`STRATEGY.md §3`](../STRATEGY.md) and the open question in §6.1: **replacement agent vs. transparent gateway vs. hybrid**. This spec is the recommendation and the rationale; the STRATEGY.md edits land only after sign-off.

---

## 1. Decision

**Build the hybrid: gateway first, agent upgrade second.**

Concretely:

1. **Phase 2 wedge** — ship a transparent HTTP gateway (`apps/gateway/`) that speaks OpenAI-shape (and later Anthropic-shape) inbound, routes through the existing engine and adapters, and tracks cost per API key. The buyer flips one env var; their devs keep using Claude Code / Cursor / Codex / Continue without behavior changes. See [`gateway.md`](gateway.md).
2. **Phase 3+** — continue investing in the replacement agent (CLI / TUI / future desktop) as the **upgrade path**. Skills, bounded memory, the context assembler, learned routing, and agent-internal delegation are the high-ceiling features that the gateway form factor cannot deliver. They become "Metis Pro" — what a buyer adopts after the gateway has already proved savings on their workload.
3. **Shared substrate** — both deployments compose the same `metis-core` library (canonical IR, routing engine, adapters, pricing, trace store, memory). The gateway and the agent are different *front doors* to the same engine; they do not fork the codebase.
4. **Not on the table** — gateway-only (caps the ceiling and walks away from work already built) and agent-only (caps the floor and ignores the dominant adoption-friction risk in [`STRATEGY.md §3`](../STRATEGY.md)).

The remainder of this spec is the survey and effort math behind that recommendation.

---

## 2. The three deployment shapes

### 2.1 Agent-only

The current build trajectory: a replacement coding agent (CLI today, TUI shipped, desktop later) where Metis owns the entire loop — routing, context, tools, memory, skills.

- **Ceiling:** very high. All three cost levers from [`STRATEGY.md §1`](../STRATEGY.md) (context > skills > model selection) are reachable. The "agent that gets cheaper as it learns your workload" pitch from `synthesis.md` only works here.
- **Floor:** very low. The savings story only materializes after a dev adopts the tool *and* uses it for weeks. The buyer can't measure savings on day one because the workload that produced the baseline isn't running through the agent yet.
- **Adoption friction:** the highest cost in B2B dev-tooling per [`STRATEGY.md §3`](../STRATEGY.md). Asking devs to switch from Claude Code (122k★) / Cursor / Codex / Cline / OpenCode (157k★) is the #1 reason such buys don't land.
- **Market position:** the TUI multi-provider agent lane has eight credible OSS options already. Metis enters as competitor N+1 on architecture and differentiates only on memory + skill-learning mechanics that take quarters of buildout to make legible to a buyer.

### 2.2 Gateway-only

Metis becomes a transparent HTTP proxy: OpenAI-shape (and Anthropic-shape) inbound, provider-native outbound via the existing adapter set. Devs keep using whatever agent they already use. The buyer changes one env var.

- **Ceiling:** lower. The gateway sees one LLM call at a time. It can route, cache, fall back, and meter cost — but it cannot shape the prompt envelope inside someone else's agent loop. Skills, bounded memory, context assembly, and cross-turn pattern learning are unreachable. The headline savings lever (context engineering, per [`STRATEGY.md §1`](../STRATEGY.md)) is left on the table.
- **Floor:** very high. Buyer flips `OPENAI_BASE_URL`; devs don't notice; savings show up on the dashboard within hours. This is the GTM motion LiteLLM / Portkey / Helicone all run.
- **Adoption friction:** near-zero — that's the point.
- **Market position:** competing on margin with LiteLLM (46k★), Portkey (12k★), Helicone (6k★). The defensible wedge per [`synthesis.md`](../market-research/synthesis.md) — lossless canonical IR that round-trips Anthropic's `cache_control` / `thinking` / `tool_use` / citations — is *real* in the gateway shape (see §3.4 below) but it doesn't differentiate on the dashboard.

### 2.3 Hybrid (gateway first → agent upgrade)

Ship the gateway as the foot-in-the-door. Use it to prove savings on the buyer's actual workload, charge for the cost-dashboard. Position the replacement agent as the upgrade — the way buyers reach the context + skills + memory levers once the gateway has already paid for itself.

- **Ceiling:** the agent ceiling (high).
- **Floor:** the gateway floor (high).
- **Adoption friction:** gateway-low at first contact; agent-high only for the subset of buyers who want the next tier of savings.
- **Engineering cost:** highest *surface*, but lowest *new code* — the gateway is ~80% of what's already shipped (see §4).

---

## 3. The reference shapes — what LiteLLM, Portkey, and Helicone actually are

Surveyed 2026-05-13. State for LiteLLM open-issue claims sourced from [`docs/market-research/03-routing-layers.md`](../market-research/03-routing-layers.md) (verified 2026-05-09); install behavior and config shape sourced from each project's quickstart docs.

### 3.1 LiteLLM proxy (BerriAI, 46k★, MIT-ish)

- **Install friction.** `uv tool install 'litellm[proxy]'` + `litellm --model <name>`; proxy listens on `0.0.0.0:4000`. Application sets `base_url=http://0.0.0.0:4000` and a dummy `api_key`. Inbound supports both OpenAI-shape (`POST /chat/completions`, `/completions`, `/embeddings`) **and** Anthropic-shape (`POST /v1/messages`).
- **What it intercepts.** HTTP only. Not an agent loop.
- **Routing capabilities.** YAML-configured: `fallbacks`, `num_retries`, `request_timeout`, `routing_strategy` (`simple-shuffle` / `least-busy` / `usage-based` / `latency-based`), per-deployment `rpm` / `tpm` for weighted load balance. Virtual keys for per-team budgets.
- **Known limits — load-bearing for the gateway question.** Per the open-issue list in [`docs/market-research/03-routing-layers.md`](../market-research/03-routing-layers.md): `#27512` (Anthropic Messages retry drops thinking blocks), `#27469` (tool_call `function.arguments` lost in OpenAI→Anthropic conversion), `#15601` (thinking blocks missing on tool-call requests), `#26916` / `#24985` (thinking blocks collapsed to text in multi-turn), `#26625` / `#20418` / `#20485` (Bedrock + Vertex `cache_control` placement broken), `#26937` (citations on Bedrock Converse not supported). The bug-of-the-week pattern is exactly the surface Metis treats as load-bearing.

### 3.2 Portkey (Portkey AI, 12k★ OSS gateway + SaaS)

- **Install friction.** OSS gateway via `npx @portkey-ai/gateway`, or use the SaaS. Application changes base URL and adds headers. SDK install optional.
- **What it intercepts.** HTTP/API layer. Multi-modal (vision/audio/image). MCP support to attach external tools, but not an agent loop.
- **Routing capabilities.** JSON config with `strategy.mode` (`fallback` / `loadbalance` / `single`) + `targets` array supporting `passthrough`, `provider`, `override_params`. Conditional rules, A/B, load balancing. Sophisticated and well-documented.
- **Known limits.** Docs don't enumerate tool_use / cache_control / thinking-block fidelity claims at the gateway level. Treat with the same skepticism as LiteLLM until proven on the buyer's actual workload (Anthropic block round-trip is hard; nobody who hasn't built canonical IR gets it right).

### 3.3 Helicone (Helicone, 6k★ OSS + SaaS, YC W23)

- **Install friction.** Lowest of the three. Base URL → `https://ai-gateway.helicone.ai`, set `HELICONE_API_KEY`. Drop-in. Or self-host.
- **What it intercepts.** HTTP proxy. Observability-first; the gateway is the newer surface bolted onto the observability core.
- **Routing capabilities.** Caching, fallback, basic routing. Lighter than Portkey or LiteLLM on this axis. The product center of gravity is dashboards (requests, sessions, prompts, datasets, alerts), not routing policy.
- **Known limits.** Docs are quiet on streaming / tool_use / Anthropic-specific block fidelity at the quickstart level. Likely passthrough-shaped with minimal canonicalization, which means whatever the upstream provider speaks is what the client gets — a different fidelity model from LiteLLM (which transforms) and from Metis (which canonicalizes losslessly).

### 3.4 The pattern across all three

All three intercept **HTTP only**. None wraps an agent loop, owns context assembly, loads skills, or curates memory. None ships bounded memory or learned routing. All three have either documented or strongly-suspected fidelity gaps on Anthropic-native blocks — and the one that documents the issues most honestly (LiteLLM) has 8+ open issues in May 2026 on exactly those surfaces.

**This is the wedge for a Metis gateway, even if the product is "yet another OpenAI-shape proxy":** lossless canonical IR is invisible on the marketing page but load-bearing for buyers running Anthropic models through tools (which is everyone using Claude Code). A gateway that doesn't drop thinking blocks on retry, doesn't collapse `tool_use.input` across providers, and places `cache_control` correctly on Bedrock would be the only one in the lane that does.

---

## 4. Effort estimates

### 4.1 Minimum gateway prototype

What it has to do:

- Accept `POST /v1/chat/completions` (OpenAI-shape inbound; the universal contract every client speaks).
- Translate the inbound request into a canonical `CanonicalRequest` (per [`provider-adapter-contract.md §3`](provider-adapter-contract.md)).
- Route via the existing `RoutingEngine` (per [`routing-engine.md`](routing-engine.md)).
- Call the chosen adapter (Anthropic / OpenAI / OpenRouter) using the existing `metis_core.adapters` package.
- Translate the `CanonicalResponse` back to OpenAI-shape and return (or stream as OpenAI-shape SSE deltas).
- Attribute cost to an API key (existing pricing + trace store handle this once we add a `tenant_id` / `gateway_key_id` to the trace events).

What's reusable from `metis-core` (so we're not building from scratch):

| Component | Status | Reuse for gateway |
|---|---|---|
| Canonical IR | shipped | core |
| Adapters (Anthropic, OpenAI, OpenRouter) + streaming | shipped | core |
| Routing engine (7-slot chain, availability, validation) | shipped | core; gateway uses primarily rule / workspace-default / global-default slots |
| Pricing + cost stamping | shipped | core; add per-key attribution |
| Trace store + analytics API | shipped | core; extend with gateway-key dimension |
| Tool-id map | shipped | needed at gateway scope (per-request, not per-session) |

What's missing (the actual gateway build):

- **Inbound translators.** OpenAI-shape → canonical `Message` / `ToolDefinition` / system-prompt extraction (system role hoist, tool-result re-merge from `role: tool` → `user` with `ToolResult` blocks). Mirror the egress translator in `adapters/openai.py` but in reverse.
- **OpenAI-shape SSE outbound.** Map canonical streaming events to OpenAI's `data: {"choices": [{"delta": ...}]}\ndata: [DONE]\n` shape. The hard parts are tool-call deltas (OpenAI's `tool_calls[].function.arguments` are streamed as JSON-string fragments) and reasoning/thinking summaries.
- **Stateless harness.** No `SessionManager` — the gateway is per-request; the client owns the tool loop and re-submits with `tool_result` blocks. We need a thin equivalent that runs routing + adapter call + cost stamping without spinning up a workspace session.
- **Per-key auth + cost attribution.** Reuse the attach-token plumbing in `apps/server/src/metis_server/tokens.py` as a starting point; add a `gateway_keys` table keyed on the inbound `Authorization: Bearer` header.
- **Optional `POST /v1/messages` inbound.** Anthropic-shape; closer to the canonical IR (`tool_use` blocks already round-trip cleanly), so this should be a smaller add than the OpenAI side.

**Estimate.** ~80% of the code already exists. The new surface is bounded: an inbound translator (mirror of an existing outbound translator), an SSE serializer, a stateless harness, and a per-key auth bolt-on.

- **MVP (OpenAI-shape inbound only, sync + SSE, single global key):** **3–4 engineer-weeks.**
- **+ `POST /v1/messages` (Anthropic-shape inbound) and tool-call round-trip end-to-end through Claude Code:** **+1–2 weeks.**
- **+ multi-key cost attribution, basic rate limiting, error-class translation:** **+1–2 weeks.**
- **Total to "buyer plug-in":** **5–8 engineer-weeks** at the project's part-time pace, allow ~2–3 calendar months.

What this estimate explicitly excludes: configured-rule policy completion (already on the Phase 2 roadmap; the gateway can ship with `rule` slot still stubbed), prompt-cache breakpoint optimization in the gateway path (Phase 3), team/RBAC (post-pilot), multi-tenant hardening (post-pilot).

### 4.2 Replacement-agent polish to ship to a buyer

What it has to do: be presentable as a coding agent a buyer's devs could actually adopt instead of Claude Code or Cursor.

Surveyed gaps (from `apps/cli/src/metis_cli/tui/app.py` — currently 557 lines, single file — and from the "What's NOT built" list in `AGENTS.md`):

| Gap | Effort |
|---|---|
| TUI: multi-session pane, sidebar, cost panel, model picker UI, settings UI, tool-confirmation UI | 2–3 weeks |
| Real tool-confirmation handler (replace `AutoAllowHandler` — currently auto-approves writes and shell) | 1 week |
| Onboarding flow: first-run wizard, model discovery, `.env` setup, sample skills | 1–2 weeks |
| Public docs / install scripts / "5-minute setup" page | 1 week |
| Savings benchmark + demo workload (called out as the biggest gap in [`STRATEGY.md §2`](../STRATEGY.md)) | 2–3 weeks |
| Context assembler design + spec + first implementation (the biggest cost lever; currently architecture-diagram-only) | **separate, large — 4–6 weeks** |

**Estimate.** Polish-only (ignoring context assembler, which is a build, not polish): **5–8 engineer-weeks.** With context assembler — which is what makes the replacement-agent ceiling story real: **10–14 weeks.**

Critical caveat: polish does not change the §3 adoption ceiling. A perfectly polished replacement agent still has the "make your devs switch tools" friction that's the #1 reason B2B dev tool buys don't land. Polish makes the agent shippable; it does not make it adopted.

---

## 5. Why hybrid wins on the math

### 5.1 Surface-area math

The gateway and the agent share the same `metis-core` substrate. Counting the build surface that doesn't double:

| Surface | Agent | Gateway | Shared |
|---|---|---|---|
| Canonical IR | — | — | ✓ |
| Adapters | — | — | ✓ |
| Routing engine | — | — | ✓ |
| Pricing / cost | — | — | ✓ |
| Trace store / analytics | — | — | ✓ |
| Memory store | ✓ | — | — |
| Skill loading | ✓ | — | — |
| Context assembler | ✓ | — | — |
| Tool dispatcher | ✓ | — | — |
| Session manager | ✓ | — | — |
| TUI / CLI | ✓ | — | — |
| Inbound HTTP translators (OpenAI / Anthropic shape) | — | ✓ | — |
| SSE serializer | — | ✓ | — |
| Stateless gateway harness | — | ✓ | — |
| Per-key auth / cost attribution | — | ✓ | — |

The agent-specific column is what's already mostly built. The gateway-specific column is bounded and small (~5–8 weeks). Doing both costs roughly **agent-polish + gateway-MVP**, not 2×.

### 5.2 GTM math

| Shape | Time-to-first-savings-on-buyer-workload | Sale velocity | Per-account ceiling |
|---|---|---|---|
| Agent-only | weeks (after dev adoption) | slow | high (full three-lever story) |
| Gateway-only | hours (env var flip) | fast | medium (model selection + cache only) |
| Hybrid | hours (gateway) → weeks (agent upsell) | fast floor, high ceiling | high |

The gateway gives Metis the artifact the project doesn't yet have and needs most: **proof of savings on the buyer's actual workload** ([`STRATEGY.md §2`](../STRATEGY.md): *"This is currently the biggest gap between 'the architecture should work' and 'we can show it works.'"*).

### 5.3 Risk math

- **Gateway-only risk:** routers commoditize. Margin compression. Defensibility relies on the canonical-IR moat being visible to the buyer, which is not yet proven legible at the dashboard.
- **Agent-only risk:** the §3 adoption-friction risk, plus the saturation risk per [`synthesis.md`](../market-research/synthesis.md) (TUI lane is crowded; the differentiating mechanics — memory + skills + fingerprint routing — take quarters to build legibly).
- **Hybrid risk:** option-value compounding. The agent investment is *not abandoned* — it's repurposed as the upgrade tier. The gateway investment is small (5–8 weeks) and the floor it buys is high. Worst case, the agent never matters and we are a gateway. Better case, the agent becomes "Metis Pro" and the gateway is the funnel.

---

## 6. What this means for adjacent open questions

- **[`STRATEGY.md §6.3`](../STRATEGY.md) — local-first vs. SaaS.** The gateway-first GTM motion implies a server-mode-or-SaaS posture, not strict local-first-on-laptop. Buyers want "flip a URL" not "install a daemon on every laptop." Local-first remains a *deployment* property (BYO keys, BYO infra) but the v1 product is "deployed Metis instance" — either in the buyer's VPC or hosted by Metis. This narrows §6.3 without resolving it; the choice between SaaS and self-host-in-VPC is downstream of GTM evidence we don't yet have.
- **[`STRATEGY.md §6.4`](../STRATEGY.md) — savings benchmark.** The gateway *is* the benchmark. Once it's running on a buyer's workload, the savings counterfactual that `analytics-api.md §4.7` already specifies becomes a contract Metis can hold up to a buyer with their own numbers. No synthetic workload needed.
- **[`STRATEGY.md §6.5`](../STRATEGY.md) — context-assembler design.** Deferred behind the gateway. The biggest cost lever is unreachable from the gateway form factor, which means the spec design can wait until the gateway has proven that buyers want more savings beyond what model selection alone delivers. Context-assembler becomes the "Metis Pro" anchor feature.
- **[`STRATEGY.md §6.8`](../STRATEGY.md) — pricing model.** Gateway → likely per-seat *or* % of savings (the gateway's per-key cost data makes savings-share contracts measurable for the first time). Agent → flat per-seat. The hybrid supports both, with the gateway as the on-ramp.

---

## 7. Out of scope for this spec

- The gateway's HTTP surface and translation rules (drafted in [`gateway.md`](gateway.md)).
- Whether the gateway runs as SaaS, in-VPC, or on-prem (deferred behind GTM evidence).
- The agent's roadmap re-prioritization after the gateway lands (will follow once gateway is shipped and instrumented).
- Pricing model for the product itself (depends on the §6.8 question, not this spec).

---

## 8. Open questions the owner should resolve before this lands

1. **Gateway-key model.** Single-tenant (one key per deployed Metis instance) vs. multi-tenant from day one. The spec assumes single-tenant; multi-tenant is a non-trivial bolt-on (RBAC, tenant isolation in the trace store) and probably wants its own design pass.
2. **Inbound surface scope.** OpenAI-shape only for MVP, or OpenAI-shape + Anthropic-shape (`/v1/messages`) together? The latter eats 1–2 extra weeks but is the surface every Claude Code user needs.
3. **Naming.** "Metis Gateway" vs. "Metis Proxy" vs. something else. Affects positioning; defer until product copy starts being written.
4. **Where does the gateway run.** Same process as `metis serve` (one binary, two surfaces) or separate `apps/gateway/`? Recommendation: separate package, can share `metis-core`, but operationally distinct because the security/threat model is very different (gateway is a public-ish surface; the current `metis serve` is loopback-only).

---

## 9. Sign-off

This spec is the recommendation only. It does not retire [`STRATEGY.md §6.1`](../STRATEGY.md) or land any STRATEGY.md edits. Those follow on owner sign-off.

When signed off, the STRATEGY.md edits queued are:

- **§5** — new dated entry: *"2026-05-13 — Adopt hybrid deployment (gateway first → agent upgrade). See [`deployment-shape.md`](specs/deployment-shape.md)."*
- **§6.1** — retire the question; add: *"Resolved 2026-05-13: hybrid. See [`deployment-shape.md`](specs/deployment-shape.md)."*
- **§6.3** — annotate: *"Constrained by §6.1 (hybrid): gateway-first implies deployed-instance posture, not laptop-local. Choice between SaaS vs. in-VPC remains open."*
