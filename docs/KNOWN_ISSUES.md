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

## Event bus

### 🟢 Sensitivity upgrade rule unenforced

§4.4.1 says classification can only move *toward less private*. `make_event` accepts any `sensitivity` override without checking it's less private than the catalog default.

---

## Gateway

### 🟢 Gateway clients always trigger `per_message_override` slot win

OpenAI / Anthropic SDKs always include `model` in the request body, so `route.decided.chain` reports `policy=per_message_override`, `verdict=chose` on every gateway request. The `rule`, `pattern`, `workspace_default`, and `global_default` slots are unreachable unless the client deliberately omits `model`. Correct per `gateway.md §V` (treat inbound `model` as a per-message override) but worth knowing when reading gateway traces. Documented in AGENTS.md "Gotchas."

---

## Gaps that aren't bugs (but worth tracking)

Things that aren't promised by any spec but probably should be. AI agents proposing work in adjacent areas should know they're missing.

- **No context-assembler spec for skill activation / history compression.** [`docs/specs/context-assembler.md`](specs/context-assembler.md) v1+§5.1 covers cache-breakpoint placement and the minimum-cacheable-prefix rule. Skill activation, history compression, and behavior near the context window are not yet specified. See `STRATEGY.md §6`.
- **No multi-user / team-level analytics rollups.** Gateway v1 stamps `gateway_key_id` per call; teams of keys, multi-workspace per key, and tenant aggregation are deferred per `gateway.md §11`.
- **Pattern store v2 (embedding fingerprint) not specced.** v1 fingerprint is structural (intent regex tags + tool-use signals + length bucket); embedding-based fingerprinting would lift K-NN selectivity but is deferred until v1 K-NN data shows a concrete shortfall.
