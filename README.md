# Metis

A local-first AI dev agent — provider-agnostic, self-improving, and cost-aware.

> **Status:** Phase 1 + Phase 2 + Phase 2.5 shipped; Phase 3 in flight — **ready for review whether to promote to "Phase 3 shipped."** The three Phase-3 wedges (transparent HTTP gateway, multi-user identity / per-team cost attribution, evaluator) are live, and the model-selection differentiator inverted on its first end-to-end demonstration ([`benchmarks/RESULTS.md §A3-rev3`](benchmarks/RESULTS.md): Pass C picks sonnet on the one hard turn of `regex-with-edge-cases`, quality 5.55 vs Pass A 5.16 at $0.0477/quality between haiku-only $0.0383 and sonnet-only $0.1176). Three providers (Anthropic / OpenAI / OpenRouter) drive end-to-end turns with streaming, tool use, bounded memory, cost tracking, event tracing, SQLite-persisted sessions. `metis chat` (line REPL), `metis tui` (Textual TUI), `metis serve` (HTTP/WebSocket), `metis gateway` (transparent provider-shape proxy) all run. 1405 tests passing.

---

## Why Metis

Metis optimizes a buyer's LLM bill by composing three levers — context engineering (prompt-cache discipline, lean prompts), skills (focused expert instructions loaded on demand), and model selection (route each turn to the cheapest model that succeeds on the task class). The order of typical impact is context > skills > selection; the routing wedge is the one with the cleanest demonstration. On our shipped benchmark suite, slot 4 of the routing chain picked sonnet on the one hard turn of `regex-with-edge-cases` and haiku on every other turn — recovering 99% of sonnet-only's quality at roughly 25% above haiku-only's cost. See [`docs/savings-demo.md`](docs/savings-demo.md) for the evidence and [`docs/customer-trial-recipe.md`](docs/customer-trial-recipe.md) for how to reproduce on your own workload.

---

## Quick start

```bash
# Python 3.13 + uv required.
uv sync   # resolves the workspace (metis-core, metis-server, metis-cli)

# Put your Anthropic API key in a gitignored .env file
echo "ANTHROPIC_API_KEY=sk-ant-..." > .env

# Start a chat in any workspace directory
uv run metis chat . --model sonnet
```

The repo is a uv-workspace monorepo: [`packages/metis-core/`](packages/metis-core/) is the library (canonical types, events, adapters, routing, tools, memory, sessions, pricing, skills, trace); [`apps/server/`](apps/server/) and [`apps/cli/`](apps/cli/) are the deployable surfaces. The `metis` console-script is shipped by `metis-cli`.

Inside the REPL: type your message and hit return. Slash commands: `/model <alias|id>`, `/model -` (clear sticky), `/cost`, `/models`, `/help`. Ctrl-D or `exit` to leave. Per-message override: start a message with `@haiku` (or any alias) to route that single message to a different model.

Aliases configured out of the box: `opus` / `deep`, `sonnet` / `balanced`, `haiku` / `fast`.

Sanity-check the full loop against the real API in under a minute (~$0.015 with haiku):

```bash
uv run python scripts/smoke.py --model haiku
```

## Try it — transparent gateway in Docker

Want to drop Metis in front of an existing OpenAI / Anthropic SDK client without changing any code? The gateway is the high-floor adoption path from [`docs/STRATEGY.md §3`](docs/STRATEGY.md) — buyer flips one env var, savings show up on the dashboard within hours.

```bash
cp .env.example .env && $EDITOR .env   # set ANTHROPIC_API_KEY
docker compose run --rm gateway issue-key --name "my-client" --workspace /workspace
docker compose up -d
curl http://127.0.0.1:8422/healthz
```

Full deployment reference (env vars, volumes, key rotation, TLS termination, cost attribution) at [`docs/gateway-deployment.md`](docs/gateway-deployment.md).

## Buyer trial

Once the gateway is up, point your devs' existing tools at it: flip
`ANTHROPIC_BASE_URL` (Claude Code) or `OPENAI_BASE_URL` (Cursor, openai-python)
to the gateway URL, hand over a `gw_…` key, and every turn is cost-stamped per
dev, per project — no client code changes. End-to-end recipe (Claude Code,
Cursor, raw curl/SDK) at [`docs/gateway-client-quickstart.md`](docs/gateway-client-quickstart.md).

> **v1 binds loopback-only.** The gateway refuses any non-`127.0.0.1` bind
> per [`gateway.md §3.2`](docs/specs/gateway.md); do not expose it to the
> public internet directly. Front it with Caddy / nginx-ingress / a cloud LB
> that terminates TLS. The layered defenses (TLS, rate limiting, leak
> detection) are documented in
> [`docs/specs/gateway-hardening.md`](docs/specs/gateway-hardening.md).

## Operations

The operational docs a buyer's SRE will read before signing. All three
sit under [`docs/operations/`](docs/operations/):

- [`incident-response.md`](docs/operations/incident-response.md) — SEV1-SEV4 criteria, on-call alert paths (PagerDuty / Opsgenie / email), first-hour playbook, post-mortem template, and per-failure-mode playbooks for upstream LLM outage, trace-DB corruption, gateway-key compromise, and quota runaway.
- [`status-page.md`](docs/operations/status-page.md) — two-tier recipe (external UptimeRobot / Statuspage.io / Better Stack against `/healthz`, plus self-hosted Uptime Kuma in-cluster), publish/redact guidelines, and incident comm templates.
- [`sla-template.md`](docs/operations/sla-template.md) — 99.5% single-region template the buyer can customize for their own downstream-user SLA: service-credit math, exclusions, force-majeure stub (legal-counsel-deferred).

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

## What's working today

- **Three provider adapters.** Anthropic (Opus 4.7, Sonnet 4.6, Haiku 4.5), OpenAI (GPT-5, GPT-5-mini), and OpenRouter (catalog fetched at startup, pricing overlaid). Each implements wire translation, 8-class error classification, bounded retry with `retry_after` honoring, cancellation, per-model `AdapterCapabilities`, and `stream()` returning canonical streaming events. Cross-provider continuity is verified by a real-API smoke test that mid-session switches Anthropic→OpenAI→OpenRouter with tool-use round-trip.
- **Streaming end-to-end.** Adapter `stream()` → `SessionManager` streaming event handler → both CLI live-render and WebSocket clients. Text deltas, tool-use start/input-delta/end, and message-complete events flow through.
- **Five built-in tools + three memory tools.** `read_file`, `write_file`, `patch_file`, `list_dir`, `shell` (all workspace-scoped, `..` and out-of-root symlinks rejected). Plus `memory_add`, `memory_replace`, `memory_consolidate` for bounded memory mutation.
- **Bounded memory.** Per-workspace `MEMORY.md` (~2 KB soft, 4 KB hard) and `USER.md` (~1.5 KB soft, 3 KB hard) under `.metis/`. Soft cap fires `memory.eviction`; hard cap rejects the write so the agent has to consolidate. The agent reads memory fresh from disk on every LLM call (composed into the system prompt). See [`docs/specs/memory-store.md`](docs/specs/memory-store.md).
- **Session manager.** Turn-locked streaming loop, multi-call within a turn, tool cycle wiring, cost stamping, full event emission, parent-event-id chains.
- **Routing engine.** Per-message `@alias` overrides, `/model` sticky, capability validation (vision / context-window / tools / system-prompt / structured-output), per-provider availability tracking. Exactly one `route.decided` event per turn including the full chain trace. Configured-rule (yaml policy) parsing has landed; integration into the chain is in flight.
- **Event bus + SQLite trace store + SQLite session store.** WAL + `synchronous=NORMAL` for sub-millisecond fast-path writes. Replay queries, causal-chain walks, per-session isolation. Messages and sessions persist; restart preserves conversation history.
- **HTTP/WebSocket server.** Starlette + uvicorn ASGI app. REST for sessions/turns/messages/models/health; WebSocket `/sessions/{id}/stream` with single-use attach tokens, snapshot+live replay, filter presets, cancel-via-WS, ping/pong. Loopback-only bind in v1.
- **Three client surfaces.** `metis chat` (line REPL), `metis tui` (Textual app), `metis serve` (HTTP/WS server for external clients). Slash commands `/model`, `/cost`, `/models`, `/help`. Per-message `@alias` syntax.
- **Cost in real time.** Per-turn input/output/cached token costs computed by core (not parroted from provider), `Decimal` math, versioned for retroactive re-pricing. OpenRouter prices overlaid at session start.
- **729 tests** across canonical round-trips, JSON Schema enforcement, role-content invariants, event catalog, bus dispatch + filtering, workspace escape rejection, dispatcher + confirmation, adapter wire translation + streaming + error classification + retry + cancellation, cross-provider conformance, routing chain + rule loading + predicates, memory store + tools, session manager + persistence + streaming, HTTP REST + WebSocket + token registry + confirmations.

## What's NOT built yet (next-up)

- **Configured routing rules in the chain.** The yaml parser, predicate set, and rule loader are in [`packages/metis-core/src/metis_core/routing/`](packages/metis-core/src/metis_core/routing/) (`policy.py`, `policy_loader.py`, `predicates.py`); the `rule` slot in `route.decided.chain` still reports `not_applicable` until the wiring is finished.
- **Skills.** `packages/metis-core/src/metis_core/skills/` has a store and a `load_skill` tool with `skill.loaded` events emitting. Full agentskills.io conformance, FTS5 indexing, and auto-generation are still phase-2 work.
- **Tool-confirmation REST endpoint.** [`server-api.md §4.2`](docs/specs/server-api.md) specs `POST /turns/{id}/confirmations/{request_id}`; it isn't wired. The dispatcher uses `AutoAllowHandler` (auto-approves everything; safe for single-user, not for shared).
- **Pattern store + learned routing.** Phase 2.5.
- **Delegation (`delegate()` tool).** Phase 4. The routing chain has a `DELEGATE_REQUEST` stub.
- **Worker sessions** (`include_worker_sessions` accepted by the WS filter but no workers exist yet).
- **Routing policy hot-reload + version surfacing** (`GET /sessions/{id}` returns `routing_policy_version: null`).

See [`docs/KNOWN_ISSUES.md`](docs/KNOWN_ISSUES.md) for spec/impl gaps that are tracked but not yet fixed.

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

The design is specified before code lands. Start here:

**Project context** (read these first if you're new):

- [AGENTS.md](AGENTS.md) — current state of the codebase, conventions, gotchas. Load-bearing for AI agents.
- [docs/STRATEGY.md](docs/STRATEGY.md) — the *why*: cost-optimization thesis, buyer ≠ user, three cost levers, open strategic questions.
- [docs/project-overview.md](docs/project-overview.md) — vision, principles, architecture, phasing.
- [docs/KNOWN_ISSUES.md](docs/KNOWN_ISSUES.md) — spec/impl gaps tracked from prior reviews; the watchlist of "looks fine but is subtly wrong."

**Component specs** (the contracts):

- [docs/specs/canonical-message-format.md](docs/specs/canonical-message-format.md) — the load-bearing data contract
- [docs/specs/event-bus-and-trace-catalog.md](docs/specs/event-bus-and-trace-catalog.md) — observability spine + closed event-type catalog
- [docs/specs/routing-engine.md](docs/specs/routing-engine.md) — model selection, rules, delegation
- [docs/specs/provider-adapter-contract.md](docs/specs/provider-adapter-contract.md) — adapter interface, wire translation, retry, errors
- [docs/specs/tool-dispatcher.md](docs/specs/tool-dispatcher.md) — tool registry, side-effect classification, confirmation
- [docs/specs/streaming-protocol.md](docs/specs/streaming-protocol.md) — WebSocket protocol for clients
- [docs/specs/server-api.md](docs/specs/server-api.md) — REST endpoints, attach handshake, session lifecycle
- [docs/specs/memory-store.md](docs/specs/memory-store.md) — bounded MEMORY.md / USER.md schema and tools
- [docs/specs/CHANGES.md](docs/specs/CHANGES.md) — cross-spec change log

**Market context:** [docs/market-research/synthesis.md](docs/market-research/synthesis.md) and the four per-stream reports.

## License

_TBD_
