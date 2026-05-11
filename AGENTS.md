# AGENTS.md

Shared context for any AI agent working in this repo (Claude Code, Cursor, Codex, Aider, etc.). `CLAUDE.md` is a symlink to this file.

## What Metis is

A local-first AI dev agent server. Provider-agnostic via a canonical message format, with a layered routing engine (manual / configured rules / learned patterns), bounded portable memory, and an event bus that feeds a trace store. Python core server; thin clients (CLI first, TUI/Tauri later).

**Status:** Phase 1 prototype shipped. The CLI (`metis chat <workspace>`) drives a real Claude API loop end-to-end with tool use, cost tracking, and event tracing. 272 tests passing. Streaming, HTTP/WebSocket, and a proper TUI are still ahead.

## Implementation status

**What works:**

- **Canonical types** ([src/metis/canonical/](src/metis/canonical/)) — `Message`, `Role`, content blocks (Text, Image, ToolUse, ToolResult, Thinking, RedactedThinking), `ToolDefinition`, `AdapterCapabilities`, `Usage`, `RoutingDecisionRecord`. `msgspec`-backed, JSON-roundtrippable, fully validated against canonical-format §5.
- **Event bus + trace store** ([src/metis/events/](src/metis/events/), [src/metis/trace/](src/metis/trace/)) — full Phase 1 catalog (22 event types), bounded async dispatch, fast-path / non-fast-path subscribers, SQLite WAL+NORMAL writer, replay query, causal-chain walk.
- **Tool dispatcher** ([src/metis/tools/](src/metis/tools/)) — registry + JSON-Schema validation + confirmation policy + 5 builtins (`read_file`, `write_file`, `patch_file`, `list_dir`, `shell`). Workspace-scoped file API rejects `..` and out-of-root symlinks.
- **Anthropic adapter** ([src/metis/adapters/anthropic.py](src/metis/adapters/anthropic.py)) — wire translation (SYSTEM hoist, TOOL→user-with-tool_result merge), error classification (8-class), bounded retry with exponential backoff + `retry_after` honoring, cancellation, per-model `AdapterCapabilities`. `complete()` only — streaming is stubbed.
- **Routing engine** ([src/metis/routing/](src/metis/routing/)) — full 7-slot chain (per-message override / manual sticky / rule [stub] / pattern [stub] / delegate [stub] / workspace default / global default) with capability validation and provider availability tracking. Emits exactly one `route.decided` event per turn, including on hard failure.
- **Session manager** ([src/metis/sessions/manager.py](src/metis/sessions/manager.py)) — turn-locked model loop, multi-call within a turn, tool cycle wiring, cost stamping, full event emission.
- **CLI** ([src/metis/cli/](src/metis/cli/)) — `metis chat <workspace> [--model alias]`. Slash commands `/model`, `/cost`, `/models`, `/help`. Per-message `@alias` override syntax.
- **Smoke harness** ([scripts/smoke.py](scripts/smoke.py)) — drives the loop against the real API; verified working with ~$0.015 / 2-turn run.

**What's NOT built (Phase 1 finish line and beyond):**

- **Streaming** — adapter's `complete()` returns the whole response at once. `stream()` raises `NotImplementedError`. The transient streaming-event layer per `streaming-protocol.md §5` is not wired up.
- **HTTP server / WebSocket** — `server-api.md` and the attach handshake aren't implemented. The CLI talks to `SessionManager` in-process.
- **Textual TUI** — only the line-based REPL exists.
- **Configured routing rules** — yaml policy file is not parsed (`routing-engine.md §5` is spec, Phase 2 implementation).
- **Skills, memory, pattern store** — Phase 2 / 2.5.
- **Session message persistence** — `InMemorySessionStore` only; the SQLite-backed implementation per `canonical-message-format.md §9.1` (sessions, messages, tool_calls tables) is not built. Trace events DO persist, but message history is lost on restart.
- **Streaming-aware confirmation handler** — `AutoAllowHandler` is the default; it auto-approves *everything* including WRITE/EXECUTE/NETWORK. Fine for single-user dev, unsafe for production / shared use.

## Read before reasoning about the system

The design is specified before code lands. In order of load-bearing-ness:

1. [docs/project-overview.md](docs/project-overview.md) — vision, principles, architecture, phasing.
2. [docs/specs/canonical-message-format.md](docs/specs/canonical-message-format.md) — the foundational data contract; every other spec depends on it.
3. [docs/specs/event-bus-and-trace-catalog.md](docs/specs/event-bus-and-trace-catalog.md) — observability spine, closed event catalog.
4. [docs/specs/routing-engine.md](docs/specs/routing-engine.md) — model selection pipeline, delegation.
5. [docs/specs/streaming-protocol.md](docs/specs/streaming-protocol.md) — WebSocket protocol (not yet implemented).
6. [docs/specs/provider-adapter-contract.md](docs/specs/provider-adapter-contract.md), [docs/specs/tool-dispatcher.md](docs/specs/tool-dispatcher.md), [docs/specs/server-api.md](docs/specs/server-api.md) — component contracts.
7. [docs/specs/CHANGES.md](docs/specs/CHANGES.md) — cross-spec change log; check `pending review` entries before editing dependent specs.

For competitive landscape and prior art: [docs/market-research/synthesis.md](docs/market-research/synthesis.md) and the four per-stream reports alongside it.

## Architecture and package layering

Dependency direction (lower can be imported by higher, never the other way):

```
canonical      ←  events, trace, tools, adapters, routing, pricing, sessions, cli
events         ←  trace, tools, adapters, routing, sessions
trace          ←  cli (only the wiring layer composes them)
adapters       ←  pricing, sessions, cli
tools          ←  sessions, cli
routing        ←  sessions, cli
pricing        ←  sessions, cli
sessions       ←  cli
```

`canonical` is the foundation; anything can import from it. Nothing in `canonical` imports from any other Metis package. The CLI sits at the top and wires the rest together. When adding code, respect this direction — a circular import means a missing abstraction.

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

- **Streaming events are a separate layer**, not bus catalog events. `message.start`, `text.delta`, `tool.use_*`, `message.complete` exist in `streaming-protocol.md §5.3` but are NOT in `PAYLOAD_REGISTRY`. When the streaming server lands it has two input channels: the bus bridge (catalog events, persisted) and a direct channel from the agent loop (streaming events, transient). The trace store only sees the former.
- **Turn-locked model.** The model chosen at turn start owns every LLM call in the turn, including tool cycles. Routing only re-runs at turn boundaries. Don't add code paths that re-route mid-turn — it breaks cost predictability and the `route.decided` invariant.
- **Phase 1 stub policies** (`rule`, `pattern`, `delegate_request`) appear in `route.decided.chain` with `verdict: "not_applicable"`. That's correct, not a bug. They'll be filled in in Phase 2 / 2.5 / 4.
- **Provider availability is per-provider, not per-(provider, model).** The spec acknowledges this is deferred (`routing-engine.md §11`). A 401 on opus marks the whole anthropic provider unavailable.
- **Sessions are in-memory.** `InMemorySessionStore` is the only impl; restart loses message history. Trace events DO persist via SQLite. The SQLite session store per `canonical-message-format.md §9.1` is unbuilt.
- **`AutoAllowHandler` auto-approves everything** including writes and shell. Phase 1 single-user is fine; do not ship to anywhere shared without swapping in a real confirmation handler.
- **PARTIAL message bypass.** `validate_message()` skips invariant checks when `metadata.status == PARTIAL` (canonical-format §5.1.5). Mid-stream messages are intentionally allowed to violate role-content rules.

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
# Tests (272 currently)
uv run pytest

# Lint + format
uv run ruff check src tests
uv run ruff format src tests

# CLI (requires ANTHROPIC_API_KEY in env or .env)
uv run metis chat .
uv run metis chat /path/to/workspace --model haiku

# Real-API smoke test (~$0.015 with haiku)
uv run python scripts/smoke.py --model haiku
```

`.env` (gitignored) is the recommended place for `ANTHROPIC_API_KEY`. Trace store default location is `~/.metis/trace.db` (override with `--db-path`).

## Repo layout

- `docs/specs/` — load-bearing specs (read first).
- `docs/market-research/` — competitive landscape, verified 2026-05-09.
- `src/metis/` — implementation:
  - `canonical/` — foundational types (Messages, ContentBlocks, ToolDefinition, AdapterCapabilities).
  - `events/` — bus + envelope + payload catalog.
  - `trace/` — SQLite WAL writer + replay query.
  - `tools/` — dispatcher + workspace API + confirmation policy + 5 builtins.
  - `adapters/` — `ProviderAdapter` protocol + Anthropic implementation + retry + tool id map.
  - `routing/` — registry + availability + chain + per-message override parser.
  - `pricing/` — `PriceTable` with Decimal cost computation.
  - `sessions/` — `Session`, `InMemorySessionStore`, `SessionManager` (the turn loop).
  - `cli/` — argparse entry + REPL.
- `tests/` — pytest suite mirroring `src/metis/`. 272 tests across 8 packages.
- `scripts/smoke.py` — live-API end-to-end harness.
- `.env` — local API keys (gitignored).
- `README.md` — public-facing overview.

## When in doubt

Ask before refactoring or expanding scope. The owner prefers a one-line clarifying question over a large speculative diff.
