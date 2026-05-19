# Pricing Specification

**Status:** Ratified — §5.5.4 open-core gateway + per-seat Pro + enterprise %-of-savings add-on (2026-05-16)
**Last updated:** 2026-05-16

> Frames the commercial pricing model for the Metis product itself: gateway access, the analytics dashboard, multi-user identity, the upgrade-tier agent. Surveys the candidate models, names the trade-offs each one creates, and proposes one for the owner to ratify or reject. **The spec lays out the choice; the owner closes the project strategy (private).**
>
> This spec depends on:
>
> - `../the project strategy (private)` — *buyer ≠ user* framing; the buyer is the budget owner.
> - `../the project strategy (private)` — the open question this spec exists to close.
> - [`deployment-shape.md`](deployment-shape.md) — hybrid (gateway-first → agent-upgrade); pricing must accommodate "trial without payment" → "convert to paid."
> - [`multi-user.md §5`](multi-user.md) — per-user / per-team identity layer; pricing must compose with the shipped per-team budget cap primitive.
> - [`analytics-api.md §4.7`](analytics-api.md) — `actual_repriced_usd` / `baseline_repriced_usd` / `savings_pct`; the measurement substrate any "% of savings" model would depend on.
> - [`gateway.md`](gateway.md) — the OSS surface that's the foot-in-the-door.
>
> This spec is **product / commercial design**, not an engineering contract. It does not add wire fields, event types, or HTTP endpoints. The implementation lands only after the buyer signs off on the model — which means none of §5–§7 below should be read as "to-build."

---

## 1. Purpose

Today there is no specced pricing model. Every GTM conversation invents one on the spot. That is fine while the project is one part-time owner with zero paying buyers, but it is the missing piece that blocks the first commercial conversation: the buyer asks "what does this cost?" and the answer needs to be a single sentence backed by a defensible rationale, not a real-time deliberation.

The job of this spec is to:

1. **Survey** the credible pricing models for a cost-optimization product positioned as a hybrid gateway-plus-agent.
2. **Name the trade-offs** each model creates — what it incentivizes Metis to build, what it discourages buyers from doing, who pays when, the billing complexity it implies.
3. **Recommend one** with rationale, while explicitly leaving the decision to the owner. The spec frames the choice; it does not close it.
4. **Pin composability** with the multi-user identity layer ([`multi-user.md §5`](multi-user.md)) and the analytics surface ([`analytics-api.md`](analytics-api.md)) so whichever model wins is *enforceable* via primitives already shipped.

What this spec deliberately does **not** do: settle list prices, name competitors' price points (the project does not yet have the buyer signal to triangulate against), pick a tiering structure for the paid plan (that's downstream of the model choice), or commit Metis to any billing infrastructure (Stripe, metering, invoicing). Those are commercial / operational decisions, not engineering ones.

---

## 2. Scope and goals

### 2.1 Goals

1. **One owner-ratifiable recommendation.** A single proposed model with rationale tight enough that the owner can either accept it ("ship pricing.md as the answer") or reject it in favor of one of the other candidates ("section 5.X is closer to what I want").
2. **Honest trade-off framing.** Every candidate has obvious upsides; this spec names the corresponding *downsides* with the same vigor. The recommendation only earns its slot by losing fewer dimensions than the alternatives, not by winning on every dimension.
3. **Composability with shipped primitives.** The recommended model must be enforceable using the multi-user identity records, the per-team budget caps, the per-key analytics rollups, and the savings-counterfactual already specced. If a model requires a new metering subsystem, that cost surfaces in the rationale.
4. **Compatibility with the hybrid deployment.** A gateway buyer flips an env var with **zero commitment** ([`deployment-shape.md §1`](deployment-shape.md)). Pricing must not break that floor: the path from "trial" to "convert to paid" needs a deliberate point of conversion, not a paywall at the front door.
5. **Buyer-legible billing.** A CTO looking at an invoice should be able to map every line item to a single sentence ("this is what we spent on Metis seats" / "this is what Metis took of our savings") without consulting a glossary. Models that fail this test cost the sale.

### 2.2 Non-goals

1. **Price points.** $/seat, $/call, %-of-savings thresholds — the spec frames the dimensions; the numbers come from market evidence (competitor benchmarks, first buyer conversations) the project does not yet have.
2. **Billing infrastructure.** Stripe vs. Lago vs. invoice-by-PDF, currency support, tax handling, dunning, prorating, annual-vs-monthly: all downstream of model choice.
3. **Contract / SLA structure.** Service credits, uptime guarantees, support tiers: tied to the deployment-posture choice in the project strategy (private) which is still open.
4. **Channel pricing.** Reseller / partner / OEM pricing, marketplace listings (AWS / GCP / Azure): zero v1 evidence anything will use these.
5. **Open-source license choice.** Apache 2.0 vs. BUSL vs. AGPL is a separate decision, made when the OSS surface is published. The spec assumes "permissive enough that a CTO does not need to read it"; the actual license is the owner's call.

---

## 3. Background: what is being priced

Metis sells three artifacts to the same buyer:

1. **The gateway** — a transparent HTTP proxy that any OpenAI- or Anthropic-shape client can speak ([`gateway.md`](gateway.md)). Provides routing, lossless canonical IR, cost attribution, and per-(key / user / team) rollups. Per-request stateless. This is the foot-in-the-door (§4.1).
2. **The agent** — the replacement coding agent (CLI / TUI / future desktop) that owns context assembly, skill loading, bounded memory, and learned routing (the project strategy (private), "If hybrid"). High-ceiling cost lever; takes weeks of use to materialize. This is the upgrade (§4.2).
3. **The dashboard** — the analytics surface ([`analytics-api.md`](analytics-api.md)) that turns trace events into per-(user / team / model / inbound shape) cost rollups, cache-effectiveness panels, the savings counterfactual. The *artifact that closes the deal* per the project strategy (private). This is the *evidence* the gateway tier surfaces — the thing that makes the upgrade worth buying.

These three are **one product** from the buyer's perspective ("Metis"), priced under one umbrella. The pricing model is what determines which of the three each tier actually charges for.

### 3.1 What Metis does NOT bill for

Metis does **not** meter or resell provider tokens. The buyer brings their own provider API key (BYO-keys per [`gateway.md §2`](gateway.md)); Metis routes through it, but the bill for the LLM call itself goes from Anthropic / OpenAI / OpenRouter to the buyer directly. There is no transit margin. This is load-bearing for §5.2 below: a per-call Metis price stacks on top of, not in place of, the provider bill.

### 3.2 The cost lever Metis sells

Per the project strategy (private), three levers compose the savings:

1. **Context engineering** (largest; cache discipline, history pruning) — reachable only from the agent.
2. **Skills** (medium; lazy-loaded expert prompts) — reachable only from the agent.
3. **Model selection** (smallest in raw size, but the most measurable) — reachable from the gateway *and* the agent.

The gateway is the cheaper lever in the hybrid. The agent is the deeper lever. The pricing model has to make sense for both forms of value — otherwise the gateway-to-agent upsell breaks at the price tag.

---

## 4. Constraints

### 4.1 The "trial without payment" floor (from deployment-shape.md §1)

The whole point of the gateway is that a buyer flips one env var and savings show up on their dashboard within hours, **before** they have signed a contract. This requires a usable free tier — paywalling the gateway at request #1 kills the GTM motion.

The pricing model **must** accept "buyer evaluates Metis for some bounded period at $0" as a first-class state. The exit from that state (to a paid plan) is the *conversion event* the rest of pricing exists to motivate. The natural conversion triggers are:

- **Team size crosses a threshold** (e.g. 6+ users on one keystore). Solo / pair use stays free; team use pays.
- **Quota usage crosses a threshold** (e.g. >$X/month of routed cost). Light use stays free; heavy use pays.
- **Paid feature is required** (multi-user identity, hard caps, audit log export, the agent tier). Buyer opts in when they need the feature, not before.
- **Hosted SaaS posture** (vs. self-hosted). Operationally outsourcing the deploy crosses the line.

Any of these can be the conversion trigger; the model choice in §5 picks one or composes more than one.

### 4.2 The buyer-is-not-the-user constraint (from the project strategy (private))

The buyer (CTO, eng leader) does not invoice themselves for the dev tool the devs use. The buyer pays a vendor invoice and expects:

- **Predictability.** "What is my Metis bill going to be next month?" should have a tight upper bound. Pure usage-based pricing fails this.
- **Attribution.** "Why was last month's bill $X?" — the buyer wants to see the bill broken down by team / project / user. The multi-user identity layer ([`multi-user.md §5`](multi-user.md)) is the mechanism; the pricing model has to align its *unit* with that mechanism so the rollup answers the right question.
- **Single bill, single vendor.** Buyers strongly prefer one Metis line item to N. This biases against models that produce per-team or per-project sub-bills unless they're rolled up.

The user — the dev running Claude Code through the gateway — has different preferences ("don't interrupt me with a quota wall mid-debug-session"), and the routing-rule soft caps from [`multi-user.md §6`](multi-user.md) are the mechanism that lets the buyer's caps land softly on the user. Pricing should not require the buyer to explain billing to every dev; the gateway abstracts it.

### 4.3 The shipped primitives that pricing must compose with (from multi-user.md §5)

The identity layer ships with:

- **Per-user / per-team / per-key rollups** — `cost_usd`, `input_tokens`, `output_tokens`, `cached_input_tokens`, `cache_creation_input_tokens`, `call_count` per dimension per window ([`multi-user.md §5.2`](multi-user.md)).
- **Per-team hard caps** (`daily_cap_usd`, `monthly_cap_usd`) enforced at the gateway boundary ([`multi-user.md §6.3`](multi-user.md)). Request short-circuits with 429 + a typed scope.
- **Per-team soft caps** as routing-engine predicates (`team_cost_today_exceeds_usd`, `team_cost_month_exceeds_usd`) that redirect to cheaper models ([`multi-user.md §6.1`](multi-user.md)).
- **Savings counterfactual** (`actual_repriced_usd` / `baseline_repriced_usd` per window) projected from the same trace store ([`analytics-api.md §4.7`](analytics-api.md)).

Every candidate in §5 is measured against "does this compose with the primitives above without inventing new ones?" Models that require new metering surfaces lose the composability test.

### 4.4 The "first paying buyer is a startup-CTO" constraint (from the project strategy (private))

The default buyer profile is a 10–50-dev startup CTO, not a 200-dev enterprise eng leader. This profile:

- Has a credit card, not a procurement department. Wants a self-serve flow.
- Is price-sensitive in absolute dollars (sub-$5k/month total is the easy buy; $50k/year requires a conversation).
- Cares about *predictability over precision* — would rather pay a slightly higher fixed amount than a slightly lower variable amount that's hard to model.
- Does not yet need SOC2, audit log export, or RBAC; will need them later, at which point the price tier changes.

A model that targets this profile will look different from one designed for the enterprise. The recommendation in §7 picks the startup-CTO target; the enterprise tier is a follow-on once buyer evidence accumulates.

---

## 5. Pricing model survey

Each candidate is evaluated across six dimensions:

1. **What it charges for** — the unit of metering.
2. **Incentive alignment** — what the model rewards Metis for building and the buyer for doing.
3. **Buyer friction at first contact** — how hard is it to commit?
4. **Buyer friction at scale** — how predictable / defensible is the bill at $50k/yr?
5. **Composability with shipped primitives** — does the multi-user identity layer enforce it?
6. **Billing complexity** — how much non-product surface must Metis build to support it?

### 5.1 Per-seat (developer count × monthly fee)

**Unit:** active users per month. An "active user" is a `usr_<ulid>` whose keys made at least one `llm.call_completed` in the billing window (definition from [`multi-user.md §3.1`](multi-user.md)). The keystore already enumerates users.

**Incentive alignment:**

- **Rewards Metis** for adding seat-count over usage-per-seat. Metis is indifferent to whether a dev sends 10 or 10,000 requests/day.
- **Rewards buyer** to use Metis heavily on the seats they've already bought — extracting more savings per seat compounds the ROI.
- **Punishes neither side** for legitimate growth: more devs → more revenue is honest.

**Buyer friction at first contact:**

- **Predictable** — the buyer knows the bill the moment they count seats. This is the *most* friction-free model for a CTO in a procurement conversation.
- **Easy to model** — same shape as Slack, Linear, GitHub, Cursor. Buyer has zero learning curve.

**Buyer friction at scale:**

- **Decoupled from value.** A team that goes from $1k/mo of LLM spend to $50k/mo of LLM spend (savings opportunity 50×) pays Metis the same dollar amount. The buyer's CFO will eventually notice that Metis is delivering 50× more savings for 1× the price — which is *good for Metis* but feels like leaving money on the table.
- **Conversely** — the buyer might churn cheap seats (devs who don't use Metis heavily) to drive down the bill, even though those seats might still be netting savings the dashboard reports.

**Composability:** Excellent. The shipped `/analytics/by_user` rollup already counts active users per window; gating "active" on at least one event in the window is a single SQL predicate. The team / user records *are* the seat list.

**Billing complexity:** Lowest of the four. Monthly count + multiply by rate; the metering is already specced.

**Net:** Per-seat is the boring, defensible default. It is the model the buyer most easily accepts and the model the eng team least has to build for. Its weakness is that it does not visibly couple Metis's revenue to the savings Metis delivers — a problem only at the high-spend end.

### 5.2 Per-call (API surface charge per LLM call routed)

**Unit:** `llm.call_completed` events per billing window, optionally weighted by tokens. Anchored to the same event the analytics surface aggregates against; metering is the call count Metis already records.

**Incentive alignment:**

- **Rewards Metis** for every LLM call that passes through the gateway. Direct usage incentive: more devs → more calls → more revenue. Also rewards Metis for *not* aggressively caching, because cache hits reduce upstream calls but not gateway-hit count (cache hits still produce a `llm.call_completed`; cost is low but call still counted).
- **Rewards buyer** to route fewer calls (the opposite of what we want — Metis wants more devs sending more requests).
- **Cross-cuts the savings story.** Metis can save the buyer money on every call *and* charge them per call. The two are technically orthogonal.

**Buyer friction at first contact:**

- **Optical problem.** The buyer reads "$0.0001/call" and *immediately* thinks "I already pay Anthropic per call; now I pay you too?" Even if the math works out — Metis saves $0.01/call while charging $0.0001 — the buyer's intuition has been trained by twenty years of API-pricing wars to treat per-call fees as extractive.
- **Unpredictable.** Buyer cannot bound the bill without modeling call volume, which they have not measured. The first month of a paid plan becomes an experiment, not a budget line.

**Buyer friction at scale:**

- **Scales with usage in the *worst* way.** As the team grows and integrates Metis more deeply, the bill climbs without any flattening. At $50k/yr the buyer is suspicious; at $500k/yr the buyer is shopping for an alternative.
- **Rewards finance-led optimization over engineering-led optimization** — the CTO ends up writing a script to consolidate calls, which is the opposite of the "frictionless cost optimization" Metis is selling.

**Composability:** Excellent. `call_count` is already the headline metric on every analytics rollup. Per-key call counts already work; per-user / per-team counts ship with multi-user.md.

**Billing complexity:** Higher than per-seat — requires daily metering pipeline, idempotency on call retries, refund handling for failed-but-billed calls (do you bill a 5xx? a 429? edge cases multiply).

**Net:** Per-call is the model with the worst optics. It is technically the easiest to instrument (because Metis already counts calls) and the most usage-coupled, but it asks the buyer to swallow a tax shape they have learned to refuse. **Not recommended** as the primary surface, though it has a narrow role inside hybrid models (§5.5).

### 5.3 Percentage of savings (X% of baseline-minus-actual)

**Unit:** `(baseline_repriced_usd - actual_repriced_usd)` per billing window, where the two values are projected via [`analytics-api.md §4.7`](analytics-api.md)'s shipped savings endpoint. Metis charges Y% of that delta.

**Incentive alignment:**

- **Rewards Metis** for delivering real savings — and *only* for delivering real savings. If Metis fails to route correctly, the buyer pays nothing. This is the strongest alignment any model on this list achieves; it is the model an outcome-driven sales motion would *want*.
- **Rewards buyer** for routing more traffic through Metis — the more they use it, the more they save, the more the savings deepen the relationship. There is no "save less to pay less" perverse incentive.
- **Aligns both sides on the analytics dashboard** — the bill *is* the dashboard. The buyer doesn't need to be convinced the savings are real; they're reading the savings off the same page they're paying for.

**Buyer friction at first contact:**

- **Auditability is the question.** "Did Metis actually save me $X?" — every buyer asks this. The savings calculation depends on a counterfactual (the `baseline_repriced_usd` is what the buyer *would have* spent if every routed turn ran on the baseline model). The counterfactual is reproducible from the trace store, but the buyer has to *trust* it. This is the friction-at-contract-time analog of per-call's friction-at-pricing-time problem.
- **Dispute risk.** What happens when the buyer says "your $5k savings number is actually $3k because some of those turns wouldn't have happened at all without Metis"? Now Metis is litigating the counterfactual instead of selling the next feature.
- **Low entry friction in the happy case** — buyer flips the env var, watches the dashboard, sees savings, agrees to share Y% of them. The model is exactly aligned with the gateway-first GTM motion when audit happens.

**Buyer friction at scale:**

- **Best alignment of any model.** A buyer saving $50k/month gladly pays Metis $5k of that (10%) — the math is self-evident. The model gets *easier* to defend as the dollar amounts grow.
- **But:** the buyer's CFO eventually notices the % shape and asks for a cap — "we don't want to pay you more than $X regardless of savings." This collapses the model toward a per-seat-with-cap hybrid (§5.5).

**Composability:** Strong but requires the counterfactual to be *contractually trusted*, not just technically computed. The `AnalyticsStore.savings()` method already produces the delta; the per-team filter from `multi-user.md §5.4` makes the cut sensible. What's *missing* in v1: an audit-export surface that captures the counterfactual in a tamper-evident form (specced as a follow-on in [`multi-user.md §7.3`](multi-user.md)). Without that surface, every percentage-of-savings billing dispute is a manual reconciliation.

**Billing complexity:** Highest. Requires (a) a contractually frozen counterfactual definition the buyer signed off on, (b) an export surface that produces the same number every time it's run (re-priceability per [`canonical-message-format.md §6.4`](canonical-message-format.md)'s `pricing_version` is load-bearing here), (c) credit / dispute handling, (d) a way to apportion savings across teams when one buyer has multiple teams under one contract.

**Net:** Best alignment, highest auditability cost. Most viable as a **secondary** model (§5.5 hybrid) once a buyer trusts Metis's numbers from a primary per-seat relationship, or as the *enterprise* tier where contract velocity is slower and the audit overhead is acceptable.

### 5.4 Free + paid tiers (open-core)

**Unit:** binary — buyer is on the free tier (gateway + basic dashboard) or the paid tier (everything else). Within the paid tier, secondary pricing is one of the three models above.

**Incentive alignment:**

- **Rewards Metis** for distribution. A free OSS gateway is the strongest possible adoption signal — buyers do not need to be sold; they find Metis. Aligns with the gateway-first GTM motion from [`deployment-shape.md §5.2`](deployment-shape.md).
- **Rewards buyer** to try Metis with zero commitment, then upgrade only when they need the feature gate. This is the cleanest possible "trial without payment" path.

**Buyer friction at first contact:**

- **Lowest of any model.** The gateway is OSS; the buyer installs it without a credit card. By the time they're paying, they're already getting value.
- **Upgrade is opt-in** — buyer chooses when to cross the line. The line itself becomes the marketing surface (which features are paid is the most important product decision in the company).

**Buyer friction at scale:**

- **Depends entirely on the line.** If the OSS surface is large enough to be useful but lacks the features a team actually needs (multi-user identity, audit, hosted dashboard, the agent tier), the upgrade is natural. If the OSS surface is *too* generous, no one upgrades and Metis has built a free product. If too thin, no one adopts and Metis has built nothing.
- **Drawing the line is the load-bearing decision.** §7 proposes a specific cut.

**Composability:** Orthogonal — open-core composes with any of §5.1 / §5.2 / §5.3 as the paid-tier pricing model. It is *meta* to the others, not a competitor to them. The paid-tier model picks among the three.

**Billing complexity:** Adds the OSS-distribution surface (license, contributor docs, release engineering) but does not itself add billing surface; that's still whichever model §5.1–§5.3 the paid tier uses.

**Net:** Open-core is not a pricing model — it is the *shape* the model wraps around. The recommendation in §7 takes open-core as foundational and answers the "what does the paid tier charge for?" question separately.

### 5.5 Hybrid combinations

The three primary models (§5.1 / §5.2 / §5.3) compose in three credible shapes:

#### 5.5.1 Per-seat with usage-cap overage (per-seat + per-call)

- **Shape.** $X/seat/month bundles N calls; over the bundle, $Y/1000 calls.
- **Pros.** Predictable for the buyer's normal-use baseline; couples Metis revenue to the heavy-usage tail without the per-call friction at first contact.
- **Cons.** Two-axis pricing is harder for the buyer to model. Asks the buyer to predict not just headcount but also usage-per-head. Bundle sizing is finicky.
- **Where it lands well.** Enterprise tier where the contract negotiation can name the bundle. Not the right first-paid-plan shape for a startup CTO.

#### 5.5.2 Per-seat with savings cap (per-seat + capped %-of-savings)

- **Shape.** $X/seat/month for the platform; Metis additionally takes up to Y% of measurable savings, capped at $Z/month.
- **Pros.** Captures the value-delivery story for heavy-savings teams while keeping the predictable per-seat shape as the floor.
- **Cons.** Still requires the savings counterfactual to be contractually trusted. The cap protects the buyer from runaway billing but doesn't protect Metis from billing-dispute cycles.
- **Where it lands well.** Mid-market — buyers who like the per-seat predictability but want their vendor's incentives aligned to outcomes.

#### 5.5.3 Open-core gateway + paid Pro features (open-core + per-seat for paid tier)

- **Shape.** Gateway (routing, dashboard basics) is OSS. The "Pro" tier (multi-user identity, hard caps, audit log export, hosted dashboard, the agent) is $X/seat/month.
- **Pros.** Lowest possible adoption friction (OSS gateway); aligned with the deployment-shape hybrid; per-seat pricing for the paid tier is the predictable shape buyers prefer; the *thing being upgraded to* maps cleanly to the multi-user identity layer that's already shipped.
- **Cons.** Demands a credible line between OSS and paid. The owner's commercial judgment on that line is the load-bearing decision.
- **Where it lands well.** The startup-CTO default. The recommendation in §7 is this shape.

#### 5.5.4 Open-core gateway + paid Pro features + %-of-savings enterprise add-on

- **Shape.** §5.5.3 plus an enterprise tier that adds a %-of-savings line for buyers who want their bill scaled to outcomes (and have procurement to handle the contracting).
- **Pros.** Future-proofs the model for the enterprise buyer the startup CTO becomes after a Series B. Doesn't force the audit surface to be built today; %-of-savings is gated behind "we have at least one enterprise prospect asking for it."
- **Cons.** Marketing must explain three tiers (Free / Pro / Enterprise) without losing the "this is one product" message. Buyer-segment confusion is the failure mode.

---

## 6. Comparison matrix

| Model | First-contact friction | At-scale predictability | Incentive alignment | Composability | Billing complexity | Best fit |
|-------|----------------------|------------------------|---------------------|---------------|---------------------|----------|
| **Per-seat (§5.1)** | Very low | High | Medium (rewards seat-count, not usage-coupled) | Excellent | Low | Default; startup CTO |
| **Per-call (§5.2)** | High (optical) | Low | Strong on usage, but punishes buyer for using more | Excellent | Medium | Not recommended primary |
| **% of savings (§5.3)** | Medium (auditability) | Medium (scales with savings; needs cap) | Strongest (revenue iff value delivered) | Strong w/ counterfactual | Highest | Secondary; enterprise tier |
| **Open-core (§5.4)** | Lowest possible (OSS) | N/A (meta) | High (distribution) | Orthogonal | Wraps the paid-tier model | Foundational shape |
| **Per-seat + per-call cap (§5.5.1)** | Medium | High | Medium-high | Excellent | Medium | Enterprise mid-market |
| **Per-seat + capped %-of-savings (§5.5.2)** | Medium | High | High | Strong | High | Mid-market |
| **Open-core + per-seat Pro (§5.5.3)** | Very low | High | Medium-high | Excellent | Low–medium | **Recommended — startup CTO default** |
| **Open-core + per-seat Pro + % enterprise add-on (§5.5.4)** | Very low | High | High | Excellent | Medium (Enterprise tier only) | Recommended evolution |

The matrix's load-bearing rows are the bottom two: open-core + per-seat for the v1 commercial offer, with the enterprise %-of-savings add-on reserved for when the buyer evidence supports building the audit surface.

---

## 7. Recommendation

**Adopt §5.5.3: open-core gateway + per-seat paid tier, with §5.5.4's enterprise add-on reserved as the upgrade path.**

The proposed structure is:

### 7.1 Free tier — "Metis Community"

**Price:** $0.

**What's included:**

- Self-hosted gateway (`apps/gateway/`) — full routing, lossless canonical IR, sync + SSE on both OpenAI- and Anthropic-shapes, all shipped per-key analytics.
- The shipped CLI / TUI agent surface (`metis chat`, `metis tui`) — self-hosted, single-user.
- Bounded memory, pattern store, evaluator (heuristic tier), single-user-mode analytics dashboard.
- Source code under a permissive license (the specific license is the owner's call; the spec assumes "open enough that a CTO does not need legal review to install").

**What's not included:**

- **Multi-user identity** (`User` / `Team` records, per-user analytics, per-team caps). This is the headline paid feature.
- **Hosted SaaS posture** — buyers wanting Metis-as-SaaS upgrade to Pro.
- **LLM-judge evaluator tier** — the heuristic judge ships free; the hybrid / LLM tiers are Pro.
- **Audit log export** (the surface sketched in [`multi-user.md §7.3`](multi-user.md)) — Pro.

**Why this slice:** the free tier must be *usable* — a CTO running it on their laptop or a small VPC must see the savings dashboard work on their own data. A gateway without the dashboard is a curiosity, not a tool. But the free tier must not include the things teams *need* to deploy at team scale: identity, caps, audit, hosted operations. Drawing the line at "single-user works for free; team use upgrades" matches the startup-CTO conversion trigger from §4.1.

### 7.2 Paid tier — "Metis Pro"

**Price:** per-seat, $X/active-user/month. (Number is the commercial decision; spec frames the model.)

**Definition of "active user":** a `usr_<ulid>` whose gateway keys produced at least one `llm.call_completed` in the billing window. This composes with the shipped `/analytics/by_user` rollup directly — no new metering needed. Users with zero calls in the window are not billed; this protects the buyer from paying for stale accounts.

**What's included beyond Free:**

- **Multi-user identity layer in full** — `users.json` / `teams.json`, per-user / per-team analytics, hard caps, soft-cap routing predicates ([`multi-user.md §5` / §6](multi-user.md)).
- **LLM-judge evaluator tier** — hybrid and LLM judges, `/analytics/quality` dashboard.
- **Hosted SaaS option** — Metis-operated deployment; the buyer brings their provider keys, Metis operates the gateway / dashboard.
- **Audit log export** — when shipped per [`multi-user.md §7.3`](multi-user.md); Pro-tier feature out of the gate.
- **Replacement agent tier** — when shipped per the project strategy (private) "If hybrid"; the `metis chat`-and-beyond surface with skills, context-assembler, memory, learned routing.

**Why per-seat for Pro:**

- **Aligns with the buyer's mental model.** The buyer signs a SaaS-shaped contract with a SaaS-shaped bill.
- **Composes with shipped primitives.** The user identity layer *is* the seat list. Adding new pricing surface is zero; the rollup endpoint is already specced.
- **Avoids the optical problem of §5.2.** Buyer is not paying per call. Buyer is paying for *seats that get the value* of Metis.
- **Predictable at scale.** Bill = seat count × rate. CFO can model it on a napkin.
- **Disconnect from usage is intentional.** The free tier handles "I want to see if this works"; the paid tier handles "I want my team to be on this." Per-seat lets the buyer commit at the team boundary without re-negotiating per usage spike.

**Why not per-call for Pro:** §5.2's optical problem. Even at low rates, "you charge me per Anthropic call" is an objection the sales conversation should not have to win.

**Why not %-of-savings as Pro default:** §5.3's auditability burden is too heavy for the v1 paid relationship. The audit-export surface ([`multi-user.md §7.3`](multi-user.md)) is not yet built; building it on the *first* paying contract converts every billing question into a contract negotiation. Reserve it for the enterprise tier where the contract velocity supports the build.

### 7.3 Enterprise tier — "Metis Enterprise" (future)

**Price:** custom contract; baseline is per-seat from §7.2 plus optional %-of-savings add-on.

**What's included beyond Pro:**

- SOC2 / SAML / OIDC / SCIM (the [`multi-user.md §8`](multi-user.md) explicit non-goals become goals at this tier).
- Multi-org tenancy.
- On-prem / VPC support with SLA.
- Reserved-capacity pricing for the savings line (the cap from §5.5.2).
- Custom routing policies, deployment support, dedicated success engineering.

**Why save %-of-savings for here:**

- Audit-export surface is a Pro feature already; enterprise inherits it.
- Contract velocity is slower; the auditability conversation lands inside procurement, not at first contact.
- The CFO of an enterprise buyer *prefers* the outcome-coupled line (justifies the spend internally) — the opposite of the startup CTO who prefers predictability.

This tier is deliberately under-specified in v1. Its existence is more important than its shape; the shape will be the second buyer conversation, not the first.

### 7.4 Why this recommendation in one sentence

**Open-core gateway maximizes adoption (the deployment-shape gain), per-seat Pro pricing maps cleanly to the shipped multi-user identity layer (the composability gain), and reserving %-of-savings for the enterprise tier defers the audit-export surface buildout until a buyer is paying for the surface to exist.**

### 7.5 What this recommendation deliberately does NOT decide

- **The dollar amount of $X/seat/month.** That's market evidence.
- **Where exactly the OSS / Pro line falls feature-by-feature.** §7.1 names the principle; the exact feature list will refine as features ship.
- **Annual vs. monthly billing.** Standard SaaS default is annual-with-monthly-option; the spec inherits that default.
- **The OSS license.** §2.2 named this as a non-goal.
- **Whether the agent tier is included in Pro at launch or is a Pro-plus add-on.** Owner judgment; depends on agent maturity at the time the paid plan goes live.

### 7.6 Surface for human sign-off

This is an **owner-decision item**. The spec frames the choice with rationale; the owner closes it. The two paths forward:

1. **Accept §7** — adopt open-core + per-seat-Pro + future enterprise-tier. Pricing.md becomes the contract; the project strategy (private) closes with a pointer here.
2. **Reject §7** in favor of §5.1 / §5.2 / §5.3 / §5.5.1 / §5.5.2 / §5.5.4 — owner indicates which row of the matrix in §6 they prefer; pricing.md is revised; the project strategy (private) closes when revised.

In either case, **the project strategy (private) stays open until the owner closes it.** This spec adds a pointer ("specced; awaiting commercial decision") but does not retire the question. The act of choosing among §5/§7 is the commercial decision the project does not yet have evidence to automate.

---

## 8. How the recommendation composes with shipped primitives

Tracing through each pricing-relevant surface to confirm no new metering / event / endpoint is needed for §7.2:

### 8.1 Seat count

**Source of truth:** `users.json` enumerates `User` records; gateway keys carry `user_id` ([`multi-user.md §4.1`](multi-user.md)). The "active user in this window" predicate is `EXISTS (SELECT 1 FROM events WHERE payload.user_id = ? AND event_type = 'llm.call_completed' AND ts BETWEEN ? AND ?)` — same shape as the `/analytics/by_user` rollup already executes.

**No new event type needed.** No new HTTP endpoint needed for billing — the analytics surface already returns the user list with `call_count`; billing reads the count of users with `call_count > 0`.

### 8.2 Tier gating

**Source of truth:** a tier flag on the deployment configuration. The simplest shape — out of scope for this spec, named for completeness — is a `pricing_tier` field on the deployment config (`free` / `pro` / `enterprise`) that the analytics-server reads at boot. Pro-gated endpoints return 402 `payment_required` (or 404, owner choice) on a free deployment.

This is **not** a per-request enforcement — it is a deployment-level gate. A Pro deployment is a Pro deployment; a free deployment cannot opt into Pro endpoints without changing the config. This avoids per-call licensing checks (which would themselves be a metering surface).

### 8.3 Hard caps composed with billing

The per-team hard caps from [`multi-user.md §6.3`](multi-user.md) are **operational**, not commercial. A buyer setting `Team.daily_cap_usd = $50` is constraining their *provider spend*, not their Metis bill. The two are independent dimensions: a team can hit its provider cap while still being under its Metis seat allotment, and vice versa.

If the owner wants to add a *Metis-spend cap* (e.g. "auto-downgrade to Free tier if Pro seat count exceeds N"), that's a follow-on. The v1 recommendation keeps the two surfaces separate.

### 8.4 Savings rollup as a Pro feature

The `actual_repriced_usd` / `baseline_repriced_usd` / `savings_pct` endpoint ([`analytics-api.md §4.7`](analytics-api.md)) ships in v1; whether it surfaces on the Free dashboard or only the Pro dashboard is a product decision the spec doesn't close.

- **Argument for Free:** the savings number is the marketing message. Putting it behind a paywall hides Metis's own value prop from the buyer evaluating it.
- **Argument for Pro:** the savings number is the most operationally useful metric for the buyer; reserving it for Pro is a strong upgrade reason.

**Spec position:** the savings counterfactual ships on Free at *per-deployment* granularity (one number for everything routed through this Metis instance). The Pro tier unlocks per-team / per-user / per-time-window slicing of the same number. This way Free buyers see "you saved $X" but cannot answer "who saved what" — which is the buyer question the multi-user identity layer answers.

### 8.5 Future %-of-savings enterprise tier

When §7.3 is built, the surface needed is exactly the [`multi-user.md §7.3`](multi-user.md) audit-export surface plus a contractual freeze on the `pricing_version` field ([`canonical-message-format.md §6.4`](canonical-message-format.md)). The two together produce a re-priceable savings number the buyer can verify against their own records. **No additional metering primitive is needed** — the savings counterfactual already specced is the answer; what's missing is the *export surface and the contract language*, both of which are commercial concerns not engineering ones.

---

## 9. Out of scope for v1

These are deliberately deferred. The list is what differentiates v1 from a complete commercial-operations playbook.

1. **List prices.** $/seat numbers across regions / tiers / annual-vs-monthly. The owner picks these from market evidence.
2. **Discount / volume / partner pricing.** Standard B2B contract knobs; owner negotiates per-deal until a public list emerges.
3. **Billing-infrastructure choice.** Stripe / Lago / Maxio / invoice-by-PDF.
4. **Currency / tax handling.** USD-only v1 is the assumed default; expansion is downstream.
5. ~~**OSS license selection.** Apache-2.0 / BUSL / AGPL / dual-license.~~ **Resolved 2026-05-17 — Apache-2.0 for the OSS substrate; all-rights-reserved for the private `metis-pro` repo.** See §12 decision log and the repo-split plan (private).
6. **Trial-to-paid conversion mechanics.** Self-serve credit card vs. sales-touch; how long trials last; downgrade behavior.
7. **Refund / SLA / credit policy.** SaaS-standard terms — deferred behind the §6.3 SaaS-vs-VPC decision.
8. **Marketplace listings.** AWS / GCP / Azure marketplace pricing surfaces.
9. **Right-to-delete and data-residency pricing implications.** Tied to [`multi-user.md §7.4`](multi-user.md) audit posture; deferred.
10. **Internal cost-of-revenue modeling.** What does Metis's own infrastructure cost per seat, per call, per byte of trace data? Necessary for setting the dollar amount in §7.2; not a spec question.

---

## 10. Open questions

These are **live**. AI agents working in the repo should surface them, not pick.

1. **Where exactly does the OSS / Pro line fall?** §7.1 names the principle ("single-user works free; team use upgrades"); §7.2 names the headline paid features. Each feature is a separate decision: should pattern-store data export be free or Pro? Should the hosted-SaaS path be Pro or its own line? Should the agent tier be Pro-baseline or Pro-plus? Owner makes the call as features ship.
2. **Does the savings number show on the Free dashboard?** §8.4 surfaces the trade-off. Defaulting to "yes, but only deployment-aggregate; Pro unlocks slicing" is the spec's position; owner can rule otherwise.
3. **Is the agent tier (CLI / TUI / future desktop) in Free, Pro, or Pro-plus at launch?** Today the agent is OSS; the question is whether the *upgrade-tier features* (skills v3, context-assembler v2/v3, evaluator hybrid/LLM tiers) are bundled into Pro, sold separately, or kept OSS as a long-tail acquisition channel.
4. **What's the conversion trigger for the first paid plan?** §4.1 enumerates four candidates (team-size threshold / quota threshold / paid-feature requirement / SaaS posture). §7.1's "single-user free, team use Pro" picks the first; the others remain available if §7's recommendation is rejected.
5. **Does Pro pricing differentiate "active users this month" vs. "provisioned seats"?** §7.2 picks the former ("active") to protect the buyer from paying for stale accounts. The trade-off: "active" pricing is harder to forecast (buyer doesn't know who will be active); "provisioned" pricing is easier to forecast but charges for empty seats. Owner ratifies.
6. **What's the Enterprise add-on Y% rate range and where does the cap land?** §5.5.4 / §7.3 reserve this; the dollar amounts come from enterprise-prospect evidence Metis does not yet have.
7. **How does the buyer evaluate Pro without a Pro deployment?** Standard B2B answer: a free trial of Pro features (time-bound). Spec position: the multi-user identity layer can be enabled on a free deployment with a "trial" flag for N days; after, the deployment downgrades to Free behavior or the buyer commits to Pro. Mechanics deferred.
8. **Does the spec need to commit to "no provider-token markup, ever"?** §3.1 names this as today's posture (Metis does not resell tokens). Codifying it as a forever commitment is a marketing-trust gain and a future-revenue-line constraint. Owner picks.
9. **What happens to a deployment that has been on Pro and then lapses?** Does the buyer's `users.json` / `teams.json` get archived? Does Pro tier soft-degrade to Free with rollups still visible? Standard SaaS handling — but the spec doesn't pin the policy.
10. **Does the gateway emit `pricing.event` audit events on tier transitions?** A future event-catalog addition; would let analytics show "deployment moved from Free to Pro on date X." Out of scope for v1 but flagged as a clean extension once the model is ratified.

---

## 11. Invariants

Promises this spec makes that downstream specs and implementation must preserve.

1. **Free tier remains usable single-user without a credit card.** The gateway, the basic dashboard, and the single-user agent surface together must do something useful for a CTO running them on a laptop. If a future feature gate would render the free tier inert, the gate moves to Pro or the feature is rethought.
2. **Pro pricing is per-active-user, not per-call.** §7.2's optical-friction argument means per-call shapes don't sneak in as "small overage fees" later. Per-call is reserved for the enterprise tier's optional usage cap (§5.5.1), not the Pro baseline.
3. **Metis does not resell provider tokens.** §3.1's BYO-keys posture is the v1 commitment; pricing models that imply a transit margin are out of scope unless §10.8 is explicitly closed otherwise.
4. **Pricing surfaces compose with shipped multi-user primitives.** Any pricing change that would require new metering events, new keystore fields, or new HTTP endpoints needs to surface that cost in its rationale — composability isn't free, and the spec's recommendation is partly chosen because it requires zero new primitives.
5. **No per-request licensing check.** Tier gating is deployment-level (§8.2); a Pro feature is enabled or disabled at boot. Per-request "is this user paid?" checks would add latency to every gateway call and turn pricing into an availability dimension; the spec rejects that shape.
6. **Savings counterfactual is reproducible.** Whatever billing surface depends on `actual_repriced_usd` / `baseline_repriced_usd` must use the same `pricing_version`-stamped computation the analytics dashboard shows. Two different numbers for "did we save money" — one for the bill, one for the dashboard — is the failure mode the audit-export surface exists to prevent.
7. **Pricing model choice is the owner's; pricing.md frames it.** This spec does not unilaterally close the project strategy (private). The spec is a recommendation; ratification is a separate act.

---

## 12. Decision log

| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-05-14 | Open-core foundation (free OSS gateway + agent CLI/TUI; Pro for team-scale features) | Maximizes adoption per [`deployment-shape.md`](deployment-shape.md) hybrid; matches the "trial without payment" floor in §4.1. |
| 2026-05-14 | Per-seat as the Pro tier's pricing unit (not per-call, not %-of-savings) | Per-seat is the buyer's expected SaaS shape (§4.4); per-call has the optical problem of §5.2; %-of-savings requires the audit-export surface ([`multi-user.md §7.3`](multi-user.md)) which is not yet built. |
| 2026-05-14 | "Active user" as the seat metering unit, not "provisioned seat" | Protects the buyer from paying for stale accounts; composes directly with the shipped `/analytics/by_user` rollup. Trade-off: harder to forecast. |
| 2026-05-14 | Enterprise tier reserves %-of-savings as an optional add-on, not the baseline | Reserves auditability burden for contracts where procurement velocity supports it. |
| 2026-05-14 | Multi-user identity layer is the headline Pro feature | Maps the conversion trigger ("single-user works free; team use upgrades") directly to the most distinctive paid primitive. |
| 2026-05-14 | Savings counterfactual visible on Free at deployment-aggregate; Pro unlocks slicing | Free buyers see the savings story (marketing); Pro buyers can attribute it (operational). Owner can revisit per §10.2. |
| 2026-05-14 | Pricing.md does not close the project strategy (private) | The spec frames the recommendation; the commercial decision is the owner's. §6.8 stays open with a pointer to this spec until the owner ratifies. |
| 2026-05-17 | OSS license + repo strategy: Apache-2.0 single OSS repo (`metis`); private `metis-pro` repo for paid-tier code | Closes §9.5 (was "out of scope"). Two-repo "thin Pro repo" pattern: the OSS substrate is genuinely standalone-usable (gateway + canonical IR + adapters + routing + pattern store + bounded memory + tools + skills + heuristic evaluator + per-key analytics + agent CLI/TUI). `metis-pro` holds the operationally-sensitive surfaces (billing, signup, accounts store, hosted dashboard UI, curated LLM-judge rubric library, enterprise SAML/OIDC/SCIM glue). OSS defines extension Protocols (`BillingBackend`, `SignupBackend`, `AnalyticsExtension`, `JudgeRubricProvider`) with noop defaults; Pro overlays implement them at boot. Apache-2.0 chosen over BUSL/AGPL for the OSS substrate because (a) buyer trust signal is load-bearing pre-revenue, (b) the four-leg moat (strategic context, private) is operational/compounding, not source-level, (c) reversible — can switch to BUSL later if a real fork-and-SaaS threat materializes, (d) matches the playbook of comparable projects (LiteLLM Apache-2.0; Supabase Apache-2.0; PostHog Apache-2.0 + selective BUSL). Concrete migration plan in the repo-split plan (private). |

---

## 13. References

- `../the project strategy (private)` — buyer ≠ user; B2B framing; the budget owner is the buyer.
- `../the project strategy (private)` — startup-CTO default for the v1 buyer profile.
- `../the project strategy (private)` — local-first vs. SaaS deployment posture (still open).
- `../the project strategy (private)` — the open question this spec exists to surface for closure (kept open until owner ratifies).
- `../the project strategy (private)` — what changes about the build if §3 lands one way or the other; the "If hybrid" branch is the deployment substrate pricing assumes.
- [`deployment-shape.md`](deployment-shape.md) — hybrid (gateway-first → agent-upgrade); the deployment posture pricing composes with.
- [`gateway.md`](gateway.md) — the OSS gateway surface that is the free-tier foot-in-the-door.
- [`multi-user.md`](multi-user.md) — the identity layer that enforces per-seat / per-team pricing.
- [`multi-user.md §5`](multi-user.md) — the analytics surface that meters seats.
- [`multi-user.md §6.3`](multi-user.md) — per-team hard caps; orthogonal to pricing but operationally adjacent.
- [`multi-user.md §7.3`](multi-user.md) — audit-export surface; the missing precondition for an enterprise-tier %-of-savings line.
- [`multi-user.md §8`](multi-user.md) — explicit non-goals (SSO / OIDC / SAML / SCIM / RBAC / multi-org) that the enterprise tier eventually inherits as goals.
- [`analytics-api.md §4.1`](analytics-api.md) — per-user / per-team / per-key rollups; the metering substrate.
- [`analytics-api.md §4.7`](analytics-api.md) — the savings counterfactual.
- [`analytics-api.md §4.9`](analytics-api.md) — `/analytics/by_team`; the per-team rollup pricing composes with.
- [`canonical-message-format.md §6.4`](canonical-message-format.md) — `pricing_version` stamping; load-bearing for re-priceable savings counterfactual.
- [`event-bus-and-trace-catalog.md §6`](event-bus-and-trace-catalog.md) — the event catalog; pricing is read-only against this surface.
- [`benchmark.md`](benchmark.md) — workload-suite methodology that backs the savings counterfactual's credibility.

---

## 14. Sign-off

**Ratified 2026-05-16** — owner accepted the §5.5.4 recommendation: open-core gateway + per-seat Pro + reserved enterprise %-of-savings add-on. Price points (per-seat $/month, %-of-savings rate, Free-tier spend cap floor) remain commercial decisions deferred to first-buyer triangulation; this spec ratifies the *model shape*, not the *numbers*. Implementation lands as Wave 15.

the project strategy (private) edits landed alongside ratification:

- **§5** — dated entry: *"2026-05-16 — Adopt pricing model (open-core gateway + per-seat Pro + reserved enterprise %-of-savings add-on, per [`pricing.md`](specs/pricing.md))."*
- **§6.8** — retired: *"Resolved 2026-05-16: open-core gateway + per-seat Pro, with reserved enterprise %-of-savings add-on. See [`pricing.md`](specs/pricing.md)."*
