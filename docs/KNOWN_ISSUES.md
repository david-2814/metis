# Known Issues

**Last updated:** 2026-05-14

Carryover findings from prior implementation reviews that haven't been fixed yet. These are bugs that look correct in isolation — invariants the specs claim that the code doesn't quite honor, or capability declarations that are honest in intent but wrong in practice. AI agents working in the repo should know about them so they don't:

1. Build new code that depends on the broken invariant.
2. Write tests that lock in the broken behavior.
3. Quote the spec back at the user when the impl quietly disagrees.

Severity legend:

- **🔴 high** — silent correctness bug or substitutability invariant violation. Fix before the surface in question grows more consumers.
- **🟡 medium** — spec promise unfulfilled; works correctly today but will bite when the dependent surface lands.
- **🟢 low** — cosmetic, documented divergence, or design decision worth revisiting.

When you fix one, **delete the entry**. This file is not a changelog; it's a watchlist.

---

## Adapters

### 🟢 `CanonicalResponse` shape divergence from spec

Spec §3.3 returns `CanonicalResponse.message: Message`. Impl returns `content: list[ContentBlock]` + `model` + `provider`. Acknowledged in [`adapters/protocol.py`](../packages/metis-core/src/metis_core/adapters/protocol.py) docstring and AGENTS.md "Implementation conventions." Reasonable trade-off; the spec needs an entry in CHANGES.md and a §3.3 edit so downstream readers don't write to the original shape.

---

## Tool dispatcher

### 🟢 `get_definitions_for_session(session)` doesn't accept a session

Spec §3.4 signature names the session arg (used to filter memory tools from worker sessions per §6.2.1). Current impl returns all tools globally. Phase 2+; signature shape should match now so callers don't get rewritten later.

---

## Routing engine

### 🟢 Bare `@haiku` (no trailing text) accepted as override

Spec §9.2: *"The override syntax must be at the start of the message and followed by whitespace."* [`overrides.py`](../packages/metis-core/src/metis_core/routing/overrides.py) accepts a one-token message (`@haiku` alone → rest=""). Either reject or document.

---

## Event bus

### 🟢 Sensitivity upgrade rule unenforced

§4.4.1 says classification can only move *toward less private*. `make_event` accepts any `sensitivity` override without checking it's less private than the catalog default.

---

## Trace store

### 🟢 No `FOREIGN KEY (session_id) REFERENCES sessions(id)`

`event-bus-and-trace-catalog.md §7.1` declares the FK. Acceptable now that `sessions` table exists in the same DB; add when convenient.

---

## CLI

### 🟢 Stdin reader thread leak on shutdown

Documented in `chat.py::_async_input` docstring. The input thread is daemon — killed at process exit instead of joined. Acceptable; documented for context.

---

## Gateway

### 🟡 Per-key analytics roll-up has no HTTP surface

`gateway_key_id` and `inbound_shape` are stamped on every `llm.call_completed` and `turn.completed` (verified during Wave-5 smoke 2026-05-14), but `/analytics/cost` does not accept `group_by=gateway_key` and there is no `/analytics/by_key` endpoint. Operators currently roll up cost-by-key via direct SQL on the trace DB. Tracked against `gateway.md §V` ("Add a `group_by=gateway_key` dimension to `/analytics/cost` in a follow-up spec change"); both `_COST_GROUP_BY_ALLOWED` and the spec section need to land together. Spec impact: `analytics-api.md §4.1` group_by enum and gateway.md §V.

### 🟢 Gateway clients always trigger `per_message_override` slot win

OpenAI / Anthropic SDKs always include `model` in the request body, so `route.decided.chain` reports `policy=per_message_override`, `verdict=chose` on every gateway request. The `rule`, `pattern`, `workspace_default`, and `global_default` slots are unreachable unless the client deliberately omits `model`. Correct per `gateway.md §V` (treat inbound `model` as a per-message override) but worth knowing when reading gateway traces. Documented in AGENTS.md "Gotchas."

---

## Gaps that aren't bugs (but worth tracking)

Things that aren't promised by any spec but probably should be. AI agents proposing work in adjacent areas should know they're missing.

- **No context-assembler spec for skill activation / history compression.** [`docs/specs/context-assembler.md`](specs/context-assembler.md) v1+§5.1 covers cache-breakpoint placement and the minimum-cacheable-prefix rule. Skill activation, history compression, and behavior near the context window are not yet specified. See `STRATEGY.md §6`.
- **No multi-user / team-level analytics rollups.** Gateway v1 stamps `gateway_key_id` per call; teams of keys, multi-workspace per key, and tenant aggregation are deferred per `gateway.md §11`.
- **Pattern store v2 (embedding fingerprint) not specced.** v1 fingerprint is structural (intent regex tags + tool-use signals + length bucket); embedding-based fingerprinting would lift K-NN selectivity but is deferred until v1 K-NN data shows a concrete shortfall.
