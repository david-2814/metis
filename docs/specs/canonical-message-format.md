# Canonical Message Format Specification

**Status:** Draft v1.1
**Last updated:** 2026-05-08
**Owner:** _your name_

> **v1.1 changes:** `AdapterCapabilities` extended with `supports_tools`,
> `supports_system_prompt`, `supports_structured_output` (§7.2). `block_dropped`
> downgraded from "trace event" to log-line only (§7.3), consistent with bus
> diagnostics. `provider_overrides` field removed from `ToolDefinition` (unused
> across all specs). `RoutingDecisionRecord.mode` mapping to routing-engine's
> chain enum documented (§4.3).

> *Throughout: paths shown use `~/.yourtool/` as a placeholder for the final config directory.*

---

## 1. Purpose

This document specifies the canonical internal representation of messages, content blocks, tool calls, and related metadata used throughout the system. All provider adapters (Anthropic, OpenAI, Ollama, OpenRouter, etc.) translate between this canonical format and their respective wire formats. All persistence layers store messages in this format. All routing, tooling, and analytics code consumes this format.

The canonical format is the single most load-bearing data contract in the system. Changes to it cascade everywhere; getting it wrong forces rewrites; getting it right makes mid-session model swapping, cross-provider replay, schema migration, and future provider additions tractable.

---

## 2. Goals and non-goals

### 2.1 Goals

1. **Lossless round-trip for primary providers.** A message produced by an Anthropic adapter, persisted, and re-serialized to Anthropic must be byte-equivalent (or semantically equivalent for ordering-insensitive fields).
2. **Survive mid-session model swaps.** Sessions started on provider A and continued on provider B must replay correctly without loss of conversational state.
3. **Single representation for memory and storage.** No separate "in-memory" vs "stored" form; the canonical format persists directly.
4. **Extensible to new providers.** Adding Gemini, Ollama, OpenRouter, or future providers requires a new adapter, not a schema change.
5. **Stable hashable shape.** Messages are deterministically hashable for caching, deduplication, and trace correlation.

### 2.2 Non-goals

1. **Be a wire format.** The canonical form is internal. Wire formats are always provider-specific.
2. **Capture every provider-specific feature.** Provider-only knobs (e.g., Anthropic's exact prompt-cache breakpoints, OpenAI's `logit_bias`) live in adapter-level options, not the canonical format.
3. **Be human-authorable.** It will be JSON when serialized, but it is a data format, not a config language. Humans interact with it via tooling.
4. **Match any provider's shape exactly.** Convergence with Anthropic or OpenAI is incidental. The canonical form is its own design.

---

## 3. Conceptual model

### 3.1 The content-blocks insight

Both Anthropic and OpenAI converged on representing messages as ordered lists of typed content blocks rather than as strings. The shape is:

```
Message := role + ordered list of ContentBlock
ContentBlock := tagged union over { text, tool_use, tool_result, image, thinking, ... }
```

The canonical format adopts this shape directly. Every message — including system messages and tool result messages — is a role + content list.

### 3.2 Tool results are messages

Anthropic places tool results inside a `user`-role message; OpenAI uses a dedicated `tool` role. Internally we treat tool results as their own first-class message type with `role: TOOL`. Adapters figure out which on-the-wire shape to emit.

This decision avoids leaky abstractions: the rest of the system (routing, tracing, evaluation) can ask "what role is this message?" without knowing about per-provider conventions.

### 3.3 System prompts are messages

Anthropic carries system prompts as a top-level request parameter; OpenAI uses a `system`-role message. Internally, system prompts are `role: SYSTEM` messages stored in the same message list. This lets context-assembly code compose system prompts (concatenate base instructions + memory + skills + workspace info) without coupling to one provider's API shape. Adapters handle hoisting.

---

## 4. Schema

### 4.1 Top-level types

```python
class Message:
    id: str                         # ULID, monotonic
    session_id: str                 # FK to Session
    role: Role
    content: list[ContentBlock]     # ordered, non-empty for non-system messages
    metadata: MessageMetadata
    created_at: datetime            # microsecond precision UTC
    schema_version: int             # current: 1

class Role(StrEnum):
    USER       = "user"
    ASSISTANT  = "assistant"
    SYSTEM     = "system"
    TOOL       = "tool"
```

### 4.2 Content blocks (closed set, tagged union on `type`)

```python
class TextBlock:
    type: Literal["text"] = "text"
    text: str

class ToolUseBlock:
    type: Literal["tool_use"] = "tool_use"
    id: str                         # canonical id we generate; see §6
    name: str                       # canonical tool name (snake_case, no provider prefix)
    input: dict                     # JSON-Schema-validated against tool definition

class ToolResultBlock:
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str                # FK to a ToolUseBlock.id
    content: list[ContentBlock]     # usually [TextBlock]; may include ImageBlock
    is_error: bool = False

class ImageBlock:
    type: Literal["image"] = "image"
    source: ImageSource
    media_type: str                 # IANA media type, e.g. "image/png"

class ImageSource:
    kind: Literal["base64", "url", "file_ref"]
    data: str                       # base64 string, URL, or workspace-relative path

class ThinkingBlock:
    type: Literal["thinking"] = "thinking"
    text: str
    signature: str | None = None    # opaque provider token (Anthropic uses this for verifiability)

class RedactedThinkingBlock:
    type: Literal["redacted_thinking"] = "redacted_thinking"
    data: str                       # opaque provider-encoded blob
```

#### 4.2.1 Why this set

- `text` and `tool_use`/`tool_result`: the conversational core. Universal.
- `image`: vision support. Both major providers support it; canonical form must too.
- `thinking` and `redacted_thinking`: extended-reasoning models (Anthropic specifically). If we don't preserve these, mid-session swap from a thinking model loses signal needed for replay.

#### 4.2.2 Adding new block types

New block types are additive only. They get a new `type` discriminator. Existing block types are never overloaded. If a future provider introduces audio or video as first-class content, those become `audio` and `video` blocks.

When an adapter encounters a canonical block type its provider can't represent, it MUST write a structured log entry at WARN level (not a bus event — consistent with how `bus.overflow` and `bus.handler_error` are handled per `event-bus-and-trace-catalog.md` §3.5 and §5.2) with: session_id, message_id, block type, adapter name, reason. The adapter then either drops the block (default) or fails the request (if marked critical via metadata). The block dropping is a lossy projection by design — see §7.

### 4.3 Message metadata

```python
class MessageMetadata:
    # Provenance — who/what produced this message
    model: str | None = None              # canonical model id, e.g. "anthropic:claude-sonnet-4-6"
    provider: str | None = None           # provider key, e.g. "anthropic"

    # Routing decision context (for ASSISTANT messages)
    routing: RoutingDecisionRecord | None = None

    # Resource accounting
    usage: Usage | None = None

    # Tool linkage (for TOOL messages)
    parent_tool_use_id: str | None = None

    # Status
    status: MessageStatus = MessageStatus.COMPLETE

    # Provider-specific opaque payload (round-trip aid; see §6.4)
    provider_raw: dict | None = None

class MessageStatus(StrEnum):
    COMPLETE  = "complete"
    PARTIAL   = "partial"             # streaming in progress
    CANCELLED = "cancelled"           # user cancelled mid-generation
    ERROR     = "error"               # generation failed

class RoutingDecisionRecord:
    """Compact summary of a routing decision attached to an assistant
    message's metadata. The full chain trace lives in the corresponding
    `route.decided` event (see event-bus-and-trace-catalog.md §6.5).

    The `mode` enum here is a coarse summary; it projects the routing
    engine's chain enum (per routing-engine.md §4.1) down to the
    user-facing classes most useful for analytics and `/model show`."""
    mode: RoutingMode                 # see RoutingMode below
    chosen_model: str                 # echoes metadata.model
    reason: str                       # human-readable
    rule_name: str | None = None      # for RULE mode
    confidence: float | None = None   # for PATTERN mode (0..1)
    alternatives_considered: list[str] = []

class RoutingMode(StrEnum):
    """Coarse summary of why this turn picked this model. The chain enum
    in routing-engine.md §4.1 is finer-grained; this projection is what
    persists on each assistant message. Mapping:

      Chain policy            → RoutingMode
      ─────────────────────────────────────
      PER_MESSAGE_OVERRIDE    → OVERRIDE
      MANUAL_STICKY           → MANUAL
      RULE                    → RULE
      PATTERN                 → PATTERN
      DELEGATE_REQUEST        → DELEGATE
      WORKSPACE_DEFAULT       → DEFAULT
      GLOBAL_DEFAULT          → DEFAULT

    WORKSPACE_DEFAULT and GLOBAL_DEFAULT collapse to DEFAULT because the
    user-facing 'why this model' rarely cares which level the default
    came from. Inspect the route.decided event for that detail.
    """
    OVERRIDE  = "override"     # per-message @model
    MANUAL    = "manual"       # session sticky
    RULE      = "rule"         # configured rule matched
    PATTERN   = "pattern"      # pattern store recommendation
    DELEGATE  = "delegate"     # planner delegation
    DEFAULT   = "default"      # workspace or global default

class Usage:
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cost_usd: Decimal                 # computed from local price table; see §6.3
    pricing_version: str              # FK to price table entry
    latency_ms: int
```

### 4.4 Tool definitions

Tool definitions are not messages but are referenced by `ToolUseBlock.name`. Defined here for completeness:

```python
class ToolDefinition:
    name: str                         # canonical, snake_case, globally unique
    description: str                  # used in system prompt when tool is exposed
    input_schema: dict                # JSON Schema (subset; see §5.4)
    side_effects: SideEffects
    requires_workspace: bool = True

class SideEffects(StrEnum):
    NONE     = "none"          # pure read or computation
    READ     = "read"          # filesystem or network read
    WRITE    = "write"         # filesystem write
    EXECUTE  = "execute"       # shell or arbitrary code execution
    NETWORK  = "network"       # outbound network mutation (POST, etc.)
```

`side_effects` drives confirmation prompts, routing constraints, and trace classification. Every tool MUST declare it honestly.

---

## 5. Invariants

These rules must hold at all times. Adapter implementations and core code violating them are bugs.

### 5.1 Message-level invariants

1. **Non-empty content for non-system messages.** USER, ASSISTANT, TOOL messages have at least one content block.
2. **System messages may be empty content.** A SYSTEM message with empty content is a valid placeholder (used during composition).
3. **Role-content compatibility:**
   - USER messages: TextBlock, ImageBlock allowed. No tool_use, no tool_result.
   - ASSISTANT messages: TextBlock, ToolUseBlock, ThinkingBlock, RedactedThinkingBlock allowed. No tool_result, no image.
   - TOOL messages: ToolResultBlock only. Exactly one block per message.
   - SYSTEM messages: TextBlock only.
4. **Content ordering matters.** Block order is preserved end-to-end and is semantically significant.
5. **`status: PARTIAL` messages may violate other invariants.** A streaming message in progress may have empty content, malformed tool input, etc. Validation runs only at `status: COMPLETE`.

### 5.2 Tool call invariants

1. **Tool ids are canonical.** Generated by the system at tool_use creation, not the provider's id. See §6.
2. **Every ToolResultBlock points to a ToolUseBlock that exists in the same session.** Tool ids never cross sessions.
3. **A ToolUseBlock has at most one ToolResultBlock answering it.** Retries get new tool ids.
4. **Tool input matches the tool's input_schema.** Validation happens at adapter ingress (parsing) and at dispatch (execution). Failures emit `tool_input_invalid` events.

### 5.3 Metadata invariants

1. **`model` and `provider` are set on every ASSISTANT message at `status: COMPLETE`.** Required for replay correctness and analytics.
2. **`routing` is set on every ASSISTANT message at `status: COMPLETE`.** Always traceable.
3. **`usage` is set on every ASSISTANT message at `status: COMPLETE`.** Cost accounting depends on this.
4. **`parent_tool_use_id` is set on every TOOL message.** Always non-null.
5. **`provider_raw` is opaque.** Core code never reads it; only the adapter that wrote it reads it on round-trip.

### 5.4 Tool input schema invariants

The `input_schema` of every ToolDefinition is a subset of JSON Schema:

- **Allowed:** basic types (`string`, `number`, `integer`, `boolean`, `null`, `object`, `array`), `enum`, `required`, `properties`, `items`, `description`, basic `format` annotations.
- **Disallowed:** `$ref`, `oneOf`, `anyOf`, `allOf`, `not`, `if`/`then`/`else`, `patternProperties`, `additionalProperties: <schema>` (boolean is OK).

Rationale: this is the intersection of what Anthropic and OpenAI accept reliably. Tools using disallowed constructs fail at registration time with a clear error.

---

## 6. Identifiers, models, and prices

### 6.1 Message ids

ULIDs. Monotonic per session. Generated by the core, never by adapters.

### 6.2 Tool call ids

System-generated, format: `tu_<ulid>`. The core generates these when an adapter parses a streaming `tool_use_start` event from any provider. Adapters maintain a per-session bidirectional map between canonical tool ids and provider-issued tool ids:

```
canonical_id  ↔  provider_id
"tu_01HZ..."  ↔  "toolu_01ABC..."   (Anthropic)
"tu_01HZ..."  ↔  "call_xyz789"      (OpenAI)
```

When serializing canonical → wire, the adapter looks up or generates a provider-side id. When parsing wire → canonical, it looks up the canonical id from the provider-side id.

This design lets canonical content survive provider switches: the model sees its own tool_use ids reflected back in tool_result content even after a session swaps providers.

### 6.3 Model identifiers

Canonical form: `<provider>:<model_name>`.

```
anthropic:claude-sonnet-4-6
anthropic:claude-opus-4-7
anthropic:claude-haiku-4-5
openai:gpt-5
openai:gpt-5-mini
ollama:llama-3.3-70b
openrouter:deepseek/deepseek-v3
```

Model ids appear in:
- `MessageMetadata.model`
- Routing rules
- Pricing table
- Adapter capability registry
- User-facing aliases (a separate alias table maps `sonnet → anthropic:claude-sonnet-4-6`)

### 6.4 Pricing table

Costs are computed by the core from a maintained price table, never parroted from the provider. Reasons:

- Providers don't always return cost in responses.
- Pricing changes; we want historical accuracy via `pricing_version`.
- Synthetic providers (Ollama for local, custom endpoints) need a defined cost (often 0, but explicit).
- OpenRouter prices vary per underlying provider; canonical accounting requires our own resolution.

Pricing table shape (illustrative):

```yaml
pricing_version: "2026-05-08"
models:
  anthropic:claude-sonnet-4-6:
    input_per_mtok_usd:  3.00
    output_per_mtok_usd: 15.00
    cached_read_per_mtok_usd: 0.30
    cache_write_per_mtok_usd: 3.75
  openai:gpt-5:
    input_per_mtok_usd:  2.50
    output_per_mtok_usd: 10.00
  ollama:llama-3.3-70b:
    input_per_mtok_usd:  0.0
    output_per_mtok_usd: 0.0
```

`MessageMetadata.usage.pricing_version` records which version was active when the cost was computed. Retroactive reprice is possible by walking the trace store.

### 6.5 The `provider_raw` field

Round-trip aid. When an adapter parses a provider response into canonical form, it MAY stash adapter-specific data in `provider_raw` — for example, Anthropic's exact `signature` on a thinking block, or stop-reason strings. When that adapter later serializes the message back to the same provider, it consults `provider_raw` if present.

Rules:
- Only the adapter that wrote `provider_raw` reads it.
- Cross-provider serialization ignores `provider_raw` entirely.
- Core code never inspects `provider_raw` for any reason.
- `provider_raw` is not part of equality comparisons or hashing.

This is the escape hatch that handles provider-specific quirks without polluting the canonical schema.

---

## 7. Adapter contract

### 7.1 Required operations

Every adapter implements:

```python
class Adapter:
    capabilities: AdapterCapabilities

    def to_wire(self, messages: list[Message], tools: list[ToolDefinition], options: AdapterOptions) -> WireRequest:
        """Serialize canonical messages to provider wire format."""

    def from_wire_response(self, response: WireResponse, request_context: RequestContext) -> Message:
        """Parse a non-streaming response into a canonical Message."""

    async def stream_response(self, response_stream: AsyncIterator[WireChunk], context: RequestContext) -> AsyncIterator[StreamEvent]:
        """Translate provider stream chunks into canonical StreamEvents."""

    def estimate_input_tokens(self, messages: list[Message], tools: list[ToolDefinition]) -> int:
        """Pre-flight estimate for routing and budget decisions."""
```

### 7.2 Capability declaration

```python
class AdapterCapabilities:
    # Content type support
    supports_thinking: bool
    supports_images: bool
    supports_tools: bool                 # added v1.1; required by routing-engine §4.4
    supports_system_prompt: bool         # added v1.1; required by routing-engine §4.4
    supports_structured_output: bool     # added v1.1; required by routing-engine §4.4

    # Streaming
    supports_streaming: bool
    supports_streaming_tool_calls: bool
    supports_parallel_tool_calls: bool

    # Caching
    supports_prompt_caching: bool        # added v1.1; matches provider-adapter §3.4

    # System prompt placement (provider-specific quirk)
    supports_system_messages_in_list: bool   # vs hoisted

    # Limits
    max_context_tokens: int
    max_output_tokens: int

    # Image format support (only meaningful if supports_images)
    accepted_image_media_types: list[str]
```

Capability declarations are consulted by:
- Routing engine (capability validation during routing — `routing-engine.md` §4.4).
- Session manager (pre-swap validation when user changes models mid-session).
- Context assembler (image handling, thinking-block preservation).

Declarations MUST be honest. If a model technically supports a feature but the adapter implementation doesn't expose it, declare `false`. The capability surface is the substitutability boundary; lying about it breaks mid-session swaps.

The three fields added in v1.1 (`supports_tools`, `supports_system_prompt`, `supports_structured_output`) are required for honest validation of turns that genuinely need those capabilities — without them, a turn with tools could be routed to a tool-incapable model and fail at the adapter rather than cleanly fall through during routing.

### 7.3 Lossy projection rules

When canonical content cannot be represented in a provider's wire format, the adapter MUST:

1. Drop the unrepresentable content (default).
2. Write a structured log entry at WARN level with: session_id, message_id, block type, adapter, reason. (Not a bus event — bus diagnostics like this are log-only per `event-bus-and-trace-catalog.md` §3.5.)
3. Never silently corrupt — better to drop cleanly than partially serialize.

Examples:
- `ThinkingBlock` sent to OpenAI: dropped, logged.
- `ImageBlock` sent to a text-only model: dropped, logged. (Routing should prevent this from happening; this is the safety net.)
- `RedactedThinkingBlock` cross-provider: dropped, logged.

### 7.4 Streaming events

Adapters translate provider-specific stream chunks into canonical stream events:

```python
class StreamEvent:  # tagged union, type discriminator
    type: Literal["text_delta", "tool_use_start", "tool_use_input_delta",
                  "tool_use_end", "thinking_delta", "message_complete",
                  "usage_update", "error"]
    # type-specific payload fields
```

Specifics live in the streaming protocol spec (companion document). The canonical event shape is stable across providers.

---

## 8. Worked examples

### 8.1 Simple text exchange

User asks a question, assistant answers. Two messages.

```python
[
  Message(
    id="01HZ001",
    session_id="sess_42",
    role=USER,
    content=[TextBlock(text="What's a ULID?")],
    metadata=MessageMetadata(),
    created_at=...,
    schema_version=1,
  ),
  Message(
    id="01HZ002",
    session_id="sess_42",
    role=ASSISTANT,
    content=[TextBlock(text="A ULID is a 128-bit identifier...")],
    metadata=MessageMetadata(
      model="anthropic:claude-sonnet-4-6",
      provider="anthropic",
      routing=RoutingDecisionRecord(
        mode=DEFAULT,
        chosen_model="anthropic:claude-sonnet-4-6",
        reason="workspace default",
      ),
      usage=Usage(
        input_tokens=8,
        output_tokens=42,
        cost_usd=Decimal("0.000654"),
        pricing_version="2026-05-08",
        latency_ms=820,
      ),
    ),
    ...
  ),
]
```

### 8.2 Tool call round trip

User asks the assistant to read a file. Assistant calls a tool, tool returns content, assistant responds.

```python
[
  # Turn 1: user message
  Message(
    role=USER,
    content=[TextBlock(text="Summarize README.md")],
    ...
  ),

  # Turn 1: assistant uses tool
  Message(
    role=ASSISTANT,
    content=[
      TextBlock(text="I'll read the file."),
      ToolUseBlock(
        id="tu_01HZ100",
        name="read_file",
        input={"path": "README.md"},
      ),
    ],
    metadata=MessageMetadata(
      model="anthropic:claude-sonnet-4-6",
      provider="anthropic",
      routing=...,
      usage=...,
    ),
  ),

  # Turn 1: tool result
  Message(
    role=TOOL,
    content=[
      ToolResultBlock(
        tool_use_id="tu_01HZ100",
        content=[TextBlock(text="# Project Foo\n\nA tool for...")],
      ),
    ],
    metadata=MessageMetadata(
      parent_tool_use_id="tu_01HZ100",
    ),
  ),

  # Turn 1: assistant final response (same turn, second LLM call)
  Message(
    role=ASSISTANT,
    content=[TextBlock(text="The README describes Project Foo, a tool for...")],
    metadata=MessageMetadata(
      model="anthropic:claude-sonnet-4-6",
      provider="anthropic",
      ...
    ),
  ),
]
```

### 8.3 Mid-session model swap

After turn 2, user runs `/model openai:gpt-5`. Turn 3 uses the new model.

```python
[
  # Turns 1-2 use anthropic (as above) ...

  # Turn 3: user message
  Message(role=USER, content=[TextBlock(text="What about the LICENSE file?")], ...),

  # Turn 3: assistant — different provider, same canonical form
  Message(
    role=ASSISTANT,
    content=[
      ToolUseBlock(
        id="tu_01HZ200",          # new canonical id
        name="read_file",
        input={"path": "LICENSE"},
      ),
    ],
    metadata=MessageMetadata(
      model="openai:gpt-5",
      provider="openai",
      routing=RoutingDecisionRecord(
        mode=MANUAL,
        chosen_model="openai:gpt-5",
        reason="user swap via /model command",
      ),
      ...
    ),
  ),
]
```

The OpenAI adapter, when serializing this list to GPT-5's wire format:
- Hoists no system message (none in this list).
- Translates each TOOL message to a `role: tool` message with `tool_call_id` mapped from the canonical id via the per-session id map.
- Converts ToolUseBlock to OpenAI's `tool_calls` array with `function.arguments` as JSON-stringified input.
- Drops nothing in this example. (If turns 1–2 had ThinkingBlocks from Anthropic, those would be dropped at serialization with WARN-level log entries — see §7.3.)

### 8.4 Capability mismatch on swap

User has images in turn 3 history. User attempts `/model anthropic:claude-haiku-4-5-text-only` (hypothetical).

Adapter capability check fails: `supports_images: false`, but history contains ImageBlocks.

Routing engine refuses the swap. Emits `routing_constraint_failure` event. TUI shows: `Cannot swap to Haiku-text-only: session contains images that this model can't process. Active model unchanged.`

---

## 9. Persistence

### 9.1 SQLite schema

```sql
CREATE TABLE sessions (
  id TEXT PRIMARY KEY,
  workspace_path TEXT NOT NULL,
  active_model TEXT,
  routing_policy_json TEXT,
  schema_version INTEGER NOT NULL,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE TABLE messages (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  role TEXT NOT NULL,
  content_json TEXT NOT NULL,
  metadata_json TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  schema_version INTEGER NOT NULL,
  FOREIGN KEY (session_id) REFERENCES sessions(id)
);
CREATE INDEX idx_messages_session_created ON messages(session_id, created_at);

CREATE TABLE tool_calls (
  id TEXT PRIMARY KEY,                  -- canonical tool_use id
  session_id TEXT NOT NULL,
  message_id TEXT NOT NULL,             -- the ASSISTANT message containing the tool_use
  result_message_id TEXT,               -- the TOOL message answering it (nullable)
  name TEXT NOT NULL,
  status TEXT NOT NULL,                 -- pending | succeeded | failed | cancelled
  provider_id TEXT,                     -- the provider's id at the time of generation
  provider TEXT,
  created_at INTEGER NOT NULL,
  completed_at INTEGER,
  FOREIGN KEY (session_id) REFERENCES sessions(id),
  FOREIGN KEY (message_id) REFERENCES messages(id),
  FOREIGN KEY (result_message_id) REFERENCES messages(id)
);
CREATE INDEX idx_tool_calls_session_status ON tool_calls(session_id, status);
```

### 9.2 Storage rationale

- **Content as JSON in a single column.** Block structure is heterogeneous; queries are always "give me a session's messages in order"; updates rewrite whole messages anyway. Normalizing into block tables would add complexity without query benefit.
- **`tool_calls` denormalized.** "Find unanswered tool calls" and "count failed tools per session" are common queries that would require JSON extraction otherwise.
- **No FTS5 on messages in v1.** Search over message text comes via the trace store + dedicated search tooling. Adding FTS5 on `messages.content_json` is reserved for a future schema bump if needed.

---

## 10. Versioning

### 10.1 Schema version

`Message.schema_version` records which version of this spec the row conforms to. Current: `1`.

### 10.2 Evolution rules

- **Additive changes (new optional fields, new content block types, new role values):** minor revision, no version bump on existing rows. New rows use the current version.
- **Breaking changes (renamed fields, changed semantics, removed fields):** major version bump. Migration code reads old rows, transforms to new shape, writes back.
- **Migrations run on read.** A row at v1 is transformed to v2 when read; written back at v2 only on explicit save.

### 10.3 Forward compatibility

Code reading messages MUST tolerate unknown fields in metadata and unknown content block types (skip with warning rather than crash). This protects against schema drift during partial deployments.

---

## 11. Testing strategy

### 11.1 Required tests

1. **Round-trip per provider.** For each adapter: fixed canonical message list → wire format → recorded HTTP cassette → wire response → canonical form → byte equality with golden file.
2. **Cross-provider continuity.** Session of N turns alternating between providers. Persist, reload, verify canonical form. Continue from turn N+1 on a third provider; verify no exceptions, all tool ids resolve.
3. **Capability mismatch.** Swap to a model whose capabilities don't match history. Verify swap is rejected with clear error.
4. **Schema migration.** Write a v1 message, change schema to v2, read — verify migration runs and result is correct.
5. **Tool schema validation.** Register a tool with a schema using disallowed JSON Schema constructs; verify registration fails loudly.
6. **Lossy projection logging.** Send a ThinkingBlock through OpenAI adapter; verify a WARN-level log entry is written with the correct fields (session_id, message_id, block type, adapter name, reason). Verify no bus event is emitted for this case.
7. **Cost computation.** Run a known conversation with mocked provider responses; verify computed cost matches expected to-the-cent against the price table.

### 11.2 Cassette discipline

HTTP cassettes (via `pytest-recording` or `vcr.py`) are committed to the repo. Re-record when:
- Provider changes wire format.
- Adapter behavior changes intentionally.
- New test added.

Cassettes are reviewed in PRs the same as code.

---

## 12. Open questions

The following are deliberately deferred and tracked here:

1. **Streaming partial-input parsing.** Should adapters attempt best-effort JSON parse on streaming tool inputs, or always emit raw partial strings? Decision deferred to streaming spec; impacts adapter complexity and TUI rendering.
2. **Multi-modal beyond images.** Audio and video input/output not in v1. When added, will be new content block types per §4.2.2.
3. **Embedding the canonical format in the agent's own context.** Whether to expose canonical message ids and tool_use ids to the agent as observable context (vs. hide them behind opaque references) is undecided. Affects whether the agent can reason about past tool calls by id.
4. **Compression of long-context history.** When approaching context window limits, history is summarized. Whether the summary is itself a Message (with what role?) or a separate session-level artifact is undecided.
5. **`provider_raw` size and retention.** Currently unbounded. May need a size cap or TTL if it grows pathologically.

---

## 13. Decision log

| Date       | Decision                                            | Rationale                                                                                            |
|------------|-----------------------------------------------------|------------------------------------------------------------------------------------------------------|
| 2026-05-08 | TOOL as first-class role                            | Avoids leaky abstractions; routing/tracing don't need to know per-provider role conventions          |
| 2026-05-08 | SYSTEM as first-class role                          | Lets context assembly compose system prompts without coupling to provider API shape                  |
| 2026-05-08 | Generated tool ids, not provider ids                | Tool calls survive cross-provider replay; canonical id stable across the session lifetime            |
| 2026-05-08 | `provider_raw` opaque escape hatch                  | Handles per-provider quirks without polluting canonical schema or making it lossy on round-trip      |
| 2026-05-08 | JSON Schema subset for tool input                   | Intersection of what Anthropic and OpenAI reliably accept; fail loud rather than ship-then-discover  |
| 2026-05-08 | Cost computed locally, not parroted                 | Pricing changes; some providers don't return cost; need historical accuracy via `pricing_version`    |
| 2026-05-08 | Content as JSON in single column                    | Query patterns are session-ordered reads; normalization adds complexity without benefit at our scale |
| 2026-05-08 | Lossy projection writes WARN logs, never crashes; not a bus event | Mid-session swap must remain resilient; observability over hard failure; consistent with bus diagnostics. |
| 2026-05-08 | `AdapterCapabilities` extended (`supports_tools`, `supports_system_prompt`, `supports_structured_output`, `supports_prompt_caching`) | Required by routing-engine §4.4 capability validation; substitutability needs honest declaration of these. |
| 2026-05-08 | `provider_overrides` removed from `ToolDefinition`                    | Field was unused across all specs; removing rather than carrying dead surface area.        |
| 2026-05-08 | `RoutingDecisionRecord.mode` is a coarse summary; chain enum lives in event | Persisted message metadata stays compact; full chain accessible via the `route.decided` event. |

---

## 14. References

- Anthropic Messages API: content blocks, tool use, system parameter handling.
- OpenAI Chat Completions API: tool_calls, role: tool, function arguments as JSON strings.
- ULID specification: monotonic, sortable, 128-bit identifiers.
- Companion specs (planned):
  - Event Bus and Trace Event Catalog
  - Streaming Protocol
  - Routing Engine
  - Skill Format
