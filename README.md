# Metis

**A local-first AI dev agent for your terminal.** Provider-agnostic, cost-aware, and
self-improving. Your codebase stays on your machine; your prompts and traces don't
leave it. Apache-2.0.

<p>
  <a href="LICENSE"><img alt="License: Apache 2.0" src="https://img.shields.io/badge/License-Apache_2.0-blue.svg"></a>
  <img alt="Python 3.13" src="https://img.shields.io/badge/Python-3.13-blue.svg">
  <img alt="1809 tests passing" src="https://img.shields.io/badge/tests-1809_passing-brightgreen.svg">
  <img alt="Status: Phase 3 GA" src="https://img.shields.io/badge/status-Phase_3_GA-success.svg">
</p>

```text
$ uv run metis chat .
metis> help me debug src/parser.py        # uses your default model (sonnet)
metis> @haiku summarize what you just did # route one turn to a cheaper model
metis> /cost                              # per-turn USD breakdown
metis> /model haiku                       # sticky switch for the rest of the session
```

Switch providers mid-session without losing context. Memory and skills live as
plain Markdown in your workspace — `git diff`-able, portable across machines.
Every turn is cost-stamped in Decimal USD and a full routing-decision trace.

---

## New here? Pick a path

- 🦾 **Just want to chat against an LLM locally?** [Quick start](#quick-start) below — `uv sync` + `uv run metis chat .`. Two minutes to first turn.
- 🧭 **Want the design rationale first?** [Project overview](docs/project-overview.md) — vision, principles, architecture, and the three cost levers.
- 🔌 **Already use Claude Code / Cursor / an SDK?** [Gateway client quickstart](docs/gateway-client-quickstart.md) — point `ANTHROPIC_BASE_URL` / `OPENAI_BASE_URL` at a Metis gateway, no client changes.
- 🏢 **Evaluating for a team?** [First savings number in &lt; 1 hour](docs/operations/quickstart.md) — kind cluster + helm + per-key cost rollup, end-to-end.

---

## Quick start

Two-minute path from clone to first chat. Requires Python 3.13 and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/david-2814/metis && cd metis
uv sync                                       # resolves the workspace

uv run metis auth add anthropic               # interactive; key is never echoed
                                              # writes ~/.metis/credentials.yaml (mode 0o600)
                                              # ↓ alternative (12-factor / CI):
                                              # echo "ANTHROPIC_API_KEY=sk-ant-..." > .env

uv run metis chat .                           # start chatting in this workspace
```

`metis auth add` is the discoverable default. It validates the key with a
free / sub-cent ping before persisting. If you prefer the env-var path,
keep using it — the resolver finds env vars on step 2 of its chain. See
[`docs/specs/credentials.md`](docs/specs/credentials.md) for the full
resolution order, file format, and `metis auth list / test / doctor`.

Inside the REPL: type your message and hit return. Useful commands:

| Command | Effect |
|---|---|
| `@haiku <message>` | Route a single message to a different model (any alias works) |
| `/model <alias\|id>` | Sticky-switch the active model for the rest of the session |
| `/model -` | Clear the sticky model and return to workspace default |
| `/cost` | Per-turn USD breakdown for this session |
| `/models` | List configured models and their aliases |
| `/help` | Full command list |

Aliases out of the box: `opus` / `deep`, `sonnet` / `balanced`, `haiku` / `fast`.
Ctrl-D or `exit` to leave.

Want a TUI instead? `uv run metis tui .` opens the Textual app over the same loop.
Sanity-check the loop against the real API in under a minute (~$0.015 with haiku):
`uv run python scripts/smoke.py --model haiku`.

---

## What you get

- **Provider-agnostic by design.** Anthropic (Opus/Sonnet/Haiku), OpenAI (GPT-5 / GPT-5-mini), OpenRouter — one canonical message format, three adapters. Switch models mid-session and tool-use round-trips just work. Cross-provider continuity is covered by a real-API smoke test.
- **Bounded, portable memory.** `MEMORY.md` (~2 KB) and `USER.md` (~1.5 KB) per workspace, agent-curated as Markdown on disk. Soft cap emits an eviction signal; hard cap rejects the write so the agent has to consolidate. Edit, version, and sync via git.
- **Explainable routing.** Per-message `@alias` → sticky `/model` → workspace yaml rules → learned patterns → workspace default → global default. Every turn emits one `route.decided` event with the full chain trace. No silent overrides.
- **Cost in real time.** Decimal-USD per-turn accounting by model and role (planner vs delegated worker). Versioned pricing for retroactive re-pricing. `/cost` in the REPL; `/analytics/cost` over HTTP.
- **Local-first.** Everything runs on your machine. SQLite trace store + session store under `~/.metis/`. Bounded memory under `<workspace>/.metis/`. The gateway is loopback-only by default; non-loopback binds require the documented hardening checklist.
- **Specs before code.** Component contracts live in [`docs/specs/`](docs/specs/) and ship before the implementation; an integration test suite covers them end-to-end.

The repo is a uv-workspace monorepo with one published package — installed as `metis-llm`, imported as `metis` — at [`packages/metis/`](packages/metis/). It's organized internally into four subpackages: `metis.core` is the library (canonical types, events, adapters, routing, tools, memory, sessions, pricing, skills, trace); `metis.server`, `metis.gateway`, and `metis.cli` are the deployable surfaces. The `metis` console-script ships from `metis.cli`. (The bare `metis` name on PyPI is already taken by an unrelated graph-partitioning library; the install-vs-import name split follows the PyYAML pattern.)

## Try the gateway

Run the transparent OpenAI / Anthropic-shaped proxy on localhost; point
any existing SDK or tool at it without changing client code.

```bash
cp .env.example .env && $EDITOR .env   # set ANTHROPIC_API_KEY
docker compose run --rm gateway issue-key --name "my-client" --workspace /workspace
docker compose up -d
curl http://127.0.0.1:8422/healthz
```

Full deployment reference at [`docs/gateway-deployment.md`](docs/gateway-deployment.md).
For the end-to-end buyer-trial path (kind cluster + helm + pre-baked
workload + per-key cost rollup), see
[`docs/operations/quickstart.md`](docs/operations/quickstart.md).

## Local dashboard

Every turn, every model, every dollar — measured against what it would
have cost on a single-model baseline, with per-user, per-team, and
per-key rollups. The counterfactual is the same one the gateway uses
live; no spreadsheet math.

**Cost** — total spend, savings vs a pinned baseline, spend over time,
cost by model, cache effectiveness.

![Metis cost dashboard showing total spend, vs claude-sonnet-4-6 baseline, spend over time, cost by model, and cache effectiveness.](docs/assets/dashboard-cost.png)

**Activity** — routing distribution across the seven-slot chain,
reliability (p50/p95) and call counts per model, recent failures, and
recent sessions.

![Metis activity dashboard showing routing distribution, reliability per model, failures, and recent sessions.](docs/assets/dashboard-activity.png)

**Spend by identity** — per-team, per-user, and per-key rollups. The
gateway stamps `user_id` / `team_id` / `gateway_key_id` on every
`llm.call_completed`, so attribution joins straight from the trace
store — no separate metering subsystem.

![Metis spend-by-identity dashboard showing per-team, per-user, and per-key spend breakdowns.](docs/assets/dashboard-cost-attribution.png)

## Why we built it

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

Metis has two entry paths sharing one core. The **agent path** owns the full
turn loop: sessions, tools, memory, skills, routing, tracing, evaluation, and
persistence. The **gateway path** is a transparent OpenAI / Anthropic-shaped
proxy for existing clients such as Claude Code, Cursor, and SDK apps.

```mermaid
flowchart LR
  subgraph Clients
    CLI["metis chat / tui"]
    HTTP["HTTP / WebSocket clients"]
    SDK["Claude Code / Cursor / SDKs"]
  end

  subgraph Apps["Application surfaces (metis.*)"]
    CLIApp["metis.cli<br/>local runtime setup"]
    Server["metis.server<br/>agent HTTP + WS"]
    Gateway["metis.gateway<br/>transparent provider proxy"]
  end

  subgraph Core["metis.core"]
    Canonical["Canonical IR<br/>messages, blocks, tools, usage"]
    Sessions["SessionManager<br/>agent turn loop"]
    Routing["RoutingEngine<br/>7-slot decision chain"]
    Tools["ToolDispatcher<br/>file, shell, memory, delegate"]
    Memory["Memory + Skills<br/>MEMORY.md, USER.md, SKILL.md"]
    Adapters["Provider Adapters<br/>Anthropic, OpenAI, OpenRouter"]
    Bus["EventBus"]
    Trace["TraceStore<br/>SQLite event log"]
    Eval["Evaluator<br/>heuristic / LLM / hybrid"]
    Patterns["PatternStore<br/>learned routing outcomes"]
    Analytics["AnalyticsStore<br/>cost, quality, savings"]
  end

  subgraph Storage["Local storage"]
    Workspace["workspace/.metis<br/>memory, skills, patterns.db"]
    Home["~/.metis<br/>trace DB, sessions, keys, billing"]
  end

  subgraph Providers["External providers"]
    Anthropic["Anthropic API"]
    OpenAI["OpenAI API"]
    OpenRouter["OpenRouter API"]
  end

  CLI --> CLIApp
  HTTP --> Server
  SDK --> Gateway

  CLIApp --> Sessions
  Server --> Sessions

  Gateway --> Canonical
  Gateway --> Routing
  Gateway --> Adapters

  Sessions --> Canonical
  Sessions --> Routing
  Sessions --> Tools
  Sessions --> Memory
  Sessions --> Adapters

  Routing --> Patterns
  Tools --> Workspace
  Memory --> Workspace

  Adapters --> Anthropic
  Adapters --> OpenAI
  Adapters --> OpenRouter

  Sessions --> Bus
  Gateway --> Bus
  Tools --> Bus
  Routing --> Bus
  Adapters --> Bus

  Bus --> Trace
  Bus --> Eval
  Eval --> Patterns
  Trace --> Analytics
  Workspace --> Patterns
  Home --> Trace
  Home --> Analytics
```

### Agent path

The agent path is used by `metis chat`, `metis tui`, and `metis serve`.
Metis owns the conversation lifecycle:

1. A client submits a user turn.
2. `SessionManager` persists the user message and assembles context.
3. `RoutingEngine` chooses a model through the seven-slot chain.
4. A provider adapter translates canonical messages into provider wire format.
5. The adapter streams canonical events back to the session manager.
6. If the model requests tools, `ToolDispatcher` executes them inside the
   workspace guardrails and feeds results back into the turn loop.
7. Usage, cost, assistant messages, routing decisions, tool calls, and turn
   boundaries are emitted to the event bus.
8. The trace store, evaluator, pattern store, and analytics layer project from
   that event stream.

This path is where bounded memory, skills, tool execution, prompt-cache
discipline, and planner-worker delegation live.

### Gateway path

The gateway path is used when existing tools point their API base URL at
Metis. It keeps the client's agent loop intact:

1. A client sends `POST /v1/messages` or `POST /v1/chat/completions`.
2. The gateway authenticates the `gw_...` key and checks quotas.
3. The inbound OpenAI or Anthropic-shaped request is translated into canonical
   messages.
4. The router chooses a provider/model, usually honoring the inbound `model`
   field as an explicit per-message override.
5. The selected adapter calls Anthropic, OpenAI, or OpenRouter.
6. The response is translated back into the original provider shape.
7. Trace and cost events are stamped with `gateway_key_id`, inbound shape,
   user, and team.

The gateway deliberately does **not** compose memory, load skills, run tools,
or persist conversations. That boundary is the point: the gateway is the
drop-in adoption path; the full agent is the richer optimization path.

### Routing in depth

Every turn picks a model by walking a **seven-slot priority chain**. The first
slot that names a model AND passes capability validation wins; the rest of the
chain is recorded but not consulted. The same chain runs in both the agent
path and the gateway path — what changes is which slots have inputs.

| Slot | Policy                | Reads from                              | Typical winner when                       |
| ---- | --------------------- | --------------------------------------- | ----------------------------------------- |
| 1    | `per_message_override`| `@model` prefix in current user message | User types `@haiku do X`                  |
| 2    | `manual_sticky`       | Session active model (set via `/model`) | Session was pinned earlier                |
| 3    | `rule`                | `.metis/routing.yaml` predicates        | First rule predicate matches the turn     |
| 4    | `pattern`             | `.metis/patterns.db` K-NN match         | Recommendation confidence ≥ gate          |
| 5    | `delegate_request`    | Worker tier (inside a `delegate()` call)| Planner spawned a worker for this sub-task|
| 6    | `workspace_default`   | `routing.yaml workspaces[].default`     | Workspace default is set                  |
| 7    | `global_default`      | `routing.yaml global_default`           | Fallback for everything else              |

Each slot produces one of three verdicts in the recorded chain:

- `not_applicable` — slot has no model (e.g. no `@` prefix); chain continues.
- `chose` — model passes validation; chain stops, this is the winner.
- `rejected` — slot named a model but validation failed; chain continues.

**Validation gate.** When a slot names a model, Metis checks `(provider, model)`
availability and the model's `AdapterCapabilities` against the turn's needs:

```
not_configured              # model id unknown in the registry
provider_unavailable        # availability state machine is in cooldown
no_vision_support           # turn has images, model doesn't
exceeds_context_window      # estimated tokens > model's max
no_tool_support             # turn has tools, model can't use them
no_system_prompt_support    # turn has a system prompt, model can't accept one
no_structured_output_support
```

This is what makes the chain **substitutability-safe**: a rule that says "use
sonnet for refactors" can be overridden by validation when the turn has images
and sonnet on this provider doesn't support vision. The chain falls through to
slot 4 / 5 / 6 / 7 automatically; the buyer-visible result is "the next slot
that *can* handle this turn wins."

**Availability tracking.** The validation gate's `provider_unavailable` check
reads a small state machine in [`routing/availability.py`](packages/metis/src/metis/core/routing/availability.py).
Models go Unavailable on 5 consecutive failures within 2 minutes; entire
providers go Unavailable on any `AUTH` error or on ≥2 `NETWORK` errors within
30 seconds (a single SSL hiccup no longer blacks out the whole provider).
States auto-recover after 5 minutes of no attempts, or on the first successful
call.

**The interesting slot is 4.** When a workspace has `.metis/patterns.db` and
the pattern store resolver is wired, slot 4 does this:

1. Build a `FingerprintInputs` from the turn (workload id + user text + tool /
   image signals + token bucket).
2. Compute a structural fingerprint (or hybrid embedding fingerprint in v2 mode).
3. Call `PatternStore.recommend(fp)`, which does K-NN against historical
   outcomes and scores candidates as `(1 − cost_weight) × success_mean +
   cost_weight × cost_efficiency`.
4. If the resulting confidence clears `PatternConfig.min_confidence` (default
   `0.05`), the slot wins and emits `pattern.matched` alongside `route.decided`.

This is how Metis "learns" routing: every turn's outcome (cost, latency, eval
score) feeds the pattern store, and similar future turns can pick a model that
historically did well on that fingerprint shape. The pattern slot defers to
user intent (slots 1-2) and explicit policy (slot 3) so learned behavior never
silently overrides what the user or operator asked for.

**Per-turn artifact.** Every decision emits exactly one `route.decided` event,
including on hard failure. The event carries the full chain (all seven slots'
verdicts), the winner index, elapsed milliseconds, and the chosen model — the
dashboard's routing breakdown reads from this stream.

The full mechanics live in [`docs/specs/routing-engine.md`](docs/specs/routing-engine.md);
the policy file shape and predicate set are in §5 there.

### Memory in depth

Two small markdown files per workspace, both bounded:

```
<workspace>/.metis/MEMORY.md    soft cap 2 KB,  hard cap 4 KB
<workspace>/.metis/USER.md      soft cap 1.5 KB, hard cap 3 KB
```

Both are plain text on disk: editable, diffable, git-syncable. `MEMORY.md`
holds durable workspace facts ("tests live in `tests/`; auth uses bcrypt");
`USER.md` holds facts about the human ("user prefers Go; on a Mac"). The byte
budget is intentional — *eviction is a feature*, not a bug. Bounded memory
forces the agent to decide what's worth keeping; unbounded vector stores tend
to drift toward retrieval-of-irrelevant fragments.

**Read path.** Every turn, the session manager composes the final system
prompt as `base + USER.md + MEMORY.md` via [`MemoryStore.assemble_system_prompt`](packages/metis/src/metis/core/memory/store.py).
Empty files are omitted. The composed memory sits in the *volatile* segment
of the two-segment system prompt — **after the prompt-cache breakpoint** on
Anthropic — so writes to MEMORY.md mid-session don't invalidate the cached
prefix (tools + base persona). That's how the memory persistence stays
cache-compatible: the agent gets persistent context for ~free per turn.

**Write path — three tools.**

| Tool                  | Purpose                                       |
| --------------------- | --------------------------------------------- |
| `memory_add`          | Append a single entry to `MEMORY.md` or `USER.md` |
| `memory_replace`      | Edit a specific entry (`old_text` → `new_text`)   |
| `memory_consolidate`  | Replace the entire file with a rewritten compact version |

The decision of *what to remember* is the LLM's, not the framework's. Metis
provides primitives, surfaces the current contents in the system prompt every
turn, and includes soft-cap pressure signals in tool results — the agent
applies its own judgment about durable preferences vs one-off conversation
context. The base system prompt is intentionally short; the tool descriptions
themselves carry the only semantic anchoring ("workspace facts" vs "user
facts", "use sparingly").

**Soft cap vs hard cap.**

- `size < soft_cap` — write succeeds silently.
- `soft_cap ≤ size < hard_cap` — write succeeds AND the tool result appends
  `"over soft cap; consider memory_consolidate"`. The `memory.eviction` event
  fires on the bus as a signal to analytics or future curators. The agent
  typically calls `memory_consolidate` on its next turn.
- `size ≥ hard_cap` — write *rejected* with `MemoryHardCapExceeded`. The
  agent must consolidate before any further adds.

Bytes never leave the file without the agent explicitly rewriting them. No
silent garbage collection.

**Sharing model — workspace-scoped, not session-scoped, not user-scoped.**
The `MemoryStore` is keyed only on workspace path:

| Boundary                                                    | Shared? |
| ----------------------------------------------------------- | ------- |
| Two sessions in the same workspace                          | Yes — same files |
| Different workspaces                                        | No — completely separate |
| Planner session and its delegated worker                    | No — workers have memory tools removed |
| Multi-user identity (Wave 8 user_id stamping)               | Same files (limitation, not per-user) |

The mental model: **memory is a property of the workspace directory**, not of
the agent or the user. When you `cd` into a directory and start `metis chat`,
you inherit the agent's accumulated memory about that codebase.

**Worker isolation.** When a planner spawns a worker via `delegate()`, the
session manager filters memory tools out of the worker's dispatch and sets
the worker's `MemoryStore` to `None`. Workers are stateless contractors —
they finish their sub-task and exit, and they can't pollute the planner's
persistent memory with sub-task noise.

The full mechanics live in [`docs/specs/memory-store.md`](docs/specs/memory-store.md);
the cache-breakpoint placement is in [`docs/specs/context-assembler.md §3`](docs/specs/context-assembler.md).

### Core design choices

- **Canonical message format.** One internal representation for messages, content blocks, and tool calls. Provider adapters serialize to and from each provider's wire format. Adding a provider is writing an adapter, not refactoring the system.
- **Seven-slot routing.** Per-message override → manual sticky model → configured yaml rules → learned pattern recommendation → delegate request → workspace default → global default. User intent and policy beat learned behavior; every decision is recorded with a full chain trace.
- **Bounded, portable memory.** `MEMORY.md` (~2 KB) and `USER.md` (~1.5 KB) per workspace, agent-curated. Markdown on disk; edit, version, and sync via git.
- **Skills as portable markdown.** Compatible with the agentskills.io open standard; hand-written, auto-generated, or installed.
- **Event bus + trace store.** Every meaningful action emits a structured event. Analytics, dashboards, and replay all consume the same stream.
- **Cost-aware.** Tokens and USD are tracked per turn/request, attributed to model, gateway key, user/team, and role (planner vs delegated worker). Costs are computed with Decimal math, not provider-rounded floats.
- **Evidence loop.** Evaluator verdicts update pattern outcomes, pattern outcomes can inform future routing, and analytics uses the same trace data as the dashboard and buyer reports.

The component contracts live under [`docs/specs/`](docs/specs/).

## Operations

[`docs/operations/`](docs/operations/) holds the playbooks an SRE will
read before signing — quickstart (kind + helm + `metis trial`), incident
response, status-page recipe, SLA template, observability runbook, and
the SOC2 readiness audit.

## Project status

Phase 1 + Phase 2 + Phase 2.5 + Phase 3 shipped. Transparent gateway,
multi-user / per-team attribution, evaluator, compliance posture, billing,
observability, and the operational playbooks are all live.

The validated cost-savings headline is **delegation at 8.3% – 26.1% better
cost-per-quality** (19.9% midpoint) across three independent A3 runs on the
fan-out workload. Slot-4 model selection remains a proof-of-mechanism from
§A3-rev3 — §A3-rev7 didn't generalize it (zero sonnet picks across 36 routing
decisions), so the task-domain wedge is deferred post-GA. See
[`docs/savings-demo.md`](docs/savings-demo.md) for the full evidence and
[`docs/customer-trial-recipe.md`](docs/customer-trial-recipe.md) for the
reproducer.

## What's NOT built yet (next-up)

- **Context-assembler v3 skill activation.** Prompt-cache discipline and minimum-cacheable-prefix padding are live; explicit / agent-side skill activation budgets remain post-GA.
- **Skill curator.** The spec exists, but the implementation is gated on agent-authored skills (`skill_save` + `skill.created(source="auto_generated")`) landing first.
- **Delegation v1 follow-ons.** Async/concurrent workers, cancellation cascade, streaming worker output, recursive delegation, `output_schema`, per-tier timeouts, router-decided delegation, and worker pattern-store integration are deferred.
- **Pattern-store v2 cluster-tightening against real traces.** The synthetic geometry gate passes; a real-embedding / real-API fixture is still a confidence check.
- **§A3 task-domain model-selection wedge.** Math/symbolic, long-context synthesis, and rare API workloads are the next research wedge; deferred post-GA unless buyer evidence reprioritizes it.

See [`docs/KNOWN_ISSUES.md`](docs/KNOWN_ISSUES.md) for spec/impl gaps that are tracked but not yet fixed.

## Roadmap

| Phase   | Status   | Headline deliverable                                                                              |
|---------|----------|---------------------------------------------------------------------------------------------------|
| **1**   | shipped  | Two providers, canonical format, event bus, file/shell tools, basic TUI, manual routing.          |
| **2**   | shipped  | Hand-written skills, bounded memory, web dashboard, explicit feedback, configured rules.          |
| **2.5** | shipped  | Pattern fingerprints, cold-start suggestions, skill auto-generation with security scanner.        |
| **3**   | shipped  | Transparent gateway, multi-user attribution, evaluator, compliance / hardening.                   |
| **4**   | next     | Tauri desktop app, public-ready UX, marketplace foundation, skill curator, delegation follow-ons. |

## Documentation

The full documentation site is built from [`docs/`](docs/) with
[mkdocs-material](https://squidfunk.github.io/mkdocs-material/). Four
top-level sections — **Getting Started**, **Specs**, **Operations**,
**Reference** — with full-text search and per-page GitHub edit links.

```bash
# Local preview (mkdocs-material installed on demand):
uv run --with mkdocs-material mkdocs serve

# Or via Docker (mirrors the gateway shape; serves on 127.0.0.1:8423):
docker compose --profile docs up docs
```

The nav config and theme are in [`mkdocs.yml`](mkdocs.yml); the
container build lives at [`infra/docs/`](infra/docs/). The site is pure
static once built (`mkdocs build` writes to `site/`) so any static host
works for production.

The design is specified before code lands. Start here:

**Project context** (read these first if you're new):

- [AGENTS.md](AGENTS.md) — current state of the codebase, conventions, gotchas. Load-bearing for AI agents.
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

Apache License 2.0 — see [LICENSE](LICENSE) for the full text.

The OSS substrate in this repo is permissively licensed so a CTO doesn't need legal review to install. The paid-tier overlay (`metis-pro`) lives in a separate private repo under a different license; the architectural boundary between the two is exposed through the extension Protocols in [`packages/metis/src/metis/core/extensions.py`](packages/metis/src/metis/core/extensions.py).

Contributions to this repo are accepted under the same Apache-2.0 terms (see [CONTRIBUTING.md](CONTRIBUTING.md)).
