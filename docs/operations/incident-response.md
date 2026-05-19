# Incident response

Operational playbook for a Metis gateway / agent-server deployment.
Presumes the single-tenant single-region shape from
[`docs/gateway-deployment.md`](../gateway-deployment.md). Metis
ships open-core; you run the gateway and own the incident loop.

See also [`status-page.md`](status-page.md) (external comms) and
[`sla-template.md`](sla-template.md) (downstream SLA).

---

## Severity levels

Pick the highest matching row.

| Severity | Criteria                                                                                                                                                                            | Ack target     | Mitigation target | Resolution target |
|----------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|----------------|--------------------|--------------------|
| **SEV1** | Total gateway outage; irrecoverable trace-DB corruption; suspected key compromise with active exploitation; exposure of customer prompts / completions; provider bill threshold breached | 15 min, 24/7   | 1 hour             | 4 hours            |
| **SEV2** | One inbound shape down; one upstream provider down with no failover; per-key analytics rollup wrong by ≥10%; ingress TLS expired                                                     | 1 hour, 24/7   | 4 hours            | 1 business day     |
| **SEV3** | Elevated latency (p95 > 2× baseline); trace-DB near disk-full; a non-default model unavailable; one tenant's quota-alert spamming                                                    | 1 business day | 3 business days    | 1 week             |
| **SEV4** | Cosmetic dashboard bug, log noise, doc errors, deprecation warnings                                                                                                                  | Best effort    | Best effort        | Next sprint        |

Re-evaluate severity at every status update. SEV2 → SEV1 mid-incident
is normal; it tightens the comms cadence.

---

## On-call alert paths

The gateway emits no native paging signal; alerts derive from three
sources. Wire each to whatever paging provider you already use
(PagerDuty, Opsgenie, Splunk On-Call, plain email + SMS).

| Source                                                | Detection                                                                          | Suggested rule                                                                          |
|-------------------------------------------------------|-------------------------------------------------------------------------------------|------------------------------------------------------------------------------------------|
| `GET /healthz` (gateway), `GET /health` (server)      | External probe (UptimeRobot / Pingdom / Uptime Kuma)                                | Two consecutive failures in 60 s → SEV1; one → SEV2                                      |
| Container / pod logs (stdout)                         | Loki / CloudWatch / Stackdriver — filter `traceback`, `quota_exceeded`, `key_revoked` | Any ERROR traceback within 5 min → SEV2; sustained quota_exceeded > 1/min/key → SEV3   |
| Trace-DB SQL probe                                    | Cron (`sqlite3` over the mounted PVC) every 1–5 min                                 | Per-model error rate > 5% over 10 min → SEV2; daily cost projection > 2× budget → SEV3 |

One *primary* on-call rotation. SEV1 wakes them; SEV2 pages in business
hours, escalates out-of-hours; SEV3 / SEV4 go to a chatops queue.
Trace-DB probe recipe (PagerDuty Events API v2; swap to your provider's
heartbeat URL as needed):

```bash
* * * * * metis  /usr/local/bin/sqlite3 /var/lib/metis/metis.db \
  "SELECT 100.0*SUM(json_extract(payload,'\$.stop_reason')='error')/COUNT(*) \
   FROM events WHERE type='llm.call_completed' \
   AND timestamp > datetime('now','-10 minutes');" \
  | awk '$1 > 5 { exit 1 }' \
  || curl -fsS -X POST https://events.pagerduty.com/v2/enqueue -H 'content-type: application/json' \
       -d "{\"routing_key\":\"$PD_KEY\",\"event_action\":\"trigger\",
            \"payload\":{\"summary\":\"Metis LLM error rate > 5%\",
            \"severity\":\"warning\",\"source\":\"metis-gateway\"}}"
```

---

## First-hour playbook

Four beats. Time-box each; if you blow the box, escalate (page
secondary, open a war room).

### 1. Detect (≤2 min)

- Hit `/healthz` from a known-good vantage point. 200 = up; 5xx = sick
  app; timeout = ingress / network broken.
- `kubectl logs -n metis-gateway deploy/metis-gateway --tail=100 -c gateway`
  (or `docker compose logs --tail=100 gateway`).
- If neither responds, skip to [Mitigate](#3-mitigate-30-min).

### 2. Triage (≤10 min)

Classify blast radius.

- One inbound shape only? Run both halves of the
  [Smoke test](../gateway-deployment.md#smoke-test-recipe).
- One upstream provider only? Check
  `route.decided.chain[*].verdict='unavailable'` in the trace DB over
  the last 15 min — that's how routing flags a provider down.
- One tenant only? `SELECT key_id, COUNT(*) FROM events WHERE
  type='llm.call_completed' AND timestamp > datetime('now','-15
  minutes') GROUP BY key_id`.
- All tenants, all shapes, all providers? Disk / DB / process —
  skip to [Trace DB](#trace-db-corruption-or-disk-full).

Set severity; post the initial status-page update
([`status-page.md`](status-page.md)).

### 3. Mitigate (≤30 min)

Get traffic flowing. The right move is rarely a fix — it's the
fastest revert. Order of preference:

1. **Roll back** to the last good image / chart revision:
   `helm rollback metis-gateway` or
   `docker compose down && docker compose up -d` with the previous tag.
2. **Failover to a backup provider:** set
   `METIS_GATEWAY_GLOBAL_DEFAULT=openrouter:anthropic/claude-haiku-4-5`
   and restart. Per-(provider, model) availability tracking routes
   around the dead provider for keys that don't pin `model`; keys that
   pin keep failing until flipped.
3. **Restart the process.** `kubectl rollout restart deploy/metis-gateway`.
4. **Maintenance mode.** Return 503 at the TLS terminator (Caddy
   `respond 503`, nginx `return 503;`) while you investigate.

Do not fix-forward in the first hour unless the fix is one line and
locally reproduced. Mitigation first, root cause after.

### 4. Comms (every 30 min until resolved)

Post on every status change and at least every 30 min — even if the
update is "still investigating." Templates in
[`status-page.md`](status-page.md). Internal channel
(`#metis-incidents`): same updates plus the raw commands you ran.

---

## Post-mortem template

Blameless, structured, runs ≤ 1 week after resolution. Use this exact
heading set so post-mortems are scannable.

```markdown
# Incident YYYY-MM-DD — <short title>

**Severity:** SEV<n>   **Duration:** HH:MM   **Customer impact:** <plain English>

## Summary
Two sentences. What broke, what was user-visible, when it ended.

## Timeline (UTC)
- HH:MM — first signal (alert / customer report)
- HH:MM — ack by <on-call>
- HH:MM — mitigation X attempted (outcome)
- HH:MM — root cause identified
- HH:MM — full resolution; status page updated

## Impact
- Affected tenants: <list or "all">
- Affected endpoints: <list>
- Requests dropped: <count from trace DB>
- Estimated revenue / SLA-credit impact: <USD>

## Root cause
What actually broke. Code path, config, infra event. One paragraph.
Reference commit / line if applicable.

## Contributing factors
What made this worse than it had to be — slow detection, unclear
runbook, missing alert.

## What went well
Genuine. Fast detection, good rollback, clear comms.

## What went poorly
Equally genuine. No blame on individuals; focus on systems and process.

## Action items
| Item                                | Owner   | Due        | Tracking      |
|--------------------------------------|---------|------------|---------------|
| Add alert for X                      | @alice  | 2026-MM-DD | issue #123    |
```

Action items must have an owner, a date, and a tracking link. An item
without a tracking link does not exist.

---

## Common failure modes

### Upstream LLM API outage

All `llm.call_completed` events for one provider carry
`stop_reason='error'`; routing has marked `(provider, model)`
unavailable and slot 6 / 7 is flipping to the next default.

**Detect.** `SELECT json_extract(payload,'$.chain') FROM events
WHERE type='route.decided' AND timestamp > datetime('now','-5
minutes') ORDER BY timestamp DESC LIMIT 20` — look for
`verdict='unavailable'`.

**Mitigate.**

1. Confirm the provider status page (status.anthropic.com /
   status.openai.com / status.openrouter.ai). If they're red, you
   wait — no buyer-side fix.
2. Failover: set `METIS_GATEWAY_GLOBAL_DEFAULT` to a healthy provider's
   canonical id and restart. Clients pinning the dead provider still
   fail; tell their owners to flip or drop the pin.
3. OpenRouter as multi-provider fallback: with `OPENROUTER_API_KEY` set,
   route via `openrouter:anthropic/claude-haiku-4-5`. Same wire shape
   client-side; higher latency, overlay pricing.
4. Update the status page: "Degraded — <provider> upstream incident."

**Recover.** Per-(provider, model) availability auto-clears on the
next successful adapter probe.

### Trace DB corruption or disk full

Pod / container restart loop with
`sqlite3.OperationalError: database disk image is malformed` or
`database or disk is full`.

**Detect.**

- Disk: `df -h` on the PVC mount. SQLite WAL grows `metis.db-wal`
  until checkpoint; > 100 MB is normal at moderate traffic.
- Corruption:
  `sqlite3 /var/lib/metis/metis.db 'PRAGMA integrity_check'`.

**Mitigate.**

1. Stop the writer: `kubectl scale deploy/metis-gateway --replicas=0`
   (or `docker compose stop gateway`). Restoring under an active writer
   is unsafe per
   [`gateway-deployment.md`](../gateway-deployment.md#backup--restore).
2. **Disk full** — prune + vacuum:
   `DELETE FROM events WHERE timestamp < '2026-04-01T00:00:00Z'; VACUUM;`.
   Resize the volume, restart.
3. **Corruption** — restore from the most recent backup:
   `metis restore /backup/daily/metis.<latest>.db --db-path /var/lib/metis/metis.db --force`.
   You lose events between the backup and the crash — document under
   "Impact" in the post-mortem.
4. No recent backup? Cost attribution for the gap is lost; gateway logs
   are best-effort reconstructable. Open a SEV1.

**Prevent.** Daily backup cron from
[`gateway-deployment.md`](../gateway-deployment.md#backup--restore)
plus PVC-size alert at 80%.

### Gateway-key compromise

A `gw_…` token leaked. SEV1 until scope confirmed.

**Detect.** Scope spend via the trace DB:

```sql
SELECT json_extract(payload,'$.model') AS model, COUNT(*) AS calls,
       ROUND(SUM(json_extract(payload,'$.usage.cost_usd')),4) AS cost
  FROM events WHERE type='llm.call_completed'
   AND json_extract(payload,'$.gateway_key_id')='gk_01HXYZ...'
   AND timestamp > datetime('now','-24 hours')
 GROUP BY model;
```

**Mitigate.**

1. Revoke immediately, no grace: `metis gateway revoke-key gk_01HXYZ...`.
   Subsequent requests return 401 `code='key_revoked'` per
   `gateway.md §11.2`; no pod restart needed.
2. Issue a replacement via a secure channel (1Password share, Signal —
   never email): `metis gateway issue-key --name "client-x-v2" --workspace /workspace --allow-model anthropic:claude-haiku-4-5 --daily-cap-usd 5.00`.
3. If `--daily-cap-usd` was unset, cross-check the SQL against billing
   and file a provider refund claim if their TOS allows.
4. Audit other keys for the same pattern (source IP, user-agent,
   anomalous-model bursts).

**Prevent.** Always issue with `--daily-cap-usd` and `--allow-model`.
Rotate quarterly via `metis gateway rotate-key` (default 24h grace).

### Quota runaway

One key's spend rate is 10× normal. `quota.alert` fired (soft
threshold) but the hard breaker hasn't, because the key is below
`daily_cap_usd` — or has no `daily_cap_usd` set.

**Detect.**

```sql
SELECT json_extract(payload, '$.gateway_key_id') AS key,
       ROUND(SUM(json_extract(payload, '$.usage.cost_usd')), 4) AS cost_1h
  FROM events WHERE type='llm.call_completed'
   AND timestamp > datetime('now', '-1 hour')
 GROUP BY key ORDER BY cost_1h DESC LIMIT 10;
```

**Mitigate.**

1. **Hard breaker** — revoke + reissue with caps. Same flow as
   key-compromise.
2. **Soft breaker** — talk to the tenant first. Per-key rollups are
   visible via `/analytics/by_key`; the spike may be a legitimate
   CI burst. Confirm before revoking — false-positive revocations
   destroy trust faster than cost overruns destroy margin.
3. Lower the cap proactively on similarly-scoped keys.

**Prevent.** Always set `--daily-cap-usd` and `--monthly-cap-usd` at
issuance. `quota.alert` fires at 80% of the cap; tail it from the
trace DB into the same paging system as `/healthz` failures.

---

## See also

- [`status-page.md`](status-page.md), [`sla-template.md`](sla-template.md).
- [`../gateway-deployment.md`](../gateway-deployment.md) — install, TLS, backup/restore, helm chart.
- [`../specs/gateway.md`](../specs/gateway.md) §11 — key lifecycle; [`../specs/event-bus-and-trace-catalog.md`](../specs/event-bus-and-trace-catalog.md) §7.5 — backup/restore contract.
