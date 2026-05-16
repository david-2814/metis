> **Status:** v1 — drafted 2026-05-15 (Wave 12). `EventRedactor` module + `metis user forget` CLI + `metis audit export --redact <mode>` CLI shipped together. The export-time redactor is a new layer; the GDPR-forget path extends the existing `Redactor` Protocol and `PseudonymizingRedactor` shipped by 12a-2 (`analytics-api.md §4.10`) and reuses the existing `analytics.user_forgotten` / `analytics.user_exported` audit events on the catalog.

# Redaction layer for trace exports

Composes with the existing `Sensitivity` enum ([`event-bus-and-trace-catalog.md §4.4`](event-bus-and-trace-catalog.md)) and the append-only trace store ([`event-bus-and-trace-catalog.md §7`](event-bus-and-trace-catalog.md)) to produce GDPR/SOC2-aligned exports. The append-only invariant is preserved at the *recording* layer — redaction only mutates events at *export time*, never in flight. The one explicit exception is [§5 GDPR right-to-be-forgotten](#5-gdpr-right-to-be-forgotten), which performs a narrowly-scoped in-place identity overwrite documented as a deliberate deviation from append-only.

## 1. Why this exists

Today the trace export ([`event-bus-and-trace-catalog.md §7`](event-bus-and-trace-catalog.md) gives operators raw rows) includes every payload verbatim. Three classes of buyer need more:

1. **Per-user data export.** A GDPR Article 15 ("right of access") request needs all of one user's events, in a portable shape.
2. **Right to be forgotten.** GDPR Article 17 needs identity fields pseudonymized for one `user_id`; aggregate analytics survives, the link to the natural person does not.
3. **Sensitivity-graded shared exports.** A support engineer triaging an incident needs trace context, not the customer's prompts. Redacting `PRIVATE`-tier text fields preserves diagnostic value while satisfying contract obligations.

This spec defines the redaction *layer*; the data path (what produces the input stream) is owned by [`audit-log.md`](audit-log.md). The redactor accepts an `Iterable[Event]` and yields redacted `Event`s — it does not know or care whether they came from `TraceStore`, an `AuditLog` projection, or a unit-test fixture.

## 2. Modes

| Mode               | What it does                                                                                                | Common use                                                |
|--------------------|-------------------------------------------------------------------------------------------------------------|-----------------------------------------------------------|
| `passthrough`      | No redaction; returns events verbatim. Default for in-process audit / single-operator deployments.          | Self-hosted single-tenant, no compliance posture.         |
| `pseudonymize`     | SHA-256-hashes identity fields (`user_id`, `team_id`, `session_id`, `parent_session_id`, `workspace_*`, `gateway_key_id`). Timestamps, costs, token counts, model names kept verbatim. | SIEM ingestion, cross-team support handoff.               |
| `redact_private`   | `pseudonymize` **plus** strip `PRIVATE`-tier text fields (user prompts, tool input/output text, error messages) to the sentinel `"[REDACTED]"`. | Buyer-trial demo data, customer-supplied reproduction packs. |
| `aggregate_only`   | Drop per-row payloads entirely. Emits aggregate-only summary (count, sum, min/max of cost/tokens/latency, distinct sessions and users). | Vendor reporting (your bill from us), ROI presentations. |

Modes are mutually exclusive; one redactor instance owns one mode. The redactor is stateless across event boundaries (except for the `aggregate_only` accumulator); two redactors with the same mode applied to the same input produce byte-identical output.

## 3. Per-field redaction policy

The policy is a declarative table keyed by event type. The redactor never invents field names — every entry mirrors the typed payload struct in [`events/payloads.py`](../../packages/metis-core/src/metis_core/events/payloads.py).

### 3.1 Identity fields (pseudonymized under `pseudonymize` and stricter)

| Field             | Source                                                          | Hashed?                                |
|-------------------|-----------------------------------------------------------------|----------------------------------------|
| `session_id`      | `Event.session_id` envelope field                               | Yes (same hash → same input)           |
| `turn_id`         | `Event.turn_id`                                                 | Yes                                    |
| `user_id`         | `LLMCallCompleted`, `TurnCompleted`, `GatewayQuotaExceeded`, `GatewayKeyIssued` | Yes                |
| `team_id`         | same                                                            | Yes                                    |
| `gateway_key_id`  | `LLMCallCompleted`, `GatewayQuotaExceeded`, `GatewayKeyRevoked`, `GatewayKeyRotated` | Yes           |
| `parent_session_id` | `LLMCallStarted`, `LLMCallCompleted`, `TurnCompleted` (delegation dim) | Yes                          |
| `workspace_path`  | `SessionCreated`, `GatewayKeyIssued`                            | Yes                                    |
| `workspace_hash`  | `SessionCreated`, `SessionResumed`                              | **Already hashed; left as-is**         |
| `request_id`      | `LLMCallStarted`                                                | Yes                                    |
| `Event.id`, `Event.parent_event_id` | envelope                                       | **No — kept verbatim for chain reconstruction within the export** |

Hashing format matches what 12a-2 shipped for `forget_user` (`redaction/default.py::pseudonym_for`): `f"redacted_{sha256(value).hexdigest()[:12]}"`. Same value → same hash across redactor invocations (determinism is a hard contract — see [§7](#7-invariants)). An `EventRedactor(mode, salt=...)` accepts an optional bytes salt for non-correlatable exports (different exports of the same data produce different hashes); without a salt, the hash is content-addressable and matches the GDPR-forget pseudonym byte-for-byte (so a row pseudonymized by `forget_user` and re-exported under `pseudonymize` produces the same `user_id` value either way).

### 3.2 PRIVATE-tier text fields (replaced with `"[REDACTED]"` under `redact_private`)

| Event type                     | Fields redacted                                                                       |
|--------------------------------|---------------------------------------------------------------------------------------|
| `turn.started`                 | `user_message_text_redacted`                                                          |
| `tool.completed`               | `files_modified`, `command_executed`                                                  |
| `tool.failed`                  | `error_message`                                                                       |
| `tool.confirmation_requested`  | `input_summary`, `command_summary`, `projected_modifications`                         |
| `llm.call_failed`              | `error_message_redacted` (already adapter-redacted; redactor enforces sentinel)       |
| `turn.completed`               | inside `signals_extra`: keys `user_prompt_text` and `assistant_response_text` (sibling to grounding-check) |

All `tool.called.input_hash`, `pattern.*.fingerprint_id`, and similar hash-form fields are kept verbatim — hashes are not personally-identifying content on their own.

### 3.3 USER_CONTROLLED fields

Kept by default (the user opted in to sharing them). Configurable per `Redactor(mode, strip_user_controlled=True)` for operators who want a stricter contract. When stripped, the replacement is `"[REDACTED]"` (same sentinel as PRIVATE).

### 3.4 PSEUDONYMOUS / AGGREGATABLE fields

Kept verbatim under every non-`aggregate_only` mode. These are structural metadata (token counts, model names, durations, success scores) by design ([`event-bus-and-trace-catalog.md §4.4`](event-bus-and-trace-catalog.md)).

## 4. The buyer-trial recipe

```bash
# Pseudonymized SIEM-importable JSONL for a date range.
metis audit export \
    --since 2026-05-01T00:00:00Z \
    --until 2026-05-15T23:59:59Z \
    --redact pseudonymize \
    --output /tmp/metis-trace-export.jsonl

# Single user, full content, for a GDPR Article 15 request.
metis audit export --user-id usr_01HZA... --redact passthrough --output ...

# Aggregate-only billing report (no per-row payloads leave the box).
metis audit export --redact aggregate_only --output /tmp/billing.json
```

Output format is JSON Lines (one event per line) for `passthrough` / `pseudonymize` / `redact_private`; a single JSON object for `aggregate_only`. Both pass `jq` and standard SIEM ingestors. The `--output` argument is required for `aggregate_only` (no streaming shape) and optional for the row-shaped modes (stdout fallback).

## 5. GDPR right-to-be-forgotten

```bash
metis user forget <user_id> --confirm
```

CLI wrapper over the `Redactor` Protocol shipped by 12a-2 ([`packages/metis-core/src/metis_core/redaction/protocol.py`](../../packages/metis-core/src/metis_core/redaction/protocol.py)) and the HTTP surface in [`analytics-api.md §4.10`](analytics-api.md). Replaces `user_id` in every matching event's payload with `pseudonym_for(user_id)` and emits one `analytics.user_forgotten` event (already on the catalog) recording: the original `subject_user_id`, the deterministic `pseudonym`, the count of rows pseudonymized, and the caller (`requested_by=None` for the CLI, matching the loopback-dashboard convention).

**This is the one place in Metis that violates the append-only trace store invariant.** It is deliberate, narrowly scoped, and documented:

1. Only the `user_id` JSON field is touched, on every event row that carries it (the matcher is `json_extract(payload_json, '$.user_id') = ?` — generic across event types). All other content (timestamps, cost, tokens, hashed identifiers, `team_id`) is unchanged.
2. The replacement is `pseudonym_for(user_id)`, the same deterministic SHA-256 truncate the `pseudonymize` export mode emits. The per-user aggregate continues to roll up under one (now-pseudonymous) bucket; the bridge back to the natural person is severed.
3. The `--confirm` flag is mandatory. The command refuses to run without it, prints the count of events that *would* be affected, and returns with a non-zero exit code so the operator can validate scope before re-running with `--confirm`.
4. The `analytics.user_forgotten` event lands with `Sensitivity.PSEUDONYMOUS` floor: payload `(subject_user_id, pseudonym, requested_by, pseudonymized_rows)`. Idempotent re-calls still emit an audit event with `pseudonymized_rows = 0` so the audit trail records every request, not just the first.
5. Subsequent `metis audit export --user-id <user_id>` for the forgotten user matches zero events. Subsequent export filtered by the *hash* still returns the pseudonymized rows.

The append-only invariant exception is locally contained: the UPDATE runs inside a single SQL transaction. Idempotent on re-runs: the second call's `WHERE user_id = ?` matches zero rows (the original id was already rewritten to its hash), so `pseudonymized_rows = 0`.

## 6. Module shape

```
packages/metis-core/src/metis_core/redaction/
├── __init__.py        # public exports
├── protocol.py        # Redactor Protocol (shipped by 12a-2; unchanged)
├── default.py         # PseudonymizingRedactor (shipped by 12a-2; unchanged)
├── modes.py           # RedactionMode StrEnum + field policy table  (NEW)
├── event_redactor.py  # EventRedactor class for export-time redaction (NEW)
└── aggregator.py      # AggregateAccumulator for AGGREGATE_ONLY mode  (NEW)
```

Public surface:

```python
from metis_core.redaction import (
    RedactionMode,           # PASSTHROUGH | PSEUDONYMIZE | REDACT_PRIVATE | AGGREGATE_ONLY
    EventRedactor,           # per-event export-time redactor (this spec)
    AggregateAccumulator,    # rolls up events in AGGREGATE_ONLY mode
    PseudonymizingRedactor,  # GDPR-forget pathway (12a-2)
    Redactor,                # Protocol (12a-2)
    pseudonym_for,           # deterministic identity hash (12a-2)
    REDACTED_SENTINEL,       # "[REDACTED]"
)

redactor = EventRedactor(RedactionMode.PSEUDONYMIZE)
for event in trace.events_for_session(sid):
    redacted: Event | None = redactor.redact(event)
    if redacted is not None:
        sink.write(redacted)
agg: dict | None = redactor.finalize()  # populated only for AGGREGATE_ONLY
```

`EventRedactor.redact(event)` returns `None` only in `AGGREGATE_ONLY` mode (the event is folded into the accumulator instead). In every other mode it returns a new `Event`; the input is never mutated (events are `msgspec.Struct(frozen=True)` so this is structurally enforced).

## 7. Invariants

1. **Determinism.** `Redactor(mode, salt=s).redact(event)` is a pure function of `(mode, salt, event)`. No clock reads, no random sources, no environment dependencies.
2. **Idempotence.** `Redactor(mode).redact(Redactor(mode).redact(event))` ≡ `Redactor(mode).redact(event)`. Already-redacted text fields (matching `REDACTED_SENTINEL`) and already-prefixed pseudonyms (matching the `ps:<tag>:` prefix) are passed through unchanged. Tested directly.
3. **Append-only at recording.** The redactor never mutates the trace DB. The one carved-out exception is `forget_user`, which is invoked through its own explicit CLI command, not as a side effect of export.
4. **Hash determinism.** `pseudonymize_value(v, tag)` is `f"ps:{tag}:{sha256(v + salt).hexdigest()[:16]}"`. With `salt=None` the hash is content-addressable (same input across all exports). With a salt set, the hash is correlatable only within exports using that salt.
5. **Envelope structure preserved.** `Event.id`, `Event.parent_event_id`, `Event.timestamp`, `Event.actor`, `Event.type`, `Event.sensitivity` are never modified (the chain walk and the catalog lookup must continue to work post-redaction). Only `session_id`, `turn_id`, and `payload` fields are subject to redaction.
6. **Sensitivity tag is informational, not gating.** The redactor reads `Sensitivity` to choose what to do, but the mode (not the tag) governs the output. An event with a `PRIVATE` tag is *not* automatically scrubbed under `pseudonymize` — only `redact_private` strips PRIVATE-tier text fields. This mirrors the catalog's design ([`event-bus-and-trace-catalog.md §4.4`](event-bus-and-trace-catalog.md)): the tag describes what the event *could* contain, not what callers must do about it.
7. **Forget is irreversible.** Once `forget_user` has run, the original `user_id` is gone from the DB. Re-running with the original `user_id` is a no-op (zero matches). This is the GDPR contract; document it loudly in the CLI confirmation prompt.

## 8. Event catalog (no additions)

The catalog already carries `analytics.user_forgotten` and `analytics.user_exported` ([`event-bus-and-trace-catalog.md §6.9.1`](event-bus-and-trace-catalog.md), shipped by 12a-2). The `metis user forget` CLI emits the existing event verbatim — no new event type is added by this spec. Reusing the existing event keeps the audit-trail invariant (one event class per audit-relevant action) and matches the HTTP surface at `/analytics/user/{user_id}/forget`.

## 9. Integration points

| Surface                                            | Owner spec                                   | What this spec adds                                                          |
|----------------------------------------------------|----------------------------------------------|------------------------------------------------------------------------------|
| `Redactor` Protocol (`forget_user(user_id)`)       | shipped by 12a-2; this spec doesn't change   | —                                                                            |
| `PseudonymizingRedactor` default impl              | shipped by 12a-2; this spec doesn't change   | —                                                                            |
| `/analytics/user/{user_id}/export` (HTTP)          | [`analytics-api.md §4.10`](analytics-api.md) (12a-2) | Add `?redact=<mode>` query parameter that wraps the JSONL stream through `EventRedactor` before yielding. v1 keeps the parameter optional (default `passthrough`). |
| `/analytics/user/{user_id}/forget` (HTTP)          | [`analytics-api.md §4.10`](analytics-api.md) (12a-2) | —                                                                            |
| `metis audit export` (CLI)                         | this spec                                    | New CLI; reads from `AnalyticsStore.user_export` (no `user_id` filter ⇒ full window) and pipes through `EventRedactor`.    |
| `metis user forget` (CLI)                          | this spec                                    | New CLI; invokes `PseudonymizingRedactor.forget_user` and emits `analytics.user_forgotten`. |

The redactor's input contract (`Iterable[Event]` for `EventRedactor`, `forget_user(user_id) -> int` for the Protocol) is stable; downstream callers (HTTP / CLI / future tooling) compose either contract without touching this module.

## 10. Out of scope for v1

1. **Per-field operator override.** v1 ships fixed PRIVATE-tier strip targets ([§3.2](#32-private-tier-text-fields-replaced-with-redacted-under-redact_private)). A future revision can accept `--keep-field <event_type>.<field>` flags.
2. **Re-identification keys.** Some compliance regimes require a separate "reidentification" key that lets the operator un-redact a specific record on lawful request. v1 hashes are one-way; no escrow.
3. **Differential privacy guarantees on `aggregate_only`.** The aggregate output is a deterministic sum/count/min/max; k-anonymity / DP-noise additions are deferred until a buyer asks. The current aggregate is safe-by-construction for the buyer's own data but not for cross-tenant pooling.
4. **Streaming redaction over the bus.** v1 redacts only at export. A future "redacted-tee" subscriber that produces a parallel pseudonymized stream is plausible (gateway-hardening.md §6 has a related abuse-detection use case) but not in v1.
5. **Forgetting individual sessions or workspaces.** Only `user_id` is supported in v1. Per-session GDPR delete is a separate ask; documented gap.
6. **Multi-user audit-log access controls.** v1 inherits the existing loopback-only posture ([`server-api.md §3.1`](server-api.md)). Splitting "operator can export" from "operator can forget" is RBAC territory ([multi-user.md §8](multi-user.md), item 3).
