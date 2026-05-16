# Objection handling

> Real buyer objections, with the honest response. The goal is to land
> the deal *and* be right; promising things Metis can't deliver burns
> the trust that closes the next one. If the right answer is "don't buy
> us for that use case yet," say so.

For internal use. Update when you hit a new objection in the wild.

---

## "Vercel AI SDK will ship this any quarter."

**The objection:** Vercel has 24k stars on the cleanest typed message
abstraction in TS. They've been visibly pushing into agents. If they
ship an `Agent` abstraction with delegation and routing, why bet on a
new vendor?

**The honest response:**

This is the highest-risk lunch-eat scenario in our space, and
[`docs/STRATEGY.md §4`](../STRATEGY.md) names it as such.

What's true:
- Vercel AI SDK is TS-only. If your stack isn't Node, that's a hard gate.
- Vercel AI SDK has its own thinking-block bugs as of March–April 2026
  (#13430, #13703, both open). The canonical-IR work is harder than it
  looks; their head start in TS doesn't carry to Anthropic-native
  feature fidelity.
- Vercel's local-first / BYO-infra story is "not a priority." If a
  buyer wants traces that never leave their VPC, Vercel's cloud bias is
  a different shape than Metis's.
- Vercel doesn't ship learned routing or per-team cost attribution
  today. They'd be starting where we are now.

What's not in our favor:
- They could ship an `Agent` abstraction in six months and absorb a lot
  of the SDK-side wedge.
- They have a real distribution channel via Next.js.

**What to actually say:** "Yes, that's the highest competitive risk we
track. Today we ship lossless Anthropic-block fidelity, per-team cost
attribution, and learned routing — none of which Vercel ships. If
they ship those tomorrow, we'll be in a head-to-head, and our local-
first posture is the differentiator that's hardest for them to copy
(it's the opposite of their business model)."

---

## "Cursor / Claude Code can ship a local-first equivalent in a quarter."

**The objection:** Anthropic owns Claude Code. They have the protocol
docs first, the model first, and infinite engineering. If they decide
bounded memory and learned routing are features, they ship it tomorrow.

**The honest response:**

This is the second-named risk in [`docs/STRATEGY.md §4`](../STRATEGY.md).
What's true:

- Cursor and Claude Code are tied to their respective tools. Buying
  Cursor's routing means buying Cursor; same for Claude Code. Metis is
  a gateway in front of *whichever tool your devs already use* — you
  don't switch tools, you flip an env var.
- Anthropic has zero incentive to route *away* from Claude. Their
  margin depends on Claude usage. A learned router that picks GPT-5 on
  cheaper-equivalent tasks is contrary to their business model.
- Cursor's pricing is per-seat and includes routing; their cost
  attribution surface is "your Cursor invoice," not "per-dev,
  per-project, per-task-class on your own BYO-key trace DB."

What's not in our favor:
- If Anthropic decides "we'll ship the routing layer that picks our
  cheaper models" (sonnet over opus), they have the data and the
  pricing levers. Their version would route within-Anthropic only,
  but that's still a real competitor.

**What to actually say:** "If your team standardized on one IDE and one
provider and you're happy with that lock-in, Cursor or Claude Code's
in-tool routing is fine. Metis is for shops with mixed providers
(Anthropic + OpenAI + sometimes OpenRouter for long-tail models) and
mixed dev tools (some devs on Claude Code, some on Cursor, some
scripting against the SDK directly). The gateway flips one env var per
client; nobody has to switch tools."

---

## "LiteLLM is good enough and has 46k stars."

**The objection:** LiteLLM is the established player. It works, it
routes, it has dashboards. Why pick something with fewer stars?

**The honest response:**

LiteLLM is the right pick for some shapes. Not for the shapes Metis is
built for. The bug list is the honest reason:

- `#27512` (2026-05-09): Anthropic Messages retry drops thinking blocks.
- `#27469` (2026-05-08): `tool_call.function.arguments` lost in
  OpenAI→Anthropic conversion (regression in v1.83.7).
- `#26625`, `#20418`, `#20485`: Bedrock + Vertex `cache_control`
  placement broken.
- `#15601`, `#26916`, `#24985`: thinking blocks missing or collapsed
  to text across multi-turn / tool-call boundaries.

These are not edge cases. Anyone running a non-trivial Claude workload
through LiteLLM is one ticket away from a silent fidelity drop. LiteLLM
fixes these via tickets because their internal IR is OpenAI-shape and
can't represent these blocks losslessly to begin with.

Metis's internal IR is canonical, with Anthropic blocks load-bearing.
Per-provider adapters translate to/from each provider's wire format.
The cross-provider conformance suite mid-session-switches Anthropic →
OpenAI → OpenRouter with tool-use round-trip.

**What to actually say:** "If you're running pure OpenAI-shape traffic
and don't use `cache_control`, thinking, or `tool_use` round-trip
heavily, LiteLLM is fine. If your stack leans Anthropic — which the
$0.30/1M-token haiku-4.5 economics make compelling — the LiteLLM bug
list is going to bite you. We picked Anthropic blocks as the
authoritative internal shape precisely so this class of bug can't
exist."

(For more depth, see [`docs/sales/competitive-comparison.md`](competitive-comparison.md).)

---

## "We don't trust unproven cost-savings claims."

**The objection:** Every routing product claims "30% savings." Why
should we believe yours?

**The honest response:**

We don't claim 30% savings. The [`docs/savings-demo.md`](../savings-demo.md)
headline is "N=1 inversion on a 6-workload benchmark suite":

- Pass A (haiku pinned): quality 5.16, cost $0.198, $0.0383/quality.
- Pass B (sonnet pinned): quality 5.75, cost $0.676, $0.1176/quality.
- Pass C (slot 4 routes): quality 5.55, cost $0.265, **$0.0477/quality**.

Pass C achieved 8% more quality than haiku-only at 40% of sonnet-only
cost, because on the one hard turn (`regex-with-edge-cases` turn 2),
slot 4 picked sonnet; on every other turn it picked haiku.

The savings shape that's defensible to quote:
- "Pick the cheaper model on easy turns, escalate on hard ones."
- Magnitude on your workload depends on what fraction of turns are
  "hard." For workloads where every turn needs the same model, slot 4
  doesn't fire and Metis's cost wedge is the caching layer (~22% on
  long sessions) plus delegation (8.3–26.1% on fan-out workloads in
  the §A3-rev5 and §A3-rev6 runs).

**What to actually say:** "We don't quote a percentage. We quote a
mechanism, and we have one end-to-end demonstration of it firing.
Here's the trial recipe — run your prompts through, read the
cost-per-quality column, decide whether the mechanism fires on *your*
workload. We'll be honest if it doesn't (see
[`customer-trial-recipe.md §6`](../customer-trial-recipe.md): three
workload shapes where Metis won't move the needle on routing)."

---

## "We can't run another piece of infrastructure."

**The objection:** Self-hosted gateway means a pod / container /
process we have to operate, monitor, and upgrade. We don't have
headcount.

**The honest response:**

Two paths:
- **In-VPC helm chart.** Same observability surface (`/metrics`,
  `/healthz`) as any other in-cluster service; ServiceMonitor included
  for Prometheus Operator. The operational doc set ships in v1:
  [`incident-response.md`](../operations/incident-response.md),
  [`sla-template.md`](../operations/sla-template.md),
  [`status-page.md`](../operations/status-page.md),
  [`upgrade-guide.md`](../operations/upgrade-guide.md). Realistically:
  one engineer-day to deploy, near-zero ongoing operational load.
- **SaaS (when we ship it).** No infrastructure on your side; we hold
  no keys (BYO keys still). This isn't shipped yet — buyer-conversation
  evidence will decide whether SaaS comes before Metis Pro per
  [`STRATEGY.md §6.3`](../STRATEGY.md). If you need SaaS, tell us.

**What to actually say:** "If you have a kubernetes cluster, the helm
chart deploys in an afternoon and the operational load is similar to
running any other in-cluster proxy. If you need SaaS, we don't have it
yet — say so, and you'll move our roadmap. Don't sign a contract
expecting SaaS that we haven't shipped."

---

## "Per-team cost attribution? We have that already from our provider."

**The objection:** Anthropic and OpenAI both expose per-key cost in
their consoles. We already attribute by provisioning a key per team.

**The honest response:**

That's a real attribution story, with two limitations:
- **Per-key, not per-user.** If a team's key is shared (which is
  common in CI / shared dev environments / scripted clients), you lose
  the per-user dimension.
- **Provider-by-provider.** Roll-up is in the provider's console, not
  unified across Anthropic + OpenAI + OpenRouter.

Metis adds:
- `user_id` and `team_id` stamped on every event, with
  `/analytics/by_user`, `/analytics/by_team` rollups.
- Cross-provider: same trace DB rolls up Anthropic + OpenAI + OpenRouter
  spend in one query.
- `gateway_key_id` rotation without re-provisioning a provider key (the
  gateway holds the upstream key; you rotate gateway keys without
  asking the provider for new ones).

**What to actually say:** "If per-key per-provider is enough for you,
keep what you have. We're useful when you need per-user attribution
even on shared keys, or unified cross-provider rollup, or fast key
rotation."

---

## "What happens to traces / what's your data story?"

**The objection:** Where does our prompt / completion data go? Are
those traces leaving our VPC?

**The honest response:**

- Trace DB is on-disk SQLite in the workspace you point Metis at
  (default `~/.metis/metis.db`). Nothing leaves your perimeter.
- No telemetry, no phone-home, no usage reporting back to us.
- If a buyer wants to integrate with a SIEM, `metis audit export` gives
  deterministic JSONL or CSV; redaction layer (`metis audit export
  --redact <mode>`) supports pseudonymize / redact-private /
  aggregate-only modes.
- GDPR Article 17 (right to erasure): `metis user forget <user_id>`
  pseudonymizes identity fields in place across the trace DB. Aggregate
  analytics survives; the link to the natural person doesn't.
- Retention sweep: `metis trace prune --days 90` is the default; helm
  CronJob template ships in v1. Audit-flagged events (12-event subset)
  are exempt from the sweep so the audit trail of the sweep mechanism
  itself survives.

**What to actually say:** "Your data stays where you put it. The audit
log + retention + redaction triad ships in v1; see
[`docs/operations/soc2-readiness.md`](../operations/soc2-readiness.md)
for the full SOC2 Trust Service Criteria mapping. SOC2 Type 1 target is
Q3 2026 contingent on a buyer underwriting the audit fee; we're being
honest about that gap."

---

## "Are you the kind of company that's going to be around in two years?"

**The objection:** Solo, part-time owner. What happens to our
deployment if the project gets abandoned?

**The honest response:**

This is a fair concern. The honest framing:

- The architecture is local-first by design. Even if Metis the
  organization disappears tomorrow, your deployed gateway keeps running
  on your infra against your keys. The trace DB is plain SQLite.
- Source is the deliverable. If the buyer needs an exit, the codebase
  is small enough (canonical IR + ~3 provider adapters + routing
  engine + gateway harness) that any competent team can fork and
  maintain it.
- Specs-first development means every component has a contract in
  `docs/specs/`. A successor maintainer doesn't have to reverse-
  engineer the design.
- Conformance to open standards: agentskills.io for skills, OpenAI +
  Anthropic provider shapes for the gateway. No proprietary lock-in
  formats on either edge.

**What to actually say:** "Solo, part-time — that's a real risk. The
mitigation is structural, not promises: your deployment is yours; the
source is the deliverable; the format is open. If your procurement
needs a continuity clause, source escrow + a `LICENSE` that survives is
the lever, and we'll work with you on it."

---

## "We need SOC2 / ISO / HIPAA."

**The objection:** Compliance is non-negotiable. What's your posture?

**The honest response:**

Read [`docs/operations/soc2-readiness.md`](../operations/soc2-readiness.md)
front-to-back; that's the honest answer. The audit document maps Trust
Service Criteria CC1–CC9, A1, C1, PI1, P1–P8 against shipped
+ buyer-responsibility evidence. Gaps named honestly:
- CC8 change management — not a formal process yet.
- Third-party pentest — not done.
- Vendor review — not done.
- SOC2 auditor engagement — not engaged.

Type 1 readiness target Q3 2026 contingent on a buyer underwriting the
audit fee. Type 2 Q4 2026 / Q1 2027.

The Wave 12 triad ships the compliance scaffolding:
- [`audit-log.md`](../specs/audit-log.md) — 12-event subset, JSONL/CSV
  deterministic export.
- [`trace-retention.md`](../specs/trace-retention.md) — 90-day sweep,
  audit-event exemption.
- [`redaction.md`](../specs/redaction.md) — 4-mode redaction +
  pseudonymization-as-erasure.

ISO 27001 / HIPAA: not in scope for v1. If a buyer needs either,
that's a conversation about co-funding the work.

**What to actually say:** "We have the technical primitives shipped
(audit log, retention, redaction, GDPR forget). We don't have the
auditor sign-off. If you can fund the audit, we can be Type 1 by Q3.
If you can't, this might be too early for you."

---

## "Why not just use OpenRouter? They route across providers already."

**The objection:** OpenRouter is a one-stop shop. One API key, all
models, fallback built in. Why add another layer?

**The honest response:**

OpenRouter is great as a *catalog* — long-tail model availability,
unified pricing, one key. We list it as one of Metis's three provider
adapters and recommend it for the long-tail catalog access.

What OpenRouter doesn't do:
- Run on your infra. It's cloud-only; your traces go through their pipes.
- Learn from outcomes. Their "auto" model routes by price/latency rules,
  not by task-fingerprint outcome history.
- Per-user / per-team / per-project cost attribution. Per-request only.
- Anthropic-native feature fidelity. OpenRouter is OpenAI-shape internally;
  Anthropic blocks pass via `extra_body` and thinking parts often flatten.
- Replay survival across provider API changes.

**What to actually say:** "OpenRouter is in our stack as a catalog
source — we route to it for long-tail models. Metis sits in front and
adds the things OpenRouter doesn't: BYO-infra, learned routing, per-
user cost attribution, canonical IR. If you only need long-tail
catalog access and you're OK with cloud-only, OpenRouter alone is
simpler."

---

## "Are we early or are we beta?"

**The objection:** Honest version of the "are you going to be around"
question — what stage is the product at?

**The honest response:**

- **What's shipped:** Phase 1 + Phase 2 + Phase 2.5; Phase 3 in flight
  with the three wedges live (transparent gateway, multi-user identity,
  evaluator). 1678 tests passing. See [`docs/operations/phase-claim-proposal.md`](../operations/phase-claim-proposal.md).
- **What works in production:** Gateway deployed via Docker compose or
  helm. ~$0.0002 / 4 calls live-validated. Per-key / per-user / per-team
  rollups live. SOC2 readiness audit shipped.
- **What's in flight:** Pattern store v2 cluster-tightening (the
  routing inversion is N=1 in v1, generalization is the next wave of
  benchmark-suite work). Context-assembler v3 skill activation.
- **What's drafted but not implemented:** Skill curator, delegation
  v2 (async workers, cancellation cascade, recursive delegation).

**What to actually say:** "Early. The gateway is production-shipped
and we have a buyer-trial recipe with a < 1 hour onboarding path. The
mechanism that drives the cost wedge has one end-to-end demonstration,
not a regime. If you want to run a 2-week pilot and read the trace
yourself, that's the right shape; if you want a vendor with three years
of customer logos, we're not there yet."
