# Known Issues

**Last updated:** 2026-05-12

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

### 🟢 `AutoAllowHandler` is the default confirmation handler

Auto-approves *everything* including WRITE/EXECUTE/NETWORK. Fine for single-user dev (and documented in AGENTS.md gotchas). Do not ship anywhere shared without swapping in a real handler.

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

## Gaps that aren't bugs (but worth tracking)

Things that aren't promised by any spec but probably should be. AI agents proposing work in adjacent areas should know they're missing.

- **No context-assembler spec for skill activation / history compression.** [`docs/specs/context-assembler.md`](specs/context-assembler.md) v1 covers cache-breakpoint placement only. Skill activation, history compression, and behavior near the context window are not yet specified. See `STRATEGY.md §6`.
- **No pattern-store spec.** Referenced by `routing-engine.md §5.5`; mechanics undefined.
- **No evaluator spec.** Architecture mentions it; no contract.
- **No tool-confirmation REST endpoint.** `server-api.md §4.2` specs it; not wired. Dispatcher uses `AutoAllowHandler` (see above).
- **No benchmark / savings methodology.** Strategy-level gap; see `STRATEGY.md §6.4`.
