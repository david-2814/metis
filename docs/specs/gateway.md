# Gateway Specification (Skeleton)

**Status:** Draft v0 — skeleton, paired with [`deployment-shape.md`](deployment-shape.md). Endpoints and translation rules listed; field-level schema and error codes deferred until the deployment-shape recommendation is signed off.
**Last updated:** 2026-05-13

> The HTTP gateway is the transparent-proxy surface from [`deployment-shape.md §1`](deployment-shape.md). It accepts OpenAI-shape (and optionally Anthropic-shape) requests from external agent clients (Claude Code, Cursor, Codex, Continue, custom apps), routes via the existing engine, calls the existing adapters, and returns provider-shape responses — losslessly preserving Anthropic-native blocks where possible.
>
> This spec depends on:
>
> - [`canonical-message-format.md`](canonical-message-format.md) — `Message`, `ContentBlock` variants, `ToolDefinition`.
> - [`provider-adapter-contract.md`](provider-adapter-contract.md) — `CanonicalRequest`, `CanonicalResponse`, streaming events, error classes.
> - [`routing-engine.md`](routing-engine.md) — the 7-slot chain. The gateway path exercises primarily `configured_rules`, `workspace_default`, and `global_default`.
> - [`server-api.md`](server-api.md) — the gateway is a *sibling* HTTP app, not an extension of `metis serve`. Loopback-only does not apply (see §3.2).
> - [`event-bus-and-trace-catalog.md`](event-bus-and-trace-catalog.md) — gateway calls emit the same `llm.call_*` and `route.decided` events as agent calls.

---

## 1. Purpose

Give the buyer a way to get Metis's model-selection + cost-attribution + lossless-canonical-IR value without making their devs switch tools. Devs keep using whatever client they already use (typically Claude Code or Cursor); operations flips one env var (`OPENAI_BASE_URL` / `ANTHROPIC_BASE_URL`) and adds a Metis-issued API key. Every LLM call now flows through Metis, gets routed, gets cost-attributed, and writes a trace.

The gateway is a per-request stateless harness: routing decision + adapter call + response translation + cost stamping. **The agent loop stays in the client.** Multi-turn tool use happens through the client re-submitting follow-up requests with `tool_result` blocks; the gateway is stateless across requests within the same agent loop.

---

## 2. Scope

### 2.1 In scope

1. **OpenAI-shape inbound** (`POST /v1/chat/completions`, sync + SSE streaming). The universal contract.
2. **Anthropic-shape inbound** (`POST /v1/messages`, sync + SSE streaming) — closer to canonical IR; smaller translation gap; required to keep Claude Code clients native.
3. **Provider-native outbound** via the existing adapter set (Anthropic, OpenAI, OpenRouter). Adapters are unchanged.
4. **Routing per request** via the existing `RoutingEngine`. Override hints (model alias, provider hint) accepted in the inbound request body (see §5.3).
5. **Cost attribution per API key.** Each inbound request authenticates a gateway key; `llm.call_completed` events are stamped with `gateway_key_id` so [`analytics-api.md`](analytics-api.md) can roll up cost by team/key.
6. **Trace events.** Same catalog as the agent path. Gateway-specific dimensions (`gateway_key_id`, inbound shape, requested model alias) are added as optional payload fields, not new event types.
7. **Lossless block round-trip** for the cases the existing canonical IR already handles: Anthropic `thinking`, `cache_control`, `tool_use`, `tool_result`, citations.

### 2.2 Out of scope (explicit non-features)

These are the bright lines that separate the gateway from the replacement agent. The gateway must not assume them, and must not be extended to add them — those features belong in the agent.

1. **No context shaping.** The gateway forwards the prompt envelope as the client supplied it. It does not inject system instructions, trim history, or compress turns. The client's agent loop owns context.
2. **No skill loading.** Skills are an in-loop concept (description-match → activation → script execution). A stateless gateway cannot meaningfully load a skill into someone else's agent; attempting to inject SKILL.md content would corrupt the client's prompt cache and break the client's tool contract.
3. **No memory composition.** `MEMORY.md` / `USER.md` are agent-loop artifacts. The gateway is workspace-agnostic; there is no `.metis/` directory tied to a request.
4. **No tool execution.** The gateway never runs a tool. It passes `tool_use` blocks back to the client (which executes the tool); the client re-submits a follow-up request with the `tool_result`.
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
| `/v1/models` | GET | OpenAI | Lists models routable from this gateway key (intersection of registry + per-key allowlist). |
| `/healthz` | GET | — | Liveness; no auth. |

Deferred to v1.1+: `/v1/embeddings`, `/v1/completions` (legacy), tool-confirmation REST surface (depends on [`server-api.md`](server-api.md) progress), batch APIs, file uploads.

### 3.2 Network posture

Unlike `metis serve` ([`server-api.md §3.1`](server-api.md)), the gateway is **not** loopback-only. Its job is to sit in front of provider API keys for an organization. Default bind: `0.0.0.0:8422`. Operators are expected to put it behind their own TLS terminator (reverse proxy or load balancer) and restrict ingress to their own network. The gateway speaks HTTP/1.1 + HTTP/2 (uvicorn defaults); TLS is not v1's responsibility.

This is a different security posture from `metis serve` and is one reason the gateway lives in `apps/gateway/` rather than as an extension of `apps/server/`.

### 3.3 Authentication

Each inbound request carries an `Authorization: Bearer <gateway_key>` header (OpenAI clients) or `x-api-key: <gateway_key>` (Anthropic clients). Keys are issued by the operator out-of-band (CLI subcommand `metis gateway issue-key --name ...`) and stored in a new `gateway_keys` SQLite table. A key carries: id, hashed secret, display name, created-at, optional model allowlist, optional daily-spend cap. No auth ⇒ 401.

The `gateway_key_id` is recorded on every outbound `llm.call_completed` event so analytics can roll up by key. No PII flows into the key record.

### 3.4 Compatibility with existing clients

| Client | Shape | Action |
|---|---|---|
| OpenAI SDK (`openai-python`, `openai-node`) | OpenAI | Set `base_url` / `OPENAI_BASE_URL` and `OPENAI_API_KEY` to the gateway. |
| Anthropic SDK | Anthropic | Set `base_url` / `ANTHROPIC_BASE_URL` and the `x-api-key` header to the gateway key. |
| Claude Code | Anthropic | Same as Anthropic SDK. The buyer flips `ANTHROPIC_BASE_URL` org-wide. |
| Cursor (closed) | OpenAI-compat or Anthropic-compat | Configurable in Cursor settings; flip the API base. |
| OpenCode / Cline / Continue / Goose | OpenAI-compat or per-provider | Configurable; flip the API base. |
| Custom internal app | Either | Same. |

The compatibility test for v1 is "Claude Code → gateway → Anthropic API end-to-end, including tool use, thinking blocks across retries, and prompt caching." That's the workload that proves the lossless-IR claim is real, not aspirational.

---

## 4. Request translation rules

This section is **the load-bearing fidelity contract**. It is the difference between Metis as a gateway and LiteLLM as a gateway. Every rule below corresponds to a bug we want to *not* have.

### 4.1 OpenAI-shape inbound → canonical

| OpenAI field | Canonical field | Notes |
|---|---|---|
| `messages[].role: "system"` | `system_prompt` (string) | Hoisted out of the message list per canonical-format §3.2. Multiple system messages are concatenated with `\n\n`. |
| `messages[].role: "user"` with `content: string` | `Message(role=USER, content=[TextBlock(...)])` | Trivial. |
| `messages[].role: "user"` with `content: list` (multi-part) | `Message(role=USER, content=[TextBlock or ImageBlock])` | Translate `type: "text"` → `TextBlock`, `type: "image_url"` → `ImageBlock` (URL or base64). |
| `messages[].role: "assistant"` | `Message(role=ASSISTANT, content=[...])` | If `tool_calls` present, generate one `ToolUseBlock` per call (see §4.3 for id mapping). |
| `messages[].role: "tool"` with `tool_call_id` and `content` | merged into next `Message(role=USER, content=[ToolResultBlock(...)])` per canonical-format §3.3 | OpenAI sends tool results as `role: "tool"`; canonical merges them into the next user turn as `ToolResultBlock`. Multiple `tool` messages in a row collapse into one user message with multiple `ToolResultBlock`s. |
| `tools` | `list[ToolDefinition]` | Map `function.parameters` (JSON Schema) to canonical `ToolDefinition.input_schema`. |
| `tool_choice` | `ToolChoice` (in `CanonicalRequest`) | `"auto"` / `"none"` / `{type: "function", function: {name}}` → canonical equivalents. |
| `stream: true` | `CanonicalRequest.stream = True` | Drive SSE outbound (§5). |
| `temperature`, `max_tokens`, `stop` | `CanonicalRequest.temperature`, `max_output_tokens`, `stop_sequences` | Direct. |
| `response_format: {type: "json_schema", ...}` | `CanonicalRequest.output_schema` | Provider support is gated by `AdapterCapabilities.supports_structured_output`. |
| Provider-specific extensions in `extra_body` | per §4.4 | Anthropic-native blocks come in via `extra_body` (e.g. OpenRouter's convention) and need careful round-tripping. |

### 4.2 Anthropic-shape inbound → canonical

This is the simpler direction — canonical IR is Anthropic-shaped at heart. The translator is mostly identity:

| Anthropic field | Canonical field | Notes |
|---|---|---|
| `system` (string or list of blocks) | `system_prompt` | Block list flattened to string with `cache_control` markers preserved per canonical-format §4.1. |
| `messages[]` | `Message` (per role) | Direct. |
| `messages[].content[]` blocks: `text`, `image`, `tool_use`, `tool_result`, `thinking`, `redacted_thinking`, `document` (citations) | `TextBlock`, `ImageBlock`, `ToolUseBlock`, `ToolResultBlock`, `ThinkingBlock`, `RedactedThinkingBlock`, `DocumentBlock` | Lossless 1:1. The canonical IR was designed to be a superset of Anthropic's. |
| `tools[]` | `list[ToolDefinition]` | Direct (canonical IR uses Anthropic's tool shape). |
| `tool_choice` | `ToolChoice` | Direct. |
| `metadata.user_id` | passthrough (not stored without consent) | Drop or hash; do not persist plaintext. |
| `stream: true` | `CanonicalRequest.stream = True` | SSE matches Anthropic's `event: ...` format outbound. |

### 4.3 Tool-call id mapping (the LiteLLM #27469 hazard)

Tool-call ids differ across providers (OpenAI uses `call_...`, Anthropic uses `toolu_...`). When the inbound shape and the outbound provider disagree, the gateway uses the existing `ToolIdMap` from [`packages/metis-core/src/metis_core/adapters/tool_id_map.py`](../../packages/metis-core/src/metis_core/adapters/tool_id_map.py) to maintain a per-request bidirectional map.

The hazard LiteLLM #27469 documents — `tool_call.function.arguments` lost in OpenAI→Anthropic conversion — is the failure case we explicitly contract against. The translator MUST:

- Treat `function.arguments` (which OpenAI emits as a string, possibly streamed in fragments) as opaque JSON; parse to dict and place in `ToolUseBlock.input`. Never drop the field, even if parsing fails — emit a `block_dropped` log and surface a 400 with the parse error.
- Preserve id-stability across multi-turn: if the client sent a `tool_call_id: call_abc123` in a prior turn's `role: "tool"` message and the gateway routed that turn to Anthropic (where the tool_use id was `toolu_xyz`), the next turn from the client may reference `call_abc123`; the gateway looks up `toolu_xyz` in the per-request `ToolIdMap` snapshot and translates back. This works only if the *same* gateway key sends the follow-up (no cross-key state).

### 4.4 Prompt-caching `cache_control` placement (the LiteLLM #26625 hazard)

Anthropic supports `cache_control: {type: "ephemeral"}` markers on system blocks, message content blocks, and tools. Placement matters: misplaced markers turn into cache misses, which is the cost lever inverted. The canonical IR carries `cache_control` on `TextBlock`, `ToolDefinition`, and (per canonical-format §4.1) system-prompt blocks. When the inbound shape is OpenAI and the outbound provider is Anthropic / Bedrock / Vertex, the translator MUST:

- Accept `cache_control` only via `extra_body` on the OpenAI side (it's not in OpenAI's standard schema) — and document this in the gateway README.
- Place `cache_control` exactly where canonical IR puts it: on the block boundary the client requested, never on a wrapper block the gateway introduced for serialization reasons.
- When the chosen adapter does not support `cache_control` (per `AdapterCapabilities.supports_prompt_caching`), drop the marker silently. Do not error; the client gave a hint, not a contract.

### 4.5 Thinking blocks across retries (the LiteLLM #27512 hazard)

`ThinkingBlock` and `RedactedThinkingBlock` are first-class in the canonical IR. The retry layer in `packages/metis-core/src/metis_core/adapters/retry.py` already preserves them. The gateway's only new responsibility: when the inbound shape is OpenAI (which has no native thinking-block type), translate Anthropic `thinking` blocks in the *outbound* OpenAI-shape response to OpenAI's `reasoning` field (where the OpenAI SDK is configured to surface it) or drop with a log if the client did not opt in via `extra_body.include_thinking: true`. Never collapse `thinking` to `text` (the LiteLLM #26916 hazard).

### 4.6 Streaming SSE serialization

Outbound SSE format depends on the inbound shape:

- **OpenAI inbound:** `data: {"id": ..., "choices": [{"delta": {...}, "index": 0}], "model": ..., "object": "chat.completion.chunk"}\n\n` followed by `data: [DONE]\n\n`. The hard part is `tool_calls[].function.arguments` deltas — OpenAI streams the arguments JSON as raw text fragments. The translator MUST emit each canonical `ToolUseBlockDelta` as a fragment with `index` matching the position in the OpenAI `tool_calls` array.
- **Anthropic inbound:** native Anthropic SSE format (`event: message_start`, `event: content_block_start`, etc.) per the existing `adapters/anthropic.py` outbound. Smaller translation gap.

### 4.7 Cancellation

Client disconnect (TCP RST / closed read side) triggers the gateway to cancel the in-flight adapter call (per [`provider-adapter-contract.md §5.4`](provider-adapter-contract.md)). The adapter MUST honor cancellation within the contract's bounded time. Partial token usage up to cancellation is still cost-stamped and traced.

---

## 5. Routing in the gateway path

### 5.1 Which routing slots apply

The 7-slot chain from [`routing-engine.md §4`](routing-engine.md) all exist; not all of them are meaningful in stateless gateway calls.

| Slot | Meaningful in gateway? | Notes |
|---|---|---|
| `per_message_override` | Yes | Client may pass `model: "metis://opus"` or a routing hint in `extra_body` (see §5.3). |
| `manual_sticky` | No | There is no "session" in the gateway path — each request is independent. The slot reports `not_applicable`. |
| `configured_rules` | Yes (Phase 2) | The same YAML policy that drives the agent applies here. Buyer rules like "no Opus for marketing key" live here. |
| `pattern_recommendation` | Future | Pattern store may observe gateway traffic; not yet writing recommendations. |
| `delegate_request` | No | Delegation is an in-loop primitive. The gateway never delegates. |
| `workspace_default` | Per-key (Phase 2) | Reinterpreted as "per-gateway-key default" — each `gateway_key` has an optional default model. |
| `global_default` | Yes | The deployment's catch-all. |

### 5.2 Routing per request

Each inbound request constructs a `TurnContext` with:

- `session_active_model = None` (no sticky state).
- `workspace_default_model = gateway_key.default_model`.
- `global_default_model = deployment.global_default`.
- `per_message_override` derived from the inbound `model` field (see §5.3 for how `model` is interpreted).
- `policy = deployment_routing_policy` (YAML-driven; the same policy the agent uses).

The chain runs to completion; one `route.decided` event is emitted per request as usual.

### 5.3 The inbound `model` field

OpenAI-shape clients send `model: "<string>"` and expect a model identifier they recognize. Three interpretations, in priority order:

1. **Metis alias** (preferred): `model: "metis://auto"`, `model: "metis://cheap"`, `model: "metis://opus"`. The gateway resolves these via the routing engine. `metis://auto` runs the full chain; the explicit aliases bias the chain.
2. **Canonical provider:name**: `model: "anthropic:claude-opus-4-7"`. Bypasses routing slots 1–5 and goes straight to that adapter. Used for clients that already know what they want.
3. **Bare provider name**: `model: "gpt-4o"` or `model: "claude-opus-4-5"`. Treated as a hint the client thinks it's that model; the gateway re-routes per policy. The dashboard records "requested: gpt-4o → routed: ...".

Interpretation (3) is the magic-trick mode: the client says "give me gpt-4o," the policy says "for this key, route to haiku instead because the prompt is small," and the response is shaped as the client expects (OpenAI-shape outbound from a Haiku call). This is the cost-optimization headline.

### 5.4 Capability validation

The existing capability gate in [`routing-engine.md §4.4`](routing-engine.md) applies unchanged. If the inbound request uses tools and the routed adapter has `supports_tools = false`, the chain falls through to the next candidate. Hard failure returns `503 routing_failed` per [`server-api.md §4.2`](server-api.md).

---

## 6. Events emitted (no new event types)

Gateway requests emit the same catalog events as agent calls:

- `route.decided` — exactly one per request.
- `llm.call_started`, `llm.call_completed`, `llm.call_failed` — one per provider call.
- `turn.completed` — one per request (a gateway request is one "turn" from a tracing perspective, even though there's no session).

Additive payload fields (no new event types, no breaking changes):

- `llm.call_completed.gateway_key_id: str | None` — the key that authenticated this call, if any.
- `llm.call_completed.inbound_shape: Literal["openai", "anthropic"] | None` — the inbound translator that produced this call.
- `turn.completed.gateway_key_id: str | None` — same.

These are additive per the spec-change discipline in [`CHANGES.md`](CHANGES.md). Existing consumers ignore unknown fields.

---

## 7. Persistence

The gateway does **not** persist canonical messages by default. Per-request memory: routing context, adapter call, response, then discard. Trace events are persisted via the existing event bus → SQLite path; that's the only durable record.

Optional opt-in (deferred to v1.1): per-key request logging to the existing `messages` / `sessions` tables, gated by a `gateway_keys.log_messages` flag. The default is **off** because (a) buyers care about cost dashboards, not transcript replay, and (b) gateway traffic is often subject to data-residency constraints that the buyer must opt into explicitly.

---

## 8. Errors

Error class taxonomy aligns with [`provider-adapter-contract.md §6.1`](provider-adapter-contract.md). The gateway translates canonical error classes to inbound-shape error envelopes:

| Canonical class | OpenAI-shape outbound | Anthropic-shape outbound |
|---|---|---|
| `auth_failed` | 401 `invalid_api_key` | 401 `authentication_error` |
| `rate_limited` | 429 `rate_limit_exceeded` | 429 `rate_limit_error` |
| `model_unavailable` | 503 (with `Retry-After`) | 503 `overloaded_error` |
| `bad_request` (client-side translation failure) | 400 `invalid_request_error` | 400 `invalid_request_error` |
| `routing_failed` (no eligible model) | 503 `routing_failed` with chain trace | 503 `routing_failed` |
| `internal_error` | 500 | 500 |
| `cancelled` (client disconnect) | (no body — connection closed) | (no body) |
| `timeout` | 504 | 504 |

`routing_failed` is the new one; its body shape matches [`server-api.md §4.2`](server-api.md).

---

## 9. Operations

- **Deploy:** `apps/gateway/` ships its own console-script (`metis-gateway`). One binary, single-process uvicorn. Docker image to follow.
- **Key issuance:** CLI subcommand `metis gateway issue-key --name "<display>" [--allow-models ...] [--daily-cap-usd ...]`. Prints the key once; only the hash is stored.
- **Health:** `/healthz` returns 200 when uvicorn is up; `200` does not imply downstream providers are reachable. Liveness vs. readiness split deferred.
- **Observability:** the existing analytics API (per [`analytics-api.md`](analytics-api.md)) is the dashboard. Add a `group_by=gateway_key` dimension to `/analytics/cost` in a follow-up spec change.

---

## 10. Non-goals for v1 (and what they imply about future specs)

These are *not* features of the v1 gateway and not part of this spec's contract:

1. **Multi-tenant isolation beyond per-key.** v1 assumes one deployment per organization. Multi-org tenancy is a separate design (RBAC, trace-store partitioning, dashboard filtering).
2. **Streaming response cancellation propagation from gateway to provider for partial output.** v1 cancels on client disconnect; whether the provider charges for in-flight tokens is the provider's call.
3. **Response caching** (semantic or exact). Different cost lever; different design (cache key, invalidation, TTL). LiteLLM and Portkey both ship this; defer until evidence it's load-bearing for our buyers.
4. **Prompt registry / templating** (the Portkey / Helicone "Prompt" surface). Out of scope; the gateway is dumb pipe + routing.
5. **Per-key rate limiting beyond a daily-cap circuit breaker.** Add when a buyer needs it; the existing pricing pipeline can drive a soft cap without new infrastructure.

---

## 11. Open questions

1. **Inbound surface for v1.** OpenAI-shape only (smaller MVP), or OpenAI + Anthropic together (bigger MVP, but the surface every Claude Code buyer needs)? See [`deployment-shape.md §8.2`](deployment-shape.md).
2. **Where does the gateway run.** Sibling app vs. extension of `metis serve`. Recommendation: sibling. Confirm with owner.
3. **Should `gateway_keys` live in the same SQLite DB as sessions/events**, or a separate ops DB? Same DB is simpler; separate DB is safer for backup/restore. Defer until ops requirements are concrete.
4. **What's the answer to "I want my Claude Code to use Metis but I can't change `ANTHROPIC_BASE_URL` globally"?** Probably a per-project shim or a wrapper script. Not in v1 scope; flag for the docs.

---

## 12. Sequencing

This spec is paired with [`deployment-shape.md`](deployment-shape.md). It is only drafted skeleton-level until that recommendation is signed off. Once signed off, the next passes are:

- v0.1: tighten the translation tables (§4) with field-by-field schemas and edge cases.
- v0.2: design the `gateway_keys` table and the issuance CLI.
- v0.3: write the cross-spec impact: which event payloads need additive fields, which analytics endpoints need a `gateway_key` dimension.
- v1.0: green-light implementation; first PR is the OpenAI-shape translator.
