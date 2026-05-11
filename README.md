# Metis

A local-first AI dev agent — provider-agnostic, self-improving, and cost-aware.

> **Status:** Pre-alpha. Design phase. The specs in [docs/specs/](docs/specs/) are stable; implementation begins in Phase 1.

---

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
│  Textual TUI · Tauri (later) · Web UI  │
└────────────────┬───────────────────────┘
                 │  HTTP + WebSocket (localhost)
┌────────────────┴───────────────────────┐
│           Python core server           │
│                                        │
│  Session manager · Routing engine      │
│  Provider adapters (canonical format)  │
│  Tool dispatcher · Context assembler   │
│  Event bus → trace store               │
│  Skills · Memory · Patterns            │
└────────────────┬───────────────────────┘
                 │  (Phase 3+)
                 │
            git remote sync
```

Key design choices:

- **Server-client split.** The agent loop runs in a localhost server. Clients are thin and disposable; restarting your terminal doesn't kill an in-flight task, and multiple clients can attach to one session.
- **Canonical message format.** One internal representation for messages, content blocks, and tool calls. Provider adapters serialize to and from each provider's wire format. Adding a provider is writing an adapter, not refactoring the system.
- **Three-layer routing.** Manual selection → configured yaml rules → learned pattern recommendations. User intent always beats system inference. Every decision is recorded with a full chain trace you can inspect with `/model show`.
- **Bounded, portable memory.** `MEMORY.md` (~2 KB) and `USER.md` (~1.5 KB) per workspace, agent-curated. Markdown on disk; edit, version, and sync via git.
- **Skills as portable markdown.** Compatible with the agentskills.io open standard; hand-written, auto-generated, or installed.
- **Event bus + trace store.** Every meaningful action emits a structured event. Analytics, dashboards, and replay all consume the same stream.
- **Cost-aware.** Tokens and USD tracked per turn, attributed to model and role (planner vs delegated worker), and visible to you in real time.

## Roadmap

| Phase   | Target       | Headline deliverable                                                                       |
|---------|--------------|--------------------------------------------------------------------------------------------|
| **1**   | weeks 1–4    | Two providers, canonical format, event bus, file/shell tools, basic TUI, manual routing.   |
| **2**   | weeks 5–8    | Hand-written skills, bounded memory, web dashboard, explicit feedback, configured rules.   |
| **2.5** | weeks 9–10   | Pattern fingerprints, cold-start suggestions, skill auto-generation with security scanner. |
| **3**   | weeks 11–14  | In-session adjustment heuristics, full evaluator, MCP support, git sync, third provider.   |
| **4**   | weeks 15+    | Tauri desktop app, public-ready UX, marketplace foundation.                                |

(Calendar time roughly doubles at part-time pace.)

## Documentation

The design is fully specified before code lands. Start here:

- [docs/project-overview.md](docs/project-overview.md) — vision, principles, architecture, phasing
- [docs/specs/canonical-message-format.md](docs/specs/canonical-message-format.md) — the load-bearing data contract
- [docs/specs/event-bus-and-trace-catalog.md](docs/specs/event-bus-and-trace-catalog.md) — observability spine + closed event-type catalog
- [docs/specs/routing-engine.md](docs/specs/routing-engine.md) — model selection, rules, delegation
- [docs/specs/streaming-protocol.md](docs/specs/streaming-protocol.md) — WebSocket protocol for clients

## License

_TBD_
