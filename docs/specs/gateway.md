# Gateway Specification

**Status:** v1 — shipped. Captures the OpenAI- and Anthropic-shape inbound surface in `apps/gateway/`, live-smoked end-to-end on 2026-05-14 at ~$0.0002 / 4 calls.
**Last updated:** 2026-05-14

> The HTTP gateway is the transparent-proxy surface from [`deployment-shape.md §1`](deployment-shape.md). It accepts OpenAI- or Anthropic-shape requests from external clients (Claude Code, Cursor, Codex, Continue, custom apps), routes via the existing engine, calls the existing adapters, and returns provider-shape responses — losslessly preserving Anthropic-native blocks where possible.
>
> This spec depends on:
>
> - [`canonical-message-format.md`](canonical-message-format.md) — `Message`, `ContentBlock` variants, `ToolDefinition`.
> - [`provider-adapter-contract.md`](provider-adapter-contract.md) — `CanonicalRequest`, `CanonicalResponse`, streaming events, error classes.
> - [`routing-engine.md`](routing-engine.md) — the 7-slot chain.
> - [`server-api.md`](server-api.md) — the gateway is a *sibling* HTTP app, not an extension of `metis serve`.
> - [`event-bus-and-trace-catalog.md`](event-bus-and-trace-catalog.md) — gateway calls emit the same `llm.call_*` / `route.decided` / `turn.completed` events as agent calls, with two additive payload fields (`gateway_key_id`, `inbound_shape`).
> - [`analytics-api.md`](analytics-api.md) — `/analytics/cost?group_by=gateway_key` and `/analytics/by_key` consume the additive fields above.

---

## 1. Purpose

Give the buyer Metis's model-selection + cost-attribution + lossless-canonical-IR value without making their devs switch tools. Devs keep using whatever client they already use (typically Claude Code or Cursor); operations flips one env var (`OPENAI_BASE_URL` / `ANTHROPIC_BASE_URL`) and adds a Metis-issued API key. Every LLM call now flows through Metis, gets routed, gets cost-attributed, and writes a trace.

The gateway is a **per-request stateless harness**: routing decision + adapter call + response translation + cost stamping. **The agent loop stays in the client.** Multi-turn tool use happens through the client re-submitting follow-up requests with `tool_result` blocks; the gateway is stateless across requests within the same agent loop.

---

## 2. Scope

### 2.1 In scope (shipped)

1. **OpenAI-shape inbound** (`POST /v1/chat/completions`, sync + SSE streaming). The universal contract.
2. **Anthropic-shape inbound** (`POST /v1/messages`, sync + SSE streaming) — closer to canonical IR; smaller translation gap; required to keep Claude Code clients native.
3. **Provider-native outbound** via the existing adapter set (Anthropic, OpenAI, OpenRouter). Adapters are unchanged.
4. **Routing per request** via the existing `RoutingEngine`. The inbound `model` field is interpreted as a per-message override (§5.3).
5. **Cost attribution per gateway key.** Each inbound request authenticates a key; `llm.call_completed` events are stamped with `gateway_key_id` so the [`analytics-api.md`](analytics-api.md) rollups can split cost by key.
6. **Trace events.** Same catalog as the agent path. Two additive payload fields (`gateway_key_id`, `inbound_shape`) carry the gateway-specific dimensions (§6).
7. **Lossless block round-trip** for the cases the existing canonical IR already handles: Anthropic `thinking`, `cache_control`, `tool_use`, `tool_result`, citations.

### 2.2 Out of scope (explicit non-features)

These are the bright lines that separate the gateway from the replacement agent. The gateway does not assume them, and must not be extended to add them — those features belong in the agent.

1. **No context shaping.** The gateway forwards the prompt envelope as the client supplied it. It does not inject system instructions, trim history, or compress turns. The client's agent loop owns context.
2. **No skill loading.** Skills are an in-loop concept (description-match → activation → script execution). A stateless gateway cannot meaningfully load a skill into someone else's agent; attempting to inject SKILL.md content would corrupt the client's prompt cache and break the client's tool contract.
3. **No memory composition.** `MEMORY.md` / `USER.md` are agent-loop artifacts. The gateway is workspace-aware (a key is scoped to one workspace per §3.3) but doesn't read or write the workspace's `.metis/` files.
4. **No tool execution.** The gateway never runs a tool. It passes `tool_use` blocks back to the client; the client executes the tool and re-submits a follow-up request with the `tool_result`.
5. **No pattern learning that shapes context.** Pattern store may *observe* gateway traffic (cost / model-selection signal), but it must not inject learned context into the request. Future learned-routing decisions can land in the routing chain; learned *context* cannot.
6. **No agent loop.** The gateway never makes the next LLM call on its own. Every call is in response to a client-initiated HTTP request.

These non-features are load-bearing for the [`deployment-shape.md`](deployment-shape.md) hybrid argument: they are the upgrade reasons that make the agent worth buying after the gateway.

---

## 3. Surface

### 3.1 Endpoints (v1)

| Endpoint | Method | Inbound shape | Description |
|---|---|---|---|
| `/v1/chat/completions` | POST | OpenAI | Standard chat completion; supports `stream: true` (SSE). |
| `/v1/messages` | POST | Anthropic | Anthropic Messages API; supports `stream: true` (SSE). |
| `/healthz` | GET | — | Liveness; no auth. |

Deferred to v1.1+: `/v1/models` (OpenAI-shape model list), `/v1/embeddings`, `/v1/completions` (legacy), tool-confirmation REST surface, batch APIs, file uploads.

### 3.2 Network posture

The gateway is `loopback-only by default in v1`, matching `metis serve`'s safety posture ([`server-api.md §3.1`](server-api.md)). `run_gateway()` silently rewrites any non-loopback bind to `127.0.0.1` until production-bind hardening lands (auth/rate limiting/audit; gateway.md §11). Operators who want the gateway in front of a TLS terminator on a real network must wait for that follow-on or run it themselves on top of the in-process Starlette app.

This decision reverses the original draft's "default `0.0.0.0`" plan. The reason for the change: until per-key rate limits and audit logging exist, exposing the gateway on a real network gives an attacker with one leaked key a wide blast radius (unbounded model spend on the operator's provider account, plus unmetered prompt exfiltration). Loopback is a conservative starting point; the surface can be widened deliberately.

### 3.3 Authentication

Each inbound request carries a gateway-issued bearer token:

- **OpenAI clients:** `Authorization: Bearer gw_<ulid>` (the standard OpenAI header).
- **Anthropic clients:** `x-api-key: gw_<ulid>` (the standard Anthropic header). The handler also accepts `Authorization: Bearer ...` as a fallback so generic SDKs can hit `/v1/messages` without special-casing the auth header.

Tokens are issued out-of-band via `metis gateway issue-key --name "<display>" --workspace "<path>" [--allow-models ...] [--daily-cap-usd ...] [--user <id>] [--team <id>]`. The plaintext is printed once; only the SHA-256 hex digest is persisted in the keystore (`~/.metis/gateway/keys.json` by default, mode `0o600`). The keystore records:

| Field | Source | Notes |
|---|---|---|
| `key_id` | `gk_<ulid>` minted at issuance | Stable analytics identifier; stamped on every trace event. |
| `secret_hash` | SHA-256(token) | The plaintext token is never stored or recoverable. |
| `name` | `--name` | Display label. |
| `workspace_path` | `--workspace` | Exactly one workspace per key in v1 (§11). |
| `allowed_models` | `--allow-models` (optional) | Tuple of canonical model ids; non-conforming routing falls through. |
| `daily_cap_usd` | `--daily-cap-usd` (optional) | Per-key daily spend cap. `Decimal`, must be > 0. Hard breaker (§6.4); soft alerts at 80% / 95%. |
| `monthly_cap_usd` | `--monthly-cap-usd` (optional) | Per-key calendar-month spend cap (UTC). Same enforcement model as `daily_cap_usd`. |
| `user_id` | `--user` (optional) | Stable per-developer identity tag (see [`multi-user.md §4.2`](multi-user.md)). `^[a-z0-9_-]+$`. Existing keys with `None` keep working. |
| `team_id` | `--team` (optional) | Stable team identity tag ([`multi-user.md §4.2`](multi-user.md)). `^[a-z0-9_-]+$`. |

Authentication: the handler hashes the inbound token, looks it up in the keystore, and returns 401 on miss. The handler then projects the resolved key onto a request-scoped `Identity` (`gateway_key_id`, `workspace_path`, `user_id`, `team_id` — multi-user.md §3.2 calls this the `Principal`); the harness reads only the `Identity`, never the raw `GatewayKey` for stamping. The `gateway_key_id`, `user_id`, and `team_id` are recorded on every outbound `llm.call_completed` and `turn.completed` event so [`analytics-api.md §4.8`](analytics-api.md) (and `/analytics/by_team`) can roll up cost by key / user / team. No PII flows into the key record — `users.json` carries plaintext (multi-user.md §3.3) and is a separate file; the keystore only references identity tags by id.

### 3.4 Compatibility with existing clients

| Client | Shape | Action |
|---|---|---|
| OpenAI SDK (`openai-python`, `openai-node`) | OpenAI | Set `base_url` / `OPENAI_BASE_URL` and `OPENAI_API_KEY` to the gateway. |
| Anthropic SDK | Anthropic | Set `base_url` / `ANTHROPIC_BASE_URL` and the `x-api-key` header to the gateway key. |
| Claude Code | Anthropic | Same as Anthropic SDK. The buyer flips `ANTHROPIC_BASE_URL` org-wide. |
| Cursor (closed) | OpenAI-compat or Anthropic-compat | Configurable in Cursor settings; flip the API base. |
| OpenCode / Cline / Continue / Goose | OpenAI-compat or per-provider | Configurable; flip the API base. |
| Custom internal app | Either | Same. |

The compatibility bar for v1 is "Claude Code → gateway → Anthropic API end-to-end, including tool use, thinking blocks, and prompt caching." The Wave-5 smoke (`scripts/smoke_*`) exercises that workload.

---

## 4. Request translation rules

This section is **the load-bearing fidelity contract**. It is the difference between Metis as a gateway and LiteLLM as a gateway. Every rule below corresponds to a bug we explicitly contract against.

### 4.1 OpenAI-shape inbound → canonical

Implemented in [`translators.py::parse_openai_request`](../../apps/gateway/src/metis_gateway/translators.py).

| OpenAI field | Canonical field | Notes |
|---|---|---|
| `messages[].role: "system"` | `system_prompt` (string) | Hoisted out of the message list per canonical-format §3.2. Multiple system messages are concatenated with `\n\n`. |
| `messages[].role: "user"` with `content: string` | `Message(role=USER, content=[TextBlock(...)])` | Trivial. |
| `messages[].role: "user"` with `content: list` (multi-part) | `Message(role=USER, content=[TextBlock or ImageBlock])` | Translate `type: "text"` → `TextBlock`, `type: "image_url"` → `ImageBlock` (URL or base64). |
| `messages[].role: "assistant"` | `Message(role=ASSISTANT, content=[...])` | If `tool_calls` present, generate one `ToolUseBlock` per call (see §4.3 for id mapping). |
| `messages[].role: "tool"` with `tool_call_id` and `content` | merged into next `Message(role=USER, content=[ToolResultBlock(...)])` per canonical-format §3.3 | OpenAI sends tool results as `role: "tool"`; canonical merges them into the next user turn as `ToolResultBlock`. Multiple consecutive `tool` messages collapse into one user message with multiple `ToolResultBlock`s. |
| `tools` | `list[ToolDefinition]` | `function.parameters` (JSON Schema) → `ToolDefinition.input_schema`. |
| `tool_choice` | `ToolChoice` (in `CanonicalRequest`) | `"auto"` / `"none"` / `{type: "function", function: {name}}` map to canonical equivalents. |
| `stream: true` | drives SSE outbound (§4.6) | The harness's `stream()` path is wired separately from `call()`. |
| `temperature`, `max_tokens`, `stop` | `temperature`, `max_output_tokens`, `stop_sequences` | Direct. |
| `response_format: {type: "json_schema", ...}` | `output_schema` | Provider support is gated by `AdapterCapabilities.supports_structured_output`. |
| `stream_options.include_usage` | `OpenAIInboundRequest.include_usage` | Threads through `render_openai_sse_stream` so the final chunk includes usage when requested. |

### 4.2 Anthropic-shape inbound → canonical

Implemented in [`endpoints/anthropic.py::parse_anthropic_request`](../../apps/gateway/src/metis_gateway/endpoints/anthropic.py). This is the simpler direction — canonical IR is Anthropic-shaped at heart.

| Anthropic field | Canonical field | Notes |
|---|---|---|
| `system` (string or list of blocks) | `system_prompt` (+ optionally `system_prompt_volatile`) | Block list flattened to string with `cache_control` markers preserved per canonical-format §4.1. The split between stable and volatile segments uses Anthropic's `cache_control` boundary if the client supplied one. |
| `messages[]` | `Message` (per role) | Direct. |
| `messages[].content[]` blocks: `text`, `image`, `tool_use`, `tool_result`, `thinking`, `redacted_thinking`, `document` (citations) | `TextBlock`, `ImageBlock`, `ToolUseBlock`, `ToolResultBlock`, `ThinkingBlock`, `RedactedThinkingBlock`, `DocumentBlock` | Lossless 1:1. The canonical IR was designed to be a superset of Anthropic's. |
| `tools[]` | `list[ToolDefinition]` | Direct (canonical IR uses Anthropic's tool shape). |
| `tool_choice` | `ToolChoice` | Direct. |
| `metadata.user_id` | dropped | Not persisted in v1. |
| `stream: true` | drives SSE outbound (§4.6) | SSE matches Anthropic's `event: ...` format. |

### 4.3 Tool-call id mapping (the LiteLLM #27469 hazard)

Tool-call ids differ across providers (OpenAI uses `call_...`, Anthropic uses `toolu_...`). When the inbound shape and the outbound provider disagree, the handler builds a per-request `ToolIdMap` ([`metis_core.adapters.tool_id_map`](../../packages/metis-core/src/metis_core/adapters/tool_id_map.py)) and round-trips ids through it.

The hazard LiteLLM #27469 documents — `tool_call.function.arguments` lost in OpenAI→Anthropic conversion — is the failure case the translator explicitly contracts against:

- `function.arguments` (which OpenAI emits as a string, possibly streamed in fragments) is treated as opaque JSON; parsed to dict and placed in `ToolUseBlock.input`. Parse failure → 400 `invalid_request_error`.
- Id-stability is **per-request**, not cross-request. The gateway is stateless across requests; if the client re-submits with a `tool_call_id` from a prior turn, the client owns the round-trip (which is fine: the client also stored the assistant's prior reply, so it sees the same id either side).

### 4.4 Prompt-caching `cache_control` placement (the LiteLLM #26625 hazard)

Anthropic supports `cache_control: {type: "ephemeral"}` markers on system blocks, message content blocks, and tools. Placement matters: misplaced markers turn into cache misses, which is the cost lever inverted. The canonical IR carries `cache_control` on `TextBlock`, `ToolDefinition`, and (per canonical-format §4.1) system-prompt blocks.

The Anthropic-inbound translator preserves `cache_control` markers verbatim because both sides speak the same shape. The OpenAI-inbound translator accepts `cache_control` only via `extra_body` (it's not in OpenAI's standard schema) and places markers exactly where the canonical IR puts them. When the chosen adapter does not support `cache_control` (per `AdapterCapabilities.supports_prompt_caching`), the marker is dropped silently — the client gave a hint, not a contract.

### 4.5 Thinking blocks across retries (the LiteLLM #27512 / #26916 hazards)

`ThinkingBlock` and `RedactedThinkingBlock` are first-class in the canonical IR. The retry layer in [`metis_core.adapters.retry`](../../packages/metis-core/src/metis_core/adapters/retry.py) preserves them across attempts. The gateway never collapses `thinking` to `text` (LiteLLM #26916). When the inbound shape is OpenAI, `thinking` content is dropped from the outbound JSON unless the client opted in via `extra_body.include_thinking: true`; never re-typed.

### 4.6 Streaming SSE serialization

Outbound SSE format depends on the inbound shape:

- **OpenAI inbound:** `data: {"id": ..., "choices": [{"delta": {...}, "index": 0}], "model": ..., "object": "chat.completion.chunk"}\n\n` followed by `data: [DONE]\n\n`. Implemented in [`translators.py::render_openai_sse_stream`](../../apps/gateway/src/metis_gateway/translators.py). The hard part is `tool_calls[].function.arguments` deltas — OpenAI streams the arguments JSON as raw text fragments; the translator emits each canonical `ToolUseInputDelta` as a fragment with `index` matching the position in the OpenAI `tool_calls` array.
- **Anthropic inbound:** native Anthropic SSE format (`event: message_start`, `event: content_block_start`, etc.). Implemented in [`endpoints/anthropic.py::render_sse_stream`](../../apps/gateway/src/metis_gateway/endpoints/anthropic.py).

The streaming path **primes the first event** before committing to a 200 response, so routing-time failures (`RoutingFailedError`, `ModelNotAllowedError`) surface as JSON error bodies rather than 200 SSE streams. After the first event, errors mid-stream terminate the response without further events (the client sees an early EOF).

### 4.7 Cancellation

Client disconnect (TCP RST / closed read side) triggers the harness to cancel the in-flight adapter call (per [`provider-adapter-contract.md §5.4`](provider-adapter-contract.md)). The disconnect probe races the adapter task; on disconnect, the adapter's `cancel()` is invoked and the harness raises `ClientDisconnected`. Partial token usage up to cancellation is still cost-stamped and traced. The HTTP handler returns a 499-style sentinel in case the socket is somehow still open.

---

## 5. Routing in the gateway path

### 5.1 Which routing slots apply

The 7-slot chain from [`routing-engine.md §4`](routing-engine.md) all run; not all are meaningful in stateless gateway calls.

| Slot | Meaningful in gateway? | Notes |
|---|---|---|
| `per_message_override` | Yes — and dominant in practice (§5.3). | Client's inbound `model` is interpreted as a per-message override. |
| `manual_sticky` | No | There is no "session" in the gateway path — each request is independent. Reports `not_applicable`. |
| `configured_rules` | Yes | The same `~/.metis/routing.yaml` policy that drives the agent applies here. Buyer rules like "no Opus for marketing key" live here. |
| `pattern_recommendation` | Future | Pattern store may observe gateway traffic; not yet writing recommendations into the gateway chain. |
| `delegate_request` | No | Delegation is an in-loop primitive. Reports `not_applicable`. |
| `workspace_default` | Yes | Resolved from `gateway_key.workspace_path` against the policy. |
| `global_default` | Yes | The deployment's catch-all. |

### 5.2 Routing per request

Each inbound request constructs a `TurnContext` with:

- `session_id = "gw_<ulid>"`, `turn_id = "gt_<ulid>"` (synthetic per-request ids so the trace events still join).
- `session_active_model = None` (no sticky state).
- `workspace_default_model = None` (the policy resolves it via `workspaces.{key.workspace_path}.default`).
- `global_default_model = <runtime config>`.
- `per_message_override = registry.resolve_alias(parsed.model)`.
- `policy = routing.policy` (the same policy the agent uses).

The chain runs to completion; one `route.decided` event is emitted per request as usual.

### 5.3 The inbound `model` field

OpenAI- and Anthropic-shape clients send `model: "<string>"` and expect a model identifier they recognize. The gateway treats it as a **per-message override** in three forms, in priority order:

1. **Metis alias** (preferred): `model: "metis://auto"`, `model: "metis://cheap"`, `model: "metis://opus"`. Resolved by `registry.resolve_alias`.
2. **Canonical `provider:name`**: `model: "anthropic:claude-opus-4-7"`. Identity-resolves.
3. **Bare provider name**: `model: "gpt-4o"` or `model: "claude-opus-4-5"`. Resolved if the registry has it as an alias; otherwise the override is treated as "the client's literal name, accepted as a hint" and the chain falls through.

**Real-world consequence.** Mainstream OpenAI / Anthropic SDKs always include `model` in the request body, so `route.decided.chain` reports `policy=per_message_override`, `verdict=chose` on **every** gateway request unless the client deliberately omits `model`. The `rule`, `pattern`, `workspace_default`, and `global_default` slots are unreachable in that mode. This is correct (the spec interprets `model` as a per-message override), but worth knowing when reading gateway traces.

**Open question — "transparent mode" override.** A future `--ignore-inbound-model` flag could ignore the inbound `model` field and let routing fall through to the rule / workspace / global slots, giving operators the cost-optimization magic-trick mode (client says "gpt-4o", policy says "haiku for short prompts on this key"). The recommendation is to leave the default as-is: per-message override is the documented contract per `gateway.md §5.3`, and clients that want to delegate model choice can already pass `model: "metis://auto"`. A flag is straightforward to add when a buyer specifically asks for it; treating it as opt-in keeps the default behavior predictable.

### 5.4 Capability validation

The existing capability gate in [`routing-engine.md §4.4`](routing-engine.md) applies unchanged. If the inbound request uses tools and the routed adapter has `supports_tools = false`, the chain falls through to the next candidate. Hard failure returns `503 routing_failed`.

### 5.5 Per-key allowed_models

After routing produces a `chosen_model`, the harness checks the key's `allowed_models` tuple (when set). If the chosen model is not allowed, the harness raises `ModelNotAllowedError`, which the HTTP handler translates to `403 invalid_request_error` (OpenAI) or `403 permission_error` (Anthropic). The check happens **after** routing rather than feeding into capability validation so the trace records what *would have* been routed, surfacing the rejection cleanly.

---

## 6. Events emitted (additive payload fields)

Gateway requests emit the same catalog events as agent calls:

- `route.decided` — exactly one per request.
- `llm.call_started`, `llm.call_completed`, `llm.call_failed` — one per provider call.
- `turn.completed` — one per request (a gateway request is one "turn" from a tracing perspective, even though there's no session).

Additive payload fields (no new event types, no breaking changes):

- `llm.call_completed.gateway_key_id: str | None` — typed field on `LLMCallCompleted` (`events/payloads.py`). `None` for agent-loop traffic; set for gateway calls.
- `llm.call_completed.inbound_shape: Literal["openai", "anthropic"] | None` — same.
- `llm.call_completed.user_id: str | None` — typed field; stable principal id resolved from the gateway key at request entry (see [`multi-user.md §4.4`](multi-user.md)). `None` for agent-loop traffic and pre-multi-user keys; rolls up under the null bucket in `/analytics/cost?group_by=user`.
- `llm.call_completed.team_id: str | None` — typed field; same null-bucket convention; drives `/analytics/by_team` (multi-user.md §5.2).
- `turn.completed.user_id` / `turn.completed.team_id` — typed fields on `TurnCompleted` (matching the `LLMCallCompleted` shape so analytics can roll up at either grain).
- `turn.completed.gateway_key_id` and `inbound_shape` — still stamped on the dict envelope at emit time (`harness.py::_emit_turn_completed`) until the typed extension on `TurnCompleted` lands for them too (§11 follow-on). The fields read identically by the analytics SQL.

These additive fields drive [`analytics-api.md`](analytics-api.md) §4.1 (`group_by=gateway_key`) and §4.8 (`/analytics/by_key`), and the new `group_by=user` / `group_by=team` / `/analytics/by_team` surfaces in [`multi-user.md §5`](multi-user.md). Existing consumers ignore unknown fields.

### 6.4 Quota events (multi-user.md §5)

Two additional event types fire from the gateway's per-request auth path when a key carries a `daily_cap_usd` or `monthly_cap_usd`. Both are pseudonymous (stable ids, no plaintext PII).

| Event type | When | Payload (new) |
|---|---|---|
| `quota.alert` | Spend on the key/user/team is in [80%, 95%) (`severity="warning"`) or [95%, 100%) (`severity="critical"`) of the configured cap. One event per (request, scope) — the same scope on a later request fires again, but the same scope twice in one request does not. | `scope`, `severity`, `current_usd`, `limit_usd`, `percentage`, `gateway_key_id?`, `user_id?`, `team_id?` |
| `gateway.quota_exceeded` | Spend has reached the cap (≥100%). Emitted alongside the 429 response described below — no quota.alert is also emitted in this case. | `scope`, `current_usd`, `limit_usd`, `inbound_shape`, `gateway_key_id?`, `user_id?`, `team_id?` |

`scope` is one of `key_daily`, `key_monthly`, `user_daily`, `user_monthly`, `team_daily`, `team_monthly` (multi-user.md §5.1) — v1 ships `key_daily` and `key_monthly` only; user/team scopes land when `users.json` / `teams.json` do.

When the hard cap fires, the HTTP layer returns **429** with the documented body shape:

```json
{
  "error": {
    "code": "quota_exceeded",
    "identity": "key",
    "scope": "key_daily",
    "limit_usd": "1.00",
    "current_usd": "1.50",
    "type": "rate_limit_error",
    "message": "key_daily cap of $1.00 hit ($1.50 spent)"
  }
}
```

The shape is the same for OpenAI and Anthropic inbound clients; only the trailing `type` discriminator (always `rate_limit_error` here) is wired for shape parity. The check runs **before** routing or adapter invocation, so a capped identity never burns provider-side cost on a rejected request.

Soft alerts and the hard breaker share the same SQL projection (`AnalyticsStore.cost(group_by=...)`-shaped query) running once per request through a per-request `RequestQuotaCache`. Daily windows reset at UTC midnight; monthly windows at UTC first-of-month.

---

## 7. Persistence

The gateway does **not** persist canonical messages by default. Per-request memory: routing context, adapter call, response, then discard. Trace events are persisted via the existing event bus → SQLite path; that's the only durable record.

Optional opt-in (deferred to v1.1): per-key request logging to the existing `messages` / `sessions` tables, gated by a `gateway_keys.log_messages` flag. The default is **off** because (a) buyers care about cost dashboards, not transcript replay, and (b) gateway traffic is often subject to data-residency constraints that the buyer must opt into explicitly.

---

## 8. Errors

Error class taxonomy aligns with [`provider-adapter-contract.md §6.1`](provider-adapter-contract.md). The gateway translates canonical error classes to inbound-shape error envelopes (see `app.py::_openai_error_from_adapter` and `_anthropic_error_from_adapter`):

| Canonical class | OpenAI-shape outbound | Anthropic-shape outbound |
|---|---|---|
| `auth` | 401 `invalid_request_error` / `invalid_api_key` | 401 `authentication_error` |
| `rate_limit` | 429 `rate_limit_error` / `rate_limit_exceeded` | 429 `rate_limit_error` |
| `context_overflow` | 400 `invalid_request_error` / `context_length_exceeded` | 400 `invalid_request_error` |
| `invalid_request` | 400 `invalid_request_error` | 400 `invalid_request_error` |
| `network` | 502 `api_error` | 502 `api_error` |
| `server_error` | 503 `api_error` | 503 `overloaded_error` |
| _other / classify_-internal_ | 500 `api_error` | 500 `api_error` |
| `RoutingFailedError` (no eligible model) | 503 `api_error` / `routing_failed` | 503 `overloaded_error` |
| `ModelNotAllowedError` (key's allowlist) | 403 `invalid_request_error` | 403 `permission_error` |
| `ClientDisconnected` (TCP RST) | (no body — connection closed) | (no body) |

---

## 9. Operations

- **Deploy:** `apps/gateway/` ships its own console-script entry (`metis gateway`, mounted via the unified `metis` console-script in `metis-cli`). One binary, single-process uvicorn. Docker image to follow.
- **Key issuance:** `metis gateway issue-key --name "<display>" --workspace "<path>" [--allow-models ...] [--daily-cap-usd ...]`. Prints the key once; only the SHA-256 hash is stored. Keystore file mode `0o600`.
- **Health:** `/healthz` returns 200 when uvicorn is up; the response does not imply downstream providers are reachable. Liveness vs. readiness split deferred.
- **Observability:** [`analytics-api.md`](analytics-api.md) is the dashboard. `group_by=gateway_key` on `/analytics/cost` and the dedicated `/analytics/by_key` endpoint (§4.8) split cost by key + inbound shape using the additive payload fields described in §6.

---

## 10. Non-goals for v1 (and what they imply about future specs)

These are *not* features of the v1 gateway and not part of this spec's contract:

1. **Multi-tenant isolation beyond per-key.** v1 assumes one deployment per organization. Multi-org tenancy is a separate design (RBAC, trace-store partitioning, dashboard filtering).
2. **Streaming response cancellation propagation from gateway to provider for partial output.** v1 cancels on client disconnect; whether the provider charges for in-flight tokens is the provider's call.
3. **Response caching** (semantic or exact). Different cost lever; different design (cache key, invalidation, TTL). LiteLLM and Portkey both ship this; defer until evidence it's load-bearing for our buyers.
4. **Prompt registry / templating** (the Portkey / Helicone "Prompt" surface). Out of scope; the gateway is dumb pipe + routing.
5. **Per-key rate limiting beyond the daily / monthly cap circuit breaker.** v1 ships hard breakers + soft alerts on `daily_cap_usd` / `monthly_cap_usd` (§6.4); request-rate limits and IP-bucket throttles are out of scope.
6. **Non-loopback bind.** Production deployment behind a TLS terminator is gated behind future hardening (auth/rate limiting/audit). See §3.2.

---

## 11. Follow-ons (next-up after v1)

1. **Multi-user / team-level rollups.** v1 stamps `gateway_key_id` per call; teams of keys, multi-workspace per key, and tenant aggregation are Phase 3 follow-on. Requires both a keystore schema change (group/team membership) and an analytics rollup dimension (`group_by=team` or a `team_id` filter).
2. **Production-bind hardening.** Audit logging (who called what when), per-key rate limiting, and CIDR allowlists are the gating items before the gateway can default to a non-loopback bind.
3. **`--ignore-inbound-model` flag** (§5.3 open question). Lets routing fall through to rule / workspace / global slots even when the client's `model` is set, opting into transparent cost-optimization mode.
4. **`/v1/models` listing.** OpenAI clients expect this surface; deferred until a Cursor or Continue user asks.
5. **Typed `gateway_key_id` / `inbound_shape` on `TurnCompleted`.** Currently dict-envelope-stamped; promoting them to typed fields keeps the catalog discipline (`event-bus-and-trace-catalog.md`) honest.

---

## 12. References

- [`canonical-message-format.md`](canonical-message-format.md) — `Message`, `ContentBlock`, persistence schema.
- [`provider-adapter-contract.md`](provider-adapter-contract.md) — adapter interface, retry, error classes.
- [`routing-engine.md`](routing-engine.md) — the 7-slot chain consumed in §5.
- [`event-bus-and-trace-catalog.md`](event-bus-and-trace-catalog.md) — additive payload fields on `LLMCallCompleted`.
- [`analytics-api.md §4.1, §4.8`](analytics-api.md) — `group_by=gateway_key` on `/analytics/cost` and the `/analytics/by_key` rollup.
- [`deployment-shape.md`](deployment-shape.md) — the gateway/agent/hybrid framing this spec is paired with.
- [`apps/gateway/src/metis_gateway/`](../../apps/gateway/src/metis_gateway/) — implementation.
