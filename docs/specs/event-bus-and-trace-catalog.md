# Event Bus and Trace Catalog

**Status:** Draft v3.1
**Last updated:** 2026-05-14
**Owner:** _your name_

> **v3.1 changes (2026-05-14):** `eval` domain added to closed list (§4.5)
> with three new event types — `eval.started`, `eval.completed`, `eval.failed`
> (§6.12) per `evaluator.md §8`. Three new pattern event types added under
> the existing pattern domain (§6.5b) — `pattern.recorded`, `pattern.matched`,
> `pattern.evicted` per `pattern-store.md §10`. All six payloads landed in
> `events/payloads.py` and `PAYLOAD_REGISTRY`. Sensitivity floor is
> `pseudonymous` for five; `eval.completed`'s floor is `user_controlled`
> (the worst case, when `signals.rationale_redacted` is populated) and
> downgrades to `pseudonymous` when the rationale field is absent — a
> move toward less private, which §4.4.1 explicitly allows.

> **v3 changes:** Streaming events explicitly excluded from catalog (§4.5);
> streaming server removed from bus subscriber table (§5.4) — they receive
> events directly from the agent loop, not via the bus. Cross-reference to
> `EventFrame` (§5.4 note). Error class enums extended in `llm.call_failed`
> (§6.3) and `tool.failed` (§6.4) to match adapter and dispatcher contracts.
> `tool.confirmation_requested` and `tool.confirmation_resolved` added (§6.4).
> `block_dropped` confirmed as log-only, not catalog event.

> **v2 changes:** `route.decided` exactly-once preserved by introducing
> `route.overridden` as a distinct type (§6.5b). Pattern domain lifted into
> its own §6.5b. `bus.gap_detected` defined (§6.10). Bus diagnostics
> (`bus.overflow`, `bus.handler_error`) moved out of the catalog and into
> structured logs to avoid recursion and chicken-and-egg failures (§3.5,
> §5.2, §6.10). Redelivery claim narrowed to "across restarts, not
> in-process" (§5.1). Trace store committed to SQLite WAL +
> `synchronous=NORMAL` (§3.4, §7.2). Memory snapshotter moved off fast path
> (§5.4). Dynamic sensitivity on opt-in (§4.4). Delegation phase asymmetry
> documented (§6.8). `bus.gap_detected` test rewritten (§9.1). Various nits.

---

## 1. Purpose

This document specifies the event bus — the in-process pub/sub spine that decouples the agent loop from observability, persistence, and analytics — and the closed catalog of event types that flow through it. Every meaningful action in the system emits an event; subscribers consume events; the trace store persists them.

The catalog is the contract. Adding a new event type is a deliberate spec change. Removing or renaming a type is a breaking change with a major version bump.

This spec is referenced by:
- `canonical-message-format.md` (events that touch messages)
- `routing-engine.md` (the `route.decided` event and routing auxiliaries)
- `streaming-protocol.md` (forthcoming — defines client subscriptions)
- `skill-format.md` (forthcoming — events for skill load/create/modify)

---

## 2. Goals and non-goals

### 2.1 Goals

1. **Decouple emit from consume.** The agent loop emits events without knowing or caring who consumes them.
2. **At-least-once persistence.** Every emitted event reaches the trace store, even on partial crashes. Duplicates are tolerable; loss is not.
3. **Closed type catalog.** Every event type and its payload schema is enumerated in this document.
4. **Causal traceability.** Events form chains via `parent_event_id`; "why did this happen?" is a walk back to the root cause.
5. **Cheap.** Emit must add ≤100µs to the agent loop on the synchronous path. Async fanout to subscribers must not block the emitter.
6. **Sensitivity-tagged.** Every event type declares its data sensitivity, so future sync and cross-user features can filter by classification.

### 2.2 Non-goals

1. **Cross-process or cross-machine event delivery.** Single Python process. If we ever need cross-process, an HTTP/WebSocket adapter subscribes to the bus and forwards — but the bus itself stays in-process.
2. **Exactly-once delivery.** Subscribers must dedupe by `event.id` if they care.
3. **Durable queue with retention policies.** The trace store is the durable record. The bus itself holds events only in transit.
4. **Replace structured logging.** Application logs (debug, info, warn, error) live alongside the event bus. Events describe domain actions; logs describe internal state.
5. **Priority queues, ordering guarantees beyond per-session monotonicity.** Events for one session are monotonic. Across sessions, no guarantees.

---

## 3. The bus

### 3.1 Interface

```python
class EventBus:
    def emit(self, event: Event) -> None:
        """Synchronous from caller's perspective. Appends to the dispatch queue,
        which is drained asynchronously. Validates payload against the type's
        schema; raises EventValidationError on bad payload (dev mode) or logs
        and drops (prod mode)."""

    def subscribe(self, sub: Subscription) -> SubscriptionHandle:
        """Register a handler for events matching the filter. Returns a handle
        used to unsubscribe."""

    def unsubscribe(self, handle: SubscriptionHandle) -> None:
        """Remove a subscription. Idempotent."""

class Subscription:
    filter: EventFilter
    handler: Callable[[Event], Awaitable[None]]   # async
    name: str                                      # for diagnostics
    fast_path: bool = False                        # see §3.4

class SubscriptionHandle:
    id: str                                        # opaque, returned by subscribe()
    subscription: Subscription                     # the original (for diagnostics)

class EventFilter:
    session_ids: set[str] | None = None            # None = all sessions
    event_types: set[str] | None = None            # None = all types
    actors: set[Actor] | None = None               # None = all actors
```

`emit` returns immediately after enqueueing. The dispatch worker drains the queue and fans out to matching subscribers. Handlers run on the asyncio event loop; slow handlers do not block other handlers or the emitter.

### 3.2 Validation mode

The bus runs in one of two modes:

- **Strict** (development): payload schema mismatches raise `EventValidationError`. Tests must run in strict mode.
- **Lenient** (production): payload schema mismatches are logged at WARN and the event is dropped (not enqueued). The emitter is not interrupted.

Mode is set by environment variable. Default in development is strict; default in production is lenient.

### 3.3 Persistence

The trace store is one subscriber, registered automatically by the server at startup with `EventFilter()` (all events). It writes each event to SQLite as it arrives.

The bus does *not* persist events itself. If the trace store subscriber is slow or crashes, events queue up in the dispatch worker's buffer (bounded — see §3.5). Events that have been enqueued but not yet persisted are lost on hard process crash.

This is acceptable because:
- The dispatch buffer drains in milliseconds in steady state.
- The trace store is durable once it persists.
- A hard crash is rare; slight event loss in that case is tolerable.

If stronger guarantees are needed later (write-ahead log to disk before fanout), they can be added without changing the bus interface.

### 3.4 Fast-path vs. batch subscribers

Subscribers are tagged `fast_path: true` or `false`. Fast-path subscribers run inside the dispatch worker, synchronous with each event:

- Trace store (writes one row to SQLite in WAL mode with `synchronous=NORMAL`; typically <1ms).
- Streaming-server bridge (forwards bus events to attached clients; <1ms). Note: this is *only* the bridge for bus events. The streaming server *also* receives streaming-only events directly from the agent loop on a separate channel (per §4.5.1 and `streaming-protocol.md` §5.1).

Batch subscribers run on their own schedules and query the trace store directly:

- Evaluator (computes analytics nightly or on request).
- Pattern store outcome computation (runs at session end).
- Dashboard data assembly (queries on view).
- Memory snapshotter (captures before/after for diff display; runs after-the-fact, not on every `memory.updated` event).

The convention: anything that can't reliably finish in <1ms per event must be batch, not fast-path. Adding a slow handler as fast-path is a bug — it stalls all event processing.

The trace store's SQLite mode is committed (see §7.2 for full details and durability trade-off). The "writes one row, sub-millisecond" claim depends on this configuration; the default `synchronous=FULL` mode would not meet the fast-path budget.

### 3.5 Backpressure

The dispatch queue is bounded (default 10,000 events). On overflow:

- Emit raises `EventBusOverflowError` to the caller.
- A structured log entry is written (level `ERROR`, with the rejected event type and current queue depth). This goes to the application log, *not* through the bus — the bus is, by definition, the thing that's full.
- The caller (typically the agent loop) is expected to handle this by either retrying or ignoring; the spec recommends ignore-and-log to avoid hot-loop amplification.

In v1 this should never happen at single-user scale. The bound is a safety net against runaway emit loops, not a backpressure-shaping mechanism.

> *Bus diagnostics (overflow, handler errors, gap detection) are written as structured logs rather than events. They describe the bus's own health and would create chicken-and-egg problems if routed through the bus they describe (e.g., overflow events while overflowing). The catalog (§6) covers domain events; bus diagnostics are observability about the bus itself.*

### 3.6 Subscription lifecycle

Subscribers register at server startup or session creation. Unsubscribe at shutdown. The bus does not retain references to handlers across server restarts; subscribers re-register on startup.

---

## 4. The Event type

### 4.1 Envelope

Every event has the same envelope:

```python
class Event:
    # Identity
    id: str                          # ULID, monotonic per process
    timestamp: datetime              # microsecond precision UTC

    # Scoping
    session_id: str
    turn_id: str | None              # null for session-level and system events

    # Causality
    parent_event_id: str | None      # the event that caused this one, if any

    # Type
    type: str                        # dotted lowercase, e.g. "llm.call_started"
    actor: Actor                     # USER | AGENT | SYSTEM | TOOL | WORKER

    # Payload
    payload: dict                    # validated against the type's schema

    # Sensitivity classification
    sensitivity: Sensitivity         # PRIVATE | USER_CONTROLLED | PSEUDONYMOUS | AGGREGATABLE

class Actor(StrEnum):
    USER   = "user"
    AGENT  = "agent"      # the planner-role LLM
    SYSTEM = "system"     # the server itself (router, dispatcher, etc.)
    TOOL   = "tool"       # tool dispatcher
    WORKER = "worker"     # delegated sub-agent

class Sensitivity(StrEnum):
    PRIVATE          = "private"           # contains user prompt, file content, etc.
    USER_CONTROLLED  = "user_controlled"   # skill bodies; user chose to make shareable
    PSEUDONYMOUS     = "pseudonymous"      # structural metadata (file types, tags)
    AGGREGATABLE     = "aggregatable"      # outcomes safe for cross-user aggregation
```

### 4.2 Identity and ordering

- `id` is a ULID generated at emit time. ULIDs are sortable, monotonic per process, and globally unique.
- Events for one session are emitted from a single process, so ULIDs give per-session monotonic order.
- Cross-session ordering is not guaranteed and not relied upon by any consumer.

### 4.3 Causal chains

`parent_event_id` is a single pointer to the event that directly caused this one. Chains are reconstructed by walking pointers backward. Examples:

```
turn.started
  └─ llm.call_started               (parent: turn.started)
       └─ llm.call_completed         (parent: llm.call_started)
            └─ tool.called           (parent: llm.call_completed — assistant emitted tool_use)
                 └─ tool.completed   (parent: tool.called)
                 └─ llm.call_started (parent: tool.completed — agent loop made the next call)
                      └─ ...
```

Branches in the chain (one event causing many) are represented by multiple events each pointing back to the same parent — not by a single event with multiple children. The chain is a tree of parent pointers.

Events without a parent (e.g., `session.created`, `turn.started` from user input) have `parent_event_id: null`.

### 4.4 Sensitivity classifications

| Class            | Meaning                                                           | Examples                                |
|------------------|-------------------------------------------------------------------|-----------------------------------------|
| `private`        | Contains user prompts, file content, command outputs.             | `turn.started`, `tool.completed` body   |
| `user_controlled`| User explicitly chose to share/sync this.                         | `skill.created` (skill body)            |
| `pseudonymous`   | Structural metadata, no raw user content.                         | `routing.policy_invalid`, fingerprint tags |
| `aggregatable`   | Safe to include in cross-user aggregations with k-anonymity.      | Pattern outcome rollups, `feedback.explicit` |

Every event type declares its *default* sensitivity in the catalog. The default for any new type is `private` — opting up to less restrictive requires deliberate design.

This classification gates the future sync layer and any future cross-user features. No code in v1 acts on it (everything stays local). But the tag is *recorded* on every event from day one so future features have the data.

#### 4.4.1 Dynamic sensitivity for opt-in payloads

Some event types have payload fields that are populated only when the user opts in. The most important case is `turn.started.user_message_text_redacted`: nullable by default, populated only if the user enabled trace sharing.

When such a field is populated, the event's *recorded* sensitivity may upgrade to a less-restrictive class than the catalog default. The rule:

- The catalog declares the *floor* sensitivity for an event type — the **worst case**, i.e., the most-private classification the event can have when all opt-in fields are populated.
- An event's actual `sensitivity` value is computed at emit time based on which optional fields are populated.
- The classification can only move *toward less private* (i.e., from `private` to `user_controlled` to `pseudonymous` to `aggregatable`) — never toward more private than the floor. `make_event` enforces this: a sensitivity override more private than the catalog floor raises `EventValidationError`.

Concrete examples:

- `turn.started` floor is `private`. A `turn.started` event with `user_message_text_redacted: null` is recorded as the floor `private`. The same event type with the field populated (because the user opted into trace sharing) is recorded as `user_controlled` — a downgrade toward less private, which the rule allows.
- `eval.completed` floor is `user_controlled`. With `signals.rationale_redacted` populated, recorded as the floor `user_controlled`. With the rationale field absent (heuristic verdict, or LLM verdict without rationale opt-in), the subscriber passes `pseudonymous` — again a downgrade toward less private.

This keeps the catalog contract honest: the floor is the most-private possible recording for that event type, and the actual sensitivity tag reflects what was actually included.

### 4.5 Type names

Convention: `<domain>.<verb_phrase>` in lowercase, dot-separated.

```
session.created
turn.started
llm.call_started
tool.called
route.decided
skill.loaded
delegate.started
memory.updated
feedback.explicit
```

Domains: `session`, `turn`, `llm`, `tool`, `route`, `skill`, `memory`, `delegate`, `feedback`, `bus`, `pattern`, `provider`, `eval`. Closed list. Adding a domain is a spec change.

#### 4.5.1 Streaming-only domains (NOT in this catalog)

The streaming protocol (`streaming-protocol.md` §5.3) defines a separate family of transient event types for live UI updates. Their domains are `message`, `text`, `thinking`, plus the `tool.use_*` sub-namespace (`tool.use_start`, `tool.use_input_delta`, `tool.use_end`).

These are **not** bus catalog events:

- They are not persisted in the trace store.
- The trace store writer does not receive them.
- They are reconstructible from the persisted `Message` content and the `usage` totals on `llm.call_completed`.
- They flow on a separate in-process channel from the agent loop directly to the streaming server.

The names `message.*`, `text.*`, `thinking.*`, `tool.use_*` are reserved for streaming use; bus catalog events MUST NOT use these prefixes. The `tool` domain's bus events (`tool.called`, `tool.completed`, `tool.failed`, `tool.input_invalid`, `tool.confirmation_*`) are distinct from the streaming `tool.use_*` events — different verbs, no collision.

---

## 5. Subscriber behavior

### 5.1 Handler contract

```python
async def handler(event: Event) -> None:
    ...
```

Handlers MUST:
- Tolerate unknown event types (skip with debug log, do not raise). Forward compatibility.
- Not raise for handled errors. Unhandled exceptions are caught by the dispatcher, logged, and the event is dropped for that subscriber only — other subscribers still receive it.
- Complete in <1ms if `fast_path: true`; otherwise the handler should be `fast_path: false` and operate from the trace store.

Handlers SHOULD:
- Be idempotent across server restarts. v1 has no in-process redelivery, but a subscriber that crashes mid-handle and restarts may re-process the tail of events the dispatcher had already attempted to deliver. (Implementation detail: the dispatch worker doesn't track per-subscriber acknowledgments.) Subscribers that care about exactly-once semantics dedupe by `event.id`.

> *In-process redelivery does not happen in v1. The dispatcher fans out each event once per matching subscriber, then moves on. A handler raising means the event is dropped for that subscriber and that's it. Exactly-once across restarts would require per-subscriber acknowledgment tracking, which v1 doesn't implement; it's a Phase 3+ concern if it becomes necessary.*

### 5.2 Handler error handling

If a handler raises:
- The exception is caught by the dispatcher.
- A structured log entry is written at level `WARN` with the subscription name, failed event id, event type, and the exception. This goes to the application log, *not* through the bus (see §3.5 rationale on bus diagnostics).
- The original event continues to other subscribers.
- The failing subscription is *not* automatically removed. Persistent failures appear in the application log; the operator decides whether to remove.

This avoids the recursive-failure problem where a subscriber that filters on all events would receive its own error notifications and fail again, generating more error notifications, indefinitely.

### 5.3 Replay

Replay (used by streaming protocol on client reconnect) is *not* a bus operation. It's a trace store query: `SELECT * FROM events WHERE session_id = ? AND id > ? ORDER BY id`. The bus only fans out live events; historical replay queries the persistent layer directly.

### 5.4 Built-in subscribers

Registered automatically at server startup:

| Subscriber             | Filter                | Fast path | Purpose                                |
|------------------------|-----------------------|-----------|----------------------------------------|
| Trace store writer     | All events            | Yes       | Append to SQLite (WAL mode; see §7.2). |
| Streaming bus bridge   | Per-client filter     | Yes       | Forward bus events to attached WebSocket clients (wraps each event in an `EventFrame`, see `streaming-protocol.md` §4.2). |
| Cost accumulator       | `llm.call_completed`  | Yes       | Update running session cost.           |
| Pattern outcome        | `session.ended`       | No        | Compute fingerprint + outcome.         |
| Memory snapshotter     | `memory.updated`      | No        | Capture before/after for diff display via batch read of the memory file. |

Additional subscribers may be registered by analytic plugins in Phase 3+.

The streaming server has *two* input channels: the bus bridge (above) for catalog events, and a direct channel from the agent loop for streaming-only events (per §4.5.1). On the wire, both are wrapped in `EventFrame` and sent to clients in a single ordered stream. Clients see one merged stream and don't need to know about the internal split.

---

## 6. The catalog

This section enumerates every event type, its payload, its sensitivity, its parent type (the typical event it descends from), and the phase it ships in.

For each event:

> **Type:** dotted name
> **Sensitivity:** classification
> **Phase:** which project phase first emits this
> **Actor:** typical emitter
> **Parent:** typical parent event type
> **Payload:**

### 6.1 Session domain

#### `session.created`

> **Sensitivity:** `pseudonymous`
> **Phase:** 1
> **Actor:** SYSTEM
> **Parent:** none

```python
{
    "workspace_path": str,           # absolute, ~ expanded
    "workspace_hash": str,           # SHA-256 of workspace_path, for joining without exposing path
    "initial_active_model": str | None,
    "routing_policy_version": str,   # SHA-256 of routing.yaml contents at session start
                                     # (not mtime — restore-from-backup can reuse mtimes
                                     # across distinct files; content hash is unambiguous)
}
```

#### `session.resumed`

> **Sensitivity:** `pseudonymous`
> **Phase:** 1
> **Actor:** SYSTEM
> **Parent:** none

```python
{
    "workspace_hash": str,
    "last_event_id_at_resume": str | None,   # for replay
}
```

#### `session.ended`

> **Sensitivity:** `pseudonymous`
> **Phase:** 1
> **Actor:** SYSTEM
> **Parent:** none

```python
{
    "disposition": Literal["completed", "abandoned", "error"],
    "turn_count": int,
    "total_cost_usd": float,
    "duration_seconds": float,
}
```

`abandoned` is emitted on a configurable inactivity timeout (default 24h). `error` is emitted on unrecoverable session failure.

### 6.2 Turn domain

#### `turn.started`

> **Sensitivity:** `private`
> **Phase:** 1
> **Actor:** USER
> **Parent:** none

```python
{
    "user_message_hash": str,        # SHA-256 of message text, for dedup detection
    "user_message_text_redacted": str | None,  # populated only if user opted into trace sharing
    "estimated_input_tokens": int,
    "has_images": bool,
    "has_tool_calls_in_history": bool,
}
```

The full user message text is *not* in the event payload — it's persisted as part of the canonical Message in the session store. The event carries metadata sufficient for routing and analytics without duplicating content.

#### `turn.completed`

> **Sensitivity:** `pseudonymous`
> **Phase:** 1
> **Actor:** AGENT
> **Parent:** `turn.started`

```python
{
    "stop_reason": Literal["end_turn", "max_tokens", "stop_sequence", "tool_use"],
    "llm_call_count": int,
    "tool_call_count": int,
    "total_input_tokens": int,
    "total_output_tokens": int,
    "total_cost_usd": float,
    "wall_time_seconds": float,
}
```

#### `turn.cancelled`

> **Sensitivity:** `pseudonymous`
> **Phase:** 1
> **Actor:** USER
> **Parent:** `turn.started`

```python
{
    "reason": Literal["user_cancel", "client_disconnect", "timeout"],
    "partial_llm_calls": int,
    "partial_tool_calls": int,
}
```

### 6.3 LLM domain

#### `llm.call_started`

> **Sensitivity:** `private`
> **Phase:** 1
> **Actor:** AGENT
> **Parent:** `turn.started` (first call) or `tool.completed` (subsequent calls)

```python
{
    "model": str,                    # canonical "provider:name"
    "provider": str,
    "estimated_input_tokens": int,
    "request_id": str,               # adapter-issued, for cross-referencing logs
    "is_worker": bool,               # true if this is inside a delegated worker session
}
```

#### `llm.call_completed`

> **Sensitivity:** `pseudonymous`
> **Phase:** 1
> **Actor:** AGENT
> **Parent:** `llm.call_started`

```python
{
    "model": str,
    "provider": str,
    "input_tokens": int,
    "output_tokens": int,
    "cached_input_tokens": int,
    "cache_creation_input_tokens": int,
    "cost_usd": float,
    "pricing_version": str,
    "latency_ms": int,
    "stop_reason": Literal["end_turn", "max_tokens", "stop_sequence", "tool_use"],
    "produced_tool_calls": int,      # number of tool_use blocks in the response
    "produced_thinking_blocks": int,
}
```

#### `llm.call_failed`

> **Sensitivity:** `pseudonymous`
> **Phase:** 1
> **Actor:** AGENT
> **Parent:** `llm.call_started`

```python
{
    "model": str,
    "provider": str,
    "error_class": str,              # see ErrorClass enum in provider-adapter-contract.md §6.1:
                                     # "rate_limit" | "auth" | "server_error" | "network"
                                     # | "context_overflow" | "invalid_request" | "cancelled" | "other"
    "error_message_redacted": str,   # provider message with PII heuristically scrubbed
    "retry_count": int,              # how many retries the adapter attempted
    "latency_ms": int,
}
```

### 6.4 Tool domain

#### `tool.called`

> **Sensitivity:** `private`
> **Phase:** 1
> **Actor:** AGENT
> **Parent:** `llm.call_completed`

```python
{
    "tool_use_id": str,              # canonical id (tu_<ulid>)
    "tool_name": str,                # canonical name
    "input_hash": str,               # SHA-256 of canonical input JSON
    "input_size_bytes": int,
    "side_effects": Literal["none", "read", "write", "execute", "network"],
}
```

Input content is in the canonical `ToolUseBlock`, not in the event. Hash and size let us detect duplicate calls without storing the input twice.

#### `tool.completed`

> **Sensitivity:** `private`
> **Phase:** 1
> **Actor:** TOOL
> **Parent:** `tool.called`

```python
{
    "tool_use_id": str,
    "success": bool,
    "output_size_bytes": int,
    "latency_ms": int,
    "files_modified": list[str] | None,    # for write tools; null for others
    "command_executed": str | None,        # for execute tools; null for others
}
```

For execute and write tools, side-effect details are recorded (file paths, command strings) for audit. The actual command output is in the canonical `ToolResultBlock`.

#### `tool.failed`

> **Sensitivity:** `private`
> **Phase:** 1
> **Actor:** TOOL
> **Parent:** `tool.called`

```python
{
    "tool_use_id": str,
    "error_class": Literal["timeout", "permission_denied", "not_found", "validation_error",
                            "execution_error", "cancelled", "user_denied", "confirmation_timeout"],
    "error_message": str,
    "latency_ms": int,
}
```

#### `tool.input_invalid`

> **Sensitivity:** `pseudonymous`
> **Phase:** 1
> **Actor:** SYSTEM
> **Parent:** `llm.call_completed`

```python
{
    "tool_name": str,
    "validation_errors": list[str],
}
```

Emitted when a tool_use block's input fails JSON Schema validation against the tool's schema. The agent loop returns an error tool_result to the model.

#### `tool.confirmation_requested`

> **Sensitivity:** `private`
> **Phase:** 1
> **Actor:** SYSTEM
> **Parent:** `tool.called` (logically; the tool call is paused waiting for user response)

Emitted when a tool with `WRITE`/`EXECUTE`/`NETWORK` side effects requires user confirmation per `tool-dispatcher.md` §5.2. The tool's execution is paused until a `tool.confirmation_resolved` event is emitted (or the confirmation times out).

```python
{
    "tool_use_id": str,
    "tool_name": str,
    "side_effects": Literal["write", "execute", "network"],
    "confirmation_request_id": str,    # ULID; used by the response endpoint
    "input_summary": str,              # human-readable, redacted of long content
    "projected_modifications": list[str] | None,  # for WRITE: paths to be modified
    "command_summary": str | None,     # for EXECUTE: the command line, possibly truncated
    "expires_at": datetime,            # when the confirmation request times out
}
```

The streaming server forwards this event to all attached clients of the session; clients render a UI prompt. The user's response goes through HTTP per `server-api.md` §4.2.

#### `tool.confirmation_resolved`

> **Sensitivity:** `private`
> **Phase:** 1
> **Actor:** USER
> **Parent:** `tool.confirmation_requested`

```python
{
    "tool_use_id": str,
    "confirmation_request_id": str,
    "decision": Literal["allow", "deny", "timeout"],
    "scope": Literal["once", "session"] | None,    # null if decision is "timeout"
    "responding_client_attach_token": str | None,  # which client answered, if multiple attached
}
```

The dispatcher proceeds to execute (`allow`) or aborts (`deny`, `timeout`) based on the decision.

### 6.5 Route domain

These are defined in detail in `routing-engine.md` §7. Repeated here with full payloads:

#### `route.decided`

> **Sensitivity:** `pseudonymous`
> **Phase:** 1
> **Actor:** SYSTEM
> **Parent:** `turn.started`

Exactly one `route.decided` event per turn (per routing-engine spec §7.2). User-driven overrides after the fact emit a separate `route.overridden` event under the Pattern domain (§6.5b).

```python
{
    "chosen_model": str,
    "winner_index": int,
    "elapsed_ms": float,
    "chain": [
        {
            "policy": Literal["per_message_override", "manual_sticky", "rule",
                              "pattern", "delegate_request", "workspace_default", "global_default"],
            "verdict": Literal["not_applicable", "deferred", "rejected", "chose"],
            "candidate_model": str | None,
            "reason": str,
            "rule_name": str | None,
            "confidence": float | None,
            "pattern_alternatives": list[{"model": str, "score": float, "sample_size": int}] | None,
            "validation_failure": Literal["no_vision_support", "exceeds_context_window",
                                          "no_tool_support", "no_system_prompt_support",
                                          "no_structured_output_support",
                                          "provider_unavailable", "not_configured"] | None,
        },
        # ... one per policy in chain order
    ]
}
```

#### `routing.policy_invalid`

> **Sensitivity:** `pseudonymous`
> **Phase:** 1
> **Actor:** SYSTEM
> **Parent:** none

```python
{
    "policy_path": str,
    "errors": list[str],
    "using_last_known_good": bool,
}
```

#### `routing.provider_unavailable`

> **Sensitivity:** `pseudonymous`
> **Phase:** 1
> **Actor:** SYSTEM
> **Parent:** `llm.call_failed` (typically the failure that crossed the threshold)

```python
{
    "provider": str,
    "scope": Literal["model_specific", "provider_wide"],
    "models_affected": list[str],
    "trigger_reason": str,           # "5_consecutive_failures" | "auth_error"
                                     # | "dns_error" | "multi_model_failures"
}
```

#### `routing.provider_recovered`

> **Sensitivity:** `pseudonymous`
> **Phase:** 1
> **Actor:** SYSTEM
> **Parent:** none

```python
{
    "provider": str,
    "scope": Literal["model_specific", "provider_wide"],
    "models_recovered": list[str],
    "downtime_seconds": float,
}
```

### 6.5b Pattern domain

Pattern-domain events describe user actions on routing recommendations from the pattern store. They are distinct from `route.decided` (which describes the routing computation itself) — they describe what happened *after* the decision was surfaced to the user.

#### `route.overridden`

> **Sensitivity:** `pseudonymous`
> **Phase:** 3
> **Actor:** USER
> **Parent:** `route.decided` (the decision being overridden)

Emitted when the user runs `/route override` to apply a pattern recommendation that was deferred behind a rule. The original `route.decided` event remains intact (preserving history); this event records the swap.

```python
{
    "original_chosen_model": str,    # what route.decided picked
    "new_chosen_model": str,         # what the user chose to use instead
    "deferred_policy": str,          # the policy that originally would have produced new_chosen_model
                                     # (typically "pattern")
    "rule_name": str | None,         # the rule that won the original route.decided, if any
    "pattern_confidence": float,     # the confidence of the pattern recommendation
}
```

The session manager re-dispatches the turn to `new_chosen_model` after this event is emitted. Subsequent `llm.call_started` etc. carry the new model.

#### `pattern.override_dismissed`

> **Sensitivity:** `pseudonymous`
> **Phase:** 3
> **Actor:** USER
> **Parent:** `route.decided`

Emitted when the user runs `/route ignore` (or otherwise dismisses a pattern-disagreement suggestion).

```python
{
    "chosen_model": str,             # what route.decided picked (and is keeping)
    "dismissed_pattern_model": str,
    "rule_name": str | None,
    "pattern_confidence": float,
}
```

The session continues with the original routing decision; this event is purely informational (and feeds back into pattern learning to track which suggestions get dismissed).

#### `pattern.recorded`

> **Sensitivity:** `pseudonymous`
> **Phase:** 2.5
> **Actor:** SYSTEM
> **Parent:** `session.ended`

Emitted by the session-ended batch subscriber after computing the session's contributing fingerprints + outcomes and calling `PatternStore.record()` for each. One event per (fingerprint, primary_model) write, not one per session. See `pattern-store.md §10.1`.

```python
{
    "fingerprint_id": str,                        # ULID
    "fingerprint_kind": Literal["structural", "hybrid"],
    "primary_model": str,
    "sample_size_before": int,
    "sample_size_after": int,
    "was_new_fingerprint": bool,
    "success_score": float | None,                # this session's score (None if evaluator didn't run)
    "cost_usd_at_record": str,                    # Decimal serialized as string; this session's contribution
    "pricing_version": str,
    "over_soft_cap": bool,
}
```

Field-name note: `cost_usd_at_record` (not `cost_usd`) — disambiguates from `llm.call_completed.cost_usd` and follows the `Decimal` serialization convention from `canonical-message-format.md §6.4`. The pattern-store draft (§10.1) currently names this field `cost_usd`; reconcile in the Wave 4 sweep.

#### `pattern.matched`

> **Sensitivity:** `pseudonymous`
> **Phase:** 2.5
> **Actor:** SYSTEM
> **Parent:** `route.decided`

Emitted when the routing engine's slot 4 wins (the pattern policy chose the model used for the turn). Distinct from `route.decided`, so consumers can query "how often does pattern routing fire?" without a JSON scan over `route.decided.chain`. Not emitted when the pattern policy deferred — the deferred recommendation is already captured in `route.decided.chain[].verdict = "deferred"`. See `pattern-store.md §10.2`.

```python
{
    "fingerprint_id": str,
    "fingerprint_kind": Literal["structural", "hybrid"],
    "chosen_model": str,                          # mirrors route.decided.chosen_model
    "confidence": float,
    "sample_size": int,                           # neighbors backing chosen_model
    "k_cluster_size": int,                        # total neighbors found (≤ K)
    "alternatives_count": int,                    # how many distinct models scored
}
```

#### `pattern.evicted`

> **Sensitivity:** `pseudonymous`
> **Phase:** 2.5
> **Actor:** SYSTEM
> **Parent:** `pattern.recorded` (cap-triggered) or none (manual / scheduled trim)

Mirrors `memory.eviction`. Fired when (1) a write lands the store over `soft_cap_rows` (signal only; `entries_evicted` may be 0), (2) a write lands the store over `hard_cap_rows` and auto-evict removed rows, (3) the continuous age-trim removed stale rows, or (4) the operator ran `/patterns clear`. Counts and ages only; no content. See `pattern-store.md §10.3`.

```python
{
    "trigger": Literal["soft_cap_signal", "hard_cap_evict", "age_trim", "manual_clear"],
    "fingerprints_before": int,
    "fingerprints_after": int,
    "outcomes_before": int,
    "outcomes_after": int,
    "entries_evicted": int,                       # outcomes removed; 0 for soft_cap_signal
    "oldest_evicted_age_days": float | None,      # for age_trim and hard_cap_evict
}
```

### 6.6 Skill domain

#### `skill.loaded`

> **Sensitivity:** `pseudonymous`
> **Phase:** 2
> **Actor:** SYSTEM
> **Parent:** `turn.started` or `llm.call_completed` (on-demand load)

```python
{
    "skill_id": str,
    "skill_version": str,
    "load_reason": Literal["always", "on_demand", "auto_suggested"],
    "load_size_tokens": int,
    "source": Literal["global", "workspace"],  # which directory served the skill (additive 2026-05-12)
    "triggered_by_tool_use_id": str | None,    # for on_demand loads via load_skill tool
}
```

#### `skill.created`

> **Sensitivity:** `user_controlled`
> **Phase:** 2.5
> **Actor:** SYSTEM | USER
> **Parent:** `session.ended` (auto-generation) or none (manual)

```python
{
    "skill_id": str,
    "source": Literal["manual", "auto_generated", "imported"],
    "source_session_id": str | None,
    "size_tokens": int,
    "security_scan_result": Literal["clean", "warning", "blocked"] | None,
    "security_scan_findings": list[str],
}
```

#### `skill.modified`

> **Sensitivity:** `user_controlled`
> **Phase:** 2
> **Actor:** SYSTEM | USER
> **Parent:** varies

```python
{
    "skill_id": str,
    "modification_type": Literal["edit", "version_bump", "rename"],
    "before_hash": str,
    "after_hash": str,
    "diff_size_bytes": int,
    "reason": str,
}
```

#### `skill.search`

> **Sensitivity:** `private`
> **Phase:** 2
> **Actor:** AGENT
> **Parent:** `llm.call_completed`

```python
{
    "query": str,                    # the agent's search query
    "results_count": int,
    "result_skill_ids": list[str],
}
```

### 6.7 Memory domain

#### `memory.updated`

> **Sensitivity:** `private`
> **Phase:** 2
> **Actor:** AGENT
> **Parent:** `llm.call_completed`

```python
{
    "file": Literal["MEMORY.md", "USER.md"],
    "operation": Literal["add", "replace", "consolidate"],
    "before_hash": str,
    "after_hash": str,
    "before_size_bytes": int,
    "after_size_bytes": int,
}
```

#### `memory.eviction`

> **Sensitivity:** `private`
> **Phase:** 2
> **Actor:** SYSTEM
> **Parent:** `memory.updated`

```python
{
    "file": Literal["MEMORY.md", "USER.md"],
    "trigger": Literal["size_cap_exceeded", "manual"],
    "entries_evicted": int,
    "size_before_bytes": int,
    "size_after_bytes": int,
}
```

### 6.8 Delegate domain

> *Phase note: the `delegate()` tool itself ships in Phase 4 (per the project overview). However, the routing chain's `delegate_request` policy slot exists from Phase 1 — it just always returns `not_applicable` until the tool exists. This is why `route.decided.chain[].policy` includes `"delegate_request"` from day one even though `delegate.*` events don't fire until Phase 4. The asymmetry is deliberate: the routing pipeline's shape is fixed, so adding the delegation slot later is filling in a stub rather than refactoring the chain.*

#### `delegate.started`

> **Sensitivity:** `pseudonymous`
> **Phase:** 4
> **Actor:** AGENT
> **Parent:** `llm.call_completed`

```python
{
    "tool_use_id": str,              # the delegate() call's tool_use_id
    "worker_session_id": str,
    "tier": Literal["fast", "balanced", "deep"],
    "resolved_model": str,
    "context_mode": Literal["minimal", "explicit"],
    "context_reference_count": int,  # for explicit mode
    "task_size_tokens": int,
}
```

#### `delegate.completed`

> **Sensitivity:** `pseudonymous`
> **Phase:** 4
> **Actor:** AGENT
> **Parent:** `delegate.started`

```python
{
    "tool_use_id": str,
    "worker_session_id": str,
    "success": bool,
    "worker_turn_count": int,
    "worker_total_input_tokens": int,
    "worker_total_output_tokens": int,
    "worker_total_cost_usd": float,
    "wall_time_seconds": float,
}
```

#### `delegate.failed`

> **Sensitivity:** `pseudonymous`
> **Phase:** 4
> **Actor:** AGENT | SYSTEM
> **Parent:** `delegate.started`

```python
{
    "tool_use_id": str,
    "worker_session_id": str | None,  # null if worker never started
    "failure_mode": Literal["worker_error", "max_tokens_exceeded", "insufficient_context",
                            "output_schema_validation_failed", "no_model_available_for_tier",
                            "cancelled_by_user"],
    "error_message": str,
    # When failure_mode == "insufficient_context", this carries the structured request
    # (see routing-engine.md §6.6.1 for the InsufficientContextRequest schema).
    "insufficient_context_request": dict | None,
}
```

### 6.9 Feedback domain

#### `feedback.explicit`

> **Sensitivity:** `aggregatable`
> **Phase:** 2
> **Actor:** USER
> **Parent:** `turn.completed` or `session.ended`

```python
{
    "scope": Literal["turn", "session"],
    "rating": Literal["thumbs_up", "thumbs_down"],
    "comment": str | None,
    "subject_turn_id": str | None,
    "subject_session_id": str | None,
}
```

#### `feedback.implicit`

> **Sensitivity:** `pseudonymous`
> **Phase:** 2
> **Actor:** SYSTEM
> **Parent:** varies

```python
{
    "type": Literal["retry", "manual_swap", "edit_followup", "abandon", "accept"],
    "confidence": float,             # 0..1, system's confidence this signal is meaningful
    "subject_turn_id": str | None,
    "context": dict,                 # type-specific extras
}
```

`retry` is detected when a user message has high similarity to a recent prior user message in the same session. `manual_swap` is when the user runs `/model` after an unsatisfactory turn. `edit_followup` is when a user message starts with patterns like "no, actually..." or "that's wrong, ...". These are heuristic; `confidence` reflects that.

### 6.10 Bus meta-events

This section is shorter than v1. `bus.handler_error` and `bus.overflow` were originally event types here. They've been moved to structured logs (see §3.5 and §5.2 rationale) — they describe bus health, and routing them through the bus they describe creates chicken-and-egg failures and recursive amplification.

What remains as actual events: subscriber lifecycle (helpful for debugging "did my subscriber actually attach?") and gap detection (helpful for trace store consistency checks).

#### `bus.subscriber_registered`

> **Sensitivity:** `pseudonymous`
> **Phase:** 1
> **Actor:** SYSTEM
> **Parent:** none

```python
{
    "subscription_name": str,
    "filter": dict,
    "fast_path": bool,
}
```

#### `bus.subscriber_unregistered`

> **Sensitivity:** `pseudonymous`
> **Phase:** 1
> **Actor:** SYSTEM
> **Parent:** none

```python
{
    "subscription_name": str,
    "reason": Literal["explicit", "client_disconnect", "shutdown", "removed_after_errors"],
}
```

#### `bus.gap_detected`

> **Sensitivity:** `pseudonymous`
> **Phase:** 1
> **Actor:** SYSTEM
> **Parent:** none

Emitted on server startup when the trace store detects a gap in the per-session monotonic event-id sequence. This indicates events were emitted but not persisted (typically due to the trace store crashing while the dispatch worker was buffered).

The gap itself is not recoverable — those events are lost. The event documents the gap so consumers (replay, analytics) can flag affected sessions.

```python
{
    "session_id": str,
    "gap_start_id": str,             # last persisted event id before the gap
    "gap_end_id": str,               # first persisted event id after the gap
    "estimated_missing_count": int,  # ULID arithmetic estimate; not exact
    "detected_at": datetime,
}
```

> *Bus diagnostics that go to logs only (not events): `EventBusOverflowError` rejections (§3.5), handler exceptions (§5.2). Reasons are detailed in those sections.*

### 6.11 Provider domain

#### `provider.degraded`

> **Sensitivity:** `pseudonymous`
> **Phase:** 2
> **Actor:** SYSTEM
> **Parent:** `llm.call_failed`

```python
{
    "provider": str,
    "recent_failure_count": int,
    "window_seconds": int,
}
```

Distinguished from `routing.provider_unavailable`: degraded is a soft state (Phase 2 refinement); unavailable is the hard state that causes routing to reject.

### 6.12 Eval domain

The evaluator (`evaluator.md`) emits one verdict per scored subject. Subjects are turns, tool cycles, sessions, and benchmark workloads. Verdicts are append-only — re-evaluating an older subject produces a new `eval.completed` event with a fresh `eval_id`; the prior verdict is preserved. The `eval` domain is closed (see §4.5).

#### `eval.started`

> **Sensitivity:** `pseudonymous`
> **Phase:** 3
> **Actor:** SYSTEM
> **Parent:** `turn.completed` / `tool.completed` / `tool.failed` / `session.ended` / `feedback.explicit`

Emitted when the evaluator begins scoring a subject. Pairs 1:1 with a later `eval.completed` or `eval.failed` carrying the same `eval_id`. See `evaluator.md §8.1`.

```python
{
    "eval_id": str,                               # monotonic ULID
    "subject_kind": Literal["turn", "tool_cycle", "session", "workload"],
    "subject_id": str,
    "rubric_id": str,
    "rubric_version": str,
    "judge_kind_planned": Literal["heuristic", "llm", "hybrid"],
    "trigger": Literal["bus", "batch", "feedback_arrived", "benchmark"],
}
```

#### `eval.completed`

> **Sensitivity:** `user_controlled` (floor; downgrades to `pseudonymous` per §4.4.1 when `signals.rationale_redacted` is absent)
> **Phase:** 3
> **Actor:** SYSTEM
> **Parent:** `eval.started`

```python
{
    "eval_id": str,
    "subject_kind": Literal["turn", "tool_cycle", "session", "workload"],
    "subject_id": str,
    "score": float,                               # in [0.0, 1.0]; 1.0 = clear success
    "confidence": float,                          # in [0.0, 1.0]; judge's confidence in `score`
    "judge_kind": Literal["heuristic", "llm", "hybrid"],
    "judge_model": str | None,                    # canonical id when llm/hybrid used the LLM tier
    "judge_cost_usd": str,                        # Decimal serialized as string (same as Usage.cost_usd per canonical-format §6.4)
    "judge_pricing_version": str | None,          # set when judge_cost_usd > 0
    "judge_latency_ms": int,
    "rubric_id": str,
    "rubric_version": str,
    "signals": dict,                              # judge-specific evidence; see evaluator.md §4.4
    "parent_eval_id": str | None,                 # for tool_cycle→turn / turn→session rollups
}
```

`judge_cost_usd` is `Decimal("0")` for heuristic verdicts and `judge_pricing_version` is `None` in that case — pricing semantics don't apply to code that did no inference.

**Sensitivity floor.** The catalog floor is `user_controlled` — the worst case, when `signals.rationale_redacted` is populated and the event carries LLM-generated text the user opted into capturing. When the rationale field is absent (heuristic verdicts, opt-in disabled), the emitter passes `Sensitivity.PSEUDONYMOUS` to `make_event` — a move toward less private, which §4.4.1 allows.

#### `eval.failed`

> **Sensitivity:** `pseudonymous`
> **Phase:** 3
> **Actor:** SYSTEM
> **Parent:** `eval.started`

Emitted instead of `eval.completed` when the judge couldn't produce a verdict. See `evaluator.md §8.3`.

```python
{
    "eval_id": str,
    "subject_kind": Literal["turn", "tool_cycle", "session", "workload"],
    "subject_id": str,
    "failure_mode": Literal[
        "judge_output_invalid",                   # LLM response didn't parse against the rubric schema
        "judge_call_failed",                      # LLM call hit a hard error (provider down, auth, etc.)
        "throttled_no_heuristic",                 # caps fired AND heuristic also unavailable (defensive; v1 unreachable)
        "subject_not_found",                      # subject_id resolved to no events
        "rubric_invalid",                         # rubric file failed to load
    ],
    "error_message": str,
    "judge_latency_ms": int,
}
```

---

## 7. Persistence

### 7.1 SQLite schema

```sql
CREATE TABLE events (
  id TEXT PRIMARY KEY,
  timestamp_us INTEGER NOT NULL,        -- unix microseconds
  session_id TEXT NOT NULL,
  turn_id TEXT,                          -- nullable
  parent_event_id TEXT,                  -- nullable
  type TEXT NOT NULL,
  actor TEXT NOT NULL,
  sensitivity TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  FOREIGN KEY (session_id) REFERENCES sessions(id)
);

CREATE INDEX idx_events_session_id     ON events(session_id, id);
CREATE INDEX idx_events_type_timestamp ON events(type, timestamp_us);
CREATE INDEX idx_events_turn           ON events(turn_id);
CREATE INDEX idx_events_parent         ON events(parent_event_id);
```

The `sessions` table is defined in `canonical-message-format.md` §9.1 (sessions, messages, tool_calls). The events table shares the same SQLite database file in v1.

### 7.2 Storage notes

- **Payload as JSON in a single column.** Same rationale as the canonical message format: heterogeneous payloads, queries are predominantly "give me a session's events," updates don't happen.
- **`timestamp_us` is microseconds for wall-clock accuracy.** ULIDs already enforce per-process monotonic ordering within a millisecond (the random component increments on tie); microseconds in a separate column improve human-readable timing precision in dashboards and analytics, not ordering. Ordering is established by `id`.
- **SQLite mode commitment.** The events database is opened with `journal_mode=WAL` and `synchronous=NORMAL`. This is necessary for the trace store writer to meet its <1ms fast-path budget — `synchronous=FULL` (the SQLite default) makes single-row inserts 5–20ms due to fsync.

  *Durability trade-off:* in WAL + NORMAL, events from the last fsync window may be lost on hard process crash or OS crash (typically <1s of events). On graceful shutdown, all events flush. This trade-off is acceptable because: (1) events are not the system of record for any user-visible state — sessions and messages are stored separately and atomically; (2) clients can reconnect with replay if they were mid-stream; (3) the dispatch worker batches WAL commits opportunistically, reducing the practical loss window.

  Higher durability is available later: switching to `synchronous=FULL` makes inserts safe at the cost of fast-path budget. Phase 3 may optionally add batched-write durability (group commits every 100ms) which combines durability and throughput at the cost of replay-window latency.
- **No FTS5 on payloads in v1.** Query patterns are by session, type, and time. Add FTS5 later if needed for free-text search over payload contents (probably never — that's what the canonical message store is for).
- **Indexes are pragmatic.** `(session_id, id)` covers session replay. `(type, timestamp_us)` covers cross-session analytics ("how many `tool.failed` events this week?"). `(turn_id)` covers turn detail. `(parent_event_id)` covers causal walk.

### 7.3 Retention

V1: unbounded retention. At single-user scale, even 100k events/month is small.

Phase 3+: optional retention policy in config:
```yaml
trace_retention:
  default_days: 365
  by_type:
    "llm.call_started": 90       # short retention for high-volume types
    "llm.call_completed": 90
```

Retention runs as a batch task, deletes oldest events past the threshold. Pattern store outcomes are computed before retention purges, so analytics are not lost.

### 7.4 Virtual columns

In v1, no virtual columns extracted from payload JSON. If specific queries get slow, add them via `ALTER TABLE events ADD COLUMN ... AS (json_extract(...)) VIRTUAL`. This is a non-breaking change.

Likely candidates for Phase 2: `payload_model` (extract from `llm.call_completed` for cost queries), `payload_skill_id` (for skill analytics).

---

## 8. Worked examples

### 8.1 A simple turn's event chain

User asks "What time is it?" The agent calls a `current_time` tool and answers.

```
session.created                                       (parent: none)
  ↓ time passes ↓
turn.started                                          (parent: none)
  ↓
route.decided                                         (parent: turn.started)
  ↓
llm.call_started     model=sonnet                     (parent: turn.started)
  ↓
llm.call_completed   produced_tool_calls=1            (parent: llm.call_started)
  ↓
tool.called          tool_name=current_time           (parent: llm.call_completed)
  ↓
tool.completed       success=true                     (parent: tool.called)
  ↓
llm.call_started     model=sonnet                     (parent: tool.completed)
  ↓
llm.call_completed   stop_reason=end_turn             (parent: llm.call_started)
  ↓
turn.completed                                        (parent: turn.started)
```

Walking back from `turn.completed` via `parent_event_id` reconstructs the full causal chain.

### 8.2 A turn with a failed tool

User asks the agent to read a file that doesn't exist.

```
turn.started
  ↓
route.decided
  ↓
llm.call_started
  ↓
llm.call_completed   produced_tool_calls=1
  ↓
tool.called          tool_name=read_file, input_hash=...
  ↓
tool.failed          error_class=not_found
  ↓
llm.call_started     (the agent loop tries again with the failure as context)
  ↓
llm.call_completed   stop_reason=end_turn
  ↓
turn.completed
```

### 8.3 A delegated sub-task

```
[planner session sess_42]
turn.started        session_id=sess_42
  ↓
route.decided
  ↓
llm.call_started     model=opus, is_worker=false
  ↓
llm.call_completed   produced_tool_calls=1 (delegate)
  ↓
tool.called          tool_name=delegate
  ↓
delegate.started     worker_session_id=sess_43, tier=fast, resolved_model=haiku
  ↓
[worker session sess_43 — separate session_id, related via parent_session_id in session record]
session.created     session_id=sess_43
  ↓
turn.started        session_id=sess_43
  ↓
route.decided        DELEGATE_REQUEST chose haiku
  ↓
llm.call_started    model=haiku, is_worker=true
  ↓
llm.call_completed
  ↓
turn.completed
  ↓
session.ended       session_id=sess_43
[back in planner session]
  ↓
delegate.completed  worker_session_id=sess_43, success=true
  ↓
tool.completed      tool_name=delegate
  ↓
llm.call_started    (planner integrates worker output)
  ↓
llm.call_completed  stop_reason=end_turn
  ↓
turn.completed      session_id=sess_42
```

The worker session's events have `is_worker: true` on `llm.call_started`. They are queryable independently and roll up into the parent session's cost via the `delegate.completed` event.

### 8.4 A pattern override

User has a rule "fast for commits → haiku" but the pattern store suggests sonnet at high confidence. The TUI surfaces the disagreement; the user chooses to override.

```
turn.started
  ↓
route.decided   (winner: rule chose haiku; pattern deferred sonnet at 0.87
                 — recorded in chain[].verdict = "deferred" for pattern policy)
  ↓
[TUI surfaces the disagreement; user runs /route override]
  ↓
route.overridden   (parent: route.decided)
                   original_chosen_model=haiku
                   new_chosen_model=sonnet
                   deferred_policy=pattern
                   rule_name=fast_for_commits
                   pattern_confidence=0.87
  ↓
llm.call_started   model=sonnet  (note: parent is route.overridden, not route.decided)
  ↓
...
```

This shape preserves the routing-engine.md invariant of exactly one `route.decided` per turn (per `routing-engine.md` §7.2 and test §10.1.17). The original decision is intact; the override is a distinct event that records what changed.

If the user runs `/route ignore` instead:

```
route.decided   (rule chose haiku, pattern deferred sonnet at 0.87)
  ↓
pattern.override_dismissed   (purely informational; turn proceeds with haiku)
  ↓
llm.call_started   model=haiku  (parent: route.decided)
```

---

## 9. Testing strategy

### 9.1 Required tests

1. **Schema validation.** For every event type in the catalog, construct a valid payload and an invalid one. Verify strict mode raises on invalid; lenient mode logs and drops.
2. **Causal chain integrity.** Run a fixture turn; walk back from `turn.completed` via `parent_event_id`; verify the chain reaches `turn.started` with no missing links.
3. **Gap detection.** Crash the trace store mid-write (mocked); restart the server; verify a `bus.gap_detected` event is emitted on startup with `gap_start_id` and `gap_end_id` corresponding to the missing range.
4. **Slow handler registration is rejected.** Attempt to register a subscription with `fast_path=true` whose handler is annotated `@slow` (testing helper). The test passes when registration raises `FastPathHandlerError`; this enforces the convention that slow handlers cannot register on the fast path.
5. **Subscriber error isolation.** Register two handlers; have one always raise. Verify the other still receives every event, the failing one's exception is logged at WARN level (not emitted as a bus event), and the failing subscription remains registered.
6. **Filter semantics.** Subscribe with each filter dimension (session, type, actor); emit a mix of events; verify only matching events are delivered.
7. **Replay equivalence.** Run a session, persist events, query trace store for replay, compare event sequence to the live one. Must be identical.
8. **Sensitivity tagging.** Every emitted event has a non-null `sensitivity` consistent with the catalog. Specifically: (a) events at their default-fields-only state match the catalog's declared default sensitivity; (b) opt-in events with their optional fields populated have sensitivity upgraded per §4.4.1.
9. **Forward compatibility.** A subscriber receives an event of an unknown type (simulated by emitting a type added in a "future" catalog); verify the subscriber skips with debug log and does not raise.
10. **Bounded queue rejection.** Fill the dispatch queue; verify `EventBusOverflowError` is raised at the emitter and a structured log entry at level `ERROR` is written. Verify no `bus.overflow` event is emitted (it is no longer a catalog type).
11. **Trace store fast-path budget.** With the trace store configured for `journal_mode=WAL` and `synchronous=NORMAL`, measure single-row insert latency over 1,000 inserts on the test storage; verify p95 < 1ms. (Skipped if running on storage that can't sustain this; documented in test output.)
12. **`route.decided` exactly-once.** Run a turn that triggers a pattern override; verify exactly one `route.decided` is emitted, followed by exactly one `route.overridden`. The original decision's payload is unchanged after the override.
13. **`route.overridden` causality.** Verify the `route.overridden` event has `parent_event_id` pointing at the original `route.decided`, and subsequent `llm.call_started` has `parent_event_id` pointing at the `route.overridden`.
14. **Recursive failure non-occurrence.** Register a subscriber that filters on all events and always raises. Run a session. Verify no recursion: the WARN logs are bounded, no event flood occurs.

### 9.2 Property tests

- **Monotonicity per session:** Every emitted event for a given session has a higher ULID than all prior events for that session.
- **Parent existence:** Every event with non-null `parent_event_id` has a parent that exists in the trace store (eventually — replay-window after a small delay accounts for fast-path race).

---

## 10. Open questions

1. **Compaction.** Trace stores grow unboundedly without retention. Phase 3 retention policy is sketched (§7.3); the exact compaction strategy (delete vs. archive to cold storage) is undecided.
2. **Cross-session causality.** A worker session's events have `parent_session_id` on the session record but no event-level pointer back to the planner's `delegate.started`. Should `delegate.started` and the worker's `session.created` reference each other? V1: only via session metadata. May refine in Phase 4.
3. **Sub-millisecond handler enforcement.** The spec says fast-path handlers must be <1ms but doesn't enforce. A timing-based annotation (`@fast_handler` decorator that asserts wall-time per call) is plausible; deferred.
4. **PII redaction quality.** `error_message_redacted` and `user_message_text_redacted` rely on heuristic scrubbing. The exact scrub algorithm (regex-based? LLM-based?) is undecided. V1: simple regex for emails, paths, common API key formats. Reviewable and improvable.
5. **Event type evolution.** What happens when a payload schema is extended? Today: additive only (new fields are optional). Removing or renaming fields is a major version bump on the type, with migration. The exact mechanism is sketched but not exercised.
6. **Batch subscribers and trace store gaps.** Batch subscribers query the trace store directly; if their query happens during a gap (events emitted but not yet persisted), they get inconsistent reads. A "watermark" mechanism (latest persisted event id) would let batch readers wait. Deferred.

---

## 11. Decision log

| Date       | Decision                                                              | Rationale                                                                                  |
|------------|-----------------------------------------------------------------------|--------------------------------------------------------------------------------------------|
| 2026-05-08 | In-process bus; no Kafka, no IPC, no cross-machine                    | Single-user app; in-process latency is microseconds and sufficient.                        |
| 2026-05-08 | No in-process redelivery; subscribers dedupe by event id across restarts only | Exactly-once is distributed-systems work, wildly overkill for the use case.            |
| 2026-05-08 | Closed type catalog enumerated in this doc                            | New types are deliberate spec changes; prevents type sprawl.                               |
| 2026-05-08 | Sensitivity tagging on every event; dynamic on opt-in payloads        | Future sync and cross-user features need this from day one; opt-in upgrades the tag honestly. |
| 2026-05-08 | Fast-path vs. batch subscribers                                       | Slow handlers as fast-path stall everything; convention enforces the split.                |
| 2026-05-08 | Trace store as a subscriber, not the bus itself                       | Other subscribers don't pay disk-write latency; clean separation of concerns.              |
| 2026-05-08 | SQLite WAL + `synchronous=NORMAL` for the trace store                 | Required to meet fast-path budget; <1s durability window acceptable for trace data.        |
| 2026-05-08 | Memory snapshotter on the batch path                                  | Reading and diffing memory files isn't <1ms; doesn't belong on fast path.                  |
| 2026-05-08 | Causal chains via single `parent_event_id` pointer, not graphs        | Trees are sufficient; graphs are over-engineered for the question "why did this happen?"   |
| 2026-05-08 | Strict vs. lenient validation modes                                   | Strict catches bugs in dev; lenient prevents production crashes from a malformed payload.  |
| 2026-05-08 | No FTS5 on payloads in v1                                             | Query patterns don't need it; canonical message store handles content search.              |
| 2026-05-08 | Pattern override emits `route.overridden`, not a second `route.decided` | Preserves the routing-engine.md invariant of one `route.decided` per turn; override is its own observable action. |
| 2026-05-08 | Bus diagnostics (overflow, handler errors) go to logs, not events     | Avoids chicken-and-egg failures and recursive amplification.                               |
| 2026-05-08 | `routing_policy_version` is content hash, not mtime                   | mtimes can collide across restore-from-backup; hash is unambiguous.                        |
| 2026-05-08 | Streaming events explicitly excluded from catalog                     | A 200-token message produces 200+ rows otherwise; reconstructible from persisted Message. |
| 2026-05-08 | Streaming server has two input channels (bus bridge + direct)         | Different lifetimes (persisted vs. live); merged on the wire only.                         |
| 2026-05-08 | Error class enums in `llm.call_failed` and `tool.failed` extended     | Reconciled with provider-adapter (8 values) and tool-dispatcher (8 values).                |
| 2026-05-08 | `tool.confirmation_*` events added to catalog                         | Confirmation flow is observable history; needs persistence for analytics.                  |
| 2026-05-08 | `block_dropped` is log-only, not catalog event                        | Consistent with `bus.overflow` precedent; not a domain action worth persisting.            |

---

## 12. References

- `canonical-message-format.md` — `Message`, `ToolDefinition`, content blocks referenced by tool events; `sessions` table referenced by §7.1's foreign key.
- `routing-engine.md` — `route.decided` event consumer details; `InsufficientContextRequest` schema referenced by `delegate.failed`.
- `streaming-protocol.md` — how WebSocket clients subscribe to a filtered event stream and replay on reconnect.
- `skill-format.md` (planned) — skill events emitted on load, create, modify.
