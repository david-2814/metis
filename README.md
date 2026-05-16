# Metis

A local-first AI dev agent — provider-agnostic, self-improving, and cost-aware.

> **Status:** Phase 1 + Phase 2 + Phase 2.5 shipped; Phase 3 in flight — **ready for review whether to promote to "Phase 3 shipped"** ([proposal](docs/operations/phase-claim-proposal.md)). The three Phase-3 wedges (transparent HTTP gateway, multi-user identity / per-team cost attribution, evaluator) are live; Wave 12 closes the SOC2/GDPR compliance gap (audit log + trace retention + redaction layer + GDPR data export/forget + SOC2 readiness audit); Wave 13 lifts the gateway's loopback-only constraint behind a documented hardening checklist; Wave 14 lands the v1.2 partial-credit rubric primitive, self-serve `/signup` + `/account/keys` endpoint, mkdocs-material doc site, sales toolkit, and a [GA-readiness audit](docs/operations/ga-readiness-audit.md) (which surfaced two owner-triage GA blockers — NETWORK error class trips provider-wide on a single SSL hiccup, and the gateway over-reports cost ~6× when SDK clients strip the `anthropic:` prefix). The model-selection differentiator inverted on its first end-to-end demonstration ([`benchmarks/RESULTS.md §A3-rev3`](benchmarks/RESULTS.md): Pass C picks sonnet on the one hard turn of `regex-with-edge-cases`, quality 5.55 vs Pass A 5.16 at $0.0477/quality between haiku-only $0.0383 and sonnet-only $0.1176); §A3-rev4..rev6 confirmed the mechanical chain is fully wired and the remaining bottleneck is benchmark-suite signal strength, not routing-knob tuning. §A3-rev7 (Wave 14a-7) tested the finer-grained-outcome-scoring wedge end-to-end but **aborted partway through Pass B on Anthropic-credit exhaustion** (~$1.08); the 2 workloads with complete haiku + sonnet partial-credit data show a +0.000 gap (direct evidence that haiku-4.5 is genuinely strong on dev-loop coding at temperature=0), with `regex-with-edge-cases` mid-scores 0.63-0.75 as the residual signal. The delegation differentiator was validated end-to-end at 8.3% – 26.1% better cost-per-quality on a delegation-suited workload (§A3-rev5 + §A3-rev6); delegation now leads the GTM ordering for routing-surface levers. Three providers (Anthropic / OpenAI / OpenRouter) drive end-to-end turns with streaming, tool use, bounded memory, cost tracking, event tracing, SQLite-persisted sessions. `metis chat` (line REPL), `metis tui` (Textual TUI), `metis serve` (HTTP/WebSocket), `metis gateway` (transparent provider-shape proxy with optional `/signup`) all run. Wave 14a closes the production-grade observability gap with latency-percentile histograms (routing + tool dispatch), dedicated LLM/tool error counters, the new `gateway.auth_failed` audit-flagged event + per-key cost counter, four `PrometheusRule` alert templates (LLM p99 latency, LLM error rate, gateway auth-failure rate, per-key spend anomaly), a Grafana dashboard JSON, and the [observability runbook](docs/operations/observability-runbook.md). Wave 15 closes both Wave-14 GA blockers (NETWORK error refinement — single SSL hiccup no longer blacks out the whole provider; gateway model normalization — SDK-canonical bare names like `claude-3-5-haiku-20241022` now resolve to the canonical provider id before routing instead of falling through to `global_default`), ships first-buyer concierge tools (`metis customer-report`, `metis trial-status`, [`concierge-onboarding.md`](docs/operations/concierge-onboarding.md), additive `customer_tier` keystore field), lands the Wave 15 [billing module](apps/gateway/src/metis_gateway/billing/) per the ratified [`pricing.md §5.5.4`](docs/specs/pricing.md) (Stripe-backed Pro per-seat + reserved enterprise %-of-savings metered add-on; six new `billing.*` audit events; opt-in via `--enable-billing`), and adds the [status-page live-deployment recipe](docs/operations/status-page.md) (helm Uptime Kuma sidecar + four monitoring probes + SEV-mapped templates; hosting account remains owner-side). The phase-claim bump remains owner-decision territory — the proposal is unchanged from Wave 13 and no sign-off is recorded. 1829 tests passing.

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

## Try it — first savings number in &lt; 1 hour

The smoothest landing path: kind cluster + helm install + pre-baked
workload + per-key cost rollup, automated end-to-end.

```bash
echo "ANTHROPIC_API_KEY=sk-ant-..." > .env
infra/gateway/scripts/quickstart.sh           # cluster + helm install + first key
source .metis-trial/state.env                 # exports gateway URL + key
uv run metis trial \
    --gateway-url "$METIS_TRIAL_GATEWAY_URL" \
    --gateway-key "$METIS_TRIAL_GATEWAY_KEY"
# → prints `actual / baseline / savings_pct` for the pre-baked workload
infra/gateway/scripts/tear-down.sh            # when done
```

Full step-by-step (curl + Python SDK examples, dashboard view, pitfalls
table) at [`docs/operations/quickstart.md`](docs/operations/quickstart.md).

## Try it — transparent gateway in Docker

Prefer Docker Compose over kind? Same loop, single host:

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

## Sales toolkit

The docs a salesperson reads before a buyer conversation. All sit
under [`docs/sales/`](docs/sales/):

- [`one-pager.md`](docs/sales/one-pager.md) — single-page pitch with the headline numbers, honest caveats, and a deployment-shape grid.
- [`competitive-comparison.md`](docs/sales/competitive-comparison.md) — Metis vs LiteLLM / Portkey / Helicone: canonical-IR fidelity, learned routing, per-user / per-team attribution, where each competitor wins.
- [`objection-handling.md`](docs/sales/objection-handling.md) — common buyer objections (Vercel AI SDK, Cursor / Claude Code, LiteLLM-is-good-enough, unproven-savings, operational-load, SOC2, "are you going to be around") with honest responses.
- [`faq.md`](docs/sales/faq.md) — buyer FAQ: how it works, how it compares, how to evaluate, what's the savings number, what's the SOC2 story, where does data go, what's the roadmap.
- [`case-study-template.md`](docs/sales/case-study-template.md) — slot to be filled in by the first GA customer; honest framing with reproducible numbers.

## Operations

The operational docs a buyer's SRE will read before signing. All sit
under [`docs/operations/`](docs/operations/):

- [`quickstart.md`](docs/operations/quickstart.md) — &lt; 1-hour buyer-trial path: kind + helm + `metis trial` end-to-end, with per-key cost rollup and a pitfalls table from validation.
- [`incident-response.md`](docs/operations/incident-response.md) — SEV1-SEV4 criteria, on-call alert paths (PagerDuty / Opsgenie / email), first-hour playbook, post-mortem template, and per-failure-mode playbooks for upstream LLM outage, trace-DB corruption, gateway-key compromise, and quota runaway.
- [`status-page.md`](docs/operations/status-page.md) — two-tier recipe (external UptimeRobot / Statuspage.io / Better Stack against `/healthz`, plus self-hosted Uptime Kuma in-cluster), publish/redact guidelines, and incident comm templates.
- [`sla-template.md`](docs/operations/sla-template.md) — 99.5% single-region template the buyer can customize for their own downstream-user SLA: service-credit math, exclusions, force-majeure stub (legal-counsel-deferred).
- [`compliance-overview.md`](docs/operations/compliance-overview.md) + [`soc2-readiness.md`](docs/operations/soc2-readiness.md) — one-page buyer-conversation index and the full SOC2 Trust Service Criteria gap audit (Security CC1-CC9, Availability A1, Confidentiality C1, Processing Integrity PI1, Privacy P1-P8) mapped against shipped + buyer-responsibility evidence. Honest about gaps (CC8 change management, third-party pentest, vendor review, SOC2 auditor); Type 1 readiness target Q3 2026 contingent on buyer underwriting the audit cost.

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
- **1829 tests** across canonical round-trips, JSON Schema enforcement, role-content invariants, event catalog, bus dispatch + filtering, workspace escape rejection, dispatcher + confirmation, adapter wire translation + streaming + error classification + retry + cancellation, cross-provider conformance, routing chain + rule loading + predicates + NETWORK-error escalation refinement, memory store + tools, session manager + persistence + streaming, HTTP REST + WebSocket + token registry + confirmations, pattern store v1 + v2 + concurrency hardening, evaluator heuristic + LLM + hybrid + budget + partial-credit primitive, gateway auth + per-key/user/team identity + rate limiting + TLS + bind hardening + self-serve signup + bare-model normalization + auth-failure event emission + `customer_tier` keystore extension, audit log + trace retention + redaction layer + GDPR export/forget, observability metric collector + Prometheus exposition + latency-percentile histograms + dedicated error counters + per-key cost attribution, billing module subscription lifecycle + webhook idempotency + tier-axis quota composition + Stripe `FakeBillingClient`, `metis customer-report` HTML offline-contract + XSS escaping + JSON determinism, `metis trial-status` conversion-readiness bands + threshold pinning.

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

The full documentation site is built from [`docs/`](docs/) with
[mkdocs-material](https://squidfunk.github.io/mkdocs-material/). Four
top-level sections — **Getting Started**, **Specs**, **Operations**,
**Strategy** — with full-text search and per-page GitHub edit links.

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
