# Spec Changes

This file tracks breaking and significant changes to specs in `docs/specs/`. Its purpose is to prevent cross-spec drift: when one spec changes a contract, this log records which other specs reference that contract and need verification.

## How to use this file

When making a substantive change to a spec, add an entry below with:

1. **Date** — when the change was made.
2. **Spec** — which spec changed.
3. **Change** — one-line description.
4. **Type** — `breaking` (consumers must update) or `additive` (consumers can ignore).
5. **References to verify** — which other specs reference the changed contract and must be checked for consistency.
6. **Status** — `pending review` until cross-references are verified, then `verified`.

Trivial edits (typos, wording) don't need entries. Use judgment.

When working on a spec PR, scan this file for `pending review` entries against specs you depend on; verify them before landing.

## Specs in scope

- `canonical-message-format.md` — messages, content blocks, tool definitions, persistence.
- `event-bus-and-trace-catalog.md` — bus interface, event catalog, trace store.
- `routing-engine.md` — routing pipeline, rule format, `delegate()` contract.
- `streaming-protocol.md` — WebSocket protocol, snapshot/replay, cancellation.
- *(planned)* `provider-adapter-contract.md` — adapter interface, wire-format translation.
- *(planned)* `tool-dispatcher.md` — tool registry, side-effect handling, validation.
- *(planned)* `server-api.md` — REST endpoints, attach handshake, session lifecycle.
- `analytics-api.md` — read-only `/analytics/*` namespace backing the dashboard.
- `benchmark.md` — reproducible workload suite + measurement methodology backing the savings counterfactual.
- `deployment-shape.md` — recommendation for the replacement-agent / gateway / hybrid fork. Resolves [`STRATEGY.md §6.1`](../STRATEGY.md) when signed off.
- `gateway.md` — skeleton for the transparent HTTP gateway surface (paired with `deployment-shape.md`).
- `context-assembler.md` — v1 covers prompt-cache breakpoint placement; v2 adds the minimum-cacheable-prefix padding rule; v3 adds skill activation (explicit + pre-activation paths, per-session budget, no auto-activation in v3); history compression remains later.
- `pattern-store.md` — per-workspace bounded SQLite store of task fingerprints + outcomes that powers routing slot 4 (`PATTERN_RECOMMENDATION`). Phase 2.5.
- `skill-format.md` — retrospective v1 (2026-05-13) of the existing skills loader / store / tools; conforms to agentskills.io.
- `evaluator.md` — heuristic + hybrid LLM-as-judge feedback loop; emits `eval.*` events; resolves [`STRATEGY.md §6.7`](../STRATEGY.md) when signed off. Phase 3.

## Cross-reference map

A snapshot of which specs reference which (refresh when adding a spec):

| Source spec | Depends on |
|-------------|------------|
| `canonical-message-format.md` | (none — foundation) |
| `event-bus-and-trace-catalog.md` | canonical-message-format, routing-engine |
| `routing-engine.md` | canonical-message-format, event-bus-and-trace-catalog |
| `streaming-protocol.md` | canonical-message-format, event-bus-and-trace-catalog, routing-engine |
| `provider-adapter-contract.md` *(planned)* | canonical-message-format, event-bus-and-trace-catalog, streaming-protocol |
| `tool-dispatcher.md` *(planned)* | canonical-message-format, event-bus-and-trace-catalog |
| `server-api.md` *(planned)* | canonical-message-format, event-bus-and-trace-catalog, streaming-protocol |
| `analytics-api.md` | canonical-message-format, event-bus-and-trace-catalog, server-api |
| `benchmark.md` | analytics-api, event-bus-and-trace-catalog, canonical-message-format, provider-adapter-contract |
| `deployment-shape.md` | STRATEGY.md, market-research/synthesis.md (rationale only — no contract dependency) |
| `gateway.md` | canonical-message-format, provider-adapter-contract, routing-engine, event-bus-and-trace-catalog, server-api, analytics-api |
| `context-assembler.md` | canonical-message-format, provider-adapter-contract (planned), analytics-api |
| `pattern-store.md` | canonical-message-format, event-bus-and-trace-catalog, routing-engine, memory-store, analytics-api, evaluator |
| `skill-format.md` | canonical-message-format, event-bus-and-trace-catalog, tool-dispatcher, context-assembler |
| `evaluator.md` | event-bus-and-trace-catalog, canonical-message-format, analytics-api, benchmark, routing-engine, pattern-store *(planned)* |

When changing a spec, the dependent specs (right column whose left column is the changed spec) must be checked.

---

## Change log

### 2026-05-14 — gateway.md v1 (captures shipped surface) + per-key analytics rollup

- **Specs:** `gateway.md` (v0 skeleton → v1), `analytics-api.md` §4.1 + new §4.8, `server-api.md` (implicit — `GET /sessions/{id}.routing_policy_version` now populated).
- **Change:** Rewrote `gateway.md` from v0 skeleton to v1 documentation of the shipped transparent HTTP gateway in [`apps/gateway/`](../../apps/gateway/). Documents the actual endpoint shapes (`/v1/chat/completions`, `/v1/messages`, `/healthz`), the auth scheme (`Authorization: Bearer gw_<ulid>` or `x-api-key`), the keystore at `~/.metis/gateway/keys.json` (SHA-256 hash; mode `0o600`), the per-shape translation rules, the additive `gateway_key_id` + `inbound_shape` stamps on `LLMCallCompleted` / `TurnCompleted` (gateway.md §6), and the v1 loopback-only network posture (§3.2 — reverses the original v0 "default `0.0.0.0`" plan until per-key rate limiting and audit log land). Notes the §5.3 "transparent mode" trade-off — gateway clients passing `model` always trigger the `per_message_override` slot win — recommends leaving the default as-is and tracks a future `--ignore-inbound-model` flag for the cost-optimization magic-trick mode. Added `gateway_key` to `_COST_GROUP_BY_ALLOWED` in [`analytics/store.py`](../../packages/metis-core/src/metis_core/analytics/store.py) and shipped a new `/analytics/by_key` endpoint (analytics-api.md §4.8) backed by `AnalyticsStore.by_key()` — per-(gateway_key_id) cost + token + call_count rollup with an `by_inbound_shape` sub-array per row, rows with null `gateway_key_id` (agent-loop traffic) keyed under `null`. Surfaced `routing_policy_version` on `GET /sessions/{id}` (and the `POST /sessions` 201): added a content-derived `version` field on `RoutingPolicy` (truncated sha256 of the raw yaml at parse time; `None` for `EMPTY_POLICY`); `SessionManager.routing_policy_version()` exposes it to the HTTP layer.
- **Type:** additive. New analytics endpoint, new optional `gateway_key` group_by value, new optional response field on session endpoints, new optional `RoutingPolicy.version` (default `None` preserves call sites that construct policies directly).
- **References to verify:**
  - `event-bus-and-trace-catalog.md §6.3` — `LLMCallCompleted.gateway_key_id` / `inbound_shape` already land as typed optional fields. ✓
  - `analytics-api.md §4.1 + §4.8` — group_by enum extended; new endpoint shape documented. ✓
  - `routing-engine.md §5.7` — `RoutingPolicy` gains a `version` field; the validation rules and parser entry points are unchanged. ✓
  - `server-api.md §4.x` — `GET /sessions/{id}` response gains a populated `routing_policy_version` field. Already declared in the shape; no schema breakage. ✓
  - `KNOWN_ISSUES.md` — 🟡 "Per-key analytics roll-up has no HTTP surface" entry deleted (this change ships the HTTP surface). ✓
- **Status:** verified.

### 2026-05-14 — provider-adapter-contract.md v1.2 (CanonicalResponse returns content, not Message)

- **Spec:** `provider-adapter-contract.md` §3.3 (CanonicalResponse shape).
- **Change:** Bring §3.3 into line with the shipped impl. `CanonicalResponse` returns `content: list[ContentBlock]` + `model` + `provider` rather than a full `Message`. The adapter doesn't own two `Message` fields the spec previously implied it did: the `RoutingDecisionRecord` (decided upstream by the routing engine) and `Usage.cost_usd` (computed by core from the local price table per canonical-format §6.4). The caller (`SessionManager`) assembles the final canonical `Message` from the adapter's parts plus its own routing decision, cost computation, and id allocation. Adapter implementations have been on this shape since Phase 1 (`[adapters/protocol.py](../packages/metis-core/src/metis_core/adapters/protocol.py)` docstring + AGENTS.md "Implementation conventions" already noted the divergence); v1.2 closes the spec/impl gap. Substitutability is unaffected — the substitutability gate is the `(content, stop_reason, usage)` triple, not the `Message` envelope.
- **Type:** additive (the spec catches up with shipped impl; no consumer change required — there are no callers writing to the old shape).
- **References to verify:**
  - `canonical-message-format.md §5` — `Message` shape unchanged. The fields the adapter previously owned in `Message` (id, role, content, metadata.routing, metadata.usage.cost_usd) are now assembled by `SessionManager`; no canonical-format edit required. ✓
  - `streaming-protocol.md §5.6` — the streaming-side `MessageComplete` event's authoritative final content + usage shape is unchanged; it already returns content blocks rather than a `Message`. ✓
  - `event-bus-and-trace-catalog.md §6.3` — `llm.call_completed` payload reads from `CanonicalResponse.usage` / `model`; new shape preserves those fields. ✓
  - `KNOWN_ISSUES.md` — "`CanonicalResponse` shape divergence from spec" 🟢 entry retired by this change. ✓
- **Status:** verified.

### 2026-05-14 — context-assembler.md v3 (skill activation)

- **Spec:** `context-assembler.md` §5.2 (new), §7 (skill-activation entry retired from out-of-scope; new entries for auto-activation, mid-session eviction, per-workspace budget overrides), §8 (six new decision-log entries), §9 (new references to `skill-format.md` and `event-bus-and-trace-catalog.md §6.6`).
- **Change:** Specs the **skill activation** layer of the cost lever per [`STRATEGY.md §1`](../STRATEGY.md). Three activation paths partitioned by `skill.loaded.load_reason`: (a) **pre-activation** (`"always"`) — v2 §5.1's body-as-padding is formalized as observable activation, emitted once per inlined body at session init with `triggered_by_tool_use_id=None`; (b) **explicit activation** (`"on_demand"`) — existing `skill_load` tool path, unchanged except for the new budget check; (c) **auto-activation** (`"auto_suggested"`) — **not in v3**, reserved. No description-match-driven auto-activation in v3 (rationale: preserves agentskills.io progressive disclosure semantics; avoids non-determinism breaking caches; no usage data to tune classifier against). Per-session activation budget: `MAX_EXPLICIT_ACTIVATIONS_PER_SESSION=3` count cap, `WARN_CUMULATIVE_ACTIVATION_TOKENS=10000` log-only, `HARD_CAP_CUMULATIVE_ACTIVATION_TOKENS=30000` hard cap; all surface as `ToolExecutionError` → `tool.failed` (no new event types). Pre-activated skills don't count against budget. Discovery index entry for a pre-activated skill annotated `[preloaded]`; `skill_load(name)` for a pre-activated skill returns a pointer ("already in system prompt"), not the body, to avoid double-paying input bytes. **No mid-session eviction** in v3 — would invalidate message-level caches a future spec might place, and require unwinding structurally-linked tool_use/tool_result pairs. Deferred to history-compression spec.
- **Type:** additive on context-assembler.md; implies two additive cross-spec changes flagged below.
- **References to verify:**
  - `skill-format.md §7.1` — discovery-index format currently specified as `- {name}: {description}`. v3 §5.2.2 adds an optional `[preloaded]` annotation on pre-activated skills (`- {name} [preloaded]: {description}`). Additive — readers ignoring the annotation see no behavior change. Cross-spec edit lands with implementation; flagged in `context-assembler.md §5.2.7` open question 2.
  - `skill-format.md §8.2` — `skill_load` tool semantics gain a budget check (raises `ToolExecutionError` on exhaustion) and a pre-activated-skill special case (returns pointer text with `{"already_preloaded": true}` metadata, no body, no event re-emission). Additive: existing callers see no change in the in-budget non-preloaded case.
  - `event-bus-and-trace-catalog.md §6.6` — `skill.loaded` payload schema unchanged. v3 emits the existing `load_reason="always"` enum value from a new path (session init, post-`session.started`, pre-first-`turn.started`). No catalog edit required.
  - `analytics-api.md` — v3 mentions a future `/analytics/skills` rollup keyed on `load_reason` for tuning the v2 padding source priority; not specified in v3 and no analytics-api edit required.
  - `STRATEGY.md §1` — context > skills > model selection thesis: v3 specifies the second-largest lever (skills) inside the largest (context). No narrative change required; cross-reference only.
  - `benchmark.md` — no current workload exercises skill loading. Wave 6 should add one before tuning the default budget numbers; flagged in `context-assembler.md §5.2.7` open question 1. No spec edit required.
- **Status:** pending owner sign-off on the five open questions in §5.2.7 (default budget numbers; `[preloaded]` annotation format vs alternatives; auto-activation deferral; re-load-as-no-op semantics; pre-activation event ordering). Cross-spec edits to `skill-format.md §7.1` / §8.2 land with implementation (Wave 6+); both are additive.

---

### 2026-05-14 — context-assembler.md v2 (minimum-cacheable-prefix rule)

- **Spec:** `context-assembler.md` §5.1 (new), with rationale + decision log entries.
- **Change:** v1's prompt-cache breakpoint placement was honest but the natural Metis stable prefix (DEFAULT_SYSTEM_PROMPT + five built-in tools ≈ 265 heuristic tokens) tokenizes well below the *effective* haiku-4-5 cache floor — a live probe found a 3320-actual-token prefix produces `cache_creation_input_tokens = 0` while a 4957-token prefix succeeds. v2 adds a §5.1 rule requiring `SessionManager` to pad the stable prefix to clear that effective floor with margin (`MIN_CACHEABLE_PREFIX_TOKENS = 4500`, `MAX_CACHEABLE_PREFIX_TOKENS = 5500` heuristic tokens). Padding sources, in priority order: (1) loaded skill bodies in name-ascending order, (2) a static byte-stable `_OPERATING_CONTEXT_PADDING` block of Metis operating guidelines. Determinism is load-bearing — module-level constant; no per-call I/O. v1's breakpoint placement, the two-segment `system_prompt`/`system_prompt_volatile` shape, and the breakpoint-on-last-stable-block rule are all unchanged. Live verification: `scripts/smoke_cache.py --model haiku` now passes with the natural Metis prompt (turn 1 writes 5167 cache tokens; turn 2 reads 5167). Benchmark Run 3 (`benchmarks/RESULTS.md`): cache fires on **49 of 49 LLM calls (100%)** vs Run 2 cold's **10 of 30 (33%)**; same-3-workload aggregate cost dropped 22.8%.
- **Type:** additive. The §5.1 rule is a new section; v1's existing rules in §1–§4 and §5 (preceding §5.1) are unchanged. Callers that pass a custom `system_prompt` already above the floor see §5.1 as a no-op.
- **References to verify:**
  - `canonical-message-format.md §7` — adapter contract unchanged; `CanonicalRequest.system_prompt` / `system_prompt_volatile` shape unchanged. ✓
  - `analytics-api.md §4.2` — `cache_effectiveness` endpoint reads the same `cache_creation_input_tokens` / `cached_input_tokens` fields; no schema change. ✓
  - `skill-format.md` — v2 §5.1 inlines skill bodies into the cached prefix when padding is needed, which is a deviation from agentskills.io "progressive disclosure" (discovery only, activation via `skill_load`). The decision log records the reasoning: progressive disclosure still applies to the discovery index; bodies are only inlined when the prefix needs the bytes to clear the floor. No skill-format spec change required. ✓
  - `benchmark.md §6.2` — variance tolerance (`±5pp` on `savings_pct`, `±2 llm_call_count`) unchanged; Run 3 sits within tolerance against Run 2. ✓
- **Status:** verified.

### 2026-05-14 — benchmark workload diversity v1 (two discriminating fixtures)

- **Spec:** `benchmark.md` §4 (the suite).
- **Change:** Two new workloads added under [`benchmarks/workloads/`](../../benchmarks/workloads/): `regex-with-edge-cases` (one-shot NANP regex against 16 labeled cases; locked-down iteration via `max_tool_calls: 1` on the run turn) and `multi-file-refactor-with-shared-types` (7-file rename with an aliased import in `legacy.py`). Both ship `evaluate:` blocks with `expect_substring_in_final_response` so the heuristic judge gets an objective success signal. The shipped regex workload discriminates haiku-4-5 (`0.25`) vs sonnet-4-6 (`1.00`) at the workload-level score; the mfr workload scores `1.00 / 1.00` (parity datapoint, not a discriminator at the current model pair's capability). Full numbers and the cost-per-success inversion are in [`benchmarks/RESULTS.md`](../../benchmarks/RESULTS.md) under "Workload diversity v1". The benchmark spec's §4 "V1 ships three workloads" table is now an undercount (six workloads ship via filesystem discovery, including the prior `intentionally-failing-task` control case) — descriptive drift rather than a contract change.
- **Type:** additive. New fixtures discovered via the existing filesystem-based loader in `scripts/benchmark.py`; no harness or schema changes. The test that pins the discovered-workload set ([`apps/cli/tests/test_benchmark.py::test_shipped_workloads_load_clean`](../../apps/cli/tests/test_benchmark.py)) was updated to include the two new names — purely additive, no removal. Test count: 1029 passed (was 979; the +50 includes other parallel work landing during the same window).
- **References to verify:**
  - `pattern-store.md §8.3` — the K-cluster aggregator formula now has an input distribution where `success_mean_haiku < success_mean_sonnet`. The mechanism was already implemented; the new fixture provides the first real-API distribution that triggers the cost-vs-success trade-off. ✓ (no spec change needed; section in RESULTS.md cites the formula).
  - `evaluator.md §5.4` — workload-level rubric's `expect_substring_in_final_response` path is exercised by both new fixtures. The hybrid judge tier (just-landed) reads the same `signals_extra` plumbing, so these fixtures double as inputs to the LLM-judge upgrade. ✓
  - `benchmark.md §4` — the table listing v1's three workloads is now an undercount (six workloads discovered). Worth a follow-up edit to either enumerate all six or note that discovery is filesystem-based; not blocking.
- **Status:** verified.

### 2026-05-14 — evaluator: LLM-as-judge + hybrid escalation tier shipped

- **Spec:** `evaluator.md` §5.2 (LLM rubric), §5.3 (hybrid escalation), §9.2 (`/analytics/quality`).
- **Change:** LLM-as-judge tier landed at `packages/metis-core/src/metis_core/eval/llm_judge.py` (`LLMJudge`, `HybridJudge`, `LLMJudgeConfig`). Hybrid is the default for turn / workload subjects; tool_cycle / session remain heuristic-only per §5.5 / §5.6. Default escalation threshold = `0.7`. Budget-exhausted LLM calls return a `signals.budget_exhausted=True` verdict (confidence=0); HybridJudge falls back to its heuristic verdict and records `signals.escalation_skipped="budget_exhausted"`. New `/analytics/quality` endpoint (`apps/server/src/metis_server/analytics.py`) projects `eval.completed` over a window with `group_by` ∈ {model, judge_kind, rubric_id, none} and `min_confidence` filter; the `chosen_model` field joins via `route.decided` so the per-model rollup reflects the *judged* model, not the judge's.
- **Type:** additive (new classes, new endpoint, no breaking changes to existing heuristic path).
- **References to verify:**
  - `event-bus-and-trace-catalog.md §6.12` — three `eval.*` payloads unchanged; new signals (`budget_exhausted`, `escalation_skipped`, `heuristic_score`, `heuristic_confidence`) all live in the opaque `signals` dict so the catalog contract is preserved. ✓
  - `pattern-store.md §10.4` — pattern store reads `score` + `confidence` only; new signals don't affect that contract. ✓
  - `analytics-api.md` — new `/analytics/quality` endpoint follows the standard envelope and error mapping. ✓
- **Status:** verified.

### 2026-05-14 — evaluator: opt-in content penalty (refusal / empty response)

- **Spec:** `evaluator.md` §5.1 (turn rubric), §5.4 (workload rubric).
- **Change:** Added two signals to the heuristic judge: `assistant_refusal_detected` (×0.5 multiplicative penalty) and `empty_assistant_response` (×0.4). Both fire only when the caller plumbs `final_response_text` via `SubjectContext.signals_extra` — the bus subscriber path is unchanged. The workload rubric applies the same penalty (`workload_assistant_refusal_detected`, `workload_empty_assistant_response`) using the benchmark harness's existing `final_response_text` plumbing. Motivation: the prior rubric was content-blind and would score a clean refusal 1.0 if no `expect_substring_in_final_response` was configured — Run 2's "1.00 @ 0.80 on every workload" exposed the gap.
- **Type:** additive (new optional signals; existing tests unchanged; rubric version pinned at `1.0.0` because no caller in the live online path plumbs the new key yet, so re-runs of `metis evaluate --subject turn` against existing trace DBs produce identical scores).
- **References to verify:**
  - `pattern-store.md §10.4` — pattern store reads `score` only; new signals are in `signals` dict, not on the score contract. No change required. ✓
  - `benchmark.md §3.1` — `evaluate:` block schema unchanged; new fixture `intentionally-failing-task` added under `benchmarks/workloads/` as a control case. ✓
- **Status:** verified.

### 2026-05-13 — evaluator v1 implementation (heuristic tier)

- **Spec:** `evaluator.md`
- **Change:** v1 heuristic implementation lands at `packages/metis-core/src/metis_core/eval/` (`HeuristicJudge` + `Evaluator` bus subscriber + `BudgetTracker` + `metis evaluate` CLI). Subscribes to `turn.completed` / `tool.completed` / `tool.failed` / `session.ended` and emits `eval.started` / `eval.completed` / `eval.failed`. `workload.yaml.evaluate` block parsed by `scripts/benchmark.py` and fed to `Evaluator.evaluate_workload()` after each workload run — the quality score lands in the benchmark report. LLM-as-judge and hybrid escalation are deferred to a later wave per evaluator.md §5.2-5.3.
- **Type:** additive (new module, new optional `evaluate:` block on `workload.yaml`, new `metis evaluate` subcommand).
- **References to verify:**
  - `event-bus-and-trace-catalog.md §6.12` — three `eval.*` event payloads were added in Wave 4a (Task 4a-3). ✓
  - `benchmark.md §3.1` — `evaluate:` block documented. ✓ (this change)
  - `pattern-store.md §10.4` — pattern store's `update_score()` flow expects `eval.completed` carrying `subject_id` (turn_id), `score`, `confidence`. ✓ (payload matches; pattern store is the read-side, evaluator the write-side).
- **Status:** verified.

### 2026-05-13 — pattern-store v1 implementation

- **Spec:** `pattern-store.md`
- **Change:** v1 implementation lands at `packages/metis-core/src/metis_core/patterns/` (structural fingerprint + similarity + K-NN aggregation + SQLite store + bus subscriber). Routing engine slot 4 (`PATTERN_RECOMMENDATION`) consults the store when a `pattern_store_resolver` is injected; `pattern.recorded` / `pattern.matched` / `pattern.evicted` events flow through the bus. Spec body unchanged; the three event payloads were added to `events/payloads.py` in Wave 4a (Task 4a-3). `PatternConfig` gains `min_eval_confidence: float = 0.5` per pattern-store §15.4 reconciliation.
- **Type:** additive (new module, new code-path on existing routing chain).
- **References to verify:**
  - `routing-engine.md §5.5` — K-NN formula matches `aggregation.py`. ✓
  - `event-bus-and-trace-catalog.md §6.5b` — three new pattern events were added in Wave 4a. ✓
- **Status:** verified.

### 2026-05-08 — routing-engine v3.1

- **Spec:** `routing-engine.md`
- **Change:** Auxiliary event renamed (`pattern.override_accepted` → `route.overridden`); delegation phase asymmetry documented at §6 preamble.
- **Type:** breaking (event name change), additive (phase note).
- **References to verify:**
  - `event-bus-and-trace-catalog.md` §6.5b — confirms the canonical event name. ✓
  - Future: any client code rendering routing events. (No clients yet.)
- **Status:** verified.

### 2026-05-08 — event-bus v2

- **Spec:** `event-bus-and-trace-catalog.md`
- **Change:** Multiple. Added `route.overridden`, `bus.gap_detected`, `bus.subscriber_unregistered`. Removed `bus.handler_error`, `bus.overflow` (moved to logs). Pattern domain split out as §6.5b. SQLite WAL + NORMAL committed. Memory snapshotter moved off fast path. Dynamic sensitivity on opt-in.
- **Type:** breaking (event types removed/renamed).
- **References to verify:**
  - `routing-engine.md` — auxiliary event names. ✓ (handled by v3.1 above)
  - `streaming-protocol.md` — events flowing through stream. Verified: streaming spec doesn't enumerate specific event types beyond examples; safe.
- **Status:** verified.

### 2026-05-08 — routing-engine v3

- **Spec:** `routing-engine.md`
- **Change:** Many; see v3 changelog in the spec header.
- **Type:** mix.
- **References to verify:**
  - `canonical-message-format.md` §7.2 — `AdapterCapabilities` needs `supports_tools`, `supports_system_prompt`, `supports_structured_output` fields per routing v3 §4.4. **Pending: canonical-format spec needs an additive update.**
  - `event-bus-and-trace-catalog.md` — `route.decided.chain[].validation_failure` enum values updated (added `no_tool_support`, `no_system_prompt_support`, `no_structured_output_support`). ✓ in v2.
- **Status:** pending review (canonical-format AdapterCapabilities update).

### 2026-05-08 — Cross-spec reconciliation sweep (event-bus v3, streaming v2, others)

Several spec-boundary inconsistencies surfaced in cross-spec review and were resolved together:

- **Spec:** all five (`canonical-message-format` v1.1, `event-bus-and-trace-catalog` v3, `streaming-protocol` v2, `provider-adapter-contract` v1.1, `tool-dispatcher` v1.1, `server-api` v1.1, `routing-engine` v3.2).
- **Changes:**
  1. **Streaming events declared as separate transient layer**, not bus catalog events. Streaming server is no longer a bus subscriber for streaming events; it has two input channels (bus bridge for catalog events, direct from agent loop for streaming events). Domains `message`, `text`, `thinking`, `tool.use_*` reserved for streaming use only. (event-bus §4.5.1, streaming §5.1, provider-adapter §5.1)
  2. **Error class enums reconciled.** `llm.call_failed.error_class` (catalog) extended to 8 values matching `provider-adapter` §6.1. `tool.failed.error_class` (catalog) extended to 8 values matching `tool-dispatcher` §6.1. (event-bus §6.3, §6.4)
  3. **`tool.confirmation_requested` and `tool.confirmation_resolved` added to catalog** with full payloads (event-bus §6.4).
  4. **`block_dropped` confirmed as log-only**, not a catalog event. canonical-format §4.2.2, §7.3, §11.1.6 updated to match.
  5. **`AdapterCapabilities` extended** with `supports_tools`, `supports_system_prompt`, `supports_structured_output`, `supports_prompt_caching` (canonical-format §7.2), resolving the v3 pending review item.
  6. **`provider_overrides` removed from `ToolDefinition`** (canonical-format §4.4) — unused everywhere.
  7. **`RoutingDecisionRecord.mode` documented as a coarse summary** with explicit mapping to the routing chain enum (canonical-format §4.3).
  8. **Cancellation sequence split into three cases** (cancel during LLM, during tool dispatch, at seam) in streaming-protocol §6.2. routing-engine §3.4 cross-references.
  9. **`max_retries` semantics pinned** in provider-adapter §6.4: total attempts = 1 + max_retries.
  10. **`routing_failed` 503 body schema defined** in server-api §4.2.
  11. **Tool factory-vs-singleton clarified** in tool-dispatcher §3.1.
  12. **`EventFrame` cross-reference** added in event-bus §5.4.
- **Type:** mostly breaking (enum extensions, removed event types, field removals); some additive.
- **References to verify:** all five specs cross-checked in this sweep.
- **Status:** verified.

### 2026-05-08 — Post-v3 micro-sweep (streaming-protocol numbering, project-overview diagram)

Followup to the cross-spec sweep — five small but real defects caught in review:

- **Specs:** `streaming-protocol` (v2.1 conceptually; no version bump since changes are corrective), `provider-adapter-contract` (cross-ref fix), `project-overview` (architecture diagram + principle + spec list).
- **Changes:**
  1. **Streaming-protocol §5 numbering fixed.** Was `5.1 5.2 5.3 5.3 5.4 5.5`; now `5.1 5.2 5.3 5.4 5.5 5.6`. provider-adapter §5.4 and decision log cross-refs updated from `§5.5` to `§5.6`.
  2. **§10.4 worked example rewritten** to pick a specific case (tool dispatch per §6.2.2) and emit only events that case produces. Added note acknowledging the case split.
  3. **Cancellation tests in §11.1** split into 7 (LLM streaming, §6.2.1), 8 (tool dispatch, §6.2.2), 8b (seam, §6.2.3) — each asserts exactly the events that case produces.
  4. **`EventFrame` comment in §4.2** updated to "wraps any catalog or streaming event."
  5. **Filter validation §3.2 and §9.3** updated: accepted set is the union of catalog and streaming-only event types. Test 13 wording tightened.
  6. **`project-overview.md` architecture diagram updated** to show two channels (durable bus + transient streaming), the streaming server merging both, and the bus subscribers (trace store, cost accumulator, pattern) as a separate group. Core principle "Event bus as observability spine" rewritten as "Two-channel observability." Components table adds a "Streaming Server" row.
  7. **`project-overview.md` spec list refreshed** with current statuses (canonical-format v1.1, event-bus v3, streaming v2, routing v3.2, etc.). Added provider-adapter, tool-dispatcher, server-api, CHANGES.md to the list.
- **Type:** corrective (numbering, contradictions in examples, stale visual) — no contract changes.
- **References to verify:** none beyond the files updated above.
- **Status:** verified.

### 2026-05-12 — event-bus: `skill.loaded.source` added

- **Spec:** `event-bus-and-trace-catalog.md` §6.6.
- **Change:** Added `source: Literal["global", "workspace"]` to `skill.loaded` payload so traces record which directory served the skill after the workspace-overrides-global merge.
- **Type:** additive. Existing consumers ignore unknown fields; no migration required for stored events (the field defaults to None on records written before this entry, since the implementation defaulted it None on the typed struct — though all in-process emitters set it).
- **References to verify:**
  - `skill-format.md` *(planned)* — when that spec lands, document `source` alongside the other fields. Note pending below.
- **Status:** verified (event-bus spec updated in this change; implementation in `packages/metis-core/src/metis_core/events/payloads.py::SkillLoaded` + emitter in `packages/metis-core/src/metis_core/skills/tools.py::SkillLoadTool`).

---

### 2026-05-12 — analytics-api.md v1 drafted

- **Spec:** new `analytics-api.md` v1.
- **Change:** Adds a read-only `/analytics/*` HTTP namespace extending `server-api.md`. Endpoints derive metrics from the existing `events`, `messages`, and `sessions` tables — no new persistent state, no new bus events, no new write paths. Endpoints: `/cost`, `/cache_effectiveness`, `/routing`, `/reliability`, `/sessions`, `/turns/{id}`, `/savings`. Pricing semantics are hybrid: actuals honor stamped `pricing_version`; the savings counterfactual re-prices both numerator and denominator under the current `PriceTable`.
- **Type:** additive (new endpoints; no contract change to existing specs).
- **References to verify:**
  - `server-api.md` — analytics namespace lives on the same Starlette app and inherits the loopback-only / no-auth posture. No edit required; cross-reference only.
  - `event-bus-and-trace-catalog.md` — analytics queries depend on the `llm.call_completed`, `llm.call_failed`, `route.decided`, and `turn.completed` payload shapes. Any future change to those payloads must update the relevant analytics endpoint and its SQL. No edit required now.
  - `routing-engine.md §5.3.1` — known asymmetry between `cost_today_exceeds_usd` (UTC midnight) and the dashboard's "today" (local TZ). Documented in analytics-api §3.1; not aligning until evidence of confusion.
- **Status:** verified (no dependent specs need edits in this change).

---

### 2026-05-13 — benchmark.md v1 drafted

- **Spec:** new `benchmark.md` v1.
- **Change:** Defines a reproducible workload suite + measurement methodology that turns `/analytics/savings.actual_repriced_usd` / `baseline_repriced_usd` into a credible "saved X%" number — the artifact `STRATEGY.md §6.4` named as the biggest gap between architecture and proof. Specifies the workload model (per-workload YAML script + bundled fixture workspace under `benchmarks/workloads/`), the v1 suite (three workloads: fix-a-bug-small, write-a-doc-from-notes, multi-turn-refactor), reproducibility rules (pinned commit SHA, `PriceTable.version`, resolved model ids, `temperature=0`), and report shape. Adds `scripts/benchmark.py` (drives the loop) and bundled workload fixtures. Plumbs a `temperature: float | None = None` kwarg through `SessionManager.submit_turn` → `CanonicalRequest.temperature` so the determinism rule is enforceable.
- **Type:** additive (new spec; new optional kwarg on `submit_turn` defaulting to None preserves existing behavior).
- **References to verify:**
  - `analytics-api.md §4.7` — the savings response shape this spec consumes. No edit required.
  - `provider-adapter-contract.md` (planned) — when drafted, document that adapters honor `CanonicalRequest.temperature` when set. Native Anthropic/OpenAI/OpenRouter adapters already do.
  - `event-bus-and-trace-catalog.md` — the `llm.call_completed` / `turn.completed` payloads are the source rows for the benchmark's projection. No edit required.
  - `STRATEGY.md` — §6.4 resolved (pointer to this spec); §5 dated entry added.
- **Status:** verified (no dependent spec edits required in this change; STRATEGY.md updated in the same change).

---

### 2026-05-13 — context-assembler.md v1 drafted

- **Spec:** new `context-assembler.md` v1 (scope: cache-breakpoint placement only).
- **Change:** Specifies the two-segment system prompt on `CanonicalRequest` (`system_prompt` stable + new `system_prompt_volatile` for `MEMORY.md` / `USER.md`-shaped content), and where adapters place provider cache breakpoints. Anthropic adapter writes `cache_control: {"type": "ephemeral"}` on the last tool definition and on the last stable system block. OpenAI relies on automatic prefix-match caching; the adapter preserves prefix stability (`system → tools → messages` order, volatile content concatenated at the *end* of the system text). OpenRouter passes through markers but declares `supports_prompt_caching=False` because cache behavior depends on the upstream route. Validation surface is `/analytics/cache_effectiveness` ([analytics-api.md §4.2](analytics-api.md)) plus a `scripts/smoke_cache.py` 2-turn live-API test that asserts `cached_input_tokens > 0` on turn 2.
- **Type:** additive. New optional `system_prompt_volatile` and `workspace_path` fields on `CanonicalRequest` default to `None` and preserve existing behavior. The cache_control markers don't change the request's semantic meaning for any provider that doesn't recognize them.
- **References to verify:**
  - `canonical-message-format.md §7.2` — `AdapterCapabilities.supports_prompt_caching` is the routing-engine substitutability gate this spec leans on. No edit required; the field already exists.
  - `provider-adapter-contract.md` (planned) — when drafted, document that adapters supporting prompt caching write the breakpoints described in §3 of context-assembler.md.
  - `analytics-api.md §4.2` — the cache-effectiveness view is the validation surface; `hit_rate > 0` after a multi-turn Anthropic session signals the lever has landed. No edit required.
  - `KNOWN_ISSUES.md` — "No prompt-caching strategy" entry retired; replaced by this spec + implementation. ✓ in this change.
- **Status:** verified (no dependent spec edits required; KNOWN_ISSUES.md updated in the same change).

---

### 2026-05-13 — deployment-shape.md v1 + gateway.md v0 drafted

- **Specs:** new `deployment-shape.md` v1 (recommendation), new `gateway.md` v0 (skeleton, paired).
- **Change:** `deployment-shape.md` recommends the hybrid deployment (gateway first → agent upgrade) to resolve the architectural fork in [`STRATEGY.md §3`](../STRATEGY.md) and the open question in [`STRATEGY.md §6.1`](../STRATEGY.md). `gateway.md` is the v0 skeleton of the HTTP gateway surface it implies: OpenAI-shape (and Anthropic-shape) inbound endpoints, request-translation contracts that explicitly contract against the LiteLLM tool_use / cache_control / thinking-block hazards listed in [`docs/market-research/03-routing-layers.md`](../market-research/03-routing-layers.md), per-request stateless routing via the existing engine, and an enumerated non-feature list (no context shaping, no skill loading, no memory composition) that preserves the agent's upgrade-tier value proposition.
- **Type:** additive (two new specs; no contract changes to existing specs). `gateway.md §6` describes additive payload fields (`gateway_key_id`, `inbound_shape`) on existing `llm.call_completed` and `turn.completed` events — those land only when the gateway implementation does.
- **References to verify:**
  - `STRATEGY.md` §3 (resolution note added at top), §5 (new dated entry), §6.1 (retired with resolution pointer), §6.3 (narrowed: gateway-first implies deployed-instance posture). ✓ landed in this change.
  - `provider-adapter-contract.md` — `AdapterCapabilities` already carries the fields the gateway needs (`supports_tools`, `supports_prompt_caching`, etc.). No edit required.
  - `routing-engine.md` — 7-slot chain semantics in stateless gateway path documented in `gateway.md §5.1`. No edit required; cross-reference only.
  - `event-bus-and-trace-catalog.md` — additive payload fields (`gateway_key_id`, `inbound_shape`) documented in `gateway.md §6` will need to land in the payload registry when the gateway implementation does. Flagged as pending below.
  - `analytics-api.md` — adding `gateway_key` as a `group_by` dimension on `/analytics/cost` is a future additive change; not part of this entry.
- **Status:** verified (owner sign-off 2026-05-13; STRATEGY.md edits landed in the same change). Implementation-time payload-field additions to `event-bus-and-trace-catalog.md` remain pending below.

---

### 2026-05-14 — event-bus catalog v3.1: pattern.* and eval.* payloads landed

- **Spec:** `event-bus-and-trace-catalog.md` (v3 → v3.1).
- **Change:** Six new typed payloads landed in [`packages/metis-core/src/metis_core/events/payloads.py`](../../packages/metis-core/src/metis_core/events/payloads.py) and `PAYLOAD_REGISTRY` ahead of the implementation in Batch 4b (Wave 4); the catalog spec is updated to match.
  - **Pattern domain (§6.5b extended)** — `pattern.recorded`, `pattern.matched`, `pattern.evicted` per `pattern-store.md §10`. All `pseudonymous`. Phase 2.5.
  - **New `eval` domain (§6.12; closed-list extension in §4.5)** — `eval.started`, `eval.completed`, `eval.failed` per `evaluator.md §8`. All `pseudonymous` floor; `eval.completed` admits opt-in uplift to `user_controlled` per §4.4.1 when `signals.rationale_redacted` is populated.
  - **Decimal serialization.** `PatternRecorded.cost_usd_at_record` and `EvalCompleted.judge_cost_usd` use `Decimal`, serialized as strings via `msgspec.to_builtins`, matching the `Usage.cost_usd` convention from [`canonical-message-format.md §6.4`](canonical-message-format.md).
  - **Field-name divergence from pattern-store.md §10.1.** The catalog and implementation use `cost_usd_at_record` rather than the spec's `cost_usd` to disambiguate from `llm.call_completed.cost_usd` and to follow the codebase's `Decimal` convention. Field names otherwise match `pattern-store.md §10` and `evaluator.md §8/§10` as currently drafted; the Task 4a-2 reconciliation sweep may adjust further.
  - **Tests** added in [`packages/metis-core/tests/events/test_payloads.py`](../../packages/metis-core/tests/events/test_payloads.py) cover registry membership, round-trip (`to_builtins` → `convert`) for each new payload, `make_event` type↔payload binding, and the sensitivity-uplift path for `eval.completed`.
- **Type:** additive. No existing payload shape changed; no existing event removed or renamed. New typed payloads do not fire from any subscriber yet (Batch 4b lands `PatternStore` and `Evaluator` implementations + bus wiring).
- **References to verify:**
  - `pattern-store.md §10.1` — landed payload uses `cost_usd_at_record` (Decimal) rather than the drafted `cost_usd` (float). Reconcile name + type in the Wave 4 sweep; either update the spec to match the catalog or back out of the rename.
  - `evaluator.md §8` — payload fields and `Decimal` cost convention match the spec verbatim. `signals` is the opaque dict the spec specified; sensitivity uplift is wired via the existing `make_event(..., sensitivity=...)` override path. No edit required.
  - `routing-engine.md §5.5` — pattern-domain events do not change the routing chain payload; `pattern.matched` is queryable separately from `route.decided`. No edit required.
  - `analytics-api.md §4.6` — `/analytics/turns/{id}` and the planned `/analytics/quality` endpoint will join `eval.completed.subject_id` against `turn_id`. No edit required until the analytics endpoint lands.
- **Status:** pending review (the catalog edits and typed payloads have landed for both `pattern-store.md` and `evaluator.md`; pattern-store.md §10.1 field rename + Wave 4 reconciliation per the two earlier entries below remain open).

---

### 2026-05-13 — pattern-store.md v1 drafted

- **Spec:** new `pattern-store.md` v1 (specs-only; no implementation).
- **Change:** Defines the per-workspace, bounded SQLite-backed store of task fingerprints + outcomes that powers routing slot 4 (`PATTERN_RECOMMENDATION`) per [`routing-engine.md §5.5`](routing-engine.md). Specifies: (a) per-turn fingerprinting unit with a v1 structural-only feature set (file extensions, tool names, side-effect classes, token-bucket, intent regex tags) and an embedding-provider-abstract v2 hybrid path that lands data-only; (b) `<workspace>/.metis/patterns.db` storage with WAL + `synchronous=NORMAL` mirroring the trace store; (c) bounded caps (5k soft / 10k hard / 180-day age) where hard-cap **auto-evicts** rather than rejects writes — asymmetric with `memory-store.md` because pattern writes are mechanical projections with no agent-curation step; (d) K-NN retrieval with weighted Jaccard similarity + sample-size-weighted cluster aggregation, implementing routing-engine.md §5.5 scoring verbatim; (e) three new event types (`pattern.recorded`, `pattern.matched`, `pattern.evicted`) added to `event-bus-and-trace-catalog.md §6.5b`; (f) decimal cost preservation with `pricing_version_last` for future reprice; (g) workspace isolation (multi-user / cross-workspace explicitly out of scope per `STRATEGY.md §2`, §6.6). Closes `STRATEGY.md §6.6`'s "pattern store mechanics" deferral; one [`routing-engine.md §5.5`](routing-engine.md) ambiguity flagged in pattern-store §13.7 (sample-size weighting).
- **Type:** additive (new spec; three new event types to be added to event-bus catalog at Phase 2.5 implementation time; no contract changes to existing specs).
- **References to verify:**
  - `routing-engine.md §5.5` — sample-size weighting in K-cluster aggregation is unspecified there; pattern-store §8.4 picks weighted means as v1 interpretation. Needs a one-line clarification in routing-engine.md to either pin or back out. **Flagged in pattern-store §15.6.**
  - `event-bus-and-trace-catalog.md §6.5b` — three new event types (`pattern.recorded`, `pattern.matched`, `pattern.evicted`) to be added when the Phase 2.5 implementation lands. Sensitivity is `pseudonymous` for all three; parent linkages documented in pattern-store §10. **Catalog edit pending; flagged below.**
  - `evaluator.md` *(parallel draft by Agent 3B)* — pattern-store §15 enumerates the touchpoints assumed: `EvaluationResult` shape consumed by the session-ended subscriber, sync vs async score timing decision, `update_score()` API for late-arriving scores if async. **Reconcile in Wave 4 sweep.**
  - `memory-store.md` — used as the reference shape for goals/non-goals/caps/eviction structure; no edit required.
  - `analytics-api.md §4.7` — re-pricing math precedent followed; no edit required.
  - `STRATEGY.md §6.6` — "pattern store mechanics" open question resolved with pointer to this spec; §5 should record the decision in the same change. **Owner update pending.**
- **Status:** pending review (catalog additions land with Phase 2.5 implementation; routing-engine §5.5 clarification and evaluator.md reconciliation tracked below).

---

### 2026-05-13 — evaluator.md v1 drafted

- **Spec:** new `evaluator.md` v1 (specs-only; no implementation).
- **Change:** Defines the heuristic-first / hybrid-LLM-as-judge feedback loop that resolves [`STRATEGY.md §6.7`](../STRATEGY.md) — "the feedback loop that *proves* savings — without it, 'is the system actually saving money vs naive sonnet-everywhere?' stays an open question forever." Specifies: (a) four subject kinds (`turn`, `tool_cycle`, `session`, `workload`) — the workload subject subsumes the v1 limitation flagged in [`benchmark.md §2.2.2`](benchmark.md); (b) verdict shape (`EvalVerdict` `msgspec.Struct(frozen=True)` — single `score` in `[0, 1]`, `confidence` as a gate, `Decimal judge_cost_usd`, versioned `rubric_id` + `rubric_version`, opaque `signals` dict for judge-specific evidence); (c) three judge tiers (heuristic ($0), LLM-as-judge (small model by default), hybrid escalation with a single `escalation_threshold` knob); (d) bus subscriber on `turn.completed` / `tool.completed` / `tool.failed` / `session.ended` / `feedback.explicit` as non-fast-path, plus a `metis evaluate` CLI for batch re-evaluation; (e) three new event types (`eval.started`, `eval.completed`, `eval.failed`) and a new `eval` domain to be added to `event-bus-and-trace-catalog.md §4.5` / §6 at implementation time; (f) per-session ($0.10 default) and per-day ($1.00 default) `judge_cost_usd` caps + workspace kill-switch; (g) one new analytics endpoint (`/analytics/quality`) and an additive `include_eval` parameter on `/analytics/cost`; (h) re-evaluation is append-only (every verdict is a new event), enabling the dashboard's "evaluator agreement rate over time" view as a query, not a side-table; (i) workload rubric integrates with `benchmark.md` via a new optional `evaluate:` block in `workload.yaml`; (j) workspace-scoped single-user per [`STRATEGY.md §2`](../STRATEGY.md), no labeled training data, no LLM-as-judge in the critical path. `evaluator.md §15` enumerates the coordination touchpoints with the parallel `pattern-store.md` draft for the Wave 4 reconciliation.
- **Type:** additive (new spec; three new event types + new `eval` domain to be added to event-bus catalog at Phase 3 implementation time; one new analytics endpoint + additive `include_eval` param + additive `evaluations` array on `/analytics/turns/{id}`; no contract changes to existing specs).
- **References to verify:**
  - `event-bus-and-trace-catalog.md §4.5` (closed domain list) and §6 — new `eval` domain plus three event types (`eval.started`, `eval.completed`, `eval.failed`) to be added when the Phase 3 implementation lands. Sensitivity floor `pseudonymous`; `eval.completed` can uplift to `user_controlled` on opt-in `signals.rationale_redacted` per §4.4.1. **Catalog edit pending; flagged below.**
  - `routing-engine.md §5.5` — pattern-store consumption of `eval.completed.score` as `success_score`; existing math reads one number, no edit required. The confidence-gate filter convention (`pattern.min_eval_confidence`) is documented in evaluator.md §4.3 and §11.1 as a pattern-store-side configuration; cross-check against pattern-store.md.
  - `analytics-api.md §4.1` / §4.6 — additive `include_eval` query parameter on `/analytics/cost`; additive `evaluations` array on `/analytics/turns/{id}.data`. Existing consumers ignore unknown fields per the additive convention. No edit required now; document at implementation time. **Analytics spec edit pending.**
  - `benchmark.md §2.2.2` — v1 "no quality scoring of outputs" limitation closed by this spec via the workload subject. New optional `workload.yaml.evaluate:` block (rubric, expect_substring_in_final_response, llm_judge_model, weight_per_turn) is additive to the schema in `benchmark.md §3.1` — when the evaluator implementation lands, `benchmark.md §3.1` should add the `evaluate:` block to the schema and `benchmark.md §8` should add the quality column to the report. **Benchmark spec edit pending.**
  - `canonical-message-format.md §6.4` — `Decimal` cost-as-string serialization convention reused for `judge_cost_usd` in event payloads. No edit required; cross-reference only.
  - `pattern-store.md` (parallel draft by Agent 3A) — evaluator.md §15 lists the touchpoints assumed (verdicts on bus, score as one number, confidence-gate filter, `MAX(eval_id)` per subject as "latest verdict," join `chosen_model` from `route.decided` rather than embedding in verdict). **Reconcile in Wave 4 sweep.**
  - `STRATEGY.md §6.7` — "evaluator scope" open question resolved with pointer to this spec; §5 should record the decision in the same change. **Owner update pending.**
- **Status:** pending review (catalog additions land with Phase 3 implementation; benchmark.md / analytics-api.md / STRATEGY.md edits and pattern-store.md reconciliation tracked below).

---

### 2026-05-14 — Pattern-store ↔ evaluator reconciliation sweep

Wave 3 produced [`pattern-store.md`](pattern-store.md) and
[`evaluator.md`](evaluator.md) in parallel. Each spec's §15 listed
touchpoints assumed about the other surface. This sweep walks those
touchpoints and pins the reconciled contract, following the
2026-05-08 cross-spec reconciliation pattern.

- **Specs:** `pattern-store.md`, `evaluator.md`, `routing-engine.md`.
- **Changes:**
  1. **Verdict shape ownership.** `EvalVerdict` ([`evaluator.md §4.1`](evaluator.md))
     is the canonical shape; `pattern-store.md §15.1` references it
     verbatim and stops re-specifying. The pattern store consumes
     `subject_id` (the `turn_id`), `score`, `confidence`, and
     `eval_id`; everything else (`signals`, `judge_kind`, `rubric_id`)
     is opaque pass-through.
  2. **Async score timing.** Pattern-store `record()` writes outcomes
     immediately on `session.ended` with `success_score=None`; an
     `eval.completed` subscriber later calls
     `PatternStore.update_score(turn_id, score, confidence, eval_id,
     pricing_version)` to fold the verdict into the outcome
     accumulator. Idempotence is keyed by `eval_id`. Re-evaluation
     produces a new `eval_id` and rolls back the prior contribution
     before applying the new score. Documented in
     `pattern-store.md §10.4` and `§15.3`; cross-referenced from
     `evaluator.md §15`. Join key: `turn_id`.
  3. **Confidence-gate filter home.** `pattern.min_eval_confidence`
     lives in **pattern-store config** (`routing.yaml::pattern.*` block)
     alongside `cost_weight` / `min_confidence` / `min_sample_size`.
     Default `0.5` (matches the value declared in
     [`evaluator.md §4.3`](evaluator.md)). The evaluator emits all
     verdicts; the pattern store applies the gate at K-cluster
     aggregation time. Verdicts below the gate stay queryable in the
     trace store for the agreement-rate view. Documented in
     `pattern-store.md §15.4`; cross-referenced from `evaluator.md §15`.
  4. **Sample-size-weighted mean pinned in
     [`routing-engine.md §5.5`](routing-engine.md).** One-line
     clarification: `normalized_success_M = Σ(success_score_i ×
     sample_size_i) / Σ(sample_size_i)`. A neighbor row with 50
     contributing sessions weights 50× a single-shot row. This was
     the v1 interpretation `pattern-store.md §8.4` already designed
     to; pinning it in the routing spec removes the open ambiguity
     called out in `pattern-store.md §13.7`.
  5. **`MAX(eval_id)` as the latest-verdict rule.** Documented in
     `pattern-store.md §10.4` alongside the `update_score()` flow.
     Re-evaluation produces a new `eval.completed` with a fresh
     `eval_id`; pattern-store consumers join on `MAX(eval_id) per
     subject` to surface the latest verdict. Aligned with
     [`evaluator.md §4.6`](evaluator.md) and §11.1.
- **Type:** spec reconciliation (no contract breaks; clarifications +
  consolidated ownership of shared shapes).
- **References to verify:**
  - `routing-engine.md §5.5` — sample-size-weighted clarification
    landed in this change. ✓
  - `pattern-store.md §10.4`, §15 — async flow + `update_score()` +
    confidence-gate filter + `MAX(eval_id)` rule documented. ✓
  - `evaluator.md §15` — reconciliation table reflects pinned
    outcomes; open coordination items closed. ✓
  - `STRATEGY.md §5`, §6.6, §6.7 — retired entries for "pattern
    store mechanics" and "evaluator scope" with pointers to the
    drafted specs. ✓
- **Status:** verified. Phase 2.5 / Phase 3 implementation-time
  catalog additions to `event-bus-and-trace-catalog.md §4.5` / §6
  remain pending (tracked below under the original pattern-store and
  evaluator entries).

---

### 2026-05-13 — skill-format.md v1 drafted (retrospective)

- **Spec:** new `skill-format.md` v1 (specs-only; documents the existing implementation in [`packages/metis-core/src/metis_core/skills/`](../../packages/metis-core/src/metis_core/skills/)).
- **Change:** Captures retrospectively what the skills loader / store / tools already do: agentskills.io-conformant six-field frontmatter (`name`, `description`, `license`, `compatibility`, `metadata`, `allowed-tools`); `SKILL.md` directory layout with `scripts/` / `references/` / `assets/` siblings; two on-disk roots (`~/.metis/skills/` global, `<workspace>/.metis/skills/` workspace) merged workspace-overrides-global; three-stage progressive disclosure (discovery index in stable system prompt → `skill_load` activation → execution); two tools (`skill_search` / `skill_load`) both `SideEffects.READ`; `skill.loaded` event emission semantics including the `source` field added 2026-05-12. Surfaces seven implementation observations (name-validation error message wording; metadata scalar coercion; unbounded discovery index; no reload-on-change; hidden dirs not excluded; symlinks followed; `allowed-tools` parsed-not-enforced) in §11 for triage, not fixed in this change. Follows the `memory-store.md` retro-spec pattern.
- **Type:** additive (new spec; no code or contract changes). Resolves the pending cross-reference for `skill.loaded.source` (added 2026-05-12) by documenting the field alongside the rest of the payload.
- **References to verify:**
  - `event-bus-and-trace-catalog.md §6.6` — `skill.loaded` payload (including `source`) documented in skill-format.md §9.1. No edit required; cross-reference only. ✓
  - `tool-dispatcher.md` *(planned)* — `ToolContext.skills` field carries the per-session `SkillStore`; skill-format.md §8 documents the two tools' registration / dispatch semantics. No edit required.
  - `context-assembler.md §2-§5` — discovery index injected into the *stable* system prompt segment ahead of the cache breakpoint; skill-format.md §7.1 cross-references. No edit required.
  - `project-overview.md` — spec list refresh: `skill-format.md` line at §"Specs and documents" should move from "Planned" to "Drafted (v1, 2026-05-13)". Defer to next doc-refresh pass.
  - `STRATEGY.md` — "skills" cost lever (one of three in §2) is now spec-backed; no narrative change required.
- **Status:** verified. The "Pending cross-references" entry for `skill-format.md` (`skill.loaded.source` field, 2026-05-12) is resolved by skill-format.md §9.1 and §10.6 and removed below.

---

### 2026-05-12 — Implementation milestone + doc refresh

Not a spec change; an alignment pass between the docs and what's actually been built.

- **Files touched:** `README.md`, `docs/project-overview.md`, `docs/specs/project-overview.md`, new `docs/STRATEGY.md`, new `docs/KNOWN_ISSUES.md`, new `docs/specs/memory-store.md`.
- **What landed in code since the last doc refresh:** three provider adapters (Anthropic / OpenAI / OpenRouter), streaming end-to-end (adapter → session manager → CLI + WebSocket), Textual TUI, HTTP/WebSocket server (`metis serve`, loopback-only), SQLite session/message persistence, bounded memory (MEMORY.md / USER.md + 3 tools), skills store + `load_skill` tool, configured-rule parser (yaml policy + predicate set + loader; integration into routing chain pending), cross-provider conformance suite. Test count went from 272 → 592.
- **Spec-list status changes:** `memory-store.md` moved from "planned" to "drafted (v1)." `skill-format.md` and `pattern-store.md` remain planned.
- **New strategy artifacts:** `docs/STRATEGY.md` captures the cost-optimization thesis, buyer ≠ user framing, three cost levers (skills / context / model selection), and the open replacement-agent-vs-gateway question. `docs/KNOWN_ISSUES.md` tracks carryover review findings (spec promises not yet honored by code).
- **References to verify:** none in specs proper.
- **Status:** doc-only update.

---

## Pending cross-references

When you land a spec change, move it from "pending review" up here for visibility, then back to "verified" when the dependent spec is updated.

- `pattern-store.md` v1 (2026-05-13) — three new event types (`pattern.recorded`, `pattern.matched`, `pattern.evicted`) to land in `event-bus-and-trace-catalog.md §6.5b` when Phase 2.5 implementation does. Routing-engine §5.5 sample-size-weighting clarification and evaluator.md reconciliation **verified 2026-05-14** (see "Pattern-store ↔ evaluator reconciliation sweep" above).
- `evaluator.md` v1 (2026-05-13) — new `eval` domain + three event types (`eval.started`, `eval.completed`, `eval.failed`) to land in `event-bus-and-trace-catalog.md §4.5` / §6 when Phase 3 implementation does. New `/analytics/quality` endpoint + additive `include_eval` param on `/analytics/cost` + additive `evaluations` array on `/analytics/turns/{id}` to land in `analytics-api.md` at implementation time. Optional `evaluate:` block in `workload.yaml` schema to land in `benchmark.md §3.1` plus quality column in `§8` report. STRATEGY.md §6.7 resolution + §5 dated decision entry and pattern-store reconciliation **verified 2026-05-14**.
- `gateway.md` v0 (2026-05-13) — STRATEGY.md edits landed on owner sign-off; the additive `gateway_key_id` / `inbound_shape` payload fields in `event-bus-and-trace-catalog.md` §6.3 / §6.6 land when the gateway implementation does.
