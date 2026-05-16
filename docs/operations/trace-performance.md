# Trace-DB Performance and Tuning

**Status:** v1 (Wave 13, 2026-05-15)
**Audience:** SRE / platform operators running `metis-gateway` or `metis-server` in a multi-tenant deployment.

This document is the operational companion to
[`event-bus-and-trace-catalog.md §7`](../specs/event-bus-and-trace-catalog.md). It captures the
write-throughput baseline, the index posture, the VACUUM schedule, and the WAL-monitoring
contract for the SQLite trace DB. The goal is to give operators concrete numbers to plan
capacity from instead of "it's SQLite, it'll be fine."

---

## 1. Write-throughput baseline

The trace store is wired as a **fast-path bus subscriber** ([`trace/store.py`](../../packages/metis-core/src/metis_core/trace/store.py)) and writes one row per event under autocommit (`isolation_level=None`, WAL + `synchronous=NORMAL`). This commits to a sub-millisecond per-event budget at the cost of group-commit batching.

Run the benchmark yourself:

```bash
uv run python scripts/bench_trace_throughput.py --events 50000              # full bus + trace path
uv run python scripts/bench_trace_throughput.py --events 50000 --bus-only   # bus-only ceiling
uv run python scripts/bench_trace_throughput.py --events 50000 --raw-sqlite # SQLite INSERT ceiling
```

Reference numbers (Apple M-series laptop, Python 3.13.12, SQLite 3.50.4, local APFS SSD, 50k synthetic `llm.call_completed` events, payloads ~280 bytes JSON):

| Scenario | Events/sec | Wall | CPU share | Notes |
|---|---|---|---|---|
| `bus + trace` (full path) | **~4,800** | 10.5 s | 88 % | One INSERT per event; msgspec encode + bus dispatch + SQLite C-call overhead. |
| `bus-only` (no subscriber) | ~62,000 | 0.8 s | 97 % | Bus dispatch ceiling. |
| `raw-sqlite` (one INSERT loop, no bus, no msgspec.Struct) | ~28,000 | 1.8 s | 93 % | SQLite ceiling on this hardware under WAL + NORMAL. |

**Bottleneck:** the full-path number is **CPU-bound on per-event payload encoding + Python→SQLite C-call overhead**, not disk-bound (`cpu_share_of_wall ≈ 88 %` and `block_output_ops` shows no IO wait — confirmed on Linux where `rusage` populates that field). Switching to `synchronous=FULL` would cap us at ~5–20 ms per INSERT (~50–200 events/sec) — out of the question for the spec's <1 ms fast-path budget.

### 1.1 Capacity planning

For a SaaS deployment, the relevant load number is **events per second per gateway key, multiplied by active keys**. A typical conversational workload emits 4–8 catalog events per LLM turn (`turn.started`, `route.decided`, `llm.call_started`, `llm.call_completed`, `tool.called` × N, `turn.completed`). At 1 turn/sec sustained per key, that's ~6 events/sec per key.

| Active gateway keys | Sustained events/sec | Headroom |
|---|---|---|
| 100 | ~600 | 8× |
| 500 | ~3,000 | 1.6× |
| 1,000 | ~6,000 | over budget |

**Actionable:** a single `metis-gateway` pod on default settings handles ~100 active concurrent keys at a steady-state turn-per-second pace with comfortable headroom, and can absorb short bursts at 5–10× that rate (the bus queue + WAL absorb the spike; the disk catches up). Beyond ~500 sustained-active keys per pod, the trade-offs in §5 apply.

### 1.2 What the bottleneck is NOT

- **Not disk-bound.** `cpu_share_of_wall ≈ 88 %` means we're spinning CPU, not waiting on `fsync`. `synchronous=NORMAL` defers fsync to checkpoint time.
- **Not WAL-bound.** WAL grew to ~33 MB during the 50k-event run — close to the 32 MB checkpoint threshold (§3 below) — and SQLite checkpointed once near the end without any throughput cliff.
- **Not bus-overflow-bound.** The default queue size (`EventBus(queue_size=512)`) tolerates short bursts without `EventBusOverflowError`. The benchmark uses a larger queue to measure steady-state, not burst tolerance.

---

## 2. Index posture (Wave 13)

The v1 schema in [`trace/store.py`](../../packages/metis-core/src/metis_core/trace/store.py) carried five indexes covering session replay, type-windowed analytics, turn lookup, parent-event walk, and the retention sweep cutoff. Wave 13 adds **five more** to cover the multi-tenant analytics rollups (`gateway_key_id` / `user_id` / `team_id`), the GDPR portability export, the eval slice, and the per-turn ORDER BY:

| Index | Covers | Added |
|---|---|---|
| `idx_events_session_id` `(session_id, id)` | streaming-protocol §3.6 replay | v1 |
| `idx_events_type_timestamp` `(type, timestamp_us)` | most `/analytics/*` slices | v1 |
| `idx_events_turn` `(turn_id)` | `events_for_turn` (legacy) | v1 |
| `idx_events_parent` `(parent_event_id)` | causal walk | v1 |
| `idx_events_timestamp_us` `(timestamp_us)` | retention sweep cutoff | Wave 12a-2 |
| **`idx_events_turn_id_id`** `(turn_id, id)` | `events_for_turn` ORDER BY id (eliminates TEMP B-TREE) | **Wave 13** |
| **`idx_events_gateway_key_id`** expr index, partial `WHERE gateway_key_id IS NOT NULL` | `/analytics/by_key` filter | **Wave 13** |
| **`idx_events_user_id`** expr index, partial `WHERE user_id IS NOT NULL` | `/analytics/by_user`, `user/{id}/export`, `user/{id}/forget` | **Wave 13** |
| **`idx_events_team_id`** expr index, partial `WHERE team_id IS NOT NULL` | `/analytics/by_team` filter | **Wave 13** |
| **`idx_events_eval_subject_kind`** `(json_extract(...,'$.subject_kind'), timestamp_us)`, partial `WHERE type = 'eval.completed'` | `/analytics/quality` | **Wave 13** |

**Verification.** Each query in [`analytics/store.py`](../../packages/metis-core/src/metis_core/analytics/store.py) is covered by an `EXPLAIN QUERY PLAN` test in [`packages/metis-core/tests/trace/test_query_plans.py`](../../packages/metis-core/tests/trace/test_query_plans.py). Failure of those tests means a query was added without index coverage — fix the index, not the test.

**Migration.** All indexes are `CREATE INDEX IF NOT EXISTS` and additive — `TRACE_SCHEMA_VERSION` stays at `1`. Existing trace DBs pick up the new indexes on the next `TraceStore.__init__()`. The first open of a large existing DB will block briefly while SQLite builds the indexes (single-threaded, but on the order of seconds for a few million rows on local SSD); subsequent opens are instant.

**What's NOT indexed.** The cost-by-key/user/team rollups under `WHERE type = 'llm.call_completed' AND timestamp_us BETWEEN ...` still drive primarily through `idx_events_type_timestamp` and post-filter the JSON in Python — the planner picks this over the expression index because the type+timestamp slice is more selective for typical windows. This is **acceptable**: at any reasonable query window the post-filter cost is dominated by the row-fetch + Python aggregation cost, both bounded. If a single tenant ever produces enough volume that this dominates, we'd add a virtual column (event-bus-and-trace-catalog.md §7.4) — additive.

---

## 3. WAL monitoring and checkpoint tuning

SQLite's WAL accumulates committed transactions until a **checkpoint** copies them into the main DB. Wave 13 raises the auto-checkpoint threshold from SQLite's 1000-page default (~4 MB) to **8192 pages (~32 MB)**:

```python
PRAGMA wal_autocheckpoint = 8192   # set by TraceStore._configure
```

**Why bigger.** The default 4 MB threshold triggers checkpoints every ~14k events at our payload size, and each checkpoint stalls writers briefly while it copies pages. At 32 MB the checkpoint happens roughly every ~110k events — out of the way during typical multi-tenant bursts.

**Recovery trade-off.** A 32 MB WAL replay on cold start is ~250 ms on local SSD. Operators with very tight crash-recovery SLAs can lower the threshold via the `wal_autocheckpoint_pages` constructor argument:

```python
TraceStore(db_path, wal_autocheckpoint_pages=2048)   # 8 MB threshold
```

### 3.1 Prometheus gauge

The `metis_trace_wal_bytes` gauge ([`observability/metrics.py`](../../packages/metis-core/src/metis_core/observability/metrics.py)) reports the live WAL file size on every scrape. Polled via `TraceStore.wal_size_bytes()`. Wired into both the gateway and the server runtimes — the gateway is the highest-throughput writer, so its WAL gauge is the canonical signal.

**Recommended alert (Prometheus / Grafana):**

```yaml
- alert: MetisTraceWalBacklogged
  expr: metis_trace_wal_bytes > 100 * 1024 * 1024   # 100 MB = 3x the auto-checkpoint
  for: 5m
  labels:
    severity: warning
  annotations:
    summary: Trace-DB WAL has not checkpointed in >5min — long-running reader holding barrier?
    runbook: docs/operations/trace-performance.md §3.2
```

### 3.2 What to check when WAL grows past the alert threshold

A WAL persistently above ~3× the auto-checkpoint threshold means **a long-running reader is holding the checkpoint barrier**. SQLite cannot move pages out of the WAL while any reader has a snapshot pinned at an older transaction. Likely causes:

1. A `metis backup` is running (uses `VACUUM INTO`; long for big DBs). Wait for it.
2. An analytics query is iterating row-by-row and not closing its cursor. Check `/analytics/user/{id}/export` clients — that endpoint streams JSONL and a slow consumer keeps the cursor open. Mitigation: enforce a server-side timeout on the response.
3. A pre-Wave-13 analytics consumer set `PRAGMA read_uncommitted = 0` (never seen — but worth grepping).

**Force-checkpoint manually** (operator action, not in production hot path):

```bash
sqlite3 /var/lib/metis/metis.db "PRAGMA wal_checkpoint(TRUNCATE);"
```

`TRUNCATE` mode resets the WAL to zero bytes once it can acquire the write lock. Do this only when reads are quiescent.

---

## 4. VACUUM schedule

SQLite reclaims free pages lazily. Over a year of typical traffic + monthly retention sweeps, the `events` table accumulates fragmentation that bloats the file by 10–30 %. `VACUUM` rebuilds the file in place to reclaim the slack.

**Three options, in increasing operator effort:**

| Mode | When to use | Cost |
|---|---|---|
| Do nothing | DB stays under ~1 GB and operator doesn't care about wasted disk | 0 |
| `VACUUM` from a CronJob | Default for production multi-tenant deployments | Rebuild = ~size of DB; pod takes a long minute |
| `auto_vacuum = INCREMENTAL` + scheduled `incremental_vacuum` | Cleanest, but **must be set on a freshly-created DB** | One-time DB rebuild cost |

### 4.1 Recommended: monthly `VACUUM` CronJob

Wave 13 ships [`infra/gateway/helm/templates/cronjob-trace-vacuum.yaml`](../../infra/gateway/helm/templates/cronjob-trace-vacuum.yaml), a separate CronJob (independent of the Wave 12 retention CronJob) that runs `metis trace vacuum` on a configurable schedule. Default: monthly at 04:00 UTC.

```yaml
# values.yaml
traceVacuum:
  enabled: false                  # opt-in; flip to true after retention is stable
  schedule: "0 4 1 * *"           # first of every month, 04:00 UTC
  successfulJobsHistoryLimit: 3
  failedJobsHistoryLimit: 3
```

**Storage gotcha** (same as the retention CronJob): the VACUUM pod mounts the same PVC as the gateway. On `ReadWriteOnce` volumes this fails because two pods can't mount the volume simultaneously. Either move to `ReadWriteMany` (Longhorn / NFS / EFS) **or** schedule the VACUUM during a deployment maintenance window when the gateway pod is paused **or** vacuum a backup taken via `metis backup` and swap the file back.

### 4.2 Why not `auto_vacuum = INCREMENTAL` by default

`PRAGMA auto_vacuum = INCREMENTAL` requires being set **before any tables are created**. The trace DB is shipped with `auto_vacuum = NONE` (SQLite default) and we cannot retroactively change it without rebuilding the file from scratch. New deployments could opt in with a one-line change, but doing so silently for upgrading deployments would waste a one-time rebuild on every pod's first start. v1 keeps `auto_vacuum = NONE` and relies on the CronJob; upgrade to `INCREMENTAL` is a future migration owned by `event-bus-and-trace-catalog.md §7.6`.

---

## 5. When the single-process model isn't enough

The default gateway pod is **1 SQLite writer**. At ~5,000 events/sec and ~600 events per 100 active keys, this serves a low-hundreds active-key tenant comfortably with margin. Beyond that, the design gets sharper trade-offs:

| Capacity gap | Option | Trade-off |
|---|---|---|
| 5–10× throughput | Batched INSERT subscriber (multi-row `VALUES`) — replace fast-path subscriber with a coalescing one that flushes every N events / M ms | Breaks per-event durability; on hard crash you lose up to N events. **Not load-bearing for any user-visible state per `event-bus-and-trace-catalog.md §7.2`** but breaks the streaming-protocol §3.6 replay-on-reconnect window. Owner sign-off required. |
| 10–50× throughput | Per-replica trace DB + cross-replica analytics aggregation | Cost rollups need cross-replica join; not free. Closes the gateway HPA gap noted in `values.yaml`. |
| 50× + | Move trace store off SQLite (Postgres / ClickHouse) | Major architectural change; the per-event sensitivity floor (§4.4) and the audit-export contract carry over. Out of scope for v1. |

**Cross-reference 13a-3 (lift loopback).** When the gateway moves off `127.0.0.1` per [`gateway-hardening.md`](../specs/gateway-hardening.md), the public surface area expands and a single hostile client could push event volume far above the steady-state planning curve. The rate-limit middleware at [`apps/gateway/src/metis_gateway/middleware_ratelimit.py`](../../apps/gateway/src/metis_gateway/middleware_ratelimit.py) caps per-key + per-IP RPS at the request boundary, which is what protects the trace store from runaway emit. **Do not lift loopback without enabling the rate limiter** (or fronting the gateway with an L7 WAF that does the same).

---

## 6. Summary table

| Setting | Default | Override | Effect |
|---|---|---|---|
| `journal_mode` | `WAL` | not configurable | meets fast-path budget |
| `synchronous` | `NORMAL` | not configurable | meets fast-path budget; loses ≤1 s on hard crash |
| `wal_autocheckpoint` | 8192 pages (~32 MB) | `TraceStore(wal_autocheckpoint_pages=...)` | bigger = fewer checkpoint stalls; longer crash replay |
| `auto_vacuum` | `NONE` | not configurable in v1 | rely on monthly `metis trace vacuum` CronJob |
| `idx_events_*` (10 total) | created by `_SCHEMA` | not configurable | covers all analytics queries; verified by `test_query_plans.py` |

Operators who change any of the above should add a corresponding alert + runbook entry and update this document.
