# Provider Adapter Contract

**Status:** Draft v1.3
**Last updated:** 2026-05-21
**Owner:** _your name_

> **v1.3 changes:** New §4.5 — prompt-caching capability detection and the
> adapter's breakpoint responsibility for aggregator upstreams (OpenRouter).
> Pins per-model caching detection from `/api/v1/models` pricing fields, the
> `cache_control` wire shape on the OpenAI-shaped `/chat/completions`
> endpoint, the `usage` cache-token fields, and the provider-routing posture
> for keeping cache hits sticky. Resolves the OpenRouter half of §11 open
> question 7. Additive — no contract change to existing adapters.

> **v1.2 changes:** `CanonicalResponse` returns `content: list[ContentBlock]`
> + `model` + `provider` rather than a full `Message` (§3.3). The adapter
> doesn't see the routing decision or the cost, so it returns the parts it
> knows and the caller (`SessionManager`) assembles the final canonical
> `Message`. Substitutability is unchanged: any two adapters returning the
> same `(content, stop_reason, usage)` triple still produce identical
> downstream `Message`s.

> **v1.1 changes:** Clarified that streaming events emit to a separate
> streaming-only channel, not through the bus (§5.1). Pinned `max_retries`
> semantics (§6.4): total attempts = 1 + max_retries.

> *Throughout: paths shown use `~/.yourtool/` as a placeholder for the final config directory.*

---

## 1. Purpose

This document specifies the contract every LLM provider adapter implements — the Python interface, the wire-format translation rules, streaming normalization, error classification, cost reporting, and capability declaration.

Without this contract, adapters built in parallel (Anthropic, OpenAI, eventually Ollama and OpenRouter) will diverge structurally in subtle ways: different tool-result shapes, different cancellation semantics, different cost computations, different stream-chunk handling. The canonical-format guarantee (lossless round-trip across providers, mid-session swap survives) depends on adapters being substitutable at the contract level.

Two adapters built without this spec will pass tests individually but break when a session swaps between them. This spec is the substitutability contract.

This spec depends on:

- `canonical-message-format.md` for `Message`, `ContentBlock`, `ToolDefinition`, `Usage`, `AdapterCapabilities`.
- `event-bus-and-trace-catalog.md` for `llm.call_*` events and the `error_class` enum.
- `streaming-protocol.md` for the canonical streaming events (`text.delta`, `tool.use_start`, etc.) the adapter must emit.
- `routing-engine.md` for capability validation requirements (§4.4).

---

## 2. Goals and non-goals

### 2.1 Goals

1. **Substitutable.** Two adapters meeting this contract are interchangeable at the canonical layer. Mid-session model swap works.
2. **Honest capability declaration.** Adapters declare what they actually support, not what the underlying model supports in theory.
3. **Errors classified consistently.** A rate limit on Anthropic and a rate limit on OpenAI both surface as `error_class: rate_limit`.
4. **Cost reportable.** Adapters report token counts in canonical units; cost computation happens elsewhere from a local price table.
5. **Cancellation works.** Every adapter exposes `cancel(request_id)` that aborts an in-flight call.
6. **Streaming normalized.** Provider-specific chunk formats are translated to canonical streaming events at the adapter boundary; nothing downstream needs to know about provider quirks.

### 2.2 Non-goals

1. **Pricing.** Adapters do not own price tables. They report raw token counts; the core computes USD from a maintained price table per canonical-format §6.4.
2. **Retry policy beyond the adapter's own bounds.** Adapters do bounded transient retry internally. Sustained failure escalates to the availability state machine in routing-engine §4.5; that's not the adapter's concern.
3. **Provider-specific feature exposure.** Anthropic's prompt-cache breakpoints, OpenAI's `logit_bias`, etc. are not in the canonical interface. Adapters may use them internally for performance but cannot require them in the canonical API.
4. **Authentication beyond API keys.** OAuth flows, refresh tokens, etc. are out of scope. v1 is API-key auth only.
5. **Local model serving.** Ollama and similar are deferred; the contract is designed to accommodate them but v1 only ships Anthropic and OpenAI adapters.

---

## 3. The interface

### 3.1 Adapter protocol

Every adapter implements this Python protocol:

```python
class ProviderAdapter(Protocol):
    """Implemented by every provider adapter."""

    name: str                    # "anthropic" | "openai" | "ollama" | ...
    capabilities: AdapterCapabilities

    def __init__(self, config: AdapterConfig) -> None: ...

    async def complete(
        self,
        request: CanonicalRequest,
    ) -> CanonicalResponse:
        """Non-streaming call. Returns once the response is fully received.
        Raises AdapterError subclasses on failure (see §6)."""

    async def stream(
        self,
        request: CanonicalRequest,
    ) -> AsyncIterator[StreamEvent]:
        """Streaming call. Yields canonical StreamEvents in order until the
        response completes or is cancelled. See §5 for event sequence rules."""

    def estimate_input_tokens(
        self,
        messages: list[Message],
        tools: list[ToolDefinition],
        system_prompt: str | None,
    ) -> int:
        """Pre-flight token estimate for routing decisions. Does not call
        the provider; uses local tokenizer or heuristic. Accuracy: ±10%
        is acceptable."""

    async def cancel(self, request_id: str) -> bool:
        """Abort an in-flight request. Returns True if the request was
        cancelled cleanly, False if it had already completed or wasn't
        found. Idempotent."""

    async def close(self) -> None:
        """Release adapter resources (HTTP client connection pool, etc.).
        Called at server shutdown."""
```

### 3.2 Adapter configuration

```python
class AdapterConfig:
    api_key: str | None              # may be None for local adapters
    base_url: str | None             # override default endpoint; for proxies/Ollama
    timeout_seconds: float = 600     # overall request timeout
    max_retries: int = 2             # bounded retry within the adapter; see §6.4
    extra_headers: dict[str, str] = {}  # custom headers (e.g. for OpenRouter)
    # Adapter-specific options accepted but not required:
    options: dict = {}
```

`options` is a permission to pass adapter-specific knobs (e.g., Anthropic's `anthropic-beta` headers, OpenAI's `organization` field). Core code never reads from `options`; only the specific adapter does.

### 3.3 Canonical request and response

The adapter sees canonical inputs and produces canonical outputs. It does not see other adapters' types, even indirectly.

```python
class CanonicalRequest:
    request_id: str                  # ULID, generated by core; passed to cancel()
    messages: list[Message]          # canonical messages, in order
    tools: list[ToolDefinition]      # tools to expose; may be empty
    system_prompt: str | None        # composed by context assembler; nullable
    model: str                       # provider:name canonical id
    max_output_tokens: int           # required; adapter must honor
    stop_sequences: list[str] = []
    temperature: float | None = None
    output_schema: dict | None = None  # for structured output; v1 used only for delegation
    # Streaming-only:
    stream: bool = False             # True = use stream(); False = use complete()

class CanonicalResponse:
    request_id: str
    model: str                       # canonical "provider:name" — the actual model that served the call
    provider: str                    # adapter.name; for trace-side bookkeeping
    content: list[ContentBlock]      # the assistant's reply blocks, in order
    stop_reason: StopReason
    usage: TokenUsage                # raw token counts, no cost
    latency_ms: int                  # wall-clock for the call

class StopReason(StrEnum):
    END_TURN       = "end_turn"
    MAX_TOKENS     = "max_tokens"
    STOP_SEQUENCE  = "stop_sequence"
    TOOL_USE       = "tool_use"
    CANCELLED      = "cancelled"
    ERROR          = "error"

class TokenUsage:
    # The three input buckets are DISJOINT and sum to the total prompt
    # token count (see §7.1). `input_tokens` is the *uncached* remainder.
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int = 0     # cache hit (reads from cache)
    cache_creation_input_tokens: int = 0  # cache write (creates cache entry)
    # Cost is NOT reported here; computed by core from price table.
```

The adapter returns `content` rather than a full `Message` because it does
not own two of the required `Message` fields: the `RoutingDecisionRecord`
(decided upstream by the routing engine) and `Usage.cost_usd` (computed by
the core from the local price table per canonical-format §6.4). The caller
(`SessionManager`) assembles the final `Message` by combining the adapter's
`content` + `model` + `provider` with its own routing decision, cost
computation, and id allocation. Adapters never see `Message` on the
response side. Substitutability is unaffected: two adapters returning the
same `(content, stop_reason, usage)` triple produce identical downstream
`Message`s.

### 3.4 Capability declaration

Every adapter declares its capabilities. Per `routing-engine.md` §4.4, routing validates against these before dispatch.

```python
class AdapterCapabilities:
    # Content type support
    supports_images: bool
    supports_thinking: bool
    supports_tools: bool
    supports_system_prompt: bool
    supports_structured_output: bool

    # Streaming
    supports_streaming: bool
    supports_streaming_tool_calls: bool   # whether tool_use_input_delta is meaningful
    supports_parallel_tool_calls: bool    # multiple tool_use blocks in one assistant turn

    # Caching
    supports_prompt_caching: bool

    # Limits
    max_context_tokens: int
    max_output_tokens: int

    # Image format support (only meaningful if supports_images)
    accepted_image_media_types: list[str]
```

Declarations MUST be honest. If a model technically supports a feature but the adapter implementation doesn't expose it, declare `false`. The capability surface is the substitutability boundary; lying about it breaks mid-session swaps.

For example, if Ollama's API supports tools but the specific local model loaded doesn't tool-call reliably, declare `supports_tools: false` for that model. Routing will skip it for tool turns.

---

## 4. Wire-format translation

This is where most of the work lives. Per provider, the adapter translates canonical → wire on request and wire → canonical on response.

### 4.1 The two universal hard parts

**Tool calls and system prompts** are where Anthropic and OpenAI most divergently shape their wire formats. The canonical format is a *superset*; adapters project losslessly onto each provider's accepted shape.

#### 4.1.1 Tool call serialization

| Aspect                  | Canonical                                              | Anthropic                                | OpenAI                                                 |
|-------------------------|--------------------------------------------------------|------------------------------------------|--------------------------------------------------------|
| Tool definition         | `ToolDefinition` with `name`, `description`, `input_schema` | `{name, description, input_schema}` direct | `{type: "function", function: {name, description, parameters}}` |
| Tool call (in message)  | `ToolUseBlock` in ASSISTANT message                    | `tool_use` content block                 | `tool_calls[]` array on the message; `function.arguments` is JSON-stringified |
| Tool result (separate role) | `ToolResultBlock` in TOOL message                      | `tool_result` content block in USER message | message with `role: tool`, `tool_call_id`, `content`   |
| Input data type         | `dict` (validated against schema)                      | `dict`                                   | JSON-stringified; adapter parses on parse, stringifies on serialize |
| Tool ids                | Canonical `tu_<ulid>`; bidirectional map per session   | `toolu_*` (provider-issued)              | `call_*` (provider-issued)                             |

Adapters maintain a per-session bidirectional map between canonical and provider-issued tool ids per canonical-format §6.2. When parsing wire → canonical, look up or create the canonical id; when serializing canonical → wire, look up the provider id (or generate if first use of this canonical id with this provider).

#### 4.1.2 System prompt placement

| Canonical                      | Anthropic                  | OpenAI                              |
|--------------------------------|----------------------------|-------------------------------------|
| `SYSTEM` role messages in list | Top-level `system` parameter | First message in `messages` with `role: system` |

The adapter hoists / injects as needed. Multiple `SYSTEM` messages in the canonical list are concatenated (with `\n\n` separator) before placement.

### 4.2 Anthropic adapter specifics

**Endpoint:** `POST https://api.anthropic.com/v1/messages`

**Request shape (high level):**
```python
{
    "model": <wire model name, derived from canonical id>,
    "max_tokens": request.max_output_tokens,
    "system": <hoisted system prompt or omitted>,
    "messages": [
        # USER, ASSISTANT, TOOL messages translated; SYSTEM hoisted out
    ],
    "tools": [<tool defs>] or omitted,
    "stop_sequences": request.stop_sequences,
    "temperature": request.temperature,
    "stream": request.stream,
}
```

**Message translation:**

- Canonical `USER` → Anthropic `user`. Content blocks pass through (text, image).
- Canonical `ASSISTANT` → Anthropic `assistant`. Content blocks pass through (text, tool_use, thinking).
- Canonical `TOOL` → Anthropic `user` with `tool_result` content blocks. The `tool_use_id` is mapped to the provider's stored id via the per-session map.

**Thinking blocks:** Anthropic returns these natively for extended-thinking models. The adapter passes them through as `ThinkingBlock` and stashes the opaque `signature` in `provider_raw` for round-trip fidelity (per canonical-format §6.5).

**Token caching:** The adapter MAY add `cache_control` markers to messages or system prompt for prompt caching. This is performance optimization; users don't see it in the canonical surface. Cache token counts are reported in `TokenUsage.cached_input_tokens` and `cache_creation_input_tokens`.

### 4.3 OpenAI adapter specifics

**Endpoint:** `POST https://api.openai.com/v1/chat/completions` (or `/v1/responses` for newer models).

**Request shape:**
```python
{
    "model": <wire model name>,
    "max_completion_tokens": request.max_output_tokens,
    "messages": [
        # SYSTEM as first role:system message; USER, ASSISTANT, TOOL as their respective roles
    ],
    "tools": [{"type": "function", "function": {...}}] or omitted,
    "stop": request.stop_sequences,
    "temperature": request.temperature,
    "stream": request.stream,
    # if request.output_schema:
    "response_format": {"type": "json_schema", "json_schema": {...}},
}
```

**Message translation:**

- Canonical `SYSTEM` → OpenAI `system`. First message; if multiple canonical SYSTEMs, concatenated.
- Canonical `USER` → OpenAI `user`. Content blocks pass through; images use OpenAI's `image_url` shape.
- Canonical `ASSISTANT` → OpenAI `assistant`. Tool uses become `tool_calls[]` on the message; `function.arguments` is JSON-stringified from the canonical `dict`.
- Canonical `TOOL` → OpenAI `tool`. The `tool_call_id` is mapped via the per-session id map. Content is the tool result text (multiple content blocks concatenated).

**Thinking blocks:** OpenAI's reasoning models use a different mechanism. The adapter MUST drop canonical `ThinkingBlock` and `RedactedThinkingBlock` on the way out (with a WARN-level log entry per canonical-format §7.3). On the way in, OpenAI's reasoning content is *not* mapped to canonical thinking blocks in v1 (the formats are too different). This is a known asymmetry: a session that originated on Anthropic and swaps to OpenAI loses thinking-block content; a session that originated on OpenAI and swaps to Anthropic doesn't gain thinking blocks.

**Caching:** OpenAI's prompt cache is applied automatically by the provider. The adapter reports `cached_input_tokens` from response usage; `cache_creation_input_tokens` is always 0 (OpenAI doesn't separately report cache creation).

### 4.4 Lossy projection rules

When canonical content cannot be represented in a provider's wire format, the adapter MUST:

1. Drop the unrepresentable content silently from the wire request.
2. Emit a structured log entry at level `WARN` with: session_id, message_id, block type, adapter name, reason. (Not a bus event — this is bus diagnostics per event-bus §3.5 reasoning.)
3. Never partially serialize. Drop cleanly or fail.

Examples:

- `ThinkingBlock` sent to OpenAI: dropped, logged.
- `RedactedThinkingBlock` cross-provider (any direction not Anthropic→Anthropic): dropped, logged.
- `ImageBlock` sent to a model whose `supports_images: false`: should never reach the adapter (routing rejects), but if it does, dropped and logged. The session manager should treat this as a bug.

---

### 4.5 Prompt-caching capability detection and breakpoint responsibility

A **direct** provider adapter (Anthropic, OpenAI) knows its upstream's caching contract at build time — §4.2 and §4.3 hardcode it. An **aggregator** adapter (OpenRouter today; an optional LiteLLM egress proxy later) fronts dozens of upstreams with *different* caching contracts behind one OpenAI-shaped wire. It cannot hardcode caching; it must (a) detect per-model whether caching is available and which *style* it uses, and (b) attach explicit cache breakpoints for the upstreams that require them, or those upstreams cache nothing.

This subsection pins both. The worked example is OpenRouter (`/api/v1/chat/completions`, OpenAI wire shape); the rules generalize to any aggregator. The findings here are verified against OpenRouter's documentation and a live `/api/v1/models` fetch on 2026-05-21.

#### 4.5.1 Two caching styles

| Style | Upstreams (via OpenRouter) | Adapter responsibility |
|-------|----------------------------|------------------------|
| **Implicit / automatic** — upstream caches the prompt prefix on its own; the client sends no markers. | OpenAI, Grok, DeepSeek, Moonshot, Groq, Gemini 2.5 (implicit) | Attach nothing. Only read cache token counts back (§4.5.4). |
| **Explicit / breakpoint** — upstream caches only spans the client marks with a `cache_control` breakpoint. | Anthropic Claude, Google Gemini (explicit path), Alibaba Qwen | MUST attach a breakpoint (§4.5.3) or caching never fires. |

A direct adapter targets one style. An aggregator adapter sees both and branches per model. **The current `OpenRouterAdapter` attaches no breakpoints and declares `supports_prompt_caching=False` for every model — so Anthropic models routed via OpenRouter get zero prompt caching today. This subsection is the contract for closing that gap.**

#### 4.5.2 Detecting caching capability per model

OpenRouter's `/api/v1/models` carries **no dedicated caching capability flag**. `supported_parameters` does **not** list `cache_control` (verified 2026-05-21 across `anthropic/claude-haiku-4.5`, `anthropic/claude-sonnet-4.5`, `openai/gpt-4o-mini`, `google/gemini-2.5-flash`, `deepseek/deepseek-chat`). The only machine-readable signal is the `pricing` block:

| `pricing` field | Meaning | Present on (2026-05-21 sample) |
|-----------------|---------|--------------------------------|
| `input_cache_read` | Cache reads are priced → caching pays off. $/token string. | Anthropic, OpenAI, Gemini |
| `input_cache_write` | Cache writes are *separately* priced. $/token string. | Anthropic, Gemini (absent on OpenAI — its cache writes are free; absent on DeepSeek) |

Detection rules:

1. **`supports_prompt_caching`** — set `AdapterCapabilities.supports_prompt_caching = True` iff `pricing.input_cache_read` is present, else `False`. This is the honest per-model declaration §3.4 requires.
2. **Cache-write pricing** — `_parse_pricing` currently hardcodes `ModelPricing.cache_creation_per_mtok = Decimal("0")`. Fix it to read `pricing.input_cache_write` (× `_PER_MTOK`) when present, mirroring how it already reads `input_cache_read` for `cached_read_per_mtok`.
3. **The pricing block does NOT tell you the caching *style*.** `input_cache_write` present is a *hint* toward the explicit-breakpoint family (Anthropic, Gemini, Alibaba all expose it; OpenAI does not), but it is neither authoritative nor complete: `deepseek/deepseek-chat` exposes *neither* cache field yet still caches automatically (verified 2026-05-21).

Because the API gives no style signal, the adapter MUST carry a small **family allowlist** keyed on the wire-id prefix:

```python
# Maintained constant in the OpenRouter adapter. Reviewed when OpenRouter
# adds explicit-caching providers. The API offers no way to derive this.
EXPLICIT_BREAKPOINT_FAMILIES = ("anthropic/", "google/", "qwen/")
```

A model gets an explicit breakpoint **iff** its wire id starts with an allowlisted prefix **AND** `pricing.input_cache_read` is present. Every other model gets no markers and relies on implicit caching. Note the asymmetry this preserves honesty: a model can have `supports_prompt_caching=True` (it has cache-read pricing) and still not be on the allowlist — it caches *implicitly*, the adapter just doesn't mark it. The allowlist governs *breakpoint emission*, not the capability flag.

#### 4.5.3 Breakpoint wire format (OpenAI-shaped `/chat/completions`)

For explicit-breakpoint families, `cache_control` attaches to individual **content-part objects** inside a message's `content` array. It is **not** a top-level message field, and — per OpenRouter's documented surface — **not** a field on `tools[]` entries (see the tools note below).

A breakpoint is a content part with one extra key:

```json
{ "type": "text", "text": "...", "cache_control": { "type": "ephemeral" } }
```

`{"type": "ephemeral"}` is the only cache type. An optional `"ttl": "1h"` extends the default 5-minute TTL to 1 hour (`{"type": "ephemeral", "ttl": "1h"}`); omit it for the 5-minute default, which is what a turn-locked agent loop wants.

**System-prompt placement — the placement Metis uses.** Metis assembles a two-segment system prompt (stable + volatile; see `context-assembler.md §5.1`). To attach a breakpoint, the system message `content` must be promoted from a plain string to a content-part array, breakpoint on the last *stable* part:

```json
{
  "role": "system",
  "content": [
    { "type": "text", "text": "<stable system prompt>", "cache_control": { "type": "ephemeral" } },
    { "type": "text", "text": "<volatile system prompt>" }
  ]
}
```

This mirrors the direct Anthropic adapter's `_system_blocks` (`adapters/anthropic.py`) exactly: the breakpoint sits on the stable segment so per-turn mutations to the volatile content don't churn the cached prefix. If the volatile segment is empty, emit a single stable part carrying the breakpoint. The OpenAI adapter's `_canonical_messages_to_openai` currently concatenates both segments into one string `content` — the OpenRouter path must instead emit the content-part array above when the target model is breakpoint-eligible (§4.5.2).

**Tool definitions.** OpenRouter forwards the breakpoint to Anthropic's Messages API, whose cache-prefix walk is `tools → system → messages`. A single breakpoint at the end of the *stable system segment* therefore caches the tool definitions too — the cached prefix includes everything before the breakpoint. OpenRouter's OpenAI-shaped `tools[]` array carries **no documented `cache_control` field**, so the direct-Anthropic adapter's separate last-tool breakpoint (`_tools_to_anthropic_with_cache`) has no OpenRouter equivalent — and needs none: the system-tail breakpoint subsumes it. (This is a real documentation gap, flagged in §11 — if a future need arises to cache tools *without* a system prompt, the Anthropic-Messages-shaped endpoint `/api/v1/messages` would be required.)

**Limits.** Anthropic allows at most **4** explicit breakpoints per request; Gemini honors only the **last** one. Metis emits exactly **one** (system-tail) — within both limits.

**Minimum cacheable prefix.** Anthropic will not cache a prefix below a per-model floor: **4096** tokens for `claude-haiku-4.5` / `claude-opus-4.5`+, **2048** for `claude-sonnet-4.6`, **1024** for older Sonnet/Opus. The context-assembler's existing `MIN_CACHEABLE_PREFIX_TOKENS = 4500` padding floor (`context-assembler.md §5.1`) clears the highest of these, so **no aggregator-specific padding work is required** — the stable prefix is already padded above the haiku-4.5 floor.

**Why not top-level automatic caching.** OpenRouter also offers a request-body-level `"cache_control"` field that auto-advances a breakpoint over the growing history. Metis does **not** use it: (a) it caches up to the last cacheable block, *including* volatile content, defeating the stable/volatile split; (b) its presence forces routing to Anthropic-direct and **excludes Bedrock / Vertex** upstreams. Explicit per-block breakpoints work across *all* Anthropic-compatible upstreams and keep the split under Metis's control.

#### 4.5.4 Reading cache usage back

OpenRouter returns cache counts inside the standard `usage` object on **every** response — no opt-in. `usage: {include: true}` and `stream_options: {include_usage: true}` are deprecated no-ops.

```json
{
  "usage": {
    "prompt_tokens": 194,
    "completion_tokens": 2,
    "total_tokens": 196,
    "prompt_tokens_details": { "cached_tokens": 0, "cache_write_tokens": 100 },
    "cost": 0.95,
    "cost_details": { "upstream_inference_cost": 19 }
  }
}
```

Canonical mapping into `TokenUsage`:

| OpenRouter `usage` field | `TokenUsage` field |
|--------------------------|--------------------|
| `prompt_tokens` (total — includes cached + written) | — used to *derive* `input_tokens`, see below |
| `prompt_tokens − cached_tokens − cache_write_tokens` | `input_tokens` (the uncached remainder) |
| `completion_tokens` | `output_tokens` |
| `prompt_tokens_details.cached_tokens` | `cached_input_tokens` (cache **read** / hit) |
| `prompt_tokens_details.cache_write_tokens` | `cache_creation_input_tokens` (cache **write**) |

This is a strict superset of the OpenAI mapping in §7.2. `prompt_tokens` is the *total* prompt count and **already includes** both `cached_tokens` and `cache_write_tokens`, so `input_tokens` is the subtraction `prompt_tokens − cached_tokens − cache_write_tokens` (the uncached remainder) per the disjoint-bucket contract in §7.1 — returning `prompt_tokens` verbatim would double-bill the cached span against the §7.1 cost formula. The shared `_usage_to_canonical` helper (`adapters/openai.py`) currently hardcodes `cache_creation_input_tokens = 0`; it must be extended to read `prompt_tokens_details.cache_write_tokens` for the OpenRouter path. `cache_write_tokens` is returned only for models with explicit caching + cache-write pricing; absent → `0` (a plain cache hit, or an implicit-cache model). On a cold call that establishes the cache, expect `cached_tokens = 0` and `cache_write_tokens > 0`; on a warm hit, `cached_tokens > 0` and `cache_write_tokens = 0`.

`usage.cost` and `cost_details.upstream_inference_cost` are OpenRouter's own accounting; Metis **ignores them** and computes cost from the local price table per §7.1, using the catalog's `input_cache_read` / `input_cache_write` rates (§4.5.2). The `cache_discount` field referenced in OpenRouter's caching guide is surfaced via `/api/v1/generation`, **not** the inline `usage` object — Metis does not need it, since canonical cost is recomputed locally.

#### 4.5.5 Keeping cache hits sticky without losing failover

A cached prefix lives on **one** upstream endpoint. A cache hit only happens if the follow-up request lands on that same upstream. OpenRouter handles this with **provider sticky routing**: after a cache-eligible request it routes subsequent same-conversation requests for that model to the same upstream, and falls back to the next-best upstream if the sticky one is unavailable. Stickiness is keyed per account × model × conversation (OpenRouter hashes the first system/developer message + the first non-system message of each request).

**Implication for the adapter: do NOT send a `provider` object to chase cache hits.** Setting `provider.order` or `provider.sort` **disables** sticky routing (and price-based load balancing) — the explicit ordering takes priority. The default — no `provider` object, `allow_fallbacks` defaults to `true` — gives *both* cache warmth (via sticky routing) *and* failover. The OpenRouter adapter SHOULD leave provider routing unset on the caching path.

If a deployment needs a deterministic upstream for an unrelated reason (e.g. data residency), `provider: {"order": ["<slug>"], "allow_fallbacks": true}` pins `<slug>` first and still fails over to the normal list — but trades away sticky routing's load-balancing. That is a deployment-policy choice, outside the adapter's caching path. `allow_fallbacks: false` and `provider.only` remove failover entirely; **do not** use them to pin caching.

---

## 5. Streaming normalization

### 5.1 Provider stream → canonical events

Provider stream chunks are translated to the canonical streaming events from `streaming-protocol.md` §5.3. The adapter is the translation layer.

> *Channel note: streaming events (`message.start`, `text.delta`, `tool.use_start`, etc.) flow on a **separate channel** from the bus, directly to the streaming server. They are NOT bus catalog events and are NOT persisted in the trace store (per `event-bus-and-trace-catalog.md` §4.5.1 and `streaming-protocol.md` §5.1). Bus events emitted by the adapter (`llm.call_started`, `llm.call_completed`, `llm.call_failed`) flow through the bus normally. The adapter is responsible for emitting on the right channel for each event family.*

**Anthropic stream chunks** (server-sent events with named types):

| Anthropic event                              | Canonical event                                      |
|----------------------------------------------|------------------------------------------------------|
| `message_start`                              | `llm.call_started` (bus, already emitted at request init); `message.start` (streaming) |
| `content_block_start` (type: text)           | implicit (incremented `content_block_index`)         |
| `content_block_start` (type: tool_use)       | `tool.use_start` (streaming) with tool_use_id, tool_name |
| `content_block_start` (type: thinking)       | implicit (incremented `content_block_index`)         |
| `content_block_delta` (delta.type: text_delta) | `text.delta`                                       |
| `content_block_delta` (delta.type: input_json_delta) | `tool.use_input_delta` with `partial_json`   |
| `content_block_delta` (delta.type: thinking_delta) | `thinking.delta`                              |
| `content_block_stop` (text block)            | implicit                                             |
| `content_block_stop` (tool_use block)        | `tool.use_end` with `final_input` (parsed from accumulated deltas) |
| `content_block_stop` (thinking block)        | `thinking.delta` final with `signature` populated    |
| `message_delta` (with usage)                 | accumulated for `message.complete`                   |
| `message_stop`                               | `message.complete` with `final_content`, `usage`     |

**OpenAI stream chunks** (server-sent events with `data:` payloads):

| OpenAI chunk shape                                      | Canonical event                                      |
|---------------------------------------------------------|------------------------------------------------------|
| First chunk with `choices[0].delta.role == "assistant"` | `message.start`                                      |
| `choices[0].delta.content` (string)                     | `text.delta` with `content_block_index = 0`          |
| `choices[0].delta.tool_calls[i].id` (first appearance)  | `tool.use_start`                                     |
| `choices[0].delta.tool_calls[i].function.arguments` (string fragment) | `tool.use_input_delta` with `partial_json` |
| `choices[0].finish_reason` set                          | `tool.use_end` for each accumulated tool_call (with parsed JSON), then `message.complete` |
| `usage` field in final chunk (or via `stream_options: {include_usage: true}`) | populated in `message.complete.usage`  |

OpenAI's stream is more compressed than Anthropic's; the adapter buffers per-tool-call argument fragments to emit `tool.use_end` at the right time.

### 5.2 Cross-provider event invariants

Regardless of provider, the canonical event sequence MUST satisfy:

1. `message.start` precedes any deltas for that message.
2. For each tool call: exactly one `tool.use_start`, zero or more `tool.use_input_delta`, exactly one `tool.use_end`. In that order.
3. `tool.use_end.final_input` is a valid JSON object (parsed from accumulated deltas, or the provider's authoritative final input if available).
4. `message.complete` is the last event for a message; carries `final_content` reflecting all deltas seen plus any provider-authoritative state.
5. `text.delta`, `thinking.delta`, `tool.use_*` events for the same `message_id` carry monotonically non-decreasing `content_block_index` values. Multiple events at the same index are fine (multiple deltas to one block).

These invariants are the contract `streaming-protocol.md` clients rely on. Adapters MUST validate their own output against these in tests.

### 5.3 Token streaming and cancellation

When `cancel(request_id)` is called mid-stream:

1. The adapter aborts the underlying HTTP request (most providers honor abort cleanly).
2. The stream iterator yields a final `message.complete` with `stop_reason: cancelled` and the partial `final_content` accumulated so far.
3. Any in-flight tool_use blocks (started but not ended at cancel time) are emitted as `tool.use_end` with `final_input` set to whatever JSON parses cleanly from the accumulated deltas, or `{}` if nothing parses.
4. The adapter does NOT emit `llm.call_failed` from inside the stream; the session manager's cancellation handler (per `routing-engine.md` §3.4 and `streaming-protocol.md` §6) is responsible for higher-level event emission.

The stream iterator MUST terminate after cancellation (raise `StopAsyncIteration`); it must not hang.

### 5.4 Partial JSON in tool inputs

Per `streaming-protocol.md` §5.6, v1 streams raw partial JSON strings without best-effort parsing. The adapter MUST emit `tool.use_input_delta.partial_json` as the literal fragment received from the provider, not as a best-effort parsed object.

The adapter MAY internally accumulate fragments to detect when a complete JSON object has been received (for emitting `tool.use_end` with `final_input`). This internal accumulation is for the adapter's own bookkeeping; the streaming events emitted to consumers carry the raw fragments.

---

## 6. Errors and retries

### 6.1 The error class enum

Adapters MUST classify all errors into one of these classes (matching event-bus §6.3 `llm.call_failed.error_class`):

```python
class ErrorClass(StrEnum):
    RATE_LIMIT       = "rate_limit"        # provider returned a rate-limit signal
    AUTH             = "auth"              # 401, 403, invalid API key
    SERVER_ERROR     = "server_error"      # 5xx other than rate limit
    NETWORK          = "network"           # DNS, connection refused, timeout pre-response
    CONTEXT_OVERFLOW = "context_overflow"  # request exceeds model's context window
    INVALID_REQUEST  = "invalid_request"   # 4xx other than auth (bad params, etc.)
    CANCELLED        = "cancelled"         # client called cancel()
    OTHER            = "other"             # anything else
```

### 6.2 HTTP status mapping

Adapters apply these mappings as a starting point, then adjust based on provider error bodies:

| HTTP status | Default class      | Provider-body adjustments                                      |
|-------------|--------------------|----------------------------------------------------------------|
| 401, 403    | `AUTH`             |                                                                |
| 408         | `NETWORK`          |                                                                |
| 413         | `CONTEXT_OVERFLOW` | Some providers use 400 with body indicating overflow; remap.   |
| 429         | `RATE_LIMIT`       |                                                                |
| 5xx         | `SERVER_ERROR`     | Some providers use 529 specifically; same class.               |
| Connection refused, DNS error, TLS error | `NETWORK` | Pre-response errors.                                |
| 4xx other   | `INVALID_REQUEST`  | Anthropic returns `error.type` like `"invalid_request_error"` or `"overloaded_error"`; adjust class. |

Per-provider error-body conventions:

- **Anthropic:** Body has `{error: {type, message}}`. Use `error.type` as a hint:
  - `"overloaded_error"` → `RATE_LIMIT` (even if HTTP 529).
  - `"rate_limit_error"` → `RATE_LIMIT`.
  - `"authentication_error"`, `"permission_error"` → `AUTH`.
  - `"invalid_request_error"` with message containing "context" or "tokens exceeds" → `CONTEXT_OVERFLOW`.
  - `"api_error"` → `SERVER_ERROR`.

- **OpenAI:** Body has `{error: {type, code, message}}`. Use:
  - `error.code == "rate_limit_exceeded"` → `RATE_LIMIT`.
  - `error.code == "context_length_exceeded"` → `CONTEXT_OVERFLOW`.
  - `error.code == "invalid_api_key"` → `AUTH`.
  - `error.type == "server_error"` → `SERVER_ERROR`.

### 6.3 Exception hierarchy

```python
class AdapterError(Exception):
    """Base. All adapter exceptions inherit."""
    error_class: ErrorClass
    provider_status: int | None      # HTTP status if applicable
    provider_message: str             # raw provider message, possibly redacted
    retryable: bool                   # whether the adapter retried internally
    request_id: str

class RateLimitError(AdapterError):
    retry_after_seconds: float | None  # if provider provided a hint

class AuthError(AdapterError): pass
class ServerError(AdapterError): pass
class NetworkError(AdapterError): pass
class ContextOverflowError(AdapterError): pass
class InvalidRequestError(AdapterError): pass
class CancelledError(AdapterError): pass
```

Adapters raise the most specific subclass. Code in the core catches `AdapterError` for general handling; specific subclasses for targeted recovery.

### 6.4 Retry behavior within the adapter

Adapters retry transient errors with bounded exponential backoff:

- **Retryable classes:** `RATE_LIMIT`, `SERVER_ERROR`, `NETWORK`.
- **Non-retryable classes:** `AUTH`, `CONTEXT_OVERFLOW`, `INVALID_REQUEST`, `CANCELLED`. Raise immediately.
- **Max retries:** `config.max_retries` (default 2). This is the number of *additional* attempts after the first; total attempts = 1 + max_retries. With the default of 2, a request can be attempted up to 3 times before raising.
- **Backoff:** start at 1 second, double each attempt, with up to ±25% jitter. Cap at 30 seconds.
- **Honor `retry_after`:** If a `RATE_LIMIT` response includes a retry-after hint, sleep for that duration (capped at 60 seconds) before retry.

After exhausting retries, raise the appropriate subclass with `retryable=True` so the caller knows it was a transient class. Sustained failure is the routing-engine's availability state machine's concern (§4.5), not the adapter's.

### 6.5 Streaming error handling

When an error occurs mid-stream:

1. The adapter completes the current event if possible (e.g., flushes any partial `text.delta`).
2. Emits a final `message.complete` with `stop_reason: error` and the partial content accumulated.
3. Raises the appropriate `AdapterError` subclass after the iterator yields final.

The session manager catches the exception and emits the `llm.call_failed` event; the adapter does not emit it directly.

---

## 7. Cost reporting

### 7.1 Token reporting only

Adapters report raw token counts in `TokenUsage`. They do NOT compute USD cost. Cost is the core's responsibility, computed from the local price table per canonical-format §6.4.

```python
class TokenUsage:
    input_tokens: int                # uncached prompt tokens only
    output_tokens: int
    cached_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
```

**The three input buckets are disjoint.** `input_tokens` is the *uncached*
prompt tokens; `cached_input_tokens` is the cache-read (hit) span; and
`cache_creation_input_tokens` is the cache-write span. They do not overlap, and
`input_tokens + cached_input_tokens + cache_creation_input_tokens` equals the
total prompt token count. The cost formula below depends on this — it prices
each bucket exactly once at its own rate. An adapter whose upstream reports a
*total* prompt count that already includes the cached/created spans (every
OpenAI-wire provider — see §7.2 and §4.5.4) MUST subtract those spans out of
`input_tokens` before returning `TokenUsage`, or the cached span is billed
twice. The Anthropic API reports disjoint buckets natively, so its adapter maps
them straight through.

The core, on receiving a `CanonicalResponse` from the adapter:

1. Looks up the `pricing_version` and per-model rates from the local price table.
2. Computes `cost_usd = input_tokens * input_rate + output_tokens * output_rate + cached_input_tokens * cached_rate + cache_creation_input_tokens * cache_creation_rate`.
3. Populates `Message.metadata.usage.cost_usd` and `pricing_version`.

This separation lets the core retroactively reprice (walk traces, recompute) and handle synthetic providers (Ollama at zero cost, OpenRouter with provider-resolved rates).

### 7.2 Provider-specific token reporting

Both Anthropic and OpenAI report tokens in their response bodies:

- **Anthropic:** `usage: {input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens}`. Anthropic's `input_tokens` is already the uncached remainder (the buckets are disjoint upstream). Map directly: `cache_read_input_tokens` → `cached_input_tokens`.
- **OpenAI:** `usage: {prompt_tokens, completion_tokens, prompt_tokens_details: {cached_tokens}}`. `prompt_tokens` is the *total* prompt count and **already includes** `cached_tokens`. Map: `input_tokens` = `prompt_tokens − cached_tokens` (the uncached remainder, per the disjoint-bucket contract in §7.1), `completion_tokens` → `output_tokens`, `prompt_tokens_details.cached_tokens` → `cached_input_tokens`. `cache_creation_input_tokens = 0` (OpenAI doesn't separately report it). Mapping `prompt_tokens` straight to `input_tokens` would double-bill the cached span.

For streaming responses, both providers send usage in the final stream chunk (OpenAI requires `stream_options: {include_usage: true}` in the request). Adapters MUST request usage in streaming mode and propagate it via `message.complete.usage`.

If usage is unavailable for some reason (provider didn't send it; rare), the adapter MAY set `input_tokens` and `output_tokens` to `estimate_input_tokens()`'s output and the streamed-token count respectively, with a `WARN` log noting the estimation. The core's analytics layer flags estimated usage as such.

---

## 8. Configuration and lifecycle

### 8.1 Adapter registry

The core maintains a registry mapping canonical model ids to (adapter, provider-specific config). Example:

```yaml
# ~/.yourtool/models.yaml
adapters:
  anthropic:
    type: anthropic
    api_key_env: ANTHROPIC_API_KEY
    base_url: https://api.anthropic.com
    timeout_seconds: 600
    max_retries: 2

  openai:
    type: openai
    api_key_env: OPENAI_API_KEY
    base_url: https://api.openai.com
    timeout_seconds: 600
    max_retries: 2

models:
  anthropic:claude-opus-4-7:
    adapter: anthropic
    wire_name: claude-opus-4-7
    tier: deep
    can_delegate: true
    aliases: [opus, deep]
  anthropic:claude-sonnet-4-6:
    adapter: anthropic
    wire_name: claude-sonnet-4-6
    tier: balanced
    can_delegate: true
    aliases: [sonnet, balanced]
  anthropic:claude-haiku-4-5:
    adapter: anthropic
    wire_name: claude-haiku-4-5
    tier: fast
    can_delegate: false
    aliases: [haiku, fast]
  openai:gpt-5:
    adapter: openai
    wire_name: gpt-5
    tier: balanced
    can_delegate: true
    aliases: [gpt5]
```

Each model entry maps to an adapter instance and carries `wire_name` (the actual model string the adapter sends to the provider), `tier`, `can_delegate`, and `aliases` (per `routing-engine.md` §6.8 and §9.2).

The registry is loaded at server startup. Hot reload on config change is desirable but deferred to Phase 2 (the routing.yaml hot reload covers the more common case).

### 8.2 API key resolution

`api_key_env` references an environment variable. Direct `api_key` in config is also accepted but discouraged (key in plaintext config file). Missing API key → adapter fails to register; models routed through that adapter fail validation with `not_configured`.

### 8.3 Lifecycle

- **Startup:** Registry loaded, adapters instantiated, capabilities cached. Each adapter opens an HTTP client connection pool.
- **Steady state:** Adapter instances are long-lived. Concurrent calls share the connection pool.
- **Shutdown:** `close()` called on every adapter; connection pools drain.

---

## 9. Worked examples

### 9.1 Anthropic happy path

Canonical request:

```python
CanonicalRequest(
    request_id="req_01HZ...",
    model="anthropic:claude-sonnet-4-6",
    messages=[
        Message(role=USER, content=[TextBlock("Read README.md and summarize")]),
    ],
    tools=[ToolDefinition(name="read_file", input_schema={...}, ...)],
    system_prompt="You are a helpful assistant.",
    max_output_tokens=2048,
    stream=True,
)
```

Adapter serializes to Anthropic wire:

```json
{
  "model": "claude-sonnet-4-6",
  "max_tokens": 2048,
  "system": "You are a helpful assistant.",
  "messages": [
    {"role": "user", "content": [{"type": "text", "text": "Read README.md and summarize"}]}
  ],
  "tools": [{"name": "read_file", "description": "...", "input_schema": {...}}],
  "stream": true
}
```

Anthropic streams back `message_start`, `content_block_start` (text), `content_block_delta` (text_delta), `content_block_stop`, `content_block_start` (tool_use), `content_block_delta` (input_json_delta) ×N, `content_block_stop`, `message_delta`, `message_stop`.

Adapter emits canonical events: `message.start`, `text.delta` ×N, `tool.use_start`, `tool.use_input_delta` ×N, `tool.use_end` (with parsed final input), `message.complete` (with usage).

### 9.2 OpenAI tool-call round-trip after Anthropic prefix

Session has 4 prior messages (USER, ASSISTANT with tool_use, TOOL with result, ASSISTANT with text). All produced on Anthropic. User runs `/model openai:gpt-5`. Next turn, OpenAI adapter must serialize the entire history.

Translation of the history:

- `SYSTEM` (composed): hoisted as `messages[0]` with `role: system`.
- USER: `messages[1]` with `role: user`.
- ASSISTANT with `text` + `tool_use` blocks: `messages[2]` with `role: assistant`, `content: <text>`, `tool_calls: [{id: <provider-id>, type: "function", function: {name: <tool_name>, arguments: <JSON-stringified input>}}]`. The provider id is fetched from the per-session map (or generated if first cross-provider use of this canonical id).
- TOOL: `messages[3]` with `role: tool`, `tool_call_id: <provider-id>`, `content: <result text>`.
- ASSISTANT with text only: `messages[4]` with `role: assistant`, `content: <text>`.

If the original ASSISTANT message had a `ThinkingBlock`, the adapter drops it on serialize (WARN log entry; rationale in §4.4).

OpenAI processes the request and streams back deltas. The adapter normalizes them to canonical events same as in §9.1.

### 9.3 Provider failure with retry

Adapter calls Anthropic; receives 529. Adapter classifies as `RATE_LIMIT` (per the body's `error.type: overloaded_error`). Sleeps with backoff (1s + jitter). Retries.

Second attempt: 529 again. Sleeps 2s + jitter. Retries.

Third attempt (max_retries=2 means 2 retries after the first failure): 200 OK, normal response.

The session sees no failure — the retries are internal. The trace store sees three `llm.call_started` events (the original plus two retries) but only one `llm.call_completed`. The first two have `llm.call_failed` events with `error_class: rate_limit, retry_count: 0` and `retry_count: 1`.

If the third attempt also failed, the adapter raises `RateLimitError`. The session manager catches it, emits `llm.call_failed` with `retry_count: 2`. Routing's availability state machine (per `routing-engine.md` §4.5) sees the failure pattern; if rules trigger, the (provider, model) or provider transitions to Unavailable.

### 9.4 Cancellation mid-stream

Adapter is mid-stream on Anthropic, having emitted 200 `text.delta` events and started a `tool.use_start` (no `tool.use_end` yet — tool input still streaming).

Client sends cancel via WebSocket (per `streaming-protocol.md` §6). Session manager calls `adapter.cancel(request_id)`.

Adapter:

1. Aborts the HTTP request.
2. Emits `tool.use_end` for the in-flight tool: `final_input = {}` (nothing parses cleanly from partial JSON).
3. Emits `message.complete` with `stop_reason: cancelled` and partial `final_content` (the 200 text deltas reconstructed plus the cancelled tool_use with empty input).
4. Stream iterator terminates.

Session manager handles the higher-level cancellation events per `streaming-protocol.md` §6.2.

---

## 10. Testing strategy

### 10.1 Required tests per adapter

1. **Round-trip canonical → wire → canonical.** Fixed canonical message list, serialize to wire (recorded HTTP cassette), parse response back, assert byte-equality with golden file.
2. **Tool call serialization.** Canonical `ToolUseBlock` with various input shapes (nested objects, arrays, all primitive types) → wire format → back to canonical → assert equality.
3. **Tool id round-trip.** Per-session id map: create canonical id, serialize to provider id, deserialize back, assert canonical id is preserved.
4. **System prompt placement.** Empty SYSTEM, single SYSTEM, multiple SYSTEMs concatenated → correct wire placement per provider.
5. **Capability declaration honesty.** For each capability flag, construct a request that requires that capability; if `false`, assert the adapter rejects or surfaces failure cleanly.
6. **Streaming event sequence.** Recorded stream cassette → emitted canonical events match the invariants in §5.2 (ordering, monotonic indices, exactly-one start/end per tool).
7. **Streaming partial JSON fidelity.** `tool.use_input_delta.partial_json` matches the raw provider fragment, not a parsed object.
8. **Error classification.** For each `ErrorClass`, construct a recorded response (HTTP status + body) and verify the correct class is raised.
9. **HTTP status edge cases.** 413, 429, 529, 401 with various body shapes — all classified correctly.
10. **Retry behavior.** Inject sequential transient errors (rate_limit, server_error, network); verify retry count, backoff timing (within tolerance), final outcome.
11. **`retry_after` honored.** 429 with retry-after header; verify adapter sleeps for the indicated duration before retry (capped at 60s).
12. **Cancellation cleanup.** Mid-stream cancel; verify tool.use_end with empty input for in-flight tool, message.complete with cancelled stop_reason.
13. **Cost-token mapping.** Provider response with all four token fields populated → `TokenUsage` matches.
14. **Streaming usage propagation.** Stream with `include_usage`; verify final `message.complete.usage` matches non-streaming equivalent.

### 10.2 Cross-adapter conformance

Beyond per-adapter tests, the contract is enforced by a cross-adapter conformance suite:

1. **Substitutability suite.** A fixed canonical conversation script is run through each adapter (with cassettes); the resulting canonical message list is identical across adapters (modulo metadata.model and provider-specific block-dropping).
2. **Mid-session swap.** A 6-turn conversation alternates Anthropic/OpenAI/Anthropic/OpenAI/Anthropic/OpenAI. Each turn's wire request includes the prior canonical history. Verify each adapter accepts the prior history regardless of which adapter produced earlier turns.
3. **Tool id consistency.** The per-session id map round-trips: a tool_use_id created by Anthropic is referenced in a subsequent OpenAI turn's tool_result; verify the provider id in OpenAI's wire matches what was earlier mapped.
4. **Error class consistency.** Equivalent failure conditions (e.g., bad API key) raise the same `ErrorClass` regardless of provider.
5. **Capability fail-cleanly.** Run a request requiring vision through a text-only configured model; verify capability validation rejects (per routing) before the adapter is called. (This is a routing test, listed here because it depends on adapter capability declarations being honest.)

### 10.3 Cassette discipline

HTTP cassettes are committed to the repo per canonical-format §11.2. Re-record when:

- Provider changes wire format.
- Adapter behavior changes intentionally.
- New test added.

Cassettes are reviewed in PRs the same as code.

---

## 11. Open questions

1. **OpenAI Responses API vs. Chat Completions.** OpenAI offers both. v1 uses Chat Completions for simplicity; the Responses API has different streaming and tool-call shapes. Migration is deferred — possibly Phase 2.
2. **Anthropic prompt-caching breakpoint placement.** Where to insert `cache_control` markers for optimal cache hits is a heuristic. v1 caches the system prompt and tool definitions only. Phase 2 may add session-history caching once we have data on access patterns.
3. **OpenAI structured-output `response_format`.** v1 uses for delegation only (when `output_schema` is set on `CanonicalRequest`). Other use cases (general structured agent output) deferred.
4. **Tool definition deduplication across providers.** When the same tool definition is sent on every turn, both providers handle it cheaply, but for cache efficiency the adapter could emit `tools` only on the first turn of a session. Deferred — premature optimization.
5. **Streaming reconnection at the adapter level.** If the underlying HTTP connection drops mid-stream, can the adapter resume? v1: no, the call fails and routing handles fallthrough. Both providers don't currently support stream resume.
6. **`response_format` validation.** When `output_schema` is set, OpenAI's `response_format: {type: "json_schema"}` enforces schema. Anthropic doesn't have an equivalent strict mode in the same way; the adapter currently passes the schema as a hint in the system prompt. Inconsistency worth flagging.
7. **Ollama and OpenRouter capability declarations.** Ollama-served models have wildly varying tool-call quality; the adapter config should let the user explicitly declare per-model capability (`supports_tools: false` for a specific local model). Not implemented; spec accommodates. The OpenRouter half of this question is resolved: the OpenRouter adapter ships and §4.5 pins how it detects per-model prompt-caching capability (from `/api/v1/models` pricing fields, no dedicated flag exists) and where it attaches `cache_control` breakpoints for explicit-caching upstreams.
8. **OpenRouter tool-definition cache breakpoints.** OpenRouter's OpenAI-shaped `/chat/completions` endpoint documents `cache_control` on message content parts only, not on `tools[]` entries (§4.5.3). Metis's system-tail breakpoint subsumes tool caching because Anthropic's cache-prefix walk runs `tools → system`, so this is not a blocker. But it means a hypothetical "cache tools without a system prompt" need could not be met on the chat-completions endpoint — it would require the Anthropic-Messages-shaped `/api/v1/messages` endpoint. Flagged, not pursued.

---

## 12. Decision log

| Date       | Decision                                                              | Rationale                                                                                  |
|------------|-----------------------------------------------------------------------|--------------------------------------------------------------------------------------------|
| 2026-05-08 | Adapters report token counts only; cost computed by core              | Pricing is a core concern; adapters stay simple; retroactive reprice possible.             |
| 2026-05-08 | Per-session bidirectional tool-id map maintained by adapter           | Cross-provider tool id consistency without provider-id pollution in canonical layer.       |
| 2026-05-08 | Bounded transient retry inside adapter; sustained failure to routing  | Hide trivial transient errors; escalate sustained patterns to routing's availability machine. |
| 2026-05-08 | Capability declarations are honest, not theoretical                   | Substitutability depends on declared capability matching actual implementation.            |
| 2026-05-08 | Lossy projection rules drop unrepresentable content with WARN log     | Mid-session swap remains resilient; observability over hard failure.                       |
| 2026-05-08 | Streaming partial JSON is raw fragments, not best-effort parsed       | Per streaming-protocol §5.6; provider-portable; clients render placeholder until tool.use_end. |
| 2026-05-08 | Cancellation emits `tool.use_end` with empty input for in-flight tools | Stream invariants (every start has an end) preserved even on cancel.                      |
| 2026-05-08 | Adapter registry separate from routing.yaml                            | Adapter config is per-installation; routing rules are per-user-policy. Different lifecycles. |
| 2026-05-08 | OpenAI thinking-block translation deferred; Anthropic→OpenAI loses thinking | Formats are too different for clean v1 mapping; documented asymmetry.                  |
| 2026-05-08 | Closed `ErrorClass` enum drives consistent classification             | Routing and analytics depend on uniform error semantics across providers.                  |
| 2026-05-21 | Aggregator adapters detect prompt-caching from `/api/v1/models` pricing fields + a maintained family allowlist (§4.5) | OpenRouter exposes no caching capability flag and `supported_parameters` omits `cache_control`; `pricing.input_cache_read` is the only machine signal and is incomplete, so a wire-id-prefix allowlist is required for breakpoint-style detection. |
| 2026-05-21 | OpenRouter explicit caching uses a single system-tail `cache_control` breakpoint, not a `tools[]` breakpoint or top-level auto-caching (§4.5.3) | `tools[]` cache_control is undocumented on the chat-completions endpoint; Anthropic's `tools → system` prefix walk means a system-tail breakpoint caches tools anyway. Top-level auto-caching caches volatile content and forces Anthropic-direct routing (excludes Bedrock/Vertex). |
| 2026-05-21 | OpenRouter adapter leaves the `provider` routing object unset on the caching path (§4.5.5) | OpenRouter's automatic provider sticky routing keeps caches warm *and* preserves failover; setting `provider.order`/`sort` disables sticky routing. |
| 2026-05-22 | OpenAI-wire adapters derive `input_tokens` as `prompt_tokens − cached − written` so the three `TokenUsage` input buckets stay disjoint (§4.5.4, §7.1, §7.2) | OpenAI/OpenRouter report `prompt_tokens` as the total (it already includes the cached span); the §7.1 cost formula sums the three buckets, so returning the total verbatim double-bills cache reads — caching was reported as *more* expensive than no caching. |

---

## 13. References

- `canonical-message-format.md` — `Message`, `ContentBlock`, `ToolDefinition`, `Usage`, `AdapterCapabilities`. The provider-id ↔ canonical-id mapping convention is in §6.2.
- `event-bus-and-trace-catalog.md` — `llm.call_started`, `llm.call_completed`, `llm.call_failed` payloads; `error_class` enum; provider availability events.
- `streaming-protocol.md` — canonical streaming events (`text.delta`, `tool.use_start`, etc.); cancellation contract.
- `routing-engine.md` — capability validation (§4.4); availability state machine (§4.5); retry vs. routing escalation boundary.
- `tool-dispatcher.md` (planned) — how `ToolUseBlock` outputs are dispatched after the adapter returns them.
- `server-api.md` (planned) — request lifecycle from API entry through adapter call.
