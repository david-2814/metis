# Multi-User / Team Identity & Rollups Specification

**Status:** v1 — §3, §4 (gateway identity stamping), §5 (analytics rollups), and §5.1 + §6.1 + §6.3 (key-scoped quota caps with hard breakers, soft alerts at 80%/95%, and the `team_budget_remaining_lt` routing predicate) shipped. User/team-scoped caps and `users.json` / `teams.json` storage land in a follow-on (see §8.1).
**Last updated:** 2026-05-14

> Adds a per-user / per-team identity layer on top of the existing per-(gateway-key) cost attribution shipped in [`gateway.md`](gateway.md) §3.3 / §6. Extends [`analytics-api.md`](analytics-api.md) with `group_by=user` and `group_by=team`, a new `/analytics/by_team` rollup, and `?user=` / `?team=` filter parameters on existing endpoints. Adds routing-rule predicates and gateway-level circuit breakers for per-user and per-team budget caps. Closes the "multi-user from day one is real" requirement called out in [`STRATEGY.md §2`](../STRATEGY.md) and the "team-level rollups" follow-on listed in [`gateway.md §11`](gateway.md).
>
> This spec depends on:
>
> - [`canonical-message-format.md`](canonical-message-format.md) — `Message`, `MessageMetadata.usage`, the persistence schema.
> - [`event-bus-and-trace-catalog.md`](event-bus-and-trace-catalog.md) — the `llm.call_completed` / `turn.completed` payloads gain two additive fields (`user_id`, `team_id`).
> - [`gateway.md`](gateway.md) — keystore shape, key-issuance UX, per-request stamping mechanics, error-class taxonomy.
> - [`routing-engine.md`](routing-engine.md) — the rule predicate set; new predicates land in §5.3.2.
> - [`analytics-api.md`](analytics-api.md) — the rollup surface this spec extends.
>
> This spec **does not** depend on a particular deployment shape ([`STRATEGY.md §6.3`](../STRATEGY.md) is still open). The identity model is intentionally portable: local-first laptop-served, self-hosted-in-VPC, and SaaS deployments all consume the same struct shape. Storage location varies (local JSON files vs server-side SQLite) but the wire and event contracts do not.

---

## 1. Purpose

Today's gateway attributes cost per **gateway key**. Every key maps to exactly one workspace ([`gateway.md §3.3`](gateway.md)), the keystore records `key_id` / `name` / `workspace_path` / `secret_hash`, and every `llm.call_completed` is stamped with `gateway_key_id` ([`gateway.md §6`](gateway.md)). That is enough for "show me what each integration spent" but it does not answer the two questions the buyer actually has:

1. **Who** spent it? — the budget owner needs per-developer rollups for headcount planning, fairness, and incident triage ("Alice's keys are at 4× the team's median; something looks off").
2. **What's the team's cap?** — Engineering leaders run their AI budget the same way they run their cloud bill: per-team monthly caps, hard breakers, soft alerts at 80% / 95%.

The current keystore answers question (1) only if every developer has exactly one key, which is operationally fragile (a dev rotates a key, the new key shows up as a new identity in the rollups). It does not answer question (2) at all — `daily_cap_usd` is stored per-key and not enforced ([`gateway.md §10.5`](gateway.md)).

This spec adds two stable identity dimensions — `User` (a developer) and `Team` (a budget-owning group of users) — and threads them through the trace stamping, the analytics rollups, and the routing-rule predicate set. The gateway key remains the auth artifact; users and teams are properties **of** the key, not separate auth surfaces.

---

## 2. Goals and non-goals

### 2.1 Goals

1. **Stable per-developer identity across key rotations.** A developer who rotates their gateway key should keep the same `user_id` — the new key inherits the user binding. The cost rollup follows the user, not the key.
2. **Per-team budget caps as a first-class primitive.** Both soft (route to cheaper model) and hard (reject the request) caps land in v1.
3. **Backward compatibility with gateway v1.** Existing keys (issued without `--user` / `--team`) keep working; their traffic rolls up under `user_id: null` / `team_id: null` in the analytics surface — the same "ungrouped" convention `gateway.md §6` already uses for agent-loop traffic that has no `gateway_key_id`.
4. **Deployment-shape neutral.** The struct, the events, and the HTTP surface are the same in local-first and SaaS deployments. Only the **storage** of `users.json` / `teams.json` differs (local FS vs server-side DB) — that's an operational concern, not a contract one.
5. **No external identity provider in v1.** Metis-issued user/team records, persisted to disk alongside the keystore. SSO / OIDC / SCIM / SAML are all explicitly out of scope ([§8.1](#8-out-of-scope-for-v1)). The [`STRATEGY.md §6.2`](../STRATEGY.md) startup-CTO default — small teams who don't want to wire an IdP — is what v1 targets.
6. **Privacy by default.** Plaintext PII (emails, real names) lives in the user record only. The trace store carries the stable `user_id` and never the email. This keeps a single trace-store dump from being a PII spill.

### 2.2 Non-goals

1. **No identity provider integration in v1.** No SSO, no OIDC, no SAML, no SCIM, no LDAP. Out of scope until a buyer asks for it; see §8.1.
2. **No per-user authentication.** The gateway key authenticates the request; the key's `user_id` field declares *whose request it is*. v1 trusts the operator to issue keys responsibly. A future surface may add per-user login (e.g. a TUI/dashboard with multi-user auth) — that's a separate spec.
3. **No multi-org / multi-tenant isolation.** A Metis deployment is single-tenant in v1; a single keystore, a single trace store, a single dashboard ([`gateway.md §10.1`](gateway.md)). Multi-org tenancy is a Phase 4 design (separate stores, role-based filtering, dashboard partitioning).
4. **No user-visible per-user dashboards in v1.** The dashboard is the **buyer's** view; per-user views ("show Alice her own spend") are a Phase 3 follow-on after the buyer surface is paying-customer-ready.
5. **No quota enforcement on the agent path.** v1 enforcement lives at the gateway boundary and in the routing engine's rule predicates. The agent-loop path (CLI / TUI / `metis serve`) is single-user and inherits the operating dev's budget posture; it does not bind to a `user_id` in v1 ([§3.4](#34-the-agent-path)).

---

## 3. Identity model

### 3.1 The three dimensions

| Dimension     | What it is                                                            | Stable across      | Identifier         |
|---------------|-----------------------------------------------------------------------|--------------------|--------------------|
| **User**      | An individual developer (or service account)                          | key rotations      | `usr_<ulid>`       |
| **Team**      | A budget-owning group of users sharing a quota                        | membership changes | `team_<ulid>`      |
| **Workspace** | A project directory (already exists; [`gateway.md §3.3`](gateway.md)) | — (path-keyed)     | absolute path      |

Every `GatewayKey` ([`gateway.md §3.3`](gateway.md)) gains two optional fields: `user_id` and `team_id`. The (User, Team, Workspace) tuple is the **principal** that owns each request; the gateway key is the *auth artifact* that asserts the principal.

A user **may** belong to a team; teams are not required (a solo developer's key has `user_id` set and `team_id` null). A user may belong to multiple teams only through multiple keys — one key carries one (user, team, workspace) tuple. This keeps the routing-rule predicate set tractable (no "which team is this turn billing to" ambiguity) and makes the analytics joins cheap.

### 3.2 Canonical structs

The three records, in [`msgspec`](https://jcristharif.com/msgspec/) idiom matching the existing keystore in [`apps/gateway/src/metis_gateway/auth.py`](../../apps/gateway/src/metis_gateway/auth.py). All persisted to disk; `frozen=True` everywhere.

```python
class User(msgspec.Struct, frozen=True):
    user_id: str                # "usr_<ulid>"; stable across key rotations
    display_name: str           # human label; shown in dashboards
    email: str | None = None    # PII; lives in users.json only
    email_sha256: str | None = None  # derived; for join-by-email if email is set
    created_at: str             # ISO 8601 UTC
    disabled: bool = False      # soft delete; keys with this user stop authing


class Team(msgspec.Struct, frozen=True):
    team_id: str                # "team_<ulid>"
    name: str                   # human label; unique within the deployment
    daily_cap_usd: Decimal | None = None      # optional hard cap
    monthly_cap_usd: Decimal | None = None    # optional hard cap
    created_at: str
    disabled: bool = False


class Principal(msgspec.Struct, frozen=True):
    """The (user, team, workspace) tuple a request bills to.

    Resolved from the gateway key at request entry; passed to the routing
    chain and stamped onto trace events. Never persisted directly — it is
    a per-request projection of the keystore.
    """
    user_id: str | None         # None for v1 keys issued without --user
    team_id: str | None         # None for v1 keys issued without --team
    workspace_path: str         # from the gateway key
    gateway_key_id: str | None  # the auth artifact; None for agent-loop traffic
```

`Principal` is the request-scoped projection that the harness builds once per inbound request. It is what the routing chain and trace stamping consume — not the raw `GatewayKey`. This keeps the contract clean if a future deployment shape derives the principal from a non-gateway-key auth path (e.g. an authenticated dashboard request).

### 3.3 What `email_sha256` is for

Plaintext email lives **only** in `users.json` (file mode `0o600`, sibling to the keystore). The hash exists for two narrow purposes:

1. **Join keys when a buyer imports a user list.** "Add these 12 emails as users" → the bootstrap deduplicates on `email_sha256` so re-running the import doesn't create duplicate users.
2. **Privacy-preserving correlation in a future SSO bridge.** If a Phase 4 SAML/OIDC integration lands, the IdP-supplied `email_claim` can be hashed and matched against existing users without storing the plaintext on the IdP path.

The trace store **never** carries plaintext email and never carries `email_sha256`. It carries the stable `user_id` only. This is the privacy-by-default posture from [§2.1.6](#21-goals).

### 3.4 The agent path

The single-developer agent surfaces — `metis chat`, `metis tui`, `metis serve` — do not bind to a `user_id` in v1. Their traces carry `user_id: null` / `team_id: null`, the same convention `gateway.md §6` already uses for `gateway_key_id: null`. This is intentional:

- The agent path is operationally single-user (`metis serve` binds loopback-only per [`server-api.md §3.1`](server-api.md)); the operating developer **is** the user.
- Adding `--user` / `--team` flags to `metis chat` would force the local-first dev to opt into an identity layer they don't need.
- The analytics surface already handles the null case cleanly (see [`analytics-api.md §4.8`](analytics-api.md)) — the "agent-loop traffic" bucket gets a friendly label in the SPA.

A future "team agent surface" (multi-dev `metis serve` behind an authenticated dashboard) would re-introduce per-request `user_id` binding via the auth context. That's downstream of the [`STRATEGY.md §6.3`](../STRATEGY.md) local-first-vs-SaaS resolution; v1 leaves the door open without specifying a contract.

---

## 4. Gateway integration

### 4.1 Backward compatibility

The shipped keystore record ([`gateway.md §3.3`](gateway.md)) is:

```
key_id, secret_hash, name, workspace_path, allowed_models, daily_cap_usd, created_at
```

v1 of this spec adds two **optional** fields:

```
+ user_id: str | None
+ team_id: str | None
```

The keystore loader treats missing fields as `None`. Every existing key continues to auth; its traffic rolls up under `user_id: null` / `team_id: null` in the analytics surface. Zero migration is required.

### 4.2 Issuance UX

`metis gateway issue-key` gains two optional flags:

```
metis gateway issue-key \
  --name "alice-claude-code" \
  --workspace /Users/alice/repos/foo \
  --user alice \
  --team eng \
  [--allow-models ...] [--daily-cap-usd ...]
```

Semantics:

| Flag        | What happens if the value is unknown                                       |
|-------------|----------------------------------------------------------------------------|
| `--user`    | If `alice` is not a known user, the CLI prompts: `Create user 'alice'? [y/N]`. On `y`, a `User` record is created with `display_name="alice"`, `email=None`, `user_id="usr_<ulid>"`. On `n`, the issuance is aborted. |
| `--team`    | Same flow: prompt-on-unknown, create-on-confirm. `daily_cap_usd` and `monthly_cap_usd` default to `None` (no cap); set them via `metis gateway team set-cap`. |

Two new top-level subcommands ship alongside `issue-key`:

- `metis gateway user add --name "Alice Liu" --email "alice@org" [--alias alice]` — creates a user record. The `--alias` form is the short identifier used in `--user` flags; defaults to a slug of the name.
- `metis gateway user list` / `metis gateway user disable <alias>` — display and soft-delete.
- `metis gateway team add --name "eng" [--daily-cap-usd 50] [--monthly-cap-usd 1200]`.
- `metis gateway team set-cap <name> --daily-cap-usd 75` — adjust caps; takes effect on the next request.
- `metis gateway team list`.

The disk shape, sibling to `keys.json`:

```
~/.metis/gateway/
├── keys.json
├── users.json   (mode 0o600)
└── teams.json   (mode 0o600)
```

All three are append-and-update JSON, the same shape as `keys.json`. They are *not* shared with the trace store; trace events join to them by `user_id` / `team_id` at analytics-query time.

### 4.3 Authentication and principal resolution

The auth path in [`gateway.md §3.3`](gateway.md) is unchanged: the inbound `Authorization: Bearer gw_…` is hashed, the SHA-256 hex digest is looked up in the keystore, 401 on miss. After resolution, the harness builds the request-scoped `Principal`:

```python
key = keystore.resolve(token_hash)             # GatewayKey
user = users.get(key.user_id) if key.user_id else None
team = teams.get(key.team_id) if key.team_id else None

if user is not None and user.disabled:
    raise AuthError("user disabled")
if team is not None and team.disabled:
    raise AuthError("team disabled")

principal = Principal(
    user_id=user.user_id if user else None,
    team_id=team.team_id if team else None,
    workspace_path=key.workspace_path,
    gateway_key_id=key.key_id,
)
```

Disabled users/teams produce **401 `authentication_error`** rather than the request-shape `403 permission_error` — the *credential* is no longer valid, not a permission denial within an active key. This matches OAuth-style "revoked subject" semantics and lets clients distinguish "fix the cap" from "your access was rotated."

### 4.4 Stamping on trace events

The two additive payload fields land on the same two event types as the gateway's existing `gateway_key_id` / `inbound_shape` ([`gateway.md §6`](gateway.md)):

- `llm.call_completed.user_id: str | None`
- `llm.call_completed.team_id: str | None`
- `turn.completed.user_id: str | None`
- `turn.completed.team_id: str | None`

Both are typed extensions to `LLMCallCompleted` and `TurnCompleted` in [`events/payloads.py`](../../packages/metis-core/src/metis_core/events/payloads.py). Existing consumers ignore unknown fields per the catalog discipline in [`event-bus-and-trace-catalog.md`](event-bus-and-trace-catalog.md).

Agent-loop traffic (CLI / TUI / `metis serve`) emits both fields as `null` — same shape as `gateway_key_id: null` today.

---

## 5. Analytics surface

This section extends [`analytics-api.md`](analytics-api.md). Every change is additive; no existing endpoint shape changes.

### 5.1 New `group_by` values on `/analytics/cost`

The allowed set in `analytics-api.md §4.1` gains two values:

| `group_by` | Key columns                | Shape | Order            | Notes |
|------------|----------------------------|-------|------------------|-------|
| `user`     | `user_id` (nullable)       | array | `cost_usd DESC`  | Null bucket = agent-loop traffic + v1 keys without `--user`. |
| `team`     | `team_id` (nullable)       | array | `cost_usd DESC`  | Same null convention. |

The `_COST_GROUP_BY_ALLOWED` whitelist in [`analytics/store.py`](../../packages/metis-core/src/metis_core/analytics/store.py) extends to include the two new values; SQL projection `json_extract(payload_json, '$.user_id')` / `$.team_id'` matches the stamping in §4.4. Same SPA defense-in-depth as `gateway_key` (parameter is whitelist-mapped to a literal GROUP BY column).

### 5.2 New endpoint: `GET /analytics/by_team`

The companion to the shipped `/analytics/by_key` ([`analytics-api.md §4.8`](analytics-api.md)). Per-team cost + token + call-count rollup with a per-user sub-array.

**Query parameters:**

| Parameter   | Type              | Required | Default |
|-------------|-------------------|----------|---------|
| `from`,`to` | ISO 8601 UTC      | no       | last 7d |
| `team`      | team_id or team name | no    | (all teams) |

The `team` filter accepts either the stable `team_id` (`team_<ulid>`) or the human `name`; the handler resolves names against `teams.json` before issuing the SQL. Unknown name → 400 `unknown_team`. Names matching `^[A-Za-z0-9_-]{1,200}$`; same guard as the existing `gateway_key` filter.

**Response:**

```json
{
  "window": {"start": "...", "end": "..."},
  "current_pricing_version": "2026-05-08",
  "data": [
    {
      "team_id": "team_01HZ...",
      "team_name": "eng",
      "cost_usd": 12.4231,
      "input_tokens": 4_910_220,
      "output_tokens": 81_502,
      "cached_input_tokens": 2_500_000,
      "cache_creation_input_tokens": 410_220,
      "call_count": 412,
      "daily_cap_usd": 50.0,
      "monthly_cap_usd": 1200.0,
      "by_user": [
        {"user_id": "usr_01HZ...", "display_name": "alice",
         "cost_usd": 8.1010, "call_count": 281},
        {"user_id": "usr_01HZ...", "display_name": "bob",
         "cost_usd": 4.3221, "call_count": 131}
      ]
    },
    {
      "team_id": null,
      "team_name": null,
      "cost_usd": 1.0512,
      "input_tokens": 51_220,
      "output_tokens": 1_842,
      "cached_input_tokens": 0,
      "cache_creation_input_tokens": 0,
      "call_count": 30,
      "daily_cap_usd": null,
      "monthly_cap_usd": null,
      "by_user": [
        {"user_id": null, "display_name": null,
         "cost_usd": 1.0512, "call_count": 30}
      ]
    }
  ]
}
```

Rows sorted by `cost_usd` DESC. The `by_user` sub-array is also `cost_usd` DESC. `daily_cap_usd` and `monthly_cap_usd` are echoed from `teams.json` so the SPA can render the bar against the cap without a second round-trip.

### 5.3 Optional filter parameters on existing endpoints

The five existing time-windowed endpoints (`/analytics/cost`, `/analytics/cache_effectiveness`, `/analytics/routing`, `/analytics/reliability`, `/analytics/savings`) gain two optional filter parameters:

| Parameter | Type                       | Effect                                                               |
|-----------|----------------------------|----------------------------------------------------------------------|
| `user`    | `usr_<ulid>` or alias      | Filters to events whose `payload.user_id` matches. |
| `team`    | `team_<ulid>` or name      | Filters to events whose `payload.team_id` matches. |

Both filters resolve names → ids at the handler boundary against `users.json` / `teams.json`; unknown → `400 unknown_user` / `400 unknown_team`. The SQL filter is a parameterized predicate `AND json_extract(payload_json, '$.user_id') = ?`; defense-in-depth regex on the inbound value matches the existing `gateway_key` filter (`^[A-Za-z0-9_-]{1,200}$`).

The combination of `user` + `team` (both set) is an **AND** filter, not OR — the request narrows to events stamped with *both* (which is the dominant case: a user belongs to one team per key, so this should usually be a no-op refinement).

### 5.4 The savings counterfactual under a team filter

[`analytics-api.md §4.7`](analytics-api.md)'s savings endpoint already does the right thing with a filter — it sums over a narrower window. The team-filtered counterfactual is the natural extension: "what would this team have spent if every turn ran on the baseline model?" The denominator semantic doesn't change.

One nuance worth recording: a team's rollup over a window where some keys are not yet user/team-tagged (mixed-mode deployment during rollout) under-counts the team's spend, because the un-tagged calls fall into the `null` bucket. The SPA should render team rollups with a "tagged coverage: 87% of calls" badge when the window contains both tagged and untagged traffic from the same keystore — surfaced via a new boolean `partial_coverage` flag on the team-filtered responses. The flag fires when, within the requested window, any `gateway_key_id` present in the result set has at least one row with both `user_id is null` and at least one row with `user_id is not null`, indicating a key was retagged mid-window.

---

## 6. Routing-engine integration

### 6.1 New rule predicates

[`routing-engine.md §5.3.2`](routing-engine.md) gains three predicates parallel to the existing `cost_today_exceeds_usd`:

| Predicate                          | Snapshot point   | Semantics                                                  |
|------------------------------------|------------------|------------------------------------------------------------|
| `user_cost_today_exceeds_usd`      | turn start       | Today's (UTC) cumulative cost across all keys for this `user_id` exceeds the threshold. |
| `team_cost_today_exceeds_usd`      | turn start       | Same for `team_id`. |
| `team_cost_month_exceeds_usd`      | turn start       | This UTC month's cumulative cost for `team_id` exceeds the threshold. |

These are evaluated against the trace store at turn-start time, the same way `cost_today_exceeds_usd` is today ([`routing-engine.md §5.4`](routing-engine.md)). They are *snapshot* predicates — they read a value, they do not subscribe to updates mid-turn. Cost accumulates across the configured window; restart-resilient because the trace store is the source of truth.

When the agent path is the caller, `user_id` / `team_id` are `null` and these predicates evaluate to `false` (the trace store has no rows matching `payload.user_id = null` filtered against a non-null binding). This is correct: an agent-path turn has no user/team to cap. The matching rule simply doesn't fire.

### 6.2 Example rule using the new predicates

```yaml
schema_version: 1
rules:
  - name: "team-eng-daily-soft-cap"
    when:
      team_cost_today_exceeds_usd: 50
    use: anthropic:claude-haiku-4-5
    reason: "team daily soft cap hit; routing to haiku"

  - name: "team-eng-monthly-soft-cap"
    when:
      team_cost_month_exceeds_usd: 1000
    use: anthropic:claude-haiku-4-5
    reason: "team monthly soft cap hit; routing to haiku"
```

This is the **soft cap** mechanism: route to a cheaper model when the cap fires, instead of rejecting the request. It matches the cost-circuit-breaker pattern that already exists for the single-user case.

The **hard cap** mechanism is at the gateway boundary (§6.3) and is independent of the routing engine: hard caps reject the request entirely with a 429.

### 6.3 Gateway-level hard caps (circuit breakers)

`Team.daily_cap_usd` and `Team.monthly_cap_usd` are **hard caps** — when the team's running total exceeds the cap, the gateway short-circuits the request before routing or adapter invocation.

The flow, before the routing chain runs:

```python
if team is not None and team.daily_cap_usd is not None:
    today_usd = analytics.team_cost_today(team.team_id)
    if today_usd >= team.daily_cap_usd:
        raise QuotaExceeded(scope="team_daily",
                            limit_usd=team.daily_cap_usd,
                            current_usd=today_usd)
if team is not None and team.monthly_cap_usd is not None:
    month_usd = analytics.team_cost_month(team.team_id)
    if month_usd >= team.monthly_cap_usd:
        raise QuotaExceeded(scope="team_monthly", ...)
```

Same shape for the existing `GatewayKey.daily_cap_usd` (was reserved-but-not-enforced per [`gateway.md §10.5`](gateway.md); this spec activates it under the same circuit-breaker mechanism).

Error mapping:

| Scope                       | OpenAI-shape outbound                 | Anthropic-shape outbound |
|-----------------------------|---------------------------------------|--------------------------|
| `key_daily`, `user_daily`, `team_daily`, `team_monthly` | 429 `rate_limit_exceeded` / `quota_exceeded` | 429 `rate_limit_error` |

The error envelope carries the scope (`scope`, `limit_usd`, `current_usd`) in the body so the buyer's tooling can render the right banner ("team daily cap hit at $50.00 of $50.00 — resets at 00:00 UTC").

A new audit-relevant event fires on hard-cap rejection: `gateway.quota_exceeded` (see §7).

### 6.4 Where this lives in the routing chain

The hard cap is **not** a routing slot — it short-circuits before the chain runs. The soft caps (§6.2) live in the `configured_rules` slot ([`routing-engine.md §4.1`](routing-engine.md), slot 3). No new slot is added.

This is the right layering because:

- A rule firing means "route to a cheaper model and continue." That's the chain's job.
- A hard cap firing means "refuse the request." That's not a routing decision; it's a precondition failure. Putting it in the chain would force the chain to model "no eligible model" as a distinct verdict, which it already does via `RoutingFailedError` — but conflating budget rejection with capability rejection would muddy the trace.

The `route.decided` event is still emitted on hard-cap rejection (the chain ran zero slots and produced `winner_index = -1`, reason = `quota_exceeded`), so the analytics surface still has a row to render. This is consistent with how routing already handles its own hard failures ([`analytics-api.md §4.3`](analytics-api.md), `hard_failures` bucket).

---

## 7. Audit + compliance posture

### 7.1 What audit means

[`STRATEGY.md §2`](../STRATEGY.md) names **audit and compliance** as a B2B requirement: "Trace events are the raw material; aggregation/retention/redaction policies for buyer-facing artifacts are not yet designed." This spec is not the full audit spec — but it pins what the *identity-relevant* audit events look like, so the eventual audit-export surface has them to project from.

The principle: **the trace store is the source of truth.** Audit log = filtered projection of trace events, not a parallel write path. This matches [`analytics-api.md §2.1.5`](analytics-api.md)'s rule that catalog-sourced data is the only source.

### 7.2 New audit-relevant events

Three new event types land on the catalog ([`event-bus-and-trace-catalog.md §6`](event-bus-and-trace-catalog.md)), all under the `gateway.` namespace to match the existing `gateway_key_id` stamping:

| Event type                  | Sensitivity     | When emitted                                                    |
|-----------------------------|-----------------|-----------------------------------------------------------------|
| `gateway.key_issued`        | `pseudonymous`  | `metis gateway issue-key` succeeds. Payload: `key_id`, `name`, `workspace_path`, `user_id`, `team_id`, `allowed_models`, `daily_cap_usd`, `issued_at`. |
| `gateway.key_revoked`       | `pseudonymous`  | A key is revoked via `metis gateway revoke-key`. Payload: `key_id`, `revoked_at`, `reason`. |
| `gateway.quota_exceeded`    | `pseudonymous`  | A hard cap (per-key, per-user, or per-team) blocks a request. Payload: `gateway_key_id`, `user_id`, `team_id`, `scope`, `limit_usd`, `current_usd`, `inbound_shape`. |

Schema definition idiom matches [`events/payloads.py`](../../packages/metis-core/src/metis_core/events/payloads.py):

```python
class GatewayKeyIssued(msgspec.Struct, frozen=True):
    key_id: str
    name: str
    workspace_path: str
    user_id: str | None
    team_id: str | None
    allowed_models: tuple[str, ...] | None
    daily_cap_usd: Decimal | None
    issued_at: str

class GatewayKeyRevoked(msgspec.Struct, frozen=True):
    key_id: str
    revoked_at: str
    reason: str

class GatewayQuotaExceeded(msgspec.Struct, frozen=True):
    gateway_key_id: str
    user_id: str | None
    team_id: str | None
    scope: Literal["key_daily", "user_daily", "team_daily", "team_monthly"]
    limit_usd: Decimal
    current_usd: Decimal
    inbound_shape: Literal["openai", "anthropic"]
```

Both `gateway.key_issued` and `gateway.key_revoked` are `pseudonymous`-sensitive (carry stable ids, no plaintext PII). The trace store's existing retention policy applies unchanged.

### 7.3 The audit export surface (deferred, sketched)

This spec defines the **events**; a future audit-export spec defines the **export shape**. The minimum audit log a buyer would want is a CSV/JSON-Lines dump of:

- All `gateway.key_issued` / `gateway.key_revoked` in a date range.
- All `gateway.quota_exceeded` in a date range.
- A `who-paid-what` rollup: `(date, user_id, team_id, key_id, model, cost_usd)` aggregated from `llm.call_completed`.

That shape is straightforward to derive from the trace store via the same `json_extract` queries the analytics surface already uses. The format question (CSV vs JSON-Lines vs SOC2-aligned attestation report) is downstream of a buyer asking; left as future work.

### 7.4 SOC2-relevant questions to surface

These are not in scope to **answer** in this spec — they're surfaced for the owner so a future audit spec can resolve them:

1. **Retention period.** SOC2 typically expects 1 year of audit logs. The trace store has no automatic retention policy today; do we add one when audit ships, or rely on operator-side backups?
2. **Tamper-evidence.** SOC2 wants append-only logs. SQLite WAL is append-only at the SQL layer but not cryptographically signed; do we add hash-chained event ids, or treat operator-controlled access as sufficient?
3. **Plaintext PII handling.** `users.json` carries plaintext email; an audit export that includes `email_sha256` only is privacy-preserving but breaks "who is `usr_01HZ...`?" without a join back to `users.json`. The trade-off: trace-export contains stable ids only; audit-export *may* opt-in to including display names by joining to `users.json` at export time.
4. **Right-to-delete.** GDPR / CCPA "delete my user" affects three surfaces: `users.json` (mark `disabled=true` + null out email), all keys for that user (revoke), trace store (cannot mutate per the append-only invariant — closest equivalent is purging via window deletion). v1 does *not* commit to a right-to-delete pathway; documented gap. **Partially resolved 2026-05-15:** the trace-store half of this lands via [`analytics-api.md §4.10`](analytics-api.md)'s `POST /analytics/user/{user_id}/forget` (pseudonymize-in-place via [`redaction.md`](redaction.md)'s `Redactor`). The `users.json` / key-revoke half remains future work — gated on the §8.1 follow-on.

---

## 8. Out of scope for v1

These are deliberately not in this spec; they're the upgrade reasons that distinguish v1 from a full enterprise identity layer.

1. **SSO / OIDC / SAML / SCIM / LDAP.** v1 ships Metis-issued user records in `users.json`. A future "Identity Provider Bridge" spec defines the pluggable interface (likely an `IdentityProvider` Protocol with `authenticate(token) -> User` / `enumerate_users() -> Iterable[User]`). Until a buyer asks, the startup-CTO default ([`STRATEGY.md §6.2`](../STRATEGY.md)) is "manual user list works fine."
2. **Per-user dashboard authentication.** v1 dashboards inherit loopback-only ([`analytics-api.md §2.1.4`](analytics-api.md)). Multi-user authenticated dashboard access is downstream of the local-first-vs-SaaS resolution in [`STRATEGY.md §6.3`](../STRATEGY.md).
3. **Role-based access control (RBAC).** No "viewers" vs "admins" within a deployment. The operator who can run `metis gateway issue-key` can also `revoke-key`, set caps, etc. Splitting roles is RBAC territory — a Phase 4 design.
4. **Multi-org tenancy.** v1 assumes one deployment per organization, matching [`gateway.md §10.1`](gateway.md). Multi-org (separate keystores per org, trace-store partitioning, dashboard filtering) is a separate design.
5. **Multi-workspace per key.** A single key still maps to exactly one workspace ([`gateway.md §3.3`](gateway.md)). A future v2 may allow `--workspace A --workspace B` on one key; the analytics surface would need a workspace-dimension addition. Default for v1 stays at one-workspace-per-key per §10.1.
6. **Per-user audit retention overrides.** All trace data shares one retention policy in v1. Per-user GDPR-style retention is a Phase 4 concern.
7. **Quota enforcement on `metis chat` / `metis serve`.** The agent path is loopback-only and operator-trusted; no user-binding, no budget enforcement. The gateway is where caps land.

### 8.1 Why no IdP in v1

The startup-CTO buyer ([`STRATEGY.md §6.2`](../STRATEGY.md)) has 10–50 devs and is using whatever auth their cloud provider gives them — they do not have a Keycloak / Okta tenant they're eager to wire Metis into. Forcing one upfront is friction with no value for the v1 buyer. By the time a buyer needs SSO, they need SSO for **everything** (CI, dashboards, gateway, audit), so a single IdP-bridge spec covering all of those is more useful than a half-built one in v1.

The chosen default — Metis-issued user records — is also the cheapest thing to **discard** if v2 deletes it in favor of an IdP bridge. The 5-line `User` struct and a single JSON file are a trivial migration vs the operational sunk cost of an early SSO integration.

---

## 9. Invariants

1. **Existing keys keep working.** A pre-v1 gateway key (no `user_id`, no `team_id`) authenticates exactly as before. Its traffic rolls up under the null bucket in user/team analytics.
2. **`user_id` is stable across key rotations.** Revoking a key and issuing a new one for the same user under `users.json` preserves the rollup history.
3. **Trace stamping is irreversible.** `user_id` / `team_id` are stamped from the request-time keystore lookup. Re-tagging a key (changing its `user_id` after the fact) does not retroactively rewrite past trace events — past events keep their stamp. This is the same append-only discipline `gateway_key_id` already uses.
4. **The trace store never carries plaintext email.** Only the stable `user_id` is in events. Email lives in `users.json` only.
5. **Hard caps are enforced at the gateway boundary, not in the routing chain.** Soft caps live in the rule predicates. Both are independent; both can fire on the same request (soft cap was breached but didn't fire because the rule was disabled; hard cap kicks in).
6. **Disabled users and teams produce 401 `authentication_error`, not 403.** Revocation of the binding is a credential change, not a permission denial.
7. **All identity records are local-FS-backed in v1.** No external state, no network calls during auth. This is what makes the local-first deployment shape work; SaaS deployments swap the storage backend without changing the auth contract.

---

## 10. Decision log

| Date       | Decision                                                                | Rationale                                                                                                          |
|------------|-------------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------|
| 2026-05-14 | Identity dimensions are User + Team + Workspace; the gateway key carries the binding | Keeps the auth artifact (key) and the identity (user/team) decoupled — a user can rotate keys without losing rollup continuity. |
| 2026-05-14 | One workspace per key, one (user, team) per key in v1                   | Matches the shipped [`gateway.md §3.3`](gateway.md) contract; avoids "which team is this turn for" ambiguity in routing predicates. |
| 2026-05-14 | `user_id` / `team_id` are additive fields on `LLMCallCompleted` / `TurnCompleted` | Mirrors the existing `gateway_key_id` / `inbound_shape` additive pattern; existing consumers ignore unknown fields. |
| 2026-05-14 | Plaintext email lives in `users.json` only; trace events carry stable `user_id` | Privacy by default. A trace-store dump is not a PII spill.                                                          |
| 2026-05-14 | Metis-issued user records in v1; no SSO / OIDC / SAML / SCIM            | Startup-CTO default ([`STRATEGY.md §6.2`](../STRATEGY.md)) does not need IdP integration; a full IdP-bridge spec is cheaper to write once than to evolve a v1 partial. |
| 2026-05-14 | Hard caps short-circuit before routing; soft caps live in `configured_rules` | Hard cap is a precondition failure (refuse), not a routing decision (route cheaper). Distinct shapes deserve distinct surfaces. |
| 2026-05-14 | New `/analytics/by_team` endpoint mirrors the shipped `/analytics/by_key` | Same shape, same query parameter style, same null-bucket convention. Minimizes SPA learning curve.                  |
| 2026-05-14 | Disabled users/teams produce 401, not 403                               | A disabled subject is a credential revocation, not an in-scope permission denial. OAuth-style semantics.            |
| 2026-05-14 | The agent path (`metis chat` / `metis serve`) is null-binding in v1     | Loopback-only single-user surface; adding `--user` would force the local-first dev to opt into an identity layer they don't need. |
| 2026-05-14 | `partial_coverage` flag on team-filtered responses                      | During rollout some keys are tagged and others are not; surfacing the mixed-mode case beats silently under-counting. |
| 2026-05-14 | Three new `gateway.*` audit-relevant events: `key_issued`, `key_revoked`, `quota_exceeded` | Audit surface = filtered projection of the trace store, not a parallel log. Catalog-sourced per [`analytics-api.md §2.1.5`](analytics-api.md). |

---

## 11. Open questions

These are **live**. The owner closes them when evidence shows up; agents working in the repo should surface them, not pick.

1. **Identity provider for v2.** When the buyer asks for SSO, which protocol is the first integration — OIDC (broadest, simplest) or SAML (enterprise-incumbent, more painful)? Default lean: OIDC, on the assumption that the next buyer cohort up from startup-CTO is a Series-B-shaped company already running Auth0 / Clerk / WorkOS.
2. **Email handling on the gateway side.** Should the gateway *prompt for* an email when `--user` creates a user, or treat email as optional metadata that the buyer fills in later via `metis gateway user set-email`? v1 leans optional — a key issuance flow with a forced email prompt is friction; the email is only load-bearing if SSO ships.
3. **Multi-workspace per key.** A single Claude Code instance often spans repos. v1 forces a separate key per repo. Is that OK, or does v2 need a key with a workspace allowlist? Wait for evidence (a buyer asking, or an internal user complaint).
4. **Per-user dashboards.** When does a developer want to see *their own* cost (vs the buyer-facing rollup)? Probably never the headline view — but a "self-serve" page would be a low-cost addition once authenticated dashboards exist. Couples to [`STRATEGY.md §6.3`](../STRATEGY.md).
5. **Right-to-delete pathway.** GDPR / CCPA. The append-only trace store is a problem; the closest equivalent is "purge events older than N days for this user_id." Surface honestly in a future audit-export spec; do not commit to a contract in v1. **Partial close 2026-05-15:** [`analytics-api.md §4.10`](analytics-api.md) now ships `GET /analytics/user/{user_id}/export` (portability) and `POST /analytics/user/{user_id}/forget` (pseudonymize-in-place, idempotent). The forget half delegates pseudonymization to [`redaction.md`](redaction.md)'s `Redactor` protocol; this satisfies "the closest equivalent" while preserving the append-only invariant.
6. **Quota reset windows.** Daily cap = UTC midnight (matches `cost_today_exceeds_usd`). Monthly cap = UTC first-of-month. Should the operator be able to configure these (per-team timezone, per-team reset day)? Probably yes, but not in v1 — the structural decisions go in first, configurability follows usage signal.
7. **Should `team_id` flow through the agent path?** A future "team agent surface" (multi-dev `metis serve` behind auth) would need it. v1 leaves the field `null` on the agent path; a future spec defines how authenticated dashboard / agent sessions resolve the binding.
8. **Soft-cap predicate cost.** Each new predicate (`user_cost_today_exceeds_usd`, etc.) runs a `SELECT SUM(...)` query at turn start, filtered to the day. At single-user scale this is trivial; at 1M events / team / day it needs an index or a rollup table. Wait for evidence the predicate is slow before optimizing.

---

## 12. Testing strategy

### 12.1 Required tests (deferred to implementation, sketched here)

1. **Existing v1 key still authenticates.** A key issued without `--user` / `--team` resolves to `Principal(user_id=None, team_id=None, ...)`; its traffic appears in `/analytics/cost?group_by=user` under the `null` bucket.
2. **`metis gateway user add` then `issue-key --user`.** End-to-end: a user record is created, a key bound to it is issued, a request through that key stamps `user_id` on the trace event.
3. **Disabled user produces 401.** A key bound to a `disabled=true` user fails auth.
4. **`/analytics/by_team` rollup correctness.** Seed three keys across two users in one team and one user in another team; verify per-team / per-user totals and ordering.
5. **`partial_coverage` flag fires.** Within a window: one key issued without `--user`, then re-issued with `--user`; assert the team filter response surfaces `partial_coverage: true`.
6. **Team daily hard cap blocks the request.** Set `Team.daily_cap_usd = 0.001`, run one request that costs $0.01, run a second request: second one returns 429 with `scope: "team_daily"`.
7. **Routing soft cap routes to cheaper model.** Rule with `team_cost_today_exceeds_usd: 1.00`; team has already spent $1.50 today; turn routes to the rule's `use:` target rather than the workspace/global default.
8. **Soft + hard cap interaction.** Both fire on the same request: hard cap wins (request is rejected before routing runs).
9. **`gateway.key_issued` event fires on issuance.** `metis gateway issue-key --user alice --team eng` produces exactly one `gateway.key_issued` trace event with the right fields.
10. **`gateway.quota_exceeded` event fires on hard-cap rejection.** A request blocked by hard cap produces exactly one `gateway.quota_exceeded` event; the corresponding `route.decided` event has `winner_index = -1, reason="quota_exceeded"`.
11. **Stamping is irreversible.** Issue a key bound to user A, run a request, re-bind the key to user B (or delete user A), run another request: the first event still carries `user_id=A`.
12. **Trace store carries no plaintext email.** Sweep the events table for any string matching the `@`-bearing email in `users.json`; assert zero matches.
13. **Whitelist enforcement on `?user` / `?team` filters.** A request with `?user=DROP TABLE` returns 400 `invalid_user`; never reaches SQL.

### 12.2 Property tests

- **Monotonicity of team rollups.** Extending the time window forward never decreases a team's `cost_usd`.
- **Sum-of-users equals team total.** For any team in `/analytics/by_team`, `sum(by_user[].cost_usd) == team.cost_usd` to within the spec's decimal precision ([`analytics-api.md §5.1`](analytics-api.md)).

---

## 13. References

- [`canonical-message-format.md`](canonical-message-format.md) — `Message`, `MessageMetadata`, persistence schema.
- [`event-bus-and-trace-catalog.md`](event-bus-and-trace-catalog.md) — additive `user_id` / `team_id` fields on `LLMCallCompleted` / `TurnCompleted`; three new `gateway.*` event types.
- [`gateway.md`](gateway.md) — the keystore shape, `Principal` request-time resolution, hard-cap circuit-breaker placement.
- [`routing-engine.md`](routing-engine.md) — new predicates in §5.3.2; the soft-cap pattern matches the existing `cost_today_exceeds_usd` shape.
- [`analytics-api.md`](analytics-api.md) — base surface this spec extends; `/analytics/by_team` mirrors the shipped `/analytics/by_key`.
- [`deployment-shape.md`](deployment-shape.md) — the identity model is intentionally deployment-shape neutral.
- [`../STRATEGY.md §2`](../STRATEGY.md) — buyer ≠ user framing; multi-user-from-day-one requirement.
- [`../STRATEGY.md §6.2`](../STRATEGY.md) — startup-CTO default for the v1 buyer profile.
- [`../KNOWN_ISSUES.md`](../KNOWN_ISSUES.md) — open infra/identity items.
