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

The gateway defaults to loopback bind (`127.0.0.1`). Post-Wave-13 this
is a **default, not a constraint**: the operator opts into a public
bind explicitly via `--host 0.0.0.0` once the hardening Wave 13 + Wave
11 layered on top of v1 is wired ([`gateway-hardening.md §2.1`](gateway-hardening.md)):

| Hardening layer | Status | Owner |
|---|---|---|
| Per-key + per-IP rate limiting | Shipped (Wave 11, opt-in via `RateLimitConfig.enabled=True`) | In-process |
| Audit-log export | Shipped (Wave 12, `metis audit export`) | In-process |
| Connection-rate cap | Shipped (Wave 13, default 1000 concurrent) | In-process |
| TLS termination | Shipped (Wave 13, `--tls-cert/--tls-key` for in-process; sidecar still recommended) | In-process or sidecar |
| Key rotation / revocation | Shipped (Wave 10, `metis gateway rotate-key` / `revoke-key`) | In-process |
| WAF / volumetric DDoS | **Buyer-owned** (edge CDN / cloud LB) | Upstream |

Pre-Wave-13, `run_gateway()` silently rewrote any non-loopback bind to
`127.0.0.1` because per-key rate limits and audit logging hadn't shipped
yet. Both have since landed; the rewrite is removed. The operator
opts in deliberately and gets a one-time `WARN` log line at boot
summarizing whether in-process TLS / rate-limit middleware are on so
the perimeter checklist stays honest.

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

### 4.8 Model normalization (the bare-name pitfall)

SDK clients speak the bare model names their upstream provider expects — the Anthropic SDK sends `claude-3-5-haiku-20241022` and rejects any `anthropic:` prefix because the real Anthropic API doesn't take one; the OpenAI SDK sends `gpt-4o-mini` and rejects an `openai:` prefix for the same reason. Metis's internal model id is always `provider:name` so the routing engine, the price table, and the registry all agree on a single key. Without an explicit bridge between the two namespaces, the gateway's `per_message_override` slot can't resolve the bare client name → routing falls through to `global_default` → cost is billed under whichever model that points at. The GA-readiness audit (§2.4) caught this on the canonical `claude-haiku-4-5` workload: every call was routed and priced as `anthropic:claude-sonnet-4-6`, over-reporting cost ~6×.

The fix lives in [`harness.py::_normalize_inbound_model`](../../apps/gateway/src/metis_gateway/harness.py) and runs once per request, immediately before `registry.resolve_alias`. Rules (first match wins):

1. **Registry already knows the name** — Metis aliases (`haiku`, `sonnet`, etc.) and canonical ids (`anthropic:claude-haiku-4-5`) pass through unchanged. This preserves the alias path the agent loop and the CLI already use.
2. **`metis://` opt-out** — `metis://auto`, `metis://cheap`, `metis://opus` are the documented "let routing decide" form and MUST NOT be prefixed.
3. **Already `provider:name`** — any string containing `:` (other than the `metis://` form) passes through; if the registry doesn't know it, the routing chain falls through as documented in §5.3.
4. **Bare name** — no `:`, not `metis://`: prepend the inbound shape's provider prefix. The current shape→prefix map is `{"openai": "openai", "anthropic": "anthropic"}`; other shapes pass through unchanged so the chain falls through cleanly.

The normalization is **registry-aware on the alias step but otherwise unconditional**: a bare name unknown to the registry still ends up prefixed so the routing trace records the buyer's intent (and the price table can look up the canonical key) rather than stamping a bare string no analytics surface recognizes.

What does NOT change:
- The outbound JSON body still echoes the client's original `model` string verbatim. SDKs that compare the echo against what they sent (some clients do) continue to work.
- The translator modules ([`translators.py`](../../apps/gateway/src/metis_gateway/translators.py) for OpenAI, [`endpoints/anthropic.py`](../../apps/gateway/src/metis_gateway/endpoints/anthropic.py) for Anthropic) remain pure data parsers — they don't depend on the registry. Normalization happens at the harness boundary because that's where both the inbound shape and the registry are in scope.
- `PriceTable.compute_cost` is unchanged — it's a strict canonical-id lookup. The pre-existing `UnknownPricingModelError` still surfaces if a buyer points at a model that's registered for routing but missing from the price table.

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
3. **Bare provider name**: `model: "gpt-4o"` or `model: "claude-opus-4-5"`. Resolved if the registry has it as an alias; otherwise normalized to canonical form (`<inbound_shape>:<name>`) per §4.8 — if the registry knows that canonical id, slot 1 wins on the prefixed form; if not, the chain falls through.

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

## 11. Key lifecycle (Wave 10)

v1.0 issued keys but had no online revocation or rotation — the
operator's only options were "delete the JSON entry and restart" or
"leave a leaked key alive." Wave 10 lands three online operations on
top of the existing keystore. All three are atomic writes
(write-temp-then-rename), all three emit pseudonymous audit events,
and all three are loopback-only CLI operations — there is no
HTTP-level admin surface (operators reach the keystore through the
same shell that ran `issue-key`).

### 11.1 Keystore fields

`GatewayKey` gains three lifecycle fields on top of the v1 record:

| Field | Type | Notes |
|---|---|---|
| `status` | `Literal["active", "revoked"]` (default `"active"`) | Missing on pre-Wave-10 records — the loader fills `"active"`. A revoked key is still loaded into the keystore so auth can return the documented `key_revoked` body with the stable `key_id`. |
| `revoked_at` | `datetime \| None` | Set when `status="revoked"`. UTC, ISO-8601 in the JSON file. Required when `status="revoked"` — the loader rejects a revoked record without a timestamp. |
| `grace_period_until` | `datetime \| None` | Set by `rotate-key` on the predecessor. While the key is `active` and `now < grace_period_until`, auth accepts it; past that boundary `is_active` returns False (auth read-only — the next admin sweep persists the transition). |

A `created_at` ISO-8601 timestamp is now stamped on every new record so
`list-keys` can sort + display issuance order. Pre-Wave-10 records read
back with `created_at=None`, which `list-keys` renders as `-`.

### 11.2 `metis gateway revoke-key <key_id>`

Marks the key revoked. Idempotent against an already-revoked key
(returns the existing `revoked_at`; emits no second audit event).
Subsequent gateway requests authenticating with that key return:

```http
HTTP/1.1 401 Unauthorized
Content-Type: application/json

{
  "error": {
    "code": "key_revoked",
    "key_id": "gk_01HXYZ...",
    "revoked_at": "2026-05-15T14:22:10+00:00",
    "type": "invalid_request_error"  | "authentication_error",
    "message": "gateway key gk_01HXYZ... has been revoked"
  }
}
```

`type` carries the shape-specific discriminator (OpenAI vs Anthropic)
so each SDK's error parser still recognizes the envelope; the body is
otherwise identical between inbound shapes.

### 11.3 `metis gateway rotate-key <key_id> [--grace-period <duration>]`

Mints a successor key that inherits the predecessor's metadata
(`workspace_path`, `user_id`, `team_id`, `allowed_models`,
`daily_cap_usd`, `monthly_cap_usd`) and stamps `grace_period_until` on
the predecessor. Default grace period: 24 hours.

During the grace window, both predecessor and successor authenticate;
`llm.call_completed` / `turn.completed` events stamp the
`gateway_key_id` actually used so operators see the migration land in
`/analytics/by_key`. Past the boundary, the predecessor reads as
revoked at auth time (`is_active(now=...)` returns False) and the next
admin sweep — any subsequent admin op against the keystore, or an
explicit `sweep_expired_grace_periods()` call — persists the
`active → revoked` transition and emits a paired `gateway.key_revoked`
with `reason="grace_period_expired"`.

The new plaintext token is printed once and is recoverable only from
the client team's secrets broker after that — the keystore only
persists the SHA-256 hash, same as `issue-key`.

`--grace-period` accepts forms like `30m`, `24h`, `7d`, `2w`. Zero or
negative durations are rejected (use `revoke-key` for an immediate
cutoff with no successor).

### 11.4 `metis gateway list-keys [--format text|json]`

Returns every key in the keystore — including revoked ones — with
status, identity, caps, and timestamps. `status` is the on-disk value;
`effective_status` applies the same `is_active` rule the auth path
uses, so an active key whose grace window has lapsed reads as
`revoked` even before the next sweep persists the transition.

The JSON output is the keystore admin contract for buyer tooling
(SOX-style "list every credential and when it was issued / revoked");
the text output is the terminal-friendly summary. Both are
non-mutating — `list-keys` never writes to the keystore.

### 11.5 Audit events

Three new event types in the catalog (`pseudonymous` floor; see
`event-bus-and-trace-catalog.md §6.13`):

- `gateway.key_issued` — emitted by `metis gateway issue-key` after the
  keystore write succeeds. Carries the resolved `(gateway_key_id, name,
  workspace_path, user_id, team_id, allowed_models, daily_cap_usd,
  monthly_cap_usd, issued_at)` so dashboards can correlate cost rows
  back to issuance.
- `gateway.key_revoked` — emitted on explicit `revoke-key` (with
  `reason="admin_revoke"`) or on grace-period sweep
  (`reason="grace_period_expired"`). The `reason` enum is the third
  value `"rotated"` (reserved for a future "fail-fast revoke on
  rotate" variant; not emitted in v1).
- `gateway.key_rotated` — emitted by `rotate-key`. Carries both
  `old_gateway_key_id` and `new_gateway_key_id` so the trace traces the
  migration; also stamps the inherited `workspace_path`, `user_id`,
  and `team_id` for the dashboard's per-identity rollup.

Audit emission is best-effort — failures don't abort the keystore
mutation. The keystore file is the source of truth; the audit event
is a follow-on for operators.

### 11.6 Non-goals (still)

1. **HTTP admin surface.** All key-lifecycle ops are CLI-only — there
   is no `POST /admin/keys/revoke` endpoint. Adding one requires the
   production-bind hardening (auth/rate-limiting/audit) listed in §12
   below.
2. **Per-key TTL.** No automatic expiration based on age. Operators
   that want scheduled rotation run `rotate-key` from cron.
3. **Soft-delete history.** `revoke-key` overwrites the in-memory key
   to `status="revoked"`; if you need a longer audit trail than the
   trace-DB events provide, snapshot the keystore before each ops
   action.

---

## 12. Self-serve signup (Wave 14)

Pre-Wave-14 the only way for a buyer to get a gateway key was for an
operator to run `metis gateway issue-key` on the host. That works for
in-VPC deployments where buyer-owned operators provision their own
accounts, but it's friction for a SaaS-style on-ramp where the buyer
arrives at a hosted gateway, wants to create an account themselves,
get a key, and point their client at it — without filing a ticket.

Wave 14 adds a thin self-serve surface on top of the existing keystore.
Accounts are a Metis-issued record (no SSO in v1; SSO remains the v2
upgrade from [`multi-user.md §8.1`](multi-user.md)); the first key on
an account is issued the moment the account verifies its email. The
account session token is then enough to manage subsequent keys via
the same shape `metis gateway issue-key` / `revoke-key` already use,
without ever touching the operator's shell.

### 12.1 Endpoints

| Endpoint | Method | Auth | Description |
|---|---|---|---|
| `/signup` | POST | none | Create a pending account; mint + email a verification magic link. |
| `/signup/verify` | POST | magic-link token (one-shot) | Mark account verified, issue first gateway key, return key plaintext + session token. |
| `/account/keys` | GET | session bearer | List the keys this account owns (joins on the keystore's `key_id`). |
| `/account/keys` | POST | session bearer | Issue another key for this account, scoped to the account's workspace. |
| `/account/keys/{key_id}` | DELETE | session bearer | Revoke a key the account owns. Wraps the existing `keystore_admin.revoke_key`. |

All five mount only when the gateway is launched with
`SignupConfig(enabled=True)` (CLI: `--enable-signup`; helm: `signup.enabled`).
The endpoints return 404 when signup is off so in-VPC deployments don't
have to firewall a public surface.

### 12.2 Account record

`accounts.json` lives at `~/.metis/gateway/accounts.json` (mode `0o600`,
mirroring the keystore). Per-record shape:

```python
class Account(frozen=True):
    account_id: str         # "acc_<ulid>"
    email: str              # plaintext; only place it lives
    email_sha256: str       # derived; used for dedup / future SSO bridge
    workspace_path: str     # synthetic, "/metis/accounts/<account_id>/<name>"
    user_id: str | None     # echoed onto every issued key
    team_id: str | None     # same
    created_at: datetime
    verified_at: datetime | None
    key_ids: tuple[str, ...]  # foreign keys into keys.json
```

The trace store never carries plaintext email (matches
[`multi-user.md §3.3`](multi-user.md)). The signup payload carries email
for the magic-link sender; the key it issues references the account via
`user_id` only.

### 12.3 Magic-link transport (stubbed in v1)

The magic-link "email" is **logged to stdout** in Wave 14:

```
[magic-link signup] email=alice@example.com url=<dashboard>/signup/verify?token=mlk_... expires_in_seconds=1800
```

Wave 15 swaps in a real transport (SES / SendGrid pluggable). Logging
to stdout is a development affordance — the operator MUST swap in real
email before exposing `/signup` on the open internet. The logged URL
isn't a security hole on a private network (only the operator can read
stdout), but it would be one on a public host without the swap.

Tokens are 32-byte URL-safe randoms, SHA-256-hashed before persisting.
Defaults: magic-link TTL 30 minutes, session TTL 24 hours. Magic links
are single-use; re-posting `/signup` against a still-pending account
re-mints (so a user who lost the first email isn't stuck).

### 12.4 Account session

Verifying the magic link returns a `sess_<random>` token. The token is
SHA-256-hashed before persisting (same shape as gateway keys). The
account uses this token in `Authorization: Bearer sess_…` to call
`/account/keys`. Sessions expire after `session_ttl` and are deleted
on the next access; there is no refresh path in v1 (request another
magic link to extend).

### 12.5 What the operator still owns

- **Real email delivery.** The Wave-14 stub is a placeholder, not a
  feature; Wave 15 wires SES/SendGrid.
- **Rate limiting `/signup`.** The existing per-IP rate limiter
  ([`gateway-hardening.md §3`](gateway-hardening.md)) applies to signup
  endpoints too; operators exposing the surface publicly should enable
  it.
- **Billing secrets and Stripe live-mode readiness.** Wave 15 / Wave 16
  ship the billing engine and self-service endpoints, but the operator
  still owns Stripe account provisioning, webhook secret rotation, tax /
  invoice policy, and live-mode validation before public exposure.
- **Storage durability.** `accounts.json` is a local-FS file; SaaS
  deployments running multi-pod must mount it on durable shared
  storage (same RWX caveat as the trace DB; helm `signup.accountsPath`
  is the mount point).

### 12.6 Non-goals (still)

1. **No SSO / OIDC / SAML in v1.** Same posture as multi-user.md §8.1 —
   the IdP bridge lands when a buyer specifically asks.
2. **No password auth.** Magic link is the only credential.
3. **No HTTP key-rotation endpoint.** `metis gateway rotate-key` remains
   CLI-only; `/account/keys` POST is mint-new, DELETE is revoke. A
   rotation surface lands when there's evidence buyers want one.
4. **No per-account dashboard UI.** `dashboard_url` is a placeholder
   field on the verification response — the SPA itself ships later.

---

## 13. Billing (Wave 15 + Wave 16)

Billing is opt-in and only mounts when `GatewayConfig.billing` is set
(CLI: `--enable-billing`; helm: `billing.enabled`). It also requires
signup/account sessions because every billing mutation is scoped to a
verified account. Deployments with billing disabled return 404 for the
billing namespace and preserve the pre-billing gateway surface.

### 13.1 Endpoints

| Endpoint | Method | Auth | Description |
|---|---|---|---|
| `/account/billing` | GET | session bearer | Return the current subscription summary, effective billing tier, payment state, and free-tier cap. |
| `/account/billing/portal` | GET | session bearer | Create a one-shot Stripe Customer Portal link for payment method, invoice, and cancellation self-service. |
| `/account/billing/plan` | POST | session bearer | Switch plan with `{"plan": "free" | "pro" | "enterprise"}` plus optional `seats` and `payment_method_id`. |
| `/account/billing/subscribe` | POST | session bearer | Back-compat Pro-subscription creation endpoint from Wave 15. |
| `/account/billing/payment-method` | POST | session bearer | Attach or replace a Stripe payment method by id. |
| `/account/billing/cancel` | POST | session bearer | Cancel the subscription, at period end by default. |
| `/account/billing/pause` | POST | session bearer | Pause Stripe collection. |
| `/account/billing/resume` | POST | session bearer | Resume Stripe collection. |
| `/webhooks/stripe` | POST | Stripe signature | Handle subscription update/delete and invoice paid/payment-failed events idempotently. |

### 13.2 Plan semantics

- **Free** is the entry tier. New signups have no subscription record
  and inherit the configured Free cap (`free_monthly_cap_usd = 5.00`
  by default).
- **Pro** is a per-seat Stripe subscription. `POST /account/billing/plan`
  creates a subscription if one does not exist, or changes seats on the
  existing subscription if it does.
- **Enterprise** is Pro plus the reserved metered %-of-savings add-on.
  The self-service endpoint can attach/remove the enterprise metered
  item, but the contract-specific savings rate and monthly cap remain
  operator-configured.

Plan changes emit the existing `billing.subscription_created`,
`billing.subscription_updated`, or `billing.subscription_canceled`
audit events. No new event type is required for Wave 16.

### 13.3 Failed-payment policy

Stripe `invoice.payment_failed` puts the account in a 7-day grace
window (`failed_payment_grace_days`, default 7). During grace, the
effective customer tier remains paid so buyers can fix payment without
interrupting dev workflows. After grace expires, the billing service
marks the subscription as frozen, sets the effective tier to Free, and
lets the normal tier-cap path enforce the Free monthly cap. A later
`invoice.payment_succeeded` restores the paid tier and clears the
frozen marker.

The v1 email notification is intentionally stubbed: operators should
send the notification from the billing operator runbook until a real
transactional-email adapter is installed.

---

## 14. Follow-ons (next-up after Wave 16)

1. **`--ignore-inbound-model` flag** (§5.3 open question). Lets routing fall through to rule / workspace / global slots even when the client's `model` is set, opting into transparent cost-optimization mode.
2. **`/v1/models` listing.** OpenAI clients expect this surface; deferred until a Cursor or Continue user asks.
3. **Real billing / signup email adapters.** Magic links and failed-payment notifications are still stdout / operator-runbook stubs in v1; public SaaS exposure needs SES / SendGrid or equivalent.
4. **SSO / OIDC / SAML.** Magic links are the only account auth mechanism until a buyer asks for an IdP bridge.
5. **Typed `gateway_key_id` / `inbound_shape` on `TurnCompleted`.** Currently dict-envelope-stamped; promoting them to typed fields keeps the catalog discipline (`event-bus-and-trace-catalog.md`) honest.

---

## 15. References

- [`canonical-message-format.md`](canonical-message-format.md) — `Message`, `ContentBlock`, persistence schema.
- [`provider-adapter-contract.md`](provider-adapter-contract.md) — adapter interface, retry, error classes.
- [`routing-engine.md`](routing-engine.md) — the 7-slot chain consumed in §5.
- [`event-bus-and-trace-catalog.md`](event-bus-and-trace-catalog.md) — additive payload fields on `LLMCallCompleted`.
- [`analytics-api.md §4.1, §4.8`](analytics-api.md) — `group_by=gateway_key` on `/analytics/cost` and the `/analytics/by_key` rollup.
- [`deployment-shape.md`](deployment-shape.md) — the gateway/agent/hybrid framing this spec is paired with.
- [`apps/gateway/src/metis_gateway/`](../../apps/gateway/src/metis_gateway/) — implementation.
