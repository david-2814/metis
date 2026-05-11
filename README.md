# Metis

A local-first AI dev agent — provider-agnostic, self-improving, and cost-aware.

> **Status:** Phase 1 prototype running. The `metis chat <workspace>` CLI drives a real Claude API loop end-to-end with tool use, cost tracking, and event tracing. 272 tests passing. Streaming, HTTP/WebSocket surface, and a Textual TUI are the remaining Phase 1 work.

---

## Quick start

```bash
# Python 3.13 + uv required.
uv sync

# Put your Anthropic API key in a gitignored .env file
echo "ANTHROPIC_API_KEY=sk-ant-..." > .env

# Start a chat in any workspace directory
uv run metis chat . --model sonnet
```

Inside the REPL: type your message and hit return. Slash commands: `/model <alias|id>`, `/model -` (clear sticky), `/cost`, `/models`, `/help`. Ctrl-D or `exit` to leave. Per-message override: start a message with `@haiku` (or any alias) to route that single message to a different model.

Aliases configured out of the box: `opus` / `deep`, `sonnet` / `balanced`, `haiku` / `fast`.

Sanity-check the full loop against the real API in under a minute (~$0.015 with haiku):

```bash
uv run python scripts/smoke.py --model haiku
```

## What it is

Metis is a developer-oriented AI assistant that runs as a small Python server on your localhost, with thin clients (terminal first; desktop and web later). It sits between Claude Desktop (chat) and Cursor (editor-coupled) in scope — a workspace-aware agent that:

- accumulates **skills, memory, and learned task patterns** so it gets more useful over time,
- treats LLM providers as **swappable adapters** so you can change models mid-session without losing state,
- routes each turn through an **explainable, user-overridable** policy chain,
- tracks **cost and behavior** down to the turn so you can see what your agent is actually doing.

## Why

Today's AI dev tools have recurring frictions:

- **Provider lock-in.** Switching tools means rebuilding your context, rules, and history from scratch.
- **No memory across sessions.** You re-explain your codebase, your conventions, and your preferences every time.
- **Opaque model choice.** Either you pay premium prices for routine work, or you fear-cap to a small model and get worse results — with no way to see which would have been right.
- **Auto-routing without trust.** Tools that pick models for you don't show their reasoning. One silent override and the feature gets disabled.
- **Cost is invisible.** Per-turn dollar accounting is rarely surfaced; usage anxiety distorts how people work.
- **Sessions die with the client.** Long-running tasks vanish when the IDE or terminal restarts.
- **Cloud-by-default.** Code, prompts, and traces leave your machine before you opt in.

Metis is built around the inverse of each: portability, persistence, transparency, and local-first by default.

## How it works

```
┌─ Clients ──────────────────────────────┐
│  CLI (now) · Textual TUI · Web UI      │
└────────────────┬───────────────────────┘
                 │  in-process (CLI) · HTTP+WebSocket (later)
┌────────────────┴───────────────────────┐
│           Python core server           │
│                                        │
│  Session manager · Routing engine      │
│  Provider adapters (canonical format)  │
│  Tool dispatcher · Workspace API       │
│  Event bus → SQLite trace store        │
│  Skills · Memory · Patterns (Phase 2+) │
└────────────────┬───────────────────────┘
                 │  (Phase 3+)
            git remote sync
```

Key design choices:

- **Canonical message format.** One internal representation for messages, content blocks, and tool calls. Provider adapters serialize to and from each provider's wire format. Adding a provider is writing an adapter, not refactoring the system.
- **Three-layer routing.** Manual selection → configured yaml rules → learned pattern recommendations. User intent always beats system inference. Every decision is recorded with a full chain trace.
- **Bounded, portable memory.** `MEMORY.md` (~2 KB) and `USER.md` (~1.5 KB) per workspace, agent-curated. Markdown on disk; edit, version, and sync via git.
- **Skills as portable markdown.** Compatible with the agentskills.io open standard; hand-written, auto-generated, or installed.
- **Event bus + trace store.** Every meaningful action emits a structured event. Analytics, dashboards, and replay all consume the same stream.
- **Cost-aware.** Tokens and USD tracked per turn, attributed to model and role (planner vs delegated worker), and visible in real time. Decimal math, no float drift.

## What's working today (Phase 1 prototype)

- **Anthropic adapter.** Opus 4.7, Sonnet 4.6, Haiku 4.5 with full wire translation (system hoist, TOOL→tool_result merge), bounded retry with exponential backoff and `retry_after` honoring, and 8-class error classification.
- **Five built-in tools.** `read_file`, `write_file`, `patch_file`, `list_dir`, `shell`. All workspace-scoped — `..` and out-of-root symlinks are rejected at the path-resolution layer.
- **Routing engine.** Per-message `@alias` overrides, `/model` sticky, capability validation (vision / context-window / tools / system-prompt / structured-output), per-provider availability tracking. Exactly one `route.decided` event per turn including the full chain trace.
- **Event bus + SQLite trace store.** WAL mode + `synchronous=NORMAL` for sub-millisecond fast-path writes. Replay queries, causal-chain walks, per-session isolation.
- **Cost in real time.** Per-turn input/output/cached token costs computed by core (not parroted from provider), versioned for retroactive re-pricing.
- **272 tests** covering canonical round-trips, JSON Schema subset enforcement, role-content invariants, event catalog membership, bus dispatch and filtering, workspace escape rejection, dispatcher flow + confirmation, adapter wire translation + error classification + retry + cancellation, routing chain with all 7 slots, end-to-end session manager turn loop.

## What's not built yet (the rest of Phase 1)

- **Streaming.** The adapter returns the whole response at once; tool-use turns feel like 2 batches rather than a live stream. The streaming-event layer per `streaming-protocol.md §5` is spec'd but unimplemented.
- **HTTP / WebSocket surface.** `server-api.md` and the attach handshake are spec'd; the CLI currently calls `SessionManager` in-process.
- **Textual TUI.** Only the line-based REPL exists.
- **Configured routing rules.** The yaml policy format is spec'd in `routing-engine.md §5` but the parser arrives in Phase 2.
- **Session message persistence.** Trace events persist; the canonical messages/tool_calls tables per `canonical-message-format.md §9.1` are not built. Restart loses conversation history but not the event trail.

## Roadmap

| Phase   | Target       | Headline deliverable                                                                                              |
|---------|--------------|-------------------------------------------------------------------------------------------------------------------|
| **1**   | weeks 1–4    | Two providers, canonical format, event bus, file/shell tools, basic TUI, manual routing. **CLI prototype done.** |
| **2**   | weeks 5–8    | Hand-written skills, bounded memory, web dashboard, explicit feedback, configured rules.                          |
| **2.5** | weeks 9–10   | Pattern fingerprints, cold-start suggestions, skill auto-generation with security scanner.                        |
| **3**   | weeks 11–14  | In-session adjustment heuristics, full evaluator, MCP support, git sync, third provider.                          |
| **4**   | weeks 15+    | Tauri desktop app, public-ready UX, marketplace foundation.                                                       |

(Calendar time roughly doubles at part-time pace.)

## Documentation

The design is fully specified. Start here:

- [docs/project-overview.md](docs/project-overview.md) — vision, principles, architecture, phasing
- [docs/specs/canonical-message-format.md](docs/specs/canonical-message-format.md) — the load-bearing data contract
- [docs/specs/event-bus-and-trace-catalog.md](docs/specs/event-bus-and-trace-catalog.md) — observability spine + closed event-type catalog
- [docs/specs/routing-engine.md](docs/specs/routing-engine.md) — model selection, rules, delegation
- [docs/specs/provider-adapter-contract.md](docs/specs/provider-adapter-contract.md) — adapter interface, wire translation, retry, errors
- [docs/specs/tool-dispatcher.md](docs/specs/tool-dispatcher.md) — tool registry, side-effect classification, confirmation
- [docs/specs/streaming-protocol.md](docs/specs/streaming-protocol.md) — WebSocket protocol for clients (planned)
- [docs/specs/server-api.md](docs/specs/server-api.md) — REST endpoints (planned)
- [docs/specs/CHANGES.md](docs/specs/CHANGES.md) — cross-spec change log
- [AGENTS.md](AGENTS.md) — context for AI agents (Claude Code, Cursor, etc.) working in this repo

## License

_TBD_
