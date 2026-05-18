# Business Model

**Status:** Synthesis. The contract lives in [`docs/specs/pricing.md`](specs/pricing.md) (§5.5.4 ratified 2026-05-16). This doc collects the *why* — thesis, tiers, savings evidence, unit-economics intuition, and risks — into one place.
**Last updated:** 2026-05-17

> Read [`STRATEGY.md`](STRATEGY.md) first for the cost-optimization thesis and *buyer ≠ user* framing, and [`docs/specs/pricing.md`](specs/pricing.md) for the formal pricing-model spec. This doc is the synthesis between them: how the ratified pricing shape composes with the shipped primitives and the validated savings claims into a defensible commercial story.
>
> If anything here drifts from [`pricing.md`](specs/pricing.md), the spec wins.

---

## 1. Thesis — what's being sold

Metis does **not** sell tokens. Provider usage stays billed by Anthropic / OpenAI / OpenRouter directly. What Metis sells is **cost reduction on the buyer's existing LLM spend**, attributable per developer and per team via the gateway, observed via the savings counterfactual ([`analytics-api.md §4.7`](specs/analytics-api.md)).

The buyer is the budget owner — an engineering leader or CTO ([STRATEGY.md §2](STRATEGY.md)). The user is the developer running an SDK client. Pricing follows the buyer's mental model (per-seat for predictability), the savings claim follows the user's traffic (reproducible on their own workload).

## 2. The three tiers

Ratified shape: **open-core gateway + per-seat Pro + reserved Enterprise %-of-savings add-on** ([pricing.md §5.5.4 / §7](specs/pricing.md)).

| Tier | Price shape | What's included | Conversion trigger |
|---|---|---|---|
| **Community** | $0, self-host | Gateway (full routing, lossless canonical IR, per-key analytics), CLI/TUI agent surface, heuristic evaluator, bounded memory, pattern store, source under permissive license | Buyer wants team scale, not just personal use |
| **Pro** | Per active user / month | Multi-user identity (`users.json` / `teams.json`), per-user + per-team analytics, hard caps + soft-cap routing predicates, LLM-judge evaluator tier, audit log export, hosted SaaS option, the upgrade-tier agent | Procurement comfortable with seat-based SaaS |
| **Enterprise** | Custom: Pro per-seat + **capped %-of-savings add-on** | SOC2 / SAML / OIDC / SCIM, multi-org tenancy, on-prem / VPC + SLA, reserved-capacity savings pricing, custom routing policies, dedicated success engineering | Procurement-led buyer wants outcome-linked contract |

**"Active user"** = a `usr_<ulid>` whose gateway keys produced at least one `llm.call_completed` in the billing window. Composes directly with the shipped [`/analytics/by_user`](specs/analytics-api.md) rollup — no new metering subsystem. Stale accounts don't bill.

**Enterprise %-of-savings** is *capped* (target shape: ~10-20% of `(baseline_repriced_usd - actual_repriced_usd)` with a monthly ceiling). Both numbers come from [`analytics-api.md §4.7`](specs/analytics-api.md) so the bill is reproducible from the trace store — buyer audits the number, doesn't have to trust it.

## 3. Why this shape

The model wins on five dimensions ([pricing.md §6 / §7.4](specs/pricing.md)):

1. **Lowest adoption friction at the front door.** Open-core gateway = env-var flip, savings dashboard inside hours, zero commitment. Matches the "trial without payment" floor from [`deployment-shape.md §1`](specs/deployment-shape.md).
2. **Per-seat maps to a CFO's mental model.** Bill = seat count × rate. Predictable, modelable on a napkin. Avoids the per-call optical problem ("you charge me for every Anthropic call").
3. **Composes with shipped primitives.** The user identity layer *is* the seat list. The [`/analytics/by_user`](specs/analytics-api.md) rollup is the meter. The [billing module](../apps/gateway/src/metis_gateway/billing/) is live and Stripe-backed (Wave 15: subscription lifecycle, self-service portal, plan changes, failed-payment grace, webhook idempotency, tier-axis quota composition).
4. **Reserves %-of-savings for the right buyer.** Startup CTO wants predictability; enterprise CFO *prefers* outcome-linked spend (it justifies internally). Saving the audit-export-heavy %-of-savings model for enterprise defers contract-negotiation overhead to the buyer who wants it.
5. **The Stripe-backed primitive exists.** Wave 15 shipped the implementation. Stripe live-mode validation is the only remaining step before the first paid invoice.

## 4. The validated savings claims

This is what makes the model commercially viable. The pricing only works if a buyer can *see* the savings before they're asked to pay.

After 8 §A3 benchmark iterations plus the Run-3 cache demo ([benchmarks/RESULTS.md](../benchmarks/RESULTS.md)):

| Lever | Evidence | Posture |
|---|---|---|
| **Context engineering** (prompt cache + lean prompts) | 49/49 cache fire; same-workload cost down 22.8% (Run 3) | Largest typical lever; ships in all tiers |
| **Delegation** (slot 5 routing) | 8.3% / 19.9% / 26.1% better cost-per-quality across 3 runs (§A3-rev5/rev6/rev7) | **Validated GTM headline** for the routing surface |
| **Model selection** (slot 4 routing) | §A3-rev3 N=1 inversion on `regex-with-edge-cases` | Proof-of-mechanism; generalization gated on workload-domain rate of measurable haiku-vs-sonnet gaps |

Generalization status: after 8 A3 iterations the model-selection differentiator's posture stabilizes — *mechanism proven, generalization gated on benchmark-suite signal strength rather than routing-engine tuning*. The §A3 task-domain wedge (math/symbolic, long-context multi-document synthesis, rare API surfaces) is deliberately deferred post-GA. See [STRATEGY.md §1](STRATEGY.md) for the dated entries.

## 5. Unit economics intuition

Per-seat is recurring revenue from a buyer whose **dev count is the bill driver, not their LLM spend**. Contrast with %-of-savings as the default, where revenue scales with the buyer's *usage* — which rewards Metis for the buyer spending more on LLMs (perverse-incentive flavor).

Per-seat properties:

- **Aligned** — Metis is rewarded for adding seats, not for buyer LLM spend going up.
- **Capacity-modelable** — N customers × M seats × $X is a simple revenue ladder a CFO can plan against.
- **Enterprise add-on is the upside** — large accounts opt into the %-of-savings add-on with a cap, so Metis captures upside on the highest-spend buyers without exposing every buyer to audit-export overhead.

## 5a. Implementation pattern — two repos, "thin Pro repo"

The commercial model maps to a **two-repo layout** ([STRATEGY.md §5 2026-05-17](STRATEGY.md), [pricing.md §9.5 / §12](specs/pricing.md)):

- **`metis` (OSS, Apache-2.0)** — the entire substrate that delivers savings: gateway, canonical IR, adapters, routing chain, pattern store, bounded memory, tools, skills, heuristic evaluator, per-key analytics, agent CLI/TUI/serve, concierge CLIs. Standalone-usable end-to-end.
- **`metis-pro` (private, all-rights-reserved)** — only operationally-sensitive surfaces: billing, signup, accounts store, hosted dashboard UI, curated LLM-judge rubric library, enterprise SAML/OIDC/SCIM. Plugs into OSS via extension Protocols (`BillingBackend`, `SignupBackend`, `AnalyticsExtension`, `JudgeRubricProvider`) with noop defaults.

The split is deliberately thin — most `pricing.md §7.2` Pro features are *access-restricted* (route handlers, rollup endpoints) rather than *code-restricted* (sensitive logic). Apache-2.0 is the OSI-approved permissive choice because (a) buyer trust signal is load-bearing pre-revenue, (b) the four-leg moat ([STRATEGY.md §4](STRATEGY.md)) is operational/compounding, not source-level — LiteLLM (Apache-2.0, ~17.5k stars) and Supabase / PostHog / Cal.com all survive permissive licensing because moat lives in roadmap velocity + accumulated buyer data + brand, and (c) reversible (single-repo merge or BUSL relicense remain on the table if a real fork-and-SaaS threat materializes — a 2028+ problem at current scale).

Concrete migration checklist: [`docs/operations/repo-split-plan.md`](operations/repo-split-plan.md). No code has moved yet — the migration is scheduled, not executed.

## 6. What's deliberately NOT decided

Per [pricing.md §7.5](specs/pricing.md):

- **$X/seat/month.** No buyer evidence yet to triangulate. Price points are downstream of first paid conversations.
- **Where the OSS / Pro line falls feature-by-feature.** §7.1 names the principle; the line moves as features ship.
- **Annual vs monthly billing.** Standard SaaS default (annual-with-monthly-option) is inherited.
- **The OSS license.** Apache 2.0 vs BUSL vs AGPL is owner's call when the OSS surface publishes.
- **Whether the upgrade-tier agent is in Pro at launch or is a Pro-plus add-on.** Depends on agent maturity at GA.

## 7. Risks and open strategic questions

1. **First paid buyer doesn't exist yet.** All price-point triangulation comes after first commercial conversations. Wave 16 shipped concierge tooling (`metis trial`, `metis customer-report --anonymize`, `metis trial-status`, day-0 to day-30 runbook in [`first-customer-runbook.md`](operations/first-customer-runbook.md)) so the owner can run the conversion loop.
2. **Local-first vs SaaS posture** ([STRATEGY.md §6.3](STRATEGY.md)) is still open. Pricing works under either deployment shape, but GTM messaging differs.
3. **Competitive pressure on the free tier.** If LiteLLM / Portkey / Helicone ship comparable open-core surfaces, the Community tier loses its adoption-funnel advantage. The moat is the four-leg differentiation ([STRATEGY.md §4](STRATEGY.md)): bounded agent-curated memory, lossless canonical IR, task-fingerprint pattern learning, auto-derived skill curation.
4. **Buyer profile not pinned** ([STRATEGY.md §6.2](STRATEGY.md)). 20-dev startup CTO vs 200-dev enterprise eng leader want very different products. Current default lean: startup CTO first → per-seat Pro is the right anchor.
5. **Outcome-coupled spend has a perverse incentive for *Metis*.** If %-of-savings dominates, Metis is rewarded by buyers using more LLMs. The cap and the per-seat baseline are what defuse this.

## 8. The close — savings reproducibility

The whole pricing edifice rests on **the savings number being reproducible on the buyer's own traffic**.

- **Community tier:** buyer runs `metis trial` against their workload, sees `savings_pct` on their own data inside an hour. Recipe in [`docs/operations/quickstart.md`](operations/quickstart.md).
- **Pro tier:** per-user / per-team rollups show "Alice's keys spent $X this month, savings vs counterfactual was $Y." Surface in [`/analytics/by_user`](specs/analytics-api.md) and [`/analytics/by_team`](specs/analytics-api.md).
- **Enterprise tier:** monthly invoice line = capped percentage of `(baseline - actual)`, audit-exportable via [`metis audit export`](specs/audit-log.md).

That's why Wave 15 prioritized the GA-readiness audit's two blockers (NETWORK escalation refinement + bare-model normalization) and Wave 16 prioritized concierge tooling — together they made the savings number reliable enough to put on a buyer-facing report (`metis customer-report --anonymize`).

---

## Pointers

- [`docs/specs/pricing.md`](specs/pricing.md) — the formal pricing spec (contract).
- [`docs/STRATEGY.md`](STRATEGY.md) — cost-optimization thesis, buyer ≠ user, open strategic questions.
- [`docs/specs/multi-user.md`](specs/multi-user.md) — the identity layer the Pro tier composes with.
- [`docs/specs/analytics-api.md`](specs/analytics-api.md) — the savings-counterfactual surface (§4.7) and per-user / per-team rollups.
- [`docs/specs/deployment-shape.md`](specs/deployment-shape.md) — hybrid (gateway-first → agent-upgrade) that the pricing must accommodate.
- [`apps/gateway/src/metis_gateway/billing/`](../apps/gateway/src/metis_gateway/billing/) — Stripe-backed billing module (Wave 15).
- [`docs/operations/first-customer-runbook.md`](operations/first-customer-runbook.md) — day-0 to day-30 concierge cadence for the first paid cohort.
- [`docs/operations/billing-operator-guide.md`](operations/billing-operator-guide.md) — Stripe test-mode operations (refunds, disputes, plan changes, failed-payment grace).
- [`docs/sales/`](sales/) — buyer-facing collateral (one-pager, competitive comparison, FAQ, objection-handling, case-study templates).
- [`benchmarks/RESULTS.md`](../benchmarks/RESULTS.md) — §A3 iteration log + Run-3 cache evidence backing the savings claims.
