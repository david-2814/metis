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

## Canonical types

### 🟡 No tolerance path for unknown content block types

`canonical-message-format.md §10.3` requires *"skip with warning rather than crash"* for unknown `type` discriminators. msgspec's tagged union decoding raises on unknown types. Currently no test exercises this; no impact while the catalog is closed.

### 🟡 `ImageSource.kind: str` is unconstrained

Spec says `Literal["base64", "url", "file_ref"]`. Impl uses `str`. msgspec will happily decode `{"kind": "garbage", ...}`.

---

## Adapters

### 🔴 `AnthropicAdapter` `ImageBlock.kind="file_ref"` is interpreted as base64

[`packages/metis-core/src/metis_core/adapters/anthropic.py`](../packages/metis-core/src/metis_core/adapters/anthropic.py) treats `file_ref` source by stuffing the workspace-relative path string into Anthropic's base64 `data` field. The path needs to be resolved (read bytes, base64-encode, fill `media_type`). Currently a `file_ref` ImageBlock through this adapter would deliver garbled payload to the API. No test exercises `file_ref`.

### 🟡 Anthropic `supports_streaming` honesty — verify

Last reviewed before `adapters/streaming.py` landed. After streaming layer arrived, the `supports_streaming=True` capability declaration may now be honest. Spend 60s running with streaming enabled and confirm; if it still doesn't drive `stream()`, the declaration is a lie that routing will rely on.

### 🟢 `CanonicalResponse` shape divergence from spec

Spec §3.3 returns `CanonicalResponse.message: Message`. Impl returns `content: list[ContentBlock]` + `model` + `provider`. Acknowledged in [`adapters/protocol.py`](../packages/metis-core/src/metis_core/adapters/protocol.py) docstring and AGENTS.md "Implementation conventions." Reasonable trade-off; the spec needs an entry in CHANGES.md and a §3.3 edit so downstream readers don't write to the original shape.

---

## Tool dispatcher

### 🟡 No per-session concurrency cap

Spec §4.1 mandates a default cap of 4 concurrent dispatches per session. Impl runs every dispatch immediately. Risk: a model emitting 12 parallel tool_use blocks all run at once.

### 🟡 `tool.called` emitted before workspace escape detection

Spec §9.2 worked example shows escape rejection emits `tool.failed` *without* a preceding `tool.called`. Impl emits `tool.called` first, then catches `WorkspaceEscapeError` lazily inside the tool. Effect: orphan `tool.called` events on escape rejections.

### 🟡 `confirmation_request_id` is not a ULID

Spec catalog says `confirmation_request_id: str # ULID`. Impl uses `f"conf_{tool_use.id}"`. Unique but not ULID-sortable.

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

### 🟡 `bus.subscriber_registered` / `bus.subscriber_unregistered` are never emitted

The structs are in the catalog as Phase 1, but `EventBus.subscribe` / `.unsubscribe` don't emit. Either wire emission or strike from Phase 1.

### 🟡 `FastPathHandlerError` registration check missing

`event-bus-and-trace-catalog.md §9.1 test 4` requires registering a `@slow`-annotated handler with `fast_path=True` to raise. The error class exists; `subscribe()` performs no check.

### 🟡 No `bus.gap_detected` mechanism

Spec §6.10 / §9.1 test 3 defines a startup ULID-gap scan; the payload struct is registered but no detector runs.

### 🟢 Sensitivity upgrade rule unenforced

§4.4.1 says classification can only move *toward less private*. `make_event` accepts any `sensitivity` override without checking it's less private than the catalog default.

---

## Trace store

### 🟡 Datetime fields in payloads stored as strings, never re-typed

[`trace/store.py`](../packages/metis-core/src/metis_core/trace/store.py) uses `json.dumps(..., default=str)`. Payload fields like `tool.confirmation_requested.expires_at` become ISO strings on write and stay strings on read. Downstream readers expecting `datetime` need to round-trip through `msgspec.convert`. Either standardize on stringly-typed payload reads (and document it) or store payloads via `msgspec.json.encode` and decode back via the registered Struct class on read.

### 🟢 No `FOREIGN KEY (session_id) REFERENCES sessions(id)`

`event-bus-and-trace-catalog.md §7.1` declares the FK. Acceptable now that `sessions` table exists in the same DB; add when convenient.

---

## CLI

### 🟢 Stdin reader thread leak on shutdown

Documented in `chat.py::_async_input` docstring. The input thread is daemon — killed at process exit instead of joined. Acceptable; documented for context.

---

## Gaps that aren't bugs (but worth tracking)

Things that aren't promised by any spec but probably should be. AI agents proposing work in adjacent areas should know they're missing.

- **No context-assembler spec.** Biggest cost lever; design undefined. See `STRATEGY.md §6`.
- **No pattern-store spec.** Referenced by `routing-engine.md §5.5`; mechanics undefined.
- **No evaluator spec.** Architecture mentions it; no contract.
- **No prompt-caching strategy.** `AdapterCapabilities.supports_prompt_caching` exists; no adapter writes `cache_control` markers. Leaving 5–10× on the table for any session with stable system prompt + tools.
- **No tool-confirmation REST endpoint.** `server-api.md §4.2` specs it; not wired. Dispatcher uses `AutoAllowHandler` (see above).
- **No benchmark / savings methodology.** Strategy-level gap; see `STRATEGY.md §6.4`.
