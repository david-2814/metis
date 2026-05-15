# AGENTS.md

Shared context for any AI agent working in this repo (Claude Code, Cursor, Codex, Aider, etc.). `CLAUDE.md` is a symlink to this file.

## What Metis is

A local-first AI dev agent server. Provider-agnostic via a canonical message format, with a layered routing engine (manual / configured rules / learned patterns), bounded portable memory, and an event bus that feeds a trace store. Python core server; thin clients (CLI first, TUI/Tauri later).

**Status:** Phase 1 + Phase 2 + Phase 2.5 shipped (pattern store wired into routing slot 4; evaluator heuristic + LLM + hybrid tiers live on the bus, `/analytics/quality` endpoint exposed). Phase 3 in flight: the transparent HTTP gateway is wired end-to-end (OpenAI + Anthropic inbound shapes, sync + SSE, gateway-key auth, `gateway_key_id` stamped on trace events) and live-smoked at ~$0.0002 / 4 calls on 2026-05-14; per-key analytics is exposed via `/analytics/cost?group_by=gateway_key` and `/analytics/by_key`. Wave 8a additionally landed per-user / per-team identity in the gateway: keys now carry optional `user_id` / `team_id` tags, `llm.call_completed` and `turn.completed` stamp typed `user_id` / `team_id` fields, and `/analytics/cost?group_by=user|team` + `/analytics/by_user` + `/analytics/by_team` roll up cost / tokens / calls per identity (multi-user.md §3 / §4). The model-selection differentiator (routing slot 4 reading cross-model outcomes) is mechanically wired across three Wave-8a unblocks (workload-tagged fingerprints, `cost_weight=0.1`, grounding-check rubric primitive) but **the differentiator still does not invert** — Pass C of [`benchmarks/RESULTS.md §A3-rev2`](benchmarks/RESULTS.md) shows slot 4 emits `not_applicable` on all 18 routed turns and slot 7 (`global_default`) wins every time. The K-NN itself is reading correctly (same-workload partition works; on `write-a-doc-from-notes` Pass C turn 2 the K-NN aggregated sonnet=0.900 ahead of haiku=0.842 — the first time in any A3 series), but the score gaps under `cost_weight=0.1` collapse below the unchanged `min_confidence=0.3` gate. Wave 9 candidate: lower `PatternConfig.min_confidence` to ~0.05 (or reshape the confidence formula) so the workload-level quality deltas the K-NN now sees can actually fire slot 4. The CLI (`metis chat <workspace>`) drives end-to-end turns across Anthropic / OpenAI / OpenRouter with tool use, streaming, cost tracking, and event tracing. Bounded MEMORY.md / USER.md gives the agent cross-session continuity. `metis serve` exposes an HTTP/WebSocket surface for external clients with live token-delta streaming; `metis gateway` exposes a provider-API-shaped HTTP surface for unmodified OpenAI/Anthropic clients with per-key / per-user / per-team cost attribution. 1239 tests passing.

**Repo shape.** uv-workspace monorepo: `packages/metis-core/` (the library — canonical types, events, adapters, routing, tools, memory, sessions, pricing, skills, trace), `apps/server/` (HTTP/WS server), `apps/gateway/` (transparent provider-shape HTTP proxy), `apps/cli/` (chat / tui / serve / gateway entry point). One `metis` console-script is shipped by `metis-cli`; it depends on `metis-core`, `metis-server`, and `metis-gateway`.

## Implementation status

**What works:**

- **Canonical types** ([packages/metis-core/src/metis_core/canonical/](packages/metis-core/src/metis_core/canonical/)) — `Message`, `Role`, content blocks (Text, Image, ToolUse, ToolResult, Thinking, RedactedThinking), `ToolDefinition`, `AdapterCapabilities`, `Usage`, `RoutingDecisionRecord`. `msgspec`-backed, JSON-roundtrippable, fully validated against canonical-format §5.
- **Event bus + trace store** ([packages/metis-core/src/metis_core/events/](packages/metis-core/src/metis_core/events/), [packages/metis-core/src/metis_core/trace/](packages/metis-core/src/metis_core/trace/)) — full catalog (31 event types across session/turn/llm/tool/route/skill/memory/pattern/eval/bus domains), bounded async dispatch, fast-path / non-fast-path subscribers, SQLite WAL+NORMAL writer, replay query, causal-chain walk. Bus lifecycle is observable: `bus.subscriber_registered` / `bus.subscriber_unregistered` fire on every `subscribe()` / unsubscribe, and `bus.gap_detected` fires when the per-subscription monotonic frame counter detects a dropped event (overflow on a bounded queue).
- **Tool dispatcher** ([packages/metis-core/src/metis_core/tools/](packages/metis-core/src/metis_core/tools/)) — registry + JSON-Schema validation + confirmation policy + 5 file/shell builtins + 3 memory tools. Workspace-scoped file API rejects `..` and out-of-root symlinks.
- **Provider adapters** — Anthropic ([adapters/anthropic.py](packages/metis-core/src/metis_core/adapters/anthropic.py)), OpenAI ([adapters/openai.py](packages/metis-core/src/metis_core/adapters/openai.py)), OpenRouter ([adapters/openrouter.py](packages/metis-core/src/metis_core/adapters/openrouter.py)). Each implements wire translation, error classification (8-class), bounded retry with `retry_after`, cancellation, per-model `AdapterCapabilities`, and `stream()` returning canonical streaming events. Cross-provider continuity verified end-to-end against real APIs (Anthropic→OpenAI→OpenRouter mid-session with tool-use round-trip).
- **Routing engine** ([packages/metis-core/src/metis_core/routing/](packages/metis-core/src/metis_core/routing/)) — full 7-slot chain (per-message override / manual sticky / configured rules / pattern / delegate [stub] / workspace default / global default) with capability validation and per-(provider, model) availability tracking. Emits exactly one `route.decided` event per turn, including on hard failure. YAML rule policy is parsed from `<workspace>/.metis/routing.yaml` (per `routing-engine.md §5`); the `rule` slot reports a real verdict, not `not_applicable`. Slot 4 (`pattern`) consults the per-workspace `PatternStore` when a resolver is injected and emits `pattern.matched` on a win.
- **Session manager** ([packages/metis-core/src/metis_core/sessions/manager.py](packages/metis-core/src/metis_core/sessions/manager.py)) — turn-locked streaming model loop, multi-call within a turn, tool cycle wiring, cost stamping, full event emission, optional streaming-event handler for live token deltas.
- **Bounded memory** ([packages/metis-core/src/metis_core/memory/](packages/metis-core/src/metis_core/memory/)) — per-workspace `MEMORY.md` (~2 KB) and `USER.md` (~1.5 KB) under `.metis/`. Soft cap → `memory.eviction` event; hard cap → write rejected. `memory_add`/`memory_replace`/`memory_consolidate` tools mutate them. `SessionManager` composes USER.md + MEMORY.md into the system prompt fresh each LLM call.
- **Session message persistence** ([packages/metis-core/src/metis_core/sessions/sqlite_store.py](packages/metis-core/src/metis_core/sessions/sqlite_store.py)) — SQLite-backed session/message store per `canonical-message-format.md §9.1`. Wired by default in the CLI runtime; `InMemorySessionStore` remains for tests.
- **HTTP/WebSocket server** ([apps/server/src/metis_server/](apps/server/src/metis_server/)) — Starlette + uvicorn ASGI app. REST endpoints for sessions/turns/messages/models/health; WebSocket `/sessions/{id}/stream` with single-use attach tokens, snapshot+live, `preset:chat`/`preset:full` filters, cancel-via-WS, ping/pong. Streaming-only events (`message.*`, `text.delta`, `tool.use_*`) flow through a per-session `StreamingHub` and reach connected clients live; bus catalog events flow through the normal Subscription path. Loopback-only bind in v1.
- **CLI** ([apps/cli/src/metis_cli/](apps/cli/src/metis_cli/)) — `metis chat <workspace>` (line REPL), `metis tui <workspace>` (Textual TUI), `metis serve <workspace>` (HTTP/WS server), `metis gateway` (transparent gateway server) and `metis gateway issue-key --name … --workspace …` (key issuance). Slash commands `/model`, `/cost`, `/models`, `/help`. Per-message `@alias` override syntax.
- **Transparent HTTP gateway** ([apps/gateway/src/metis_gateway/](apps/gateway/src/metis_gateway/)) — Starlette ASGI app exposing `POST /v1/chat/completions` (OpenAI shape) and `POST /v1/messages` (Anthropic shape), each in sync + SSE flavors. Authenticates per-request via `Authorization: Bearer gw_…` (or `x-api-key` for Anthropic clients), maps the bearer hash to a `GatewayKey` scoped to one workspace, routes via `metis_core.routing.RoutingEngine`, and writes `route.decided` / `llm.call_started` / `llm.call_completed` / `turn.completed` events with `gateway_key_id` + `inbound_shape` stamped on the LLM/turn payloads. Per-request stateless — no session manager, no tool dispatcher, no memory store (gateway.md §2). Loopback-only bind. Keys live in `~/.metis/gateway/keys.json` (default); the plaintext token is printed once by `metis gateway issue-key` and only the SHA-256 hash is persisted.
- **Smoke harnesses** ([scripts/smoke.py](scripts/smoke.py), [scripts/smoke_cross_provider.py](scripts/smoke_cross_provider.py), [scripts/smoke_cache.py](scripts/smoke_cache.py)) — drive the loop against real APIs; cross-provider test passes at ~$0.007.
- **Configured routing rules** ([packages/metis-core/src/metis_core/routing/policy.py](packages/metis-core/src/metis_core/routing/policy.py)) — per-workspace YAML rule file at `<workspace>/.metis/routing.yaml`; matches turn inputs against rule predicates and emits a real verdict in the `rule` slot of `route.decided.chain` (shipped commit `e71fedd`).
- **Tool-confirmation REST endpoint** — `POST /turns/{id}/confirmations/{request_id}` wired against a per-turn request registry bridged to the `tool.confirmation_requested` event stream (shipped commit `e71fedd`).
- **CLI confirmation handler** ([packages/metis-core/src/metis_core/tools/cli_confirmation.py](packages/metis-core/src/metis_core/tools/cli_confirmation.py)) — terminal-prompting handler wired as the default for `metis chat` / `metis tui`. NONE/READ side effects auto-approve; WRITE/EXECUTE/NETWORK consult `<workspace>/.metis/trust.yaml` (`always_allow` / `always_deny` lists) and otherwise prompt the user, with "always" / "never" answers persisting back to the file. `--auto-allow` reverts to the old `AutoAllowHandler`.
- **Persisted skills** — `SkillStore` and the `skill_load` tool, agentskills.io-compatible (shipped commit `e71fedd`).
- **Savings benchmark suite** ([scripts/benchmark.py](scripts/benchmark.py), [benchmarks/workloads/](benchmarks/workloads/)) — versioned workload bundle + harness; six workloads in suite v1 (`fix-a-bug-small`, `multi-turn-refactor`, `write-a-doc-from-notes`, `intentionally-failing-task`, plus diversity-wave additions `regex-with-edge-cases` and `multi-file-refactor-with-shared-types`). The two diversity workloads are deliberately failure-prone in the haiku case so per-workload quality scores discriminate models. Run 3 captured in [benchmarks/RESULTS.md](benchmarks/RESULTS.md). Spec at `docs/specs/benchmark.md`.
- **Prompt caching (universal, live-validated)** — Anthropic `cache_control` breakpoints attached to the stable prefix (tools + system); v2 of the context assembler adds a minimum-cacheable-prefix rule that pads the prefix above the effective haiku-4-5 cache floor (`MIN_CACHEABLE_PREFIX_TOKENS = 4500`, `MAX_CACHEABLE_PREFIX_TOKENS = 5500` heuristic tokens; padding sources are loaded skills first, then a deterministic `_OPERATING_CONTEXT_PADDING` block). Spec at `docs/specs/context-assembler.md §5.1`. Two-segment `system_prompt` field on `CanonicalRequest` separates stable vs mutating instructions. Benchmark Run 3 (see [benchmarks/RESULTS.md §Run 3](benchmarks/RESULTS.md)): cache fires on **49 of 49 LLM calls (100%)** in the 6-workload suite vs Run 2 cold's **10 of 30 (33%)**; same-3-workload aggregate cost dropped 22.8%.
- **Analytics + dashboard** — `AnalyticsStore.savings()` and the metric dashboard surface ([apps/server/src/metis_server/](apps/server/src/metis_server/)); spec at `docs/specs/analytics-api.md`.
- **Pattern store** ([packages/metis-core/src/metis_core/patterns/](packages/metis-core/src/metis_core/patterns/)) — per-workspace, bounded SQLite store of structural fingerprints + Welford-accumulated outcomes (cost, latency, success score). Wires routing slot 4 (`PATTERN_RECOMMENDATION`) when a `pattern_store_resolver` + `fingerprint_inputs_builder` are injected; emits `pattern.recorded` / `pattern.matched` / `pattern.evicted`. Spec at `docs/specs/pattern-store.md`.
- **Evaluator (heuristic + LLM + hybrid tiers)** ([packages/metis-core/src/metis_core/eval/](packages/metis-core/src/metis_core/eval/)) — bus subscriber + `HeuristicJudge` / `LLMJudge` / `HybridJudge` for turn / tool-cycle / session / workload subjects. Subscribes to `turn.completed` / `tool.completed` / `tool.failed` / `session.ended` and emits `eval.started` / `eval.completed` / `eval.failed` per `docs/specs/evaluator.md`. `metis evaluate --db-path … --subject turn` re-runs the judge over a window for batch re-evaluation; `workload.yaml.evaluate` feeds the benchmark suite's quality column. `BudgetTracker` (per-session $0.10 / per-day $1.00 defaults) is shared across LLM-eligible judges; over-budget calls land a `signals.budget_exhausted=True` verdict and HybridJudge falls back to its heuristic verdict. `HybridJudge(escalation_threshold=0.7)` is the default — heuristic confidence at or above the threshold short-circuits the LLM call. Tool-cycle and session subjects remain heuristic-only in v1 (evaluator.md §5.5 / §5.6).
- **Quality analytics** — `/analytics/quality` endpoint projects `eval.completed` over a time window with `group_by` ∈ {model, judge_kind, rubric_id, none} and `min_confidence` filter. The `chosen_model` field joins via `route.decided` so the per-model rollup shows which *judged* model scored best, not the judge's own model. Spec at [docs/specs/evaluator.md §9.2](docs/specs/evaluator.md).
- **Per-gateway-key analytics** — `/analytics/cost?group_by=gateway_key` and `/analytics/by_key` roll up `llm.call_completed` / `turn.completed` cost + tokens + call-count by `gateway_key_id`, with an optional `gateway_key` exact-match filter. Direct-API calls (no stamp) bucket under `gateway_key_id: null`. Closes the Wave-5 follow-on noted in `gateway.md §V`.
- **Gateway client quickstart** — [docs/gateway-client-quickstart.md](docs/gateway-client-quickstart.md) walks Claude Code / Cursor / raw SDK clients through pointing `ANTHROPIC_BASE_URL` / `OPENAI_BASE_URL` at a running gateway, with runnable examples under [examples/gateway/](examples/gateway/).
- **Benchmark harness LLM-judge integration** — `scripts/benchmark.py` exposes `--judge {heuristic,hybrid,llm}`, `--judge-escalation-threshold`, and `--judge-model` flags that swap the per-turn and workload-level evaluator's `Judge` between `HeuristicJudge` / `HybridJudge` / `LLMJudge` without touching `metis-core`. First measured end-to-end in [benchmarks/RESULTS.md §A3](benchmarks/RESULTS.md) — a three-pass experiment (haiku → sonnet → no-active-model, sharing one patterns DB) under `--judge hybrid --judge-escalation-threshold 0.7`. Pass C lands slot 4 wins on 17 of 18 turns reading cross-model outcomes; the differentiator does **not** invert in v1 (every win picks haiku), and §A3 identifies the two specific unblocks (heuristic needs a `tool.completed.success=False` penalty; the session manager needs to forward `assistant_response_text` on `turn.completed.signals_extra` so the LLM judge can read it).
- **§A3-rev evidence** — both §A3 unblocks have landed (heuristic `weight_no_tool_exit_failure` and `SessionManager` `signals_extra` plumbing for `user_prompt_text` + `assistant_response_text`); a follow-up three-pass run is captured in [benchmarks/RESULTS.md §A3-rev](benchmarks/RESULTS.md). The unblocks fire as intended at the per-turn level (15 hybrid escalations across the three passes vs 0 in §A3-original; LLM judge returns differentiated 0.3/0.7/0.8/1.0 scores), but slot 4 in Pass C still picks haiku on all 15 routed turns — the K-NN aggregation across mixed-workload clusters + `cost_weight=0.3` consistently produces haiku-aggregated scores 0.755–1.000 vs sonnet-aggregated 0.245–0.700. §A3-rev identifies a third blocker (workload-grain K-NN clustering or `cost_weight` reduction). Total spend: $1.032.
- **§A3-rev2 evidence** — all three §A3-rev unblocks have landed (Wave 8a-1 workload-tagged fingerprint, 8a-2 `cost_weight=0.3 → 0.1`, 8a-3 grounding-check rubric primitive replacing the single-substring check on `architectural-explanation-without-hallucination`); a follow-up three-pass run is captured in [benchmarks/RESULTS.md §A3-rev2](benchmarks/RESULTS.md). The unblocks individually work as designed: workload-tag partitioning gives clean clusters (same-workload neighbors score ≥ 0.85 vs different-workload ≤ 0.15), `cost_weight=0.1` lowers the success-delta needed to flip the chooser to ~0.143, and the grounding-check eliminates §A3-rev's artifact where sonnet was wrongly penalized 0.50 on the hallucination workload (now 0.90 / 0.95). Pass B was re-run on three workloads that hit Anthropic transient connection errors so the patterns DB had 5-11 samples per (workload, model). Pass C: **slot 4 emits `not_applicable` on all 18 turns** and slot 7 (`global_default`) wins. The K-NN does read cross-model data correctly (e.g. on `write-a-doc-from-notes` Pass C turn 2, sonnet's aggregated score 0.900 is ahead of haiku 0.842 — the first time in any A3 series), but the confidence gap formula `(top-runner)/top` produces values 0.030–0.231 under `cost_weight=0.1`, all below the unchanged `min_confidence=0.3` gate. The three correct unblocks combined to gate slot 4 off entirely rather than inverting it. Wave 9 candidate: lower `PatternConfig.min_confidence` to ~0.05 (or reshape the confidence formula) — at 0.05 the `write-a-doc-from-notes` turn 2 inversion would have fired. Total spend: $1.316.
- **Gateway multi-user identity (Wave 8a-4 + 8a-5 + 8a-6)** — `GatewayKey` ([`apps/gateway/src/metis_gateway/auth.py`](apps/gateway/src/metis_gateway/auth.py)) gains optional `user_id` / `team_id` fields (`^[a-z0-9_-]+$`, ≤200 chars); `metis gateway issue-key --user <id> --team <id>` persists them. A request-scoped `Identity` projects `(gateway_key_id, workspace_path, user_id, team_id)` per multi-user.md §3.2 and the gateway harness stamps `user_id` / `team_id` as typed fields on `llm.call_completed` and `turn.completed`. The analytics surface reads them: `/analytics/cost?group_by=user|team`, `/analytics/by_user`, `/analytics/by_team` roll up cost / tokens / call-counts per identity (multi-user.md §4.1, analytics-api.md §4.1 / §4.9). Pre-multi-user keys keep `user_id: None` / `team_id: None` and bucket under the null-row convention. Agent-loop traffic (`metis chat` / `metis serve`) also stamps `None`. Closes the Wave-7-deferred multi-user-rollup gap noted in `gateway.md §11`.

**What's NOT built (next-up):**

- **Delegation** — Phase 4. The `delegate_request` slot in `route.decided.chain` still reports `not_applicable`; `include_worker_sessions` in the WS subscribe filter is accepted but no workers are spawned in v1.
- **Slot-4 confidence gating recalibration** — the load-bearing Wave 9 candidate identified by §A3-rev2. `PatternConfig.min_confidence=0.3` was calibrated for the `cost_weight=0.3` era when the cost differential alone produced ~0.35 confidence on tied-quality clusters. Under the shipped `cost_weight=0.1` the same near-tied data produces ~0.10 confidence, so slot 4 emits `not_applicable` and slot 7 wins every time. Three independent fixes are on the table (lower `min_confidence` to ~0.05; reshape `(top-runner)/top` to a `top - mean(others)` or softmax shape; lower `--judge-escalation-threshold` to ~0.5 to widen per-cluster quality deltas) — option 1 is one line.
- **Pattern-store eval-update path drops on tool-heavy 1-turn workloads** — the §A3-rev2 caveat: `architectural-explanation-without-hallucination` outcome rows show `success_score_count = 0` across all four fingerprints despite the trace recording `eval.completed kind=turn` in time order after `pattern.recorded`. `intentionally-failing-task` (also 1-turn, 0 tool calls) accumulates correctly. Open question for Wave 9: shutdown-time race in `bus.drain()`, or session-to-workspace resolver mismatch when the workload-level evaluator fires during shutdown. Doesn't block the v1 differentiator (the K-NN defaulted to `wmean=1.0` for both models on the affected workload).
- **Pattern store v2 (embedding fingerprint)** — v1 fingerprint is structural (intent tags from regex matches against `user_message_text` + tool-use signals + length bucket + optional workload_id partition). An embedding-based fingerprint would lift K-NN selectivity further; deferred until v1 K-NN data shows a concrete shortfall.
- **Context-assembler v3 skill activation** — spec is shipped (`context-assembler.md §5.2`) and partial scaffolding exists (`_assemble_stable_system_prompt` / `_initialize_skill_activations` / `SkillActivationRegistry`); the agent-side activate-on-tool-use path and the explicit-activation budget aren't fully wired yet. Deferred behind the §A3-rev2 inversion work.
- **Skill format loader extensions** — agentskills.io-compatible on-disk skill packaging beyond the current `SkillStore` substrate. Spec drafted at `docs/specs/skill-format.md`; loader extensions deferred.

## Read before reasoning about the system

The design is specified before code lands. In order of load-bearing-ness:

1. [docs/project-overview.md](docs/project-overview.md) — vision, principles, architecture, phasing.
2. [docs/specs/canonical-message-format.md](docs/specs/canonical-message-format.md) — the foundational data contract; every other spec depends on it.
3. [docs/specs/event-bus-and-trace-catalog.md](docs/specs/event-bus-and-trace-catalog.md) — observability spine, closed event catalog.
4. [docs/specs/routing-engine.md](docs/specs/routing-engine.md) — model selection pipeline, delegation.
5. [docs/specs/streaming-protocol.md](docs/specs/streaming-protocol.md) — WebSocket protocol (v1 subset implemented; see "What's NOT built" for gaps).
6. [docs/specs/provider-adapter-contract.md](docs/specs/provider-adapter-contract.md), [docs/specs/tool-dispatcher.md](docs/specs/tool-dispatcher.md), [docs/specs/server-api.md](docs/specs/server-api.md) — component contracts.
7. [docs/specs/analytics-api.md](docs/specs/analytics-api.md), [docs/specs/context-assembler.md](docs/specs/context-assembler.md), [docs/specs/benchmark.md](docs/specs/benchmark.md) — savings-attribution surface, prompt-cache placement, and the workload suite that turns the dashboard's numbers into a credible "we saved X%."
8. [docs/specs/deployment-shape.md](docs/specs/deployment-shape.md), [docs/specs/gateway.md](docs/specs/gateway.md), [docs/specs/pattern-store.md](docs/specs/pattern-store.md), [docs/specs/evaluator.md](docs/specs/evaluator.md), [docs/specs/skill-format.md](docs/specs/skill-format.md) — pattern-store and evaluator have shipped (v1 heuristic tier for the evaluator); deployment-shape is signed off; gateway and skill-format remain drafts pending implementation.
9. [docs/specs/CHANGES.md](docs/specs/CHANGES.md) — cross-spec change log; check `pending review` entries before editing dependent specs.

For competitive landscape and prior art: [docs/market-research/synthesis.md](docs/market-research/synthesis.md) and the four per-stream reports alongside it.

## Architecture and package layering

The repo is a uv workspace with four Python packages:

```
metis-core     (packages/metis-core/src/metis_core/)
metis-server   (apps/server/src/metis_server/)         depends on metis-core
metis-gateway  (apps/gateway/src/metis_gateway/)       depends on metis-core
metis-cli      (apps/cli/src/metis_cli/)               depends on metis-core, metis-server, metis-gateway
```

`metis-server` and `metis-gateway` are independent siblings — neither imports the other. Both consume `metis-core` building blocks (registry, routing engine, adapters, pricing, event bus, trace store) and expose their own Starlette ASGI surface. `metis-server` is the agent-mode surface (sessions, tools, memory, streaming); `metis-gateway` is the transparent-proxy surface (per-request stateless, no agent loop).

Within `metis-core`, the internal layering is unchanged from the pre-split shape (lower can be imported by higher, never the other way):

```
canonical      ←  events, trace, tools, adapters, routing, pricing, memory, patterns, eval, sessions
events         ←  trace, tools, adapters, routing, patterns, eval, sessions
trace          ←  eval
adapters       ←  pricing, sessions
tools          ←  memory, sessions
memory         ←  sessions
patterns       ←  routing, sessions
routing        ←  sessions
pricing        ←  sessions
eval           ←  (consumed by metis-cli only — bus subscriber + `metis evaluate` CLI)
sessions       ←  (nothing else in core)
```

`canonical` is the foundation; anything can import from it. Nothing in `canonical` imports from any other module. `metis-cli` is the top — it composes a `ChatRuntime` that the REPL, TUI, and `serve` subcommand all consume. The `metis-server` package sits just below `metis-cli`: it accepts a `ChatRuntime` (built by `metis_cli/runtime.py`) and exposes it over HTTP/WebSocket. When adding code, respect this direction — a circular import means a missing abstraction.

## Working norms

- **Specs-first.** Don't propose implementation changes that contradict the specs without flagging the spec impact. If a spec needs to change, draft the spec change first, then the code.
- **Cross-spec discipline.** When a spec change touches a contract, add an entry to `docs/specs/CHANGES.md` (date, change, type, references to verify, status). See the file's header for the format.
- **Solo, part-time owner.** One engineer, ~part-time pace. Scope decisions should favor what one person can land and maintain, not what a team could.
- **Bounded memory is a feature.** `MEMORY.md` and `USER.md` are intentionally small (~2 KB / ~1.5 KB). Don't propose unbounded growth — the eviction is the point.

## Implementation conventions

- **`msgspec.Struct(frozen=True)`, not Pydantic.** Canonical types use msgspec with tagged unions for discriminated content. See [canonical/content.py](packages/metis-core/src/metis_core/canonical/content.py).
- **`next_monotonic_ulid()`** from [canonical/ids.py](packages/metis-core/src/metis_core/canonical/ids.py). Raw `ULID()` from `python-ulid` is NOT strictly monotonic within a millisecond; the spec requires monotonic, so we wrap it with a process-wide lock that bumps the integer value on tie. Any new id generator must use it.
- **Tool factories, not instances.** `ToolDispatcher.register(ReadFileTool)` — pass the class. The dispatcher instantiates a fresh tool per call to prevent shared state across concurrent dispatches. The test suite enforces this.
- **Typed event payloads + dict envelope.** Each event type has a typed `msgspec.Struct` payload in [events/payloads.py](packages/metis-core/src/metis_core/events/payloads.py); `Event.payload` is `dict`. Use `make_event(type=..., payload=TypedStruct(...))` to bridge — it validates the type↔payload binding and converts.
- **`CanonicalResponse` returns content, not Message.** The adapter doesn't know the routing decision or the cost; the caller (`SessionManager`) assembles the full `Message` with metadata. This is a documented deviation from `provider-adapter-contract.md` §3.3 — see the file docstring at [adapters/protocol.py](packages/metis-core/src/metis_core/adapters/protocol.py).
- **Async fixtures.** Test fixtures that call `bus.start()` (which uses `asyncio.create_task`) must be `async def` so they run with an event loop. See [packages/metis-core/tests/tools/test_dispatcher.py](packages/metis-core/tests/tools/test_dispatcher.py).
- **Cost is `Decimal`, not float.** `Usage.cost_usd` and `PriceTable.compute_cost` use `Decimal` to avoid drift on cent-level math. `pricing_version` is recorded with every cost record so historical traces can be re-priced.
- **Workspace path security.** Any tool that touches files goes through `WorkspaceFileAPI`. `..` segments resolve during checking; symlinks pointing outside the root are rejected. Do not bypass it.

## Adding a new X

**New event type:**

1. Define a `msgspec.Struct(frozen=True)` payload in [packages/metis-core/src/metis_core/events/payloads.py](packages/metis-core/src/metis_core/events/payloads.py).
2. Add `"my.event": (MyPayload, Sensitivity.PSEUDONYMOUS)` to `PAYLOAD_REGISTRY`.
3. Update `docs/specs/event-bus-and-trace-catalog.md` §6 with the payload schema and sensitivity.
4. Log to `docs/specs/CHANGES.md` (additive vs breaking; which other specs to verify).
5. Tests cover the round-trip and the catalog registry membership.

**New built-in tool:**

1. Implement the `Tool` protocol from [tools/protocol.py](packages/metis-core/src/metis_core/tools/protocol.py) — `definition: ToolDefinition`, `async execute(input, context)`, `async cancel()`.
2. Schema must conform to the JSON Schema subset in [canonical/tools.py](packages/metis-core/src/metis_core/canonical/tools.py) — no `$ref`, `oneOf`, `anyOf`, `allOf`, `not`, etc.
3. Workspace-scoped file ops go through `context.workspace_files: WorkspaceFileAPI`.
4. Register via `register_builtins()` in [tools/builtins/__init__.py](packages/metis-core/src/metis_core/tools/builtins/__init__.py) if it's a default tool.

**New provider adapter:**

1. Implement the `ProviderAdapter` Protocol from [adapters/protocol.py](packages/metis-core/src/metis_core/adapters/protocol.py).
2. Wire translation: see [adapters/anthropic.py](packages/metis-core/src/metis_core/adapters/anthropic.py) for the pattern — system hoist, TOOL→user-with-tool_result merge, tool id map. `ToolIdMap` becomes load-bearing when providers don't accept canonical ids verbatim (OpenAI's `call_*` ids).
3. Error classification: implement a `classify_<provider>_response()` and return via `error_for_class()` with the correct `ErrorClass`.
4. Declare per-model `AdapterCapabilities` honestly (false ≥ unsupported); routing's validation gate trusts these.
5. Register via `ModelRegistry.register(model_id=..., adapter=..., aliases=...)`.

## Gotchas (things that will surprise you)

- **Streaming events are a separate layer**, not bus catalog events. `message.start`, `text.delta`, `tool.use_*`, `message.complete` exist in `streaming-protocol.md §5.3` but are NOT in `PAYLOAD_REGISTRY`. The streaming server has two input channels: the bus bridge (catalog events, persisted) via `EventBus.subscribe`, and a direct channel from the agent loop via [server/hub.py](apps/server/src/metis_server/hub.py) (streaming events, transient). The trace store only sees the former.
- **Turn-locked model.** The model chosen at turn start owns every LLM call in the turn, including tool cycles. Routing only re-runs at turn boundaries. Don't add code paths that re-route mid-turn — it breaks cost predictability and the `route.decided` invariant.
- **Stub policy** (`delegate_request`) still appears in `route.decided.chain` with `verdict: "not_applicable"`. That's correct, not a bug — it'll be filled in in Phase 4. The `rule` slot parses `.metis/routing.yaml`; the `pattern` slot consults the per-workspace `PatternStore` when a resolver is injected (otherwise reports `not_applicable` with reason `"pattern store not configured"`).
- **Per-(provider, model) availability.** A 401 or capability mismatch on one model no longer marks the whole provider unavailable; routing tracks availability per `(provider, model)` pair. The routing-engine.md §11 deferred-behavior note is resolved.
- **Memory is per-session, not per-process.** Each `SessionManager.create_session` builds a fresh `MemoryStore` via the injected `memory_factory`. The on-disk files (`<workspace>/.metis/{MEMORY.md,USER.md}`) are shared across sessions in the same workspace, but each session's store reads them fresh.
- **Memory writes don't auto-truncate.** Soft-cap overflow emits `memory.eviction` as a signal; hard-cap overflow rejects the write so the agent has to `memory_consolidate`. The eviction is the spec's intended user-visible action, not silent garbage collection.
- **`CLIConfirmationHandler` is the default confirmation handler** for `metis chat` and `metis tui`. NONE/READ side effects auto-approve; WRITE/EXECUTE/NETWORK prompt at the terminal unless the tool name is in `<workspace>/.metis/trust.yaml`'s `always_allow` (the prompt's "always" answer appends there; "never" appends to `always_deny`). The prompt has a 60-second timeout; on timeout the call is denied. Pass `--auto-allow` to `metis chat` / `metis tui` to revert to the old `AutoAllowHandler` behavior. `metis serve` is unaffected — the server installs `RemoteConfirmationHandler` on top of whatever the CLI set.
- **PARTIAL message bypass.** `validate_message()` skips invariant checks when `metadata.status == PARTIAL` (canonical-format §5.1.5). Mid-stream messages are intentionally allowed to violate role-content rules.
- **Server binds loopback-only in v1.** `metis serve --host 0.0.0.0` is silently rewritten to `127.0.0.1`. This is a v1 safety guarantee per server-api.md §3.1. Same applies to `metis gateway` per gateway.md §3.2 — production binds require a TLS terminator in front and are gated behind future hardening.
- **Attach tokens are single-use, 60-second TTL.** Each `GET /sessions/{id}` mints fresh; the WebSocket consumes it on upgrade. Reconnects need a new HTTP roundtrip.
- **Gateway clients passing `model` in the request body win the routing chain at slot 1.** OpenAI / Anthropic SDKs always include `model`, so `route.decided.chain` reports `policy=per_message_override` with `verdict=chose` on every gateway request. The `rule`, `pattern`, `workspace_default`, and `global_default` slots are not exercised unless the client omits `model`. This is correct (gateway.md §V interprets the inbound `model` as a per-message override) but worth knowing when reading gateway traces — you won't see other slot wins until a client deliberately omits `model`.
- **Gateway is per-request stateless.** No session manager, no tool dispatcher, no memory store, no skill loader. A turn is one HTTP request. This is the documented gateway-mode boundary (gateway.md §2), not a TODO. Clients owning conversation history is the point.

## Non-obvious external context

- **LiteLLM is *not* safe as the canonical internal representation.** It has live bugs around `tool_use`, `cache_control`, and thinking-block translation. Write per-vendor adapters against canonical types; use LiteLLM only as an optional egress proxy if at all.
- **agentskills.io is a verified open standard** (Anthropic-originated, multiple implementers). When designing the skill format, conform to it; don't invent fields.
- **Letta is the bounded-memory peer worth studying** (Series A; ships core/archival/recall memory plus agent self-edit tools). Reference its mechanics before redesigning memory primitives.

## Stack and conventions

- **Python 3.13**, `uv` workspace (per-member `uv_build` backend; see each `pyproject.toml`).
- **`msgspec`** for canonical types (not Pydantic).
- **`anthropic`** SDK + `httpx` for the provider adapter.
- **`jsonschema`** (Draft 7) for tool input validation.
- **`python-ulid`** wrapped with `next_monotonic_ulid()` for monotonic per-process ids.
- **Ruff** lint+format: line length 100, rules `E,F,I,B,UP,RUF`, `E501` ignored. Avoid the `×` multiplication sign in strings (RUF001 flags it as ambiguous).
- **pytest** with `pytest-asyncio` in auto mode; each workspace member has its own `tests/` directory. Root `conftest.py` puts `tests_shared/` on `sys.path` so cross-member test helpers (e.g. the scripted adapter) are importable from anywhere.
- **mypy** for typing.

## Running things

```bash
# Sync the workspace (resolves all four members).
uv sync

# Tests (1239 currently — collected from all four workspace members including apps/gateway/tests).
uv run pytest

# Lint + format
uv run ruff check packages apps scripts
uv run ruff format packages apps scripts

# CLI (requires at least one of ANTHROPIC_API_KEY / OPENAI_API_KEY / OPENROUTER_API_KEY)
uv run metis chat .
uv run metis chat /path/to/workspace --model haiku
uv run metis tui /path/to/workspace
uv run metis serve /path/to/workspace --port 8421

# Transparent gateway: issue a key (printed once), then run the server.
# Clients hit http://127.0.0.1:8422/v1/chat/completions (OpenAI) or /v1/messages (Anthropic).
uv run metis gateway issue-key --name "my-client" --workspace /path/to/workspace
uv run metis gateway --port 8422

# Re-run the heuristic evaluator over a window of trace events (evaluator.md §6.2)
uv run metis evaluate --db-path ~/.metis/metis.db --subject turn
uv run metis evaluate --db-path ~/.metis/metis.db --subject session --since 2026-05-01T00:00:00Z

# Real-API smoke tests
uv run python scripts/smoke.py --model haiku                # ~$0.015 / 2-turn run
uv run python scripts/smoke_cross_provider.py               # ~$0.007, mid-session provider switch
uv run python scripts/smoke_cache.py --model haiku          # < $0.05, asserts cache hits

# Savings benchmark suite (writes to benchmarks/.runs/)
uv run python scripts/benchmark.py                          # ~$0.30-1.00 full suite
uv run python scripts/benchmark.py --workload fix-a-bug-small   # ~$0.05-0.20 smoke
```

`.env` (gitignored) is the recommended place for API keys. Default SQLite path is `~/.metis/metis.db` (holds trace events + sessions/messages; override with `--db-path`).

## Repo layout

```
metis/
├── packages/
│   └── metis-core/                # library: foundation for everything else
│       ├── pyproject.toml
│       ├── src/metis_core/
│       │   ├── canonical/         # Messages, ContentBlocks, ToolDefinition, AdapterCapabilities
│       │   ├── events/            # bus + envelope + payload catalog (24 types)
│       │   ├── trace/             # SQLite WAL writer + replay query
│       │   ├── tools/             # dispatcher + workspace API + confirmation + 5 file/shell builtins
│       │   ├── memory/            # MemoryStore (byte-budgeted MEMORY.md/USER.md) + 3 memory tools
│       │   ├── adapters/          # Anthropic / OpenAI / OpenRouter + retry + tool id map
│       │   ├── routing/           # registry + per-(provider, model) availability + chain + override parser + YAML rule policy
│       │   ├── pricing/           # PriceTable (Decimal) + overlay versioning
│       │   ├── analytics/         # AnalyticsStore.savings() + windows (backs /analytics/savings)
│       │   ├── patterns/          # PatternStore + structural fingerprint + K-NN aggregation (slot 4)
│       │   ├── eval/              # HeuristicJudge + Evaluator bus subscriber + BudgetTracker + `metis evaluate` CLI
│       │   ├── sessions/          # Session, InMemorySessionStore, SqliteSessionStore, SessionManager
│       │   └── skills/            # SkillStore + skill_load tool (agentskills.io-compatible)
│       └── tests/                 # mirrors the package layout
├── apps/
│   ├── server/                    # HTTP/WS agent surface; depends on metis-core
│   │   ├── pyproject.toml
│   │   ├── src/metis_server/      # Starlette app + StreamingHub + token registry + TurnExecutor
│   │   └── tests/
│   ├── gateway/                   # Transparent OpenAI/Anthropic-shape HTTP proxy; depends on metis-core
│   │   ├── pyproject.toml
│   │   ├── src/metis_gateway/     # Starlette app + Keystore + GatewayHarness + per-shape translators + issue-key
│   │   └── tests/
│   └── cli/                       # CLI + Textual TUI + serve + gateway entry; depends on metis-core + metis-server + metis-gateway
│       ├── pyproject.toml
│       ├── src/metis_cli/
│       │   ├── chat.py main.py runtime.py serve.py models_display.py
│       │   └── tui/               # Textual TUI
│       └── tests/
├── docs/                          # specs/, market-research/, STRATEGY.md, KNOWN_ISSUES.md
├── benchmarks/                    # workload suite + RESULTS.md + .runs/ (gitignored)
├── scripts/                       # smoke.py, smoke_cross_provider.py, smoke_cache.py, benchmark.py (live-API harnesses)
├── tests_shared/                  # cross-member test helpers (scripted adapter, etc.)
├── conftest.py                    # workspace-root pytest config (adds tests_shared to sys.path)
├── pyproject.toml                 # workspace root: members, dev deps, ruff/pytest config
├── uv.lock
├── infra/                         # (empty placeholder for future deploy/CI/IaC artifacts)
└── README.md / AGENTS.md / CLAUDE.md
```

`CLAUDE.md` is a symlink to `AGENTS.md`. Specs live at `docs/specs/`; market research at `docs/market-research/`; the cost-optimization thesis and open strategic questions at `docs/STRATEGY.md`; carryover review findings at `docs/KNOWN_ISSUES.md`.

## When in doubt

Ask before refactoring or expanding scope. The owner prefers a one-line clarifying question over a large speculative diff.
