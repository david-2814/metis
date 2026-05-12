# Strategy

**Last updated:** 2026-05-12
**Status:** Working document. Strategic decisions and open questions that aren't visible from the code or the technical specs. Update when a decision lands.

This doc captures the **why** behind the project — the kind of context an AI agent walking into the codebase cold can't infer from `docs/project-overview.md` (which describes the *shape* of the system) or the per-component specs (which describe the *contracts*). Read this before recommending scope changes, priority shifts, or architectural pivots.

---

## 1. The thesis

**Metis optimizes a buyer's LLM usage cost.** The wedge is doing it through three levers, applied together:

1. **Model selection** — pick the cheapest model that can do the task well; route the rest to bigger models.
2. **Context engineering** — keep prompts lean (prompt-cache discipline, history pruning, skill lazy-loading, stable tool-def prefixes). This is the largest typical lever; cache reads at 0.1× vs cache writes at 1.25× can move bills 5–10× on long sessions.
3. **Skills** — load expert instructions on demand (`agentskills.io`-compatible). Smaller models can do focused work when given a focused prompt. Progressive disclosure (~100 token metadata until activated) means most skills cost ~nothing until needed.

The order of impact on a typical workload is **context > skills > model selection**. The current implementation has the inverse priority — routing is most built, skills exist as a Phase 2 wedge, the context-assembler is still architectural-diagram-only. This is a known mismatch to resolve.

## 2. Buyer ≠ user

**The buyer is the budget owner — an engineering leader or CTO. The user is the dev who runs `metis chat` (or whatever the eventual surface is).**

Confirmed 2026-05-12: *"the buyer's AI usage cost."*

This is a B2B product, not a personal tool. Consequences:

- **Multi-user from day one is real**, not optional. The HTTP/WS surface that's already shipping is load-bearing for the buyer story.
- **Team-level cost attribution** matters. Per-dev, per-project, per-task-class rollups need to land before any GTM conversation.
- **Policy enforcement, not just policy explanation.** The routing engine today is built for *explainability to the user* (full chain trace per turn). A buyer wants *enforcement* — "no one in marketing can use Opus" — which is a different mode the routing engine doesn't natively support.
- **Audit and compliance posture.** Trace events are the raw material; aggregation/retention/redaction policies for buyer-facing artifacts are not yet designed.
- **Deployment story.** `uv run metis serve` on a dev's laptop isn't the install. The product needs a server-in-a-box (Docker, helm, or SaaS) — TBD which.
- **Proof of savings.** This is the artifact that closes the deal. No benchmark workload or before/after measurement exists yet. **This is currently the biggest gap between "the architecture should work" and "we can show it works."**

What doesn't change: local-first as a *deployment* property (their infra, their keys, their data) is still a feature. But "local-first by default" as a *user* property doesn't apply to the buyer.

## 3. The open architectural fork

**Replacement agent vs. transparent gateway.** This is unresolved. The current build is closer to the first; the market dynamics favor the second.

| Shape | What devs see | Where Metis sits | Adoption friction |
|---|---|---|---|
| **Replacement agent** (current direction) | New CLI / TUI / desktop app; devs switch from Claude Code / Cursor | Inside the agent loop — owns routing, context, tools | **Very high.** B2B dev-tool history says "make your devs switch tools" is the #1 reason buys don't land. |
| **Transparent gateway** (LiteLLM / Portkey / Helicone shape) | Nothing — devs keep using their existing tools | In front of API keys; intercepts HTTP, routes, caches | **Very low.** Buyer flips an env var; no dev workflow change. |

Trade-offs:

- Replacement-agent ceiling is higher: owning context + skills + memory enables deeper savings (the three-lever story works fully). Lower-floor sale: the savings story only materializes after the user gets value from the loop.
- Gateway ceiling is lower: can route and cache but can't shape context or load skills inside someone else's agent. Higher-floor sale: drop us in, save 30%, no workflow change.
- A hybrid — ship the gateway first for fast adoption + measurable savings, then upsell the agent for deeper savings — keeps both options on the table.

**Decision needed before Phase 3.** The replacement-agent path needs polish (TUI, docs, onboarding). The gateway path needs an HTTP proxy layer that doesn't exist. Doing both doubles the surface area.

## 4. Competitive position

Per `docs/market-research/synthesis.md` (verified 2026-05-09):

- **Multi-provider + cost tracking + server/client split + Ollama** are *table stakes*, not differentiators. OpenCode (157k★), Claude Code (122k★), Cline (62k★), Goose (45k★), Aider (45k★) all do most of this.
- **Defensible wedge** is the trio of:
  1. Bounded agent-curated memory (Letta is the only Series-A peer; everyone else uses unbounded vector slop)
  2. Lossless canonical message format (LiteLLM has bug-of-the-week on this surface)
  3. Task-fingerprint pattern learning + auto-derived skills (no one ships this)
- **Cost optimization is the metric; learning is the mechanism.** The headline isn't "smart routing for cost." The headline is "the agent that gets cheaper the longer you use it because it learns your workload" — savings as the *outcome* of the differentiating mechanics.

Risks:
- **Vercel AI SDK** shipping an Agent abstraction is the most credible "ate Metis's lunch" candidate.
- **Cursor / Claude Code / Copilot** can ship local-first equivalents of bounded memory in a quarter.

Implication: the moat is execution speed + opinionated defaults + the FTS5/fingerprint stack working together. Not any single piece.

## 5. Strategic decisions made

| Date | Decision | Rationale |
|---|---|---|
| 2026-05-09 | Don't depend on LiteLLM for canonical IR | Live bug list on the exact surfaces (tool_use, cache_control, thinking) Metis treats as load-bearing. |
| 2026-05-09 | Adopt agentskills.io as the skill format | Verified open standard, Anthropic-originated, ~35 implementers including OpenAI / Google / GitHub / JetBrains. |
| 2026-05-09 | Letta is the reference for bounded memory | Series-A funded peer with the same "eviction is a feature" stance. Don't reinvent. |
| 2026-05-11 | Pull OpenAI + OpenRouter forward from Phase 2/3 to Phase 1 | Substitutability story is unprovable with one adapter; OpenRouter brings the long-tail catalog cheaply. |
| 2026-05-12 | Buyer ≠ user; B2B framing | Pricing and surface decisions follow from this. Multi-user from day one is non-negotiable. |

## 6. Open questions (decisions deferred)

These are **live**. AI agents working in the repo should not unilaterally close them — surface to the owner.

1. **Replacement agent vs. gateway** (or both). See §3.
2. **Buyer profile.** 20-dev startup CTO vs. 200-dev enterprise eng leader want very different products (the latter wants SOC2/governance/audit). Anchoring on one narrows the build. Current default lean: startup-CTO first.
3. **Local-first vs. SaaS deployment.** Local-first is a feature for individuals; many B2B buyers actively prefer SaaS (one bill, one vendor relationship, no infra). The commitment costs the easiest GTM path. Worth deciding consciously.
4. **Savings benchmark.** No defined workload + cost baseline + measurement methodology exists. Until one does, "30–50% savings" is a number we'd be guessing.
5. **Context-assembler design.** The biggest cost lever (per §1) has no spec. What's the algorithm for: skill loading (description-match vs activation), history compression vs drop, prompt-cache breakpoint placement, behavior near the context window? Each has direct $$ consequences.
6. **Pattern store mechanics.** Routing-engine §5.5 references it; no spec exists. Without it, the "learned routing" leg is interface-only.
7. **Evaluator scope.** Architecture mentions it; no spec. This is the feedback loop that *proves* savings — without it, "is the system actually saving money vs naive sonnet-everywhere?" stays an open question forever.
8. **Pricing model for the product itself.** Per-seat? % of savings? Free + paid features? Tied to deployment shape from §3.

## 7. What changes about the build if §3 lands one way or the other

**If replacement agent wins:**

- Pull skills / memory / context assembler forward; they're the differentiated value.
- Invest in TUI / desktop app / onboarding polish.
- The HTTP/WS surface becomes the device-portability story (multiple clients per user).
- The savings story takes weeks-of-use to materialize. Sales cycle is longer; ACV can be higher.

**If gateway wins:**

- New module: HTTP proxy layer that translates between OpenAI-shape inbound (everything speaks it) and provider-native via the existing adapter set.
- Skills / memory / context assembler are deferred or repurposed (can't shape context inside someone else's agent).
- Cost dashboards become the product surface — the TUI/CLI is internal-tooling-only.
- Sales cycle is hours, not weeks. Lower per-account value, much faster growth.

**If hybrid:**

- Build the gateway first; ship the cost dashboard; sell the savings story.
- The agent layer becomes "Metis Pro" — upgrade path for buyers who already see the savings and want more.
- Highest engineering cost; highest optionality.

---

## How to use this document

- **AI agent walking into the repo:** read this after `AGENTS.md` and before `docs/project-overview.md`. Understand what's a settled design vs. an open strategic question.
- **Working on scope-affecting changes:** check §6. If your change presupposes an answer to an open question, surface it.
- **Adding a major feature:** update §5 with the decision and rationale; if it changes the answer to a §6 question, retire the question.
