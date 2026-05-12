# Streaming Protocol Specification

**Status:** Draft v2
**Last updated:** 2026-05-08
**Owner:** _your name_

> **v2 changes:** Streaming events declared as a separate transient layer,
> not bus catalog events (§5). They reach clients live but are not persisted —
> they're reconstructible from the persisted Message + usage. Cancellation
> event sequences split into two cases (§6.2). Cross-reference to EventFrame
> in event-bus spec (§4.2).

> *Throughout: paths shown use `~/.yourtool/` as a placeholder for the final config directory.*

---

## 1. Purpose

This document specifies the protocol clients use to receive live updates from a session: token-level LLM output, tool invocations, routing decisions, and all other domain events. The protocol runs over a single WebSocket per client per session, with a lightweight HTTP attach handshake. It defines snapshot-then-stream semantics, reconnection with replay, cancellation, and backpressure.

This spec depends on:
- `event-bus-and-trace-catalog.md` for the `Event` envelope and the closed catalog of types.
- `routing-engine.md` for turn lifecycle (the lock) and cancellation semantics on turns.
- `canonical-message-format.md` for `Message`, content blocks, and `MessageStatus`.

---

## 2. Goals and non-goals

### 2.1 Goals

1. **Live updates with minimal latency.** Token-level deltas reach a connected client within a few milliseconds of the adapter producing them.
2. **Reconnect-tolerant.** Network blips, client crashes, and lid-closes are normal. Clients reconnect with a cursor and replay missed events without data loss.
3. **Multi-client.** Multiple clients can attach to the same session simultaneously (TUI on laptop + dashboard in browser); each gets the same stream.
4. **Backpressure-safe.** A slow client cannot stall the agent loop or other clients.
5. **Provider-agnostic.** Token-level events are canonical; clients render the same way regardless of which model is producing the stream.
6. **Cancellation is observable.** When a user cancels, every connected client sees the same canonical cancellation events.

### 2.2 Non-goals

1. **Authentication and authorization.** Localhost only in v1. Cross-machine streaming is deferred.
2. **Cross-session multiplexing in one WebSocket.** One session per connection. Clients open multiple connections if they want multiple sessions.
3. **Bidirectional command channel for long control flow.** Slash commands and turn submission go through HTTP; the WebSocket is a one-way (server-to-client) event stream plus a small set of client→server control messages (cancel, ack).
4. **Compression.** At localhost speeds, JSON is fine. Add later if needed.
5. **Best-effort streaming tool input parsing.** v1 streams raw partial JSON strings; structured-input parsing during streaming is deferred.

---

## 3. Connection lifecycle

### 3.1 Attach handshake

Clients first call HTTP `GET /sessions/{session_id}` to verify the session exists and to receive an attach token (a short-lived nonce; primarily there to namespace this connection's filter and reconnect cursor). Response:

```json
{
  "session_id": "sess_42",
  "active_model": "anthropic:claude-sonnet-4-6",
  "attach_token": "atk_01HZ...",
  "ws_url": "ws://localhost:8421/sessions/sess_42/stream?attach=atk_01HZ..."
}
```

The client opens a WebSocket to `ws_url`. The token is single-use; reconnects request a fresh token via the HTTP endpoint.

### 3.2 Subscribe message

Immediately after connection, the client sends a `subscribe` control message:

```json
{
  "type": "subscribe",
  "filter": {
    "event_types": ["text.delta", "tool.use_start", "tool.use_input_delta",
                    "tool.use_end", "tool.completed", "route.decided",
                    "turn.started", "turn.completed", "turn.cancelled"],
    "actors": null,
    "include_worker_sessions": false
  },
  "since": null,
  "snapshot": true
}
```

Fields:

- `event_types`: list of event-type strings to receive. `null` means all types. Valid type strings are the union of: catalog event types from `event-bus-and-trace-catalog.md` §4.5, plus streaming-only event types from §5.3 below (`message.start`, `message.complete`, `text.delta`, `thinking.delta`, `tool.use_start`, `tool.use_input_delta`, `tool.use_end`). The example above mixes both — `text.delta` and `tool.use_*` are streaming, the rest are catalog.
- `actors`: list of `Actor` values. `null` means all.
- `include_worker_sessions`: whether to receive events from worker sessions spawned by this session's planner (§7).
- `since`: an event id cursor. Server replays events with `id > since` from the trace store before going live. `null` means no replay.
- `snapshot`: if `true`, the server sends a session snapshot before any events. If `false`, the client receives only events.

The server validates and replies with `subscribe_ack` or `subscribe_error`.

### 3.3 Preset filters

For convenience, two preset filters are defined. Clients can pass these instead of explicit filter objects:

```json
{ "type": "subscribe", "filter": "preset:chat", "since": null, "snapshot": true }
{ "type": "subscribe", "filter": "preset:full", "since": null, "snapshot": true }
```

| Preset       | Includes                                                                          | Use case                  |
|--------------|-----------------------------------------------------------------------------------|---------------------------|
| `preset:chat`| Token deltas, tool events, route.decided, turn lifecycle, feedback, delegate events. | TUI, end-user views.    |
| `preset:full`| Every event type in the catalog.                                                  | Dashboard session detail, debugging. |

Presets are syntactic sugar for explicit filters — equivalent and substitutable. The server normalizes the preset to its underlying event-type list and includes that in the `subscribe_ack`.

### 3.4 Snapshot

When `snapshot: true`, the server first sends:

```json
{
  "type": "snapshot",
  "session": {
    "id": "sess_42",
    "workspace_path": "/Users/me/code/myproject",
    "active_model": "anthropic:claude-sonnet-4-6",
    "routing_policy_version": "2026-05-08T14:23:11Z",
    "cost_so_far_usd": 0.142,
    "turn_count": 7,
    "current_turn_id": "01HZ_xyz" or null,    # null if no turn in flight
    "current_turn_status": "in_flight" | null
  },
  "messages": [
    # The canonical message list, capped at the most recent N (default 50).
    # See §3.5 for older-history retrieval.
  ],
  "snapshot_at_event_id": "evt_01HZ..."
}
```

After the snapshot, live streaming begins. Any events with `id > snapshot_at_event_id` are sent in order; events with `id <= snapshot_at_event_id` are already reflected in the snapshot and are not re-sent.

### 3.5 Older-history retrieval

The snapshot caps `messages` at the most recent 50 by default. Clients needing older history call HTTP `GET /sessions/{id}/messages?before={message_id}&limit=50`. This is paginated; clients can walk back as far as they want.

The streaming protocol is intentionally not used for history backfill — REST is the right tool for paginated reads, and mixing the two on one channel makes flow control harder.

### 3.6 Disconnect and reconnect

The server emits no special "goodbye" frame on client disconnect. The connection just closes. Cleanup on the server: drop the per-client outbound queue, log a `bus.subscriber_unregistered` meta-event, do nothing else.

To reconnect, the client repeats §3.1 and §3.2 with `since: <last_received_event_id>`. The server:

1. Verifies the session still exists.
2. If `since` is older than the trace store retention window, returns `subscribe_error: cursor_expired`. Client must request `snapshot: true` and start fresh.
3. Otherwise, replays events with `id > since` matching the filter, then attaches to the live stream at the gap-free seam (§3.7).

Replay is bounded: the server caps replay at 10,000 events. If the gap is larger, the server replies `subscribe_error: replay_too_large` and the client must request a snapshot.

### 3.7 The snapshot/replay seam

The seam between snapshot/replay and live streaming is the trickiest part. Events arriving while the snapshot is being computed (or replay is being drained) must end up exactly once in the client's stream — no duplicates, no gaps.

Implementation contract:

1. Server starts buffering live events for this connection in a per-client queue *immediately* on accept.
2. Server computes snapshot or replay range based on the cursor.
3. Server sends snapshot/replay events. Each carries its own `id`.
4. Server drains the per-client buffer, *skipping* any events whose `id <= last_replayed_id` (already covered by replay).
5. Server transitions to live mode: new events are enqueued and drained in order.

Because the per-client queue is in place from accept, no live event is missed. Because the replay cutoff is by `id`, no event is duplicated. The cost: brief memory overhead during the seam transition (typically <100ms of buffered events).

### 3.8 Heartbeats

The server sends a `ping` frame every 30 seconds when the connection is otherwise idle. The client responds with `pong`. If three pings are unanswered, the server closes the connection (the client probably crashed without socket cleanup).

The client may also send `ping` proactively; the server responds with `pong`. Either side can drive heartbeats.

---

## 4. Wire protocol

### 4.1 Frame envelope

All WebSocket frames are JSON text frames with a top-level `type` discriminator. Frames are one of two categories:

- **Server → client**: snapshot, event, ack, error, ping/pong.
- **Client → server**: subscribe, cancel, ack, ping/pong.

### 4.2 Server-to-client frames

```ts
// Sent once after subscribe, only if snapshot: true requested.
type SnapshotFrame = {
  type: "snapshot";
  session: SessionSummary;
  messages: Message[];
  snapshot_at_event_id: string;
}

// The bulk of the stream — wraps any catalog event or streaming-only event.
// Catalog events have shapes from event-bus-and-trace-catalog.md §6;
// streaming-only events (message.*, text.*, thinking.*, tool.use_*) have
// shapes from streaming-protocol.md §5.3. Both use the same Event envelope.
type EventFrame = {
  type: "event";
  event: Event;                  // see event-bus-and-trace-catalog.md §4.1 for envelope
}

// Acknowledgement of a subscribe.
type SubscribeAckFrame = {
  type: "subscribe_ack";
  resolved_filter: EventFilter;  // server-normalized (presets expanded)
  since: string | null;          // echoes client's cursor
  snapshot: boolean;
  replay_event_count: number;    // 0 if no replay requested
}

// Subscription failed.
type SubscribeErrorFrame = {
  type: "subscribe_error";
  code: "session_not_found" | "cursor_expired" | "replay_too_large" | "invalid_filter";
  message: string;
}

// Heartbeat.
type PingFrame = { type: "ping"; nonce: string; }
type PongFrame = { type: "pong"; nonce: string; }
```

### 4.3 Client-to-server frames

```ts
// First frame after connection.
type SubscribeFrame = {
  type: "subscribe";
  filter: EventFilter | "preset:chat" | "preset:full";
  since: string | null;
  snapshot: boolean;
}

// Cancel an in-flight turn.
type CancelFrame = {
  type: "cancel";
  turn_id: string;
  reason?: string;               // optional, propagated to turn.cancelled.payload.reason
}

// Heartbeat (either direction).
type PingFrame = { type: "ping"; nonce: string; }
type PongFrame = { type: "pong"; nonce: string; }
```

There is no "submit turn" frame on the WebSocket. Turn submission goes through HTTP `POST /sessions/{id}/turns` with the user's message in the body. This separation simplifies flow control: WebSocket is a stream; HTTP is request/response.

### 4.4 Frame ordering guarantees

For a given client connection, server-to-client frames arrive in the order they are sent. WebSocket itself guarantees this; the protocol relies on it.

Across multiple client connections to the same session, frame arrival is independent per connection. Each client sees a consistent per-connection stream, but two clients may see the same event at slightly different wall-clock times.

---

## 5. Token-level streaming

### 5.1 Streaming events are a separate layer

Streaming events are *not* bus catalog events. They are a transient live-update protocol that flows from the agent loop to attached clients. They are not persisted, not stored in the trace store, and not reconstructible from queries to the trace store after the fact.

Why this split:

- A 200-token assistant message produces 200+ `text.delta` chunks. Persisting each would balloon the trace store with rows that drive nothing analytical (nobody queries "show me text deltas from yesterday").
- Streaming events are 100% reconstructible from the persisted `Message` content (canonical format spec) plus the `usage` totals on `llm.call_completed`. The reconstruction is deterministic; the live stream is a UX optimization.
- Bus catalog events describe domain actions worth persisting and querying (`turn.started`, `tool.completed`, `route.decided`). Streaming events describe live UI updates. Different access patterns, different lifetimes.

Architectural consequence: the **streaming server is not a bus subscriber.** It is a separate component that the agent loop notifies directly during streaming output. Bus events flow through their own dispatch. The two channels share the `EventFrame` envelope on the wire (so clients can multiplex one WebSocket) but originate differently inside the server.

```
                ┌─────────── persisted ──────────┐
agent loop ──── bus event ──→ trace store
                              event subscribers (cost accumulator, etc.)
                              streaming server (forwards to clients)

                ┌─── transient (live only) ──────┐
agent loop ──── streaming event ──→ streaming server ──→ clients
                                    (no trace store, no other subscribers)
```

The streaming server receives both channels and forwards them to clients over WebSocket. Clients see a unified `EventFrame` stream. The persistence asymmetry is invisible to clients but real on the server side.

### 5.2 Adapter translation

Adapters receive provider-specific stream chunks and emit canonical streaming events:

- **Anthropic** sends `content_block_start`, `content_block_delta` (with type discriminators for text vs. input JSON), `content_block_stop`, `message_delta`, `message_stop`.
- **OpenAI** sends `choices[].delta` with `content` (string), `tool_calls[].function.arguments` (string), and `finish_reason` on the final delta.

Both converge to the canonical streaming events listed in §5.3.

### 5.3 Canonical streaming events

These are streaming-only event types. Names follow `<domain>.<verb>` convention (matching the bus catalog naming) but are NOT in the bus catalog. The reserved streaming-only domains are: `message`, `text`, `thinking`, `tool.use_*` (a sub-namespace of `tool`).

> *Note on domain reservation: the bus catalog (`event-bus-and-trace-catalog.md` §4.5) does NOT include `message`, `text`, `thinking` in its closed domain list. The streaming layer reserves these names. Adapters and the agent loop emit streaming events under these names; the streaming server forwards them to clients under the same names. They never enter the trace store.*

Streaming events appear interleaved with bus events in the live WebSocket stream. The order, for a typical assistant turn, is:

```
turn.started                  ← bus event (persisted)
route.decided                 ← bus event
llm.call_started              ← bus event
   message.start              ← streaming event (NOT persisted)
   text.delta                 ← streaming event ×N
   thinking.delta             ← streaming event ×N (if applicable)
   tool.use_start             ← streaming event (one per tool_use)
   tool.use_input_delta       ← streaming event ×M
   tool.use_end               ← streaming event (with final_input)
   message.complete           ← streaming event
llm.call_completed            ← bus event (persisted; carries final usage)
   tool.called                ← bus event (one per tool, on dispatch)
   tool.completed             ← bus event
llm.call_started              ← bus event (next call in tool loop)
   ...
turn.completed                ← bus event
```

Clients render incrementally from streaming events and verify against the authoritative canonical state in `message.complete.final_content` and `llm.call_completed.usage` (both reach the client; `message.complete` is streaming-only, `llm.call_completed` is bus).

#### Per-event payloads

#### `message.start`

```python
{
    "message_id": "01HZ...",
    "role": "assistant",
    "model": "anthropic:claude-sonnet-4-6"
}
```

#### `text.delta`

```python
{
    "message_id": "01HZ...",
    "content_block_index": 0,        # which block in the message this delta belongs to
    "text": "...",                    # the new chunk only, not cumulative
}
```

#### `thinking.delta`

```python
{
    "message_id": "01HZ...",
    "content_block_index": 0,
    "text": "...",                    # thinking text chunk
    "signature": "..." | null         # populated only on the final delta of the block
}
```

#### `tool.use_start`

```python
{
    "message_id": "01HZ...",
    "content_block_index": 1,
    "tool_use_id": "tu_01HZ...",
    "tool_name": "read_file"
}
```

#### `tool.use_input_delta`

```python
{
    "message_id": "01HZ...",
    "content_block_index": 1,
    "tool_use_id": "tu_01HZ...",
    "partial_json": "..."             # raw JSON string fragment, may be invalid mid-stream
}
```

The client accumulates `partial_json` strings to reconstruct the input. The fragments are not guaranteed to be JSON-parseable until `tool.use_end` arrives.

#### `tool.use_end`

```python
{
    "message_id": "01HZ...",
    "content_block_index": 1,
    "tool_use_id": "tu_01HZ...",
    "final_input": {...}              # parsed JSON object — authoritative
}
```

The client should *replace* its accumulated partial input with `final_input` once received. Even if the client perfectly accumulated all `partial_json` fragments, the canonical authoritative value is `final_input` (in case a fragment was lost or the partial parse was wrong).

#### `message.complete`

```python
{
    "message_id": "01HZ...",
    "stop_reason": "end_turn" | "max_tokens" | "stop_sequence" | "tool_use",
    "final_content": [ContentBlock],  # the full canonical content list
    "usage": Usage                     # finalized token counts and cost
}
```

`final_content` is the authoritative content of this assistant message. Clients can use it to validate their reconstructed state (sum of deltas should equal `final_content`); on mismatch, the client trusts `final_content` and re-renders.

### 5.4 What clients render and when

Clients should render incrementally:

- On `message.start`: append a new empty assistant message slot.
- On `text.delta`: append text to the appropriate content block, render incrementally.
- On `thinking.delta`: render thinking content (TUI may show in a folded section; dashboard may show inline).
- On `tool.use_start`: render a "calling {tool_name}…" placeholder.
- On `tool.use_input_delta`: optionally show partial input as it streams (or just keep the placeholder).
- On `tool.use_end`: render the structured input.
- On `message.complete`: replace the content with `final_content` if reconstruction differs (silent fix; debug-log on mismatch).

### 5.5 Why deltas, not state snapshots

Two reasons:

1. **Wire efficiency.** A 200-token message is hundreds of small frames; sending the full state with each delta would multiply bandwidth.
2. **UI smoothness.** Incremental rendering at the delta level is what makes streaming feel live. State-replacement causes visible flicker.

The `message.complete` event provides the state-snapshot fallback, so any delta-loss recovery happens at message boundaries (which is fine — users won't notice a flicker at the end of a message).

### 5.6 What v1 deliberately does not do

- **Best-effort partial JSON parse during tool input streaming.** The client gets raw fragments only. Phase 2 may add lenient parsing that emits `tool.use_input_partial_parsed` events as the JSON becomes parseable. v1 is simpler and provider-portable.
- **Cross-block content reordering.** Some providers stream content blocks in a different order than they end up in the final message. v1 trusts `content_block_index` to disambiguate; if a provider sends out-of-order indices, the adapter fixes the index assignment before emitting the canonical event. Clients render in arrival order; gaps are filled by `message.complete`.

---

## 6. Cancellation

### 6.1 Trigger

A client sends `{"type": "cancel", "turn_id": "01HZ_xyz", "reason": "user_cancel"}`. The reason is informational; the server applies the same cancellation logic regardless.

The cancel can also originate from the server side (e.g., the configured turn timeout fires); the wire protocol is the same, just emitted by the session manager rather than received from a client.

### 6.2 Server-side propagation

A turn can be cancelled at three different points in its lifecycle. The event sequence differs across cases. The dispatcher, adapter, and session manager coordinate to ensure exactly one consistent sequence is emitted.

#### 6.2.1 Cancel during LLM streaming

Cancel arrives while the assistant is still emitting deltas (no tool dispatched yet for this LLM call).

1. Session manager marks the turn as cancelling.
2. Adapter aborts the in-flight HTTP stream. Tokens already received remain in the partial assistant message.
3. Adapter emits final streaming events:
   - `tool.use_end` for any in-flight `tool.use_start` that didn't yet emit its end (`final_input` set to whatever JSON parses cleanly, or `{}`).
   - `message.complete` with `stop_reason: cancelled` and partial `final_content`.
4. Bus events emitted (in order):
   - `llm.call_failed` with `error_class: cancelled`.
   - `turn.cancelled` with `reason` and partial-state metadata.

There is no `tool.failed` in this case — no tool was dispatched yet.

#### 6.2.2 Cancel during tool dispatch

Cancel arrives after `llm.call_completed` and one or more tools are running (the LLM call already finished normally; the agent loop is mid-tool-execution).

1. Session manager marks the turn as cancelling.
2. Tool dispatcher cancels each in-flight tool per `tool-dispatcher.md` §8.
3. Each cancelled tool emits `tool.failed` with `error_class: cancelled`. Tool-specific cleanup (subprocess SIGTERM/SIGKILL for shell, etc.) happens during this step.
4. Bus event: `turn.cancelled` with `reason`.

There is no `llm.call_failed` in this case — the LLM call already completed normally before the cancel arrived. The streaming layer also has nothing to flush; no `message.complete` is re-emitted (the original `message.complete` was already sent at the end of the LLM stream).

#### 6.2.3 Cancel at the seam

Cancel arrives between `llm.call_completed` and the start of tool execution (the agent loop has the tool_use but hasn't yet entered the tool's `execute()`).

1. Session manager marks the turn as cancelling.
2. Tool dispatcher checks pending tools; they have not yet started.
3. For each pending tool: emits `tool.failed` with `error_class: cancelled` (the tool was scheduled but never executed).
4. Bus event: `turn.cancelled`.

This is the same as 6.2.2 mechanically; the difference is only that no SIGTERM is needed because nothing was running yet.

#### 6.2.4 Cancel during a follow-up LLM call

Cancel arrives during a later LLM call in the tool loop (e.g., after one round-trip of tool_use/tool_result, the agent is mid-second-LLM-call). This is just §6.2.1 but in the middle of a multi-call turn. The same sequence applies; `turn.cancelled` follows.

#### 6.2.5 What clients see

In all cases the client sees a consistent termination: either `llm.call_failed` (case 6.2.1, 6.2.4) or `tool.failed` events (case 6.2.2, 6.2.3) followed by `turn.cancelled`. The originating client gets no special frame — canonical events are sufficient. Clients render the partial assistant message with a "(cancelled)" annotation; cancelled tools render with strike-through or similar.

#### 6.2.6 Updates to message state

The partial assistant message gets `status: cancelled` per `MessageStatus` in the canonical format spec. Any cancelled tool calls get `status: cancelled` in the `tool_calls` table.

### 6.3 Cancellation does not "resume"

A cancelled turn cannot be resumed. The next user message starts a fresh turn. If the user wants to retry the same task, they re-send the message; routing fires fresh.

This is deliberate — partial assistant messages may have inconsistent tool-call state, and "resume from line 14 of a JSON tool input" is a footgun. The simplicity is worth the slight redundancy of resending.

### 6.4 Cancellation during delegation

If a planner is mid-delegation when the user cancels:

1. The cancel applies to the planner's turn.
2. The worker session receives a cancellation signal; its in-flight LLM call and tools cancel per §6.2.
3. The worker's `delegate.failed` event is emitted with `failure_mode: cancelled_by_user`.
4. The planner's `turn.cancelled` follows.

Connected clients of the planner session see the planner's cancellation events. Clients of the worker session (if any are explicitly attached) see the worker's cancellation events.

### 6.5 Cancel-while-already-cancelling

If the client sends `cancel` for a turn that is already cancelling (e.g., the user mashes Ctrl-C twice), the second cancel is a no-op. The server logs `bus.handler_warning` but does not error. There is no canonical event for redundant cancels.

---

## 7. Worker sessions and event visibility

### 7.1 Default: workers hidden from parent's stream

By default (`include_worker_sessions: false` in the subscribe filter), a parent session's stream does *not* include events from its workers. Parent-stream clients see:

- `delegate.started` (the planner called `delegate()`)
- *gap — worker is doing its thing*
- `delegate.completed` or `delegate.failed`

This matches §6.2.2 of the routing engine spec: workers are background sub-tasks, not part of the parent's user-visible flow.

### 7.2 Opt-in: `include_worker_sessions: true`

When set, the parent stream interleaves events from all worker sessions spawned by this session's planner, with each worker event tagged via the standard `session_id` field. Clients can identify them by:

- `event.session_id` differing from the parent's session id.
- The presence of a `parent_session_id` on the worker session record (queryable via REST).

This mode is for the dashboard's "trace mode" or for power-user debugging. The TUI does not enable it by default.

### 7.3 Direct attach to worker sessions

Clients can also attach directly to a worker session via the same handshake — worker session ids are returned in `delegate.started` events. The dashboard's "drill into worker" link does this.

A worker's WebSocket stream behaves identically to a parent's. The worker is just a session.

---

## 8. Backpressure

### 8.1 Per-client outbound queue

Each WebSocket connection has a bounded outbound queue (default 1,000 events). Events are enqueued by the streaming subscriber when they match the client's filter. The connection writer drains the queue and writes frames.

### 8.2 Slow client policy

If a client's queue fills:

1. The next event that would exceed the bound is *not* enqueued.
2. The streaming subscriber emits `bus.handler_warning` (a meta-event) with `subscription_name` identifying the slow client.
3. The server closes the WebSocket connection with status code `1008` (policy violation) and a JSON close frame:
   ```json
   { "code": "client_too_slow", "message": "Outbound queue overflowed; reconnect with replay." }
   ```
4. Server cleans up the per-client queue.
5. The client sees the close, reconnects per §3.6 with `since: <last_received_event_id>`. Any missed events are replayed.

This is harsh but clean. Trying to gracefully degrade a slow client (drop oldest, drop newest, batch frames) produces inconsistent UI state. Forced reconnect with replay is well-defined and terminates: either the client catches up, or it cannot keep up at all (in which case the user should know).

### 8.3 Why this doesn't stall the agent loop

The bus dispatcher fans out to subscribers asynchronously. The streaming subscriber's "enqueue to client queue" is O(1) and never blocks. If a client queue is full, the streaming subscriber enqueues to a different client's queue normally and triggers the close on the slow one — without any back-pressure to the bus emitter or other subscribers.

This means a single slow client can never affect:
- The agent loop's ability to produce events.
- Other clients' streams.
- The trace store (which is its own subscriber).

### 8.4 Sizing

1,000 events at single-user scale is roughly 30 seconds of typical streaming behavior. A client falling 30 seconds behind almost certainly has a real problem (frozen UI, network hang); forced reconnect is appropriate.

The bound is configurable in server config. Test environments may want larger to avoid spurious closes during heavy logging.

---

## 9. Errors and edge cases

### 9.1 Errors during stream

If the agent loop produces an error mid-turn (e.g., adapter raises), the canonical events are:

```
llm.call_failed
turn.completed   (or turn.cancelled if recovery is impossible)
```

Clients receive these like any other events. There is no special "error stream" — errors are events. The TUI's responsibility is to render `llm.call_failed` as a visible error in the message area.

### 9.2 Unknown event types

If the server emits an event whose type the client doesn't recognize (e.g., the catalog gained a type the client's version doesn't know about), the client must skip silently. The server makes no assumption that all clients understand all event types. Forward compatibility.

### 9.3 Filter mismatches

If the client's `subscribe.filter.event_types` includes a string that is neither a catalog event type (event-bus §4.5) nor a streaming-only event type (§5.3), the server returns `subscribe_error: invalid_filter` with the offending string. The client must fix its filter and re-subscribe.

The server is strict because tolerating unknown filter strings would silently drop events the client thought it asked for. The accepted set is the union of both event families — `text.delta` (streaming) and `route.decided` (catalog) are both valid filter entries.

Update §11.1.13: the "unknown event type" test must use a string that is in neither family (e.g., `made.up.thing`); using a streaming-only type in the filter is valid and must pass.

### 9.4 Stale snapshot

If a client requests `snapshot: true` and the session is being heavily edited mid-snapshot, the snapshot may not reflect the absolute latest state. That's acceptable — the seam logic in §3.7 ensures the client catches up via the live stream. The snapshot is "consistent as of `snapshot_at_event_id`," not "consistent as of right now."

### 9.5 Very large messages

For unusually long assistant messages (>50k tokens), `message.complete.final_content` may be large. v1 sends it whole; if this becomes a bandwidth issue, Phase 3 may add a flag for the client to opt out of `final_content` and rely on its delta reconstruction alone.

---

## 10. Worked examples

### 10.1 Fresh attach

```
client: GET /sessions/sess_42                                      → 200 with attach_token
client: WS connect ws://.../sessions/sess_42/stream?attach=...
client → server: { "type": "subscribe", "filter": "preset:chat",
                   "since": null, "snapshot": true }
server → client: { "type": "subscribe_ack", "resolved_filter": {...},
                   "since": null, "snapshot": true, "replay_event_count": 0 }
server → client: { "type": "snapshot", "session": {...}, "messages": [...],
                   "snapshot_at_event_id": "evt_01HZ_a" }
[user submits a turn via HTTP POST /sessions/sess_42/turns]
server → client: { "type": "event", "event": { "type": "turn.started", ... } }
server → client: { "type": "event", "event": { "type": "route.decided", ... } }
server → client: { "type": "event", "event": { "type": "llm.call_started", ... } }
server → client: { "type": "event", "event": { "type": "message.start", ... } }
server → client: { "type": "event", "event": { "type": "text.delta", ... } }
... many text.delta ...
server → client: { "type": "event", "event": { "type": "message.complete", ... } }
server → client: { "type": "event", "event": { "type": "llm.call_completed", ... } }
server → client: { "type": "event", "event": { "type": "turn.completed", ... } }
```

### 10.2 Reconnect after disconnect

Client was attached, received events through `evt_01HZ_q`, then dropped. Reconnects:

```
client: GET /sessions/sess_42                                      → 200 with new attach_token
client: WS connect ...
client → server: { "type": "subscribe", "filter": "preset:chat",
                   "since": "evt_01HZ_q", "snapshot": false }
server → client: { "type": "subscribe_ack", ... "replay_event_count": 47 }
server → client: 47 event frames (events with id > evt_01HZ_q at time of subscribe)
[server transitions to live stream; any events that arrived during replay drain are queued and now flushed]
server → client: live event frames as they happen
```

If the gap is too large (`replay_event_count` would exceed 10,000) or the cursor is past retention:

```
server → client: { "type": "subscribe_error", "code": "replay_too_large",
                   "message": "Gap exceeds replay limit. Reconnect with snapshot: true." }
client: WS close, retry with snapshot: true
```

### 10.3 Cancellation mid-tool (§6.2.2 case)

Turn in progress. LLM finished its call (already emitted `llm.call_completed` with `stop_reason: tool_use`). Tool is running.

```
server → client: llm.call_completed
server → client: tool.called   tool_name=shell
[tool is running — long-running command]
client → server: { "type": "cancel", "turn_id": "01HZ_xyz", "reason": "user_cancel" }
server → client: tool.failed   error_class=cancelled, tool_use_id=tu_01HZ...
server → client: turn.cancelled   reason=user_cancel
```

No `message.complete` retransmitted (already sent at end of LLM stream); no `llm.call_failed` (LLM call completed normally before cancel arrived). The TUI renders the cancelled tool with a strikethrough; the prior assistant message remains as-rendered.

### 10.3b Cancellation mid-LLM (§6.2.1 case)

Turn in progress. LLM is mid-stream (text deltas arriving).

```
server → client: message.start
server → client: text.delta   "I'll start by..."
server → client: text.delta   "...analyzing the..."
client → server: { "type": "cancel", "turn_id": "01HZ_xyz", "reason": "user_cancel" }
server → client: message.complete   stop_reason=cancelled, final_content=[partial text]
server → client: llm.call_failed   error_class=cancelled
server → client: turn.cancelled   reason=user_cancel
```

The TUI renders the partial assistant message with a "(cancelled)" annotation. No tools were involved.

### 10.4 Multi-client with one cancel

Two clients (TUI and dashboard) attached to the same session. A tool is running. User cancels in the TUI (per §6.2.2 — cancel during tool dispatch).

```
TUI → server: cancel
server propagates internally (signals tool dispatcher; tool receives SIGTERM etc.)
server → TUI:       tool.failed, turn.cancelled
server → Dashboard: tool.failed, turn.cancelled   (same events)
```

Both clients render the cancellation simultaneously. There is no asymmetry between the originating client and others. The exact events emitted depend on which §6.2 case applies (cancel during LLM streaming → `message.complete`+`llm.call_failed`+`turn.cancelled`; cancel during tool dispatch → `tool.failed`+`turn.cancelled`; cancel at the seam → `tool.failed`+`turn.cancelled` with no actual tool execution). All connected clients see the same sequence regardless of which one initiated the cancel.

### 10.5 Backpressure-induced reconnect

Dashboard client stops reading frames (browser tab backgrounded; render thread frozen). Outbound queue fills.

```
... events stream normally ...
server: queue overflow detected for dashboard's connection
server → bus: bus.handler_warning event emitted
server → dashboard: WS close 1008 { "code": "client_too_slow", ... }
[dashboard tab unfreezes]
dashboard: WS reconnect with since: <last_received_id>
server → dashboard: subscribe_ack, replay events
```

The TUI on the same session is unaffected throughout.

### 10.6 Worker visibility

Planner session attached, dashboard subscribed with `include_worker_sessions: false` (default).

```
server → dashboard: route.decided (planner's turn)
server → dashboard: llm.call_started
server → dashboard: tool.called   tool_name=delegate
server → dashboard: delegate.started   worker_session_id=sess_99
[worker session runs, emitting its own events; dashboard does NOT receive them]
server → dashboard: delegate.completed   worker_session_id=sess_99, success=true
server → dashboard: tool.completed   tool_name=delegate
server → dashboard: llm.call_started   (planner integrates worker output)
... continues ...
```

To inspect the worker, dashboard opens a separate WebSocket to `/sessions/sess_99/stream`.

---

## 11. Testing strategy

### 11.1 Required tests

1. **Happy-path stream.** Submit a turn; receive events in canonical order; verify final reconstructed message matches `message.complete.final_content`.
2. **Snapshot consistency.** Connect mid-session; verify snapshot reflects state through `snapshot_at_event_id`; verify subsequent events have `id > snapshot_at_event_id`.
3. **Reconnect with replay.** Drop and reconnect; verify replayed event sequence matches the gap; verify no duplicates.
4. **Replay too large.** Force a 15,000-event gap; verify `subscribe_error: replay_too_large`.
5. **Cursor expired.** Use a cursor older than retention; verify `subscribe_error: cursor_expired`.
6. **Multi-client identical streams.** Two clients attached, same filter; verify they receive identical event sequences (modulo timing).
7. **Cancellation during LLM streaming (§6.2.1).** Cancel mid-text; verify `message.complete.stop_reason: cancelled` with partial content, then `llm.call_failed` with `error_class: cancelled`, then `turn.cancelled`. No `tool.failed` events.
8. **Cancellation during tool dispatch (§6.2.2).** A tool is executing (LLM call already emitted `llm.call_completed`); cancel; verify `tool.failed` with `cancelled` for each running tool, then `turn.cancelled`. No `message.complete` retransmission. No `llm.call_failed`.
8b. **Cancellation at the seam (§6.2.3).** Cancel arrives after `llm.call_completed` but before any tool has entered `execute()`; verify each pending tool emits `tool.failed` with `cancelled`, then `turn.cancelled`. No actual tool execution; no SIGTERM signaled.
9. **Cancellation during delegation.** Cancel a planner with an in-flight worker; verify worker emits `delegate.failed` with `cancelled_by_user` and planner's `turn.cancelled` follows.
10. **Idempotent cancel.** Send cancel twice; verify only one cancellation occurs.
11. **Slow client closure.** Stub a client that never reads; verify the server closes with `1008` after queue overflow and other clients are unaffected.
12. **Worker-hidden vs. worker-included.** Same session viewed with both filter modes; verify worker events appear in `include_worker_sessions: true` and not in `false`.
13. **Filter validation.** Subscribe with an unknown event type; verify `subscribe_error: invalid_filter`.
14. **Forward-compat unknown type.** Server emits a synthetic event of unknown type; verify the client's recommended skip behavior in the test client (no raise, debug log).
15. **Heartbeat.** Connection idle for >30s; verify ping; client unresponsive for 3 pings; verify server closes.
16. **Snapshot/replay seam.** Force events to arrive during snapshot computation; verify they appear exactly once after the snapshot.

### 11.2 Property tests

- **Per-connection ordering.** For any sequence of emitted events that match a connection's filter, the connection receives them in emission order.
- **Reconstruction equivalence.** For any assistant message produced by streaming, the deltas accumulated by the client equal `message.complete.final_content` (modulo intentional state replacement on mismatch).

### 11.3 Cross-implementation conformance

When clients are implemented in multiple languages (TUI in Python via Textual, eventually a TypeScript dashboard, eventually a Rust client in Tauri), all must pass a shared conformance suite that drives them with recorded server traces and asserts the rendered state. This suite lives in `tests/streaming-conformance/` and is part of the canonical test infrastructure.

---

## 12. Open questions

1. **Best-effort streaming JSON parsing.** Phase 2 may add `tool.use_input_partial_parsed` events. Exact emission rules (every N tokens? every successful parse attempt?) deferred.
2. **Worker streaming back to planner.** Currently planner waits for `delegate.completed`. Streaming worker output to the planner mid-execution is deferred (per routing engine spec §11.4).
3. **Compression.** None in v1. If WebSocket bandwidth becomes an issue (unlikely at localhost), `permessage-deflate` is the standard option.
4. **Push-to-client of routing config changes.** When the user edits `routing.yaml` and the server hot-reloads, no event is currently emitted. Should the streaming protocol surface a `routing.policy_reloaded` event so dashboards can refresh? Probably yes, in Phase 2.
5. **Cross-machine streaming.** Localhost-only in v1. Adding TLS, auth, and remote attach is a Phase 4+ project tied to making the server runnable on a remote box.
6. **Frame batching.** Multiple small events in a tight burst could be coalesced into a single WebSocket frame for efficiency. Not needed at single-user scale; deferred.
7. **Replay limit configurability.** 10,000 events is a default. May expose as config in Phase 2 once we see real usage patterns.

---

## 13. Decision log

| Date       | Decision                                                              | Rationale                                                                                  |
|------------|-----------------------------------------------------------------------|--------------------------------------------------------------------------------------------|
| 2026-05-08 | One WebSocket per session; not multiplexed                            | Per-connection flow control; simpler implementation; localhost speeds make this fine.      |
| 2026-05-08 | Event types as the filtering primitive; presets are sugar             | Direct mechanism; presets prevent endless mode proliferation.                              |
| 2026-05-08 | Snapshot+replay with cursor seam buffered from accept                 | Standard pattern; gap-free, duplicate-free; brief memory cost during transition.           |
| 2026-05-08 | Token-level deltas, not state snapshots, with `message.complete` as fallback | Wire-cheap, smooth UI; state fallback handles any delta loss at message boundaries.   |
| 2026-05-08 | No best-effort JSON parsing of tool input deltas in v1                | Provider-portable; clients render placeholder until `tool.use_end`.                        |
| 2026-05-08 | Cancelled turns cannot be resumed                                     | Partial state has too many edge cases; resending the user message is simpler and reliable. |
| 2026-05-08 | Slow clients are forcibly disconnected, not gracefully degraded       | Graceful degradation produces inconsistent UI; forced reconnect with replay is well-defined. |
| 2026-05-08 | Workers hidden from parent stream by default                          | Workers are background sub-tasks; surfacing them clutters the user-visible flow.           |
| 2026-05-08 | Turn submission via HTTP, not WebSocket                               | Clean separation: WebSocket is a stream; HTTP is request/response. Simplifies flow control. |
| 2026-05-08 | Single attach token, not session-wide                                 | Namespaces this connection's filter and cursor; simplifies reconnect.                      |
| 2026-05-08 | Replay capped at 10,000 events                                        | Bounds server work; clients with larger gaps reconnect with snapshot.                      |
| 2026-05-08 | History older than snapshot retrieved via REST, not WebSocket         | Mixing paginated reads with live streaming complicates flow control.                       |
| 2026-05-08 | Strict filter validation; unknown event types reject the subscribe    | Tolerating unknowns silently drops events the client thought it asked for.                 |
| 2026-05-08 | Streaming events are a separate transient layer, not bus catalog events | A 200-token message would produce 200+ trace rows; reconstructible from persisted Message + usage. |
| 2026-05-08 | Streaming server is not a bus subscriber; receives events directly from agent loop | Two channels with different lifetimes (persisted vs. live); merged on the wire only. |
| 2026-05-08 | Cancellation event sequence has three distinct cases (LLM, tool, seam) | One sequence "always" was wrong; cases differ in whether the LLM call had completed.       |

---

## 14. References

- `event-bus-and-trace-catalog.md` — `Event` envelope, the closed catalog of types, fast-path subscriber semantics.
- `routing-engine.md` — turn lifecycle and lock; cancellation semantics on turns; `delegate()` worker visibility.
- `canonical-message-format.md` — `Message`, content blocks, `MessageStatus`, `Usage`.
- `skill-format.md` (planned) — skill events that flow through this stream.
