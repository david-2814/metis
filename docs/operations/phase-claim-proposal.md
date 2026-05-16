# Phase claim review proposal

**Status:** Proposal pending owner review (do not bump AGENTS.md until owner signs off).
**Drafted:** 2026-05-15 (post-Wave-13).
**Author:** AI agent, on behalf of the part-time owner.
**Reviewer:** owner.

This document lays out the evidence for each candidate phase-claim level
and recommends a position. It does **not** change AGENTS.md's status
sentence — that bump is owner-decision territory per the standing rule.

The current status sentence in [`AGENTS.md`](../../AGENTS.md) reads:

> Phase 1 + Phase 2 + Phase 2.5 shipped … Phase 3 in flight
> (**ready-for-review whether to promote to "Phase 3 shipped"** …).

The three candidate positions:

1. **Hold at "Phase 3 in flight"** (current claim).
2. **Bump to "Phase 3 shipped."**
3. **Bump to "Phase 3 shipped + Phase 4 v1 started."**

## 1. What Phase 3 originally promised

[`docs/project-overview.md §Phasing summary`](../project-overview.md):

> **Phase 3 — Polish + sync.** In-session adjustment heuristics, full
> evaluator, MCP support, git sync, third provider.

[`docs/STRATEGY.md §4`](../STRATEGY.md) ratified three "Phase 3 wedges"
the owner cares about for the buyer story: **transparent gateway,
multi-user identity / per-team cost attribution, evaluator.**

The two lists don't perfectly overlap (the project-overview list
predates the gateway resolution in §3); the STRATEGY.md list is the
load-bearing one for the GTM thesis.

## 2. Evidence inventory (what's actually live)

### 2.1 STRATEGY.md Phase 3 wedges

| Wedge | Status | Evidence |
|-------|--------|----------|
| Transparent gateway | shipped | [`apps/gateway/`](../../apps/gateway/), live-smoked 2026-05-14, helm chart + Docker compose, OpenAI + Anthropic shapes sync + SSE, per-key cost attribution. |
| Multi-user identity | shipped | `GatewayKey.user_id` / `team_id` (Wave 8a-4..6); `/analytics/by_user` + `/analytics/by_team` + `group_by=user|team` rollups. |
| Evaluator | shipped | Heuristic + LLM + Hybrid tiers, four subject kinds, `BudgetTracker`, `/analytics/quality` endpoint. |

### 2.2 project-overview.md Phase 3 line items

| Line item | Status | Notes |
|-----------|--------|-------|
| Third provider | shipped (since Phase 1) | OpenRouter landed in Wave 2; Anthropic + OpenAI + OpenRouter all live. |
| Full evaluator | shipped | Heuristic + LLM + Hybrid; one tier per spec (`evaluator.md §5`); tool-cycle and session subjects remain heuristic-only by design. |
| In-session adjustment heuristics | **not shipped** | The routing engine is turn-locked by design (`AGENTS.md` gotcha); the spec does not contemplate mid-turn re-routing. If the project-overview's "in-session adjustment heuristics" meant mid-turn re-routing, that's intentionally out of scope; if it meant cross-turn pattern learning, the pattern store (Phase 2.5) covers it. |
| MCP support | **not shipped** | No MCP server or client wiring. Not on any active wave. |
| Git sync | **not shipped** | Memory and skills files live on disk; the owner uses git outside Metis. There is no in-Metis sync primitive. |

### 2.3 What landed beyond the original Phase 3 scope

- **Pattern store v1 (Wave 4) + v2 hybrid embeddings (Wave 10–11)** —
  routing slot 4 is live end-to-end, K-NN math verified, recording-side
  HYBRID rows written natively at turn-start.
- **Delegation v1 MVP (Wave 10)** — `delegate()` tool + worker sessions
  + slot 5 routing + cost attribution + worker isolation. Listed as
  Phase 4 in `project-overview.md` but shipped in Wave 10 (see §3.3).
- **Production hardening (Wave 11–13)** — Prometheus `/metrics`,
  rate-limit middleware, API versioning enforcement, trace
  backup/restore, gateway key lifecycle, audit log, trace retention,
  redaction layer, GDPR export/forget, SOC2 readiness audit,
  multi-tenant gateway bind (Wave 13), benchmark-suite v2,
  trace-store and pattern-store production audits.
- **Operations docs (Wave 11–12)** — `docs/operations/` ships
  incident-response, SLA template, status-page, upgrade-guide,
  compliance-overview, SOC2 readiness, buyer-trial quickstart.

### 2.4 Differentiator evidence (the GTM-load-bearing part)

- **Model-selection lever** — §A3-rev3 (N=1) end-to-end inversion on
  `regex-with-edge-cases` turn 2; did **not** reproduce in
  §A3-rev4/rev5/rev6 despite all five mechanical blockers being
  removed and verified. 13a-1 ruled out workload-signal-strengthening
  as a sufficient single-knob fix. 13b-1 (§A3-rev7) brief lists two
  paths still open: finer-grained outcome scoring, and task domains
  haiku has known weakness in. Neither yet run.
- **Delegation lever** — §A3-rev5 + §A3-rev6 end-to-end: 8.3% – 26.1%
  better cost-per-quality on a delegation-suited workload
  (`multi-step-with-delegation`). Reproducible across two runs.
- **Caching lever** — Run 3 in [`benchmarks/RESULTS.md`](../../benchmarks/RESULTS.md):
  cache fires on 49 of 49 LLM calls (100%) vs Run 2 cold's 10 of 30
  (33%); same-3-workload aggregate cost dropped 22.8%.

## 3. The three candidate positions

### 3.1 Position A — hold at "Phase 3 in flight"

**What's missing to bump.** The owner has stated the bump remains
owner-decision territory; the conservative read is that "Phase 3
shipped" should be claimed only when the differentiator generalizes
beyond N=1 on the model-selection lever (which the
6-A3-iteration story argues is now a benchmark-suite signal-strength
problem, not a routing problem).

**Why hold:**

- The model-selection differentiator at N=1 is a real datapoint but
  not a reproducible regime; "Phase 3 shipped" reads as "the wedge
  works at GTM scale" and that's not yet defensible.
- 13b-1 (§A3-rev7) hasn't run; the brief identifies two specific
  unblocks (finer-grained outcome scoring, task domains with known
  haiku weakness) that might land the generalization or might rule
  out routing-engine work entirely.
- The operational case (audit / retention / redaction / SOC2 / multi-
  tenant gateway / production audits) is the *strongest* it's been,
  but a buyer can sign on the operational case without us claiming
  Phase 3 shipped.

**Cost:** the GTM narrative stays softer than the engineering reality;
external readers underweight what's actually live.

### 3.2 Position B — bump to "Phase 3 shipped"

**What's needed.** All three STRATEGY.md wedges shipped end-to-end
with measurable buyer-facing value. By the strict-text test, that's
already true:

- Gateway: live, helm-deployable, per-key cost rollup verified.
- Multi-user identity: per-user / per-team rollups live; documented;
  helm-ready.
- Evaluator: heuristic + LLM + hybrid tiers live; `/analytics/quality`
  exposed; benchmark harness integrates the judge end-to-end.

**Plus operational completeness:**

- Audit log + trace retention + redaction layer + GDPR data export +
  GDPR forget — all shipped (Wave 12).
- SOC2 readiness gap audit shipped (CC1–CC9, A1, C1, PI1, P1–P8
  mapped with honest gap calls).
- Multi-tenant gateway bind shipped (Wave 13, 13a-3) — the long-
  standing loopback-only deferral is closed.
- Buyer-trial quickstart shipped (kind + helm + `metis trial` end-to-
  end < 1 hour).

**What the position deliberately defers:**

- MCP support — not on any active wave; should be reframed as Phase 4
  scope (or dropped, since the gateway provides a similar
  client-substitutability story).
- Git sync — the owner does this outside Metis; should be reframed as
  out-of-scope, not "Phase 3 unfinished."
- In-session adjustment heuristics — the design choice (turn-locked
  routing) makes this incoherent at the spec layer; should be dropped.
- N>1 generalization on the model-selection differentiator — gated on
  benchmark-suite signal strength per the 13a-1 finding; the
  delegation differentiator covers the cost-per-quality GTM datapoint
  in the interim.

**Cost:** committing to "Phase 3 shipped" before the model-selection
inversion generalizes makes the next-quarter GTM conversation
contingent on the delegation lever alone for the cost-per-quality
story.

### 3.3 Position C — bump to "Phase 3 shipped + Phase 4 v1 started"

**What's needed beyond Position B.** `project-overview.md` defines
Phase 4 as "Tauri desktop app, public-ready UX, marketplace
foundation." `STRATEGY.md §4` reframes the moat as four legs (bounded
memory, canonical IR, pattern learning, skill curation) with the
fourth leg (skill curator) gated on agent-authored skills landing
first. Phase 4 in the spec sense ≠ Phase 4 in the GTM sense.

If "Phase 4 v1 started" is interpreted as **"the Phase-4-scoped MVP
of delegation is live,"** then Wave 10 already crossed that line:

- `delegate()` tool shipped end-to-end (`delegation.md §4-7`).
- Worker sessions + worker isolation + cost attribution + analytics
  rollups all live.
- §A3-rev5 + §A3-rev6 validated delegation's cost-per-quality
  differentiator.

The deferral list (delegation.md §3.6 — async/concurrent workers,
cancellation cascade, recursive delegation, router-decided
delegation) is real but doesn't block claiming "v1 started."

**What's missing for the project-overview version of Phase 4:**

- Tauri desktop app — not started.
- Public-ready UX — partial (TUI ships; web UI does not).
- Marketplace foundation — not started; `skills-format.md` is the
  ingredient, but no marketplace primitives exist.

**Cost:** claiming "Phase 4 v1 started" without the Tauri / UX / marketplace
pieces requires a footnote disambiguating "delegation v1" from the
project-overview's Phase 4 scope. Confusing for external readers.

## 4. Recommendation

**Recommend Position B (bump to "Phase 3 shipped") with two explicit
caveats stamped on the status sentence:**

1. **Operationally Phase-3-complete.** The three STRATEGY.md wedges
   shipped end-to-end; the Wave 12 compliance triad and Wave 13
   multi-tenant gateway close the buyer-facing operational gaps
   that previously made "in flight" the honest framing.

2. **Model-selection differentiator generalization remains an open
   benchmark-suite problem, not a routing-engine problem.** The
   §A3-rev3 N=1 inversion stands as canonical proof-of-concept; the
   delegation differentiator (8.3% – 26.1% cost-per-quality)
   stands as the reproducible GTM datapoint. 13b-1 (§A3-rev7) is the
   path to N>1 generalization.

**Rationale.**

- Holding at "in flight" understates what shipped. A reader hitting
  `AGENTS.md` cold cannot distinguish "evaluator landed but is
  half-wired" (was true in Wave 5) from "evaluator + gateway +
  multi-user + audit log + GDPR + SOC2 readiness + multi-tenant
  gateway all landed and are verifiable end-to-end" (true post-Wave-13).
  The current "in flight" framing reads as the former; the reality is
  the latter.

- Bumping all the way to Position C requires either redefining Phase 4
  (delegation v1 as the v1-started milestone) or footnoting which
  Phase 4 scope is meant. The disambiguation is not worth the GTM
  ambiguity it creates; better to claim Phase 3 shipped cleanly and
  flag delegation v1 as a separately-shipped capability in the next
  status iteration.

- The model-selection differentiator's N>1 gap is real but does not
  block Phase 3 shipping. Phase 3's contract was "the three wedges
  work end-to-end with buyer-facing value." All three meet that bar.
  The N>1 question is a Phase 3.5 / Phase 4 sharpening, not a
  Phase 3 gate.

## 5. Suggested replacement status sentence

If the owner ratifies Position B, the AGENTS.md status sentence
should read approximately (full draft text owner-editable):

> **Status:** Phase 1 + Phase 2 + Phase 2.5 + Phase 3 shipped. The
> three Phase-3 wedges — transparent gateway, multi-user identity /
> per-team cost attribution, evaluator — are live end-to-end with
> buyer-facing value; Wave 12 closes the SOC2/GDPR compliance gap
> (audit log + trace retention + redaction + GDPR export/forget + SOC2
> readiness audit); Wave 13 lifts the gateway's loopback-only
> constraint behind a documented hardening checklist. **Differentiator
> posture:** the delegation lever produces a reproducible 8.3% –
> 26.1% cost-per-quality improvement (§A3-rev5 + §A3-rev6); the
> model-selection lever inverted end-to-end on 1 workload
> (§A3-rev3 N=1) and remains gated on benchmark-suite signal
> strength for N>1 generalization (six A3 iterations confirm the
> mechanical chain is fully wired). Delegation v1 MVP is live
> separately (Wave 10). 1678 tests passing.

## 6. Decisions requested

| # | Question | Recommended answer |
|---|----------|-------------------|
| 1 | Bump to "Phase 3 shipped"? | Yes (Position B). |
| 2 | Reframe MCP support / git sync / in-session adjustment as out-of-scope? | Yes — they are project-overview.md artifacts, not STRATEGY.md commitments. |
| 3 | Claim "Phase 4 v1 started" for delegation v1? | No — disambiguation cost > narrative benefit. Call out delegation v1 separately. |
| 4 | If yes to #1, ratify the replacement status sentence in §5? | Owner-edit pass requested. |

## 7. How to use this document

- **Owner:** read, mark up §6 decisions, and either ratify the §5
  sentence or send back redlines. Until the owner approves, the
  AGENTS.md status sentence stays at "Phase 3 in flight."
- **Future AI agents:** if you find this document after the owner has
  ratified, update AGENTS.md and STRATEGY.md to the ratified language
  and mark this proposal "superseded by <commit hash>."
- **Future AI agents pre-ratification:** do not bump the status
  sentence based on this proposal alone; it is a draft until §6 is
  signed.
