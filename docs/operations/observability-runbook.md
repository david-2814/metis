# Observability runbook

**Status:** v1 (Wave 14a, 2026-05-16)
**Audience:** SRE / platform operators running `metis-gateway` or `metis-server` in production.

Companion to [`incident-response.md`](incident-response.md) — that doc tells
you what to *do* when something goes wrong; this one tells you what your
graphs are *showing* you and how to read them. Pairs 1:1 with the
[Prometheus alert rules](../../infra/gateway/helm/templates/prometheus-rules.yaml)
and the [Grafana dashboard](../../infra/gateway/helm/dashboards/metis-gateway.json)
that ship with the helm chart.

The spec contract for the metric surface itself is
[`docs/specs/observability.md`](../specs/observability.md); this doc covers
the *operational* contract — meaning, alert thresholds, runbook entries.

---

## 1. Metric reference

Every metric the gateway and server expose, what it counts, the alert that
fires on it, and the first thing to check when the alert pages.

### 1.1 LLM call metrics

| Metric | Type | Labels | What it means |
|---|---|---|---|
| `metis_llm_calls_total` | counter | `provider`, `model`, `status` | One row per LLM API call. `status` is `ok` for completions, or the 8-value `LLMErrorClass` for failures. |
| `metis_llm_call_errors_total` | counter | `provider`, `model`, `error_class` | Failure-only counter split out so error-rate alerts don't have to sum across `status` labels. |
| `metis_llm_call_latency_seconds` | histogram | `provider`, `model` | Wall-time per call, both success and failure paths. Bucket range covers 50 ms through 120 s. |
| `metis_llm_cost_usd_total` | counter | `provider`, `model` | Cumulative spend. `Decimal` is converted to `float` at the export boundary. |
| `metis_gateway_key_cost_usd_total` | counter | `gateway_key_id` | Per-key cost attribution. Agent-loop traffic (no key) buckets under `gateway_key_id="null"`. |

### 1.2 Routing & tool metrics

| Metric | Type | Labels | What it means |
|---|---|---|---|
| `metis_routing_decisions_total` | counter | `winning_slot`, `chosen_model` | One row per `route.decided`. `winning_slot` is the 7-value `RoutingPolicyName` literal. |
| `metis_routing_decision_latency_seconds` | histogram | (none) | Wall-time of the routing engine itself. Sub-millisecond in steady state; tails out under K-NN cluster-tightening regimes. |
| `metis_pattern_matches_total` | counter | `chose_model`, `fingerprint_version` | Slot-4 (pattern store) wins only. |
| `metis_tool_call_latency_seconds` | histogram | `tool_name` | Tool dispatcher wall-time, drained from both `tool.completed` and `tool.failed`. The collector correlates `tool_name` from the prior `tool.called` via a bounded LRU. |
| `metis_tool_failures_total` | counter | `tool_name`, `error_class` | Tool failures only, with the 8-value `ToolErrorClass`. |

### 1.3 Gateway-specific metrics

| Metric | Type | Labels | What it means |
|---|---|---|---|
| `metis_gateway_auth_failures_total` | counter | `reason` | Auth-time rejection counter. Three reasons: `missing_token`, `invalid_token`, `key_revoked`. |
| `metis_gateway_keys_active` | gauge | (none) | Number of `is_active(now)` keys in the keystore, polled at scrape. |
| `metis_gateway_keys_revoked` | gauge | (none) | Total – active, so grace-period-expired keys count here even before the next admin sweep persists them. |
| `metis_quota_used_ratio` | gauge | `identity_kind`, `identity_id` | Per-identity (`key`/`user`/`team`) most-recent quota usage ratio. Pinned to `1.0` when `gateway.quota_exceeded` fires. |

### 1.4 Other metrics

| Metric | Type | Labels | What it means |
|---|---|---|---|
| `metis_session_count` | gauge | (none) | Server-only. Active in-memory sessions. |
| `metis_eval_verdicts_total` | counter | `judge_kind`, `subject_kind` | Evaluator verdicts (`eval.completed` only — `eval.failed` is *not* counted here). |
| `metis_trace_wal_bytes` | gauge | (none) | Trace-DB WAL file size, polled at scrape. |
| `metis_pattern_embedding_cache_hit_ratio` | gauge | `workspace_id` | v2 embedding-cache hit ratio per workspace. |

---

## 2. Alert runbook

The helm chart ships four `PrometheusRule` alert templates under
[`prometheus-rules.yaml`](../../infra/gateway/helm/templates/prometheus-rules.yaml).
Each is off by default; enable via `monitoring.prometheusRules.enabled: true` and
tune individual thresholds in `values.yaml`. After enablement, *triage*
according to the runbook entries below.

### 2.1 LLM latency p99 high

**Alert:** `MetisLLMCallLatencyP99High`
**Default threshold:** p99 > 30 s for 5 min
**PromQL:**
```promql
histogram_quantile(0.99,
  sum by (provider, model, le) (rate(metis_llm_call_latency_seconds_bucket[5m]))
) > 30
```

**What it means.** A specific `(provider, model)` pair has 1% of its calls
taking more than 30 seconds wall-time. The threshold matches the worst-case
turn-latency budget in [`sla-template.md`](sla-template.md); your SLA may
warrant tighter.

**First-action checklist** (in priority order):
1. Check the provider status page (status.anthropic.com, status.openai.com,
   status.openrouter.ai). If they're red, you're in the
   ["Upstream LLM API outage" playbook](incident-response.md#upstream-llm-api-outage).
2. Check whether the p99 spike is concentrated on one model or fleet-wide.
   On the dashboard, "LLM call latency — p50 / p95 / p99" panel.
3. Cross-check `metis_llm_call_errors_total` for the same `(provider, model)`.
   If errors are also up, you're latency-bound *because* of retries — the
   upstream is degraded, not just slow. If errors are flat, the upstream is
   simply slow; consider failover.

**Mitigations:**
- Failover via `METIS_GATEWAY_GLOBAL_DEFAULT` to a healthy provider /
  model (incident-response.md §"Upstream LLM API outage" step 2).
- For OpenRouter-backed paths, the OpenRouter routing fabric may already
  be flapping; route the affected model directly (canonical id, not
  `openrouter:...`) if the direct provider is healthy.

**False-positive patterns:** legitimately long single calls (large thinking
blocks, complex tool-use chains, long output). If the alert fires once a
week for ~15 minutes and the dashboard shows a single tall bar in the
histogram heat-map, it's probably real-but-rare. Bump the threshold or the
`for` clause.

### 2.2 LLM error rate high

**Alert:** `MetisLLMErrorRateHigh`
**Default threshold:** error rate > 5% for 10 min
**PromQL:**
```promql
sum by (provider, model) (rate(metis_llm_call_errors_total[10m]))
  /
sum by (provider, model) (rate(metis_llm_calls_total[10m])) > 0.05
```

**What it means.** A `(provider, model)` pair is failing more than 5% of
the time over a 10-minute window.

**First-action checklist:**
1. Check the dashboard's "LLM errors by class" panel to identify the
   dominant `error_class`. The five common shapes:
   - `rate_limit` — provider throttling. The client / agent is the cause
     (too high a sustained burst), not the gateway. Tell the client to back
     off, or raise the relevant provider's account-level rate limit.
   - `auth` — provider key invalid or revoked. Rotate
     `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `OPENROUTER_API_KEY`.
   - `server_error` (5xx) — upstream provider issue. Status page first;
     failover if confirmed.
   - `network` — connection / TLS / DNS issue. Probe from inside the pod:
     `kubectl exec deploy/metis-gateway -- curl -v https://api.anthropic.com`.
     If repeated `network` errors trip provider-wide unavailability, this
     is the [GA-readiness-audit blocker](ga-readiness-audit.md) — restart
     the pod to reset adapter availability state.
   - `context_overflow` — client sent a request larger than the model's
     window. The client's responsibility; surface to them.
2. Check the provider-side metric tier (if you have one — Anthropic Console
   / OpenAI Platform / OpenRouter analytics). Their error rate should
   confirm or deny what you're seeing.

**Mitigations:** see incident-response.md §"Upstream LLM API outage".

**False-positive patterns:** a low-traffic model on a single canary client
will spike its error rate to 100% on one bad call (denominator: 1, numerator:
1). The `for: 10m` clause filters most of these; tune `for` higher for
fleet shapes with many low-traffic models.

### 2.3 Gateway auth failure rate high

**Alert:** `MetisGatewayAuthFailureRateHigh`
**Default threshold:** > 0.1 failures/sec (≈ 360/hour) for 5 min
**PromQL:**
```promql
sum(rate(metis_gateway_auth_failures_total[5m])) > 0.1
```

**What it means.** The gateway is rejecting authentication at a sustained
rate well above the steady-state baseline (~0 failures/sec under healthy
operation; the alert fires above ~6 fails/min). This is the signature of:

- A credential-stuffing scan against `/v1/chat/completions` (large
  `reason="invalid_token"` series, varied source IPs).
- A leaked key being brute-forced (one specific `token_hash_prefix` showing
  up repeatedly in the audit log — `metis audit export --event-type
  gateway.auth_failed`).
- Internal misconfiguration — e.g. a deploy that rotated keys without
  updating the downstream client config (large `reason="key_revoked"`
  series, source IPs all internal).
- A buggy SDK client sending no Authorization header (large
  `reason="missing_token"` from one IP).

**First-action checklist:**
1. Open the dashboard's "Gateway auth failures by reason" panel. Which
   reason dominates? That decides triage:

   | Dominant reason | Investigation |
   |---|---|
   | `invalid_token` | External attack OR internal client with wrong key. Check source-IP distribution in the trace DB (`SELECT json_extract(payload, '$.token_hash_prefix'), COUNT(*) FROM events WHERE type='gateway.auth_failed' GROUP BY 1 ORDER BY 2 DESC LIMIT 20;`). If one IP / prefix dominates: external attacker; consider rate-limit middleware (`gateway-hardening.md §3`). If diffuse: a deploy regression. |
   | `key_revoked` | Recent rotation. Cross-reference `gateway.key_revoked` events around the same timestamp — the rotation didn't propagate to all clients. Reach out before re-issuing. |
   | `missing_token` | Client misconfig. Check user-agent in the access log of your TLS terminator (Ingress / Caddy). |

2. If external attack is confirmed, enable the rate-limit middleware
   (`monitoring.rateLimit.enabled: true` in helm values) to give the
   in-process limiter a chance to slow the scanner before it exhausts
   resources. This is *not* a substitute for an edge WAF — the buyer's
   CDN / WAF should be the first line of defense. The rate-limit middleware
   protects against attackers who bypass the WAF.
3. The `gateway.auth_failed` event is audit-flagged, so it survives the
   90-day trace-retention sweep. Pull a long-window export for the security
   team: `metis audit export /tmp/auth-failures.jsonl --event-type
   gateway.auth_failed --since 2026-04-01`.

**Mitigations:**
- Edge: enable rate limiting at the CDN / WAF.
- In-process: enable `monitoring.rateLimit.enabled: true` per
  [`gateway-hardening.md §3`](../specs/gateway-hardening.md). Per-IP bucket
  defaults to 1000 RPM — well above any well-behaved client.
- Investigate + revoke compromised keys (`metis gateway revoke-key
  <key_id>`) per incident-response.md §"Gateway-key compromise".

**False-positive patterns:** a deploy that flips the keystore without
warning produces a brief `key_revoked` spike that resolves itself once
clients catch up. If the alert resolves within 15 minutes after a planned
key rotation, it's the rotation. If it lingers, real.

### 2.4 Gateway key cost spike

**Alert:** `MetisGatewayKeyCostSpike`
**Default threshold:** > $10/hour per single gateway key, sustained 10 min
**PromQL:**
```promql
sum by (gateway_key_id) (
  rate(metis_gateway_key_cost_usd_total{gateway_key_id!="null"}[1h]) * 3600
) > 10
```

**What it means.** One gateway key's burn rate is above $10/hour over the
last 60 minutes. This catches runaway spend BEFORE the per-key daily /
monthly hard cap (`quota.alert` / `gateway.quota_exceeded`) fires.

**First-action checklist:**
1. Identify the key: `{{ $labels.gateway_key_id }}` in the alert.
2. Cross-check via the analytics rollup:
   ```bash
   curl http://gateway/analytics/by_key?key=<gateway_key_id> | jq
   ```
3. Inspect *which models* the key is calling:
   ```sql
   SELECT json_extract(payload, '$.model') AS model,
          COUNT(*) AS calls,
          ROUND(SUM(json_extract(payload, '$.cost_usd')), 4) AS cost
     FROM events WHERE type='llm.call_completed'
      AND json_extract(payload, '$.gateway_key_id')='<key>'
      AND timestamp > datetime('now', '-1 hour')
    GROUP BY model;
   ```
4. **Talk to the tenant before revoking.** Per incident-response.md
   §"Quota runaway": false-positive revocations destroy trust faster
   than cost overruns destroy margin. A legitimate CI burst, model
   benchmarking run, or evaluation pass can look identical to a leak
   for the first hour.

**Mitigations:**
- Confirmed leak / runaway: revoke + reissue with caps (`metis gateway
  revoke-key`, then `issue-key --daily-cap-usd 5.00 --allow-model
  anthropic:claude-haiku-4-5`).
- Confirmed legitimate burst: raise the per-key cap, no action needed.
- Recurring issue: enable per-key quotas (`--daily-cap-usd` /
  `--monthly-cap-usd` at issuance) on similarly-scoped keys.

**False-positive patterns:** the first hour of a key's lifetime when a
client is bulk-loading a fresh embedding cache. The `for: 10m` clause
filters short bursts; longer-running legitimate workloads may need a
per-key threshold override.

---

## 3. Dashboard tour

The Grafana dashboard JSON ships at
[`infra/gateway/helm/dashboards/metis-gateway.json`](../../infra/gateway/helm/dashboards/metis-gateway.json).
Import into Grafana 9+ via "Dashboards → Import → Upload JSON file"; bind
the `DS_PROMETHEUS` datasource to your existing Prometheus instance.

Layout (top-to-bottom):

1. **Traffic & Latency** — LLM call rate (by provider/model/status) +
   p50/p95/p99 latency. The first place to look during any incident: tells
   you whether the gateway is busy, idle, or stuck.
2. **Errors** — LLM error rate (per provider/model) + errors by class.
   Distinguishes "infrastructure is failing" from "legitimate edge-case
   outputs."
3. **Routing & Tools** — Routing decisions (which slot is winning) + tool
   call p95 latency by tool. Use the routing panel when triaging "why is
   model X picking up traffic it shouldn't"; the tool panel for triaging
   "why is each turn so slow."
4. **Gateway Auth & Cost** — Auth-failure rate (by reason) + top-10 spend
   per gateway key. Both feed the corresponding alerts.
5. **Quotas & Active Keys** — Active vs revoked-key gauges + per-identity
   quota ratio (`metis_quota_used_ratio`) — useful for budgeting and
   capacity planning.
6. **Trace-DB WAL size** — `metis_trace_wal_bytes` over time. Sustained
   growth above ~3× the auto-checkpoint threshold means a long-running
   reader is holding the checkpoint barrier; see
   [`trace-performance.md`](trace-performance.md) §WAL for the SQL probe.

---

## 4. Tuning checklist (week 1 post-install)

1. Install the chart with `monitoring.enabled=true` and
   `monitoring.prometheusRules.enabled=false`. The dashboard renders, no
   alerts fire.
2. Let the gateway run under typical load for 1 week.
3. Open the dashboard at the longest time window your Prometheus retains
   (typically 15 days). Note:
   - 95th percentile of `metis_llm_call_latency_seconds` p99 →
     `llmLatencyP99.threshold` is ~2-3x that.
   - 95th percentile of `metis_llm_call_errors_total / metis_llm_calls_total` →
     `llmErrorRate.threshold` is ~2x that.
   - 95th percentile of `rate(metis_gateway_auth_failures_total[5m])` →
     `gatewayAuthFailureRate.threshold` is ~3-5x that.
   - 95th percentile of per-key spend rate / hour →
     `gatewayKeyCostSpike.threshold` is ~3-5x that.
4. Set the four thresholds in `values.yaml`, flip
   `monitoring.prometheusRules.enabled=true`, redeploy.
5. Page yourself by deliberately triggering one alert (e.g. revoke and
   re-issue a key to bump `key_revoked` count) so you confirm the paging
   path works end-to-end.

---

## 5. What this is not

- **Not a tracing backend.** No spans, no flame graphs. The bus catalog
  already provides causal chains via `parent_event_id`; use the trace DB
  + `metis evaluate` for per-turn deep dives.
- **Not a logging backend.** Application logs go through your existing
  log pipeline (Loki / CloudWatch / Stackdriver). Prometheus metrics are
  aggregates, not events.
- **Not a SLO calculator.** No `metis_*_slo_compliant` boolean
  metrics — operators wire SLO calculations in Prometheus itself or
  Grafana SLO panels against the raw counters and histograms.
- **Not a cost-management surface.** Use the `/analytics/cost`,
  `/analytics/by_key`, `/analytics/by_user`, `/analytics/by_team` REST
  endpoints for budget reporting. The `metis_gateway_key_cost_usd_total`
  counter is for *alerting* on cost anomalies, not for monthly billing
  reconciliation.

---

## See also

- [`docs/specs/observability.md`](../specs/observability.md) — the metric-surface contract.
- [`docs/operations/incident-response.md`](incident-response.md) — what to do once an alert pages you.
- [`docs/operations/trace-performance.md`](trace-performance.md) — WAL gauge interpretation and SQLite-level tuning.
- [`docs/operations/sla-template.md`](sla-template.md) — buyer-facing SLA that the latency / error thresholds are derived from.
- [`infra/gateway/helm/templates/prometheus-rules.yaml`](../../infra/gateway/helm/templates/prometheus-rules.yaml) — alert rule definitions.
- [`infra/gateway/helm/dashboards/metis-gateway.json`](../../infra/gateway/helm/dashboards/metis-gateway.json) — Grafana dashboard JSON.
