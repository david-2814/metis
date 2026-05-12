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

### 🔴 `provider_raw` is not excluded from equality / hashing

[`canonical-message-format.md §6.5`](specs/canonical-message-format.md) says: *"`provider_raw` is not part of equality comparisons or hashing."* [`src/metis/canonical/messages.py`](../src/metis/canonical/messages.py) uses `msgspec.Struct(frozen=True)`, which includes every field in `__eq__` and `__hash__`.

```text
>>> a = MessageMetadata(model='m', provider='p', provider_raw={'x': 1})
>>> b = MessageMetadata(model='m', provider='p', provider_raw={'x': 2})
>>> a == b
False
>>> hash(a)
TypeError: unhashable type: 'dict'
```

Fix options: store `provider_raw` outside `MessageMetadata` (side table keyed by message id), or replace the field with a JSON-string and override `__eq__`/`__hash__` to skip it.

### 🟡 No tolerance path for unknown content block types

`canonical-message-format.md §10.3` requires *"skip with warning rather than crash"* for unknown `type` discriminators. msgspec's tagged union decoding raises on unknown types. Currently no test exercises this; no impact while the catalog is closed.

### 🟡 `ImageSource.kind: str` is unconstrained

Spec says `Literal["base64", "url", "file_ref"]`. Impl uses `str`. msgspec will happily decode `{"kind": "garbage", ...}`.

---

## Adapters

### 🔴 `AnthropicAdapter` `ImageBlock.kind="file_ref"` is interpreted as base64

[`src/metis/adapters/anthropic.py`](../src/metis/adapters/anthropic.py) treats `file_ref` source by stuffing the workspace-relative path string into Anthropic's base64 `data` field. The path needs to be resolved (read bytes, base64-encode, fill `media_type`). Currently a `file_ref` ImageBlock through this adapter would deliver garbled payload to the API. No test exercises `file_ref`.

### 🟡 Anthropic `supports_streaming` honesty — verify

Last reviewed before `adapters/streaming.py` landed. After streaming layer arrived, the `supports_streaming=True` capability declaration may now be honest. Spend 60s running with streaming enabled and confirm; if it still doesn't drive `stream()`, the declaration is a lie that routing will rely on.

### 🟢 `CanonicalResponse` shape divergence from spec

Spec §3.3 returns `CanonicalResponse.message: Message`. Impl returns `content: list[ContentBlock]` + `model` + `provider`. Acknowledged in [`adapters/protocol.py`](../src/metis/adapters/protocol.py) docstring and AGENTS.md "Implementation conventions." Reasonable trade-off; the spec needs an entry in CHANGES.md and a §3.3 edit so downstream readers don't write to the original shape.

---

## Tool dispatcher

### 🔴 Canonical JSON Schema subset is not enforced at registration

`tool-dispatcher.md §7.1` says tools registering with `oneOf` / `$ref` / `allOf` / `not` / etc. must fail loudly. [`src/metis/tools/dispatcher.py`](../src/metis/tools/dispatcher.py) calls only `jsonschema.Draft7Validator.check_schema()` — that validates *valid JSON Schema*, not the canonical *subset*. The canonical module exposes [`validate_tool_input_schema`](../src/metis/canonical/tools.py) for this exact check; it isn't called.

Fix is one line: add `validate_tool_input_schema(definition.input_schema)` before the existing `check_schema` call.

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

### 🔴 Per-(provider, model) availability collapsed to per-provider only

`routing-engine.md §4.5` makes per-(provider, model) the *default* scope: a 401 on Opus should not blackout Sonnet. [`src/metis/routing/availability.py`](../src/metis/routing/availability.py) tracks per-provider only, with a comment acknowledging the deviation as deferred. Spec test §10.1.19 cannot pass.

### 🔴 Consecutive-failure window is not enforced

Spec §4.5.1: *"≥5 consecutive failures within 2 minutes."* Impl increments unbounded — a failure today plus four next week trips the breaker. The counter needs to reset (or use a sliding window) when `now - last_failure_at > 120s`.

### 🔴 DNS / network error doesn't trigger immediate Unavailable

Spec §4.5.1 says any DNS or network error reaching a provider's host marks the whole provider Unavailable. Impl only treats `AUTH` as immediate. `NETWORK` errors fall into the consecutive-failure counter, missing the explicit signal.

### 🟡 Multi-model escalation to provider-wide not implemented

Spec §4.5.1: *"≥3 distinct models from one provider hit Unavailable within 2 minutes → the whole provider."* Cannot be implemented while per-model is collapsed (above).

### 🟢 Bare `@haiku` (no trailing text) accepted as override

Spec §9.2: *"The override syntax must be at the start of the message and followed by whitespace."* [`overrides.py`](../src/metis/routing/overrides.py) accepts a one-token message (`@haiku` alone → rest=""). Either reject or document.

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

[`trace/store.py`](../src/metis/trace/store.py) uses `json.dumps(..., default=str)`. Payload fields like `tool.confirmation_requested.expires_at` become ISO strings on write and stay strings on read. Downstream readers expecting `datetime` need to round-trip through `msgspec.convert`. Either standardize on stringly-typed payload reads (and document it) or store payloads via `msgspec.json.encode` and decode back via the registered Struct class on read.

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
