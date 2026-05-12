# Server API Specification

**Status:** Draft v1.1
**Last updated:** 2026-05-08
**Owner:** _your name_

> **v1.1 changes:** `routing_failed` 503 response body shape defined (§4.2).

> *Throughout: paths shown use `~/.yourtool/` as a placeholder for the final config directory.*

---

## 1. Purpose

This document specifies the HTTP REST API the core server exposes for clients. The API covers:

- Session lifecycle (create, read, list, end).
- Turn submission (the user's input that triggers an LLM call).
- Message history (paginated read of past turns).
- The streaming attach handshake (the HTTP step that precedes a WebSocket connection).
- Tool confirmation responses (when a tool prompts the user via the streaming layer, the response comes back via HTTP).
- Routing inspection (`/rules check`, model registry queries).

Streaming itself (live event delivery, snapshot, replay, cancellation) is the WebSocket protocol from `streaming-protocol.md`. This spec covers the request/response side; the streaming spec covers the long-lived event subscription side.

This spec depends on:

- `canonical-message-format.md` for `Message`, `Session`, `ToolResultBlock`.
- `event-bus-and-trace-catalog.md` for events the API surfaces.
- `streaming-protocol.md` for the attach handshake, cancellation, and the WebSocket counterpart of this REST surface.
- `routing-engine.md` for `/rules check` semantics.
- `tool-dispatcher.md` for confirmation-response semantics.

---

## 2. Goals and non-goals

### 2.1 Goals

1. **Clean separation from the streaming surface.** REST is for request/response actions; WebSocket is for live event delivery. This separation simplifies flow control on both sides.
2. **Stateless from the HTTP layer's perspective.** Session state lives in the server; HTTP requests carry session ids and operate on persisted state.
3. **Idempotent where possible.** Common operations (read session, list sessions, get message page) are pure GETs. State-mutating operations have explicit POST semantics.
4. **Localhost-only in v1.** No auth, no TLS, no CORS concerns. Phase 4+ remote-attach is a separate concern.
5. **Self-describing errors.** All non-2xx responses carry a structured error body.

### 2.2 Non-goals

1. **Real-time updates over HTTP.** Long polling, SSE on REST endpoints, etc. — not provided. Clients use WebSocket.
2. **Bulk operations.** No batch creation, batch deletion, etc. v1 is single-resource per request.
3. **GraphQL or query DSL.** Plain REST. Filtering on list endpoints is via simple query parameters.
4. **Authentication.** API key, bearer tokens, OAuth — none in v1. Phase 4+.
5. **Versioned URL prefix.** No `/v1/` prefix in v1. When breaking changes happen, a `/v2/` prefix is added; v1 remains for migration. (This is a defensible v1 choice; alternative is to start with `/v1/` from day one. See §11.1.)

---

## 3. Conventions

### 3.1 Base URL

```
http://127.0.0.1:8421
```

The port is configurable; default 8421. v1 binds only to loopback (`127.0.0.1`), not `0.0.0.0`. The server refuses to bind to non-loopback in v1 even if asked — this is a safety guarantee until auth ships.

### 3.2 Content type

Request and response bodies are JSON: `Content-Type: application/json`. The server returns 415 for non-JSON request bodies on endpoints that require a body.

### 3.3 IDs

All IDs are ULIDs (per `canonical-message-format.md`). They appear in URL paths and bodies as strings.

### 3.4 Time

All timestamps are ISO 8601 with microsecond precision and UTC offset:

```
"2026-05-08T14:23:11.123456Z"
```

### 3.5 Pagination

List endpoints with potentially large result sets use cursor pagination:

- `?cursor=<opaque>`: continuation cursor from a prior response.
- `?limit=N`: max results (default 50, max 200).

Responses include `next_cursor` (or `null` if no more results).

### 3.6 Errors

All non-2xx responses have a JSON body:

```json
{
  "error": {
    "code": "session_not_found",
    "message": "No session with id sess_99",
    "details": { /* optional, error-specific */ }
  }
}
```

The `code` field is a closed enum per endpoint (documented per endpoint). Clients dispatch on `code`, not on `message` (which may change wording).

---

## 4. Endpoints

### 4.1 Sessions

#### `POST /sessions`

Create a new session.

**Request body:**
```json
{
  "workspace_path": "/Users/me/code/myproject",
  "initial_active_model": "anthropic:claude-sonnet-4-6"
}
```

`workspace_path` is required (the session is workspace-scoped). `initial_active_model` is optional; if omitted, the workspace's default model from `routing.yaml` is used.

**Response 201:**
```json
{
  "id": "sess_01HZ...",
  "workspace_path": "/Users/me/code/myproject",
  "active_model": "anthropic:claude-sonnet-4-6",
  "created_at": "2026-05-08T14:23:11.123456Z",
  "routing_policy_version": "<sha256 of routing.yaml>"
}
```

**Error codes:**
- `workspace_not_found` (400) — path doesn't exist or isn't a directory.
- `model_not_configured` (400) — the requested initial model isn't in the registry.

#### `GET /sessions`

List sessions, most recent first.

**Query parameters:**
- `workspace_path`: filter by workspace.
- `include_workers`: default `false`. Per `routing-engine.md` §6.2.2.
- `cursor`, `limit`: pagination.

**Response 200:**
```json
{
  "sessions": [
    {
      "id": "sess_01HZ...",
      "workspace_path": "/Users/me/code/myproject",
      "active_model": "anthropic:claude-sonnet-4-6",
      "created_at": "...",
      "updated_at": "...",
      "turn_count": 7,
      "cost_so_far_usd": 0.142,
      "is_worker": false,
      "parent_session_id": null
    }
  ],
  "next_cursor": "..." 
}
```

#### `GET /sessions/{session_id}`

Read session metadata. **This endpoint serves the dual purpose of returning session info and issuing the WebSocket attach token** (per `streaming-protocol.md` §3.1).

**Response 200:**
```json
{
  "id": "sess_01HZ...",
  "workspace_path": "/Users/me/code/myproject",
  "active_model": "anthropic:claude-sonnet-4-6",
  "routing_policy_version": "<sha256>",
  "cost_so_far_usd": 0.142,
  "turn_count": 7,
  "current_turn_id": null,
  "current_turn_status": null,
  "attach_token": "atk_01HZ...",
  "ws_url": "ws://127.0.0.1:8421/sessions/sess_01HZ.../stream?attach=atk_01HZ..."
}
```

`attach_token` is single-use, valid for 60 seconds, scoped to this session. Each call to `GET /sessions/{id}` issues a fresh token; existing tokens for the same session are not invalidated (multiple clients can attach independently).

**Error codes:**
- `session_not_found` (404).

#### `DELETE /sessions/{session_id}`

End a session. Marks the session as `disposition: completed`. Open WebSocket connections are closed cleanly (server sends a close frame, then disconnects). The session record persists for history; this endpoint does not delete data.

**Response 200:**
```json
{
  "id": "sess_01HZ...",
  "ended_at": "..."
}
```

**Error codes:**
- `session_not_found` (404).
- `session_already_ended` (409).

To actually delete a session and its data: `DELETE /sessions/{session_id}?purge=true`. This deletes messages, events, tool calls. Used for privacy / cleanup. Sessions in worker mode follow their parent's purge.

#### `PATCH /sessions/{session_id}`

Update session settings. Currently supported: `active_model` (the manual sticky from `routing-engine.md` §3.3 and §4.3).

**Request body:**
```json
{
  "active_model": "anthropic:claude-opus-4-7"
}
```

If a turn is in flight, the swap is queued per `routing-engine.md` §3.3.

**Response 200:**
```json
{
  "id": "sess_01HZ...",
  "active_model": "anthropic:claude-opus-4-7",
  "swap_queued": true,
  "swap_queued_until_turn": "01HZ_xyz"
}
```

`swap_queued: false` if no turn is in flight (swap is immediate). `swap_queued: true` and `swap_queued_until_turn` populated if it'll apply at the next turn boundary.

To clear sticky (`/model -` in TUI): `PATCH` with `{"active_model": null}`.

**Error codes:**
- `session_not_found` (404).
- `model_not_configured` (400).
- `session_already_ended` (409).

### 4.2 Turns

#### `POST /sessions/{session_id}/turns`

Submit a user message; trigger a turn. This is the main work endpoint.

**Request body:**
```json
{
  "content": [
    {"type": "text", "text": "Read README.md and summarize"}
  ],
  "per_message_override": null
}
```

`content` is a list of canonical content blocks the user's message contains (TextBlock, ImageBlock — anything valid for a USER message per canonical-format §5.1.3).

`per_message_override` is optional. If set, it's a model id (canonical or alias) to use for this one turn — same semantics as `@haiku` syntax in TUI message text. If both are set (the message starts with `@haiku` AND `per_message_override` is set), the explicit field wins.

**Response 202:**
```json
{
  "turn_id": "01HZ_xyz",
  "session_id": "sess_01HZ...",
  "submitted_at": "...",
  "user_message_id": "01HZ_msg..."
}
```

The endpoint returns immediately — the actual turn happens asynchronously and emits events via the streaming WebSocket. Clients should be subscribed to the WebSocket *before* submitting a turn to avoid missing the early streaming events.

**Error codes:**
- `session_not_found` (404).
- `session_already_ended` (409).
- `turn_in_flight` (409) — a prior turn hasn't completed; submit again after `turn.completed`.
- `invalid_content` (400) — content blocks fail canonical validation.
- `routing_failed` (503) — no model available (routing's hard failure per `routing-engine.md` §4.7). Response body shape:
  ```json
  {
    "error": {
      "code": "routing_failed",
      "message": "No model available for this turn",
      "details": {
        "tried": [
          {
            "model": "anthropic:claude-opus-4-7",
            "policy": "rule",
            "rule_name": "deep for architecture",
            "reason": "provider_unavailable"
          },
          {
            "model": "anthropic:claude-sonnet-4-6",
            "policy": "workspace_default",
            "rule_name": null,
            "reason": "provider_unavailable"
          }
        ],
        "alternative_providers_configured": ["openai"]
      }
    }
  }
  ```
  `tried` lists each policy's candidate that was rejected, in chain order. `policy` values match the routing chain enum (`per_message_override`, `manual_sticky`, `rule`, `pattern`, `workspace_default`, `global_default`). `reason` values match the `validation_failure` enum from `route.decided` (per `event-bus-and-trace-catalog.md` §6.5). `alternative_providers_configured` is a hint for clients to surface in the UI ("try /model openai:gpt-5"); empty list if no other configured providers exist.

#### `POST /sessions/{session_id}/turns/{turn_id}/cancel`

Cancel an in-flight turn. Equivalent to the WebSocket `cancel` frame; provided as REST for clients that don't have an active WebSocket (rare — most cancellation goes through WebSocket because the cancelling client is the same client streaming).

**Request body:**
```json
{
  "reason": "user_cancel"
}
```

**Response 202:**
```json
{
  "turn_id": "01HZ_xyz",
  "cancellation_initiated": true
}
```

The cancellation is asynchronous — the turn's actual end is signaled by `turn.cancelled` on the WebSocket.

**Error codes:**
- `session_not_found` (404).
- `turn_not_found` (404).
- `turn_already_completed` (409).

#### `POST /sessions/{session_id}/turns/{turn_id}/confirmations/{request_id}`

Respond to a tool confirmation request (per `tool-dispatcher.md` §5.3). The dispatcher emitted `tool.confirmation_requested` over the streaming layer; the user clicked allow/deny in the UI; this endpoint carries the answer back.

**Request body:**
```json
{
  "decision": "allow",
  "scope": "once"
}
```

`decision` is `allow` or `deny`. `scope` is `once` (default) or `session` (Phase 2 — `prompt_once` mode).

**Response 200:**
```json
{
  "request_id": "01HZ_conf...",
  "decision": "allow",
  "applied": true
}
```

`applied: false` if the confirmation has already been resolved (e.g., another client answered first, or the request timed out).

**Error codes:**
- `confirmation_not_found` (404) — request_id unknown or expired.
- `confirmation_already_resolved` (409).

### 4.3 Messages

#### `GET /sessions/{session_id}/messages`

List messages in a session. Used by clients to load history before/around the streaming snapshot.

**Query parameters:**
- `before`: a message id; return messages with `id < before`. For walking back through history.
- `after`: a message id; return messages with `id > after`. For forward pagination.
- `limit`: default 50, max 200.

If neither `before` nor `after` is set, returns the most recent `limit` messages.

**Response 200:**
```json
{
  "messages": [
    /* canonical Message objects */
  ],
  "has_more_before": true,
  "has_more_after": false
}
```

**Error codes:**
- `session_not_found` (404).

### 4.4 Events (read-only access to trace store)

#### `GET /sessions/{session_id}/events`

Query events for a session from the trace store. Used by the dashboard's session-detail view.

**Query parameters:**
- `event_types`: comma-separated list. If omitted, all types.
- `since`, `until`: event id range.
- `limit`: default 100, max 1000.
- `cursor`: continuation.

**Response 200:**
```json
{
  "events": [
    /* canonical Event objects */
  ],
  "next_cursor": null
}
```

**Error codes:**
- `session_not_found` (404).

This is for consumption by analytic clients. Real-time access goes through WebSocket.

### 4.5 Routing inspection

#### `GET /routing/policy`

Read the current parsed routing policy.

**Response 200:**
```json
{
  "version": "<sha256>",
  "valid": true,
  "loaded_at": "...",
  "global_default": "anthropic:claude-sonnet-4-6",
  "tiers": {"fast": "...", "balanced": "...", "deep": "..."},
  "rules": [
    {"name": "fast for commits", "when": {...}, "use": "..."}
  ],
  "workspaces": {...},
  "pattern": {...}
}
```

If `valid: false`, the response also includes `errors: list[str]` and `using_last_known_good: true`.

#### `POST /routing/check`

Validate the policy file without loading it. Used by `/rules check`.

**Request body (optional):**
```json
{
  "policy_yaml": "..."
}
```

If the body is present, validates that yaml. If absent, validates the on-disk `routing.yaml`.

**Response 200:**
```json
{
  "valid": true,
  "errors": [],
  "warnings": []
}
```

`warnings` includes things like rule shadowing detection (Phase 2 — see `routing-engine.md` §11.8).

#### `POST /routing/reload`

Force a re-read of `routing.yaml` (normally automatic per `routing-engine.md` §4.8).

**Response 200:**
```json
{
  "version": "<new sha256>",
  "valid": true
}
```

### 4.6 Model registry inspection

#### `GET /models`

List configured models with their capabilities.

**Response 200:**
```json
{
  "models": [
    {
      "id": "anthropic:claude-sonnet-4-6",
      "adapter": "anthropic",
      "tier": "balanced",
      "can_delegate": true,
      "aliases": ["sonnet", "balanced"],
      "capabilities": {
        "supports_images": true,
        "supports_tools": true,
        "max_context_tokens": 200000,
        "max_output_tokens": 8192
      },
      "availability": "healthy"
    }
  ]
}
```

`availability` is `healthy | model_unavailable | provider_unavailable` per the routing-engine availability state machine.

### 4.7 Health and meta

#### `GET /health`

Liveness check.

**Response 200:**
```json
{
  "status": "ok",
  "started_at": "...",
  "uptime_seconds": 12345,
  "active_sessions": 3,
  "active_turns": 1
}
```

No errors expected; the endpoint returns 503 only if the server is in shutdown.

#### `GET /server/version`

Server version info.

**Response 200:**
```json
{
  "version": "0.1.0",
  "schema_versions": {
    "canonical_message": 1,
    "events": 1,
    "routing_policy": 1
  }
}
```

Useful for clients to detect server upgrades and to warn about schema mismatches.

---

## 5. WebSocket attach (cross-reference)

The WebSocket attach handshake is detailed in `streaming-protocol.md` §3.1–3.2. Briefly:

1. Client calls `GET /sessions/{id}` (this spec, §4.1) to get an `attach_token`.
2. Client opens WebSocket to `ws_url` (returned in the same response).
3. Client sends `subscribe` frame on the WebSocket.

The HTTP and WebSocket sides are deliberately separate. Mixing them on one channel complicates flow control.

---

## 6. Worked examples

### 6.1 Create a session and submit a turn

```http
POST /sessions
Content-Type: application/json

{ "workspace_path": "/Users/me/code/myproject" }

→ 201 Created
{
  "id": "sess_01HZ_a",
  "workspace_path": "/Users/me/code/myproject",
  "active_model": "anthropic:claude-sonnet-4-6",
  "created_at": "2026-05-08T14:23:11Z",
  "routing_policy_version": "abc123..."
}
```

```http
GET /sessions/sess_01HZ_a

→ 200 OK
{
  "id": "sess_01HZ_a",
  ...
  "attach_token": "atk_01HZ_b",
  "ws_url": "ws://127.0.0.1:8421/sessions/sess_01HZ_a/stream?attach=atk_01HZ_b"
}
```

[Client opens WebSocket to ws_url, sends subscribe, receives snapshot]

```http
POST /sessions/sess_01HZ_a/turns
Content-Type: application/json

{ "content": [{"type": "text", "text": "What's a ULID?"}] }

→ 202 Accepted
{
  "turn_id": "01HZ_t1",
  "session_id": "sess_01HZ_a",
  "submitted_at": "2026-05-08T14:23:15Z",
  "user_message_id": "01HZ_m1"
}
```

[Client receives streaming events on WebSocket: turn.started, route.decided, llm.call_started, message.start, text.delta..., message.complete, llm.call_completed, turn.completed]

### 6.2 Mid-turn cancel via WebSocket fallback to REST

[Turn 01HZ_t2 in flight]
[WebSocket connection drops; client reconnects but cancellation is urgent]

```http
POST /sessions/sess_01HZ_a/turns/01HZ_t2/cancel
Content-Type: application/json

{ "reason": "user_cancel" }

→ 202 Accepted
{ "turn_id": "01HZ_t2", "cancellation_initiated": true }
```

[Client reconnects WebSocket; receives the trailing turn.cancelled event during replay]

### 6.3 Confirmation response

[Streaming event arrived: tool.confirmation_requested with request_id "01HZ_c1"]
[User clicks "Allow" in the TUI]

```http
POST /sessions/sess_01HZ_a/turns/01HZ_t3/confirmations/01HZ_c1
Content-Type: application/json

{ "decision": "allow", "scope": "once" }

→ 200 OK
{ "request_id": "01HZ_c1", "decision": "allow", "applied": true }
```

[Streaming event arrives: tool.confirmation_resolved, then tool.called, then tool.completed]

### 6.4 Manual model swap mid-turn (queued)

[Turn 01HZ_t4 in flight on sonnet]

```http
PATCH /sessions/sess_01HZ_a
Content-Type: application/json

{ "active_model": "anthropic:claude-opus-4-7" }

→ 200 OK
{
  "id": "sess_01HZ_a",
  "active_model": "anthropic:claude-opus-4-7",
  "swap_queued": true,
  "swap_queued_until_turn": "01HZ_t4"
}
```

[Turn 01HZ_t4 completes on sonnet; turn 01HZ_t5 starts on opus]

### 6.5 Page through history

```http
GET /sessions/sess_01HZ_a/messages?limit=10

→ 200 OK
{
  "messages": [/* 10 most recent */],
  "has_more_before": true,
  "has_more_after": false
}
```

```http
GET /sessions/sess_01HZ_a/messages?before=01HZ_m_first_in_first_page&limit=10

→ 200 OK
{
  "messages": [/* 10 older messages */],
  "has_more_before": true,
  "has_more_after": true
}
```

### 6.6 Routing validation

```http
POST /routing/check
Content-Type: application/json

{ "policy_yaml": "schema_version: 1\nglobal_default: nonexistent:model\nrules: []" }

→ 200 OK
{
  "valid": false,
  "errors": ["global_default references unknown model: nonexistent:model"],
  "warnings": []
}
```

---

## 7. Server lifecycle

### 7.1 Startup

1. Load `routing.yaml`. Validate. If invalid, server starts but routing uses last-known-good (or hardcoded defaults if no last-known-good exists).
2. Load model registry from `models.yaml`. Instantiate adapters. Validate API keys are accessible.
3. Open SQLite database (sessions, messages, events).
4. Register built-in event subscribers (trace store writer, streaming server, cost accumulator).
5. Register built-in tools.
6. Bind HTTP server to loopback.
7. Bind WebSocket handler to the same port.
8. Emit `bus.gap_detected` events for any gaps detected in the trace store on startup.

### 7.2 Shutdown

1. Stop accepting new HTTP requests (return 503).
2. Cancel any in-flight turns.
3. Close WebSocket connections cleanly (1001 going away).
4. Drain bus subscribers (final flush of trace store buffer).
5. Close adapter connection pools.
6. Close SQLite.

Graceful shutdown timeout: 30 seconds. After that, hard exit.

### 7.3 Configuration files

Server reads:

- `~/.yourtool/server.yaml` — port, log level, directories, server-specific settings.
- `~/.yourtool/routing.yaml` — routing policy (per `routing-engine.md` §5).
- `~/.yourtool/models.yaml` — model registry (per `provider-adapter-contract.md` §8.1).
- `~/.yourtool/skills/` — directory of skill files (Phase 2+).

All paths can be overridden via env var or CLI flag.

---

## 8. Errors — closed enum of codes

For each endpoint, the closed set of error codes is documented above. Aggregated here for reference:

| Code                              | HTTP | Endpoints                                          |
|-----------------------------------|------|----------------------------------------------------|
| `session_not_found`               | 404  | most endpoints                                      |
| `session_already_ended`           | 409  | turn submission, cancellation, patch                |
| `workspace_not_found`             | 400  | `POST /sessions`                                    |
| `model_not_configured`            | 400  | `POST /sessions`, `PATCH /sessions/{id}`            |
| `routing_failed`                  | 503  | `POST /sessions/{id}/turns`                         |
| `turn_in_flight`                  | 409  | `POST /sessions/{id}/turns`                         |
| `invalid_content`                 | 400  | `POST /sessions/{id}/turns`                         |
| `turn_not_found`                  | 404  | turn cancellation, confirmations                    |
| `turn_already_completed`          | 409  | turn cancellation                                   |
| `confirmation_not_found`          | 404  | confirmation response                               |
| `confirmation_already_resolved`   | 409  | confirmation response                               |
| `validation_error`                | 400  | any endpoint with body validation                   |
| `internal_error`                  | 500  | any endpoint, exceptional                           |
| `service_shutting_down`           | 503  | any endpoint, during graceful shutdown              |

---

## 9. Testing strategy

### 9.1 Required tests

1. **Session lifecycle.** Create, read, list, end, purge — all correct shapes; correct error codes for missing/invalid inputs.
2. **Workspace path validation.** `POST /sessions` with non-existent path → 400; with file (not directory) → 400.
3. **Turn submission and event flow.** Submit turn; verify turn_id returned; verify WebSocket events fire in expected order. (Cross-references streaming-protocol tests.)
4. **Concurrent turn rejection.** Submit a turn while one is in flight → 409 with `turn_in_flight`.
5. **Routing failure surfaces correctly.** Stub all providers as Unavailable; submit turn → 503 with `routing_failed` and details listing tried models.
6. **Cancel via REST.** Submit turn; cancel via REST; verify cancellation event over WebSocket.
7. **Confirmation response idempotency.** Send confirmation twice with the same request_id; first wins, second returns `applied: false` or 409.
8. **Multiple clients answering confirmation.** Two clients answer; first wins; second gets `confirmation_already_resolved`.
9. **Pagination.** Walk through 100+ messages with `before` cursor; verify all retrieved exactly once.
10. **Model swap queueing.** Patch active_model mid-turn; verify response indicates queueing; verify swap applies on next turn.
11. **Model swap clearing.** Patch active_model to null; verify subsequent turn uses the rule-driven default.
12. **Routing check validation.** Submit invalid yaml to `/routing/check`; verify `valid: false` and errors list.
13. **Health endpoint shape.** `/health` always returns 200 (or 503 in shutdown).
14. **Loopback-only binding.** Server refuses to bind to `0.0.0.0` even if config asks; logs error and binds to `127.0.0.1`.
15. **Schema version exposure.** `/server/version` reflects current schema versions.

---

## 10. Open questions

1. **URL versioning.** Start with `/v1/` prefix or unversioned (and add `/v2/` later)? v1 unversioned for simplicity; the cost is later-future migration. See §2.2.
2. **Session list query DSL.** v1 has filter by workspace and worker flag. Adding more filters (date range, cost range, model used) is straightforward but needs design decisions on indexing.
3. **Bulk operations.** No bulk delete, bulk export. Phase 3+ might add export-session for backups. Not in v1.
4. **WebSocket-only cancellation.** REST cancel endpoint duplicates WebSocket; consider whether to drop the REST endpoint (forcing WebSocket). v1 keeps both for resilience.
5. **Confirmation TTL.** v1: 5-minute timeout per `tool-dispatcher.md` §5.3. Configurable per-tool? Deferred.
6. **Streaming attach token use-once vs. multi-use.** v1 is single-use. Multi-use would let a client reattach without a new HTTP roundtrip. Deferred.
7. **CORS for the eventual web dashboard.** Dashboard runs on the same loopback origin in v1, so no CORS needed. When the dashboard is hosted separately (Phase 4+), CORS becomes a concern.
8. **Rate limiting.** v1 trusts the local client; no rate limits. Multi-user remote (Phase 4+) needs them.

---

## 11. Decision log

| Date       | Decision                                                              | Rationale                                                                                  |
|------------|-----------------------------------------------------------------------|--------------------------------------------------------------------------------------------|
| 2026-05-08 | REST for actions, WebSocket for streams, no overlap in concerns       | Cleaner flow control on both sides; matches streaming-protocol §2.2.                       |
| 2026-05-08 | Localhost-only bind in v1; refuses 0.0.0.0                            | Safety guarantee until auth ships.                                                         |
| 2026-05-08 | No URL version prefix in v1; add /v2/ on first breaking change        | Simplicity now; future migration cost accepted.                                            |
| 2026-05-08 | `GET /sessions/{id}` doubles as the WebSocket attach token issuer     | One handshake roundtrip; convenient pairing of metadata + attach in one response.          |
| 2026-05-08 | Single-use attach token, 60-second validity                           | Limits replay risk; cheap to issue fresh.                                                  |
| 2026-05-08 | Turn submission returns 202 immediately; events flow on WebSocket     | Matches the actual async work; clients should subscribe before submitting.                 |
| 2026-05-08 | Concurrent turns rejected with 409, not queued                        | Per-session sequential turn semantics; queueing would need session-level queue management. |
| 2026-05-08 | Cancellation available via both REST and WebSocket                    | Resilient to dropped WebSocket; both paths converge in the dispatcher.                     |
| 2026-05-08 | `DELETE /sessions/{id}?purge=true` for actual deletion                | Default delete is "end this session"; explicit flag for destructive purge.                 |
| 2026-05-08 | Closed error code enum per endpoint                                   | Clients dispatch on stable codes, not message strings.                                     |

---

## 12. References

- `streaming-protocol.md` — WebSocket-side counterpart; the attach handshake (§3.1) is paired with `GET /sessions/{id}` here.
- `canonical-message-format.md` — `Message`, `Session`, `ToolResultBlock` shapes returned by these endpoints.
- `event-bus-and-trace-catalog.md` — events surfaced by `/sessions/{id}/events`.
- `routing-engine.md` — `/routing/check`, `/routing/reload`, swap-queueing semantics.
- `tool-dispatcher.md` — confirmation request/response flow.
- `provider-adapter-contract.md` — model registry (§8.1) shape returned by `/models`.
