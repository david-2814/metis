# Observability — Prometheus `/metrics` surface

**Status:** v1.1, shipped (gateway + server, 2026-05-15; Wave 14a extensions 2026-05-16).

**Owners:** the same operators who run `metis serve` / `metis gateway` in
production. The buyer's k8s ops team.

## 1. Why this exists

`docs/gateway-deployment.md "Observability hooks"` enumerates the
surfaces a Metis operator can lean on today: `GET /healthz` for
liveness, the trace DB for the per-event audit, and the
`/analytics/*` REST family for cost-attribution roll-ups exposed by
the server. None of those is a `/metrics` endpoint, and every k8s
operator running Metis at any non-trivial scale will reach for one
first. This spec closes that gap.

The goal is the smallest endpoint a Prometheus scraper will recognize:
text exposition on `/metrics`, a fixed and bounded set of
counters / gauges / histograms, no per-session label cardinality.
Anything richer (per-turn dashboards, p99 latencies sliced by tool,
flame graphs) keeps living in the trace DB and `/analytics/*`.

## 2. Surface

### 2.1 Endpoint

Mounted on **both** apps:

| App | Path | Bind |
|-----|------|------|
| `metis-server` | `GET /metrics` | loopback (server-api.md §3.1) |
| `metis-gateway` | `GET /metrics` | loopback (gateway.md §3.2) |

Content-Type matches `prometheus_client.exposition.CONTENT_TYPE_LATEST`
(`text/plain; version=0.0.4; charset=utf-8` at the time of writing).
No auth, no version header gating; both apps already expose `/healthz`
the same way.

### 2.2 Body

Standard Prometheus exposition format. The bytes are produced by
`prometheus_client.generate_latest(collector.registry)` against a
private per-collector `CollectorRegistry` (so two collectors in the
same process — tests, or a side-by-side gateway+server — don't fight
over the global default registry).

## 3. Metrics

| Name | Type | Labels | Source event | Notes |
|------|------|--------|--------------|-------|
| `metis_llm_calls_total` | counter | `provider, model, status` | `llm.call_completed` (status=`ok`), `llm.call_failed` (status=`error_class`) | `error_class` is the 8-value enum from `provider-adapter-contract.md §6.1`. |
| `metis_llm_call_latency_seconds` | histogram | `provider, model` | both `llm.call_completed` and `llm.call_failed` | Buckets `(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0)`. Observation = `latency_ms / 1000`. |
| `metis_llm_cost_usd_total` | counter | `provider, model` | `llm.call_completed.cost_usd` | `Decimal → float` at the export boundary (Prometheus client doesn't grok `Decimal`). Only positive cost increments are recorded; missing / zero cost rows are skipped. |
| `metis_routing_decisions_total` | counter | `winning_slot, chosen_model` | `route.decided` | `winning_slot = chain[winner_index].policy` — one of the 7 `RoutingPolicyName` literals (`per_message_override` / `manual_sticky` / `rule` / `pattern` / `delegate_request` / `workspace_default` / `global_default`). |
| `metis_pattern_matches_total` | counter | `chose_model, fingerprint_version` | `pattern.matched` | `fingerprint_version` reads `fingerprint_kind` (`structural` / `hybrid` per pattern-store.md §16). Only fires when slot 4 wins (not on `pattern.matched.deferred`, which we don't emit). |
| `metis_quota_used_ratio` | gauge | `identity_kind, identity_id` | `quota.alert`, `gateway.quota_exceeded` | Identity = leading token of `scope` (`key` / `user` / `team`); identity-id resolves from the matching `gateway_key_id` / `user_id` / `team_id` payload field. `gateway.quota_exceeded` pins ratio to `1.0`. |
| `metis_eval_verdicts_total` | counter | `judge_kind, subject_kind` | `eval.completed` | `eval.failed` is **not** counted here — it's a judge-error signal, not a verdict. |
| `metis_session_count` | gauge | (none) | polled at scrape | Server-only. Reads `runtime.session_store.list_sessions()` length. Gateway omits this gauge entirely (it's per-request stateless — no sessions). |
| `metis_gateway_keys_active` | gauge | (none) | polled at scrape | Gateway-only. `sum(1 for k in keystore.keys() if k.is_active(now=…))`. |
| `metis_gateway_keys_revoked` | gauge | (none) | polled at scrape | Gateway-only. Total minus active — captures both explicit revocations (`status="revoked"`) and grace-period-expired keys (still on disk as `active`, but `is_active(now=…)` returns False). |

### 3.1 Cardinality discipline

Labels are deliberately bounded:

* `provider` is the canonical adapter name (`anthropic` / `openai` /
  `openrouter`, plus future static providers).
* `model` is the canonical `provider:name` id (the registry's enumerated
  list — bounded by what the runtime registers at startup).
* `status` (LLM calls) is `ok` plus the 8-value `LLMErrorClass` enum.
* `winning_slot` is the closed `RoutingPolicyName` literal (7 values).
* `chosen_model` mirrors `model`'s bound.
* `fingerprint_version` is the `FingerprintKindLiteral` enum
  (`structural` / `hybrid`).
* `judge_kind` ∈ `heuristic | llm | hybrid`; `subject_kind` ∈
  `turn | tool_cycle | session | workload`.
* `identity_kind` ∈ `key | user | team`.
* `error_class` (LLM errors) is the 8-value `LLMErrorClass` enum;
  `error_class` (tool failures) is the 8-value `ToolErrorClass` enum.
* `tool_name` is bounded by the tool registry (a fixed registration
  list at runtime startup; unmatched dispatches collapse to `unknown`).
* `reason` (gateway auth failures) is the 3-value
  `GatewayAuthFailureReason` enum.

The unbounded labels are `identity_id` on `metis_quota_used_ratio` and
`gateway_key_id` on `metis_gateway_key_cost_usd_total`. Both are
deliberate — per-tenant ratios and per-key spend are interesting at
operator-bounded cardinality (typically <100 keys per deployment) and
a single deployment is unlikely to outgrow Prometheus's per-series
budget on these dimensions. If it does, the operator can drop the
gauge / counter in their scrape config or apply `metricRelabelings` in
the helm chart's `monitoring.serviceMonitor.metricRelabelings`.

Unknown / missing fields collapse to a single `unknown` bucket per
label rather than proliferating series on a malformed event. The one
exception is the per-identity null-bucket convention: agent-loop traffic
without a `gateway_key_id` lands under `gateway_key_id="null"` (not
`"unknown"`) per multi-user.md §3.4 so dashboards distinguish "no key"
from "key field missing on event."

### 3.2 Wave 14a extensions

Production-grade observability additions per the GA-readiness checklist. All
are additive; the v1 metric set is unchanged. Paired with the
`PrometheusRule` alert templates in [`infra/gateway/helm/templates/prometheus-rules.yaml`](../../infra/gateway/helm/templates/prometheus-rules.yaml)
and the runbook in [`docs/operations/observability-runbook.md`](../operations/observability-runbook.md).

| Name | Type | Labels | Source event | Notes |
|------|------|--------|--------------|-------|
| `metis_routing_decision_latency_seconds` | histogram | (none) | `route.decided.elapsed_ms` | Wall-time of the routing engine itself. Buckets span 100µs–500ms (`_ROUTING_LATENCY_BUCKETS_SECONDS`). Typically sub-millisecond; tails out under K-NN cluster-tightening regimes. |
| `metis_tool_call_latency_seconds` | histogram | `tool_name` | both `tool.completed` and `tool.failed` | Tool dispatcher wall-time. `tool_name` is correlated from the prior `tool.called` event via a bounded in-collector LRU (`_TOOL_NAME_CACHE_MAX=1000`); unmatched completes/fails collapse to `tool_name="unknown"`. Buckets span 5ms–30s. |
| `metis_llm_call_errors_total` | counter | `provider`, `model`, `error_class` | `llm.call_failed` | Failure-only counter split out of `metis_llm_calls_total{status}` so rate() queries don't have to sum across the success+error label space. Same row simultaneously bumps the legacy `metis_llm_calls_total{status=<error_class>}` for back-compat with Wave-11 dashboards. |
| `metis_tool_failures_total` | counter | `tool_name`, `error_class` | `tool.failed` | Same tool-name correlation as the latency histogram. `error_class` is the 8-value `ToolErrorClass`. |
| `metis_gateway_auth_failures_total` | counter | `reason` | `gateway.auth_failed` | Auth-time rejections. `reason` ∈ `missing_token | invalid_token | key_revoked` — the closed `GatewayAuthFailureReason` enum. Drives the credential-stuffing / leaked-key alert. |
| `metis_gateway_key_cost_usd_total` | counter | `gateway_key_id` | `llm.call_completed.cost_usd` | Per-key cost attribution. Calls without a `gateway_key_id` (agent-loop, pre-multi-user gateway keys) collapse under `gateway_key_id="null"` per multi-user.md §3.4. Drives the per-key spend-anomaly alert. |

A new event type — `gateway.auth_failed` — backs the auth-failure counter.
Payload schema in `event-bus-and-trace-catalog.md §6.x` (Wave 14a addition):

```python
class GatewayAuthFailed(msgspec.Struct, frozen=True):
    reason: Literal["missing_token", "invalid_token", "key_revoked"]
    inbound_shape: Literal["openai", "anthropic"]
    token_hash_prefix: str | None = None   # SHA-256 first 8 hex chars
    gateway_key_id: str | None = None      # set on reason="key_revoked"
```

The event is `PSEUDONYMOUS` sensitivity and `AUDIT_EVENT_TYPES`-flagged
(audit-log.md §AUDIT_EVENT_TYPES) — the trace-retention sweep preserves
brute-force / credential-stuffing forensics past the 90-day window.

## 4. Implementation

### 4.1 Module layout

* [`packages/metis-core/src/metis_core/observability/metrics.py`](../../packages/metis-core/src/metis_core/observability/metrics.py) —
  `MetricsCollector` class, the bus subscriber, and the exposition
  helper. Lives in `metis-core` because both apps need the same
  collector logic and the events it reads are catalog events.
* [`apps/server/src/metis_server/app.py`](../../apps/server/src/metis_server/app.py) —
  builds a `MetricsCollector` with a `session_count_getter` and mounts
  `GET /metrics`.
* [`apps/gateway/src/metis_gateway/app.py`](../../apps/gateway/src/metis_gateway/app.py) —
  builds a `MetricsCollector` with a `gateway_keys_getter` and mounts
  `GET /metrics`.

### 4.2 Bus subscription

The collector subscribes to a single subscription on the catalog
events listed in §3 (the implementation pins them in
`_OBSERVED_EVENT_TYPES`). Subscription is **non-fast-path** —
observability never blocks a turn (`event-bus-and-trace-catalog.md`
§3.4). A handler exception is logged and swallowed.

### 4.3 Polled gauges

`metis_session_count` and the `metis_gateway_keys_*` gauges aren't
driven by bus events — they read their underlying source on every
scrape via getters injected at `MetricsCollector` construction time.
The exposition helper (`MetricsCollector.expose()`) calls those
getters before generating the Prometheus body. A getter exception is
logged and the previously-observed gauge value remains.

### 4.4 Decimal → float

`Usage.cost_usd` and `quota.alert.percentage` are `Decimal` end-to-end
in `metis-core` (canonical-message-format.md §6.4). Prometheus client
expects `float`. The collector calls `float(value)` at the export
boundary; the loss of cent-level precision is acceptable for an
operator-facing metric (the trace DB and `/analytics/cost` retain
`Decimal`).

## 5. Dependency trade-off

This spec adds `prometheus-client>=0.20.0` as a runtime dep on
`metis-core`, `metis-server`, and `metis-gateway`. The Metis
dependency floor is already non-stdlib (`msgspec`, `starlette`,
`uvicorn`, `anthropic`, `httpx`, `jsonschema`, `pyyaml`, `python-ulid`,
`openai`); adding one more library is a small marginal cost in
exchange for the canonical Prometheus Python client.

A pure-stdlib alternative is possible (~120 lines: text-format
generation, label-string escaping, histogram bucket bookkeeping). It
was considered and rejected — `prometheus-client` is small (~50KB),
maintained by the Prometheus org, and gets the exposition format
right by definition. Re-implementing it on every spec revision
(histogram exemplars, `OpenMetrics` switch-over) is not where this
project should spend its single-engineer budget.

## 6. Helm

[`infra/gateway/helm/values.yaml`](../../infra/gateway/helm/values.yaml)
gains a `monitoring` section:

```yaml
monitoring:
  enabled: false           # render a ServiceMonitor when true
  serviceMonitor:
    interval: 30s
    scrapeTimeout: 10s
    namespace: ""          # default: same as the release
    labels: {}             # additional labels for Prometheus operator selectors
    relabelings: []
    metricRelabelings: []
```

When `monitoring.enabled` is true, the chart renders
`templates/servicemonitor.yaml` (Prometheus operator's
`monitoring.coreos.com/v1.ServiceMonitor`) targeting the gateway
Service on the same `proxy.listenPort` the Service exposes. The
operator (Prometheus operator must already be installed in the
cluster) discovers the new monitor via its ServiceMonitor selector and
starts scraping `/metrics`.

The Service's port is unchanged — `/metrics` rides the same port as
the LLM endpoints; no second port to expose.

The chart does not ship a fallback for clusters without
`prometheus-operator`. Hand-roll a `Prometheus` scrape job pointing
at the same `Service:port/metrics` if you don't run the operator.

## 7. What this is not

* **Not a request log.** No per-request labels (request id, key id on
  every llm.call). Use the trace DB for that.
* **Not a tracing backend.** No spans, no causal chains. The bus
  catalog already provides causal chains via `parent_event_id`.
* **Not authenticated.** Loopback bind + in-cluster scraper is the
  threat model. Ingress should not expose `/metrics`.
* **Not a SLO surface.** No `metis_*_slo_compliant` boolean
  metrics — operators wire SLO calculations in Prometheus / their
  alerting layer against the raw counters and histograms.

## 8. Open follow-ons

* **`metis_bus_*` self-metrics.** `bus.gap_detected` /
  `bus.subscriber_unregistered(reason="removed_after_errors")` are
  catalog events with operational signal but aren't projected into
  `/metrics` yet. Cheap add when the demand surfaces.
* ~~`metis_tool_calls_total{tool, success}`.~~ **Closed in Wave 14a** —
  superseded by `metis_tool_call_latency_seconds{tool_name}` (histogram
  carries success-vs-failure count via the `_count` series across both
  `tool.completed` and `tool.failed`) and `metis_tool_failures_total{tool_name, error_class}`
  for the failure breakdown.
* **Exemplars.** OpenMetrics exemplars on the latency histogram could
  link a hot bucket back to a specific trace event id. Not in v1.
* **Histogram exemplar links to runbook.** Once exemplars land, the
  runbook entries in [`docs/operations/observability-runbook.md`](../operations/observability-runbook.md)
  should embed `traceID` exemplars so a Grafana panel click jumps
  straight to the matching trace event in the SQLite trace DB browser.

## 9. Alert rule templates (Wave 14a)

The helm chart ships [`infra/gateway/helm/templates/prometheus-rules.yaml`](../../infra/gateway/helm/templates/prometheus-rules.yaml)
with four `PrometheusRule` templates per the production-grade observability
checklist. All are off by default (`monitoring.prometheusRules.enabled: false`);
the runbook in [`docs/operations/observability-runbook.md §4`](../operations/observability-runbook.md)
walks operators through a one-week baseline-then-enable workflow rather than
shipping pre-tuned thresholds that drift from real traffic.

| Alert | Default threshold | Primary input |
|---|---|---|
| `MetisLLMCallLatencyP99High` | p99 > 30s, 5m | `metis_llm_call_latency_seconds_bucket` |
| `MetisLLMErrorRateHigh` | error rate > 5%, 10m | `metis_llm_call_errors_total` / `metis_llm_calls_total` |
| `MetisGatewayAuthFailureRateHigh` | > 0.1/sec, 5m | `metis_gateway_auth_failures_total` |
| `MetisGatewayKeyCostSpike` | > $10/hour/key, 10m | `metis_gateway_key_cost_usd_total` |

Each rule's `threshold`, `for`, `severity`, and `enabled` flags are exposed
in `values.yaml` under `monitoring.prometheusRules.<rule>` so buyers can tune
without editing the chart. The runbook covers the runtime triage path for
each alert; see [`docs/operations/observability-runbook.md §2`](../operations/observability-runbook.md).

The chart also ships a default Grafana dashboard at
[`infra/gateway/helm/dashboards/metis-gateway.json`](../../infra/gateway/helm/dashboards/metis-gateway.json)
that buyers import into their Grafana instance; it visualizes every metric
in §3 plus the §3.2 extensions, partitioned into five rows
(Traffic & Latency, Errors, Routing & Tools, Gateway Auth & Cost,
Quotas & Active Keys + WAL).
