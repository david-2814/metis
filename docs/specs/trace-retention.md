# Trace Retention Specification

**Status:** Draft v1 (Wave 12)
**Last updated:** 2026-05-15

> Sliding-window retention for the trace DB. The trace store has been
> unbounded since v1; production buyers operating multi-month deployments
> need a configurable cutoff that prunes old events while keeping
> audit-flagged events forever. The sweep is an explicit operator action
> (`metis trace prune`) or a Kubernetes `CronJob` running the same binary
> against the same DB — never an in-process background task in the
> gateway hot path.

This spec depends on:

- [`event-bus-and-trace-catalog.md`](event-bus-and-trace-catalog.md) — defines
  the trace DB schema (§7.1), the durability posture (§7.2), and the
  pre-Wave-12 placeholder retention sketch (§7.3). This document replaces
  §7.3.
- [`audit-log.md`](audit-log.md) — Wave 12a-1 ships
  `AUDIT_EVENT_TYPES: frozenset[str]` and `is_audit_event()` in
  `metis_core.events.payloads`. Audit-flagged event types are the subset
  this sweep must preserve. `trace.swept` is added to the frozenset
  by this spec.

---

## 1. Scope

In scope:

- Time-based retention with a single `retention_days` knob per workspace.
- Sweep mechanics: on-demand via `metis trace prune` and a recommended
  daily Kubernetes `CronJob`.
- Audit-event exemption: events whose registry entry carries `audit=True`
  are never deleted by sweep, regardless of age.
- Eviction observability: every sweep emits a `trace.swept` event with
  row counts and the oldest kept timestamp. The eviction event itself is
  audit-flagged so subsequent sweeps preserve the eviction history.

Out of scope (deferred to later waves):

- Per-event-type retention (the §7.3 placeholder showed `by_type`
  overrides; v1 ships a single global cutoff).
- Cold-storage tiering (move-rather-than-delete to object storage).
- Session-message retention — `SqliteSessionStore` has its own
  lifecycle and is not touched here.
- Pattern-store retention — `PatternStore` already has its own
  bounded eviction (pattern-store.md §6).
- In-process background sweeps inside the gateway/server. v1 keeps
  retention out of the request hot path; the operator wires a separate
  Kubernetes `CronJob` (or any equivalent host scheduler) to invoke
  `metis trace prune` on the shared trace-DB volume.

---

## 2. Configuration

### 2.1 Defaults

| Constant | Default | Override |
|---|---|---|
| `retention_days` | 90 | CLI `--days N`, helm `traceRetention.days` |
| Sweep cadence (operator-defined) | daily 03:00 UTC | helm `traceRetention.schedule` (cron expression) |
| Audit-exemption | always on | library `exempt_audit=False` for tests only |

90 days matches the §7.3 "Phase 3+: optional retention policy" sketch's
intent — comfortably covers any realistic billing / debugging window
without unbounded growth.

### 2.2 Per-workspace vs global

In v1 the retention cutoff is **global** to the trace DB file, not
per-workspace. The gateway and the agent server both write to a single
trace DB in their respective deployments; events from many workspaces
share it. Per-workspace cutoffs would require splitting the DB or adding
a `workspace_path` column to every event (today only some payload types
carry it). Either change is out of scope for v1.

If a deployment needs per-workspace retention (e.g. one buyer needs
365-day audit trail while another is fine with 30), run separate gateway
deployments with separate trace-DB volumes — this is already the path
buyers take for multi-tenant isolation.

---

## 3. Sweep mechanics

### 3.1 Algorithm

A sweep is a single SQL `DELETE` constrained by:

1. **Cutoff:** `timestamp_us < <cutoff_us>` where `cutoff_us` is
   `(now - retention_days)` in unix microseconds.
2. **Audit exemption:** `type NOT IN (<audit_types>)` where
   `<audit_types>` is the set of event types whose `PAYLOAD_REGISTRY`
   entry is flagged `audit=True`. `trace.swept` is one of them, so a
   sweep cannot delete the history of previous sweeps.

```sql
DELETE FROM events
 WHERE timestamp_us < ?
   AND type NOT IN (?, ?, ...)
```

The query rides a dedicated `idx_events_timestamp_us` index added in
§4. Without that index, the planner falls back to a full table scan or
walks `(type, timestamp_us)` once per non-audit type — neither is
acceptable on a multi-million-event trace.

### 3.2 WAL safety

`TraceStore` opens with `journal_mode=WAL` + `synchronous=NORMAL`
(event-bus-and-trace-catalog.md §7.2). Under WAL, a long-running
`DELETE` does not block concurrent readers — they see a consistent
snapshot from before the delete. Writers (the gateway / server emitting
new events) take a brief BEGIN IMMEDIATE lock only at commit time. As
long as the sweep transaction is bounded to a reasonable batch (v1: one
statement, no chunking), the contention window is the SQL execution
time, not the entire sweep duration.

Empirical bound (Wave 12 unit test): deleting 100k rows from a 1M-row
table on a modern SSD completes in well under a second; the writer-lock
window is shorter still.

### 3.3 Dry-run vs apply

`TraceStore.purge_older_than(cutoff)` defaults to `dry_run=True`. The
library is deliberately the safe default — programmatic callers
(tests, future tooling) have to opt into deletion. The CLI inverts
this: `metis trace prune --days 90` deletes; `metis trace prune --days
90 --dry-run` reports what would be deleted without touching rows. The
inversion is because the CLI is an explicit operator action — a cron
that runs `metis trace prune` should just do the work without an extra
flag every iteration.

In dry-run mode the implementation runs a `SELECT COUNT(*)` with the
same predicates and returns the would-have-deleted count. No
`trace.swept` event is emitted in dry-run.

### 3.4 Atomicity

The sweep runs in a single statement under `isolation_level=None`
(SQLite autocommit). The `DELETE` itself is atomic. After the delete
returns, the implementation:

1. Computes `oldest_kept_timestamp` via `SELECT MIN(timestamp_us) FROM
   events` (NULL if empty).
2. Emits `trace.swept` on the bus (when a bus is provided). The
   `trace.swept` event is itself an audit-preserved event, so the next
   sweep will not delete it.

The bus emission is best-effort: if it fails, the delete still happened
and the `PurgeResult` return value still has the correct counts.
Callers can therefore observe the result without subscribing to the
bus.

---

## 4. Schema

`TraceStore.__init__` runs the schema `executescript` on every open
(event-bus-and-trace-catalog.md §7.1). v1 adds one new index:

```sql
CREATE INDEX IF NOT EXISTS idx_events_timestamp_us ON events(timestamp_us);
```

This is purely additive — existing DBs pick it up on next open. The
`PRAGMA user_version` (TRACE_SCHEMA_VERSION) does **not** bump because
adding an index does not break the row format. Backups produced before
this change will restore cleanly.

The `(type, timestamp_us)` index added earlier serves the analytics
query "events of type X this week" and is unchanged. The new
single-column index serves the sweep's "every type older than cutoff"
shape.

---

## 5. Audit-event exemption

### 5.1 Source of truth

Audit-ness is a property of the event type, owned by Wave 12a-1
([`audit-log.md`](audit-log.md)). 12a-1 ships `AUDIT_EVENT_TYPES:
frozenset[str]` and `is_audit_event(event_type)` in
`metis_core.events.payloads`. The retention sweep reads that frozenset
to build its `type NOT IN (...)` predicate; the retention module does
not redefine the mechanism.

Audit-flagged types as of v1 (registered by 12a-1, extended by this
spec):

| Event type | Why audit |
|---|---|
| `gateway.key_issued` / `gateway.key_revoked` / `gateway.key_rotated` | Key lifecycle (gateway.md §11) — the trail of which key existed when. |
| `gateway.quota_exceeded` / `quota.alert` | Spend-cap breaches and pre-breach warnings (multi-user.md §5). |
| `routing.policy_invalid` | Failed policy reloads — operator must know. |
| `memory.eviction` / `pattern.evicted` | Bounded-store enforcement records. |
| `tool.confirmation_resolved` | Operator/user explicit consent decisions. |
| `trace.swept` (added by this spec) | Sweep history — "when did we last prune what." |

Adding `trace.swept` to `AUDIT_EVENT_TYPES` is a deliberate spec change
per audit-log.md's "Adding or removing a type is a deliberate spec
change with a CHANGES.md entry" rule. The CHANGES.md entry for this
spec records the addition.

### 5.2 Exemption is not configurable per-call (almost)

The library exposes `exempt_audit: bool = True` on `purge_older_than`
for unit-test ergonomics only — a test that wants to verify the delete
math on a controlled fixture can flip it off. The CLI does **not**
expose a flag for it; production sweeps always exempt audit events.

---

## 6. Eviction events

### 6.1 `trace.swept` payload

```python
class TraceSwept(msgspec.Struct, frozen=True):
    rows_deleted: int
    rows_audit_exempt: int
    cutoff_timestamp: datetime
    oldest_kept_timestamp: datetime | None  # None if DB is empty after sweep
    dry_run: bool
    swept_at: datetime
```

Default sensitivity: `Sensitivity.PSEUDONYMOUS`. Audit-flagged.
`session_id` on the envelope is the literal string `"system"` (no
session owns a sweep — it's an operator action).

### 6.2 What's NOT in the payload

- A per-type breakdown (`{tool.called: 1234, llm.call_started: 5678,
  ...}`). The sweep deletes by predicate without enumerating; computing
  the breakdown would require a second pre-delete `SELECT type,
  COUNT(*) GROUP BY type` pass. v1 ships without it; future versions
  can add the breakdown behind a `--detailed` flag if operators ask
  for it.
- The deleted rows themselves. Sweep is a delete, not a tombstone.

### 6.3 Replay semantics

Because `trace.swept` rides the bus like any other event, downstream
subscribers (analytics, dashboards, alerting) get a real-time view of
prune activity. The trace store itself persists the event, so a
replay query against the trace DB returns the full sweep history.

---

## 7. CLI

### 7.1 `metis trace prune`

```
metis trace prune [--days N] [--db-path PATH] [--dry-run]
```

| Flag | Default | Meaning |
|---|---|---|
| `--days N` | 90 | Cutoff: delete events older than N days. |
| `--db-path PATH` | `~/.metis/metis.db` | Trace DB. |
| `--dry-run` | off | Report counts without deleting. |

Exit codes: 0 on success (including dry-run), non-zero on error
(missing DB, unwritable file, etc.). Deterministic stdout output:

```
trace prune complete (dry_run=false)
  db_path:               /var/lib/metis/metis.db
  cutoff:                2026-02-14T03:00:00+00:00 (90 days)
  rows_deleted:          12345
  rows_audit_exempt:     7
  oldest_kept_timestamp: 2026-02-14T03:01:12+00:00
```

The output matches the `metis backup` / `metis restore` style — paths,
counts, ISO timestamps, no random ids — so operators can checksum it
in their cron logs.

### 7.2 Sub-command shape

`metis trace` is a new top-level subcommand group with one operation
(`prune`) in v1. Future operations (`metis trace stats`, `metis trace
size`) can join the same group without re-shaping the CLI.

---

## 8. Kubernetes `CronJob`

The helm chart ships an optional `CronJob` template that runs `metis
trace prune` against the shared trace-DB PVC. The template is **off by
default** so existing buyers don't see new resources without opting in.

```yaml
traceRetention:
  enabled: false
  days: 90
  schedule: "0 3 * * *"   # daily at 03:00 UTC
  image:
    # Defaults to the gateway image; override only if shipping a
    # retention-only image.
    repository: ""
    tag: ""
  resources:
    requests:
      cpu: 50m
      memory: 64Mi
    limits:
      cpu: 500m
      memory: 256Mi
  successfulJobsHistoryLimit: 3
  failedJobsHistoryLimit: 3
```

Design decisions:

1. **Separate pod, not in-process.** The gateway is on the request hot
   path; an in-process sweep would compete with serving traffic for
   CPU and writer-lock windows. A CronJob in its own pod, mounting the
   same PVC, isolates the contention.
2. **Same image.** The retention binary is the same `metis` CLI the
   gateway ships. Operators don't manage two images.
3. **ReadWriteMany not required.** The CronJob mounts the trace-DB PVC
   `ReadWriteOnce` (one pod at a time runs prune); the gateway's pod
   has the PVC mounted concurrently — which works only on
   ReadWriteMany filesystems. If the cluster's storage class is RWO,
   the operator must either (a) move to RWX (Longhorn, NFS, AWS EFS),
   (b) pause the gateway during the prune window via a separate hook,
   or (c) run prune against a backup file outside the cluster. v1
   documents the trade-off; the operator picks.
4. **`concurrencyPolicy: Forbid`.** Sweep should not overlap itself
   if a prior run is still going (large retention catch-up after a
   long pause).

---

## 9. Testing

Required tests (`packages/metis-core/tests/trace/test_retention.py`):

1. **Cutoff math.** Events older than cutoff are deleted; events at-or-after the cutoff survive.
2. **Audit exemption.** Audit-flagged event types survive a sweep that would otherwise delete them.
3. **`trace.swept` survives.** Emit a `trace.swept`, advance time, run another sweep with a cutoff past the first sweep's timestamp; assert the first sweep's event survives.
4. **Dry-run.** `dry_run=True` returns the would-have-deleted count without changing row counts and emits no `trace.swept`.
5. **Empty DB.** Sweep on an empty DB returns `rows_deleted=0`, `oldest_kept_timestamp=None`.
6. **Bus emission.** When a bus is provided, `purge_older_than` emits exactly one `trace.swept` event with matching counts.
7. **Index presence.** After `TraceStore.__init__`, `idx_events_timestamp_us` exists (introspection via `sqlite_master`).
8. **CLI dry-run.** `metis trace prune --dry-run` exits 0, prints `dry_run=true`, does not delete rows.
9. **CLI apply.** `metis trace prune --days 1` against a fixture with old + new rows deletes only the old ones and prints a deterministic summary.
10. **Audit-set membership.** `AUDIT_EVENT_TYPES` contains `trace.swept` so the sweep cannot delete its own history.

---

## 10. Open questions

1. **Per-type retention?** §7.3's pre-Wave-12 placeholder sketched
   `by_type` overrides ("`llm.call_started: 90`, default `365`"). v1
   ships without them — single global cutoff is the simpler contract.
   Revisit if operators report a specific need (e.g. they want to
   retain `route.decided` longer for compliance while pruning
   `tool.called` aggressively).
2. **Cold-storage tiering?** Moving rather than deleting (S3 / GCS / a
   sibling SQLite file). Deferred; v1 deletes. The §7.5 backup
   contract (`metis backup`) is the operator's pre-prune snapshot
   recipe in the meantime.
3. **Async sweep API?** v1's `purge_older_than` is synchronous because
   the CronJob model doesn't need async. If an in-process scheduler
   later wants to call it without blocking the event loop, wrap with
   `asyncio.to_thread`.

---

## 11. Decision log

- **2026-05-15** — Single global cutoff (not per-type, not
  per-workspace). Rationale: §1 scope.
- **2026-05-15** — CLI defaults to apply, `--dry-run` is opt-in.
  Rationale: CronJob ergonomics.
- **2026-05-15** — Library defaults to `dry_run=True`. Rationale:
  programmatic-caller safety.
- **2026-05-15** — Out-of-process sweep (CronJob), not in-process.
  Rationale: §8 design decision 1.
- **2026-05-15** — Audit exemption is a property of the event type
  (registry-driven), not the row. Rationale: §5.1 — the flag is
  schema-stable across deployments; per-row flagging would require a
  schema change.
- **2026-05-15** — `trace.swept` is itself audit-flagged. Rationale:
  prune history is the audit trail of the prune mechanism.

---

## 12. References

- `event-bus-and-trace-catalog.md §7` — trace store schema, durability,
  retention placeholder this spec replaces.
- `gateway.md §11` — key lifecycle audit events that motivate the
  audit-exemption mechanism.
- `STRATEGY.md §2` — buyer ≠ user; retention is a buyer-facing
  compliance concern, not a developer ergonomic.
