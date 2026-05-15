# Spec Changes

This file tracks breaking and significant changes to specs in `docs/specs/`. Its purpose is to prevent cross-spec drift: when one spec changes a contract, this log records which other specs reference that contract and need verification.

## How to use this file

When making a substantive change to a spec, add an entry below with:

1. **Date** — when the change was made.
2. **Spec** — which spec changed.
3. **Change** — one-line description.
4. **Type** — `breaking` (consumers must update) or `additive` (consumers can ignore).
5. **References to verify** — which other specs reference the changed contract and must be checked for consistency.
6. **Status** — `pending review` until cross-references are verified, then `verified`.

Trivial edits (typos, wording) don't need entries. Use judgment.

When working on a spec PR, scan this file for `pending review` entries against specs you depend on; verify them before landing.

## Specs in scope

- `canonical-message-format.md` — messages, content blocks, tool definitions, persistence.
- `event-bus-and-trace-catalog.md` — bus interface, event catalog, trace store.
- `routing-engine.md` — routing pipeline, rule format, `delegate()` contract.
- `streaming-protocol.md` — WebSocket protocol, snapshot/replay, cancellation.
- *(planned)* `provider-adapter-contract.md` — adapter interface, wire-format translation.
- *(planned)* `tool-dispatcher.md` — tool registry, side-effect handling, validation.
- *(planned)* `server-api.md` — REST endpoints, attach handshake, session lifecycle.
- `analytics-api.md` — read-only `/analytics/*` namespace backing the dashboard.
- `benchmark.md` — reproducible workload suite + measurement methodology backing the savings counterfactual.
- `deployment-shape.md` — recommendation for the replacement-agent / gateway / hybrid fork. Resolves [`STRATEGY.md §6.1`](../STRATEGY.md) when signed off.
- `gateway.md` — skeleton for the transparent HTTP gateway surface (paired with `deployment-shape.md`).
- `context-assembler.md` — v1 covers prompt-cache breakpoint placement; v2 adds the minimum-cacheable-prefix padding rule; v3 adds skill activation (explicit + pre-activation paths, per-session budget, no auto-activation in v3); history compression remains later.
- `pattern-store.md` — per-workspace bounded SQLite store of task fingerprints + outcomes that powers routing slot 4 (`PATTERN_RECOMMENDATION`). Phase 2.5.
- `skill-format.md` — retrospective v1 (2026-05-13) of the existing skills loader / store / tools; conforms to agentskills.io.
- `evaluator.md` — heuristic + hybrid LLM-as-judge feedback loop; emits `eval.*` events; resolves [`STRATEGY.md §6.7`](../STRATEGY.md) when signed off. Phase 3.
- `multi-user.md` — per-user / per-team identity layer on top of the shipped per-key cost attribution; analytics rollups, routing-rule predicates, gateway-level circuit breakers. Drafted 2026-05-14; Phase 3 implementation pending.
- `delegation.md` — Phase 4 design for worker sessions and the `delegate()` tool: slot 5 (`DELEGATE_REQUEST`) consumer contract, worker lifecycle, isolation, cost attribution, integration with pattern store + evaluator. Drafted 2026-05-14; Phase 4 implementation pending.
- `pricing.md` — commercial pricing model recommendation (open-core gateway + per-seat Pro + reserved enterprise %-of-savings add-on); surveys candidate models, names trade-offs, composes with `multi-user.md §5`. Drafted 2026-05-14; awaiting owner ratification — does not close [`STRATEGY.md §6.8`](../STRATEGY.md).
- `skill-curator.md` — periodic auxiliary-model maintenance of agent-authored skills (pin / archive / consolidate / edit); shared `BudgetTracker` with the evaluator, sidecar JSON state, archive-not-delete; pattern lifted from hermes-agent `agent/curator.py`. Drafted 2026-05-14; gated on agent-authored skills (Phase 2.5) landing first.
- `api-versioning.md` — pins the versioning posture for Metis's two HTTP surface categories: provider-shape endpoints (frozen by upstream SDK contracts; `/v1/chat/completions`, `/v1/messages`) and Metis-owned endpoints (versioned by us via the `Metis-API-Version` header). Drafted 2026-05-15; lightweight middleware shipped in both apps.

## Cross-reference map

A snapshot of which specs reference which (refresh when adding a spec):

| Source spec | Depends on |
|-------------|------------|
| `canonical-message-format.md` | (none — foundation) |
| `event-bus-and-trace-catalog.md` | canonical-message-format, routing-engine |
| `routing-engine.md` | canonical-message-format, event-bus-and-trace-catalog |
| `streaming-protocol.md` | canonical-message-format, event-bus-and-trace-catalog, routing-engine |
| `provider-adapter-contract.md` *(planned)* | canonical-message-format, event-bus-and-trace-catalog, streaming-protocol |
| `tool-dispatcher.md` *(planned)* | canonical-message-format, event-bus-and-trace-catalog |
| `server-api.md` *(planned)* | canonical-message-format, event-bus-and-trace-catalog, streaming-protocol |
| `analytics-api.md` | canonical-message-format, event-bus-and-trace-catalog, server-api |
| `benchmark.md` | analytics-api, event-bus-and-trace-catalog, canonical-message-format, provider-adapter-contract |
| `deployment-shape.md` | STRATEGY.md, market-research/synthesis.md (rationale only — no contract dependency) |
| `gateway.md` | canonical-message-format, provider-adapter-contract, routing-engine, event-bus-and-trace-catalog, server-api, analytics-api |
| `context-assembler.md` | canonical-message-format, provider-adapter-contract (planned), analytics-api |
| `pattern-store.md` | canonical-message-format, event-bus-and-trace-catalog, routing-engine, memory-store, analytics-api, evaluator |
| `skill-format.md` | canonical-message-format, event-bus-and-trace-catalog, tool-dispatcher, context-assembler |
| `evaluator.md` | event-bus-and-trace-catalog, canonical-message-format, analytics-api, benchmark, routing-engine, pattern-store *(planned)* |
| `multi-user.md` | canonical-message-format, event-bus-and-trace-catalog, gateway, routing-engine, analytics-api |
| `delegation.md` | canonical-message-format, event-bus-and-trace-catalog, routing-engine, streaming-protocol, server-api, tool-dispatcher, context-assembler, pattern-store, evaluator, analytics-api |
| `pricing.md` | STRATEGY.md, deployment-shape, multi-user, analytics-api, gateway, canonical-message-format (rationale + composability — no contract dependency) |
| `skill-curator.md` | skill-format, event-bus-and-trace-catalog, evaluator, canonical-message-format, analytics-api, memory-store, multi-user *(planned)*, pattern-store *(planned)* |
| `api-versioning.md` | gateway, analytics-api, server-api *(planned)* |

When changing a spec, the dependent specs (right column whose left column is the changed spec) must be checked.

---

## Change log

### 2026-05-15 — delegation.md v1 MVP shipped (`delegate()` tool + worker sessions; Wave 10)

- **Specs:** `delegation.md` (status flipped from "Draft v1, Phase 4 implementation pending" to "v1 MVP shipped"; new §3.6 enumerates explicit deferrals — async/concurrent workers, cancellation cascade, streaming, recursive delegation, `output_schema` validation, worker timeout, router-decided delegation, worker pattern-store integration); `event-bus-and-trace-catalog.md §6.8` updated to reflect shipped payload fields (`allowed_tool_count` + `dropped_tools` on `delegate.started`; `output_size_bytes` + `worker_total_cost_usd` + `model` on `delegate.completed`; `worker_total_cost_usd` on `delegate.failed`; phase note flipped from "Phase 4 deferred" to "v1 MVP shipped — Wave 10"); `analytics-api.md` cost-endpoint subsection picks up `include_workers` query parameter and the new `parent_session` / `is_worker` `group_by` values (see verification list below); `AGENTS.md` "What's NOT built" rewritten to remove the "Delegation — Phase 4 ..." line and add a "Delegation v1 MVP" entry under "What works" with the deferred features named.
- **Change:** Lands the `delegate()` built-in tool, the worker-session lifecycle, and the routing slot-5 re-entry path end-to-end. (a) New module [`packages/metis-core/src/metis_core/workers/`](../../packages/metis-core/src/metis_core/workers/) exporting `DelegateRequest` / `DelegateResult` / `DelegateUsageSummary` / `DelegateOutcome` / `WorkerSpawner` protocol / `ContextSpec` + tier / failure-mode literal types (msgspec frozen structs, `Decimal` cost). (b) [`SessionManager.spawn_worker`](../../packages/metis-core/src/metis_core/sessions/manager.py) resolves the tier → model via `ModelRegistry.model_for_tier`, creates a worker `Session` (`is_worker=True`, `parent_session_id` + `parent_tool_use_id` set; `active_model=None` so slot 5 fires fresh per §5.2), stashes the tier model in a per-id dict so `_build_turn_context` populates `TurnContext.worker_tier_model`, emits `delegate.started`, runs `submit_turn` synchronously, and returns `DelegateOutcome`. Failure modes mapped: tier miss → `no_model_available_for_tier` short-circuits before session creation; worker raise → `worker_error`; worker `stop_reason=max_tokens` → `max_tokens_exceeded`. (c) `ModelEntry` gains `can_delegate: bool = False` and `delegation_tier: str | None = None`; `ModelRegistry.register` accepts both; `can_delegate(model)` and `model_for_tier(tier)` helpers added. (d) `Session` gains `parent_session_id` / `parent_tool_use_id` / `is_worker` fields; `SqliteSessionStore` runs an idempotent `PRAGMA table_info` → `ALTER TABLE` migration on open (additive columns + a partial index on `parent_session_id`). (e) New [`DelegateTool`](../../packages/metis-core/src/metis_core/tools/builtins/delegate.py) implements the spec's input schema (tier required, task required, optional context spec / allowed_tools / max_tokens), refuses if `context.is_worker` is True or `context.worker_spawner` is None, awaits `spawner.spawn_worker`, emits `delegate.completed` / `delegate.failed` based on the outcome, and returns the worker's text as the tool result. (f) `ToolContext` gains `worker_spawner` and `is_worker` fields; `ToolDispatcher.dispatch` accepts and propagates both; `SessionManager.submit_turn` passes `worker_spawner=self, is_worker=session.is_worker` to every dispatch. (g) `SessionManager._effective_tool_definitions` filters `delegate` out of worker sessions and out of top-level sessions whose active model has `can_delegate=False` (delegation.md §5.6). Workers additionally lose `memory_add` / `memory_replace` / `memory_consolidate` so durable state stays read-only from inside a worker (§5.4). (h) `LLMCallStarted` / `LLMCallCompleted` / `TurnCompleted` gain `parent_session_id: str | None`; SessionManager stamps `session.parent_session_id` on every emit and uses `Actor.WORKER` instead of `Actor.AGENT` for worker turns. (i) `ConfirmationRequest` gains `is_worker: bool = False`; `CLIConfirmationHandler._apply_answer` skips trust.yaml persistence on "always" / "never" when the request originated inside a worker (§13's conservative default). (j) `AnalyticsStore.cost` gains `include_workers: bool = True` and two new `group_by` values: `parent_session` (rolls workers under their planner via `COALESCE(parent_session_id, session_id)`) and `is_worker` (partitions `planner` vs `worker` buckets). HTTP handler reads `?include_workers=false` and forwards. (k) Routing engine slot 4 (pattern) defers with `reason="delegate_request_in_flight"` when `ctx.worker_tier_model` is set, so a learned pattern can't silently override the planner's explicit `tier=` choice (delegation.md §11). Three new typed payloads in `events/payloads.py` → `DelegateStarted` / `DelegateCompleted` / `DelegateFailed` (all `Sensitivity.PSEUDONYMOUS`).
- **Type:** additive. (1) Pre-delegation registries continue to compile — `can_delegate` defaults to `False` so the tool is invisible everywhere by default; `delegation_tier` defaults to `None` so `model_for_tier` returns `None` and the failure mode `no_model_available_for_tier` is the natural opt-out. (2) Pre-delegation SQLite session DBs auto-migrate via the additive `ALTER TABLE` columns; readers tolerate the schema bump. (3) Pre-delegation `InMemorySessionStore.create_session` callers still work — the three new kwargs default to `None` / `False`. (4) Slot 5 still reports `not_applicable` for top-level sessions; the `test_phase1_stub_policies_always_not_applicable` test still passes (the default ctx leaves `worker_tier_model=None`). (5) Existing `route.decided.chain` shape is unchanged — the seven policy slots, same order, same verdicts. (6) All existing analytics endpoints continue to accept their existing query strings (the new `include_workers` defaults to `True` so existing callers see no behavior change; `group_by=parent_session` / `is_worker` are opt-in). (7) The `delegate` tool is registered by `register_builtins` but filtered out per-session by SessionManager; dispatchers that opt out via `register_builtins(dispatcher, with_delegate=False)` see the pre-Wave-10 surface.
- **References to verify:**
  - `routing-engine.md §4.1 / §6.9` — slot 5 (`DELEGATE_REQUEST`) now reports `chose: <tier model>` inside worker re-entry and `not_applicable: "not a delegation re-entry"` elsewhere. ✓
  - `routing-engine.md §5.6` / `pattern-store.md` — slot 4 defers with `reason="delegate_request_in_flight"` when worker_tier_model is set. ✓ (delegation.md §11)
  - `canonical-message-format.md §9.1` — `Session` record gains `parent_session_id` / `parent_tool_use_id` / `is_worker`; nullable, no migration on existing rows. ⏳ canonical-format spec to be updated when next opened.
  - `tool-dispatcher.md` — `ToolContext` gains `worker_spawner` + `is_worker`; `dispatch()` accepts them; confirmation-handler flow gets `is_worker`. ⏳ tool-dispatcher spec to be updated when next opened.
  - `event-bus-and-trace-catalog.md §6.3` — `LLMCallStarted` / `LLMCallCompleted` / `TurnCompleted` gain `parent_session_id`; `Actor.WORKER` now fires on worker emissions per §4.1. ✓
  - `analytics-api.md §4.1` — `group_by` enum gains `parent_session` and `is_worker`; `include_workers` query parameter added. ⏳ analytics-api spec to be updated when next opened.
  - `streaming-protocol.md §7` — `include_worker_sessions` filter remains accepted-but-unused; no worker streaming in v1 MVP. ✓
- **Status:** verified. **17 new tests** under `packages/metis-core/tests/workers/test_delegation.py` (14 — tool visibility filtering for can_delegate / can't-delegate planners and worker sessions, end-to-end planner→delegate→worker→planner loop with scripted adapter, worker LLM events stamp `parent_session_id`, worker `turn.completed` stamps `parent_session_id`, slot 5 fires inside worker chain, slot 4 defers inside worker chain, recursive delegation refused with `ToolExecutionError`, `no_model_available_for_tier` returns `delegate.failed`, worker `Session` record carries `is_worker` + parent fields, worker uses parent's workspace, dispatcher reused but per-session id maps isolated, top-level chain unchanged when delegation unused) and `packages/metis-core/tests/analytics/test_store.py` (3 — `group_by=parent_session` rolls workers under planner, `group_by=is_worker` partitions, `include_workers=False` excludes worker rows). Suite total: **1405 passed** (1388 baseline + 17 new). Ruff clean.

### 2026-05-15 — api-versioning.md v1 (new spec; lightweight middleware shipped on both apps)

- **Specs:** `api-versioning.md` (new — drafted v1). Adds the `Metis-API-Version` header contract for Metis-owned endpoints; pins provider-shape paths (`/v1/chat/completions`, `/v1/messages`) as frozen by upstream SDK contracts. Updates `CHANGES.md` specs-in-scope + cross-reference map. Updates `docs/gateway-client-quickstart.md` with a §8 "Pinning a Metis API version" subsection so buyers can opt in.
- **Change:** Two surface categories distinguished. (1) **Provider-shape (frozen)** — `/v1/chat/completions` and `/v1/messages` are versioned by OpenAI / Anthropic respectively; Metis doesn't get a vote and the middleware passes them through untouched (no `Metis-API-Version` request read, no response stamp). (2) **Metis-owned (versioned by us)** — every other route on the gateway and the agent server (`/healthz`, `/health`, `/server/version`, `/sessions/*`, `/analytics/*`, `/models`, future Metis-specific surfaces). Metis-owned endpoints accept an optional `Metis-API-Version` request header (default `CURRENT_VERSION = "1.0"`) and stamp the resolved version on every response. Deprecation policy: when a Metis-owned endpoint changes breakingly, the old version is supported for ≥6 months with `Deprecation: true` + `Sunset: <ISO date>` headers per RFC 8594 (with the simplified ISO-date profile documented in §3). Semver discipline: minor for additive (new fields, new endpoints, looser validation), major for breaking (removed fields, semantic changes, stricter validation). Currently `Metis-API-Version: 1.0`; no version-dispatch logic in v1 — the scaffolding lets later majors land without churning callers. Implementation: pure ASGI middleware (not `BaseHTTPMiddleware`, which would buffer SSE / WebSocket bodies) in [`apps/gateway/src/metis_gateway/middleware_versioning.py`](../../apps/gateway/src/metis_gateway/middleware_versioning.py) and [`apps/server/src/metis_server/middleware_versioning.py`](../../apps/server/src/metis_server/middleware_versioning.py); near-identical files since the two apps are independent siblings. Both files expose `CURRENT_VERSION`, `MIN_SUPPORTED_VERSION`, `DEPRECATED_VERSIONS` (empty in v1), `DEFAULT_BELOW_MIN_SUNSET = "2026-11-15"`, and `resolve_version(requested) -> (resolved, is_deprecated, sunset_iso)`. Wired via `Starlette(..., middleware=[Middleware(VersioningMiddleware)])` in both [`apps/gateway/.../app.py`](../../apps/gateway/src/metis_gateway/app.py) and [`apps/server/.../app.py`](../../apps/server/src/metis_server/app.py); the gateway's middleware defaults to skipping `PROVIDER_SHAPE_PREFIXES`, the server's defaults to no skip set since it has no provider-shape surface. A version-below-`MIN_SUPPORTED_VERSION` is served (not rejected) with a logged warning so operators can see who is still pinned before removal. Future revs may add a `400 unsupported_version` once telemetry shows buyers upgrade promptly enough.
- **Type:** additive. (1) No buyer-facing breaks — clients that don't send `Metis-API-Version` resolve to the current version transparently. (2) Provider-shape endpoints are unchanged in shape, headers, and routing (the middleware skips them entirely; auth-failure responses on those paths also don't gain the header — guarded by `test_provider_shape_auth_failure_still_skips_versioning`). (3) Existing analytics / health / sessions handlers are unchanged at the route level — the middleware operates above them. (4) Two new public modules (`metis_gateway.middleware_versioning`, `metis_server.middleware_versioning`); no changes to existing public APIs.
- **References to verify:**
  - `gateway.md §3.1` (provider-shape endpoints) — unchanged; api-versioning.md §1.1 cross-references this as the frozen surface. ✓
  - `gateway.md §3` (overall surface table) — `/healthz` is now documented as versioned per api-versioning.md §1.2. No edit required to gateway.md (the spec is the cross-cutting concern, not a gateway-specific addition). ✓
  - `analytics-api.md §3.2` (response envelope) — the envelope's `current_pricing_version` is orthogonal to the new `Metis-API-Version` header; the former is a per-row pricing concern, the latter a transport-level versioning concern. No edit required. ✓
  - `server-api.md` *(planned)* — when this spec lands it should reference api-versioning.md §1.2 as the versioning posture for the routes it documents. ⏳
  - `event-bus-and-trace-catalog.md` — no new event types (api-versioning.md §5 invariant: versioning is a transport concern, not an audited operation). ✓
  - `KNOWN_ISSUES.md` — no entry needed; this is preventive scaffolding, not a fix. ✓
- **Status:** verified. **20 new tests** under `apps/gateway/tests/test_middleware_versioning.py` (10 cases — `resolve_version` unit tests, header round-trip, default when absent, below-min stamps `Deprecation` + `Sunset`, explicitly-deprecated stamps mapped sunset, both provider-shape paths skip versioning entirely, provider-shape auth-failure still skips) and `apps/server/tests/test_versioning_middleware.py` (10 cases — `resolve_version` unit tests, header round-trip on `/health` and `/analytics/cost`, default when absent, below-min stamps `Deprecation` + `Sunset`, explicitly-deprecated stamps mapped sunset). Suite total: 1361 passed (was 1323 baseline before this change; the delta includes a few previously-shadowed tests that resurfaced after a stale `__pycache__` cleanup). Ruff clean.

### 2026-05-15 — gateway.md §11 key lifecycle (revoke / rotate / list + audit events; Wave 10)

- **Specs:** `gateway.md` (new §11 "Key lifecycle (Wave 10)", §12 follow-ons renumbered, §13 references renumbered); `event-bus-and-trace-catalog.md` (new §6.13 "Gateway admin domain" + three pseudonymous-floor event types); `docs/gateway-deployment.md` "Key management" subsection rewritten with `revoke-key` / `rotate-key` / `list-keys` recipes; "Keystore rotation" subsection in the Production checklist points to the new path.
- **Change:** Closes the v1 "no online revocation or rotation" gap noted in `gateway.md §11`. (a) `GatewayKey` ([`apps/gateway/src/metis_gateway/auth.py`](../../apps/gateway/src/metis_gateway/auth.py)) gains `status: Literal["active", "revoked"] = "active"`, `revoked_at: datetime | None = None`, and `grace_period_until: datetime | None = None`; loader is back-compat (missing fields default to `"active"` / `None`). `Keystore.from_dict` rejects a `status="revoked"` record without `revoked_at`. `GatewayKey` adds `is_active(now)` + `effective_revoked_at(now)` methods that read the grace-period boundary as read-only — auth never writes the keystore. (b) New module [`apps/gateway/src/metis_gateway/keystore_admin.py`](../../apps/gateway/src/metis_gateway/keystore_admin.py) exposes `revoke_key`, `rotate_key`, `list_keys`, `sweep_expired_grace_periods`, plus CLI shims (`revoke_key_command` / `rotate_key_command` / `list_keys_command`) and `parse_duration("30m"|"24h"|"7d"|"2w")`. All mutating ops do atomic write-temp-then-rename (`os.replace`) so a running gateway never observes a partial keystore. (c) `issue_key.py` now also writes atomically (via the shared `atomic_write_keystore`) and emits a `gateway.key_issued` audit event when a `db_path` is supplied. (d) Three new typed event payloads in [`packages/metis-core/src/metis_core/events/payloads.py`](../../packages/metis-core/src/metis_core/events/payloads.py) — `GatewayKeyIssued`, `GatewayKeyRevoked` (`reason: Literal["admin_revoke", "grace_period_expired", "rotated"]`), `GatewayKeyRotated`; all registered in `PAYLOAD_REGISTRY` with `Sensitivity.PSEUDONYMOUS`. Emission is best-effort — failures don't roll back the keystore mutation. (e) Auth middleware in [`apps/gateway/src/metis_gateway/app.py`](../../apps/gateway/src/metis_gateway/app.py) checks `key.is_active(now=...)` after the keystore lookup and returns the documented 401 body `{"error": {"code": "key_revoked", "key_id": "...", "revoked_at": "...", "type": "invalid_request_error"|"authentication_error", "message": "..."}}` before any harness / routing call. Shape-specific `type` matches the existing OpenAI vs Anthropic envelopes. (f) `metis-cli` ([`apps/cli/src/metis_cli/main.py`](../../apps/cli/src/metis_cli/main.py)) gains three subcommands: `metis gateway revoke-key <key_id>`, `metis gateway rotate-key <key_id> [--grace-period <duration>]`, `metis gateway list-keys [--format text|json]`; `issue-key` gains an optional `--db-path` that wires the audit-event emission target (defaults to `~/.metis/metis.db`). Rotation default grace period: 24h.
- **Type:** additive. (1) Pre-Wave-10 keystores load cleanly (missing `status` → `"active"`; missing `revoked_at` / `grace_period_until` → `None`). (2) `GatewayKey` constructors that omit the new fields compile and behave identically to v1. (3) `Keystore.authenticate` still returns revoked keys (auth needs the `key_id` to render the `key_revoked` body); the `is_active` filter is the middleware's job. (4) `issue_key()` gains optional kwargs (`now`, `db_path`) — existing callers compile unchanged. (5) Three new event types in the catalog — existing consumers that don't subscribe to them see no change. (6) The HTTP 401 body shape for `code="invalid_api_key"` is unchanged for unknown bearers; the new `code="key_revoked"` shape is documented in `gateway.md §11.2` and only fires for keys whose `is_active` returns False.
- **References to verify:**
  - `gateway.md §3.3 / §11 / §13` — keystore record table extended; new §11 captures the full surface (CLI ops, 401 body, audit-event contract, non-goals). ✓
  - `event-bus-and-trace-catalog.md §6.13` — three new pseudonymous event types; matches the `PAYLOAD_REGISTRY` entries. ✓
  - `analytics-api.md §4.1 / §4.8` — gateway-admin events use the same `gateway_key_id` projection the cost endpoint already reads; no schema change. ✓
  - `multi-user.md §3 / §4` — rotation preserves the `user_id` / `team_id` tags so per-identity rollups (`/analytics/by_user` / `/analytics/by_team`) reflect the migration without re-tagging the successor. ✓
  - `docs/gateway-deployment.md` — Key management subsection rewritten; Keystore rotation subsection in Production checklist points to the new path. ✓
  - `KNOWN_ISSUES.md` — "no online revocation API in v1" gap closed by this change. ⏳ Update entry when next opened.
- **Status:** verified. New tests (27 cases): `apps/gateway/tests/test_keystore_admin.py` (25 — revoke marks status / stamps revoked_at + audit emission + idempotency, unknown key, rotate inherits metadata + emits link event with old→new + identity tags, default vs custom grace, refuses revoked predecessor / zero-or-negative grace, both keys active during grace window, predecessor auto-revokes at boundary, `sweep_expired_grace_periods` persists transition + emits paired key_revoked event with `reason="grace_period_expired"`, sweep idempotent, list-keys shape stable across rotation, list-keys empty keystore, list-keys text + JSON output formats, `parse_duration` variants, atomic write leaves no partial temp file, audit-event payload metadata sanity, pre-Wave-10 back-compat); `apps/gateway/tests/test_app_http.py` (+2 — 401 `key_revoked` body on both inbound endpoints). `apps/gateway/tests/conftest.py` adds `revoked_runtime` / `revoked_client` fixtures. Full gateway suite: 144 → 171 cases. Full project suite passes at 1383 cases (excluding the pre-existing `test_subscriber.py::test_drain_processes_eval_completed_cascade_before_returning` flaky/hung test, unrelated to this change). Ruff clean across `packages/`, `apps/`, `scripts/`.

### 2026-05-15 — event-bus-and-trace-catalog.md §7.5 (trace-DB backup & restore contract)

- **Specs:** `event-bus-and-trace-catalog.md` §7.5 (new — backup & restore contract under "Persistence"). `docs/gateway-deployment.md` gains a "Backup & restore" subsection under "Production checklist" with the buyer-facing recipe (cron, rotation, restore drill, helm/PVC volume-snapshot composition).
- **Change:** Ships buyer-runnable backup + restore for the trace DB so helm-chart buyers can snapshot before a risky upgrade and restore on failure without the WAL pitfalls of a naive `cp`. New module [`packages/metis-core/src/metis_core/trace/backup.py`](../../packages/metis-core/src/metis_core/trace/backup.py) exposes `backup(source_db, dest) -> BackupResult` (uses SQLite's `VACUUM INTO` — atomic, WAL-safe, single-file output; source DB stays open and writable) and `restore(source, dest_db, *, allow_overwrite=False) -> RestoreResult` (schema-version checked via `PRAGMA user_version`, refuses to clobber by default, refuses if `-wal` / `-shm` companions sit alongside the source backup). [`packages/metis-core/src/metis_core/trace/store.py`](../../packages/metis-core/src/metis_core/trace/store.py) gains a `TRACE_SCHEMA_VERSION = 1` constant and stamps `PRAGMA user_version` on every opened trace DB so the backup module has a stable version handle. Two new `metis` CLI subcommands ([`apps/cli/src/metis_cli/main.py`](../../apps/cli/src/metis_cli/main.py), [`apps/cli/src/metis_cli/backup.py`](../../apps/cli/src/metis_cli/backup.py)): `metis backup <dest> [--db-path <source>]` and `metis restore <source> [--db-path <dest>] [--force]`. Both emit a deterministic human-readable metadata block on success (source / dest / byte count / schema version / event count / oldest+newest event timestamps; no random ids) and a one-line diagnostic to stderr with non-zero exit on failure.
- **Type:** additive. (1) Existing trace DBs without `user_version` stamped get bumped to `1` the next time they're opened by `TraceStore._configure` — read paths are unaffected. (2) No new event types, no payload changes, no catalog edits beyond §7.5. (3) The CLI gains two top-level subcommands; existing `chat` / `tui` / `serve` / `evaluate` / `gateway` flows are untouched. (4) Helm chart / docker compose surfaces are unchanged — backup/restore are operator commands run against the same SQLite file the gateway and serve already write.
- **References to verify:**
  - `event-bus-and-trace-catalog.md §7.1` — schema declaration unchanged; new §7.5 stamps `user_version` from §7.1's schema-version constant. ✓
  - `event-bus-and-trace-catalog.md §7.2` — storage notes (WAL + synchronous=NORMAL) preserved; backup module opens the source read-only via URI and uses `VACUUM INTO` which composes cleanly with WAL mode. ✓
  - `event-bus-and-trace-catalog.md §7.3` — retention is orthogonal to backup; pruning before a backup is fine, the backup just captures the post-prune state. ✓
  - `gateway.md §3.2` (loopback-only bind, TLS terminator in front) — backup/restore is a sidecar/operator command, not a network surface. No bind-policy change. ✓
  - `deployment-shape.md` — backup recipe is the missing piece for the "buyer-trial floor" (close-the-loop on data safety before they commit). ✓
  - `analytics-api.md` — backups capture the full `events` table; `/analytics/*` reads against a restored DB are identical to the pre-backup numbers (no analytics-side schema change). ✓
- **Status:** verified. 18 new tests (13 library + 5 CLI): round-trip (write → backup → restore → events match), empty-DB backup, schema-version mismatch refusal, default-overwrite-refusal + `--force` opt-in, WAL-companion refusal, hot backup with source still open, missing-source error paths, 100k-event backup completes in well under 5s on a developer laptop. Library tests in [`packages/metis-core/tests/trace/test_backup.py`](../../packages/metis-core/tests/trace/test_backup.py); CLI tests in [`apps/cli/tests/test_backup_cli.py`](../../apps/cli/tests/test_backup_cli.py).

### 2026-05-14 — pattern-store.md §16 (v2 hybrid fingerprint shipped; Wave 10)

- **Specs:** `pattern-store.md` §16 status flipped from "drafted" to "implemented" (header revised); new §16.13 "Implementation notes (Wave 10)" + §16.14 "Migration: upgrading a v1 workspace to v2" subsections added inside §16.
- **Change:** Ships the v2 hybrid fingerprint contract described in §16. New module `packages/metis-core/src/metis_core/patterns/embeddings.py` defines a `@runtime_checkable` `EmbeddingProvider` Protocol and three concrete providers (`OpenAIEmbeddingProvider` → `text-embedding-3-small`, 1536-dim; `CohereEmbeddingProvider` → `embed-multilingual-v3.0`, 1024-dim, via raw httpx; `LocalEmbeddingProvider` → sentence-transformers `all-MiniLM-L6-v2`, 384-dim, deferred Torch import) plus a `DeterministicEmbeddingProvider` for tests/fixtures and a `resolve_embedding_provider(provider_id)` registry. `PatternStore` (`patterns/store.py`) gains a new `embedding_cache(text_sha256, provider_id, embedding_blob, embedding_dim, created_at_us, last_used_at_us, use_count)` table — keyed `(provider_id, SHA-256(user_message_text))` per §16.4.1, vector blobs packed `array.array('f', ...).tobytes()` (no NumPy dep), bounded by `embedding_cache_max_rows=10_000` + `embedding_cache_max_age_days=180` with age-first → LRU → use-count tie-break eviction (§16.4.3). `find_k_nearest` consumes blended similarity when the query carries an embedding; mixed-version K-NN falls back to v1 weighted-Jaccard when either side lacks an embedding or the dims disagree (§16.5.3). Schema_version bumps `"1" → "2"` via `WHERE store_meta.value < excluded.value` so a v1 process opening a v2 db never downgrades; the catalog spec already had `pattern.recorded.fingerprint_kind` so no new event types. `patterns/similarity.py` adds `cosine_similarity(a, b)` (raises on dim mismatch / empty) and `blended_similarity(a, b, *, a_embedding, b_embedding, alpha)` (alpha out of `[0, 1]` raises); v1 `weighted_jaccard` is unchanged and reused as the structural half. `patterns/fingerprint.py` `FingerprintInputs` gains `embedding: tuple[float, ...] | None = None` + `embedding_provider: str | None = None`; `compute_fingerprint` produces a HYBRID `Fingerprint` when the embedding is set; new `attach_embedding_for_recording(inputs, *, store, embedder)` async helper does the cache-first / embed-on-miss / cache-write loop for the recording path; `text_sha256(text)` helper exposes the cache pre-image. `routing/policy.py` `PatternConfig` gains `fingerprint_version: Literal["v1", "v2"] = "v1"` + `embedding_provider: str | None = None` + `embedding_alpha: float = 0.6` with `__post_init__` validation (v2 requires `embedding_provider`; `embedding_alpha` must be in `[0, 1]`). `routing/engine.py` slot 4 in v2 mode does a sync cache-only lookup via `_attach_cached_embedding` before computing the query fingerprint — cache hit → blended K-NN; cache miss → v1 jaccard. The routing critical path never blocks on a network call (§16.6 trade-off).
- **Type:** additive. (1) v1 default behavior is unchanged — `PatternConfig()` returns `fingerprint_version="v1"`; structural-only path runs identically against existing v1 patterns dbs. (2) v1 patterns dbs reopen under v2 mode cleanly; `schema_version` bumps in-place; no rows touched in `fingerprints` / `outcomes` / `outcome_score_history` / `store_meta`. (3) `PatternStore.__init__` gains optional kwargs (`fingerprint_version`, `embedding_alpha`, `embedding_cache_max_rows`, `embedding_cache_max_age_days`); all default to v1-compatible values. (4) `FingerprintInputs` gains two optional fields with `None` defaults; existing constructors compile unchanged. (5) `compute_fingerprint(inputs)` signature unchanged; v1 callers continue to get STRUCTURAL fingerprints. (6) No new event types — `pattern.recorded.fingerprint_kind` already discriminates `"structural"` vs `"hybrid"`. (7) The shipped impl differs from the original spec in four documented ways recorded in §16.13: `embedding_alpha` rename (was `embedding_blend_alpha`), no `embedding_strategy` knob (effectively always-async at query layer because routing-engine lookup is cache-only-sync; recording is async via `attach_embedding_for_recording`), sync `recommend()` preserved, no NumPy hard dep.
- **References to verify:**
  - `pattern-store.md §5` (v1 fingerprint) — unchanged. ✓
  - `pattern-store.md §5.3` (v1 weighted Jaccard) — unchanged; reused as the structural half of the v2 blend. ✓
  - `pattern-store.md §16` — status flipped; §16.13 / §16.14 added; deviations recorded in §16.13. ✓
  - `routing-engine.md §5.5` — slot-4 K-NN math; v2 introduces no new `routing.yaml` keys outside the existing `pattern.*` namespace. The engine's v2 cache-lookup path is internal to `_evaluate_pattern`. ⏳ Optional follow-up to mention the v2 sync-cache path in routing-engine.md §5.5.
  - `event-bus-and-trace-catalog.md §6.5b` — `pattern.recorded.fingerprint_kind` already discriminates `"structural"` / `"hybrid"`. No catalog change. ✓
  - `STRATEGY.md §4` / §6.2 — third differentiator + self-hosting buyer profile; v2 ships `local:sentence-transformers:all-MiniLM-L6-v2` as the buyer-friendly path. ✓
  - `benchmarks/RESULTS.md §A3-rev3` — v1 differentiator inverted under `min_confidence=0.05`; v2 is the implementation-ready alternative for workspaces whose structural Jaccard washes out (agent-loop traffic with empty `intent_tags`). Cluster-tightening A/B (§16.10 test 5 against the 60-turn fixture) is deferred to a follow-up wave. ⏳
- **Status:** verified for the additive scope. **52 new tests** under `packages/metis-core/tests/patterns/` (`test_embeddings.py`, `test_v2_similarity.py`, `test_v2_store.py`, `test_v2_routing.py`) cover the Protocol contract + `runtime_checkable` rejection, cosine/blend math (α=0 reduces to v1, α=1 to cosine, headline α=0.6 cases), cache hit/miss/store/clear + TTL eviction + LRU eviction with use-count tie-break, provider-id-segregated cache keys, mixed-version K-NN (v1 row + v2 query), schema bump verification, recording-path cache-first embed (zero API calls on second hit), v1 db reopening cleanly under v2 mode, routing slot-4 v2 code path with cache-hit ranking sonnet above haiku on aligned embedding, routing fallback to v1 jaccard on cache miss. Suite total: **1322** (was 1270 baseline). Ruff clean.

### 2026-05-14 — event-bus-and-trace-catalog.md §3 `EventBus.drain()` loops to quiescent (closes the §A3-rev3 outcome-update bug)

- **Specs:** `event-bus-and-trace-catalog.md` §3 (drain semantics). No code-visible API change, but the post-condition is strengthened.
- **Change:** `EventBus.drain()` ([`packages/metis-core/src/metis_core/events/bus.py:182`](../../packages/metis-core/src/metis_core/events/bus.py)) now loops until both the queue is empty *and* no handler tasks are in flight, instead of awaiting a single `queue.join` + one `gather`. Python 3.13's `asyncio.Queue.join` returns on the first time `unfinished_tasks` drops to zero; handler tasks scheduled before that point may not have run yet when `join` returns, and the events they then emit are still in flight when callers expect drain to be complete. The cascade that exposed this: `turn.completed` → pattern subscriber records outcome → evaluator emits `eval.completed` → pattern subscriber writes the score back via `update_score`. With the single-pass drain, `shutdown_runtime` in the agent loop (which detaches subscribers immediately after `drain()`) raced the cascading `eval.completed` and dropped the score, leaving `success_score_count = 0` on outcome rows for 1-turn workloads with multiple tool calls (the §A3-rev3 caveat: `architectural-explanation-without-hallucination`).
- **Type:** additive (correctness fix). Existing callers see the same `await bus.drain()` signature; the post-condition strengthens from "first wave of in-flight handlers done" to "bus is fully quiescent." No bus event types added, no payload changes, no subscription contract changes.
- **References to verify:**
  - `event-bus-and-trace-catalog.md §3` — drain post-condition: "When `drain()` returns, the queue is empty and no handler tasks are in flight." Stronger than the prior implicit contract. ✓ (Regression test in `packages/metis-core/tests/patterns/test_subscriber.py::test_drain_processes_eval_completed_cascade_before_returning` pins this.)
  - `pattern-store.md §15.3` — outcomes are recorded asynchronously off the fast event path; this fix is precisely what guarantees the `eval.completed → update_score` cascade lands before subscribers detach. ✓
  - `evaluator.md §6.1` — subscriber is non-fast-path; cascading emits flow through the bus dispatch loop and were the source of the dropped scores. ✓ (No change to the evaluator's emission shape.)
  - `benchmarks/RESULTS.md §A3-rev3 caveats` — the `architectural-explanation-without-hallucination` row that recorded `success_score_count=0` across all three passes. Re-running that workload with the fix produces `success_score_count=1, success_score_mean=1.0` on the outcome row. Caveats text remains accurate as a record of the prior state; the bug is now closed.
- **Status:** shipped — implementation + regression test live, test count 1270 → 1271, ruff clean on changed files.

### 2026-05-14 — skill-curator.md v1 (new spec; gated on agent-authored skills Phase 2.5)

- **Specs:** `skill-curator.md` (new — drafted v1). No code changes; pure spec. Additive references to `event-bus-and-trace-catalog.md §6.6` (one new value `"curator_generated"` on `skill.created.source`) and `analytics-api.md` (one new optional `include_curator` query param + a new `/analytics/curator` endpoint). Updates `CHANGES.md` specs-in-scope + cross-reference map. Updates `AGENTS.md` "What's NOT built" to point to this spec.
- **Change:** Lifts the curator pattern from hermes-agent (`agent/curator.py`) and adapts it to Metis's primitives. Periodic auxiliary-model maintenance of **agent-authored** skills only. Six actions (`pin` / `unpin` / `archive` / `restore` / `consolidate` / `edit`); never auto-deletes (archive is `mv` to a sibling `skills-archive/` root, reversible). Pinned skills bypass every auto-transition. Inactivity-triggered at `session.ended` (no daemon); explicit `metis curate <workspace>` CLI for power users. Shared `BudgetTracker` with the evaluator with independent caps (`curator.per_run_max_usd: Decimal("0.50")`, `curator.per_day_max_usd: Decimal("1.00")`). One new bus event `skill.curated` (USER_CONTROLLED floor with `signals.rationale_redacted` downgrade), plus two run-boundary events `curator.run_started` / `curator.run_finished` (PSEUDONYMOUS). Sidecar JSON state at `~/.metis/curator/state.json` and `<workspace>/.metis/curator/state.json` carries pin / archive / origin / lineage — **no SKILL.md frontmatter changes** (preserves agentskills.io conformance per the AGENTS.md memory pin "conform; don't invent fields"). Curator-touchable origin matrix (§3) restricts mutation authority to `auto_generated` and `curator_generated` skills; `manual` / `imported` / no-`skill.created`-event are read-only. Cluster consolidation uses substring-overlap heuristic in v1 (`name_overlap >= 0.6` OR `description_overlap >= 0.7`) plus an auxiliary-model confirmation call per cluster; embedding-based clustering deferred to v2 alongside `pattern-store.md §16`. Implementation gated on Phase 2.5 `skill.created(source="auto_generated")` landing first (the curator only acts on skills with that event in the trace).
- **Type:** additive. (1) `skill-format.md` is unchanged — the curator runs on top of the shipped `SkillStore` / `load_skills` substrate without modifying either. (2) `event-bus-and-trace-catalog.md §6.6` gains one enum value (`"curator_generated"` on `skill.created.source`); existing consumers that pattern-match the enum need to handle the new value or be tolerant. The catalog spec edit lands when the curator implementation lands (deferred — this CHANGES.md entry covers the spec only). (3) Two new event types (`skill.curated`, `curator.run_started`, `curator.run_finished`) are introduced; their payload Structs land in `events/payloads.py` + `PAYLOAD_REGISTRY` at implementation time. (4) `evaluator.md` is unchanged — the curator reuses the `BudgetTracker` primitive without modifying the evaluator's caps or surface. (5) `analytics-api.md` gains one optional query param (`include_curator`) and one new endpoint (`/analytics/curator`); both additive, the existing surface is unchanged. (6) `multi-user.md` is unchanged — curator is workspace-scoped, not identity-scoped; multi-user rollups bucket curator spend under `null` for user/team groupings (matches the pre-multi-user direct-API convention).
- **References to verify:**
  - `skill-format.md §2.1 / §2.2 / §11` — the curator does not modify the loader's invariants. Curator state lives outside the skill directory (sidecar JSON) so the loader's hidden-directory-not-excluded gap (§11.5) is irrelevant here. ✓
  - `event-bus-and-trace-catalog.md §6.6` — the `skill.created.source` enum gains `"curator_generated"`. The catalog edit + new event types land at implementation time, not now. ⏳ Confirm at implementation: enum bump is additive against current consumers (`events/payloads.py::SkillCreated` is the only registered consumer).
  - `evaluator.md §7` — `BudgetTracker` is the shared primitive. The evaluator's caps are independent; the curator's caps add to the workspace's daily ceiling but do not throttle the evaluator. ✓
  - `analytics-api.md §3 / §4` — `include_curator=true` parameter and `/analytics/curator` endpoint follow §3 window-parameter conventions and §4 projection patterns. Schema change is additive. ⏳ Wire at implementation.
  - `canonical-message-format.md §6.4` — `curator_cost_usd` is a `Decimal` serialized as string, matches the `Usage.cost_usd` and `eval.completed.judge_cost_usd` conventions. ✓
  - `memory-store.md` — sister "soft cap → eviction event, hard cap → reject the write" pattern; curator follows analogously (soft "stale" annotation, hard "archive" action). No contract change required. ✓
  - `pattern-store.md` — orthogonal feedback loop in v1; no read or write between them. v2 cross-link deferred per §13.8. ✓
  - `multi-user.md §3 / §4` — curator spend buckets under `null` for `user_id` / `team_id` projections (matches pre-multi-user direct-API treatment). No identity-stamping on `skill.curated`. ✓
  - `AGENTS.md` — "What's NOT built" entry on skill-format loader extensions gets a pointer to this spec. ✓
- **Status:** drafted; implementation deferred to Phase 2.5b. The implementation order is (1) Phase 2.5 agent-authored skills (`skill_save` tool + `skill.created(source="auto_generated")`), then (2) curator (this spec). The curator without (1) has nothing to act on. Update this entry to "shipped" when `metis_core.skills.curator` lands and the §12.1 required-tests pass.

### 2026-05-14 — multi-user.md §5 / gateway.md §6.4 ship: per-key quota caps with hard breakers, soft alerts, and `team_budget_remaining_lt` routing predicate (Wave 9a-2)

- **Specs:** `multi-user.md` §1 / §5.1 / §6.1 / §6.3 (status header flipped to "shipped"); `gateway.md` §3.3 (`monthly_cap_usd` keystore field; `daily_cap_usd` widened to `Decimal`), §6.4 (new — `quota.alert` + `gateway.quota_exceeded` event types + 429 body shape), §10 (per-key rate-limit non-goal updated); `event-bus-and-trace-catalog.md` (additive, two new event types).
- **Change:** Lands the second half of `multi-user.md §5` against the shipped gateway. (a) `GatewayKey` ([`apps/gateway/src/metis_gateway/auth.py`](../../apps/gateway/src/metis_gateway/auth.py)) gains `monthly_cap_usd: Decimal | None`; `daily_cap_usd` widens from `float | None` to `Decimal | None`. The keystore loader accepts the new field (back-compat: missing or `None` = no cap); legacy keystores that wrote `daily_cap_usd` as a JSON number coerce via `Decimal(str(value))` so reload is exact. (b) `metis gateway issue-key` gains `--monthly-cap-usd` and tightens `--daily-cap-usd` validation (must parse as a positive number; zero/negative rejected with a deterministic message shared between CLI and keystore loader). Both caps persist to JSON as Decimal-stable strings. (c) New module `apps/gateway/src/metis_gateway/quotas.py` provides `QuotaTracker` (read-only spend aggregator over the trace store; one query per identity dimension) + `QuotaStatus` (used / cap / percentage snapshot) + `RequestQuotaCache` (per-request memoization) + `enforce_quotas()` (the policy loop that emits `quota.alert` + `gateway.quota_exceeded` events). (d) Two new typed event payloads in [`packages/metis-core/src/metis_core/events/payloads.py`](../../packages/metis-core/src/metis_core/events/payloads.py) — `QuotaAlert` (severity `warning`@80% / `critical`@95%) and `GatewayQuotaExceeded` (scope, current_usd, limit_usd, inbound_shape, identity stamps); both registered in `PAYLOAD_REGISTRY` with `Sensitivity.PSEUDONYMOUS`. (e) `apps/gateway/src/metis_gateway/app.py` builds a `RequestQuotaCache` per request after auth, runs `enforce_quotas` before parsing the body, and returns the documented 429 envelope on hard-cap rejection (`{"error": {"code": "quota_exceeded", "identity": ..., "scope": ..., "limit_usd": ..., "current_usd": ..., "type": "rate_limit_error", "message": ...}}`). The check fires before routing/adapter invocation per `multi-user.md §6.3` — no provider-side spend on a capped identity. (f) New routing predicate `team_budget_remaining_lt: <usd>` in [`packages/metis-core/src/metis_core/routing/policy.py`](../../packages/metis-core/src/metis_core/routing/policy.py) + [`predicates.py`](../../packages/metis-core/src/metis_core/routing/predicates.py) + [`policy_loader.py`](../../packages/metis-core/src/metis_core/routing/policy_loader.py); evaluates against `TurnContext.team_budget_remaining_usd` (new optional `Decimal` field) which the gateway harness populates from the per-request quota cache. Agent-loop traffic leaves the field `None` and the predicate returns `False`. (g) `GatewayRuntime` gains an optional `quota_tracker: QuotaTracker | None` field initialized in `setup_gateway_runtime` against the existing `db_file`; `shutdown_gateway_runtime` closes it.
- **Type:** additive. (1) Existing keystores load unchanged (missing cap fields → `None`, no enforcement). (2) `GatewayKey` constructors that don't pass cap fields compile and behave identically. (3) `daily_cap_usd` field type widened from `float` to `Decimal`; the only in-tree caller that constructed `GatewayKey` with the float field type was the keystore loader itself, updated. (4) `GatewayHarness.call()` / `stream()` signatures gain optional `team_budget_remaining_usd: Decimal | None = None` kwarg, defaulting to `None` (current behavior). (5) `TurnContext` gains optional `team_budget_remaining_usd: Decimal | None = None`; existing constructors compile unchanged. (6) Two new event types in the catalog — existing consumers that don't subscribe to them see no change; subscribers that do see them stamped on hard-cap rejections and 80%/95% threshold crossings.
- **References to verify:**
  - `multi-user.md §5 / §6.1 / §6.3` — shipped surface matches spec contract: `Decimal` caps, hard breaker before routing, soft alerts at 80%/95%, `team_budget_remaining_lt` predicate. Status header updated. ✓
  - `gateway.md §3.3 / §6.4 / §10` — keystore-record table, new event types + 429 body shape, per-key rate-limit non-goal updated in this change. ✓
  - `event-bus-and-trace-catalog.md §6` — two new pseudonymous-floor event types added to the catalog (additive; the catalog spec doesn't enumerate every payload struct exhaustively, so no edit required there). ✓
  - `analytics-api.md §4.1` — quota events use the same `gateway_key_id` / `user_id` / `team_id` projection the cost endpoint already reads; no schema change. ✓
  - `routing-engine.md §5.3.2` — predicate set gains `team_budget_remaining_lt`; the spec lists the predicate set in §5.3 as documentation, no breaking change. ⏳
  - `KNOWN_ISSUES.md` — `gateway.md §10.5` "stores daily_cap_usd but doesn't enforce it" gap closed by this change. ✓
- **Status:** verified. New tests (27 cases): `apps/gateway/tests/test_issue_key.py` (5 — Decimal round-trip, monthly cap CLI, validation rejection, legacy float back-compat); `apps/gateway/tests/test_quotas.py` (12 — QuotaStatus shape, dimension filters, soft alert at warn/critical thresholds, hard breaker emits `gateway.quota_exceeded`, alert idempotency, no-cap no-op); `apps/gateway/tests/test_app_http.py` (2 — HTTP 429 with documented body, untagged keys still pass through); `packages/metis-core/tests/routing/test_predicates.py` (4 — predicate fires below threshold, doesn't fire at/above, returns False without team binding, handles zero headroom); `packages/metis-core/tests/routing/test_policy_loader.py` (1 — yaml parser accepts `team_budget_remaining_lt`); `packages/metis-core/tests/routing/test_engine_rules.py` (2 — rule wins slot 3 when headroom below threshold, falls through when no team binding). Suite total: 1270 (was 1243 baseline).

### 2026-05-14 — pattern-store.md §16 (v2 hybrid fingerprint contract; Phase 4 pending §A3-rev3)

- **Specs:** `pattern-store.md` (new §16 "v2 hybrid fingerprint: implementation contract"; header status updated; §5.2, §5.3, §13.1, §13.2 cross-references redirected to §16; References renumbered §16 → §17). No code changes; pure spec.
- **Change:** Converts the §5.2 / §13.1 / §13.2 v2 sketch into an implementation-ready contract so Wave 10 can begin work if §A3-rev3 (Wave 9 candidate; `PatternConfig.min_confidence: 0.3 → 0.05`) fails to invert routing slot 4 under v1's structural-only fingerprint. Specifies: (1) `EmbeddingProvider` Protocol (`provider_id`, `dim`, `max_input_tokens`, async `embed`, `aclose`) with `@runtime_checkable` semantics. (2) Three concrete provider impls — `openai:text-embedding-3-small` ($0.02/1M tokens, 1536-dim, 50–150ms), `cohere:embed-multilingual-v3.0` ($0.10/1M, 1024-dim, 80–200ms), and `local:sentence-transformers:all-MiniLM-L6-v2` ($0, 384-dim, 30–80ms CPU); each is selectable per workspace via `PatternConfig.embedding_provider` (`provider_id` string), with no default — unset means structural-only. (3) Embedding cache: new SQLite table `embedding_cache(text_sha256, provider_id, embedding_blob, embedding_dim, created_at_us, last_used_at_us, use_count)` in the same `<workspace>/.metis/patterns.db`, keyed by `(provider_id, SHA-256(user_message_text))` — same SHA-256 pre-image as the v1 structural dedup. Bounded by `cache_max_rows=10_000` and `cache_max_age_days=180` (mirrors §6 outcomes-table caps); eviction is age-first then LRU then use-count tie-break; **no schema migration on existing `fingerprints` / `outcomes` tables** (additive table only; `schema_version` bumps `"1" → "2"`; v1 readers tolerate the bump and ignore the unknown table). (4) Blended similarity: `similarity = α × cosine + (1 − α) × weighted_jaccard` with default `α = 0.6` (rationale: structural Jaccard is sparse on non-benchmark turns; embeddings discriminate better but structural is a load-bearing regularizer at 40% weight); workload-id near-keyed partition (§5.3, weight 0.85) still wins when both sides set `workload_id`; mixed-version K-NN (v1 row vs v2 row) falls back to pure structural-Jaccard per §16.5.3 so migration is forward-only and lossless. (5) `PatternConfig.fingerprint_version: Literal["v1", "v2"] = "v1"` toggle on a new `PatternConfig` struct that also collects the v1 routing knobs (`cost_weight`, `min_confidence`, `min_sample_size`, `min_eval_confidence`) for centralized resolution. Forward-only migration: set to `"v2"` in `routing.yaml`, restart process; new turns get hybrid fingerprints; legacy v1 rows age out under §6.3 over 180 days; downgrade is graceful (v2 rows remain readable under §16.5.3 fallback). (6) `embedding_strategy: Literal["sync", "async"]` knob exposes the routing-budget trade-off (sync default for agent loop; async required for gateway QPS). (7) Trade-off section (§16.9): v2 is qualitatively different from v1, not strictly cheaper — adds ~$0.000004/turn (OpenAI) ~50–200ms cache-miss latency, external API dependency, and bimodal sync-mode tail latency in exchange for cluster tightness on agent-loop traffic where v1's `intent_tags` washes out. Cache hit-rate target ≥80% within 100 turns of a workload, non-load-bearing. (8) Test plan: 15 specified tests, headline being §16.10 test 5 — "intra-cluster similarity ≥ 0.10 higher AND inter-cluster ≥ 0.05 lower under v2 than v1 on a curated 60-turn fixture spanning the 6 benchmark workloads + 4 agent-loop traces" — the explicit gate for v2 paying for itself. (9) Eight open questions including `α` tuning range, NumPy hard-dep, per-provider tokenizers, re-embed CLI, async cancellation timeout. (10) No new event types — `pattern.recorded.fingerprint_kind` already discriminates `"structural"` vs `"hybrid"` per §10.1.
- **Type:** additive. Pure spec firming; no code changes; no v1 contract changes. (1) §5.2 cross-references updated to point to §16; v1 structural-only path is unchanged. (2) §5.3 v2-blend pointer updated; v1 weighted-Jaccard formula is unchanged and is reused as the structural half of the v2 blend. (3) §13.1 + §13.2 open questions struck through and marked resolved by §16.3 and §16.7. (4) Decision log preserved at §14; new v2 decisions accreted in §16.12. (5) References section renumbered §16 → §17. (6) `routing.yaml::pattern.*` namespace is preserved (the v1 keys `cost_weight`, `min_confidence`, `min_sample_size`, `min_eval_confidence` are now centralized on `PatternConfig`; the parsing surface is unchanged).
- **References to verify:**
  - `pattern-store.md §5.2` / §5.3 / §13.1 / §13.2 — updated in this change. ✓
  - `routing-engine.md §4.4`, §5.1, §5.5 — slot-4 capability gates, `routing.yaml::pattern.*` resolution, K-NN scoring math. v2 introduces no new keys outside the existing `pattern.*` namespace; `PatternConfig` is the in-memory shape, not a wire-format change. The async `recommend()` surface change (§16.6.3) is contained in `metis-core.patterns`; routing's call site stays sync at the routing-engine spec layer (the `recommend()` future is awaited at the boundary). ⏳ Confirm in next routing-engine sweep that the §5.5 K-NN math reads cleanly against v2's mixed-version similarity in §16.5.3.
  - `event-bus-and-trace-catalog.md §6.5b` — three v1 events (`pattern.recorded`, `pattern.matched`, `pattern.evicted`) cover v2; the `fingerprint_kind` discriminator is already in the catalog payload per §10.1. No catalog change required. ✓
  - `STRATEGY.md §4` (third differentiator: pattern learning) — v2 is the implementation-ready fallback if v1 doesn't invert. STRATEGY.md does not pin the fingerprint version; no edit required. ✓
  - `STRATEGY.md §6.2` (self-hosting buyer profile) — §16.3 explicitly preserves the local-sentence-transformers option as the buyer-friendly path; no edit required. ✓
  - `STRATEGY.md §6.6` (multi-user / team patterns) — v2's per-workspace stance is unchanged from v1 §13.5–13.6; no edit required. ✓
  - `benchmarks/RESULTS.md §A3-rev2` — referenced for the failure case that motivates v2; no edit required. ✓
  - `analytics-api.md §4.7` — repricing math is unchanged; v2 does not alter cost-attribution semantics. ✓
  - `provider-adapter-contract.md` (planned) — v2's `EmbeddingProvider` is intentionally a separate Protocol from the LLM provider adapter; embedding providers do not implement `to_wire` / `from_wire_response` / `estimate_input_tokens`. The §7.2 `AdapterCapabilities` surface is for LLM adapters only and is not extended by v2. ✓
  - `context-assembler.md` — v2 truncates `user_message_text` to `max_input_tokens * 4` bytes for the embed call; the context assembler is not invoked. No interaction. ✓
- **Status:** verified for the additive scope. **Superseded by the 2026-05-14 entry above** — Wave 10 shipped the v2 implementation. §A3-rev3 did invert slot 4 under v1's structural fingerprint, but v2 ships anyway as the opt-in alternative for workspaces whose structural Jaccard washes out (the §16.1 motivation). The §16.10 test 5 cluster-tightening A/B (60-turn fixture) is deferred to a follow-up benchmark wave.

### 2026-05-14 — pricing.md v1 (commercial pricing model — recommendation, awaiting owner ratification)

- **Specs:** `pricing.md` (new — drafted v1). No code changes; pure spec. Updates `STRATEGY.md §6.8` with a pointer (question stays open). Updates `CHANGES.md` specs-in-scope + cross-reference map.
- **Change:** Closes the design gap in [`STRATEGY.md §6.8`](../STRATEGY.md) by surveying the credible pricing models for a hybrid gateway-plus-agent product and recommending one. Surveys five candidate shapes — per-seat (§5.1), per-call (§5.2), percentage of savings (§5.3), free + paid / open-core (§5.4), and four hybrid combinations (§5.5) — each evaluated across six dimensions (unit of metering, incentive alignment, first-contact friction, at-scale predictability, composability with shipped primitives, billing complexity). Constraints derived from [`deployment-shape.md`](deployment-shape.md) (the "trial without payment" floor from §4.1), [`STRATEGY.md §2`](../STRATEGY.md) (buyer ≠ user; predictability + attribution + single-bill-single-vendor), [`STRATEGY.md §6.2`](../STRATEGY.md) (startup-CTO default profile), and [`multi-user.md §5`](multi-user.md) (the shipped primitives any model must compose with). Recommendation (§7): **open-core gateway (Free tier) + per-seat Pro tier + reserved enterprise %-of-savings add-on**. The "active user" seat-metering unit composes directly with `/analytics/by_user`; tier gating is deployment-level, not per-request. Multi-user identity layer is the headline Pro feature (matches "single-user free / team use Pro" conversion trigger). Enterprise %-of-savings reserved until audit-export surface ([`multi-user.md §7.3`](multi-user.md)) is built. Invariants (§11) pin: free tier remains usable single-user; per-call shapes do not sneak into Pro baseline; Metis does not resell provider tokens; tier gating is deployment-level (no per-request licensing checks); savings counterfactual is reproducible via `pricing_version`. Open questions (§10) surface ten live items including OSS/Pro line placement feature-by-feature, savings-number visibility on Free, agent-tier bundling, Enterprise %-rate ranges. **The spec frames the choice; it does not close [`STRATEGY.md §6.8`](../STRATEGY.md).** Owner ratifies (or revises-then-ratifies); STRATEGY.md §6.8 closes only on owner action.
- **Type:** additive. New spec drafted; no code or other-spec contract changes. STRATEGY.md §6.8 gains a pointer ("specced; awaiting commercial decision") but the question stays open per the spec's own §7.6 / §14.
- **References to verify:**
  - `STRATEGY.md §6.8` — pointer added in this change; question stays open. ✓
  - `STRATEGY.md §5` — new dated entry queued at `pricing.md §14`; lands on owner ratification, not now. ⏳
  - `deployment-shape.md §6` — the §6 "What this means for adjacent open questions" entry on §6.8 already anticipated this shape ("Gateway → likely per-seat *or* % of savings"); pricing.md picks per-seat with %-of-savings reserved for Enterprise. No edit required. ✓
  - `multi-user.md §5` — the identity layer enforces the per-seat metering; no contract change required. The recommendation explicitly composes with shipped primitives without adding new ones. ✓
  - `analytics-api.md §4.7` — the savings counterfactual is the substrate any future %-of-savings tier reads against; no schema change in v1. ✓
  - `gateway.md` — gateway remains the OSS foot-in-the-door; pricing.md does not modify the gateway surface. ✓
  - `canonical-message-format.md §6.4` — `pricing_version` field is load-bearing for re-priceable savings; pricing.md invariant 6 pins this. No spec edit required. ✓
- **Status:** drafted; awaiting owner ratification. The owner closes [`STRATEGY.md §6.8`](../STRATEGY.md) when ratifying (or revising-then-ratifying); until then §6.8 reads "Specced; awaiting commercial decision." The cross-spec edits queued in `pricing.md §14` land on ratification, not now.

### 2026-05-14 — routing-engine.md §5.5 / pattern-store.md §8.1 / §9.4 / §15.4: `pattern.min_confidence` default lowered from `0.3` → `0.05` (slot-4 confidence gate scales with `cost_weight=0.1`)

- **Specs:** `routing-engine.md §5.5` ("Default rationale" paragraph and example yaml); `pattern-store.md §8.1` (call-site default comment), §9.4 (resolved-defaults example block + new explanatory paragraph), §15.4 (example yaml).
- **Change:** Lowers the `pattern.min_confidence` default from `0.3` to `0.05` in [`packages/metis-core/src/metis_core/routing/policy.py`](../../packages/metis-core/src/metis_core/routing/policy.py) (`PatternConfig`). The two slot-4 knobs are coupled: confidence is `(top_score - runner_up_score) / top_score`, where `score = (1 - cost_weight) * success + cost_weight * cost_efficiency`. Under the legacy `cost_weight=0.3` regime, the cost-efficiency term alone produced ~0.35 confidence on tied-quality clusters with cost differentials — so `min_confidence=0.3` acted as a noise gate without suppressing real signal. After the `cost_weight 0.3 → 0.1` migration (Wave 8a-2) the same near-tied clusters produce only ~0.10 confidence, so the legacy `0.3` gate suppressed the first cluster-level inversion observed in any A3 series: §A3-rev2 Pass C turn 2 on `write-a-doc-from-notes` aggregated `sonnet=0.900` vs `haiku=0.842` (confidence `0.064`), and slot 4 emitted `not_applicable` on all 18 routed turns. The Wave-9 fix scales the gate down with the cost-weight reduction so genuine inversions fire; cluster-empty / zero-score / fewer-than-K-cluster cases still gate off inside `aggregation.py`. Policy-file overrides (`pattern: { min_confidence: 0.3 }`) are preserved — workspaces that depended on the tighter gate restate it in `routing.yaml` and get the old behavior back.
- **Type:** breaking-default. Slot-4 will fire on more turns at the new default. The scoring formula, the K-NN cluster construction, and the per-rule override path are unchanged; only the default value of `PatternConfig.min_confidence` moved. Workspaces that have an explicit `pattern.min_confidence` in `routing.yaml` are unaffected.
- **References to verify:**
  - `routing-engine.md §5.5` — Default rationale paragraph extended with the `min_confidence` half of the story; example yaml updated. ✓
  - `pattern-store.md §8.1` / §9.4 / §15.4 — defaults updated in this change. ✓
  - `evaluator.md` — `min_eval_confidence` (consumer-side filter on per-verdict confidence) is unchanged; it remains `0.5` and is not affected by this gate. ✓
  - `analytics-api.md` — `/analytics/quality?min_confidence=…` is a separate filter on `eval.completed.confidence` and is unaffected. ✓
  - `benchmarks/RESULTS.md §A3-rev2 finding` — diagnoses the exact data this change resolves. No edit required.
- **Status:** verified. New tests in [`packages/metis-core/tests/routing/test_policy_loader.py`](../../packages/metis-core/tests/routing/test_policy_loader.py) cover the default migration and the explicit-override opt-out; a headline test in [`packages/metis-core/tests/patterns/test_store.py`](../../packages/metis-core/tests/patterns/test_store.py) named after the §A3-rev2 finding constructs a cluster with `haiku.score≈0.842` and `sonnet.score≈0.900` and asserts that slot 4 gates off under `min_confidence=0.3` and picks sonnet under `min_confidence=0.05`.

### 2026-05-14 — context-assembler.md v3 §5.2 lands: explicit-activation budget + pre-activation events + `[preloaded]` index annotation

- **Specs:** `context-assembler.md` v3 §5.2 (status header flipped to "Implemented"); `skill-format.md` §7.1 (index format gains `[preloaded]` annotation), §8.2 (`skill_load` pointer-return for pre-activated + re-loaded skills, budget exhaustion via `ToolExecutionError`), §9.1 (`load_reason="always"` is now wired); `event-bus-and-trace-catalog.md` §6.6 (parent ordering + `load_reason` semantics).
- **Change:** Adds per-session `SkillActivationRegistry` ([`packages/metis-core/src/metis_core/skills/activation.py`](../../packages/metis-core/src/metis_core/skills/activation.py)) tracking pre-activated skills (free, bodies inlined in stable prefix as v2 §5.1 padding) and explicit activations (counted against `MAX_EXPLICIT_ACTIVATIONS_PER_SESSION = 3` and `HARD_CAP_CUMULATIVE_ACTIVATION_TOKENS = 30000`; `WARN_CUMULATIVE_ACTIVATION_TOKENS = 10000` logs once). `SessionManager.create_session` pre-computes the stable system prompt via `_assemble_stable_system_prompt`, populates the registry with pre-activated names, and emits one `skill.loaded(load_reason="always", triggered_by_tool_use_id=None)` per inlined skill — events fire AFTER `session.started` (FK valid) and BEFORE any `turn.started` (no turn context). The cached stable prefix is reused on every LLM call in the turn loop so the provider's cache_control marker stays valid. Discovery-index lines for pre-activated skills get a `[preloaded]` annotation via post-rendering string substitution (byte-stable, no padding re-pass). `SkillLoadTool` ([`packages/metis-core/src/metis_core/skills/tools.py`](../../packages/metis-core/src/metis_core/skills/tools.py)) consults `ToolContext.skill_activations`: (a) pre-activated skills return a pointer with `{"already_preloaded": true}` metadata, no event; (b) already-explicitly-activated skills return a pointer with `{"already_loaded": true}` metadata, no event, no budget increment; (c) budget exhaustion raises `ToolExecutionError` → `tool.failed` per v3 §5.2.6 (no new event type). v3 §5.2.5 deferral honored: no mid-session eviction, no `skill.evicted` event.
- **Type:** additive. (1) Existing `_pad_stable_prefix_for_cache` signature returns `(prefix, inlined_skills)` tuple; only in-tree caller is `_assemble_stable_system_prompt` (updated) and the existing v2 §5.1 test slice (updated to unpack). (2) `ToolDispatcher.dispatch` gains an optional `skill_activations=` kwarg defaulting to `None`; existing callers compile unchanged. (3) `ToolContext` gains an optional `skill_activations` field defaulting to `None`. (4) Discovery index format gains the optional `[preloaded]` annotation; agents that didn't parse the annotation continue to work since the underlying `{name}: {description}` shape is preserved with one extra ` [preloaded]` token between name and colon. (5) `skill.loaded` payload unchanged; `load_reason="always"` is now produced (previously reserved).
- **References to verify:**
  - `context-assembler.md` v3 §5.2 — status header flipped to "Implemented" in this change. ✓
  - `skill-format.md` §7.1 / §8.2 / §9.1 — additive notes added in this change. ✓
  - `event-bus-and-trace-catalog.md` §6.6 — parent + `load_reason` semantics annotated in this change. ✓
  - `STRATEGY.md §1` — the "skills you don't use are wasted tokens" lever; v3 §5.2 caps the burn at 3 explicit activations. No edit required.
  - `pattern-store.md` — pattern fingerprint doesn't read activation state today; future skill-aware fingerprinting (v3 §5.2.7 q3) is out of scope. ✓
  - `analytics-api.md` — a future `/analytics/skills` rollup (v3 §5.2.6 "Analytics consequence") could project `skill.loaded` by `load_reason`; not in v3 scope. ✓
- **Status:** verified. New test file [`packages/metis-core/tests/sessions/test_skill_activation.py`](../../packages/metis-core/tests/sessions/test_skill_activation.py) covers: registry state transitions; budget count cap + token cap raising `SkillBudgetExceededError`; warn-threshold one-shot log; pre-activation events fire at `create_session` with the right payload shape; per-session registry is populated; `[preloaded]` annotation lands on the rendered index; `skill_load` returns the pointer (not the body) for pre-activated skills and emits no new event; re-loading an explicitly-activated skill returns a pointer, doesn't increment the budget, and emits no new event; `MAX_EXPLICIT_ACTIVATIONS_PER_SESSION + 1` distinct loads surface the 4th as `tool.failed`; activated bodies persist across turns via message history; the stable prefix is byte-identical across three consecutive turns.

### 2026-05-14 — gateway.md §3.3 / §6 — gateway keys gain optional `user_id` / `team_id` tags (Wave 8a-5)

- **Specs:** `gateway.md` §3.3 (keystore-record table) + §6 (events emitted); `multi-user.md §4` is the design reference.
- **Change:** Implements the first half of `multi-user.md §4` against the shipped gateway. (a) `GatewayKey` ([`apps/gateway/src/metis_gateway/auth.py`](../../apps/gateway/src/metis_gateway/auth.py)) gains optional `user_id: str | None` and `team_id: str | None` fields; both default to `None` for pre-multi-user keys. The keystore loader (`Keystore.from_dict`) reads them when present, validates them against the multi-user §3.4 shape (`^[a-z0-9_-]+$`, ≤200 chars), and leaves them `None` when absent — existing `keys.json` files load unchanged. (b) A new request-scoped `Identity` dataclass projects the resolved key onto `(gateway_key_id, workspace_path, user_id, team_id)` per `multi-user.md §3.2` (the spec calls this `Principal`; the v1 implementation names it `Identity` so the auth surface reads naturally — same fields, same semantics). `Keystore.identify(token)` returns it; `identity_from_key(key)` exposes the projection for testing and the harness. (c) `metis gateway issue-key` gains `--user <id>` / `--team <id>` flags (validated identically) that persist into the keystore JSON; the post-issuance summary prints both lines when set. (d) The HTTP handlers in [`apps/gateway/src/metis_gateway/app.py`](../../apps/gateway/src/metis_gateway/app.py) build an `Identity` per request and pass it to the harness; the harness ([`harness.py`](../../apps/gateway/src/metis_gateway/harness.py)) stamps `user_id` / `team_id` onto both `llm.call_completed` (typed catalog fields per Agent 8a-4) and `turn.completed` (typed catalog fields). Agent-loop traffic and pre-multi-user keys keep `user_id: None` / `team_id: None` — null-bucket rollup convention from `multi-user.md §3.4`.
- **Type:** additive. (1) Existing `GatewayKey` constructors that don't pass `user_id` / `team_id` continue to compile and behave identically. (2) Existing `keys.json` files load cleanly; the additive fields default to `None`. (3) The harness `call()` / `stream()` signatures changed from `(gateway_key_id, workspace_path, ...)` to `(identity: Identity, ...)`; the only in-tree callers are `app.py` handlers, both updated. (4) Trace consumers that read `gateway_key_id` see no change; consumers that look for the new typed `user_id` / `team_id` see them populated for tagged-key traffic and `None` everywhere else.
- **References to verify:**
  - `gateway.md §3.3 / §6` — updated in this change. ✓
  - `multi-user.md §4.1 / §4.2 / §4.4` — implementation matches the spec's keystore shape, issuance UX, and trace-stamping contract. ✓
  - `event-bus-and-trace-catalog.md §6.3 / §6.4` — typed `user_id` / `team_id` fields on `LLMCallCompleted` and `TurnCompleted` (Agent 8a-4); the harness change consumes those typed fields. ✓
  - `analytics-api.md §4.1 / §4.8` — `group_by=user` / `group_by=team` consume the new payload fields. Cross-spec edit landed by Agent 8a-6. ⏳
  - `KNOWN_ISSUES.md` — no entry tracked this work; no edit required. ✓
- **Status:** verified. The harness stamping path is exercised by `apps/gateway/tests/test_app_http.py::test_trace_events_stamp_user_id_and_team_id_for_tagged_key`; back-compat by `test_trace_events_stamp_null_identity_for_untagged_v1_key`. Implementation outstanding: `metis gateway user add` / `team add` subcommands, `users.json` / `teams.json` storage, hard-cap enforcement, and the audit-relevant `gateway.key_issued` / `gateway.key_revoked` / `gateway.quota_exceeded` event types remain — those land in later sub-tasks of the multi-user.md rollout.

### 2026-05-14 — evaluator.md §5.4 adds `grounding_tokens` / `forbidden_grounding` workload-rubric primitive (v1.1)

- **Specs:** `evaluator.md` §5.4 (workload rubric — new "Grounding-check primitive (v1.1)" subsection + example fields in the schema block).
- **Change:** `WorkloadRubric` gains two optional list-of-strings fields (`grounding_tokens`, `forbidden_grounding`) parsed from `workload.yaml.evaluate`. The heuristic awards `present / total` for grounding tokens (positive) and `1 - (present / total)` for forbidden tokens (positive on absence); when both are configured, the two components average. The composed workload score averages this with the substring/assertion-derived score when grounding is configured, so a workload that fully grounds is unaffected and one that fabricates is halved. New workload-level signals on the verdict: `workload_grounding_score`, `grounding_tokens_present`, `grounding_tokens_missing`, `forbidden_grounding_present`. New flags: `workload_grounding_tokens_present`, `workload_grounding_tokens_missing`, `workload_forbidden_grounding_present`, `workload_forbidden_grounding_clean`. The LLM-tier user message gains a "GROUNDING HINTS" section that surfaces the two lists so escalation can recognize paraphrased grounding the substring match misses (the LLM `_SYSTEM_PROMPT` is unchanged — the lists are inputs, not new instructions). Workload heuristic rubric version bump `1.0.0 → 1.1.0` per [§12](evaluator.md#12-invariants) invariant 7. Implementation in [`packages/metis-core/src/metis_core/eval/judge.py::_grounding_score`](../../packages/metis-core/src/metis_core/eval/judge.py); rubric parsing in [`eval/rubric.py::parse_workload_rubric`](../../packages/metis-core/src/metis_core/eval/rubric.py); LLM-judge user-message hint in [`eval/llm_judge.py::_grounding_hint`](../../packages/metis-core/src/metis_core/eval/llm_judge.py). Motivation comes from [`benchmarks/RESULTS.md §A3-rev`](../../benchmarks/RESULTS.md): the original `expect_substring_in_final_response="PATTERN_RECOMMENDATION"` rewarded stylistic mimicry — sonnet cited the real `PolicyEvaluation` / `RoutingDecision` dataclasses and lowercase `policy=` literals (strictly more grounded) but scored 0.50 because it didn't parrot the docstring's UPPERCASE label. The `architectural-explanation-without-hallucination` workload fixture has been updated to use the new primitive (drops `expect_substring_in_final_response`, adds 5-token grounding list + 4-token forbidden list); validated against the §A3-rev trace DBs (`benchmarks/.runs/diversity-hallucination-{haiku,sonnet}.db`) the new rubric scores sonnet 1.00 / haiku 0.90 — reverses the old 1.00 / 0.50 inversion. Sonnet hits all 5 grounding tokens (haiku misses `PolicyEvaluation` and `policy=`); neither model fabricates.
- **Type:** additive. New optional rubric fields default to `()`; workloads without them score identically to v1.0.0 except for the rubric-version stamp. The `architectural-explanation-without-hallucination` workload is the only fixture that switched primitives.
- **References to verify:**
  - `benchmark.md §3.1` — `evaluate:` block schema; new fields are optional, no edit required.
  - `evaluator.md §12` invariant 7 — version bump satisfies it. ✓
  - `benchmarks/RESULTS.md §A3-rev` — names this gap; future re-runs of the workload should report the v1.1 score series. ⏳
  - `pattern-store.md` — pattern store reads `score`; verdict shape unchanged. ✓
  - `analytics-api.md` `/analytics/quality` — projects `eval.completed.score` and ignores the new signal fields; no change. ✓
- **Status:** verified.

### 2026-05-14 — multi-user.md §4.4 foundation: `user_id` / `team_id` land on `LLMCallCompleted`, `TurnCompleted`, `MessageMetadata`

- **Specs:** `event-bus-and-trace-catalog.md` §6.2 (`turn.completed` payload), §6.3 (`llm.call_completed` payload); `canonical-message-format.md` §4.3 (`MessageMetadata`).
- **Change:** Lands the catalog and canonical-type foundation that `multi-user.md` §4.4 specced. `LLMCallCompleted` and `TurnCompleted` (in [`packages/metis-core/src/metis_core/events/payloads.py`](../../packages/metis-core/src/metis_core/events/payloads.py)) gain two additive optional fields each: `user_id: str | None = None` and `team_id: str | None = None`. Both default `None` so existing emit sites — agent-loop traffic and pre-multi-user gateway keys — keep working unchanged and roll up under the null bucket per `multi-user.md` §3.4. `MessageMetadata` (in [`packages/metis-core/src/metis_core/canonical/messages.py`](../../packages/metis-core/src/metis_core/canonical/messages.py)) gains the same two fields with the same defaults; `_identity()` is extended so equality and hashing reflect the new dimensions. Catalog sensitivity floors are unchanged: both events stay `pseudonymous` because `user_id` / `team_id` are stable opaque identifiers (`usr_<ulid>` / `team_<ulid>`), not raw PII (`multi-user.md` §3.2). Plaintext PII (email, real name) lives in `users.json` only — the trace store carries the stable id (`multi-user.md` §3.3). Catalog spec doc (`event-bus-and-trace-catalog.md` §6.2 / §6.3) is updated to enumerate the additive optional fields alongside the previously implementation-only `gateway_key_id` / `inbound_shape` / `signals_extra` fields. The session manager's emit sites are not modified by this change — they continue to omit the fields (defaulting to `None`); the gateway harness is the planned producer (lands in the gateway-auth follow-on per `multi-user.md` §4.3, Agent 8a-5).
- **Type:** additive. New optional fields; existing wire payloads decode cleanly to `None`; catalog sensitivity floors unchanged. No consumer break — `make_event`'s sensitivity check still rejects overrides more private than the floor (verified by new tests).
- **References to verify:**
  - `multi-user.md §3 / §4.4` — identity model + stamping mechanics; this change implements the catalog-and-canonical-type slice. ✓
  - `event-bus-and-trace-catalog.md §6.2 / §6.3` — payload schemas updated in this change. ✓
  - `canonical-message-format.md §4.3` — `MessageMetadata` updated in this change. ✓
  - `gateway.md §6` — gateway-side stamping (where the producer fills the new fields) lands in Agent 8a-5; this change is the consumer-side foundation. ⏳
  - `analytics-api.md §4.1 / §4.9` — Agent 8a-6 has landed the analytics surface that reads these fields via `json_extract`; this change provides the typed source stamps. ✓
  - `routing-engine.md §5.3.2` — three new predicates (`user_cost_today_exceeds_usd`, `team_cost_today_exceeds_usd`, `team_cost_month_exceeds_usd`) land at routing-rule integration time; this change provides the trace-store dimension they read against. ⏳
- **Status:** verified for the catalog-and-canonical-type slice; downstream consumers (gateway producer in 8a-5, routing predicates) land in follow-on changes as flagged above.

### 2026-05-14 — analytics-api.md §4.1 + new §4.9 (user/team rollups land)

- **Specs:** `analytics-api.md` §4.1 (group_by enum + user/team filter params) and new §4.9 (`/analytics/by_team`); §6 (new error codes).
- **Change:** Implements the first slice of `multi-user.md §5` — the analytics surface buyers need to attribute cost beyond the gateway-key boundary. `_COST_GROUP_BY_ALLOWED` in [`packages/metis-core/src/metis_core/analytics/store.py`](../../packages/metis-core/src/metis_core/analytics/store.py) gains `user` and `team`, projecting `json_extract(payload_json, '$.user_id')` / `'$.team_id'` parallel to the shipped `gateway_key` slot. `AnalyticsStore.cost()` gains optional `user=` / `team=` exact-match filters, both passed via SQL placeholder; the HTTP boundary additionally regex-validates the shape (`^[A-Za-z0-9_-]{1,200}$`) and returns 400 `invalid_user` / `invalid_team` on malformed values. New `AnalyticsStore.by_team()` + `/analytics/by_team` HTTP route mirror the shipped `/analytics/by_key` shape: per-team `cost_usd` + token counts + `call_count` + `user_count` (distinct non-null users in the team) + `by_user` sub-array sorted by cost DESC. The null bucket (agent-loop traffic + pre-v1 keys issued without `--user` / `--team`) appears as `team_id: null` with `user_count: 0`. v1 ships the rollup shape; `team_name` / `daily_cap_usd` / `monthly_cap_usd` join to `teams.json` and the `partial_coverage` flag from `multi-user.md §5.2 / §5.4` are deferred until the gateway-side identity records land (multi-user.md §4.2). Dependent on Agent 8a-4's catalog-field stamping and Agent 8a-5's gateway harness writing those stamps; until both land, every event projects `null` and rolls up under the null bucket — the contract still works.
- **Type:** additive. New whitelist values, new optional filter params, new endpoint, new error codes. Existing `/analytics/cost?group_by=model` callers see no shape change.
- **References to verify:**
  - `multi-user.md §5.1 / §5.2 / §5.3` — the spec this implements. The `partial_coverage` flag from §5.4 is the next slice; flagged in `analytics-api.md §4.9` "v1 scope" note. ✓
  - `event-bus-and-trace-catalog.md §6.3` — `LLMCallCompleted.user_id` / `team_id` are the source stamps; this change reads them as `json_extract` projections, so it tolerates absence (rolls up under null). Catalog edit pending Agent 8a-4. ⏳
  - `gateway.md §3.3 / §6` — gateway-side keystore changes (`GatewayKey.user_id` / `team_id`) and request-time stamping pending Agent 8a-5. ⏳
  - `analytics-api.md §4.8` — `/analytics/by_key` shape was the template; `/analytics/by_team` follows the same envelope and sort convention. ✓
- **Status:** verified for analytics layer; downstream stamp producers (8a-4, 8a-5) verify when they land. The store and HTTP handler are correct against the spec contract today and against `null`-stamped events in production until then.

### 2026-05-14 — pattern-store.md §5.1 adds optional `workload_id` near-keyed partition

- **Specs:** `pattern-store.md` §5.1 (new row in the structural-feature table), §5.3 (blended similarity formula prose for the new field).
- **Change:** `FingerprintInputs` / `StructuralFeatures` gain an optional `workload_id: str | None` field (default `None`). When both fingerprints in a comparison set `workload_id`, the K-NN similarity is blended `0.85 * cluster + 0.15 * structural` so same-workload neighbors cluster together first. When either side is `None` the blend is skipped and the formula reduces to the v1 weighted-Jaccard exactly. `SessionManager.submit_turn` accepts an optional `workload_id` kwarg that flows through `TurnContext` to the `fingerprint_inputs_builder` / `fingerprint_inputs_hook` callbacks. The benchmark harness sets it to the workload name; agent-loop callers (CLI / TUI / serve / gateway) leave it `None`. Rationale comes from §A3-rev unblock #1 (`benchmarks/RESULTS.md`): `intent_tags` is empty on most turns so K-NN was clustering by tool shape + length bucket, which mixed workloads and washed out per-workload quality deltas.
- **Type:** additive. Existing fingerprints in stored DBs decode with `workload_id=None`; new writes without a workload tag produce identical K-NN behavior to v1. Existing callers do not need to change.
- **References to verify:**
  - `routing-engine.md` §5.5 — formula and `cost_weight` default unchanged; only the fingerprint inputs are richer.
  - `benchmarks/RESULTS.md §A3-rev / §A3-rev2` — names this as unblock #1; future A3-rev2 should re-run with `workload_id` set by the harness.
  - `KNOWN_ISSUES.md` — no entry tracks this gap; no edit required.
- **Status:** verified.

### 2026-05-14 — routing-engine.md §5.5 `cost_weight` default lowered 0.3 → 0.1

- **Specs:** `routing-engine.md` §5.1 example, §5.5 (formula prose + new "Default rationale" paragraph + changelog row).
- **Change:** The default for `pattern.cost_weight` (the routing slot 4 cluster-score blend constant) drops from `0.3` to `0.1` in `PatternConfig` and the `routing.yaml` loader. The scoring formula `score_M = (1 - cost_weight) × normalized_success_M + cost_weight × normalized_cost_efficiency_M` is unchanged — only the constant moves. Rationale comes from the §A3-rev benchmark (`benchmarks/RESULTS.md`): at 0.3 the cost-efficiency term required a ~0.43 success delta to flip the chooser when the cheapest model also scored 1.0 on cost_efficiency, which swamped the 0.15–0.30 cluster-level quality deltas the LLM judge actually produced; slot 4 picked the cheaper model on every routed turn regardless of evidence. At 0.1 a quality delta of ~0.143 is enough to invert the ranking. Per-workspace override (`pattern.cost_weight: 0.3`) is unchanged — workspaces that depended on the prior cost-bias must restate the old default in `routing.yaml`.
- **Type:** breaking-default. Consumers relying on the prior 0.3 blend must opt in via policy file; behavior for any policy that explicitly set `cost_weight` is unchanged.
- **References to verify:**
  - `pattern-store.md` — §8.3/§8.4 reference the scoring formula but not the default constant; no edit required.
  - `benchmarks/RESULTS.md §A3-rev` — names this as unblock #2; future §A3-rev2 reads the new default.
  - `KNOWN_ISSUES.md` — no entry tracks this default; no edit required.
- **Status:** verified.

### 2026-05-14 — event-bus-and-trace-catalog.md §4.4.1 enforced; `eval.completed` floor inverted

- **Specs:** `event-bus-and-trace-catalog.md` §4.4.1 (rule clarification + example), §6.12 (`eval.completed` floor sensitivity); `evaluator.md` §8.2 and §8.4 (floor + downgrade pathway).
- **Change:** `make_event` now rejects a `sensitivity` override that is more private than the catalog floor (raises `EventValidationError`), per §4.4.1's "only toward less private" rule. The rule's prose is reworded so "floor" is unambiguously the *worst case* — the most-private classification the event can have when all opt-in fields are populated — and a downgrade is what happens when the event carries less than the worst-case content. To make `eval.completed` spec-consistent under the strict rule, its catalog floor moves from `pseudonymous` → `user_controlled` (the worst case, when `signals.rationale_redacted` is populated) and the evaluator subscriber's `_sensitivity_for` is inverted: when the rationale field is absent, downgrade to `pseudonymous` (allowed); when present, no override.
- **Type:** breaking for `eval.completed` consumers that filter by `sensitivity == pseudonymous` (the floor moved up). Additive for everything else — non-`eval.completed` events keep their existing floors; the new `make_event` check rejects overrides that were never spec-conformant.
- **References to verify:**
  - `evaluator.md §8.2 / §8.4` — updated in this change to match the new floor and downgrade pathway.
  - `event-bus-and-trace-catalog.md §4.4.1 / §6.12` — updated in this change.
  - `analytics-api.md` — `/analytics/quality` projects `eval.completed.score` and doesn't filter on `sensitivity`; no behavior change.
  - `KNOWN_ISSUES.md` — "Sensitivity upgrade rule unenforced" 🟢 entry deleted; replaced by the enforcing check.
- **Status:** verified.

### 2026-05-14 — delegation.md v1 (Phase 4 worker-session design)

- **Specs:** `delegation.md` (new — drafted v1). No code changes; pure spec. Implies additive cross-spec edits flagged below — none land until Phase 4 implementation.
- **Change:** Consolidates the worker-session contract that has been distributed across [`routing-engine.md §6`](routing-engine.md) (the `delegate()` tool, tier resolution, slot 5 re-entry, `InsufficientContextRequest`), [`event-bus-and-trace-catalog.md §6.8`](event-bus-and-trace-catalog.md) (the three `delegate.*` events), and [`streaming-protocol.md §6.4 + §7`](streaming-protocol.md) (cancellation cascade, `include_worker_sessions` filter) into one Phase-4 design document. Defines what the worker session *is* (full Session record with additive `parent_session_id` / `parent_tool_use_id` / `is_worker` fields), the spawn → routing-re-entry → execution → completion lifecycle, the read-only isolation contract against MEMORY.md / USER.md / skills / routing config (planner-only durable state), the cost-attribution model (worker tokens land on the worker's `llm.call_completed`; `delegate.completed.worker_total_cost_usd` is derived, single source of truth via `llm.call_completed`), pattern-store integration (workers write their own fingerprint rows; slot 4 forced to defer inside delegation re-entry so learned patterns don't silently override the planner's explicit `tier=`), evaluator integration (worker terminal turn scored independently; parent session rubric folds in `delegate.completed.success` but parent *turn* score is not transitively inflated by worker scores), and the confirmation-handler-inheritance rule (workers inherit planner's handler; "always" answers from worker prompts do NOT persist to `trust.yaml` in v1). Slot 5 (`DELEGATE_REQUEST`) treatment is non-normative — canonical source remains routing-engine §6. Documents v1 as **opt-in**: gated by `can_delegate: true` in the registry + active planner model + planner LLM choice; default registry has `can_delegate: false` on `fast`-tier models so buyers without multi-step workloads never see the surface. Open questions section surfaces (1) cost-of-delegation overhead for small sub-tasks, (2) cancellation cascade for already-completed workers, (3) concurrent delegation cap, (4) worker streaming back to planner (deferred per `streaming-protocol.md §12.2`), (5) worker wall-clock timeout, (6) router-decided delegation (rejected for v1 — predicate routing can't distinguish delegatable sub-tasks), (7) worker-prompt "always" answers persisting to trust.yaml, (8) tier name configurability, (9) worker history visibility default.
- **Type:** additive. New spec drafted; all cross-spec implications below are additive (existing consumers are unchanged; new fields default to `None` / `false`).
- **References to verify:**
  - `routing-engine.md §6` — canonical source for the `delegate()` tool signature, `can_delegate`, tier resolution, slot 5 re-entry, `InsufficientContextRequest`. No edits required; delegation.md treats §6 as the source of truth.
  - `event-bus-and-trace-catalog.md §6.8` — three `delegate.*` events already present in the catalog (Phase 4). `delegation.md §9` proposes two additive `delegate.started` payload fields (`allowed_tool_count`, `dropped_tools`); catalog edit lands with implementation.
  - `event-bus-and-trace-catalog.md §6.3` — `llm.call_started.is_worker` and `Actor.WORKER` already in the catalog. No change required.
  - `streaming-protocol.md §6.4 + §7` — cancellation-during-delegation seam and `include_worker_sessions` filter are already documented; no edits.
  - `server-api.md` — `is_worker` / `parent_session_id` already on the session record; `include_workers` query already documented. No edits.
  - `canonical-message-format.md §9.1` — Session schema gains three additive nullable columns (`parent_session_id`, `parent_tool_use_id`, `is_worker`); migration is `ALTER TABLE ADD COLUMN ... DEFAULT NULL`. Cross-spec edit lands with implementation.
  - `pattern-store.md` — worker writes its own fingerprint row; `parent_session_id` is not projected into the fingerprint. Cross-spec edit if pattern-store wants to add a worker-aware filter (§11 deferred).
  - `evaluator.md §5.6 + §6.1` — parent session rubric folds in `delegate.completed.success`; current heuristic rubric does not yet read this signal. Cross-spec edit lands with Phase 4 implementation.
  - `analytics-api.md §4.1` — `_COST_GROUP_BY_ALLOWED` gains `parent_session` and `is_worker` group_by values; `include_workers` query parameter behavior added. Cross-spec edit lands with implementation.
  - `tool-dispatcher.md` — `delegate` registered as a builtin tool with elevated kernel privileges (can spawn a session); no other builtin has this capability. Cross-spec edit lands with implementation.
  - `context-assembler.md §5` — worker's system prompt uses the same assembler path as planner's; no change required.
  - `STRATEGY.md §4` — "third lever (planner→worker delegation)" now has a drafted Phase-4 spec home. Existing thesis statement unchanged.
- **Status:** drafted; awaiting owner review. Cross-spec edits enumerated above land alongside Phase 4 implementation.

### 2026-05-14 — multi-user.md v1 (per-user / per-team identity & rollup layer)

- **Specs:** `multi-user.md` (new — drafted v1), implies additive cross-spec changes flagged below. No code changes; pure spec.
- **Change:** Adds a per-user / per-team identity layer on top of the shipped per-(gateway-key) attribution from [`gateway.md §3.3 / §6`](gateway.md). Defines three identity dimensions (`User`, `Team`, `Workspace`) and a request-scoped `Principal` projection of `GatewayKey`. `metis gateway issue-key` gains `--user` / `--team`; new `metis gateway user add` / `team add` subcommands manage `~/.metis/gateway/users.json` and `teams.json` (mode `0o600`). Trace-stamping additive: `user_id` and `team_id` land on `LLMCallCompleted` and `TurnCompleted` (parallel to the existing `gateway_key_id` / `inbound_shape`). Analytics surface extends: `group_by` ∈ {`user`, `team`} on `/analytics/cost`; new `/analytics/by_team` rollup (mirrors the shipped `/analytics/by_key`); optional `?user=` / `?team=` filters on all five time-windowed endpoints; new `partial_coverage` flag for mixed-mode rollout windows. Quota enforcement is two-layered: routing-rule **soft caps** via three new predicates (`user_cost_today_exceeds_usd`, `team_cost_today_exceeds_usd`, `team_cost_month_exceeds_usd`) parallel to the shipped `cost_today_exceeds_usd`; gateway-boundary **hard caps** via `Team.daily_cap_usd` / `monthly_cap_usd` (and finally activating the previously reserved `GatewayKey.daily_cap_usd`) — hard cap short-circuits before routing, returns 429, emits a new `gateway.quota_exceeded` audit event. Three new `gateway.*` catalog events: `key_issued`, `key_revoked`, `quota_exceeded`, all `pseudonymous`-sensitive. Privacy posture: plaintext email lives in `users.json` only; trace events carry the stable `user_id`; `email_sha256` exists for bootstrap-dedup and a future SSO bridge. Deployment-shape neutral — same struct + wire shape in local-FS and SaaS deployments; only the storage backend differs. v1 explicitly excludes SSO / OIDC / SAML / SCIM / RBAC / multi-org / multi-workspace-per-key (§8); the startup-CTO default from [`STRATEGY.md §6.2`](../STRATEGY.md) is the v1 target.
- **Type:** additive. New spec drafted; all cross-spec implications below are additive (no existing consumer breaks; missing fields default to `None`).
- **References to verify:**
  - `gateway.md §3.3` — `GatewayKey` gains two optional fields (`user_id`, `team_id`); existing keys with both `None` keep working. Cross-spec edit lands with implementation; flagged in `multi-user.md §4.1`.
  - `gateway.md §11` — "Multi-user / team-level rollups" follow-on now references `multi-user.md` as the design. Edit at implementation time.
  - `event-bus-and-trace-catalog.md §6.3` — `LLMCallCompleted.user_id` / `team_id` and `TurnCompleted.user_id` / `team_id` are typed additive fields; same pattern as the shipped `gateway_key_id` extension. Catalog edit lands with implementation.
  - `event-bus-and-trace-catalog.md §6` — three new event types (`gateway.key_issued`, `gateway.key_revoked`, `gateway.quota_exceeded`); payload structs sketched in `multi-user.md §7.2`. Catalog entry per `AGENTS.md` "Adding a new X" recipe at implementation time.
  - `routing-engine.md §5.3.2` — three new predicates (`user_cost_today_exceeds_usd`, `team_cost_today_exceeds_usd`, `team_cost_month_exceeds_usd`) parallel to `cost_today_exceeds_usd`; same snapshot-at-turn-start semantics. Edit at implementation time.
  - `analytics-api.md §4.1` — `_COST_GROUP_BY_ALLOWED` whitelist gains `user` / `team`. Endpoint shape additive.
  - `analytics-api.md §4.8` — new sibling endpoint `/analytics/by_team` documented in `multi-user.md §5.2`. Edit at implementation time.
  - `analytics-api.md §4.7` — savings endpoint's behavior under `?team` filter clarified in `multi-user.md §5.4`; no math change.
  - `STRATEGY.md §2` — "multi-user from day one is real" and "team-level cost attribution matters" both have a drafted spec home. §2 stays open per the prompt's instructions (it closes when the spec lands **and** [`STRATEGY.md §6.3`](../STRATEGY.md) — local-first vs SaaS — is decided).
- **Status:** drafted; awaiting owner review. Cross-spec edits enumerated above land alongside Phase 3 implementation.

### 2026-05-14 — evaluator.md §5.1 turn rubric reads `tool.completed.success=False`

- **Specs:** `evaluator.md` §5.1 (turn heuristic rubric — new `no_tool_exit_failure` signal + prose distinguishing the two tool-failure paths).
- **Change:** Closes the first [§A3](../../benchmarks/RESULTS.md#a3-why-the-differentiator-does-not-fire) unblock. The v1 turn heuristic's tool-failure gate previously only fired on `tool.failed` (uncaught Python exception); a shell tool that prints `"FAIL N/M"` and exits with a non-zero code emits `tool.completed` with `success=False` and was invisible to the rubric. v1.1 adds a sibling gate `no_tool_exit_failure` that scans for `tool.completed` events with `success=False`. Weighted at 0.5 (vs `weight_no_tool_failure=0.25`) — sized so a single failed exit drops a clean turn's score from 1.0 to ~0.667 (drop ≥0.3) and the heuristic confidence to 0.55, below the v1 hybrid escalation threshold (0.7). This lets `HybridJudge` escalate to the LLM judge on this class of failure regardless of whether the bus subscriber plumbs assistant-response text. Implementation in [`packages/metis-core/src/metis_core/eval/judge.py::_evaluate_turn`](../../packages/metis-core/src/metis_core/eval/judge.py); weight + total normalization in [`eval/rubric.py::TurnHeuristicConfig`](../../packages/metis-core/src/metis_core/eval/rubric.py). Rubric version bump `1.0.0 → 1.1.0` per [§12](evaluator.md#12-invariants) invariant 7 so prior `eval.completed` rows are not silently recalibrated.
- **Type:** additive. Existing positive-lifecycle signals are untouched; turns that had no `tool.completed.success=False` events behave identically (clean score still 1.0). The rubric version bump produces a new score series rather than mutating old verdicts.
- **References to verify:**
  - `event-bus-and-trace-catalog.md §6.x` — `ToolCompleted.success: bool` already exists and is unchanged. ✓
  - `evaluator.md §5.3` — Hybrid escalation threshold default 0.7 is unchanged; the new signal lowers heuristic confidence into the escalation band on tool-exit failures. ✓
  - `evaluator.md §12` — invariant 7 (rubric versioning); the bump to 1.1.0 satisfies it. ✓
  - `evaluator.md §5.1` Agent 7a-2's `signals_extra` contract paragraph — independent edit in the same section; cross-references the same §A3 unblock list. ✓
  - `benchmarks/RESULTS.md §A3` — re-run owned by Agent 7a-7; not modified here. ⏳
- **Status:** verified.

### 2026-05-14 — evaluator.md §5.1 turn-completed `signals_extra` plumbed for LLM judge

- **Specs:** `evaluator.md` §5.1 (signals_extra contract).
- **Change:** Documented the three-key `turn.completed.signals_extra` contract produced by `SessionManager._emit_turn_completed`: `final_response_text` (existing; heuristic content-penalty reader), `assistant_response_text` (new alias of `final_response_text`; LLM-judge `_build_user_message` reader), and `user_prompt_text` (new; LLM-judge `_build_user_message` reader). Closes the second [§A3](../../benchmarks/RESULTS.md#a3-why-the-differentiator-does-not-fire) unblock — the online bus path now forwards enough text for the LLM judge to grade a turn instead of reading "(not available)" / "(not available)". The `assistant_response_text` alias is intentional and points at the same string as `final_response_text`; a future migration can drop it once heuristic and LLM consumers converge on one name. Keys with empty values are omitted so absent text degrades to the judge's "(not available)" fallback honestly.
- **Type:** additive. The producer only adds keys; the existing `final_response_text` reader path is unchanged. The new `user_prompt_text` parameter on `_emit_turn_completed` is keyword-only with a `None` default.
- **References to verify:**
  - `event-bus-and-trace-catalog.md` — `TurnCompleted.signals_extra` is already typed as a free-form `dict | None` per §6.4; no payload-registry change. ✓
  - `evaluator.md §5.2` — the LLM-as-judge rubric's input list still cites "user prompt + assistant final response text" generically; the §5.1 contract update is the cross-reference that makes it concrete. ✓
  - `benchmark.md` — the workload harness already plumbs `user_prompt_text` / `assistant_response_text` at the workload subject level; no change. ✓
- **Status:** verified.

### 2026-05-14 — gateway.md v1 (captures shipped surface) + per-key analytics rollup

- **Specs:** `gateway.md` (v0 skeleton → v1), `analytics-api.md` §4.1 + new §4.8, `server-api.md` (implicit — `GET /sessions/{id}.routing_policy_version` now populated).
- **Change:** Rewrote `gateway.md` from v0 skeleton to v1 documentation of the shipped transparent HTTP gateway in [`apps/gateway/`](../../apps/gateway/). Documents the actual endpoint shapes (`/v1/chat/completions`, `/v1/messages`, `/healthz`), the auth scheme (`Authorization: Bearer gw_<ulid>` or `x-api-key`), the keystore at `~/.metis/gateway/keys.json` (SHA-256 hash; mode `0o600`), the per-shape translation rules, the additive `gateway_key_id` + `inbound_shape` stamps on `LLMCallCompleted` / `TurnCompleted` (gateway.md §6), and the v1 loopback-only network posture (§3.2 — reverses the original v0 "default `0.0.0.0`" plan until per-key rate limiting and audit log land). Notes the §5.3 "transparent mode" trade-off — gateway clients passing `model` always trigger the `per_message_override` slot win — recommends leaving the default as-is and tracks a future `--ignore-inbound-model` flag for the cost-optimization magic-trick mode. Added `gateway_key` to `_COST_GROUP_BY_ALLOWED` in [`analytics/store.py`](../../packages/metis-core/src/metis_core/analytics/store.py) and shipped a new `/analytics/by_key` endpoint (analytics-api.md §4.8) backed by `AnalyticsStore.by_key()` — per-(gateway_key_id) cost + token + call_count rollup with an `by_inbound_shape` sub-array per row, rows with null `gateway_key_id` (agent-loop traffic) keyed under `null`. Surfaced `routing_policy_version` on `GET /sessions/{id}` (and the `POST /sessions` 201): added a content-derived `version` field on `RoutingPolicy` (truncated sha256 of the raw yaml at parse time; `None` for `EMPTY_POLICY`); `SessionManager.routing_policy_version()` exposes it to the HTTP layer.
- **Type:** additive. New analytics endpoint, new optional `gateway_key` group_by value, new optional response field on session endpoints, new optional `RoutingPolicy.version` (default `None` preserves call sites that construct policies directly).
- **References to verify:**
  - `event-bus-and-trace-catalog.md §6.3` — `LLMCallCompleted.gateway_key_id` / `inbound_shape` already land as typed optional fields. ✓
  - `analytics-api.md §4.1 + §4.8` — group_by enum extended; new endpoint shape documented. ✓
  - `routing-engine.md §5.7` — `RoutingPolicy` gains a `version` field; the validation rules and parser entry points are unchanged. ✓
  - `server-api.md §4.x` — `GET /sessions/{id}` response gains a populated `routing_policy_version` field. Already declared in the shape; no schema breakage. ✓
  - `KNOWN_ISSUES.md` — 🟡 "Per-key analytics roll-up has no HTTP surface" entry deleted (this change ships the HTTP surface). ✓
- **Status:** verified.

### 2026-05-14 — provider-adapter-contract.md v1.2 (CanonicalResponse returns content, not Message)

- **Spec:** `provider-adapter-contract.md` §3.3 (CanonicalResponse shape).
- **Change:** Bring §3.3 into line with the shipped impl. `CanonicalResponse` returns `content: list[ContentBlock]` + `model` + `provider` rather than a full `Message`. The adapter doesn't own two `Message` fields the spec previously implied it did: the `RoutingDecisionRecord` (decided upstream by the routing engine) and `Usage.cost_usd` (computed by core from the local price table per canonical-format §6.4). The caller (`SessionManager`) assembles the final canonical `Message` from the adapter's parts plus its own routing decision, cost computation, and id allocation. Adapter implementations have been on this shape since Phase 1 (`[adapters/protocol.py](../packages/metis-core/src/metis_core/adapters/protocol.py)` docstring + AGENTS.md "Implementation conventions" already noted the divergence); v1.2 closes the spec/impl gap. Substitutability is unaffected — the substitutability gate is the `(content, stop_reason, usage)` triple, not the `Message` envelope.
- **Type:** additive (the spec catches up with shipped impl; no consumer change required — there are no callers writing to the old shape).
- **References to verify:**
  - `canonical-message-format.md §5` — `Message` shape unchanged. The fields the adapter previously owned in `Message` (id, role, content, metadata.routing, metadata.usage.cost_usd) are now assembled by `SessionManager`; no canonical-format edit required. ✓
  - `streaming-protocol.md §5.6` — the streaming-side `MessageComplete` event's authoritative final content + usage shape is unchanged; it already returns content blocks rather than a `Message`. ✓
  - `event-bus-and-trace-catalog.md §6.3` — `llm.call_completed` payload reads from `CanonicalResponse.usage` / `model`; new shape preserves those fields. ✓
  - `KNOWN_ISSUES.md` — "`CanonicalResponse` shape divergence from spec" 🟢 entry retired by this change. ✓
- **Status:** verified.

### 2026-05-14 — context-assembler.md v3 (skill activation)

- **Spec:** `context-assembler.md` §5.2 (new), §7 (skill-activation entry retired from out-of-scope; new entries for auto-activation, mid-session eviction, per-workspace budget overrides), §8 (six new decision-log entries), §9 (new references to `skill-format.md` and `event-bus-and-trace-catalog.md §6.6`).
- **Change:** Specs the **skill activation** layer of the cost lever per [`STRATEGY.md §1`](../STRATEGY.md). Three activation paths partitioned by `skill.loaded.load_reason`: (a) **pre-activation** (`"always"`) — v2 §5.1's body-as-padding is formalized as observable activation, emitted once per inlined body at session init with `triggered_by_tool_use_id=None`; (b) **explicit activation** (`"on_demand"`) — existing `skill_load` tool path, unchanged except for the new budget check; (c) **auto-activation** (`"auto_suggested"`) — **not in v3**, reserved. No description-match-driven auto-activation in v3 (rationale: preserves agentskills.io progressive disclosure semantics; avoids non-determinism breaking caches; no usage data to tune classifier against). Per-session activation budget: `MAX_EXPLICIT_ACTIVATIONS_PER_SESSION=3` count cap, `WARN_CUMULATIVE_ACTIVATION_TOKENS=10000` log-only, `HARD_CAP_CUMULATIVE_ACTIVATION_TOKENS=30000` hard cap; all surface as `ToolExecutionError` → `tool.failed` (no new event types). Pre-activated skills don't count against budget. Discovery index entry for a pre-activated skill annotated `[preloaded]`; `skill_load(name)` for a pre-activated skill returns a pointer ("already in system prompt"), not the body, to avoid double-paying input bytes. **No mid-session eviction** in v3 — would invalidate message-level caches a future spec might place, and require unwinding structurally-linked tool_use/tool_result pairs. Deferred to history-compression spec.
- **Type:** additive on context-assembler.md; implies two additive cross-spec changes flagged below.
- **References to verify:**
  - `skill-format.md §7.1` — discovery-index format currently specified as `- {name}: {description}`. v3 §5.2.2 adds an optional `[preloaded]` annotation on pre-activated skills (`- {name} [preloaded]: {description}`). Additive — readers ignoring the annotation see no behavior change. Cross-spec edit lands with implementation; flagged in `context-assembler.md §5.2.7` open question 2.
  - `skill-format.md §8.2` — `skill_load` tool semantics gain a budget check (raises `ToolExecutionError` on exhaustion) and a pre-activated-skill special case (returns pointer text with `{"already_preloaded": true}` metadata, no body, no event re-emission). Additive: existing callers see no change in the in-budget non-preloaded case.
  - `event-bus-and-trace-catalog.md §6.6` — `skill.loaded` payload schema unchanged. v3 emits the existing `load_reason="always"` enum value from a new path (session init, post-`session.started`, pre-first-`turn.started`). No catalog edit required.
  - `analytics-api.md` — v3 mentions a future `/analytics/skills` rollup keyed on `load_reason` for tuning the v2 padding source priority; not specified in v3 and no analytics-api edit required.
  - `STRATEGY.md §1` — context > skills > model selection thesis: v3 specifies the second-largest lever (skills) inside the largest (context). No narrative change required; cross-reference only.
  - `benchmark.md` — no current workload exercises skill loading. Wave 6 should add one before tuning the default budget numbers; flagged in `context-assembler.md §5.2.7` open question 1. No spec edit required.
- **Status:** pending owner sign-off on the five open questions in §5.2.7 (default budget numbers; `[preloaded]` annotation format vs alternatives; auto-activation deferral; re-load-as-no-op semantics; pre-activation event ordering). Cross-spec edits to `skill-format.md §7.1` / §8.2 land with implementation (Wave 6+); both are additive.

---

### 2026-05-14 — context-assembler.md v2 (minimum-cacheable-prefix rule)

- **Spec:** `context-assembler.md` §5.1 (new), with rationale + decision log entries.
- **Change:** v1's prompt-cache breakpoint placement was honest but the natural Metis stable prefix (DEFAULT_SYSTEM_PROMPT + five built-in tools ≈ 265 heuristic tokens) tokenizes well below the *effective* haiku-4-5 cache floor — a live probe found a 3320-actual-token prefix produces `cache_creation_input_tokens = 0` while a 4957-token prefix succeeds. v2 adds a §5.1 rule requiring `SessionManager` to pad the stable prefix to clear that effective floor with margin (`MIN_CACHEABLE_PREFIX_TOKENS = 4500`, `MAX_CACHEABLE_PREFIX_TOKENS = 5500` heuristic tokens). Padding sources, in priority order: (1) loaded skill bodies in name-ascending order, (2) a static byte-stable `_OPERATING_CONTEXT_PADDING` block of Metis operating guidelines. Determinism is load-bearing — module-level constant; no per-call I/O. v1's breakpoint placement, the two-segment `system_prompt`/`system_prompt_volatile` shape, and the breakpoint-on-last-stable-block rule are all unchanged. Live verification: `scripts/smoke_cache.py --model haiku` now passes with the natural Metis prompt (turn 1 writes 5167 cache tokens; turn 2 reads 5167). Benchmark Run 3 (`benchmarks/RESULTS.md`): cache fires on **49 of 49 LLM calls (100%)** vs Run 2 cold's **10 of 30 (33%)**; same-3-workload aggregate cost dropped 22.8%.
- **Type:** additive. The §5.1 rule is a new section; v1's existing rules in §1–§4 and §5 (preceding §5.1) are unchanged. Callers that pass a custom `system_prompt` already above the floor see §5.1 as a no-op.
- **References to verify:**
  - `canonical-message-format.md §7` — adapter contract unchanged; `CanonicalRequest.system_prompt` / `system_prompt_volatile` shape unchanged. ✓
  - `analytics-api.md §4.2` — `cache_effectiveness` endpoint reads the same `cache_creation_input_tokens` / `cached_input_tokens` fields; no schema change. ✓
  - `skill-format.md` — v2 §5.1 inlines skill bodies into the cached prefix when padding is needed, which is a deviation from agentskills.io "progressive disclosure" (discovery only, activation via `skill_load`). The decision log records the reasoning: progressive disclosure still applies to the discovery index; bodies are only inlined when the prefix needs the bytes to clear the floor. No skill-format spec change required. ✓
  - `benchmark.md §6.2` — variance tolerance (`±5pp` on `savings_pct`, `±2 llm_call_count`) unchanged; Run 3 sits within tolerance against Run 2. ✓
- **Status:** verified.

### 2026-05-14 — benchmark workload diversity v1 (two discriminating fixtures)

- **Spec:** `benchmark.md` §4 (the suite).
- **Change:** Two new workloads added under [`benchmarks/workloads/`](../../benchmarks/workloads/): `regex-with-edge-cases` (one-shot NANP regex against 16 labeled cases; locked-down iteration via `max_tool_calls: 1` on the run turn) and `multi-file-refactor-with-shared-types` (7-file rename with an aliased import in `legacy.py`). Both ship `evaluate:` blocks with `expect_substring_in_final_response` so the heuristic judge gets an objective success signal. The shipped regex workload discriminates haiku-4-5 (`0.25`) vs sonnet-4-6 (`1.00`) at the workload-level score; the mfr workload scores `1.00 / 1.00` (parity datapoint, not a discriminator at the current model pair's capability). Full numbers and the cost-per-success inversion are in [`benchmarks/RESULTS.md`](../../benchmarks/RESULTS.md) under "Workload diversity v1". The benchmark spec's §4 "V1 ships three workloads" table is now an undercount (six workloads ship via filesystem discovery, including the prior `intentionally-failing-task` control case) — descriptive drift rather than a contract change.
- **Type:** additive. New fixtures discovered via the existing filesystem-based loader in `scripts/benchmark.py`; no harness or schema changes. The test that pins the discovered-workload set ([`apps/cli/tests/test_benchmark.py::test_shipped_workloads_load_clean`](../../apps/cli/tests/test_benchmark.py)) was updated to include the two new names — purely additive, no removal. Test count: 1029 passed (was 979; the +50 includes other parallel work landing during the same window).
- **References to verify:**
  - `pattern-store.md §8.3` — the K-cluster aggregator formula now has an input distribution where `success_mean_haiku < success_mean_sonnet`. The mechanism was already implemented; the new fixture provides the first real-API distribution that triggers the cost-vs-success trade-off. ✓ (no spec change needed; section in RESULTS.md cites the formula).
  - `evaluator.md §5.4` — workload-level rubric's `expect_substring_in_final_response` path is exercised by both new fixtures. The hybrid judge tier (just-landed) reads the same `signals_extra` plumbing, so these fixtures double as inputs to the LLM-judge upgrade. ✓
  - `benchmark.md §4` — the table listing v1's three workloads is now an undercount (six workloads discovered). Worth a follow-up edit to either enumerate all six or note that discovery is filesystem-based; not blocking.
- **Status:** verified.

### 2026-05-14 — evaluator: LLM-as-judge + hybrid escalation tier shipped

- **Spec:** `evaluator.md` §5.2 (LLM rubric), §5.3 (hybrid escalation), §9.2 (`/analytics/quality`).
- **Change:** LLM-as-judge tier landed at `packages/metis-core/src/metis_core/eval/llm_judge.py` (`LLMJudge`, `HybridJudge`, `LLMJudgeConfig`). Hybrid is the default for turn / workload subjects; tool_cycle / session remain heuristic-only per §5.5 / §5.6. Default escalation threshold = `0.7`. Budget-exhausted LLM calls return a `signals.budget_exhausted=True` verdict (confidence=0); HybridJudge falls back to its heuristic verdict and records `signals.escalation_skipped="budget_exhausted"`. New `/analytics/quality` endpoint (`apps/server/src/metis_server/analytics.py`) projects `eval.completed` over a window with `group_by` ∈ {model, judge_kind, rubric_id, none} and `min_confidence` filter; the `chosen_model` field joins via `route.decided` so the per-model rollup reflects the *judged* model, not the judge's.
- **Type:** additive (new classes, new endpoint, no breaking changes to existing heuristic path).
- **References to verify:**
  - `event-bus-and-trace-catalog.md §6.12` — three `eval.*` payloads unchanged; new signals (`budget_exhausted`, `escalation_skipped`, `heuristic_score`, `heuristic_confidence`) all live in the opaque `signals` dict so the catalog contract is preserved. ✓
  - `pattern-store.md §10.4` — pattern store reads `score` + `confidence` only; new signals don't affect that contract. ✓
  - `analytics-api.md` — new `/analytics/quality` endpoint follows the standard envelope and error mapping. ✓
- **Status:** verified.

### 2026-05-14 — evaluator: opt-in content penalty (refusal / empty response)

- **Spec:** `evaluator.md` §5.1 (turn rubric), §5.4 (workload rubric).
- **Change:** Added two signals to the heuristic judge: `assistant_refusal_detected` (×0.5 multiplicative penalty) and `empty_assistant_response` (×0.4). Both fire only when the caller plumbs `final_response_text` via `SubjectContext.signals_extra` — the bus subscriber path is unchanged. The workload rubric applies the same penalty (`workload_assistant_refusal_detected`, `workload_empty_assistant_response`) using the benchmark harness's existing `final_response_text` plumbing. Motivation: the prior rubric was content-blind and would score a clean refusal 1.0 if no `expect_substring_in_final_response` was configured — Run 2's "1.00 @ 0.80 on every workload" exposed the gap.
- **Type:** additive (new optional signals; existing tests unchanged; rubric version pinned at `1.0.0` because no caller in the live online path plumbs the new key yet, so re-runs of `metis evaluate --subject turn` against existing trace DBs produce identical scores).
- **References to verify:**
  - `pattern-store.md §10.4` — pattern store reads `score` only; new signals are in `signals` dict, not on the score contract. No change required. ✓
  - `benchmark.md §3.1` — `evaluate:` block schema unchanged; new fixture `intentionally-failing-task` added under `benchmarks/workloads/` as a control case. ✓
- **Status:** verified.

### 2026-05-13 — evaluator v1 implementation (heuristic tier)

- **Spec:** `evaluator.md`
- **Change:** v1 heuristic implementation lands at `packages/metis-core/src/metis_core/eval/` (`HeuristicJudge` + `Evaluator` bus subscriber + `BudgetTracker` + `metis evaluate` CLI). Subscribes to `turn.completed` / `tool.completed` / `tool.failed` / `session.ended` and emits `eval.started` / `eval.completed` / `eval.failed`. `workload.yaml.evaluate` block parsed by `scripts/benchmark.py` and fed to `Evaluator.evaluate_workload()` after each workload run — the quality score lands in the benchmark report. LLM-as-judge and hybrid escalation are deferred to a later wave per evaluator.md §5.2-5.3.
- **Type:** additive (new module, new optional `evaluate:` block on `workload.yaml`, new `metis evaluate` subcommand).
- **References to verify:**
  - `event-bus-and-trace-catalog.md §6.12` — three `eval.*` event payloads were added in Wave 4a (Task 4a-3). ✓
  - `benchmark.md §3.1` — `evaluate:` block documented. ✓ (this change)
  - `pattern-store.md §10.4` — pattern store's `update_score()` flow expects `eval.completed` carrying `subject_id` (turn_id), `score`, `confidence`. ✓ (payload matches; pattern store is the read-side, evaluator the write-side).
- **Status:** verified.

### 2026-05-13 — pattern-store v1 implementation

- **Spec:** `pattern-store.md`
- **Change:** v1 implementation lands at `packages/metis-core/src/metis_core/patterns/` (structural fingerprint + similarity + K-NN aggregation + SQLite store + bus subscriber). Routing engine slot 4 (`PATTERN_RECOMMENDATION`) consults the store when a `pattern_store_resolver` is injected; `pattern.recorded` / `pattern.matched` / `pattern.evicted` events flow through the bus. Spec body unchanged; the three event payloads were added to `events/payloads.py` in Wave 4a (Task 4a-3). `PatternConfig` gains `min_eval_confidence: float = 0.5` per pattern-store §15.4 reconciliation.
- **Type:** additive (new module, new code-path on existing routing chain).
- **References to verify:**
  - `routing-engine.md §5.5` — K-NN formula matches `aggregation.py`. ✓
  - `event-bus-and-trace-catalog.md §6.5b` — three new pattern events were added in Wave 4a. ✓
- **Status:** verified.

### 2026-05-08 — routing-engine v3.1

- **Spec:** `routing-engine.md`
- **Change:** Auxiliary event renamed (`pattern.override_accepted` → `route.overridden`); delegation phase asymmetry documented at §6 preamble.
- **Type:** breaking (event name change), additive (phase note).
- **References to verify:**
  - `event-bus-and-trace-catalog.md` §6.5b — confirms the canonical event name. ✓
  - Future: any client code rendering routing events. (No clients yet.)
- **Status:** verified.

### 2026-05-08 — event-bus v2

- **Spec:** `event-bus-and-trace-catalog.md`
- **Change:** Multiple. Added `route.overridden`, `bus.gap_detected`, `bus.subscriber_unregistered`. Removed `bus.handler_error`, `bus.overflow` (moved to logs). Pattern domain split out as §6.5b. SQLite WAL + NORMAL committed. Memory snapshotter moved off fast path. Dynamic sensitivity on opt-in.
- **Type:** breaking (event types removed/renamed).
- **References to verify:**
  - `routing-engine.md` — auxiliary event names. ✓ (handled by v3.1 above)
  - `streaming-protocol.md` — events flowing through stream. Verified: streaming spec doesn't enumerate specific event types beyond examples; safe.
- **Status:** verified.

### 2026-05-08 — routing-engine v3

- **Spec:** `routing-engine.md`
- **Change:** Many; see v3 changelog in the spec header.
- **Type:** mix.
- **References to verify:**
  - `canonical-message-format.md` §7.2 — `AdapterCapabilities` needs `supports_tools`, `supports_system_prompt`, `supports_structured_output` fields per routing v3 §4.4. **Pending: canonical-format spec needs an additive update.**
  - `event-bus-and-trace-catalog.md` — `route.decided.chain[].validation_failure` enum values updated (added `no_tool_support`, `no_system_prompt_support`, `no_structured_output_support`). ✓ in v2.
- **Status:** pending review (canonical-format AdapterCapabilities update).

### 2026-05-08 — Cross-spec reconciliation sweep (event-bus v3, streaming v2, others)

Several spec-boundary inconsistencies surfaced in cross-spec review and were resolved together:

- **Spec:** all five (`canonical-message-format` v1.1, `event-bus-and-trace-catalog` v3, `streaming-protocol` v2, `provider-adapter-contract` v1.1, `tool-dispatcher` v1.1, `server-api` v1.1, `routing-engine` v3.2).
- **Changes:**
  1. **Streaming events declared as separate transient layer**, not bus catalog events. Streaming server is no longer a bus subscriber for streaming events; it has two input channels (bus bridge for catalog events, direct from agent loop for streaming events). Domains `message`, `text`, `thinking`, `tool.use_*` reserved for streaming use only. (event-bus §4.5.1, streaming §5.1, provider-adapter §5.1)
  2. **Error class enums reconciled.** `llm.call_failed.error_class` (catalog) extended to 8 values matching `provider-adapter` §6.1. `tool.failed.error_class` (catalog) extended to 8 values matching `tool-dispatcher` §6.1. (event-bus §6.3, §6.4)
  3. **`tool.confirmation_requested` and `tool.confirmation_resolved` added to catalog** with full payloads (event-bus §6.4).
  4. **`block_dropped` confirmed as log-only**, not a catalog event. canonical-format §4.2.2, §7.3, §11.1.6 updated to match.
  5. **`AdapterCapabilities` extended** with `supports_tools`, `supports_system_prompt`, `supports_structured_output`, `supports_prompt_caching` (canonical-format §7.2), resolving the v3 pending review item.
  6. **`provider_overrides` removed from `ToolDefinition`** (canonical-format §4.4) — unused everywhere.
  7. **`RoutingDecisionRecord.mode` documented as a coarse summary** with explicit mapping to the routing chain enum (canonical-format §4.3).
  8. **Cancellation sequence split into three cases** (cancel during LLM, during tool dispatch, at seam) in streaming-protocol §6.2. routing-engine §3.4 cross-references.
  9. **`max_retries` semantics pinned** in provider-adapter §6.4: total attempts = 1 + max_retries.
  10. **`routing_failed` 503 body schema defined** in server-api §4.2.
  11. **Tool factory-vs-singleton clarified** in tool-dispatcher §3.1.
  12. **`EventFrame` cross-reference** added in event-bus §5.4.
- **Type:** mostly breaking (enum extensions, removed event types, field removals); some additive.
- **References to verify:** all five specs cross-checked in this sweep.
- **Status:** verified.

### 2026-05-08 — Post-v3 micro-sweep (streaming-protocol numbering, project-overview diagram)

Followup to the cross-spec sweep — five small but real defects caught in review:

- **Specs:** `streaming-protocol` (v2.1 conceptually; no version bump since changes are corrective), `provider-adapter-contract` (cross-ref fix), `project-overview` (architecture diagram + principle + spec list).
- **Changes:**
  1. **Streaming-protocol §5 numbering fixed.** Was `5.1 5.2 5.3 5.3 5.4 5.5`; now `5.1 5.2 5.3 5.4 5.5 5.6`. provider-adapter §5.4 and decision log cross-refs updated from `§5.5` to `§5.6`.
  2. **§10.4 worked example rewritten** to pick a specific case (tool dispatch per §6.2.2) and emit only events that case produces. Added note acknowledging the case split.
  3. **Cancellation tests in §11.1** split into 7 (LLM streaming, §6.2.1), 8 (tool dispatch, §6.2.2), 8b (seam, §6.2.3) — each asserts exactly the events that case produces.
  4. **`EventFrame` comment in §4.2** updated to "wraps any catalog or streaming event."
  5. **Filter validation §3.2 and §9.3** updated: accepted set is the union of catalog and streaming-only event types. Test 13 wording tightened.
  6. **`project-overview.md` architecture diagram updated** to show two channels (durable bus + transient streaming), the streaming server merging both, and the bus subscribers (trace store, cost accumulator, pattern) as a separate group. Core principle "Event bus as observability spine" rewritten as "Two-channel observability." Components table adds a "Streaming Server" row.
  7. **`project-overview.md` spec list refreshed** with current statuses (canonical-format v1.1, event-bus v3, streaming v2, routing v3.2, etc.). Added provider-adapter, tool-dispatcher, server-api, CHANGES.md to the list.
- **Type:** corrective (numbering, contradictions in examples, stale visual) — no contract changes.
- **References to verify:** none beyond the files updated above.
- **Status:** verified.

### 2026-05-12 — event-bus: `skill.loaded.source` added

- **Spec:** `event-bus-and-trace-catalog.md` §6.6.
- **Change:** Added `source: Literal["global", "workspace"]` to `skill.loaded` payload so traces record which directory served the skill after the workspace-overrides-global merge.
- **Type:** additive. Existing consumers ignore unknown fields; no migration required for stored events (the field defaults to None on records written before this entry, since the implementation defaulted it None on the typed struct — though all in-process emitters set it).
- **References to verify:**
  - `skill-format.md` *(planned)* — when that spec lands, document `source` alongside the other fields. Note pending below.
- **Status:** verified (event-bus spec updated in this change; implementation in `packages/metis-core/src/metis_core/events/payloads.py::SkillLoaded` + emitter in `packages/metis-core/src/metis_core/skills/tools.py::SkillLoadTool`).

---

### 2026-05-12 — analytics-api.md v1 drafted

- **Spec:** new `analytics-api.md` v1.
- **Change:** Adds a read-only `/analytics/*` HTTP namespace extending `server-api.md`. Endpoints derive metrics from the existing `events`, `messages`, and `sessions` tables — no new persistent state, no new bus events, no new write paths. Endpoints: `/cost`, `/cache_effectiveness`, `/routing`, `/reliability`, `/sessions`, `/turns/{id}`, `/savings`. Pricing semantics are hybrid: actuals honor stamped `pricing_version`; the savings counterfactual re-prices both numerator and denominator under the current `PriceTable`.
- **Type:** additive (new endpoints; no contract change to existing specs).
- **References to verify:**
  - `server-api.md` — analytics namespace lives on the same Starlette app and inherits the loopback-only / no-auth posture. No edit required; cross-reference only.
  - `event-bus-and-trace-catalog.md` — analytics queries depend on the `llm.call_completed`, `llm.call_failed`, `route.decided`, and `turn.completed` payload shapes. Any future change to those payloads must update the relevant analytics endpoint and its SQL. No edit required now.
  - `routing-engine.md §5.3.1` — known asymmetry between `cost_today_exceeds_usd` (UTC midnight) and the dashboard's "today" (local TZ). Documented in analytics-api §3.1; not aligning until evidence of confusion.
- **Status:** verified (no dependent specs need edits in this change).

---

### 2026-05-13 — benchmark.md v1 drafted

- **Spec:** new `benchmark.md` v1.
- **Change:** Defines a reproducible workload suite + measurement methodology that turns `/analytics/savings.actual_repriced_usd` / `baseline_repriced_usd` into a credible "saved X%" number — the artifact `STRATEGY.md §6.4` named as the biggest gap between architecture and proof. Specifies the workload model (per-workload YAML script + bundled fixture workspace under `benchmarks/workloads/`), the v1 suite (three workloads: fix-a-bug-small, write-a-doc-from-notes, multi-turn-refactor), reproducibility rules (pinned commit SHA, `PriceTable.version`, resolved model ids, `temperature=0`), and report shape. Adds `scripts/benchmark.py` (drives the loop) and bundled workload fixtures. Plumbs a `temperature: float | None = None` kwarg through `SessionManager.submit_turn` → `CanonicalRequest.temperature` so the determinism rule is enforceable.
- **Type:** additive (new spec; new optional kwarg on `submit_turn` defaulting to None preserves existing behavior).
- **References to verify:**
  - `analytics-api.md §4.7` — the savings response shape this spec consumes. No edit required.
  - `provider-adapter-contract.md` (planned) — when drafted, document that adapters honor `CanonicalRequest.temperature` when set. Native Anthropic/OpenAI/OpenRouter adapters already do.
  - `event-bus-and-trace-catalog.md` — the `llm.call_completed` / `turn.completed` payloads are the source rows for the benchmark's projection. No edit required.
  - `STRATEGY.md` — §6.4 resolved (pointer to this spec); §5 dated entry added.
- **Status:** verified (no dependent spec edits required in this change; STRATEGY.md updated in the same change).

---

### 2026-05-13 — context-assembler.md v1 drafted

- **Spec:** new `context-assembler.md` v1 (scope: cache-breakpoint placement only).
- **Change:** Specifies the two-segment system prompt on `CanonicalRequest` (`system_prompt` stable + new `system_prompt_volatile` for `MEMORY.md` / `USER.md`-shaped content), and where adapters place provider cache breakpoints. Anthropic adapter writes `cache_control: {"type": "ephemeral"}` on the last tool definition and on the last stable system block. OpenAI relies on automatic prefix-match caching; the adapter preserves prefix stability (`system → tools → messages` order, volatile content concatenated at the *end* of the system text). OpenRouter passes through markers but declares `supports_prompt_caching=False` because cache behavior depends on the upstream route. Validation surface is `/analytics/cache_effectiveness` ([analytics-api.md §4.2](analytics-api.md)) plus a `scripts/smoke_cache.py` 2-turn live-API test that asserts `cached_input_tokens > 0` on turn 2.
- **Type:** additive. New optional `system_prompt_volatile` and `workspace_path` fields on `CanonicalRequest` default to `None` and preserve existing behavior. The cache_control markers don't change the request's semantic meaning for any provider that doesn't recognize them.
- **References to verify:**
  - `canonical-message-format.md §7.2` — `AdapterCapabilities.supports_prompt_caching` is the routing-engine substitutability gate this spec leans on. No edit required; the field already exists.
  - `provider-adapter-contract.md` (planned) — when drafted, document that adapters supporting prompt caching write the breakpoints described in §3 of context-assembler.md.
  - `analytics-api.md §4.2` — the cache-effectiveness view is the validation surface; `hit_rate > 0` after a multi-turn Anthropic session signals the lever has landed. No edit required.
  - `KNOWN_ISSUES.md` — "No prompt-caching strategy" entry retired; replaced by this spec + implementation. ✓ in this change.
- **Status:** verified (no dependent spec edits required; KNOWN_ISSUES.md updated in the same change).

---

### 2026-05-13 — deployment-shape.md v1 + gateway.md v0 drafted

- **Specs:** new `deployment-shape.md` v1 (recommendation), new `gateway.md` v0 (skeleton, paired).
- **Change:** `deployment-shape.md` recommends the hybrid deployment (gateway first → agent upgrade) to resolve the architectural fork in [`STRATEGY.md §3`](../STRATEGY.md) and the open question in [`STRATEGY.md §6.1`](../STRATEGY.md). `gateway.md` is the v0 skeleton of the HTTP gateway surface it implies: OpenAI-shape (and Anthropic-shape) inbound endpoints, request-translation contracts that explicitly contract against the LiteLLM tool_use / cache_control / thinking-block hazards listed in [`docs/market-research/03-routing-layers.md`](../market-research/03-routing-layers.md), per-request stateless routing via the existing engine, and an enumerated non-feature list (no context shaping, no skill loading, no memory composition) that preserves the agent's upgrade-tier value proposition.
- **Type:** additive (two new specs; no contract changes to existing specs). `gateway.md §6` describes additive payload fields (`gateway_key_id`, `inbound_shape`) on existing `llm.call_completed` and `turn.completed` events — those land only when the gateway implementation does.
- **References to verify:**
  - `STRATEGY.md` §3 (resolution note added at top), §5 (new dated entry), §6.1 (retired with resolution pointer), §6.3 (narrowed: gateway-first implies deployed-instance posture). ✓ landed in this change.
  - `provider-adapter-contract.md` — `AdapterCapabilities` already carries the fields the gateway needs (`supports_tools`, `supports_prompt_caching`, etc.). No edit required.
  - `routing-engine.md` — 7-slot chain semantics in stateless gateway path documented in `gateway.md §5.1`. No edit required; cross-reference only.
  - `event-bus-and-trace-catalog.md` — additive payload fields (`gateway_key_id`, `inbound_shape`) documented in `gateway.md §6` will need to land in the payload registry when the gateway implementation does. Flagged as pending below.
  - `analytics-api.md` — adding `gateway_key` as a `group_by` dimension on `/analytics/cost` is a future additive change; not part of this entry.
- **Status:** verified (owner sign-off 2026-05-13; STRATEGY.md edits landed in the same change). Implementation-time payload-field additions to `event-bus-and-trace-catalog.md` remain pending below.

---

### 2026-05-14 — event-bus catalog v3.1: pattern.* and eval.* payloads landed

- **Spec:** `event-bus-and-trace-catalog.md` (v3 → v3.1).
- **Change:** Six new typed payloads landed in [`packages/metis-core/src/metis_core/events/payloads.py`](../../packages/metis-core/src/metis_core/events/payloads.py) and `PAYLOAD_REGISTRY` ahead of the implementation in Batch 4b (Wave 4); the catalog spec is updated to match.
  - **Pattern domain (§6.5b extended)** — `pattern.recorded`, `pattern.matched`, `pattern.evicted` per `pattern-store.md §10`. All `pseudonymous`. Phase 2.5.
  - **New `eval` domain (§6.12; closed-list extension in §4.5)** — `eval.started`, `eval.completed`, `eval.failed` per `evaluator.md §8`. All `pseudonymous` floor; `eval.completed` admits opt-in uplift to `user_controlled` per §4.4.1 when `signals.rationale_redacted` is populated.
  - **Decimal serialization.** `PatternRecorded.cost_usd_at_record` and `EvalCompleted.judge_cost_usd` use `Decimal`, serialized as strings via `msgspec.to_builtins`, matching the `Usage.cost_usd` convention from [`canonical-message-format.md §6.4`](canonical-message-format.md).
  - **Field-name divergence from pattern-store.md §10.1.** The catalog and implementation use `cost_usd_at_record` rather than the spec's `cost_usd` to disambiguate from `llm.call_completed.cost_usd` and to follow the codebase's `Decimal` convention. Field names otherwise match `pattern-store.md §10` and `evaluator.md §8/§10` as currently drafted; the Task 4a-2 reconciliation sweep may adjust further.
  - **Tests** added in [`packages/metis-core/tests/events/test_payloads.py`](../../packages/metis-core/tests/events/test_payloads.py) cover registry membership, round-trip (`to_builtins` → `convert`) for each new payload, `make_event` type↔payload binding, and the sensitivity-uplift path for `eval.completed`.
- **Type:** additive. No existing payload shape changed; no existing event removed or renamed. New typed payloads do not fire from any subscriber yet (Batch 4b lands `PatternStore` and `Evaluator` implementations + bus wiring).
- **References to verify:**
  - `pattern-store.md §10.1` — landed payload uses `cost_usd_at_record` (Decimal) rather than the drafted `cost_usd` (float). Reconcile name + type in the Wave 4 sweep; either update the spec to match the catalog or back out of the rename.
  - `evaluator.md §8` — payload fields and `Decimal` cost convention match the spec verbatim. `signals` is the opaque dict the spec specified; sensitivity uplift is wired via the existing `make_event(..., sensitivity=...)` override path. No edit required.
  - `routing-engine.md §5.5` — pattern-domain events do not change the routing chain payload; `pattern.matched` is queryable separately from `route.decided`. No edit required.
  - `analytics-api.md §4.6` — `/analytics/turns/{id}` and the planned `/analytics/quality` endpoint will join `eval.completed.subject_id` against `turn_id`. No edit required until the analytics endpoint lands.
- **Status:** pending review (the catalog edits and typed payloads have landed for both `pattern-store.md` and `evaluator.md`; pattern-store.md §10.1 field rename + Wave 4 reconciliation per the two earlier entries below remain open).

---

### 2026-05-13 — pattern-store.md v1 drafted

- **Spec:** new `pattern-store.md` v1 (specs-only; no implementation).
- **Change:** Defines the per-workspace, bounded SQLite-backed store of task fingerprints + outcomes that powers routing slot 4 (`PATTERN_RECOMMENDATION`) per [`routing-engine.md §5.5`](routing-engine.md). Specifies: (a) per-turn fingerprinting unit with a v1 structural-only feature set (file extensions, tool names, side-effect classes, token-bucket, intent regex tags) and an embedding-provider-abstract v2 hybrid path that lands data-only; (b) `<workspace>/.metis/patterns.db` storage with WAL + `synchronous=NORMAL` mirroring the trace store; (c) bounded caps (5k soft / 10k hard / 180-day age) where hard-cap **auto-evicts** rather than rejects writes — asymmetric with `memory-store.md` because pattern writes are mechanical projections with no agent-curation step; (d) K-NN retrieval with weighted Jaccard similarity + sample-size-weighted cluster aggregation, implementing routing-engine.md §5.5 scoring verbatim; (e) three new event types (`pattern.recorded`, `pattern.matched`, `pattern.evicted`) added to `event-bus-and-trace-catalog.md §6.5b`; (f) decimal cost preservation with `pricing_version_last` for future reprice; (g) workspace isolation (multi-user / cross-workspace explicitly out of scope per `STRATEGY.md §2`, §6.6). Closes `STRATEGY.md §6.6`'s "pattern store mechanics" deferral; one [`routing-engine.md §5.5`](routing-engine.md) ambiguity flagged in pattern-store §13.7 (sample-size weighting).
- **Type:** additive (new spec; three new event types to be added to event-bus catalog at Phase 2.5 implementation time; no contract changes to existing specs).
- **References to verify:**
  - `routing-engine.md §5.5` — sample-size weighting in K-cluster aggregation is unspecified there; pattern-store §8.4 picks weighted means as v1 interpretation. Needs a one-line clarification in routing-engine.md to either pin or back out. **Flagged in pattern-store §15.6.**
  - `event-bus-and-trace-catalog.md §6.5b` — three new event types (`pattern.recorded`, `pattern.matched`, `pattern.evicted`) to be added when the Phase 2.5 implementation lands. Sensitivity is `pseudonymous` for all three; parent linkages documented in pattern-store §10. **Catalog edit pending; flagged below.**
  - `evaluator.md` *(parallel draft by Agent 3B)* — pattern-store §15 enumerates the touchpoints assumed: `EvaluationResult` shape consumed by the session-ended subscriber, sync vs async score timing decision, `update_score()` API for late-arriving scores if async. **Reconcile in Wave 4 sweep.**
  - `memory-store.md` — used as the reference shape for goals/non-goals/caps/eviction structure; no edit required.
  - `analytics-api.md §4.7` — re-pricing math precedent followed; no edit required.
  - `STRATEGY.md §6.6` — "pattern store mechanics" open question resolved with pointer to this spec; §5 should record the decision in the same change. **Owner update pending.**
- **Status:** pending review (catalog additions land with Phase 2.5 implementation; routing-engine §5.5 clarification and evaluator.md reconciliation tracked below).

---

### 2026-05-13 — evaluator.md v1 drafted

- **Spec:** new `evaluator.md` v1 (specs-only; no implementation).
- **Change:** Defines the heuristic-first / hybrid-LLM-as-judge feedback loop that resolves [`STRATEGY.md §6.7`](../STRATEGY.md) — "the feedback loop that *proves* savings — without it, 'is the system actually saving money vs naive sonnet-everywhere?' stays an open question forever." Specifies: (a) four subject kinds (`turn`, `tool_cycle`, `session`, `workload`) — the workload subject subsumes the v1 limitation flagged in [`benchmark.md §2.2.2`](benchmark.md); (b) verdict shape (`EvalVerdict` `msgspec.Struct(frozen=True)` — single `score` in `[0, 1]`, `confidence` as a gate, `Decimal judge_cost_usd`, versioned `rubric_id` + `rubric_version`, opaque `signals` dict for judge-specific evidence); (c) three judge tiers (heuristic ($0), LLM-as-judge (small model by default), hybrid escalation with a single `escalation_threshold` knob); (d) bus subscriber on `turn.completed` / `tool.completed` / `tool.failed` / `session.ended` / `feedback.explicit` as non-fast-path, plus a `metis evaluate` CLI for batch re-evaluation; (e) three new event types (`eval.started`, `eval.completed`, `eval.failed`) and a new `eval` domain to be added to `event-bus-and-trace-catalog.md §4.5` / §6 at implementation time; (f) per-session ($0.10 default) and per-day ($1.00 default) `judge_cost_usd` caps + workspace kill-switch; (g) one new analytics endpoint (`/analytics/quality`) and an additive `include_eval` parameter on `/analytics/cost`; (h) re-evaluation is append-only (every verdict is a new event), enabling the dashboard's "evaluator agreement rate over time" view as a query, not a side-table; (i) workload rubric integrates with `benchmark.md` via a new optional `evaluate:` block in `workload.yaml`; (j) workspace-scoped single-user per [`STRATEGY.md §2`](../STRATEGY.md), no labeled training data, no LLM-as-judge in the critical path. `evaluator.md §15` enumerates the coordination touchpoints with the parallel `pattern-store.md` draft for the Wave 4 reconciliation.
- **Type:** additive (new spec; three new event types + new `eval` domain to be added to event-bus catalog at Phase 3 implementation time; one new analytics endpoint + additive `include_eval` param + additive `evaluations` array on `/analytics/turns/{id}`; no contract changes to existing specs).
- **References to verify:**
  - `event-bus-and-trace-catalog.md §4.5` (closed domain list) and §6 — new `eval` domain plus three event types (`eval.started`, `eval.completed`, `eval.failed`) to be added when the Phase 3 implementation lands. Sensitivity floor `pseudonymous`; `eval.completed` can uplift to `user_controlled` on opt-in `signals.rationale_redacted` per §4.4.1. **Catalog edit pending; flagged below.**
  - `routing-engine.md §5.5` — pattern-store consumption of `eval.completed.score` as `success_score`; existing math reads one number, no edit required. The confidence-gate filter convention (`pattern.min_eval_confidence`) is documented in evaluator.md §4.3 and §11.1 as a pattern-store-side configuration; cross-check against pattern-store.md.
  - `analytics-api.md §4.1` / §4.6 — additive `include_eval` query parameter on `/analytics/cost`; additive `evaluations` array on `/analytics/turns/{id}.data`. Existing consumers ignore unknown fields per the additive convention. No edit required now; document at implementation time. **Analytics spec edit pending.**
  - `benchmark.md §2.2.2` — v1 "no quality scoring of outputs" limitation closed by this spec via the workload subject. New optional `workload.yaml.evaluate:` block (rubric, expect_substring_in_final_response, llm_judge_model, weight_per_turn) is additive to the schema in `benchmark.md §3.1` — when the evaluator implementation lands, `benchmark.md §3.1` should add the `evaluate:` block to the schema and `benchmark.md §8` should add the quality column to the report. **Benchmark spec edit pending.**
  - `canonical-message-format.md §6.4` — `Decimal` cost-as-string serialization convention reused for `judge_cost_usd` in event payloads. No edit required; cross-reference only.
  - `pattern-store.md` (parallel draft by Agent 3A) — evaluator.md §15 lists the touchpoints assumed (verdicts on bus, score as one number, confidence-gate filter, `MAX(eval_id)` per subject as "latest verdict," join `chosen_model` from `route.decided` rather than embedding in verdict). **Reconcile in Wave 4 sweep.**
  - `STRATEGY.md §6.7` — "evaluator scope" open question resolved with pointer to this spec; §5 should record the decision in the same change. **Owner update pending.**
- **Status:** pending review (catalog additions land with Phase 3 implementation; benchmark.md / analytics-api.md / STRATEGY.md edits and pattern-store.md reconciliation tracked below).

---

### 2026-05-14 — Pattern-store ↔ evaluator reconciliation sweep

Wave 3 produced [`pattern-store.md`](pattern-store.md) and
[`evaluator.md`](evaluator.md) in parallel. Each spec's §15 listed
touchpoints assumed about the other surface. This sweep walks those
touchpoints and pins the reconciled contract, following the
2026-05-08 cross-spec reconciliation pattern.

- **Specs:** `pattern-store.md`, `evaluator.md`, `routing-engine.md`.
- **Changes:**
  1. **Verdict shape ownership.** `EvalVerdict` ([`evaluator.md §4.1`](evaluator.md))
     is the canonical shape; `pattern-store.md §15.1` references it
     verbatim and stops re-specifying. The pattern store consumes
     `subject_id` (the `turn_id`), `score`, `confidence`, and
     `eval_id`; everything else (`signals`, `judge_kind`, `rubric_id`)
     is opaque pass-through.
  2. **Async score timing.** Pattern-store `record()` writes outcomes
     immediately on `session.ended` with `success_score=None`; an
     `eval.completed` subscriber later calls
     `PatternStore.update_score(turn_id, score, confidence, eval_id,
     pricing_version)` to fold the verdict into the outcome
     accumulator. Idempotence is keyed by `eval_id`. Re-evaluation
     produces a new `eval_id` and rolls back the prior contribution
     before applying the new score. Documented in
     `pattern-store.md §10.4` and `§15.3`; cross-referenced from
     `evaluator.md §15`. Join key: `turn_id`.
  3. **Confidence-gate filter home.** `pattern.min_eval_confidence`
     lives in **pattern-store config** (`routing.yaml::pattern.*` block)
     alongside `cost_weight` / `min_confidence` / `min_sample_size`.
     Default `0.5` (matches the value declared in
     [`evaluator.md §4.3`](evaluator.md)). The evaluator emits all
     verdicts; the pattern store applies the gate at K-cluster
     aggregation time. Verdicts below the gate stay queryable in the
     trace store for the agreement-rate view. Documented in
     `pattern-store.md §15.4`; cross-referenced from `evaluator.md §15`.
  4. **Sample-size-weighted mean pinned in
     [`routing-engine.md §5.5`](routing-engine.md).** One-line
     clarification: `normalized_success_M = Σ(success_score_i ×
     sample_size_i) / Σ(sample_size_i)`. A neighbor row with 50
     contributing sessions weights 50× a single-shot row. This was
     the v1 interpretation `pattern-store.md §8.4` already designed
     to; pinning it in the routing spec removes the open ambiguity
     called out in `pattern-store.md §13.7`.
  5. **`MAX(eval_id)` as the latest-verdict rule.** Documented in
     `pattern-store.md §10.4` alongside the `update_score()` flow.
     Re-evaluation produces a new `eval.completed` with a fresh
     `eval_id`; pattern-store consumers join on `MAX(eval_id) per
     subject` to surface the latest verdict. Aligned with
     [`evaluator.md §4.6`](evaluator.md) and §11.1.
- **Type:** spec reconciliation (no contract breaks; clarifications +
  consolidated ownership of shared shapes).
- **References to verify:**
  - `routing-engine.md §5.5` — sample-size-weighted clarification
    landed in this change. ✓
  - `pattern-store.md §10.4`, §15 — async flow + `update_score()` +
    confidence-gate filter + `MAX(eval_id)` rule documented. ✓
  - `evaluator.md §15` — reconciliation table reflects pinned
    outcomes; open coordination items closed. ✓
  - `STRATEGY.md §5`, §6.6, §6.7 — retired entries for "pattern
    store mechanics" and "evaluator scope" with pointers to the
    drafted specs. ✓
- **Status:** verified. Phase 2.5 / Phase 3 implementation-time
  catalog additions to `event-bus-and-trace-catalog.md §4.5` / §6
  remain pending (tracked below under the original pattern-store and
  evaluator entries).

---

### 2026-05-13 — skill-format.md v1 drafted (retrospective)

- **Spec:** new `skill-format.md` v1 (specs-only; documents the existing implementation in [`packages/metis-core/src/metis_core/skills/`](../../packages/metis-core/src/metis_core/skills/)).
- **Change:** Captures retrospectively what the skills loader / store / tools already do: agentskills.io-conformant six-field frontmatter (`name`, `description`, `license`, `compatibility`, `metadata`, `allowed-tools`); `SKILL.md` directory layout with `scripts/` / `references/` / `assets/` siblings; two on-disk roots (`~/.metis/skills/` global, `<workspace>/.metis/skills/` workspace) merged workspace-overrides-global; three-stage progressive disclosure (discovery index in stable system prompt → `skill_load` activation → execution); two tools (`skill_search` / `skill_load`) both `SideEffects.READ`; `skill.loaded` event emission semantics including the `source` field added 2026-05-12. Surfaces seven implementation observations (name-validation error message wording; metadata scalar coercion; unbounded discovery index; no reload-on-change; hidden dirs not excluded; symlinks followed; `allowed-tools` parsed-not-enforced) in §11 for triage, not fixed in this change. Follows the `memory-store.md` retro-spec pattern.
- **Type:** additive (new spec; no code or contract changes). Resolves the pending cross-reference for `skill.loaded.source` (added 2026-05-12) by documenting the field alongside the rest of the payload.
- **References to verify:**
  - `event-bus-and-trace-catalog.md §6.6` — `skill.loaded` payload (including `source`) documented in skill-format.md §9.1. No edit required; cross-reference only. ✓
  - `tool-dispatcher.md` *(planned)* — `ToolContext.skills` field carries the per-session `SkillStore`; skill-format.md §8 documents the two tools' registration / dispatch semantics. No edit required.
  - `context-assembler.md §2-§5` — discovery index injected into the *stable* system prompt segment ahead of the cache breakpoint; skill-format.md §7.1 cross-references. No edit required.
  - `project-overview.md` — spec list refresh: `skill-format.md` line at §"Specs and documents" should move from "Planned" to "Drafted (v1, 2026-05-13)". Defer to next doc-refresh pass.
  - `STRATEGY.md` — "skills" cost lever (one of three in §2) is now spec-backed; no narrative change required.
- **Status:** verified. The "Pending cross-references" entry for `skill-format.md` (`skill.loaded.source` field, 2026-05-12) is resolved by skill-format.md §9.1 and §10.6 and removed below.

---

### 2026-05-12 — Implementation milestone + doc refresh

Not a spec change; an alignment pass between the docs and what's actually been built.

- **Files touched:** `README.md`, `docs/project-overview.md`, `docs/specs/project-overview.md`, new `docs/STRATEGY.md`, new `docs/KNOWN_ISSUES.md`, new `docs/specs/memory-store.md`.
- **What landed in code since the last doc refresh:** three provider adapters (Anthropic / OpenAI / OpenRouter), streaming end-to-end (adapter → session manager → CLI + WebSocket), Textual TUI, HTTP/WebSocket server (`metis serve`, loopback-only), SQLite session/message persistence, bounded memory (MEMORY.md / USER.md + 3 tools), skills store + `load_skill` tool, configured-rule parser (yaml policy + predicate set + loader; integration into routing chain pending), cross-provider conformance suite. Test count went from 272 → 592.
- **Spec-list status changes:** `memory-store.md` moved from "planned" to "drafted (v1)." `skill-format.md` and `pattern-store.md` remain planned.
- **New strategy artifacts:** `docs/STRATEGY.md` captures the cost-optimization thesis, buyer ≠ user framing, three cost levers (skills / context / model selection), and the open replacement-agent-vs-gateway question. `docs/KNOWN_ISSUES.md` tracks carryover review findings (spec promises not yet honored by code).
- **References to verify:** none in specs proper.
- **Status:** doc-only update.

---

## Pending cross-references

When you land a spec change, move it from "pending review" up here for visibility, then back to "verified" when the dependent spec is updated.

- `pattern-store.md` v1 (2026-05-13) — three new event types (`pattern.recorded`, `pattern.matched`, `pattern.evicted`) to land in `event-bus-and-trace-catalog.md §6.5b` when Phase 2.5 implementation does. Routing-engine §5.5 sample-size-weighting clarification and evaluator.md reconciliation **verified 2026-05-14** (see "Pattern-store ↔ evaluator reconciliation sweep" above).
- `evaluator.md` v1 (2026-05-13) — new `eval` domain + three event types (`eval.started`, `eval.completed`, `eval.failed`) to land in `event-bus-and-trace-catalog.md §4.5` / §6 when Phase 3 implementation does. New `/analytics/quality` endpoint + additive `include_eval` param on `/analytics/cost` + additive `evaluations` array on `/analytics/turns/{id}` to land in `analytics-api.md` at implementation time. Optional `evaluate:` block in `workload.yaml` schema to land in `benchmark.md §3.1` plus quality column in `§8` report. STRATEGY.md §6.7 resolution + §5 dated decision entry and pattern-store reconciliation **verified 2026-05-14**.
- `gateway.md` v0 (2026-05-13) — STRATEGY.md edits landed on owner sign-off; the additive `gateway_key_id` / `inbound_shape` payload fields in `event-bus-and-trace-catalog.md` §6.3 / §6.6 land when the gateway implementation does.
