# Project Overview

> **Working context first.** Before reading this doc, AI agents working in the repo should read [AGENTS.md](../AGENTS.md) for current implementation state and conventions, and [docs/STRATEGY.md](STRATEGY.md) for the *why* — the cost-optimization thesis, B2B framing, and open strategic questions that aren't visible from the code. Spec/impl gaps are tracked in [docs/KNOWN_ISSUES.md](KNOWN_ISSUES.md).
>
> This doc describes the *shape* of the system (vision, principles, architecture, phasing). It is not the current build status — see AGENTS.md for that.

## What we're building

A local-first AI agent tool that sits between Claude Desktop and Cursor in capability — a developer-oriented assistant that gets more useful the longer it runs, with first-class support for switching between LLM providers, learning from past tasks, and operating on real workspaces.

## Goals

**1. Self-improving over time.** The system accumulates skills (procedural knowledge as portable markdown files), bounded memory (curated facts about the user and their work), and pattern recognition (which models and approaches worked on which kinds of tasks). The longer it's used, the less the user has to re-explain.

**2. Provider-agnostic by design.** Users can switch between LLM models and providers seamlessly, including mid-session. The system holds a canonical internal representation of conversations; provider adapters serialize to and from each provider's wire format. Adding a new provider is writing an adapter, not a refactor.

**3. Smart model routing.** Three modes, layered:
- *Manual* — the user picks a model explicitly (sticky session default, per-message override).
- *Configured* — user-defined rules (yaml policy) match patterns and route accordingly.
- *Agent-decided* — for complex tasks, a capable planner model can delegate sub-tasks to cheaper workers via a `delegate(tier, task, context)` tool.

**4. Cost-aware.** Every turn's tokens and dollar cost are tracked, attributed to model and role (planner vs worker), and visible to the user. Cost transparency is what makes routing feel valuable rather than mysterious.

**5. Cross-device portability.** Skills, memory, and user preferences sync across machines via git remote (Phase 3). Sessions stay local for privacy.

**6. Observable and analyzable.** Every action emits trace events. A web dashboard surfaces analytics on skills, prompts, models, and patterns. The user can see what the agent has been doing and whether it's getting better.

**7. Future marketplace.** Skills are portable markdown files compatible with the agentskills.io open standard. A marketplace becomes possible once the single-user loop is solid (Phase 4+).

## Audience and surface

- **Early adopters (MVP):** developers comfortable with a terminal interface.
- **Public reveal:** broader audience via a Tauri desktop app talking to the same local server.
- **Initial providers:** Anthropic and OpenAI direct adapters; Ollama (local) and OpenRouter (long-tail catalog) added in Phase 2/3.

## What this is *not*

- Not a stateless chat app. Sessions persist, accumulate context, and continue across client restarts.
- Not a cloud service. The agent runs locally; the user's data stays on their machine by default.
- Not a coding-only tool, though developers are the early audience. The architecture is workspace-scoped but not language-specific.
- Not a wrapper around one provider. The canonical-format design exists specifically to keep providers fungible.

---

# Architecture

## High-level shape

```
┌──────────────────────────────────────────────────────────┐
│  CLIENTS                                                 │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐    │
│  │  Textual TUI │  │ Tauri (later)│  │ Web dashboard│    │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘    │
└─────────┼─────────────────┼─────────────────┼────────────┘
          │                 │                 │
          └─────────────────┼─────────────────┘
                            │ HTTP + WebSocket (localhost)
┌───────────────────────────┴──────────────────────────────┐
│  CORE SERVER (Python, runs on localhost)                 │
│                                                          │
│  ┌────────────────────────────────────────────────────┐  │
│  │  Session Manager — turn loop, streaming, lifecycle │  │
│  └────────────────┬───────────────────────────────────┘  │
│                   │                                      │
│  ┌────────────────▼─────────┐  ┌──────────────────────┐  │
│  │  Routing Engine          │  │  Tool Dispatcher     │  │
│  │  - manual / rule / LLM   │  │  - file ops          │  │
│  │  - feedback-aware (P2.5) │  │  - shell             │  │
│  │                          │  │  - MCP servers (P3)  │  │
│  └────────────────┬─────────┘  └──────────┬───────────┘  │
│                   │                       │              │
│  ┌────────────────▼─────────┐             │              │
│  │  Provider Abstraction    │             │              │
│  │  - canonical msg format  │             │              │
│  │  - Anthropic / OpenAI    │             │              │
│  │  - (Ollama / OpenRouter) │             │              │
│  └────────────────┬─────────┘             │              │
│                   │                       │              │
│  ┌────────────────▼───────────────────────▼──────────┐   │
│  │  Context Assembler                                │   │
│  │  - system + memory + skills + history             │   │
│  └────────────────┬──────────────────────────────────┘   │
│                   │                                      │
│  ╔═══════════════ ▼ ═════════════════════════════════╗   │
│  ║  EVENT BUS  (every action emits trace events)     ║   │
│  ╚═══════════════════╤═══════════════════════════════╝   │
│                      │                                   │
│  ┌──────────────┐ ┌──▼──────────┐ ┌─────────────────┐    │
│  │ Skill Store  │ │ Trace Store │ │ Pattern Store   │    │
│  │ - md files   │ │ - events    │ │ - fingerprints  │    │
│  │ - FTS5       │ │ - SQLite    │ │ - embeddings    │    │
│  └──────────────┘ └──────┬──────┘ └────────┬────────┘    │
│                          │                 │             │
│  ┌──────────────┐        ▼                 ▼             │
│  │ Memory Store │  ┌──────────────────────────────────┐  │
│  │ - MEMORY.md  │  │  Evaluator (offline analytics)   │  │
│  │ - USER.md    │  │  - skill / model / prompt eval   │  │
│  └──────────────┘  └──────────────────────────────────┘  │
│                                                          │
│  ┌────────────────────────────────────────────────────┐  │
│  │ Session Store — SQLite + FTS5                      │  │
│  └────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────┘
                            │
                            │ (Phase 3+)
                  ┌─────────▼──────────┐
                  │  Sync (git remote) │
                  └────────────────────┘
```

## Core principles

**Server-client split.** The core server holds all session state and runs the agent loop. Clients (TUI, eventually Tauri, the web dashboard) are thin and communicate over HTTP+WebSocket on localhost. This makes long-running tasks survive client restarts, allows multiple clients on one session, and makes the eventual desktop app a drop-in second client rather than a rewrite.

**Canonical message format.** The system has one internal representation of messages, content blocks, and tool calls. Provider adapters translate between canonical and wire formats. Mid-session model swaps work because canonical state is provider-agnostic; replays survive because canonical content is preserved. This format is the load-bearing data contract — specified in detail in `docs/specs/canonical-message-format.md`.

**Files-on-disk for portability.** Skills, memory, and user preferences are markdown files in known directories. Power users can edit, version-control, and share these directly. Sync is just `git push`. No proprietary lock-in.

**Bounded memory by design.** `MEMORY.md` (~2 KB) and `USER.md` (~1.5 KB) are intentionally small. The agent curates them — adding, replacing, and consolidating entries — so they stay focused. Unbounded memory destroys context quality; the eviction is a feature.

**Event bus as observability spine.** Every meaningful action (turn boundaries, LLM calls, routing decisions, tool invocations, skill loads, feedback) emits a structured event. The trace store is one subscriber; streaming to clients is another. New analytics consumers don't touch the agent loop.

**Local-only by default, with hooks for future cross-user learning.** Every stored row is tagged with sensitivity. Pattern recommendations accept an optional `global_prior` parameter. Cross-user features are a future opt-in addition, not a refactor.

## Key components

| Component | Responsibility |
|-----------|----------------|
| **Session Manager** | Active session lifecycle, turn loop orchestration, streaming. |
| **Routing Engine** | Picks the model for each turn via the manual → configured → pattern → delegate → default pipeline. Validates capabilities before dispatch. |
| **Provider Abstraction** | Canonical message format and adapters per provider. One adapter per provider, shared canonical types. |
| **Tool Dispatcher** | Registry and execution of tools (file ops, shell, MCP servers). Side-effect classification drives confirmations. |
| **Context Assembler** | Builds the prompt for each LLM call: system instructions + USER.md + MEMORY.md + relevant skills + transcript. Manages token budgets and history compression. |
| **Event Bus** | In-process pub/sub. Closed event-type set. Synchronous emit, async fan-out. |
| **Trace Store** | Append-only SQLite log of every event. Drives all analytics and the dashboard. |
| **Pattern Store** | Task fingerprints (embedding + structured tags) with outcomes. Powers cold-start routing recommendations. |
| **Skill Store** | Markdown files with frontmatter; FTS5-indexed for on-demand search. Hand-written, auto-generated, or installed. |
| **Memory Store** | Bounded MEMORY.md and USER.md per workspace. Agent-curated via tools. |
| **Evaluator** | Offline analytics over traces. Surfaces skill/model/prompt performance. Produces dashboard data. |
| **Session Store** | Full transcripts with the canonical message format, indexed by FTS5. |

## Phasing summary

| Phase | Duration target | Headline deliverable | Status (2026-05-12) |
|-------|-----------------|----------------------|---------------------|
| **1. Core loop** | weeks 1–4 (full-time) | Two providers, canonical format, event bus, file/shell tools, basic TUI, manual routing. Daily-driver. | **shipped.** Three providers (Anthropic / OpenAI / OpenRouter); streaming; CLI + TUI + HTTP/WS server; SQLite persistence; 544 tests. |
| **2. Skills, memory, dashboard** | weeks 5–8 | Hand-written skills, bounded memory, web dashboard, explicit feedback, configured routing rules. | **partially shipped.** Bounded memory + 3 memory tools shipped; skills store + `load_skill` tool wired; configured-rule parser landed; rule integration into the chain and dashboard pending. |
| **2.5. Pattern learning** | weeks 9–10 | Fingerprints, cold-start suggestions, skill auto-generation with security scanner. | not started. |
| **3. Polish + sync** | weeks 11–14 | In-session adjustment heuristics, full evaluator, MCP support, git sync, third provider. | not started. |
| **4. Tauri + reveal** | weeks 15+ | Desktop app, public-ready UX, marketplace foundation. | not started. |

(Calendar time roughly doubles at part-time pace.)

## Specs and documents

**Strategy and ops:**

- [`docs/STRATEGY.md`](STRATEGY.md) — Cost-optimization thesis, buyer ≠ user, three cost levers (skills / context / model selection), open strategic questions.
- [`docs/KNOWN_ISSUES.md`](KNOWN_ISSUES.md) — Carryover review findings; spec promises the code doesn't yet honor.
- [`AGENTS.md`](../AGENTS.md) — Current implementation state, conventions, gotchas. Read first.

**Component specs:**

- [`docs/specs/canonical-message-format.md`](specs/canonical-message-format.md) — Drafted. Messages, content blocks, tool calls, metadata, persistence, versioning.
- [`docs/specs/event-bus-and-trace-catalog.md`](specs/event-bus-and-trace-catalog.md) — Drafted. Event bus interface, full event-type catalog with payload schemas.
- [`docs/specs/streaming-protocol.md`](specs/streaming-protocol.md) — Drafted. WebSocket protocol, snapshot/replay, three cancellation cases.
- [`docs/specs/routing-engine.md`](specs/routing-engine.md) — Drafted. Policy chain, configured-rule format, capability validation, `delegate()` contract.
- [`docs/specs/provider-adapter-contract.md`](specs/provider-adapter-contract.md) — Drafted. Adapter interface, wire translation, error classification.
- [`docs/specs/tool-dispatcher.md`](specs/tool-dispatcher.md) — Drafted. Registry, dispatch, side-effect classification, confirmation policy.
- [`docs/specs/server-api.md`](specs/server-api.md) — Drafted. REST endpoints, attach handshake, session lifecycle.
- [`docs/specs/memory-store.md`](specs/memory-store.md) — Drafted. Bounded MEMORY.md / USER.md schema and tools.
- [`docs/specs/CHANGES.md`](specs/CHANGES.md) — Cross-spec change log.
- *(planned)* `skill-format.md`, `pattern-store.md`, `evaluator.md`.

**Market context:** [`docs/market-research/synthesis.md`](market-research/synthesis.md) and four per-stream reports.
