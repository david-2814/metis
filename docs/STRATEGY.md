# Strategy

**Last updated:** 2026-05-16
**Status:** Working document. Strategic decisions and open questions that aren't visible from the code or the technical specs. Update when a decision lands.

This doc captures the **why** behind the project — the kind of context an AI agent walking into the codebase cold can't infer from `docs/project-overview.md` (which describes the *shape* of the system) or the per-component specs (which describe the *contracts*). Read this before recommending scope changes, priority shifts, or architectural pivots.

---

## 1. The thesis

**Metis optimizes a buyer's LLM usage cost.** The wedge is doing it through three levers, applied together:

1. **Model selection** — pick the cheapest model that can do the task well; route the rest to bigger models.
2. **Context engineering** — keep prompts lean (prompt-cache discipline, history pruning, skill lazy-loading, stable tool-def prefixes). This is the largest typical lever; cache reads at 0.1× vs cache writes at 1.25× can move bills 5–10× on long sessions.
3. **Skills** — load expert instructions on demand (`agentskills.io`-compatible). Smaller models can do focused work when given a focused prompt. Progressive disclosure (~100 token metadata until activated) means most skills cost ~nothing until needed.

The order of impact on a typical workload is **context > skills > model selection**. The current implementation has the inverse priority — routing is most built, skills exist as a Phase 2 wedge, the context-assembler is still architectural-diagram-only. This is a known mismatch to resolve.

**Model-selection lever calibration (2026-05-14, §A3-rev3 finding):**
the benchmark's flat `savings_pct=66.7%` is the structural haiku-
vs-sonnet rate-card ratio (equal-token counts × either rate). It
does not reflect a quality-weighted choice. **§A3-rev3 is the first
A3-series experiment where it stops being the only number.**
[`benchmarks/RESULTS.md §A3-rev3`](../benchmarks/RESULTS.md) re-runs
the three-pass protocol after Wave 9's one-line knob landed
(`PatternConfig.min_confidence: 0.3 → 0.05`,
[`routing/policy.py:63`](../packages/metis-core/src/metis_core/routing/policy.py#L63)).
Pass C reaches slot 4 on **14 of 18 turns** (vs §A3-rev2's 3 of 16)
and **picks sonnet on `regex-with-edge-cases` turn 2** — the hard
"16-test edge cases" turn where haiku rubric-fails (0.19@0.80 in
Pass A, 0.74@0.80 in Pass C). Cluster: haiku=0.784, sonnet=0.833,
confidence=0.058 — would have been rejected under the prior 0.3
gate. Pass C aggregate `savings_pct=62.0%` (4.7-point gap from
flat-haiku 66.7% reflects the one sonnet pick on the expensive
turn). Pass C quality sum **5.55 beats Pass A's 5.16 (+8%
quality)** at cost-per-quality **$0.0477** — landing between
haiku-only $0.0383 and sonnet-only $0.1176, with the headline that
slot 4 routed sonnet *only* on the turn where it mattered.

The savings posture moves from **"rate-card savings *given haiku
succeeds*"** to **"differentiated routing picks the succeeding
model on 1 of 6 evaluable workloads where it mattered; Pass C
quality > Pass A quality at 40% of Pass B cost."** N=1 is one
end-to-end demonstration, not a regime — the brittle parts are
(a) the sample-size asymmetry on specific fingerprints (other
workloads have sonnet-ahead cluster aggregates but haiku-ahead
specific-fingerprint clusters that slot 4 saw at decision time),
and (b) the residual per-turn-vs-workload signal divergence on
`multi-turn-refactor`. Both are addressable in Wave 10 (more
samples per cluster + the §A3-rev2 path #1 — wire
`eval.completed(subject_kind=workload)` into
`pattern.outcome_updated`).

**§A3-rev4 follow-up (2026-05-15):**
[`benchmarks/RESULTS.md §A3-rev4`](../benchmarks/RESULTS.md) re-ran
the 3-pass protocol with the v2 hybrid-embedding fingerprint
(`PatternConfig.fingerprint_version="v2"`,
`embedding_provider="openai:text-embedding-3-small"`) enabled + a 4th
pass with delegation. **Pass C produced 0 pattern-slot sonnet picks**
(vs §A3-rev3's 1) — v2 wiring in the current code stores STRUCTURAL
fingerprints with the embedding cache warmed *out-of-band* after
`store.record()`, so the routing-time K-NN falls back to v1
weighted-Jaccard via mixed-version detection. The §A3-rev3 regex
inversion didn't reproduce because per-fingerprint cluster
accumulation is non-deterministic across passes (sample-size
asymmetry on the specific fingerprint, the §A3-rev3 caveat #1).
**Wave 10's "pattern store v2 cluster-tightening A/B" deferred item
remains the real gate for whether v2 generalizes the inversion.**
The savings posture stays at the §A3-rev3 calibration — N=1
differentiated demonstration; v2 hasn't extended it yet.
**Pass D (delegation)** did not fire on `multi-turn-refactor` because
slot 4 picked haiku (not the sonnet planner that holds the
`delegate()` tool); delegation savings remain untested end-to-end.
Part A's shutdown-order bug fix in `apps/cli/src/metis_cli/runtime.py`
closed the long-standing §A3-rev3 `architectural-explanation-without-
hallucination` `success_score_count=0` caveat (the workload now
accumulates both haiku and sonnet samples cleanly).

**§A3-rev5 follow-up (2026-05-15):**
[`benchmarks/RESULTS.md §A3-rev5`](../benchmarks/RESULTS.md) re-ran
the 4-pass protocol after Wave 11 closed both §A3-rev4 blockers:
**11b-1** moves embedding compute to turn start
([apps/cli/src/metis_cli/runtime.py:284-318](../apps/cli/src/metis_cli/runtime.py#L284-L318))
so recorded rows land as `kind='hybrid'` (verified: 18 of 18 fingerprints
HYBRID in `a3rev5-patterns.db`, vs §A3-rev4's 70 of 70 STRUCTURAL); **11b-2**
ships [`benchmarks/workloads/multi-step-with-delegation/`](../benchmarks/workloads/multi-step-with-delegation/)
and the `min_delegate_calls` assertion. **Q1 outcome: v2 K-NN now fires
end-to-end at routing time (every Pass C `pattern.matched` reports
`fingerprint_kind="hybrid"`), but Pass C still produces 0 pattern-slot
sonnet picks across 17 routed turns.** The §A3-rev3 regex inversion did
not reproduce. Cross-pass aggregate has the right signal (sonnet
meaningfully ahead on 2 of 7 workloads) but per-fingerprint K-NN
clusters are dominated by haiku's 2-3× sample-size advantage under
`cost_weight=0.1`. Pass C quality sum 5.20 vs Pass A 5.72 — slot 4 routed
regex to haiku where Pass B sonnet would have succeeded (regex Pass C
0.19, Pass B 1.00). The savings posture stays at the §A3-rev3
calibration; v2 HYBRID embeddings are a necessary foundation for
further cluster-tightening (per-prompt fingerprint partitioning, or
`cost_weight` reduction below 0.1) but not sufficient by themselves.
**Q2 outcome: delegation produces 8.3% better cost-per-quality on a
workload designed to exercise it.** Pass D: 3 `delegate.started` events
fire reliably; sonnet planner + haiku workers at $0.221 / quality 0.91
= $0.243/quality-unit, vs sonnet-only-no-delegation at $0.183 / quality
0.69 = $0.265/quality-unit. The 23.9% headline "savings_pct" is the
analytics counterfactual (worker tokens repriced at sonnet rates);
absolute delegation cost is higher than sonnet-only-no-delegation but
quality is higher too. First end-to-end demonstration of delegation
moving cost-per-quality in any A3 series.

**The wedge mechanism shipped.** What's left is dialing up
selectivity. Until further cluster-tightening lands, GTM headline
numbers should quote both Pass C's flat `savings_pct=62.0%` (from
§A3-rev3, the canonical inversion datapoint) *and* the sonnet-on-regex
inversion (the two are reconcilable: the savings gap is the sonnet
pick). Delegation now has its own validated GTM datapoint: 8.3%
cost-per-quality improvement on workloads designed for fan-out
(§A3-rev5), widening to 26.1% in §A3-rev6.

**§A3-rev6 follow-up (2026-05-15):**
[`benchmarks/RESULTS.md §A3-rev6`](../benchmarks/RESULTS.md) re-ran
the 4-pass protocol after Wave 12 dropped the cost_weight default
from 0.1 → 0.05 ([`packages/metis-core/src/metis_core/routing/policy.py:76`](../packages/metis-core/src/metis_core/routing/policy.py#L76)).
**Q1 outcome: cost_weight=0.05 is mechanically correct (the cluster
math now flips haiku → sonnet on two specific Pass C turns, exactly
where the §A3-rev5 brief's simulation predicted) but the resulting
confidence values 0.006–0.009 sit *below* the `min_confidence=0.05`
gate.** Pass C produced **0 sonnet picks across 18 turns** (vs §A3-rev3's
1 of 14). The cluster aggregate on multi-file-refactor turn 2 was haiku
0.810 / sonnet 0.817 (sonnet ahead by 0.007, conf 0.009 → gated off);
on regex turn 2 haiku 0.921 / sonnet 0.926 (sonnet ahead by 0.005,
conf 0.006 → gated off). The Wave 12 fix worked exactly as designed;
the inversion margins are just too narrow to clear a sensible
confidence gate. **Q2 outcome: delegation savings hold and widen.**
Pass D: sonnet planner + haiku workers at $0.227 / quality 0.91 =
$0.249/quality-unit; Pass D-baseline (sonnet-only-no-delegation) at
$0.233 / quality 0.69 = $0.338/quality-unit. **26.1% better
cost-per-quality** (vs §A3-rev5's 8.3%), driven by Pass D workers
running fewer/shorter calls this run plus the baseline staying flat
at 0.69 (workload's `min_delegate_calls=3` assertion failing).
**The savings posture stays at the §A3-rev3 N=1 calibration on the
model-selection lever**, with §A3-rev6 confirming that the lever's
mechanical chain is fully wired and the remaining blocker is
benchmark-suite signal strength, not routing-knob tuning. Total
experiment spend: $1.56.

**At this point in the A3 series (six iterations deep), the next
move is benchmark-suite work, not routing-knob work.** The K-NN
correctly aggregates cross-model outcome history end-to-end. The
benchmark suite's haiku-vs-sonnet quality delta on most workloads is
within run-to-run variance (haiku regex 0.75 vs sonnet regex 0.74 in
Pass A/B of §A3-rev6; haiku multi-turn-refactor 1.00 vs sonnet 0.85
*in favor of* haiku on a workload §A3-rev3 had going the other way).
Three candidate Wave-13 directions named in [§A3-rev6 Q1 finding](../benchmarks/RESULTS.md):
(1) workload signal strengthening — replace marginal workloads with
ones where haiku stably fails (quality 0.3–0.5) and sonnet stably
passes (0.9+); (2) N-shot per workload — 3-5 samples per (workload,
model) in seed passes to reduce the variance the K-NN has to
overcome; (3) per-turn-text fingerprinting on top of workload-tag —
prevent turn-1 outcomes from averaging into turn-2 reads (was ruled
out under cost_weight=0.1, may be worth reconsidering at 0.05).

**Wave 13 / 13a-1 follow-up (2026-05-15):**
[`benchmarks/RESULTS.md §13a-1`](../benchmarks/RESULTS.md) ran the
path-1 (workload signal-strengthening) wedge end-to-end and **ruled it
out as a sufficient single-knob fix**. The cross-run audit across
§A3-rev3..rev6 patterns DBs found no v1 workload with a haiku-vs-sonnet
quality gap ≥ 0.15 (best: `regex-with-edge-cases` at +0.119; worst:
`multi-turn-refactor` at −0.079, a REVERSE-signal training set). Three
purpose-designed haiku-fail candidates (`subtle-bug-fix-with-test`,
`recursive-data-structure-traversal`,
`refactor-with-contract-preservation`) all came in at temperature=0
with gaps ≤ 0.083 under heuristic judging and both at 1.000 under the
hybrid judge — below both the rubric's resolution floor and the LLM
judge's agreement floor. Three plausible interpretations remain on
the table (none ruled in/out by 13a-1): (a) haiku-4.5 is genuinely
strong enough on dev-loop coding tasks that the gap is small at
temperature=0; (b) temperature=0 itself collapses model variance and
higher temperature would widen the gap but break determinism; (c) the
judges have insufficient outcome resolution and a partial-correctness
judge would surface differentiation pass/fail substring matching
erases. 13a-2 ships the harness-side path-2 mechanism
(`scripts/benchmark.py --seed-passes N` with statistical reporting) so
§A3-rev7 can reduce K-NN variance from N samples per cluster; not yet
exercised end-to-end. **13b-1 (§A3-rev7) brief — two paths remain
open:** (a) finer-grained outcome scoring (partial-test-pass mid-scores
0.3–0.7 instead of pass/fail substring detection); (b) task domains
haiku has known weakness in (math/symbolic, long-context multi-document
synthesis, rare API surfaces — none fit the dev-loop theme but might
be the only place a stable gap exists). The savings posture on the
model-selection lever stays at §A3-rev3's N=1 calibration. Total
13a-1 smoke spend: $0.815.

**§A3-rev7 follow-up (2026-05-15, partial / aborted on credits):**
[`benchmarks/RESULTS.md §A3-rev7`](../benchmarks/RESULTS.md) tested
**13b-1's path (a)** (finer-grained outcome scoring) end-to-end after
Wave 14a-1 landed the v1.2 `partial_credit` rubric primitive on 5
workloads. The run aborted partway through Pass B when the Anthropic
account hit a credit-balance exhaustion (HTTP 400). Pass A completed
across all 5 workloads; Pass B completed only on
`subtle-bug-fix-with-test`; Pass C was not executed. **Preliminary
result on the 2 workloads with complete haiku + sonnet partial-credit
data:** `subtle-bug-fix-with-test` haiku 0.950 / sonnet 0.950
(+0.000 gap); `recursive-data-structure-traversal` haiku 1.000 /
sonnet 1.000 on N=1 sonnet (+0.000 gap). The partial-credit rubric
is correctly active (`rubric_version=1.2.0` stamped on every workload
verdict, test_pass_count_ratio parsing pytest summaries as designed)
but produces zero discrimination on workloads where both models
reliably arrive at `N passed / N total`. **Interpretation (c) — the
prior judges had insufficient outcome resolution — is *largely
refuted* on these two workloads.** Interpretation (a) — haiku-4.5 is
genuinely strong on dev-loop coding at temperature=0 — now has
*direct* evidence on the two complete data points. The one residual
signal: `regex-with-edge-cases` Pass A haiku produced 0.63–0.75 across
3 reps (the only workload where partial-credit surfaced mid-scores)
but no Pass B sonnet data landed for direct comparison; if a
topped-up Pass B sonnet lands at 0.95+ the gap clears the
confidence gate easily, and §A3-rev3's regex turn 2 inversion would
generalize. Total §A3-rev7 spend before abort: $1.08 of the budgeted
$3-5. **The model-selection-routing differentiator's status after 7
A3 iterations: mechanically proven end-to-end (§A3-rev3 N=1 stands),
generalization is gated on workload-domain-side rate of producing
measurable haiku-vs-sonnet gaps rather than on any routing-engine
knob.**

**Strategic pivot (2026-05-15, post-§A3-rev7 partial):** the
positioning for the three levers in §1 should shift to reflect 7
iterations of empirical evidence:
1. **Delegation is the validated GTM lever for the routing surface.**
   §A3-rev5 (8.3%) and §A3-rev6 (26.1%) bracket a real
   cost-per-quality range on the `multi-step-with-delegation`
   workload; the headline should quote that range as the
   substantiated savings claim for slot-5 routing.
2. **Model selection (slot 4) is a Phase-4 differentiator** pending
   the second wedge from §13a-1: task domains where haiku-4.5 has
   measurable weakness (math/symbolic, long-context multi-document
   synthesis, rare API surfaces). The mechanism is built; the breadth
   of workloads where it inverts is the bottleneck. §A3-rev3 stands
   as the canonical proof-of-concept demo, not as a regime. The §1
   "rate-card savings *given haiku succeeds*" framing remains
   accurate; the breadth claim cannot be tightened with the current
   benchmark suite.
3. **Context engineering** retains its §1-top spot as the largest
   typical cost lever (prompt-cache discipline, history pruning,
   skill lazy-loading). The shipped 100% cache-hit measurement
   (`benchmarks/RESULTS.md §Run 3`) is the load-bearing savings story
   most buyers will see first.

**GTM headline posture (post-§A3-rev7 partial).** Unchanged from
§A3-rev5 for the model-selection lever (§A3-rev3 N=1 stands as the
canonical proof-of-concept; generalization is gated on
benchmark-suite signal strength + workload-domain selection, not
routing-engine knobs). The §A3-rev7 partial data adds direct
evidence on `subtle-bug-fix-with-test` and
`recursive-data-structure-traversal` that the prior judges'
pass/fail collapse was *not* the bottleneck on these workloads —
both models converge to identical scores under continuous-ratio
partial-credit. Delegation's headline number updates: §A3-rev6
Pass D shows **26.1% better cost-per-quality** on the delegation
workload (vs §A3-rev5's 8.3%); §A3-rev7 did not re-measure (Pass D
failed on credit exhaustion). Both numbers are reproducible; the
8.3% – 26.1% range across two completed runs is the published
delegation headline. **The delegation lever now sits ahead of
model-selection in the GTM ordering.**

**§A3-rev7 completion follow-up (2026-05-16, credits topped up):**
[`benchmarks/RESULTS.md §A3-rev7 completion`](../benchmarks/RESULTS.md)
executed the documented resume recipe — finished Pass B sonnet on
the 4 missing workloads, ran Pass C across all 5 workloads, and
re-ran Pass D + Pass D-baseline. Total completion spend: ~$2.0 of
API (held the budget). Two material updates to the partial
section's conclusions:

1. **The regex-residual-signal prediction did NOT materialize.**
   The partial section predicted that if Pass B sonnet on
   `regex-with-edge-cases` lands at 0.95+, the cluster aggregate
   would have a +0.2 to +0.3 gap "far above the 0.05 confidence
   gate." The completion landed Pass B sonnet at q_mean=0.88 with
   std=0.217 — one of the three reps scored 0.62 (the same failure
   mode haiku fails on, `tool_calls=4 > max_tool_calls=1` + missing
   `PASS 16/16`). Sonnet's own variance at temperature=0 on the
   load-bearing turn drove the cluster aggregate to 0.950, exactly
   tied with haiku's cost-floor-adjusted score. Pass C picked haiku
   on regex 9/9 routing decisions (6 slot-4 + 3 slot-7), with the
   slot-4 confidence sitting at the `min_confidence=0.05` gate.
   **Pass C picked haiku on every single one of 36 routing
   decisions across all 5 workloads; zero sonnet picks anywhere.**
2. **Delegation gets a third independent datapoint at 19.9% better
   cost-per-quality** (Pass D delegation $0.220 / quality 0.91 =
   $0.242/quality-unit vs Pass D-baseline $0.205 / quality 0.68 =
   $0.302/quality-unit). This lands inside the §A3-rev5 (8.3%) /
   §A3-rev6 (26.1%) range and close to the cluster midpoint. 3
   `delegate.started` events fired as designed. The 8.3% – 26.1%
   – 19.9% triplet across three runs makes delegation the most
   reproduced cost-per-quality lever in the §A3 series.

**Newly visible from the completion: sonnet variance as a
co-factor in the generalization bottleneck.** The partial section's
"haiku-vs-sonnet gap rate" framing was accurate but incomplete. The
actual generalization gate is the *product* of two distributions:
(i) the rate at which haiku-4.5 fails at temperature=0, and (ii)
the rate at which sonnet-4.6 *succeeds* at temperature=0 on the
same workload. Even on the one workload where partial-credit
surfaces haiku failures (regex 0.63-0.75), sonnet's ~33% failure
rate on the hard turn prevents the K-NN cluster aggregate from
clearing the gate. **Interpretation (c) — insufficient judge
resolution — is now refuted on all 5 workloads in the partial-
credit-enabled suite, not just 2.** The remaining wedge candidate
remains "task domains haiku has known weakness in" (math/symbolic,
long-context multi-document, rare API surfaces) where the haiku-
vs-sonnet outcome distance is large enough that sonnet's variance
can't close it.

**GTM headline posture update (post-§A3-rev7 completion).** The
ordering and content of the three levers in §1 holds. The
delegation headline tightens from "8.3% – 26.1% range across two
runs" to "8.3% – 26.1% range across three runs with a 19.9%
midpoint" — three independent measurements stabilize the claim
the buyer-facing one-pager already uses. The model-selection lever
posture is unchanged: §A3-rev3 N=1 stands as the canonical
mechanism demonstration; generalization is gated on workload-
domain choice outside the dev-loop theme, and §A3-rev7 completion
ruled out the last routing-knob-or-rubric repair candidate.

**GA launch posture (2026-05-16, Wave 16).** The planned 5-7-wave
cadence reaches GA at Wave 16. The launch story should lead with the
buyer-visible primitives that are now live: transparent gateway,
per-user / per-team attribution, compliance / redaction / retention,
observability, billing self-service, concierge reporting, and day-1
operations. The first paid cohort remains owner-driven — the repo now
has the runbooks, anonymized report path, and support/billing/status
page artifacts for those conversations, but it does not claim completed
customer onboardings. The optional §A3 task-domain wedge is deliberately
post-GA research; do not delay launch on it.

## 2. Buyer ≠ user

**The buyer is the budget owner — an engineering leader or CTO. The user is the dev who runs `metis chat` (or whatever the eventual surface is).**

Confirmed 2026-05-12: *"the buyer's AI usage cost."*

This is a B2B product, not a personal tool. Consequences:

- **Multi-user from day one is real**, not optional. The HTTP/WS surface that's already shipping is load-bearing for the buyer story.
- **Team-level cost attribution** matters. Per-dev, per-project, per-task-class rollups need to land before any GTM conversation.
- **Policy enforcement, not just policy explanation.** The routing engine today is built for *explainability to the user* (full chain trace per turn). A buyer wants *enforcement* — "no one in marketing can use Opus" — which is a different mode the routing engine doesn't natively support.
- **Audit and compliance posture.** Wave 12 ships the buyer-facing artifacts: [`docs/operations/soc2-readiness.md`](operations/soc2-readiness.md) is the SOC2 Trust Service Criteria gap audit (TSC categories CC1–CC9, A1, C1, PI1, P1–P8 mapped against shipped + buyer-responsibility evidence; honest about CC8 change management, third-party pentest, vendor review, and SOC2 auditor-engagement gaps), and the Wave 12 spec triad closes the retention / redaction / forget gaps named in [`docs/specs/multi-user.md §7.4`](specs/multi-user.md): [`audit-log.md`](specs/audit-log.md) (9-event v1 subset + `metis audit export` JSONL/CSV), [`trace-retention.md`](specs/trace-retention.md) (90-day default sweep with audit-event exemption + `metis trace prune` + helm CronJob), [`redaction.md`](specs/redaction.md) (4-mode `EventRedactor` + `metis user forget` Article 17 pseudonymization-as-erasure). [`docs/operations/compliance-overview.md`](operations/compliance-overview.md) is the one-page buyer-conversation index. Type 1 readiness target is Q3 2026 contingent on a buyer underwriting the audit fee; Type 2 Q4 2026 / Q1 2027.
- **Deployment story.** `uv run metis serve` on a dev's laptop isn't the install. The product needs a server-in-a-box (Docker, helm, or SaaS) — TBD which.
- **Proof of savings.** This is the artifact that closes the deal.
  Benchmark evidence now supports the launch claim (delegation at
  8.3% / 19.9% / 26.1% better cost-per-quality across three runs;
  model selection as §A3-rev3 proof-of-mechanism only). The remaining
  buyer-side gap is live before/after case-study evidence from the
  first paid cohort; Wave 16 adds the anonymized `customer-report`
  path and case-study templates so the owner can collect it without
  exposing customer identifiers.

What doesn't change: local-first as a *deployment* property (their infra, their keys, their data) is still a feature. But "local-first by default" as a *user* property doesn't apply to the buyer.

## 3. The open architectural fork

**Replacement agent vs. transparent gateway.** **Resolved 2026-05-13 — hybrid (gateway first → agent upgrade).** See [`docs/specs/deployment-shape.md`](specs/deployment-shape.md) for the rationale and [`docs/specs/gateway.md`](specs/deployment-shape.md) for the surface skeleton. The analysis below is preserved as historical context; the answer is in the spec.

The current build is closer to the first; the market dynamics favor the second.

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
- **Defensible wedge** is the four-leg moat:
  1. Bounded agent-curated memory (Letta is the only Series-A peer; everyone else uses unbounded vector slop)
  2. Lossless canonical message format (LiteLLM has bug-of-the-week on this surface)
  3. Task-fingerprint pattern learning (no one ships this; spec at [`docs/specs/pattern-store.md`](specs/pattern-store.md); slot 4 wired Phase 2.5, differentiator demonstrated end-to-end in [`benchmarks/RESULTS.md §A3-rev3`](../benchmarks/RESULTS.md))
  4. Auto-derived skill curation (no one ships this either; spec at [`docs/specs/skill-curator.md`](specs/skill-curator.md); Phase 4 implementation gated on Phase 2.5 agent-authored skills — `skill_save` tool + `skill.created(source="auto_generated")` event)
- **Cost optimization is the metric; learning is the mechanism.** The headline isn't "smart routing for cost." The headline is "the agent that gets cheaper the longer you use it because it learns your workload" — savings as the *outcome* of the differentiating mechanics. Legs 3 and 4 compose: pattern learning picks the right model per task class; skill curation keeps the skill library that informs those tasks pruned and current. Each makes the other more valuable.

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
| 2026-05-13 | Savings benchmark methodology defined | Specced in [`docs/specs/benchmark.md`](specs/benchmark.md). Three workloads (fix-a-bug, write-a-doc, multi-turn-refactor) under `benchmarks/workloads/`; `scripts/benchmark.py` drives them, writes to a benchmark-only trace DB, and reports `actual_repriced_usd` / `baseline_repriced_usd` via the same `AnalyticsStore.savings()` the dashboard uses. Closes §6.4. |
| 2026-05-13 | Adopt hybrid deployment (gateway first → agent upgrade) | Specced in [`docs/specs/deployment-shape.md`](specs/deployment-shape.md); gateway surface skeleton in [`docs/specs/gateway.md`](specs/gateway.md). Gateway is ~5–8 engineer-weeks of new code on top of `metis-core` (canonical IR / adapters / routing / pricing / trace all reusable), gives the high-floor sale (env-var flip, savings within hours) and turns the canonical-IR moat into a real differentiator versus LiteLLM / Portkey / Helicone (all three intercept HTTP only and have documented or strongly-suspected fidelity gaps on Anthropic blocks). Replacement agent stays alive as "Metis Pro" — the upgrade path for buyers who already see savings and want the context + skills + memory levers. Closes §6.1; narrows §6.3. |
| 2026-05-14 | Pattern store mechanics specced | Specced in [`docs/specs/pattern-store.md`](specs/pattern-store.md). Per-workspace bounded SQLite store at `<workspace>/.metis/patterns.db`; structural-only v1 fingerprint (file extensions / tool names / side-effect classes / token bucket / intent regex tags); 5k soft / 10k hard / 180-day caps with hard-cap auto-evict (asymmetric with memory-store because pattern writes are mechanical projections); K-NN retrieval with weighted Jaccard + sample-size-weighted cluster aggregation implementing routing-engine §5.5 verbatim; three new `pattern.*` event types pending catalog addition at Phase 2.5 implementation. Embedding-provider-abstract; v2 hybrid lands data-only. Closes §6.6. |
| 2026-05-14 | Evaluator scope specced | Specced in [`docs/specs/evaluator.md`](specs/evaluator.md). Heuristic-first / hybrid-LLM-as-judge feedback loop across four subject kinds (turn / tool_cycle / session / workload). `EvalVerdict` carries a single `score` in `[0, 1]` plus `confidence` gate, opaque `signals` dict, versioned rubric; cost-capped (per-session $0.10 / per-day $1.00 defaults) and append-only (re-evaluation produces new verdicts, not mutations). Pattern store consumes verdicts async via `PatternStore.update_score(turn_id, ...)`; latest-verdict join is `MAX(eval_id)` per subject (reconciliation sweep 2026-05-14). One new `/analytics/quality` endpoint + additive `include_eval` on `/analytics/cost` to land at Phase 3 implementation. Closes §6.7. |
| 2026-05-14 | Pattern-store ↔ evaluator reconciliation pinned | See `docs/specs/CHANGES.md` "2026-05-14 — Pattern-store ↔ evaluator reconciliation sweep" for the five pinned items: verdict shape (evaluator owns), async timing via `update_score()` joined on `turn_id`, confidence-gate filter in pattern-store config with default `0.5`, sample-size-weighted mean clarified in routing-engine §5.5, `MAX(eval_id)` as latest-verdict rule documented in pattern-store §10.4. |
| 2026-05-14 | §A3 documents the model-selection lever's current ceiling | [`benchmarks/RESULTS.md §A3`](../benchmarks/RESULTS.md) — first three-pass benchmark where slot 4 fires on essentially every turn (17 of 18) reading cross-model outcomes; result: still picks haiku everywhere. Pass C cost-per-quality-unit `$0.0477` is the same shape as single-model haiku; the differentiator does not invert under hybrid-0.7 + the v1 heuristic + the current `signals_extra` plumbing. Identifies two follow-up unblocks (heuristic learns `tool.completed.success=False`; bus subscriber forwards `assistant_response_text` to the LLM judge), either sufficient alone. Adjusts §1's quoted savings posture to "rate-card savings *given haiku succeeds*." Total experiment spend: $1.026. |
| 2026-05-14 | §A3-rev: both unblocks landed, differentiator *still* doesn't invert | [`benchmarks/RESULTS.md §A3-rev`](../benchmarks/RESULTS.md) — re-runs §A3's three-pass protocol after the two §A3 follow-up unblocks landed (heuristic penalty for `tool.completed.success=False`; `SessionManager` forwarding `user_prompt_text` + `assistant_response_text` on `turn.completed.signals_extra`). Both unblocks fire at the per-turn level: 15 hybrid escalations across passes (vs 0 in §A3-original); LLM judge produces differentiated 0.3 / 0.4 / 0.7 / 0.8 / 1.0 scores reading real assistant text; heuristic surfaces +0.25 sonnet/haiku quality delta on `regex-with-edge-cases`. But Pass C slot 4 picks haiku on all 15 routed turns (vs 17 of 17 in §A3-original) — the K-NN aggregation across mixed-workload clusters plus `cost_weight=0.3` cancels the per-workload signal (haiku-aggregated 0.755–1.000 vs sonnet-aggregated 0.245–0.700 on every cluster). Pass C cost-per-quality-unit `$0.0452`; essentially unchanged from haiku-only. Identifies a third unblock: K-NN clustering at workload granularity (or `cost_weight` reduction to ~0.1 via policy knob). Total experiment spend: $1.032. §1 quoted savings posture is unchanged ("rate-card savings *given haiku succeeds*"). |
| 2026-05-14 | Gateway v1 shipped | Transparent HTTP gateway ([`apps/gateway/`](../apps/gateway/)) exposes `POST /v1/chat/completions` (OpenAI shape) and `POST /v1/messages` (Anthropic shape), each in sync + SSE flavors, routed via `metis_core.routing.RoutingEngine` with `gateway_key_id` + `inbound_shape` stamped on every `llm.call_completed` / `turn.completed`. Per-request stateless (no session manager / tool dispatcher / memory store / skill loader); loopback-only bind. `metis gateway issue-key` creates keys; the keystore stores SHA-256 hashes, the plaintext token is printed once. Live-validated on 2026-05-14 at ~$0.0002 / 4 calls (OpenAI + Anthropic shapes, sync + SSE) with per-key cost roll-up confirmed via direct SQL on the trace DB. This is the §3 hybrid's "gateway first" leg in production-shape; §6.3 (local-first vs SaaS) **remains open** — the gateway can be deployed in either posture and no GTM evidence has pinned the choice. Follow-on: the `group_by=gateway_key` dimension on `/analytics/cost` (gateway.md §V) is not yet wired; per-key analytics today requires direct SQL. |
| 2026-05-14 | §A3-rev3: differentiator inverts on 1 workload | [`benchmarks/RESULTS.md §A3-rev3`](../benchmarks/RESULTS.md) — re-runs the three-pass protocol after Wave 9's one-line knob landed (`PatternConfig.min_confidence: 0.3 → 0.05`, [`routing/policy.py:63`](../packages/metis-core/src/metis_core/routing/policy.py#L63)). Pass C reaches slot 4 on **14 of 18 turns** (vs §A3-rev2's 3 of 16) and **picks sonnet on `regex-with-edge-cases` turn 2** — cluster haiku=0.784, sonnet=0.833, confidence=0.058 (above the new 0.05 gate, below the old 0.3). First end-to-end demonstration of differentiated routing in any A3 series. Pass C `savings_pct=62.0%` (vs flat-haiku 66.7%); regex row 35.5%. Pass C quality sum 5.55 beats Pass A's 5.16 (+8%) at cost-per-quality `$0.0477` (between haiku-only `$0.0383` and sonnet-only `$0.1176`). Wave 8a's three unblocks (workload-tag partition, `cost_weight=0.1`, grounding-check) remain load-bearing; Wave 9's knob is the missing piece, not a replacement. Adjusts §1's quoted savings posture from "rate-card savings *given haiku succeeds*" to "differentiated routing picks the succeeding model on 1 of 6 evaluable workloads; quality > haiku-only at 40% of sonnet-only cost." Total experiment spend: $1.138. |
| 2026-05-14 | Skill curator specced; moat reframed as four legs | New spec [`docs/specs/skill-curator.md`](specs/skill-curator.md) (~620 lines, pattern lifted from hermes-agent's `agent/curator.py`). Periodic auxiliary-model maintenance of agent-authored skills only: six actions (pin / unpin / archive / restore / consolidate / edit); never auto-deletes (archive is `mv` to `skills-archive/`, restoration is `mv` back); user-authored skills are read-only (touchability gated by `skill.created.source ∈ {auto_generated, curator_generated}`); pinned bypasses every auto-transition; no SKILL.md frontmatter changes (state in sidecar JSON, preserves agentskills.io conformance); bounded spend via shared `BudgetTracker` (curator caps `$0.50/run`, `$1.00/day` independent of evaluator caps); one new `skill.curated` event + two run-boundary events. Defaults match Hermes empirics: weekly interval, 30-day stale soft annotation, 90-day archive hard threshold. **Implementation is Phase 4 (Wave 17), gated on Phase 2.5 agent-authored skills (`skill_save` tool + `skill.created(source="auto_generated")` event) landing first — that prereq is itself not yet planned and is also not GA-blocking.** §4 reframed: the moat now lists four legs (added "auto-derived skill curation"); legs 3 and 4 compose (pattern learning picks the right model per task class; skill curation keeps the skill library that informs those tasks pruned and current). No code changes in this entry — spec-only, AGENTS.md / CHANGES.md updated. |
| 2026-05-15 | §A3-rev4: v2 wiring partial, inversion didn't generalize; eval-to-store outcome bug closed | [`benchmarks/RESULTS.md §A3-rev4`](../benchmarks/RESULTS.md) — 4-pass protocol with `PatternConfig.fingerprint_version="v2"` + `embedding_provider="openai:text-embedding-3-small"` plus a 5th pass with `--delegation-policy sonnet-planner-haiku-worker` on `multi-turn-refactor`. **Pass C produced 0 pattern-slot sonnet picks** (vs §A3-rev3's 1 on regex turn 2). Root cause: v2 wiring stores STRUCTURAL fingerprints with the embedding cache warmed *out-of-band* after `store.record()`, so routing-time K-NN falls back to v1 weighted-Jaccard via mixed-version detection. The "v2 cluster-tightening A/B" deferred Wave-10 item remains the real Q1 gate. **Pass D (delegation)** didn't fire because slot 4 picked haiku (which lacks `can_delegate=True`); routing-then-delegate composition needs the planner forced via `--model sonnet` to test Q2 end-to-end. **Two correctness fixes landed alongside**: (i) `shutdown_runtime` now drains *before* detaching subscribers ([apps/cli/src/metis_cli/runtime.py](../apps/cli/src/metis_cli/runtime.py)), closing the long-standing §A3-rev3 `architectural-explanation-without-hallucination` `success_score_count=0` caveat (architectural now accumulates both haiku and sonnet samples cleanly); (ii) `EventBus.stop()` drains before setting `_stopping=True` ([packages/metis-core/src/metis_core/events/bus.py](../packages/metis-core/src/metis_core/events/bus.py)), eliminating a deadlock when unregister events sat in queue at stop time. New benchmark flags `--fingerprint-version`, `--embedding-provider`, `--delegation-policy` ship in [scripts/benchmark.py](../scripts/benchmark.py). The savings posture stays at §A3-rev3's calibration — v2 has not yet extended the differentiation. Total experiment spend: $1.30. |
| 2026-05-15 | §A3-rev5: v2 HYBRID lands end-to-end, inversion still doesn't generalize; delegation Q2 improves cost-per-quality 8.3% | [`benchmarks/RESULTS.md §A3-rev5`](../benchmarks/RESULTS.md) — 4-pass protocol re-run after Wave 11 closed both §A3-rev4 blockers. **11b-1** (recording-side HYBRID): turn-start `fingerprint_inputs_hook` precomputes the embedding so `compute_fingerprint` produces `kind='hybrid'` rows at `store.record()` ([apps/cli/src/metis_cli/runtime.py:284-318](../apps/cli/src/metis_cli/runtime.py#L284-L318), [packages/metis-core/tests/patterns/test_v2_recording_wiring.py](../packages/metis-core/tests/patterns/test_v2_recording_wiring.py)) — verified: 18 of 18 fingerprints in `a3rev5-patterns.db` are HYBRID (vs §A3-rev4's 70 of 70 STRUCTURAL). **11b-2** (delegation workload): [`benchmarks/workloads/multi-step-with-delegation/`](../benchmarks/workloads/multi-step-with-delegation/) ships with `--model sonnet` forcing the planner non-None + `min_delegate_calls: 3` assertion. **Q1: Pass C produces 0 pattern-slot sonnet picks across 17 routed turns** — v2 K-NN actually fires at routing time (every `pattern.matched` reports `fingerprint_kind="hybrid"`, no v1 fallback) but per-fingerprint clusters are dominated by haiku's 2-3× sample-size advantage under `cost_weight=0.1`. Cross-pass aggregate has the right signal (sonnet ahead on 2 of 7 workloads, including regex +0.117) but K-NN sees larger haiku populations per cluster. The §A3-rev3 regex inversion did not reproduce. Pass C quality sum 5.20 (haiku-only Pass A 5.72, sonnet-only Pass B 5.98); regex Pass C 0.19 (rubric fail under haiku) where Pass B sonnet scored 1.00. v2 HYBRID embeddings are a *necessary* foundation for further cluster-tightening (per-prompt fingerprint partitioning, or `cost_weight` reduction below 0.1) but not sufficient. **Q2: delegation produces 8.3% better cost-per-quality on a workload designed to exercise it.** Pass D: 3 `delegate.started` events fire reliably; sonnet planner + haiku workers at $0.221 / quality 0.91 = **$0.243/quality-unit**, vs sonnet-only-no-delegation at $0.183 / quality 0.69 = $0.265/quality-unit. The 23.9% "savings_pct" headline is the analytics counterfactual (worker tokens repriced at sonnet rates); absolute delegation cost is higher than sonnet-only-no-delegation but quality is higher too. First end-to-end demonstration of delegation moving cost-per-quality in any A3 series. The savings posture stays at §A3-rev3 for the model-selection lever; delegation now has its own validated GTM datapoint. Total experiment spend: $1.45. |
| 2026-05-15 | §A3-rev6: cost_weight=0.05 mechanically correct, inversion still doesn't generalize; delegation Q2 widens to 26.1% | [`benchmarks/RESULTS.md §A3-rev6`](../benchmarks/RESULTS.md) — 4-pass protocol re-run after Wave 12 dropped the `PatternConfig.cost_weight` default from `0.1` → `0.05` ([`packages/metis-core/src/metis_core/routing/policy.py:76`](../packages/metis-core/src/metis_core/routing/policy.py#L76)). **Q1: Pass C produces 0 pattern-slot sonnet picks across 18 routed turns.** The cluster math behaves *exactly* as the §A3-rev5 brief's direct simulation predicted: cw=0.05 flips the cluster aggregate haiku → sonnet on two specific turns where sonnet has a tiny quality edge (multi-file-refactor turn 2: haiku 0.810 / sonnet 0.817; regex turn 2: haiku 0.921 / sonnet 0.926). But the resulting confidence values (0.009 and 0.006 respectively) sit *below* the `min_confidence=0.05` gate, so slot 4 gates off → slot 7 → haiku. The §A3-rev3 inversion did not reproduce. **The diagnosis at six A3 iterations:** all previously-identified routing-engine mechanical blockers (workload-tag partitioning, cost_weight floor, grounding-check rubric, min_confidence reduction, v2 HYBRID recording) are live and verified at both the per-unit-test and live-run layers. The remaining bottleneck is benchmark-suite signal strength — when sonnet outperforms haiku on a workload, the per-turn quality delta is typically 0.05–0.15, narrowing to 0.01–0.05 after K-NN aggregation across same-workload neighbors, producing confidence right at the noise-protective gate's edge. Pass A regex haiku 0.75 vs Pass B sonnet 0.74 means there's effectively no signal to invert on for regex; Pass A multi-turn-refactor haiku 1.00 vs Pass B sonnet 0.85 actively rewards haiku, the *opposite* of §A3-rev3's direction. Next-move candidates named in §A3-rev6 Q1 finding: (1) workload signal strengthening (replace marginal workloads with ones where haiku stably fails 0.3–0.5 / sonnet stably passes 0.9+); (2) N-shot per workload to reduce the variance the K-NN has to overcome; (3) per-turn-text fingerprinting on top of workload-tag (ruled out under cw=0.1, may be worth reconsidering at cw=0.05). **§A3-rev6 ruled out:** the cost_weight halving as a sufficient unblock for generalized inversion. The model-selection savings posture stays at §A3-rev3's N=1 calibration; §A3-rev6 confirms the mechanical chain is fully wired and reframes the remaining work as benchmark-suite, not routing-knob. **Q2: delegation widens to 26.1% better cost-per-quality** (vs §A3-rev5's 8.3%) on the same `multi-step-with-delegation` workload — sonnet planner + haiku workers at $0.227 / quality 0.91 = **$0.249/quality-unit**, vs sonnet-only-no-delegation at $0.233 / quality 0.69 = $0.338/quality-unit. The widening is dominated by Pass D workers running fewer/shorter calls this run (6 calls vs §A3-rev5's 9) while the baseline stays flat at 0.69 (`min_delegate_calls=3` assertion failing). The 12.7% "savings_pct" headline is the analytics counterfactual (smaller than §A3-rev5's 23.9%); the cost-per-quality story is the load-bearing one. GTM headline for delegation should quote the 8.3% – 26.1% range across two reproducible runs, not a single point. Total experiment spend: $1.5647. |
| 2026-05-16 | §A3-rev7 completion: regex residual-signal prediction failed; delegation lever gets third datapoint at 19.9% cost-per-quality (within §A3-rev5/rev6 range) | [`benchmarks/RESULTS.md §A3-rev7 completion`](../benchmarks/RESULTS.md) — executed the documented resume recipe after credit top-up. Finished Pass B sonnet on the 4 missing workloads (regex q_mean=0.88 NOISY std=0.217 / contract 0.98 / mfile 0.94 / recursive 1.00), ran Pass C across all 5 workloads with `--no-active-model`, ran Pass D + Pass D-baseline. **Pass C: zero sonnet picks across 36 routing decisions** (15 of 36 reached slot 4; all 15 picked haiku). The partial section's load-bearing prediction — that a topped-up Pass B sonnet on `regex-with-edge-cases` would land at 0.95+ and produce a +0.2-to-+0.3 cluster gap clearing the confidence gate — *did not materialize*. The completion landed Pass B sonnet at q_mean=0.88 with one rep at 0.62 (same failure mode as haiku: `tool_calls=4 > max_tool_calls=1`, missing `PASS 16/16`). Sonnet's own ~33% failure rate at temperature=0 on the hard turn drove the cluster aggregate to 0.950 — *exactly tied* with haiku's cost-floor-adjusted score, confidence sitting at the `min_confidence=0.05` gate, slot 4 chose haiku 6/9 routing decisions on regex. **New visible factor:** the generalization bottleneck is the product of two distributions, not one — haiku's failure rate AND sonnet's success rate at temperature=0; even on the workload where partial-credit surfaces haiku failures (regex 0.63-0.75), sonnet's own variance closes the K-NN-visible gap. **Q2 delegation re-measurement:** Pass D delegation $0.220 / quality 0.91 = $0.242/quality-unit vs Pass D-baseline $0.205 / quality 0.68 = $0.302/quality-unit = **−19.9% cost-per-quality**. Third independent measurement of the delegation differentiator; lands inside the §A3-rev5 (8.3%) / §A3-rev6 (26.1%) range. 3 `delegate.started` events fired as designed. **Cumulative model-selection scoreboard now reads:** 8 A3 iterations, 1 N=1 inversion (§A3-rev3), 0 of 7 reproductions. **Interpretation (c) refuted on all 5 partial-credit workloads, not just 2.** The remaining wedge candidate is unchanged: task domains haiku has known weakness in (math/symbolic, long-context multi-document, rare API surfaces) outside the dev-loop theme. Completion spend: ~$2.0 of API (held the partial section's budget estimate). §1 update: delegation headline tightens from "8.3% – 26.1% range across two runs" to "8.3% – 26.1% with 19.9% midpoint across three runs"; model-selection posture unchanged at §A3-rev3 N=1 calibration. |
| 2026-05-15 | §A3-rev7: finer-grained outcome scoring tested (partial run, aborted on credits); preliminary evidence rules out interpretation (c) on 2 of 5 partial-credit workloads | [`benchmarks/RESULTS.md §A3-rev7`](../benchmarks/RESULTS.md) — 4-pass protocol designed against the 5 partial-credit-enabled workloads after Wave 14a-1 landed the v1.2 `partial_credit` rubric primitive (evaluator.md §5.4). The run aborted partway through Pass B when the Anthropic account hit a credit-balance exhaustion (HTTP 400). Pass A completed across all 5 workloads × 3 seed-passes; Pass B completed only on `subtle-bug-fix-with-test` (3 reps) + `recursive-data-structure-traversal` (1 of 3 reps before the credit error); Pass C was not executed (no point: 3 of 5 workloads had zero sonnet samples in the patterns DB). **Preliminary Q1 outcome on the 2 workloads with complete haiku + sonnet partial-credit data:** `subtle` haiku 0.950 / sonnet 0.950 (+0.000 gap); `recursive` haiku 1.000 / sonnet 1.000 (+0.000 gap on N=1 sonnet sample). The partial-credit rubric is correctly active end-to-end (`rubric_version=1.2.0` stamped on every workload verdict, pytest summary parsing extracting test_pass_count_ratio as designed) but produces zero discrimination on workloads where both models reliably hit `N passed / N total` at temperature=0. **Interpretation (c) from §A3-rev6 / 13a-1 (finer-grained scoring would surface the gap) is *largely refuted* on these two workloads.** Interpretation (a) (haiku-4.5 is genuinely strong on dev-loop coding at temp=0) now has direct positive evidence. **Residual signal:** `regex-with-edge-cases` Pass A haiku produced 0.63-0.75 across 3 reps (the only workload where partial-credit surfaced mid-scores); the missing Pass B sonnet samples would directly test whether the gap is large enough to clear the confidence gate post-K-NN aggregation. Total §A3-rev7 spend: $1.08 (of budgeted $3-5; the run never reached Pass C). **Strategic pivot:** after 7 A3 iterations the model-selection-routing differentiator's posture stabilizes — mechanism proven (§A3-rev3 N=1), generalization gated on the workload-domain-side rate of measurable haiku-vs-sonnet gaps rather than any routing-engine knob. §1 framing pivots: delegation (slot 5) is the validated GTM lever for the routing surface (8.3-26.1% cost-per-quality range across §A3-rev5 / §A3-rev6); model-selection (slot 4) becomes a Phase-4 differentiator pending §13a-1's path-2 wedge — task domains where haiku has known weakness (math/symbolic, long-context multi-document, rare API surfaces); context engineering retains §1-top spot as the largest typical lever (Run 3's 100% cache-hit demo). |
| 2026-05-16 | Adopt pricing model (open-core gateway + per-seat Pro + reserved enterprise %-of-savings add-on) | Owner ratified [`docs/specs/pricing.md §5.5.4`](specs/pricing.md) — recommended hybrid carries the lowest-friction OSS adoption story (gateway free), the predictable per-seat shape buyers prefer for the Pro tier (multi-user identity, hard caps, audit log export, hosted dashboard, the agent), and a reserved enterprise add-on that scales bill to outcomes for procurement-led buyers via the shipped savings counterfactual ([`analytics-api.md §4.7`](specs/analytics-api.md)). Composes with the multi-user identity layer + per-team budget cap primitives without new metering subsystems. Implementation lands as Wave 15 (billing module at [`apps/gateway/src/metis_gateway/billing/`](../apps/gateway/src/metis_gateway/billing/), Stripe-backed: Subscription for Pro per-seat, metered usage records for the enterprise add-on). Price points stay deferred to first-buyer triangulation; this entry ratifies model *shape*, not numbers. Retires §6.8. |
| 2026-05-16 | Wave 15 shipped: GA-blockers closed + concierge tools + billing module + observability extensions + status-page recipe; phase-claim stays "ready-for-review" | Wave 15 ships the operational closure of the [Wave-14 GA-readiness audit](operations/ga-readiness-audit.md): GA blocker 1 ([NETWORK error refinement, `routing-engine.md §4.5.1`](specs/routing-engine.md) — single SSL hiccup no longer blacks out the whole provider; 2-within-30s sliding-window escalation required, AUTH still immediate) and GA blocker 2 ([gateway model normalization, `gateway.md §4.8`](specs/gateway.md) — SDK-canonical bare names like `claude-3-5-haiku-20241022` get the canonical provider prefix prepended before slot-1 resolution, ending the ~6× cost over-report). `metis customer-report` + `metis trial-status` CLIs ship alongside [`concierge-onboarding.md`](operations/concierge-onboarding.md) for the first-buyer trial → conversion flow; `customer_tier` is added to the keystore + `GatewayKeyIssued` payload as an additive support-context tag, **not an entitlement gate**. The Wave 15 billing module ([`apps/gateway/src/metis_gateway/billing/`](../apps/gateway/src/metis_gateway/billing/)) lands per the just-ratified `pricing.md §5.5.4` with Stripe-backed Pro per-seat + Enterprise %-of-savings metered usage + six new `billing.*` audit-flagged event types + `QuotaConfig.tier`-axis quota composition (free $5/mo cap; pro / enterprise unlimited at the tier level). [`observability.md`](specs/observability.md) bumps `v1 → v1.1` with latency-percentile histograms for routing + tool dispatch, dedicated LLM/tool error counters, the new `gateway.auth_failed` audit-flagged event + per-key cost counter, four `PrometheusRule` alert templates (LLM p99 latency, LLM error rate, gateway auth-failure rate, per-key spend anomaly), a 13-panel Grafana dashboard JSON, and [`observability-runbook.md`](operations/observability-runbook.md). [`status-page.md`](operations/status-page.md) picks up the "Live deployment" recipe (helm Uptime Kuma sidecar gated on `statusPage.enabled: false` default) + four monitoring probes (`/healthz` HTTP, synthetic `POST /v1/messages` with a capped key, `/metrics` HTTP-keyword on `metis_gateway_keys_active`, gateway-key liveness via Kuma Push + `gateway.key_rotated` audit-event correlation) + SEV-mapped templates quoting `incident-response.md §Severity levels` verbatim. **Hosting account remains owner-side** — `https://status.2sum.ai` is the target hostname but DNS / TLS / SaaS-account provisioning are not automated in this entry. **Phase-claim posture unchanged**: [`phase-claim-proposal.md`](operations/phase-claim-proposal.md) is unchanged from Wave 13 and no owner sign-off is recorded; AGENTS.md and README.md status sentences stay "ready-for-review" pending that decision. 1722 → 1829 tests passing. |
| 2026-05-16 | Phase 3 shipped ratified (Position B) | Owner accepted the recommendation in [`docs/operations/phase-claim-proposal.md`](operations/phase-claim-proposal.md): claim **Phase 3 shipped** because gateway, multi-user attribution, and evaluator are live end-to-end with buyer-facing value; do **not** claim "Phase 4 v1 started" because delegation v1 does not equal the Tauri / public UX / marketplace scope from the project overview. Status mirrors in AGENTS.md and README now use this posture. |
| 2026-05-16 | Wave 16 GA launch milestone reached | Wave 16 closes the planned GA cadence: billing self-service portal + plan changes + failed-payment grace, first-customer concierge scaffolding + `customer-report --anonymize`, industry case-study templates, product-site launch blog + pricing refresh, sales collateral refresh, status-page config artifact, launch-day playbook, pre-launch dry-run checklist, and support-channel templates. The §A3 task-domain wedge is deferred post-GA; delegation remains the validated routing-surface lever for launch. Test count refreshed to 1841 passed / 1 skipped. |

## 6. Open questions (decisions deferred)

These are **live**. AI agents working in the repo should not unilaterally close them — surface to the owner.

1. ~~**Replacement agent vs. gateway** (or both). See §3.~~ **Resolved 2026-05-13 — hybrid (gateway first → agent upgrade).** See [`docs/specs/deployment-shape.md`](specs/deployment-shape.md). The gateway lands as the Phase 2 wedge; the agent stays alive as the upgrade tier. Both deployments compose the same `metis-core` substrate so the engineering does not double-cost.
2. **Buyer profile.** 20-dev startup CTO vs. 200-dev enterprise eng leader want very different products (the latter wants SOC2/governance/audit). Anchoring on one narrows the build. Current default lean: startup-CTO first.
3. **Local-first vs. SaaS deployment.** Local-first is a feature for individuals; many B2B buyers actively prefer SaaS (one bill, one vendor relationship, no infra). The commitment costs the easiest GTM path. Worth deciding consciously. **Narrowed by §6.1 (resolved 2026-05-13):** the hybrid's gateway-first GTM implies a deployed-instance posture (in-VPC or SaaS), not strict laptop-local. Local-first remains a *deployment* property (BYO keys, BYO infra) but the v1 gateway product is "a Metis instance the buyer can point clients at." **Further narrowed 2026-05-14:** the Wave 6 gateway Docker image + helm chart ([`infra/gateway/helm/`](../infra/gateway/helm/), [`docs/gateway-deployment.md`](gateway-deployment.md)) make the in-VPC posture production-supported on the same artifacts a SaaS deploy would use — local-first, in-VPC, and SaaS now compose from one runtime container. The remaining choice — which posture to default to in GTM positioning — stays open pending buyer-conversation evidence; this is no longer an engineering-shape question. See [`docs/specs/deployment-shape.md §6`](specs/deployment-shape.md).
4. ~~**Savings benchmark.**~~ **Resolved 2026-05-13** — see [`docs/specs/benchmark.md`](specs/benchmark.md). Three-workload suite under `benchmarks/workloads/`; `scripts/benchmark.py` drives the loop end-to-end against real APIs, writes to a benchmark-only trace DB, and prints `actual_repriced_usd` / `baseline_repriced_usd` / `savings_pct` via the same `AnalyticsStore.savings()` method that backs the `/analytics/savings` HTTP handler. Determinism is approximate, not strict (LLM variance even at `temperature=0`); v1 documents the tolerance window. Open follow-ups (golden reports, per-provider suites) tracked in benchmark.md §11.
5. **Context-assembler design.** The biggest cost lever (per §1) has no spec. What's the algorithm for: skill loading (description-match vs activation), history compression vs drop, prompt-cache breakpoint placement, behavior near the context window? Each has direct $$ consequences.
6. ~~**Pattern store mechanics.**~~ **Resolved 2026-05-14** — see [`docs/specs/pattern-store.md`](specs/pattern-store.md). Per-workspace bounded SQLite store powering routing slot 4 (`PATTERN_RECOMMENDATION`) per [`routing-engine.md §5.5`](specs/routing-engine.md); structural-only v1 fingerprint, sample-size-weighted K-NN aggregation, three new `pattern.*` event types pending catalog addition at Phase 2.5 implementation. Embedding-provider-abstract for v2 hybrid mode. §5 dated decision entry added in the same change.
7. ~~**Evaluator scope.**~~ **Resolved 2026-05-14** — see [`docs/specs/evaluator.md`](specs/evaluator.md). Heuristic-first / hybrid-LLM-as-judge feedback loop across four subject kinds; `EvalVerdict` with a single `score` + confidence gate; append-only (re-evaluation produces new verdicts); cost-capped per session and per day; pattern-store consumption pinned in the 2026-05-14 reconciliation sweep (see CHANGES.md). §5 dated decision entry added in the same change.
8. ~~**Pricing model for the product itself.**~~ **Resolved 2026-05-16 — open-core gateway + per-seat Pro + reserved enterprise %-of-savings add-on.** See [`docs/specs/pricing.md`](specs/pricing.md) (§5.5.4 ratified). Three tiers: Free (OSS gateway, OSS multi-user, OSS analytics), Pro (per-seat $/month for the hosted dashboard, hard caps, audit log export, the agent), Enterprise (Pro plus a capped %-of-savings line gated on procurement contracting). Price points remain deferred to first-buyer triangulation; the ratification names the *model shape* only. Implementation lands as Wave 15.

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
