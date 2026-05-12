# AGENTS.md

Shared context for any AI agent working in this repo (Claude Code, Cursor, Codex, Aider, etc.). `CLAUDE.md` is a symlink to this file.

## What Metis is

A local-first AI dev agent server. Provider-agnostic via a canonical message format, with a layered routing engine (manual / configured rules / learned patterns), bounded portable memory, and an event bus that feeds a trace store. Python core server; thin clients (CLI first, TUI/Tauri later).

**Status:** Phase 1 + initial Phase 2 wedges shipped. The CLI (`metis chat <workspace>`) drives end-to-end turns across Anthropic / OpenAI / OpenRouter with tool use, streaming, cost tracking, and event tracing. Bounded MEMORY.md / USER.md gives the agent cross-session continuity. `metis serve` exposes an HTTP/WebSocket surface for external clients with live token-delta streaming. 458 tests passing.

## Implementation status

**What works:**

- **Canonical types** ([src/metis/canonical/](src/metis/canonical/)) — `Message`, `Role`, content blocks (Text, Image, ToolUse, ToolResult, Thinking, RedactedThinking), `ToolDefinition`, `AdapterCapabilities`, `Usage`, `RoutingDecisionRecord`. `msgspec`-backed, JSON-roundtrippable, fully validated against canonical-format §5.
- **Event bus + trace store** ([src/metis/events/](src/metis/events/), [src/metis/trace/](src/metis/trace/)) — full catalog (24 event types incl. memory.updated/memory.eviction), bounded async dispatch, fast-path / non-fast-path subscribers, SQLite WAL+NORMAL writer, replay query, causal-chain walk.
- **Tool dispatcher** ([src/metis/tools/](src/metis/tools/)) — registry + JSON-Schema validation + confirmation policy + 5 file/shell builtins + 3 memory tools. Workspace-scoped file API rejects `..` and out-of-root symlinks.
- **Provider adapters** — Anthropic ([adapters/anthropic.py](src/metis/adapters/anthropic.py)), OpenAI ([adapters/openai.py](src/metis/adapters/openai.py)), OpenRouter ([adapters/openrouter.py](src/metis/adapters/openrouter.py)). Each implements wire translation, error classification (8-class), bounded retry with `retry_after`, cancellation, per-model `AdapterCapabilities`, and `stream()` returning canonical streaming events. Cross-provider continuity verified end-to-end against real APIs (Anthropic→OpenAI→OpenRouter mid-session with tool-use round-trip).
- **Routing engine** ([src/metis/routing/](src/metis/routing/)) — full 7-slot chain (per-message override / manual sticky / rule [stub] / pattern [stub] / delegate [stub] / workspace default / global default) with capability validation and provider availability tracking. Emits exactly one `route.decided` event per turn, including on hard failure.
- **Session manager** ([src/metis/sessions/manager.py](src/metis/sessions/manager.py)) — turn-locked streaming model loop, multi-call within a turn, tool cycle wiring, cost stamping, full event emission, optional streaming-event handler for live token deltas.
- **Bounded memory** ([src/metis/memory/](src/metis/memory/)) — per-workspace `MEMORY.md` (~2 KB) and `USER.md` (~1.5 KB) under `.metis/`. Soft cap → `memory.eviction` event; hard cap → write rejected. `memory_add`/`memory_replace`/`memory_consolidate` tools mutate them. `SessionManager` composes USER.md + MEMORY.md into the system prompt fresh each LLM call.
- **Session message persistence** ([src/metis/sessions/sqlite_store.py](src/metis/sessions/sqlite_store.py)) — SQLite-backed session/message store per `canonical-message-format.md §9.1`. Wired by default in the CLI runtime; `InMemorySessionStore` remains for tests.
- **HTTP/WebSocket server** ([src/metis/server/](src/metis/server/)) — Starlette + uvicorn ASGI app. REST endpoints for sessions/turns/messages/models/health; WebSocket `/sessions/{id}/stream` with single-use attach tokens, snapshot+live, `preset:chat`/`preset:full` filters, cancel-via-WS, ping/pong. Streaming-only events (`message.*`, `text.delta`, `tool.use_*`) flow through a per-session `StreamingHub` and reach connected clients live; bus catalog events flow through the normal Subscription path. Loopback-only bind in v1.
- **CLI** ([src/metis/cli/](src/metis/cli/)) — `metis chat <workspace>` (line REPL), `metis tui <workspace>` (Textual TUI), `metis serve <workspace>` (HTTP/WS server). Slash commands `/model`, `/cost`, `/models`, `/help`. Per-message `@alias` override syntax.
- **Smoke harnesses** ([scripts/smoke.py](scripts/smoke.py), [scripts/smoke_cross_provider.py](scripts/smoke_cross_provider.py)) — drive the loop against real APIs; cross-provider test passes at ~$0.007.

**What's NOT built (next-up):**

- **Configured routing rules** — yaml policy file is not parsed (`routing-engine.md §5` is spec, Phase 2 implementation). The `rule` slot in `route.decided.chain` still reports `not_applicable`.
- **Skills, pattern store, delegation** — Phase 2.5 / 3 / 4.
- **Tool-confirmation REST endpoint** — `POST /turns/{id}/confirmations/{request_id}` (server-api §4.2) isn't wired; the dispatcher still uses `AutoAllowHandler`. Needs a request registry + bridge to the WS-emitted `tool.confirmation_requested` event before it becomes useful.
- **Routing policy version surfacing** — `GET /sessions/{id}` returns `routing_policy_version: null` because there is no policy file yet.
- **Worker sessions / delegation** — `include_worker_sessions` in the WS subscribe filter is accepted but no workers are spawned in v1.
- **`AutoAllowHandler` is the default confirmation handler** — auto-approves *everything* including WRITE/EXECUTE/NETWORK. Fine for single-user dev, unsafe for production / shared use.

## Read before reasoning about the system

The design is specified before code lands. In order of load-bearing-ness:

1. [docs/project-overview.md](docs/project-overview.md) — vision, principles, architecture, phasing.
2. [docs/specs/canonical-message-format.md](docs/specs/canonical-message-format.md) — the foundational data contract; every other spec depends on it.
3. [docs/specs/event-bus-and-trace-catalog.md](docs/specs/event-bus-and-trace-catalog.md) — observability spine, closed event catalog.
4. [docs/specs/routing-engine.md](docs/specs/routing-engine.md) — model selection pipeline, delegation.
5. [docs/specs/streaming-protocol.md](docs/specs/streaming-protocol.md) — WebSocket protocol (v1 subset implemented; see "What's NOT built" for gaps).
6. [docs/specs/provider-adapter-contract.md](docs/specs/provider-adapter-contract.md), [docs/specs/tool-dispatcher.md](docs/specs/tool-dispatcher.md), [docs/specs/server-api.md](docs/specs/server-api.md) — component contracts.
7. [docs/specs/CHANGES.md](docs/specs/CHANGES.md) — cross-spec change log; check `pending review` entries before editing dependent specs.

For competitive landscape and prior art: [docs/market-research/synthesis.md](docs/market-research/synthesis.md) and the four per-stream reports alongside it.

## Architecture and package layering

Dependency direction (lower can be imported by higher, never the other way):

```
canonical      ←  events, trace, tools, adapters, routing, pricing, memory, sessions, server, cli
events         ←  trace, tools, adapters, routing, sessions, server
trace          ←  cli (only the wiring layer composes them)
adapters       ←  pricing, sessions, server, cli
tools          ←  memory, sessions, cli
memory         ←  sessions, cli
routing        ←  sessions, cli
pricing        ←  sessions, cli
sessions       ←  server, cli
server         ←  cli
```

`canonical` is the foundation; anything can import from it. Nothing in `canonical` imports from any other Metis package. `cli` is the top — it composes a `ChatRuntime` that everything else consumes. The `server` package sits just below `cli`: it accepts a `ChatRuntime` (built by `cli/runtime.py`) and exposes it over HTTP/WebSocket. When adding code, respect this direction — a circular import means a missing abstraction.

## Working norms

- **Specs-first.** Don't propose implementation changes that contradict the specs without flagging the spec impact. If a spec needs to change, draft the spec change first, then the code.
- **Cross-spec discipline.** When a spec change touches a contract, add an entry to `docs/specs/CHANGES.md` (date, change, type, references to verify, status). See the file's header for the format.
- **Solo, part-time owner.** One engineer, ~part-time pace. Scope decisions should favor what one person can land and maintain, not what a team could.
- **Bounded memory is a feature.** `MEMORY.md` and `USER.md` are intentionally small (~2 KB / ~1.5 KB). Don't propose unbounded growth — the eviction is the point.

## Implementation conventions

- **`msgspec.Struct(frozen=True)`, not Pydantic.** Canonical types use msgspec with tagged unions for discriminated content. See [canonical/content.py](src/metis/canonical/content.py).
- **`next_monotonic_ulid()`** from [canonical/ids.py](src/metis/canonical/ids.py). Raw `ULID()` from `python-ulid` is NOT strictly monotonic within a millisecond; the spec requires monotonic, so we wrap it with a process-wide lock that bumps the integer value on tie. Any new id generator must use it.
- **Tool factories, not instances.** `ToolDispatcher.register(ReadFileTool)` — pass the class. The dispatcher instantiates a fresh tool per call to prevent shared state across concurrent dispatches. The test suite enforces this.
- **Typed event payloads + dict envelope.** Each event type has a typed `msgspec.Struct` payload in [events/payloads.py](src/metis/events/payloads.py); `Event.payload` is `dict`. Use `make_event(type=..., payload=TypedStruct(...))` to bridge — it validates the type↔payload binding and converts.
- **`CanonicalResponse` returns content, not Message.** The adapter doesn't know the routing decision or the cost; the caller (`SessionManager`) assembles the full `Message` with metadata. This is a documented deviation from `provider-adapter-contract.md` §3.3 — see the file docstring at [adapters/protocol.py](src/metis/adapters/protocol.py).
- **Async fixtures.** Test fixtures that call `bus.start()` (which uses `asyncio.create_task`) must be `async def` so they run with an event loop. See [tests/tools/test_dispatcher.py](tests/tools/test_dispatcher.py).
- **Cost is `Decimal`, not float.** `Usage.cost_usd` and `PriceTable.compute_cost` use `Decimal` to avoid drift on cent-level math. `pricing_version` is recorded with every cost record so historical traces can be re-priced.
- **Workspace path security.** Any tool that touches files goes through `WorkspaceFileAPI`. `..` segments resolve during checking; symlinks pointing outside the root are rejected. Do not bypass it.

## Adding a new X

**New event type:**

1. Define a `msgspec.Struct(frozen=True)` payload in [src/metis/events/payloads.py](src/metis/events/payloads.py).
2. Add `"my.event": (MyPayload, Sensitivity.PSEUDONYMOUS)` to `PAYLOAD_REGISTRY`.
3. Update `docs/specs/event-bus-and-trace-catalog.md` §6 with the payload schema and sensitivity.
4. Log to `docs/specs/CHANGES.md` (additive vs breaking; which other specs to verify).
5. Tests cover the round-trip and the catalog registry membership.

**New built-in tool:**

1. Implement the `Tool` protocol from [tools/protocol.py](src/metis/tools/protocol.py) — `definition: ToolDefinition`, `async execute(input, context)`, `async cancel()`.
2. Schema must conform to the JSON Schema subset in [canonical/tools.py](src/metis/canonical/tools.py) — no `$ref`, `oneOf`, `anyOf`, `allOf`, `not`, etc.
3. Workspace-scoped file ops go through `context.workspace_files: WorkspaceFileAPI`.
4. Register via `register_builtins()` in [tools/builtins/__init__.py](src/metis/tools/builtins/__init__.py) if it's a default tool.

**New provider adapter:**

1. Implement the `ProviderAdapter` Protocol from [adapters/protocol.py](src/metis/adapters/protocol.py).
2. Wire translation: see [adapters/anthropic.py](src/metis/adapters/anthropic.py) for the pattern — system hoist, TOOL→user-with-tool_result merge, tool id map. `ToolIdMap` becomes load-bearing when providers don't accept canonical ids verbatim (OpenAI's `call_*` ids).
3. Error classification: implement a `classify_<provider>_response()` and return via `error_for_class()` with the correct `ErrorClass`.
4. Declare per-model `AdapterCapabilities` honestly (false ≥ unsupported); routing's validation gate trusts these.
5. Register via `ModelRegistry.register(model_id=..., adapter=..., aliases=...)`.

## Gotchas (things that will surprise you)

- **Streaming events are a separate layer**, not bus catalog events. `message.start`, `text.delta`, `tool.use_*`, `message.complete` exist in `streaming-protocol.md §5.3` but are NOT in `PAYLOAD_REGISTRY`. The streaming server has two input channels: the bus bridge (catalog events, persisted) via `EventBus.subscribe`, and a direct channel from the agent loop via [server/hub.py](src/metis/server/hub.py) (streaming events, transient). The trace store only sees the former.
- **Turn-locked model.** The model chosen at turn start owns every LLM call in the turn, including tool cycles. Routing only re-runs at turn boundaries. Don't add code paths that re-route mid-turn — it breaks cost predictability and the `route.decided` invariant.
- **Stub policies** (`rule`, `pattern`, `delegate_request`) appear in `route.decided.chain` with `verdict: "not_applicable"`. That's correct, not a bug. They'll be filled in in Phase 2 / 2.5 / 4.
- **Provider availability is per-provider, not per-(provider, model).** The spec acknowledges this is deferred (`routing-engine.md §11`). A 401 on opus marks the whole anthropic provider unavailable.
- **Memory is per-session, not per-process.** Each `SessionManager.create_session` builds a fresh `MemoryStore` via the injected `memory_factory`. The on-disk files (`<workspace>/.metis/{MEMORY.md,USER.md}`) are shared across sessions in the same workspace, but each session's store reads them fresh.
- **Memory writes don't auto-truncate.** Soft-cap overflow emits `memory.eviction` as a signal; hard-cap overflow rejects the write so the agent has to `memory_consolidate`. The eviction is the spec's intended user-visible action, not silent garbage collection.
- **`AutoAllowHandler` auto-approves everything** including writes and shell. Phase 1 single-user is fine; do not ship to anywhere shared without swapping in a real confirmation handler.
- **PARTIAL message bypass.** `validate_message()` skips invariant checks when `metadata.status == PARTIAL` (canonical-format §5.1.5). Mid-stream messages are intentionally allowed to violate role-content rules.
- **Server binds loopback-only in v1.** `metis serve --host 0.0.0.0` is silently rewritten to `127.0.0.1`. This is a v1 safety guarantee per server-api.md §3.1.
- **Attach tokens are single-use, 60-second TTL.** Each `GET /sessions/{id}` mints fresh; the WebSocket consumes it on upgrade. Reconnects need a new HTTP roundtrip.

## Non-obvious external context

- **LiteLLM is *not* safe as the canonical internal representation.** It has live bugs around `tool_use`, `cache_control`, and thinking-block translation. Write per-vendor adapters against canonical types; use LiteLLM only as an optional egress proxy if at all.
- **agentskills.io is a verified open standard** (Anthropic-originated, multiple implementers). When designing the skill format, conform to it; don't invent fields.
- **Letta is the bounded-memory peer worth studying** (Series A; ships core/archival/recall memory plus agent self-edit tools). Reference its mechanics before redesigning memory primitives.

## Stack and conventions

- **Python 3.13**, `uv` for env and build (`uv_build` backend, see `pyproject.toml`).
- **`msgspec`** for canonical types (not Pydantic).
- **`anthropic`** SDK + `httpx` for the provider adapter.
- **`jsonschema`** (Draft 7) for tool input validation.
- **`python-ulid`** wrapped with `next_monotonic_ulid()` for monotonic per-process ids.
- **Ruff** lint+format: line length 100, rules `E,F,I,B,UP,RUF`, `E501` ignored. Avoid the `×` multiplication sign in strings (RUF001 flags it as ambiguous).
- **pytest** with `pytest-asyncio` in auto mode; tests live in `tests/` mirroring `src/metis/`.
- **mypy** for typing.

## Running things

```bash
# Tests (458 currently)
uv run pytest

# Lint + format
uv run ruff check src tests
uv run ruff format src tests

# CLI (requires at least one of ANTHROPIC_API_KEY / OPENAI_API_KEY / OPENROUTER_API_KEY)
uv run metis chat .
uv run metis chat /path/to/workspace --model haiku
uv run metis tui /path/to/workspace
uv run metis serve /path/to/workspace --port 8421

# Real-API smoke tests
uv run python scripts/smoke.py --model haiku                # ~$0.015 / 2-turn run
uv run python scripts/smoke_cross_provider.py               # ~$0.007, mid-session provider switch
```

`.env` (gitignored) is the recommended place for API keys. Default SQLite path is `~/.metis/metis.db` (holds trace events + sessions/messages; override with `--db-path`).

## Repo layout

- `docs/specs/` — load-bearing specs (read first).
- `docs/market-research/` — competitive landscape, verified 2026-05-09.
- `src/metis/` — implementation:
  - `canonical/` — foundational types (Messages, ContentBlocks, ToolDefinition, AdapterCapabilities).
  - `events/` — bus + envelope + payload catalog (24 types).
  - `trace/` — SQLite WAL writer + replay query.
  - `tools/` — dispatcher + workspace API + confirmation policy + 5 file/shell builtins.
  - `memory/` — `MemoryStore` (byte-budgeted MEMORY.md/USER.md) + 3 memory tools.
  - `adapters/` — `ProviderAdapter` protocol + Anthropic / OpenAI / OpenRouter implementations + retry + tool id map.
  - `routing/` — registry + availability + chain + per-message override parser.
  - `pricing/` — `PriceTable` with Decimal cost computation + overlay versioning.
  - `sessions/` — `Session`, `InMemorySessionStore`, `SqliteSessionStore`, `SessionManager` (the streaming turn loop).
  - `server/` — Starlette ASGI app: HTTP REST + WebSocket streaming + per-session `StreamingHub` + attach-token registry + `TurnExecutor`.
  - `cli/` — argparse entry, REPL, runtime composer, `serve` entry.
  - `tui/` — Textual TUI.
- `tests/` — pytest suite mirroring `src/metis/`. 458 tests across 11 packages.
- `scripts/smoke.py`, `scripts/smoke_cross_provider.py` — live-API end-to-end harnesses.
- `.env` — local API keys (gitignored).
- `README.md` — public-facing overview.

## When in doubt

Ask before refactoring or expanding scope. The owner prefers a one-line clarifying question over a large speculative diff.
