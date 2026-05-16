# Audit Log

**Status:** Draft v1
**Last updated:** 2026-05-15
**Owner:** _your name_

> Wave 12 split of the existing trace store into two retention tiers:
> **operational telemetry** (the bulk of the trace catalog — `llm.call_*`,
> `tool.*`, `turn.*`, etc.) that a SOC2/GDPR buyer would happily prune on a
> short retention window, vs. **audit-relevant events** that must be
> preserved beyond that window. This spec defines the subset, the export
> surface, the append-only invariant the retention sweep must honor, and
> the CLI a buyer wires into their SIEM ingest.

> This spec depends on:
>
> - [`event-bus-and-trace-catalog.md`](event-bus-and-trace-catalog.md) — the closed catalog. Audit-relevance is a derived flag on each event type, not a parallel event family.
> - [`gateway.md`](gateway.md) — `gateway.key_*` event types are the load-bearing key-lifecycle records.
> - [`multi-user.md §7`](multi-user.md) — names "audit + compliance posture" as a buyer requirement and sketches the export shape; this spec is the contract.
> - [`STRATEGY.md §2`](../STRATEGY.md) — buyer ≠ user framing; SOC2 / GDPR is the buyer's ask, not the user's.
>
> This spec **does not** define:
>
> - The retention sweep itself — that lands as a sibling spec (12a-2). This spec defines only the *invariant* the sweep must honor: audit events are not eligible for retention deletion.
> - Cryptographic tamper-evidence (hash-chained event ids). Surfaced as an open question; v1 ships operator-controlled access as the trust boundary.

---

## 1. Purpose

The trace store ([`event-bus-and-trace-catalog.md §7`](event-bus-and-trace-catalog.md)) captures every bus event. Today it's a single tier: events persist forever (`§7.3`'s retention policy is sketched but not implemented). At single-user scale that's fine; at buyer scale it's not:

1. **Volume.** `llm.call_started` / `llm.call_completed` are emitted on every LLM call. A buyer running 200 devs × 100 turns/day × ~3 calls/turn produces ~60k events/day in the LLM domain alone. Unbounded retention isn't sustainable.
2. **Compliance.** SOC2 expects 1 year of audit logs. GDPR expects deletion of PII on request. These are *opposite* pressures on the same store — operational events should be aggressively prunable; security-relevant events should be tamper-evident and durable.

The Wave 12 answer is to split the same physical store into two **logical tiers** by tagging which event types are audit-relevant. The retention sweep prunes the operational tier; the audit tier is preserved indefinitely (or under a separate, much longer policy).

Audit log = filtered projection of trace events, *not* a parallel write path. This matches [`analytics-api.md §2.1.5`](analytics-api.md)'s rule and [`multi-user.md §7.1`](multi-user.md)'s explicit guidance: **the trace store is the source of truth.**

---

## 2. Goals and non-goals

### 2.1 Goals

1. **One physical store, two logical tiers.** No parallel write path, no duplicated state. Audit-relevance is a flag on each event type in the catalog.
2. **Append-only invariant.** Audit events are never mutated. They survive every retention sweep. Re-emission produces a new row with a fresh event id; the prior row is preserved.
3. **Deterministic export.** Same input window, same registry → byte-identical export. Buyers can checksum exports and diff them across SIEM imports.
4. **SIEM-friendly shape.** JSONL (one event per line, RFC 8259 compliant) and CSV (RFC 4180) — both ubiquitous SIEM ingest formats. No proprietary container.
5. **Cheap.** The audit log is a SELECT over a `WHERE type IN (...)` index — no precomputation, no separate table, no migration.

### 2.2 Non-goals

1. **No cryptographic tamper-evidence in v1.** SOC2 wants append-only logs but does not strictly require hash-chained event ids — operator-controlled access to the SQLite file is the trust boundary in v1. Hash-chained ids are sketched as an open question.
2. **No automatic SIEM push.** The export is a file. Buyers wire `metis audit export` into their cron / pipeline / CI; we don't ship a SIEM transport.
3. **No PII redaction inside the export.** Audit events are already `pseudonymous` floor per their catalog entries — they carry stable ids, not plaintext. The trace store has no plaintext PII to redact (per [`multi-user.md §3.3`](multi-user.md)).
4. **No retention enforcement in this spec.** That lands in 12a-2. This spec defines the *flag*; the sweep reads the flag.
5. **No right-to-delete pathway in v1.** Audit events are append-only and outlive retention. GDPR "delete my user" against the audit tier is documented as an open question; the closest equivalent is `users.json` deletion + a window purge in the operational tier only.

---

## 3. Definition

An **audit event** is a trace event whose type is flagged `audit: True` in the payload registry. Three properties distinguish it from a vanilla trace event:

1. **Retention-exempt.** The retention sweep ([`event-bus-and-trace-catalog.md §7.3`](event-bus-and-trace-catalog.md); implementation 12a-2) MUST NOT delete rows whose `type` is in `AUDIT_EVENT_TYPES`.
2. **Exportable as a unit.** `AuditLog.export(window)` projects every audit event in a window into a SIEM-ready file without joining to operational events.
3. **Semantic stability.** Audit event payloads are subject to the same backwards-compatibility discipline as the rest of the catalog (additive fields are fine; removing or renaming a field is a major version bump). Exports across catalog versions remain parseable.

Audit-relevance is **not** a sensitivity classification. The sensitivity floors (`private` / `user_controlled` / `pseudonymous` / `aggregatable` per [`event-bus-and-trace-catalog.md §4.4`](event-bus-and-trace-catalog.md)) are orthogonal — every audit event in v1 happens to be `pseudonymous`-floor, but that's an outcome of the chosen subset, not a rule. A future audit-relevant event with a `private` floor (e.g., a confirmation prompt that captures the projected command line) would still be audit-relevant.

---

## 4. Event taxonomy

The v1 audit subset. Each event is already in the catalog ([`event-bus-and-trace-catalog.md §6`](event-bus-and-trace-catalog.md)); audit-relevance is a derived metadata flag, not a new event family.

| Event type                    | Why audit-relevant                                                                  | Catalog §          |
|-------------------------------|-------------------------------------------------------------------------------------|--------------------|
| `gateway.key_issued`          | Credential creation. Who got what scope and when.                                   | §6.13              |
| `gateway.key_revoked`         | Credential lifecycle. Who lost access, when, and why.                               | §6.13              |
| `gateway.key_rotated`         | Credential rotation. Predecessor/successor lineage for auditor traceback.           | §6.13              |
| `gateway.quota_exceeded`      | Hard-cap rejection. Buyer needs to demonstrate budget enforcement worked.           | §6.4 / multi-user §7.2 |
| `quota.alert`                 | Soft alert (80% / 95%). Demonstrates "we warned before we cut off."                 | §6.4 / multi-user §5 |
| `routing.policy_invalid`      | Policy compliance event. Records when configured routing rules failed to parse.     | §6.5               |
| `memory.eviction`             | Resource-cap fired. Buyer needs evidence the bounded-memory guarantee held.         | §6.7               |
| `pattern.evicted`             | Bounded pattern store cap fired. Same shape as memory.eviction.                     | §6.5b              |
| `tool.confirmation_resolved`  | Records WRITE / EXECUTE / NETWORK consent (or denial). Operator chain-of-custody.   | §6.4               |
| `trace.swept`                 | Retention-sweep audit (Wave 12a-2). Self-referential so subsequent sweeps preserve sweep history. | §6.14   |
| `analytics.user_exported`     | GDPR portability operation. Records every subject-data export.                      | §6.9.1 / analytics-api §4.10 |
| `analytics.user_forgotten`    | GDPR right-to-delete operation. Records every forget call with the redaction count. | §6.9.1 / multi-user §7.4.4 |

**Not in v1 audit:**

- `session.created` — high volume; the identity binding is already on `llm.call_completed` / `turn.completed` via `user_id` / `team_id`, so per-session audit rows would be redundant.
- `eval.completed` / `eval.failed` — operational quality telemetry, not compliance. (The brief's mention of `judge_cost > threshold` is more naturally a cost-watch query than an audit dimension.)
- `route.decided` — exactly one per turn. Audit-relevance would balloon volume; the `routing.policy_invalid` event already covers the compliance-failure case.

The set is intentionally small. Adding an event to the audit subset is a deliberate spec change with a CHANGES.md entry. Removing one is a breaking change — exports across the version boundary will diff.

---

## 5. Storage

**Pure derived view over the trace DB. No parallel state.**

The implementation lives in [`packages/metis-core/src/metis_core/audit/`](../../packages/metis-core/src/metis_core/audit/) as a thin reader on top of `TraceStore`:

```python
class AuditLog:
    def __init__(self, trace: TraceStore) -> None: ...

    def query(
        self,
        *,
        window: TimeWindow,
        event_types: Iterable[str] | None = None,
    ) -> Iterator[Event]: ...

    def export(
        self,
        dest: Path,
        *,
        window: TimeWindow,
        format: Literal["jsonl", "csv"] = "jsonl",
        event_types: Iterable[str] | None = None,
    ) -> AuditExportResult: ...
```

`AuditLog` owns no schema, no migration, no separate write path. It reads via a single indexed query:

```sql
SELECT * FROM events
WHERE type IN (<audit_event_types>)
  AND timestamp_us BETWEEN ? AND ?
ORDER BY id
```

The `(type, timestamp_us)` index from [`event-bus-and-trace-catalog.md §7.1`](event-bus-and-trace-catalog.md) covers this query directly.

### 5.1 The `is_audit` flag

Audit-relevance is a parallel constant in `metis_core.events.payloads`, sibling to `PAYLOAD_REGISTRY`:

```python
AUDIT_EVENT_TYPES: frozenset[str] = frozenset({
    "gateway.key_issued",
    "gateway.key_revoked",
    "gateway.key_rotated",
    "gateway.quota_exceeded",
    "quota.alert",
    "routing.policy_invalid",
    "memory.eviction",
    "pattern.evicted",
    "tool.confirmation_resolved",
    "trace.swept",                # retention sweep audit; self-preserving
    "analytics.user_exported",    # GDPR portability
    "analytics.user_forgotten",   # GDPR right-to-delete
})

def is_audit_event(event_type: str) -> bool:
    return event_type in AUDIT_EVENT_TYPES
```

Conceptually this is the `audit: bool = False` metadata the brief calls for — implemented as a `frozenset` so existing `PAYLOAD_REGISTRY` consumers don't need to change their tuple-unpacking call sites. `is_audit_event()` is the single read API for retention sweeps, the audit log, and any future filter. `metis_core.trace.retention` re-exports it so the sweep code reads a single source of truth.

### 5.2 Why not a parallel table

Three options were considered:

1. **Separate SQLite DB.** Double-write at emit time. Rejected — duplicates state, two stores can drift, doubles the fast-path budget on every emit.
2. **Separate table in the same DB.** Single transaction can write both rows. Rejected — still doubles the row count, and the index over `(type, timestamp_us)` on `events` already supports the query in O(log n).
3. **Derived view (chosen).** No schema change. Retention sweep filters its DELETE; audit export filters its SELECT. Same source of truth.

The retention sweep (12a-2) adds the dual `WHERE type NOT IN (<audit>)` filter to its DELETE statement. That's the only coordination point.

---

## 6. Append-only invariant

Audit events are never mutated. Three places enforce this:

1. **The trace store itself.** `TraceStore.write()` is INSERT-only ([`trace/store.py`](../../packages/metis-core/src/metis_core/trace/store.py)); no UPDATE / DELETE paths exist on the public API today. This invariant is structural, not policed.
2. **The retention sweep (12a-2).** Its DELETE statement MUST filter audit types. Test in 12a-2 asserts that a sweep over a fixture containing both kinds removes only the operational rows.
3. **Re-emission produces new rows.** A `gateway.key_revoked` event for the same key emitted twice (e.g., manual revoke then grace-period expiry) appears as two distinct audit rows with distinct event ids. The audit log preserves both — the operator sees the full lifecycle.

The trace store remains SQLite-WAL; the audit log inherits the same durability trade-off (`~1s` window on hard crash, [`event-bus-and-trace-catalog.md §7.2`](event-bus-and-trace-catalog.md)). For a buyer who cannot accept that window, a future option is to switch the trace DB to `synchronous=FULL` (slower fast-path, no loss). The audit-vs-operational split is independent of that knob.

---

## 7. Export

### 7.1 JSONL

One event per line, UTF-8 encoded. Each line is a JSON object with the full event envelope plus payload:

```jsonl
{"id":"01JJX...","timestamp":"2026-05-15T12:34:56.789012+00:00","session_id":"sess_01JJX...","turn_id":null,"parent_event_id":null,"type":"gateway.key_issued","actor":"system","sensitivity":"pseudonymous","payload":{"gateway_key_id":"gk_...","name":"alice-prod","workspace_path":"/srv/alice","issued_at":"2026-05-15T12:34:56.789+00:00","user_id":"usr_...","team_id":"team_...","allowed_models":null,"daily_cap_usd":null,"monthly_cap_usd":null}}
```

`Decimal` fields serialize as JSON strings (matching the canonical format convention from [`canonical-message-format.md §6.4`](canonical-message-format.md)). `datetime` fields serialize as ISO 8601 with timezone. Sort order is event-id ascending (ULID-sortable = timestamp ascending).

### 7.2 CSV

RFC 4180. Header row is fixed:

```csv
id,timestamp,session_id,turn_id,parent_event_id,type,actor,sensitivity,payload_json
```

The `payload_json` column holds the same JSON object as JSONL's `payload` field, embedded as a CSV-quoted string. SIEMs that ingest CSV typically re-parse this column for the payload fields. Including `payload_json` rather than flattening it into per-payload-type columns means the CSV schema is stable across catalog evolution — flattening would force a re-header on every additive payload change.

### 7.3 Determinism

Same input window, same `AUDIT_EVENT_TYPES`, same trace DB → byte-identical export. Three rules enforce this:

1. ORDER BY `id` (lexicographic ULID = timestamp + monotonic counter). No `ORDER BY timestamp_us` (would collide within a microsecond).
2. JSON field order is fixed by msgspec's struct field order (declaration order in the payload class). No `sort_keys` — we use msgspec, which preserves field order.
3. No timestamps embedded in the output other than the events' own. The `AuditExportResult` metadata block is *returned*, not written to the file.

Buyers can `sha256sum` exports across runs and diff them.

### 7.4 Export result metadata

`AuditLog.export()` returns:

```python
@dataclass(frozen=True)
class AuditExportResult:
    dest_path: Path
    format: Literal["jsonl", "csv"]
    event_count: int
    window_start: datetime
    window_end: datetime
    oldest_event_id: str | None
    newest_event_id: str | None
    byte_count: int
```

Identical shape to `BackupResult` ([`trace/backup.py`](../../packages/metis-core/src/metis_core/trace/backup.py)) so the operator's mental model is the same. The CLI prints a deterministic block (no random ids in the output).

---

## 8. API surface

```python
from metis_core.audit import AuditLog, AuditExportResult, is_audit_event
from metis_core.analytics.windows import TimeWindow
from metis_core.trace.store import TraceStore

trace = TraceStore("~/.metis/metis.db")
audit = AuditLog(trace)

# Ad-hoc query
window = TimeWindow(start=datetime(2026, 5, 1, tzinfo=UTC), end=datetime(2026, 6, 1, tzinfo=UTC))
for event in audit.query(window=window, event_types={"gateway.key_revoked"}):
    print(event.id, event.payload["reason"])

# Export to JSONL
result = audit.export(Path("/srv/exports/may.jsonl"), window=window, format="jsonl")
print(f"exported {result.event_count} events to {result.dest_path}")
```

### 8.1 `event_types` filter semantics

Defaults to `AUDIT_EVENT_TYPES` (the full audit subset). If the caller passes an explicit set, that set is intersected with `AUDIT_EVENT_TYPES` — passing a non-audit type silently filters it out rather than including it. This is a defensive choice: the audit log is for audit events; if a caller passes `quota.alert` plus `llm.call_completed`, the latter is dropped without an error. (Mistakes in scripts shouldn't accidentally include operational telemetry in an audit export.)

A stricter mode (raise on non-audit type) is left as an open question; not exercised in v1.

---

## 9. CLI

`metis audit export <dest>` lives under the existing top-level CLI. Flags:

```
metis audit export PATH
  [--db-path PATH]              # source trace DB; default ~/.metis/metis.db
  [--format {jsonl,csv}]        # default jsonl
  [--since ISO_8601]            # inclusive start of window
  [--until ISO_8601]            # exclusive end of window
  [--event-type TYPE ...]       # optional filter; defaults to all audit types
  [--redact MODE]               # passthrough | pseudonymize | redact_private
                                # | aggregate_only — see redaction.md §2.
                                # Default: passthrough.
```

Output on success (stdout, deterministic):

```
audit export complete
  destination:    /srv/exports/may.jsonl
  format:         jsonl
  redact mode:    passthrough
  events:         42
  window start:   2026-05-01T00:00:00+00:00
  window end:     2026-06-01T00:00:00+00:00
  oldest event:   01JJX...
  newest event:   01JJY...
  bytes:          18421
```

Failure: one-line diagnostic to stderr, non-zero exit (mirrors `metis backup` / `metis restore`).

`metis audit query` is reserved for a follow-on if interactive ad-hoc audit becomes common — for v1 the export is the primary interface and `query()` is library-only.

---

## 10. Coordination with retention (12a-2)

The retention sweep MUST honor the append-only invariant. The integration point is `AUDIT_EVENT_TYPES`:

```python
# Pseudocode in the retention sweep (12a-2):
conn.execute(
    """DELETE FROM events
       WHERE timestamp_us < ?
         AND type NOT IN (... AUDIT_EVENT_TYPES ...)""",
    (retention_cutoff_us,),
)
```

A test in 12a-2 (`test_retention_sweep_preserves_audit_events`) seeds the trace DB with both kinds, runs the sweep, asserts the audit rows survive and the operational rows are gone. The two specs interlock via the `AUDIT_EVENT_TYPES` constant — change the constant, both specs' behavior moves.

If 12a-2 lands later, the audit log still ships end-to-end; only the retention guarantee is incomplete. The invariant is: *until retention is wired, all trace events are de-facto preserved, which trivially satisfies "audit events are preserved."*

---

## 11. SOC2 / GDPR posture

These are not full answers — they're the posture v1 commits to. A future audit-export spec (or a Phase 4 compliance hardening pass) revisits each.

1. **Retention period.** v1 does not commit to a specific retention period for audit events; they are preserved indefinitely. A buyer can run `metis backup` on a schedule for cold storage. The 1-year SOC2 baseline is comfortably satisfied for any deployment under ~100k audit events / year (single-file SQLite handles that without effort).
2. **Tamper-evidence.** v1 trusts SQLite WAL append-only + operator file permissions. Hash-chained event ids (each event's id derived from `H(prev_id || payload)`) is an open question; the cost is non-trivial (changes the id contract from ULID to hash), and the threat model — an operator with filesystem access tampering with their own audit logs — is debatable in scope.
3. **Plaintext PII.** The trace store carries no plaintext email (multi-user.md §3.3 invariant). The audit export inherits this. Display names *can* be joined at export time by reading `users.json` if a buyer wants human-readable audit reports; this is *not* the default — the default export carries stable `user_id` only.
4. **Right-to-delete.** GDPR "delete my user" against the audit tier is unresolved. v1's closest equivalent: `users.json` deletion + revocation of all keys for that user (`metis gateway revoke-key` per key). The audit events for that user persist; the user record they reference becomes a tombstone. This is documented as a gap, not a solution.

---

## 12. Open questions

These are **live**. The owner closes them when evidence shows up; agents working in the repo should surface them, not pick.

1. **Hash-chained event ids.** Whether to add cryptographic tamper-evidence to the audit tier. Tradeoff: stronger SOC2 story vs. id contract churn (ULID → hash + ULID is feasible; pure hash is not, ids need monotonic ordering).
2. **Retention period for audit events.** Indefinite in v1 vs a configurable cap (e.g. 7 years per Sarbanes-Oxley). Defer until a buyer asks.
3. **Strict vs lax `event_types` filter.** Passing a non-audit type silently filters it (v1) vs. raising. Strict is safer; lax matches the "audit log is for audit events" mental model.
4. **CSV column flattening.** Per-payload-type columns would be more SIEM-friendly for buyers who don't re-parse the embedded JSON. v1 keeps `payload_json` as a single column for schema stability.
5. **Should `route.decided` be audit-relevant when `chain[].policy == "rule"` fires?** Records when configured policy actually steered a request. v1 says no (volume); a future "policy enforcement" hardening pass may revisit.
6. **Right-to-delete pathway.** Surface as an audit-export option (`--exclude-user-id usr_…` plus a `users.json` tombstone) vs treating audit as immutable. Couples to legal posture.

---

## 13. Decision log

| Date       | Decision                                                                | Rationale                                                                                                          |
|------------|-------------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------|
| 2026-05-15 | Audit log is a derived view over the trace store, not a parallel write  | Single source of truth; matches multi-user.md §7.1 and analytics-api.md §2.1.5. No new fast-path budget cost.       |
| 2026-05-15 | `AUDIT_EVENT_TYPES` is a `frozenset[str]` parallel to `PAYLOAD_REGISTRY` | Conceptually the `audit: bool = False` metadata the brief calls for; implemented as a sibling constant so existing `PAYLOAD_REGISTRY` unpacking call sites don't churn. |
| 2026-05-15 | v1 subset is 9 types (key lifecycle + quota + policy + eviction + confirmation) | Captures credential changes, budget enforcement, policy failures, resource-cap fires, and consent records. Excludes high-volume operational types (`llm.*`, `tool.*`, `turn.*`) and quality-telemetry types (`eval.*`). |
| 2026-05-15 | JSONL + CSV; both deterministic                                          | Ubiquitous SIEM ingest formats; deterministic so buyers can checksum across runs. Proprietary container would block SIEM compat. |
| 2026-05-15 | Append-only invariant is enforced by the retention sweep (12a-2)         | This spec defines the flag; the sweep reads it. Avoids designing retention here.                                    |
| 2026-05-15 | No automatic SIEM push in v1                                             | The export is a file; buyer wires their own pipeline. Transport is a Phase 4+ concern.                              |
| 2026-05-15 | No cryptographic tamper-evidence in v1                                   | Operator-controlled SQLite file is the trust boundary; hash-chain is an open question. SOC2 typically accepts operator-controlled append-only logs. |
| 2026-05-15 | No PII redaction in the export                                           | Trace store carries no plaintext PII (multi-user.md §3.3 invariant); audit inherits this for free.                  |
| 2026-05-15 | Lax `event_types` filter (silently drop non-audit types)                 | Defensive: a typo in a buyer's cron script shouldn't accidentally include operational telemetry.                    |

---

## 14. Testing strategy

### 14.1 Required tests

1. **Round-trip JSONL.** Emit one of each audit event type via the bus, export to JSONL, parse line by line, verify each parsed line matches the original event envelope.
2. **Round-trip CSV.** Same shape; verify the embedded `payload_json` column re-parses to the original payload dict.
3. **Determinism.** Same input window, same registry → byte-identical JSONL and CSV. Run twice; assert `sha256` matches.
4. **`is_audit_event` membership.** For every type in `AUDIT_EVENT_TYPES`, `is_audit_event(t) == True`. For every type in `PAYLOAD_REGISTRY` *not* in the audit set, `is_audit_event(t) == False`. Defends against silent drift.
5. **Audit filter rejects non-audit types.** `AuditLog.export(event_types={"llm.call_completed"})` produces an empty export (the requested type is not in `AUDIT_EVENT_TYPES`).
6. **Append-only via retention sweep.** (Depends on 12a-2.) Seed a DB with N audit events + M operational events, run the retention sweep with a cutoff that would prune both, assert N audit events remain and M operational events are gone.
7. **Window bounds.** Events emitted before `window.start` and after `window.end` do not appear in the export.
8. **Empty window.** `AuditLog.export()` over an empty window produces a zero-byte JSONL file (or a CSV file with only the header row) and an `event_count=0` result; no exception.
9. **Catalog cross-check.** Every type in `AUDIT_EVENT_TYPES` MUST be a key in `PAYLOAD_REGISTRY`. A test enforces this so a typo in the audit set fails CI rather than silently produces no-op queries.
10. **CLI parsing.** `metis audit export PATH --since ... --until ... --format csv` parses to the right `argparse.Namespace`.
11. **CLI end-to-end.** Drive `main(["audit", "export", PATH, ...])`, assert exit 0, assert output file shape.

### 14.2 Property tests

- **Subset closure.** `AUDIT_EVENT_TYPES ⊆ PAYLOAD_REGISTRY.keys()`. (Enforced as a unit test rather than a property test; cheap to run on every CI cycle.)

---

## 15. References

- [`event-bus-and-trace-catalog.md §6`](event-bus-and-trace-catalog.md) — the closed event catalog; audit subset is a derived flag, not a parallel family.
- [`event-bus-and-trace-catalog.md §7.3`](event-bus-and-trace-catalog.md) — retention policy sketch; this spec interlocks with 12a-2's implementation.
- [`event-bus-and-trace-catalog.md §7.5`](event-bus-and-trace-catalog.md) — backup/restore; identical operator UX pattern.
- [`multi-user.md §7`](multi-user.md) — the "audit + compliance posture" framing this spec implements.
- [`gateway.md §11`](gateway.md) — `gateway.key_*` event types, the load-bearing audit records.
- [`analytics-api.md §2.1.5`](analytics-api.md) — "catalog-sourced data is the only source" rule.
- [`canonical-message-format.md §6.4`](canonical-message-format.md) — `Decimal` serialization convention; reused by export.
- [`STRATEGY.md §2`](../STRATEGY.md) — buyer ≠ user; SOC2 / GDPR is the buyer's ask.
