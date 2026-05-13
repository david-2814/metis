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
- *(planned, later phases)* `skill-format.md`, `memory-store.md`, `pattern-store.md`, `evaluator.md`.

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

When changing a spec, the dependent specs (right column whose left column is the changed spec) must be checked.

---

## Change log

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

- `skill-format.md` (planned) — `skill.loaded.source` field added 2026-05-12 should be documented when this spec lands.
- `pattern-store.md` (planned) — `routing-engine.md §5.5` references the pattern store's K-nearest aggregation and `cost_weight`; when the pattern-store spec lands, cross-check that the math and config knobs match.
