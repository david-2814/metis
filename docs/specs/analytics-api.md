# Analytics API Specification

**Status:** Draft v1
**Last updated:** 2026-05-12

> **v1 revisions (same day):**
> - **First pass:** corrected cache hit-rate denominator to include cache writes (§4.2); hard-failure `route.decided` events bucketed under `hard_failures` (§4.3); sessions list sort and display reference one source field (§4.5); turn drill-down handles in-flight turns and includes `session_id` in SQL (§4.6); SQL injection guard via whitelist mapping documented for `group_by` / `order` (§4.1, §4.5); dropped speculative window-size cap (§6); composite `current_pricing_version` strings flagged as opaque (§5).
> - **Second pass:** added `invalid_order` error code symmetric with `invalid_group_by` (§6); `wins_by_policy` always emits all seven policy slots so the SPA doesn't need to know the enum (§4.3); `/reliability` scope clarified with cross-reference to `/routing.hard_failures` (§4.4); `group_by=none` aggregate behavior made explicit (§4.1); SPA UX rule for partial `savings_pct` when `rows_missing_from_price_table > 0` (§4.7).
> - **Third pass:** row schemas tabulated for all six `group_by` values (§4.1); Decimal-through-aggregate / JSON-number-at-boundary convention pinned (§5.1); `actual_stamped_usd` clarified as unconditional across missing-model rows (§4.7); negative-savings semantics called out as a valid SPA case (§4.7).

> Extends [server-api.md](server-api.md) with a read-only `/analytics/*` namespace that powers the dashboard SPA. All metrics are derived from the existing trace store (`events` table) and session store (`sessions`, `messages` tables) — no new persistent state, no new bus events, no new write paths.

---

## 1. Purpose

The dashboard surfaces the LLM-usage metrics Metis already captures: cost over time, tokens by model, routing-decision breakdown, prompt-cache effectiveness, reliability, and a savings counterfactual vs. a baseline model. This spec defines the HTTP shape that backs those views.

Two design constraints shape the surface:

1. **Audiences differ; data does not.** The dashboard exposes a Cost view (buyer-leaning) and an Activity view (dev-leaning) selected via a frontend toggle. Both views consume the same endpoints; the SPA decides what to render.
2. **Analytics are a projection.** The bus catalog ([event-bus-and-trace-catalog.md §6](event-bus-and-trace-catalog.md)) and the canonical message store ([canonical-message-format.md §9.1](canonical-message-format.md)) are the source of truth. Analytics queries are projections — no rollup tables, no parallel state.

This spec depends on:

- `canonical-message-format.md` for `Message`, `MessageMetadata`, `Usage`, `RoutingDecisionRecord`, and the SQLite schema in §9.1.
- `event-bus-and-trace-catalog.md` for the `llm.call_completed`, `llm.call_failed`, `route.decided`, `turn.completed`, `tool.completed`, and `memory.updated` payloads.
- `server-api.md` for the HTTP surface conventions (Starlette, JSON, loopback-only).

---

## 2. Goals and non-goals

### 2.1 Goals

1. **Read-only and derived.** Every endpoint is a `GET`. No bus events, no DB writes, no cache invalidation surface. Re-running the same request returns the same answer (modulo new events landing).
2. **Sub-second at single-user scale.** Target: p95 < 500ms with ≤10K `llm.call_completed` rows in the table. Existing indexes on `(type, timestamp_us)` and `(session_id, id)` carry every query.
3. **Two pricing modes, both honest.** Actuals honor the stamped `pricing_version` on each `llm.call_completed` event (matches the bill). The savings counterfactual re-prices both numerator and denominator under the current `PriceTable` (apples-to-apples).
4. **Loopback-only inherits.** No new auth surface. Multi-user / remote dashboards are downstream of the unresolved fork in [`STRATEGY.md §3`](../STRATEGY.md).
5. **Catalog-sourced.** If a metric requires data not in the catalog, the answer is to extend the catalog (deliberate spec change), not to add a side-table.

### 2.2 Non-goals

1. **No rollup / materialized tables.** Adding one is a Phase 3 decision triggered by evidence of lag, not a Phase 2 default.
2. **No multi-tenant or multi-user.** Per-user / per-team rollups deferred per [`STRATEGY.md §2`](../STRATEGY.md).
3. **No custom date ranges in v1.** The SPA exposes today / 7d / 30d / all. The API accepts arbitrary `from`/`to` already — only the UI is constrained.
4. **No pattern / skill / delegation views.** The underlying data isn't stable enough yet (Phase 2.5 / 4 work).
5. **No live updates over WebSocket.** REST polling on view changes is sufficient; the existing `/sessions/{id}/stream` already carries the live signal for clients that want it.
6. **No redaction layer.** Single-user local-first; the user is the data owner. A "redact for screenshot" mode is a future surface change, not a v1 endpoint.

---

## 3. Common request shape

### 3.1 Time window

Every endpoint that aggregates over time accepts:

| Parameter | Type            | Required | Default                       |
|-----------|-----------------|----------|-------------------------------|
| `from`    | ISO 8601 UTC    | no       | `now - 7d`                    |
| `to`      | ISO 8601 UTC    | no       | `now`                         |

The API speaks UTC end-to-end. The SPA is responsible for computing UTC bounds for "today / 7d / 30d / all" from the user's local timezone. This keeps the server pure and means flipping to a custom date-picker UI later is a frontend-only change.

> **Predicate mismatch.** The `cost_today_exceeds_usd` routing predicate ([routing-engine.md §5.3.1](routing-engine.md)) uses UTC midnight, not local. A user looking at "today on the dashboard" (local TZ) may therefore see a slightly different number than the breaker sees. Documented as a known asymmetry; not worth aligning until evidence it confuses people.

### 3.2 Response envelope

Every endpoint returns:

```json
{
  "window": {"start": "2026-05-05T00:00:00Z", "end": "2026-05-12T00:00:00Z"},
  "current_pricing_version": "2026-05-08",
  "data": ...
}
```

- `window` echoes the resolved bounds (so the SPA can label "Today (PST)" with full provenance).
- `current_pricing_version` is the version of the `PriceTable` active right now. Useful when a view mixes stamped and re-priced values: the SPA can show "actuals at write-time prices; counterfactual at <version>."
- `data` is endpoint-specific.

---

## 4. Endpoints

All endpoints live under `/analytics/` on the same Starlette app as the rest of the server. All are loopback-only.

### 4.1 `GET /analytics/cost`

Aggregated cost and token counts. Combines what would otherwise be a separate `/tokens` endpoint — same source data, same SQL shape.

**Query parameters:**

| Parameter   | Type                                                | Required | Default |
|-------------|-----------------------------------------------------|----------|---------|
| `from`,`to` | ISO 8601 UTC                                        | no       | last 7d |
| `group_by`  | `model` \| `provider` \| `session` \| `day` \| `hour` \| `gateway_key` \| `none` | no | `model` |

**Source:** `events` table, `type = 'llm.call_completed'`. Cost is the stamped value (matches invoice).

**SQL (group_by=model):**

```sql
SELECT
  json_extract(payload_json, '$.model')                          AS model,
  json_extract(payload_json, '$.provider')                       AS provider,
  SUM(json_extract(payload_json, '$.cost_usd'))                  AS cost_usd,
  SUM(json_extract(payload_json, '$.input_tokens'))              AS input_tokens,
  SUM(json_extract(payload_json, '$.output_tokens'))             AS output_tokens,
  SUM(json_extract(payload_json, '$.cached_input_tokens'))       AS cached_input_tokens,
  SUM(json_extract(payload_json, '$.cache_creation_input_tokens')) AS cache_creation_input_tokens,
  AVG(json_extract(payload_json, '$.latency_ms'))                AS avg_latency_ms,
  COUNT(*)                                                       AS call_count
FROM events
WHERE type = 'llm.call_completed'
  AND timestamp_us >= ? AND timestamp_us < ?
GROUP BY model, provider
ORDER BY cost_usd DESC;
```

For `group_by=day` and `group_by=hour`, use SQLite's `date(timestamp_us/1000000, 'unixepoch')` and `strftime('%Y-%m-%dT%H', timestamp_us/1000000, 'unixepoch')` respectively (UTC buckets). For these bucketed views the result is `ORDER BY <bucket> ASC` so the SPA can render a time series without re-sorting. For `group_by=session`, group on the **events table's `session_id` column** (covered by the `idx_events_session_id` index), not on `json_extract(payload_json, '$.session_id')` — `LLMCallCompleted` payloads carry no `session_id` field.

The `group_by` parameter is **whitelist-mapped** to a literal GROUP BY clause in the handler. The raw request string is never interpolated.

For `group_by=none`, the GROUP BY clause is omitted entirely and the response carries a single-row aggregate over the full window — useful for the headline "total spend this period" tile.

**Row schemas per `group_by` value:**

Every row carries the same numeric columns (`cost_usd`, `input_tokens`, `output_tokens`, `cached_input_tokens`, `cache_creation_input_tokens`, `avg_latency_ms`, `call_count`). What differs is the grouping key, summarized below:

| `group_by`    | Key columns                            | Shape   | Order                       |
|---------------|----------------------------------------|---------|-----------------------------|
| `model`       | `model`, `provider`                    | array   | `cost_usd DESC`             |
| `provider`    | `provider`                             | array   | `cost_usd DESC`             |
| `session`     | `session_id` (from events column)      | array   | `cost_usd DESC`             |
| `day`         | `bucket` (UTC date, `YYYY-MM-DD`)      | array   | `bucket ASC` (time series)  |
| `hour`        | `bucket` (UTC hour, `YYYY-MM-DDTHH`)   | array   | `bucket ASC` (time series)  |
| `gateway_key` | `gateway_key_id` (nullable; in-process agent traffic rolls up under `null`) | array | `cost_usd DESC` |
| `none`        | (no key)                               | object  | n/a (single aggregate)      |

`day`/`hour` are single-dimension time buckets — they do not also split by model. A future `group_by=day,model` (multi-key) is non-breaking but out of scope for v1. `none` returns a single JSON object as `data` rather than an array — the response envelope is otherwise unchanged.

Example (`group_by=day`):

```json
{
  "window": {...},
  "current_pricing_version": "...",
  "data": [
    {"bucket": "2026-05-10", "cost_usd": 0.42, "input_tokens": 18402, ...},
    {"bucket": "2026-05-11", "cost_usd": 0.31, "input_tokens": 12808, ...}
  ]
}
```

Example (`group_by=none`):

```json
{
  "window": {...},
  "current_pricing_version": "...",
  "data": {
    "cost_usd": 1.2347, "input_tokens": 410230, "output_tokens": 18402,
    "cached_input_tokens": 0, "cache_creation_input_tokens": 0,
    "avg_latency_ms": 1820, "call_count": 48
  }
}
```

**Response (`group_by=model`):**

```json
{
  "window": {"start": "...", "end": "..."},
  "current_pricing_version": "2026-05-08",
  "data": [
    {
      "model": "anthropic:claude-sonnet-4-6",
      "provider": "anthropic",
      "cost_usd": 1.2347,
      "input_tokens": 410230,
      "output_tokens": 18402,
      "cached_input_tokens": 0,
      "cache_creation_input_tokens": 0,
      "avg_latency_ms": 1820,
      "call_count": 48
    }
  ]
}
```

### 4.2 `GET /analytics/cache_effectiveness`

Cache-read share, cache-creation share, and hit rate per model.

**Source:** `llm.call_completed` events.

**SQL:**

```sql
SELECT
  json_extract(payload_json, '$.model')                          AS model,
  SUM(json_extract(payload_json, '$.input_tokens'))              AS uncached_input_tokens,
  SUM(json_extract(payload_json, '$.cached_input_tokens'))       AS cached_input_tokens,
  SUM(json_extract(payload_json, '$.cache_creation_input_tokens')) AS cache_creation_tokens,
  COUNT(*)                                                       AS call_count
FROM events
WHERE type = 'llm.call_completed'
  AND timestamp_us >= ? AND timestamp_us < ?
GROUP BY model;
```

Computed in the handler:

```
total_input = uncached_input_tokens + cached_input_tokens + cache_creation_tokens
hit_rate          = cached_input_tokens   / total_input
cache_write_share = cache_creation_tokens / total_input
```

Returns `null` for both ratios when `total_input` is zero (no input tokens recorded).

**Why include `cache_creation_tokens` in the denominator.** Cache writes are billed at roughly 1.25× the standard input rate, so they aren't free. Pretending they aren't there inflates `hit_rate` in any session with non-trivial cache turnover (e.g. a session whose system prompt has just changed and is rebuilding the cache). The honest formulation is: of *all* input tokens, what fraction were served from cache? The two ratios sum to ≤ 1 with the remainder being uncached input. `cache_write_share` is broken out separately so operators can see "am I rebuilding the cache too often?" as a distinct signal.

**Note:** at the time of writing, no adapter emits `cache_control` markers ([KNOWN_ISSUES.md](../KNOWN_ISSUES.md) — "No prompt-caching strategy"). All hit rates will read 0 until that lands. The view is therefore both diagnostic and a forcing function.

**Response:**

```json
{
  "window": {...},
  "current_pricing_version": "...",
  "data": [
    {
      "model": "anthropic:claude-sonnet-4-6",
      "uncached_input_tokens": 410230,
      "cached_input_tokens": 0,
      "cache_creation_tokens": 0,
      "hit_rate": 0.0,
      "cache_write_share": 0.0,
      "call_count": 48
    }
  ]
}
```

### 4.3 `GET /analytics/routing`

Which routing-chain slot is winning, and what's being rejected.

**Source:** `events` table, `type = 'route.decided'`.

**Implementation note:** the routing chain lives inside `payload_json.chain` as a JSON array. SQLite's `json_each()` can iterate it in SQL, but Python-side traversal is clearer and at single-user scale doesn't cost meaningfully. The handler fetches the rows in the window and walks each chain in Python:

```sql
SELECT payload_json
FROM events
WHERE type = 'route.decided'
  AND timestamp_us >= ? AND timestamp_us < ?;
```

Then aggregates three counters:

- **Wins by policy slot** — for each event where `winner_index >= 0 AND winner_index < len(chain)`, pick `chain[winner_index].policy`; group.
- **Hard failures** — events with `winner_index == -1` (or otherwise out of range) count toward `hard_failures`, not toward any policy's win count. Per [routing-engine.md §7.2](routing-engine.md) and the engine's behavior in [routing/engine.py](../../packages/metis-core/src/metis_core/routing/engine.py), every turn emits exactly one `route.decided` *including on hard failure*; the failure rows have `winner_index = -1` and the response would otherwise miscount or index-error.
- **Rejections by reason** — for each `chain` entry with `verdict='rejected'`, group by `policy` and `validation_failure`. Hard-failure events contribute their rejected chain entries here too — they're the most interesting source of rejection data.

This is the one endpoint where aggregation deliberately doesn't push to SQL. The trade-off (per §2.1.2): with 10K turns the handler walks ~70K chain entries on the all-time view, which is well under the budget; if it becomes a bottleneck, push to SQL with `json_each()`.

**`wins_by_policy` always emits all seven policy slots** (per [routing-engine.md §4.1](routing-engine.md)), even when count is zero. This keeps the SPA from needing to know the enum to render tiles in a stable order. `rejections` and `wins_by_model` are sparse — only present rows appear.

**Response:**

```json
{
  "window": {...},
  "current_pricing_version": "...",
  "data": {
    "wins_by_policy": [
      {"policy": "per_message_override", "count": 12},
      {"policy": "manual_sticky", "count": 84},
      {"policy": "rule", "count": 0},
      {"policy": "pattern", "count": 0},
      {"policy": "delegate_request", "count": 0},
      {"policy": "workspace_default", "count": 0},
      {"policy": "global_default", "count": 312}
    ],
    "hard_failures": 2,
    "rejections": [
      {"policy": "manual_sticky", "validation_failure": "exceeds_context_window", "count": 3}
    ],
    "wins_by_model": [
      {"chosen_model": "anthropic:claude-sonnet-4-6", "count": 312}
    ]
  }
}
```

### 4.4 `GET /analytics/reliability`

Error class breakdown and latency percentiles per model. **Scope:** errors that reached the adapter and came back classified — `llm.call_failed` events. Turns that never reached the LLM because routing exhausted the chain (hard failures) appear in `/analytics/routing.hard_failures`, not here. An operator seeing "reliability all green but everything is broken" should check the routing endpoint.

**Source:** `llm.call_failed` and `llm.call_completed` events.

**SQL for errors:**

```sql
SELECT
  json_extract(payload_json, '$.model')        AS model,
  json_extract(payload_json, '$.provider')     AS provider,
  json_extract(payload_json, '$.error_class')  AS error_class,
  COUNT(*)                                     AS count
FROM events
WHERE type = 'llm.call_failed'
  AND timestamp_us >= ? AND timestamp_us < ?
GROUP BY model, provider, error_class;
```

Latency percentiles: SQLite has no native `PERCENTILE_*`. The handler fetches `latency_ms` ordered ascending per model and computes p50 / p95 in Python:

```sql
SELECT
  json_extract(payload_json, '$.model')      AS model,
  json_extract(payload_json, '$.latency_ms') AS latency_ms
FROM events
WHERE type = 'llm.call_completed'
  AND timestamp_us >= ? AND timestamp_us < ?
ORDER BY model, latency_ms;
```

For 10K rows this is ~10K integers in memory per request — trivial.

**Response:**

```json
{
  "window": {...},
  "current_pricing_version": "...",
  "data": {
    "errors_by_class": [
      {"model": "anthropic:claude-opus-4-7", "provider": "anthropic",
       "error_class": "rate_limit", "count": 4}
    ],
    "latency_ms_by_model": [
      {"model": "anthropic:claude-sonnet-4-6",
       "p50": 1620, "p95": 4830, "sample_size": 312}
    ]
  }
}
```

### 4.5 `GET /analytics/sessions`

Session list with rollups. The Cost view sorts by spend; the Activity view sorts by recency.

**Query parameters:**

| Parameter | Type                                  | Required | Default    |
|-----------|---------------------------------------|----------|------------|
| `limit`   | int                                   | no       | 25         |
| `order`   | `cost` \| `recency`                   | no       | `recency`  |

**Source:** `sessions` table only. The displayed `updated_at` is bumped by `update_session()` after every turn ([sessions/sqlite_store.py](../../packages/metis-core/src/metis_core/sessions/sqlite_store.py)), which is the same surface used to sort by recency — display and ordering reference one field.

**SQL:**

```sql
SELECT
  s.id, s.workspace_path, s.active_model,
  s.cost_so_far_usd, s.turn_count,
  s.created_at, s.updated_at
FROM sessions s
ORDER BY <order_column> DESC
LIMIT ?;
```

`<order_column>` is **whitelist-mapped** from the `order` query parameter (`cost` → `s.cost_so_far_usd`, `recency` → `s.updated_at`). The handler does not interpolate the raw parameter string into SQL.

**Response field rename:** the SQL column `cost_so_far_usd` is exposed as `cost_usd` in the response body, matching the naming used elsewhere on the surface. The `updated_at` field is the same value the SQL sorts by — it tracks the last post-turn write, which is within microseconds of the last message timestamp in practice.

```json
{
  "window": {"start": null, "end": null},
  "current_pricing_version": "...",
  "data": [
    {
      "id": "sess_01HZ...",
      "workspace_path": "/Users/.../my-project",
      "active_model": "anthropic:claude-sonnet-4-6",
      "cost_usd": 0.42,
      "turn_count": 17,
      "created_at": "2026-05-12T10:14:23Z",
      "updated_at": "2026-05-12T11:02:08Z"
    }
  ]
}
```

`window` is `null/null` because session list is not time-windowed in v1. Future iteration may add filtering.

### 4.6 `GET /analytics/turns/{turn_id}`

Drill-down into a single turn. Returns the full event timeline plus the message bodies.

**Source:** `events` table by `turn_id`, `messages` table by `session_id` filtered to the turn's window.

**SQL:**

```sql
SELECT id, timestamp_us, session_id, type, actor, payload_json, parent_event_id
FROM events
WHERE turn_id = ?
ORDER BY id;
```

The handler reads `session_id` from the first event row (all rows in a turn share one session); 404 if no rows match. Plus a follow-up query for the canonical messages spanned by the turn:

```sql
SELECT id, role, content_json, metadata_json, created_at
FROM messages
WHERE session_id = ?
  AND created_at BETWEEN ? AND ?
ORDER BY created_at, id;
```

**Bounds:**

- Lower bound: timestamp of the `turn.started` event (always present).
- Upper bound: timestamp of `turn.completed` or `turn.cancelled` if present; otherwise `now()` at request time. The response carries `in_flight: true` in the latter case so the SPA knows the message list may grow on re-poll.

This lets the SPA render an in-flight turn (matching the live `/sessions/{id}/stream` view); a turn with no terminator event is not a 404.

**Response:**

```json
{
  "window": {...},
  "current_pricing_version": "...",
  "data": {
    "turn_id": "01HZ...",
    "session_id": "sess_01HZ...",
    "in_flight": false,
    "events": [
      {"id": "...", "timestamp": "...", "type": "turn.started",
       "actor": "user", "payload": {...}}
    ],
    "messages": [
      {"id": "...", "role": "user", "content": [...],
       "metadata": {...}, "created_at": "..."}
    ]
  }
}
```

This endpoint is the link target from every "top-N expensive turns" / "most-recent activity" tile in the SPA.

### 4.7 `GET /analytics/savings`

The counterfactual: what would this window have cost if every turn had run on a single baseline model? Both numerator and denominator are re-priced under the current `PriceTable` to keep the comparison meaningful when prices change.

**Query parameters:**

| Parameter    | Type                           | Required | Default                          |
|--------------|--------------------------------|----------|----------------------------------|
| `baseline`   | canonical model id             | no       | `anthropic:claude-sonnet-4-6`    |
| `from`,`to`  | ISO 8601 UTC                   | no       | last 7d                          |

**Algorithm:**

1. Fetch all `llm.call_completed` events in the window. Each carries `model`, `input_tokens`, `output_tokens`, `cached_input_tokens`, `cache_creation_input_tokens`.
2. **Actual (re-priced):** for each row, look up the row's `model` in the current `PriceTable` and compute cost. Sum.
3. **Baseline:** for each row, compute cost using the same token counts but the baseline model's rates from the current `PriceTable`. Sum.
4. Savings = baseline - actual; savings_pct = savings / baseline if baseline > 0 else 0.

**Errors:**

- `400` if `baseline` isn't in the current `PriceTable`.
- A row whose recorded `model` is missing from the current price table is included in baseline but excluded from `actual_repriced_usd`, with `rows_missing_from_price_table` incremented so the SPA can flag the discrepancy. This is a deliberate convention to keep `baseline_repriced_usd - actual_repriced_usd` honest in the dominant case (every model in the actual mix is currently priced); the alternative — silently dropping rows from both sides — would let pricing-table gaps inflate apparent savings. `actual_stamped_usd` covers the "what I actually paid" view independently for missing-model rows.

**Response:**

```json
{
  "window": {...},
  "current_pricing_version": "2026-05-08",
  "data": {
    "baseline_model": "anthropic:claude-sonnet-4-6",
    "actual_repriced_usd": 1.42,
    "baseline_repriced_usd": 4.18,
    "savings_usd": 2.76,
    "savings_pct": 0.66,
    "actual_stamped_usd": 1.39,
    "rows_total": 412,
    "rows_missing_from_price_table": 0
  }
}
```

Both `actual_repriced_usd` and `actual_stamped_usd` are returned so the SPA can show the actual invoice number *and* the apples-to-apples comparison without a second round-trip.

**SPA rule when `rows_missing_from_price_table > 0`:** display a warning beside `savings_pct` indicating the comparison is partial. The percentage is structurally overstated in this case (baseline counts every row; actual_repriced excludes the missing ones), so showing it without the caveat misleads. The numerator/denominator are still both useful — they just aren't quite the same shape.

**`actual_stamped_usd` is unconditional.** It sums every row's stamped `cost_usd` regardless of whether the row's model is in the current price table — stamped values were computed at write time, so they're correct independent of `PriceTable` state. `rows_missing_from_price_table` affects only `actual_repriced_usd` (and therefore `savings_usd` / `savings_pct`).

**Negative savings are a valid result.** If the user runs with an unusually expensive baseline or genuinely consumed more than the baseline would have, `savings_usd` and `savings_pct` can be negative — meaning "you spent 26% *more* than baseline." The SPA must render this case explicitly (e.g. red label, "26% over baseline"), not absolute-value, hide, or floor it to zero. The math is correct; suppressing the sign would lie about the workload.

### 4.8 `GET /analytics/by_key`

Per-(gateway-key) cost / token / call-count rollup with an inbound-shape breakdown per key. The companion view to `gateway.md §6` — the gateway stamps `gateway_key_id` and `inbound_shape` onto every `llm.call_completed`; this endpoint is where operators consume the rollup.

**Query parameters:**

| Parameter     | Type                | Required | Default |
|---------------|---------------------|----------|---------|
| `from`,`to`   | ISO 8601 UTC        | no       | last 7d |
| `gateway_key` | exact-match filter  | no       | (all keys) |

The `gateway_key` filter is passed via parameterized SQL placeholder; the HTTP layer additionally rejects values that don't match `^[A-Za-z0-9_-]{1,200}$` with a 400 `invalid_gateway_key` — defense in depth even though the SQL itself is safe by construction.

**Source:** `events` table, `type = 'llm.call_completed'`. Costs are stamped (matches the invoice).

**Algorithm:** the SQL fetches one row per call within the window, including `gateway_key_id` and `inbound_shape` from the payload. The handler aggregates in Python by `gateway_key_id` (using `Decimal` per §5.1) and tracks a per-shape sub-aggregate. Rows that originated from the in-process agent loop (CLI / TUI / `metis serve`) carry `gateway_key_id: null` and roll up under the `null` key.

**Response:**

```json
{
  "window": {"start": "...", "end": "..."},
  "current_pricing_version": "2026-05-08",
  "data": [
    {
      "gateway_key_id": "gk_01HZ...",
      "cost_usd": 0.4231,
      "input_tokens": 14820,
      "output_tokens": 612,
      "cached_input_tokens": 0,
      "cache_creation_input_tokens": 0,
      "call_count": 12,
      "by_inbound_shape": [
        {"inbound_shape": "openai", "call_count": 8, "cost_usd": 0.3010},
        {"inbound_shape": "anthropic", "call_count": 4, "cost_usd": 0.1221}
      ]
    },
    {
      "gateway_key_id": null,
      "cost_usd": 1.0512,
      "input_tokens": 51220,
      "output_tokens": 1842,
      "cached_input_tokens": 0,
      "cache_creation_input_tokens": 0,
      "call_count": 30,
      "by_inbound_shape": [{"inbound_shape": null, "call_count": 30, "cost_usd": 1.0512}]
    }
  ]
}
```

Rows are sorted by `cost_usd` DESC. The `by_inbound_shape` sub-array is also `cost_usd` DESC. `inbound_shape: null` is the natural shape for in-process agent traffic (no inbound translator ran).

---

## 5. Pricing semantics

Recapping §2.1.3 in concrete terms:

| Field on response                            | Source                                                   | When to display                             |
|----------------------------------------------|----------------------------------------------------------|---------------------------------------------|
| `cost_usd` on `/analytics/cost`              | Stamped on the `llm.call_completed` event                | "What I spent" views; matches the invoice.  |
| `actual_stamped_usd` on `/analytics/savings` | Stamped on the events                                    | Reconciliation with the bill.               |
| `actual_repriced_usd`, `baseline_repriced_usd` on `/analytics/savings` | Re-computed in-handler against current `PriceTable`      | Counterfactual / "vs naive baseline" panel. |

Re-pricing logic lives in `PriceTable.compute_cost` ([pricing/table.py](../../packages/metis-core/src/metis_core/pricing/table.py)). The handler calls it directly; no new pricing code is introduced by this spec.

**`current_pricing_version` is opaque.** `PriceTable.with_overlay()` produces composite version strings of the form `"2026-05-08+<overlay_version>"` (used by the OpenRouter adapter, which fetches rates at startup and overlays them on the base table). The SPA must treat the version field as an opaque string — for display, equality comparison, and join keys against historical stamps — and must not parse it.

### 5.1 Decimal serialization

JSON has no Decimal type, but `Usage.cost_usd` is `Decimal` throughout the core ([canonical-message-format.md §6.4](canonical-message-format.md)) precisely to avoid float drift on cent-level math. The convention here:

- **Aggregate in `Decimal`.** Every handler that sums cost across rows holds the running total as `Decimal` until the response is serialized.
- **Convert at the response boundary.** Cost fields are emitted as JSON numbers with **6 decimal places of precision** (`format(d, '.6f')`, then parsed back to a JSON number). 6 places sit ~4 orders of magnitude below cent precision at $10K spend, well under any drift the SPA could introduce; cost-by-model summed over 10K rows still rounds cleanly to cents.
- **The SPA renders to cents (2 decimals)** for display; the precision floor in the response is for SPA-side composition (e.g. summing across endpoints, computing ratios) without snowballing rounding error.
- **`stamped` values are passed through, not re-rounded.** A stamped `cost_usd` is whatever the source event recorded — typically already short of 6 decimal places. The 6-place convention applies to handler-side aggregates and re-priced values; pre-existing stamps are emitted as-is.

Token counts and `latency_ms` are integers; this convention applies only to cost fields.

---

## 6. Errors

Follows [server-api.md §5](server-api.md) conventions. New error codes specific to analytics:

| Code  | HTTP | When                                                                         |
|-------|------|------------------------------------------------------------------------------|
| `invalid_time_window`     | 400  | `from > to` or malformed ISO 8601.                                  |
| `invalid_group_by`        | 400  | `group_by` is not in the allowed set for that endpoint.             |
| `invalid_order`           | 400  | `order` is not in the allowed set (`cost` \| `recency`).            |
| `invalid_limit`           | 400  | `limit` is not an integer or is less than 1.                        |
| `unknown_baseline_model`  | 400  | Savings baseline isn't registered in the current `PriceTable`.      |
| `turn_not_found`          | 404  | No events match the given `turn_id`.                                |

Naming follows the symmetric convention `invalid_<param>` for value-rejection errors and `unknown_<resource>` / `<resource>_not_found` for lookup failures, matching [server-api.md §5](server-api.md).

---

## 7. Invariants

1. **Read-only.** No endpoint mutates `events`, `messages`, or `sessions`. No bus events emitted.
2. **Idempotent.** Re-running the same request with the same window returns the same data (modulo events that have landed between requests).
3. **Window bounds are echoed.** Every response carries the resolved `window.start`/`window.end` (or `null/null` for non-time-windowed endpoints).
4. **Pricing-version stamped on the response.** `current_pricing_version` always reflects the table active at request time; in-row stamped values are unaffected.
5. **No `sensitivity` filtering.** v1 is single-user local; the local user is the data owner. A future remote-deployment shape may need to filter `private`-sensitivity payloads from responses, but that's downstream of the `STRATEGY.md §3` fork.
6. **Turn drill-down is bounded in practice, not in spec.** No pagination in v1; one turn → one response. The existing turn-locked model + tool-cycle loop in [sessions/manager.py](../../packages/metis-core/src/metis_core/sessions/manager.py) means a single turn is bounded in event count in practice. If a future surface (e.g. long agent loops with many small tool calls) violates that assumption, add `?limit` and `?since_event_id` parameters — additive, non-breaking.

---

## 8. Testing strategy

### 8.1 Required tests

1. **Empty window returns empty data, valid envelope.** Window over a period with no events: every endpoint returns `data: []` (or zeroed totals) and the correct echoed window.
2. **Cost aggregation matches the trace.** Seed N `llm.call_completed` events with known costs; assert `/analytics/cost?group_by=model` sums per model.
3. **Cache hit rate math includes writes.** With `uncached=1000`, `cached=400`, `cache_creation=600`: `hit_rate == 0.2` (400 / 2000), `cache_write_share == 0.3` (600 / 2000). A row with all-uncached input has both ratios at 0.
4. **Routing slot wins.** Seed a `route.decided` event with `winner_index=2`; verify it counts under `rule` in the response.
5. **Routing hard failure bucketed separately.** Seed a `route.decided` with `winner_index=-1` and three rejected chain entries; verify `hard_failures == 1`, every win counter is unchanged, and the three rejections show up under `rejections`.
6. **Routing rejections traversed.** A `route.decided` with two `rejected` chain entries contributes two rows to `rejections`.
7. **Reliability percentiles.** Latency list `[100, 200, ..., 1000]` produces `p50 ~= 500`, `p95 ~= 950`.
8. **Sessions order.** With three sessions of varying cost, `?order=cost` and `?order=recency` produce different orderings; the displayed `updated_at` matches the field used for sort.
9. **Sessions response renames cost.** Response carries `cost_usd`, not `cost_so_far_usd`.
10. **Turn drill-down round-trips, in-flight handled.** Submit a real turn (using the scripted adapter), fetch by `turn_id`, verify all expected event types appear in order plus user and assistant messages. Separately: fetch a turn that has `turn.started` but no `turn.completed`/`turn.cancelled`; verify `in_flight == true` and the response upper-bounds at request time.
11. **Savings against missing baseline.** `?baseline=does-not-exist` → 400 `unknown_baseline_model`.
12. **Savings counterfactual math.** Two events: one on haiku, one on sonnet. Baseline = opus. Verify `baseline_repriced_usd` = sum(token_counts × opus_rates) and `actual_repriced_usd` uses each row's actual model.
13. **Savings missing-model accounting.** One event references a model not in the current price table; verify it counts toward `baseline_repriced_usd`, is excluded from `actual_repriced_usd`, and increments `rows_missing_from_price_table`.
14. **Stamped vs re-priced separation.** Two events written under pricing_version A; current table is version B with different rates. `/analytics/cost` reflects A; `/analytics/savings.actual_repriced_usd` reflects B.
15. **Pricing-version surfaced.** `current_pricing_version` matches the PriceTable's version field at request time, including composite overlay versions (e.g. `"2026-05-08+openrouter-2026-05-11"`).
16. **Time window validation.** `from > to` → 400. Malformed ISO → 400.
17. **SQL injection guard on whitelisted params.** `group_by=DROP TABLE` is rejected as 400 `invalid_group_by`; `order=; DELETE` is rejected as 400 `invalid_order`. Neither reaches SQL.
18. **Row schema per `group_by`.** For each of `model`/`provider`/`session`/`day`/`hour`, `data` is an array with the right key columns. `group_by=none` returns `data` as a single object, not an array. Time buckets are returned in ascending order.
19. **Decimal aggregation precision.** Sum 10K `llm.call_completed` events whose stamped costs end in odd long decimal expansions; assert the aggregated response value matches the sum-of-Decimals to within 1e-9. (Catches a regression to float aggregation.)
20. **Cost serialization decimal places.** Re-priced and aggregated cost fields in responses parse to numbers with ≤ 6 decimal places. Stamped fields pass through unchanged.
21. **Stamped vs missing-model interaction.** A row with a model missing from the current price table contributes to `actual_stamped_usd` and `baseline_repriced_usd`, but not `actual_repriced_usd`. `rows_missing_from_price_table == 1`.
22. **Negative savings flow through.** Two cheap actuals vs an expensive baseline produce positive savings; the reverse (expensive actuals vs cheap baseline) returns negative `savings_usd` and negative `savings_pct` — not zero, not absolute-valued.

### 8.2 Property tests

- **Cost monotonicity.** Extending the window forward (later `to`) never decreases summed `cost_usd`.
- **Savings sign.** `actual_repriced_usd <= baseline_repriced_usd` when `baseline` is the most expensive model in the catalog and every actual row used a cheaper one. (Construct the fixture to make this trivially true.)

---

## 9. Open questions

Deferred from this spec; revisit when the matching evidence shows up.

1. **Rollup table.** When does direct `json_extract` slow enough that we need to populate a `usage_rollups` table from the bus? Tentative threshold: 100K `llm.call_completed` rows or sustained p95 > 1s on any endpoint.
2. **Custom date ranges in the SPA.** The API already supports arbitrary `from`/`to`. When does the UI need a date picker? When users start asking "what did I spend in March."
3. **Per-user cost attribution.** Requires a multi-user identity layer that doesn't exist yet. Downstream of the [`STRATEGY.md §3`](../STRATEGY.md) fork.
4. **Skill / pattern / delegation analytics.** Hold until those subsystems are stable enough to be worth measuring (Phase 2.5 / 4).
5. **Streaming live updates to the dashboard.** Currently the SPA re-polls on view change; a WebSocket push could refresh tiles as new events land. Probably not worth it at single-user local scale, but the existing `/sessions/{id}/stream` is the obvious extension point.
6. **Redacted / shareable views.** A "screenshot mode" that scrubs workspace paths, file names, and prompt fragments for sharing in sales contexts. Surface change, not data change.
7. **Predicate-window alignment.** Should `cost_today_exceeds_usd` move to local TZ to match the dashboard, or should the dashboard expose a "UTC midnight" toggle? Wait for confusion before deciding.

---

## 10. Decision log

| Date       | Decision                                                              | Rationale                                                                                              |
|------------|-----------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------|
| 2026-05-12 | Read-only, projection over trace store; no rollup table               | At single-user scale `json_extract` is plenty fast; rollups add drift + fast-path-budget concerns.     |
| 2026-05-12 | Hybrid pricing: stamped for actuals, re-priced for the counterfactual | "What I spent" must reconcile with the bill; counterfactual is meaningless unless num/denom share a table. |
| 2026-05-12 | UTC at the API; SPA converts to local TZ                              | Keeps the server pure; flipping to custom date picker later is a frontend-only change.                 |
| 2026-05-12 | Loopback-only inherits; no new auth                                   | Matches v1 server posture; multi-user/remote is downstream of the unresolved STRATEGY.md §3 fork.      |
| 2026-05-12 | Routing chain aggregation in Python, not SQL                          | Clearer at this scale; SQLite `json_each()` is the escape hatch if it ever bottlenecks.                |
| 2026-05-12 | Fold `/tokens` into `/cost`                                           | Same source rows; separate endpoints would duplicate the SQL and the response envelope.                |
| 2026-05-12 | Audience toggle lives in the SPA, not the API                         | Same endpoints serve both Cost and Activity views; the toggle just reorders tiles.                     |
| 2026-05-12 | Cache hit-rate denominator includes `cache_creation_tokens`           | Cache writes are billed (~1.25× input rate); excluding them inflates hit rate during cache rebuild. `cache_write_share` broken out separately so the cache-rebuild signal stays visible. |
| 2026-05-12 | Hard-failure `route.decided` events bucketed under `hard_failures`, not policy wins | The engine emits exactly one `route.decided` per turn including on hard failure with `winner_index = -1`; indexing into `chain[winner_index]` would error or miscount. Rejections within the chain are still counted in the rejections breakdown. |
| 2026-05-12 | Sessions list sort key and displayed field are one source (`updated_at`) | The earlier draft sorted on `sessions.updated_at` but displayed `MAX(messages.created_at)`. Same source for both removes a possible ordering surprise and an N-subquery cost. The two fields are within microseconds of each other in practice. |
| 2026-05-12 | Whitelist-map `group_by` and `order` parameters; never interpolate    | Request strings entering SQL must be mapped to literal column names by the handler, even with no untrusted caller in v1. Keeps the surface safe by construction.                                |
| 2026-05-12 | Turn drill-down upper bound falls back to `now()` for in-flight turns | Lets the SPA render a live turn the same way `/sessions/{id}/stream` does. `in_flight: true` in the response signals the message list may grow on re-poll.                            |
| 2026-05-12 | No window-size cap in v1                                              | Cap was speculative; `json_extract` over indexed `(type, timestamp_us)` is fast enough at single-user scale and the p95 target is the load-bearing constraint.                       |
| 2026-05-12 | Decimal aggregated end-to-end; serialized as JSON number with 6-place precision | Pricing is `Decimal` throughout the core (AGENTS.md). Float-typed aggregates over 10K rows can drift cents; 6 decimal places is well below cent precision and parses safely. |
| 2026-05-12 | Time buckets are single-dimension                                     | `group_by=day,model` (multi-key) is a future enhancement, not v1. Splitting time series by model bloats the row count and the SPA renders fine without it for v1.                                       |
| 2026-05-12 | `actual_stamped_usd` is unconditional across missing-model rows       | Stamped values are correct at write-time; the current `PriceTable` doesn't affect them. Only re-priced fields care about the current table.                                                              |
| 2026-05-12 | Negative `savings_usd` / `savings_pct` are valid results              | "You spent more than baseline" is a real workload outcome; suppressing the sign would lie. SPA renders the case explicitly.                                                                              |

---

## 11. References

- [`canonical-message-format.md`](canonical-message-format.md) — `Message`, `Usage`, `MessageMetadata`, persistence schema.
- [`event-bus-and-trace-catalog.md`](event-bus-and-trace-catalog.md) — `llm.call_completed`, `llm.call_failed`, `route.decided`, `turn.completed` payloads.
- [`server-api.md`](server-api.md) — base HTTP surface conventions this spec extends.
- [`memory-store.md`](memory-store.md) — sibling spec drafted retrospectively from existing code; shape-reference for this doc.
- [`../STRATEGY.md`](../STRATEGY.md) — buyer ≠ user framing, savings-as-the-headline thesis, unresolved replacement-agent-vs-gateway fork.
- [`../KNOWN_ISSUES.md`](../KNOWN_ISSUES.md) — prompt-caching gap (5–10× left on the table); cache-effectiveness view doubles as forcing function.
- [`packages/metis-core/src/metis_core/trace/store.py`](../../packages/metis-core/src/metis_core/trace/store.py) — the table this spec reads from.
- [`packages/metis-core/src/metis_core/pricing/table.py`](../../packages/metis-core/src/metis_core/pricing/table.py) — `compute_cost`, used directly by `/analytics/savings`.
