# Launch-day playbook

Operator script for the first public launch window and first paid
customer handoff. This is the command center; deeper procedures live in
[`pre-launch-dry-run-checklist.md`](pre-launch-dry-run-checklist.md),
[`support-channels.md`](support-channels.md),
[`incident-response.md`](incident-response.md), and
[`status-page.md`](status-page.md).

Use [`first-customer-runbook.md`](first-customer-runbook.md) as the
account-specific plan. If a future checkout omits it, fall back to
[`concierge-onboarding.md`](concierge-onboarding.md).

---

## 1. Scope

Launch means the gateway is usable by an external buyer or pilot team,
with support, incident handling, status-page posture, and trace-backed
reporting ready enough for the owner to stand behind it.

Launch does not mean Phase 3 is promoted, `https://status.2sum.ai` is
live when DNS / TLS are still owner-side, billing live-mode validation is
complete, or a public announcement is required.

---

## 2. Launch Thread Header

Paste this before T-60: date, window, gateway, status-page target,
customer / pilot, launch owner, support owner, incident commander,
gateway operator, status-page operator, rollback owner, dry-run result,
and known owner-side blockers.

Keep decisions in this thread. If an incident opens, coordination moves
to the incident channel and summaries come back here.

---

## 3. Roles

| Role | Owns |
|---|---|
| Launch owner | Final go / no-go, scope control |
| Primary support owner | Buyer thread, inbox, non-incident triage |
| Incident commander | Severity, cadence, mitigation priority |
| Gateway operator | Deploy, rollback, key ops, trace checks |
| Status-page operator | Public updates and component state |
| Scribe | UTC timeline, commands, decisions, action items |
| Account owner | Buyer-specific handoff and concierge cadence |

One person can hold multiple roles, but no role should be implicit.

---

## 4. T-7 To T-3

Owner decisions:

- [ ] Launch shape: private pilot, first paid customer, or public
      availability.
- [ ] SLA offered or explicitly out of scope.
- [ ] Status-page path: Tier A external, Tier B Uptime Kuma, or
      owner-side deferral.
- [ ] Billing mode: off, test mode, or live mode.
- [ ] Self-serve signup on or off. Default is off.
- [ ] Support coverage window and buyer contact route.
- [ ] Who can revoke or rotate a customer key.

Artifact freeze:

- [ ] Run [`pre-launch-dry-run-checklist.md`](pre-launch-dry-run-checklist.md).
- [ ] Confirm provider keys, trace DB path, backup path, and rollback
      target.
- [ ] Confirm [`status-page-config.yaml`](status-page-config.yaml) has no
      secrets and is ready to paste if hosting is not provisioned.
- [ ] Confirm no prohibited launch edits are pending.

Recommended freeze rule: after T-3, docs / config typo fixes only unless
the launch owner approves the change in the launch thread.

---

## 5. T-1 Rehearsal

Run a short real smoke, not a benchmark suite.

```bash
curl -fsS "$GATEWAY_URL/healthz"
curl -fsS "$GATEWAY_URL/v1/messages" \
    -H "x-api-key: $SYNTHETIC_GATEWAY_KEY" \
    -H "anthropic-version: 2023-06-01" \
    -H "content-type: application/json" \
    -d '{"model":"anthropic:claude-haiku-4-5","max_tokens":1,"messages":[{"role":"user","content":"ping"}]}'
```

Pass requires:

- [ ] Anthropic-shape synthetic request succeeds.
- [ ] OpenAI-shape synthetic request succeeds if in scope.
- [ ] Trace DB records the call.
- [ ] `metis trial-status` can read the trace DB.
- [ ] Buyer opening email is ready but not sent.
- [ ] Rollback command is in the launch thread.

Post a T-1 result with: pass/fail, smoke, trace, status page, support,
rollback, and owner-side blockers.

---

## 6. T-60 Room Opens

- [ ] Launch owner posts the header.
- [ ] Primary support owner confirms inbox / buyer thread access.
- [ ] Incident commander confirms page / urgent SMS path.
- [ ] Gateway operator confirms cluster / host access.
- [ ] Status-page operator confirms admin access or owner-side deferral.
- [ ] Scribe starts UTC timeline.
- [ ] Everyone confirms the rollback deadline.

If a required owner is absent, pause. A launch with no named owner is
already degraded.

---

## 7. T-45 Health Checks

```bash
curl -fsS "$GATEWAY_URL/healthz"
curl -fsS "$GATEWAY_URL/metrics" | grep metis_gateway_keys_active
```

- [ ] `metis_gateway_keys_active >= 1`.
- [ ] No active SEV1 / SEV2.
- [ ] Provider status pages are green enough for the planned route.
- [ ] Trace DB disk has headroom.
- [ ] Gateway logs have no repeating traceback.
- [ ] TLS / rate-limit / audit posture matches the launch plan.

If `/metrics` is private, run the equivalent inside the Prometheus
namespace or gateway node.

---

## 8. T-30 Key Setup

Buyer trial key:

```bash
metis gateway issue-key \
    --name "{{customer_slug}}-trial" \
    --workspace /workspace \
    --customer-tier trial \
    --daily-cap-usd 50 \
    --user "{{primary_user_slug}}" \
    --team "{{customer_slug}}"
```

Synthetic status key:

```bash
metis gateway issue-key \
    --name "status-synthetic" \
    --workspace /workspace \
    --daily-cap-usd 0.50 \
    --allow-model anthropic:claude-haiku-4-5
```

- [ ] Buyer token is captured only in the approved secure channel.
- [ ] `customer_tier=trial` is present for the buyer key.
- [ ] `user_id`, `team_id`, and daily cap match the customer plan.
- [ ] Synthetic key is separate or explicitly approved to be shared.

If a key is misissued, revoke and issue a new one.

---

## 9. T-15 Go / No-Go

Each role answers `go`, `no-go`, or `go-with-note`.

Go requires:

- [ ] Gateway health check passes.
- [ ] At least one synthetic POST passes.
- [ ] Trace DB records the synthetic call.
- [ ] Support owner and incident commander are present.
- [ ] Rollback path is known.
- [ ] Status-page path is known, even if owner-side hosting is deferred.
- [ ] Buyer token is ready to send.

No-go examples: provider auth failing, no one can revoke a leaked key, no
support owner present, buyer token leaked, or status-page ownership
unknown for a public-user launch.

---

## 10. T+0 Launch Action

For a first paid customer or trial, send the Day 0 email from
[`concierge-onboarding.md`](concierge-onboarding.md#day-0-intake).
For a public availability launch, publish only after health checks pass.

Immediately after sending:

- [ ] Scribe records exact send time in UTC.
- [ ] Support owner watches the buyer thread.
- [ ] Gateway operator watches logs for 15 minutes.
- [ ] Status-page operator verifies status remains green or posted.
- [ ] Account owner confirms buyer has the install link.

---

## 11. First Hour

T+15:

- [ ] `/healthz` still passes.
- [ ] If buyer traffic landed, `llm.call_completed` exists.
- [ ] `gateway_key_id` matches the buyer key.
- [ ] `user_id` / `team_id` fields are present if expected.
- [ ] Cost is non-zero and plausible.
- [ ] No `gateway.auth_failed` spike or quota alert.

T+30 support checkpoint: buyer ack, open questions, gateway traffic,
incidents, and next check.

T+60 reporting check:

```bash
metis trial-status /workspace --db-path .metis-trial/snapshot/metis.db
```

If there is enough traffic, dry-run `metis customer-report`, but do not
send a launch-day report unless it is part of the planned concierge
cadence. Day 3 and Day 7 make the claims.

---

## 12. Branches

Incident branch:

- Move coordination to `#metis-incidents`.
- Assign incident commander and scribe.
- Classify severity from
  [`incident-response.md`](incident-response.md#severity-levels).
- Start the
  [`first-hour playbook`](incident-response.md#first-hour-playbook).
- Draft status-page copy if user-visible.
- Bring summaries back to the launch thread every 30 minutes.

Rollback branch:

- Preferred order: rollback, provider failover, process restart,
  maintenance mode.
- Before rollback: incident commander approves and gateway operator names
  the target revision.
- After rollback: `/healthz` passes, synthetic POST passes, trace DB
  receives a new call, and support owner updates the buyer.

Key-compromise branch:

- Treat leaked `gw_...` token as SEV1 until scoped.
- Revoke first, no grace for suspected exposure.
- Send replacement through the approved secure channel.
- Query last 24h spend and export audit evidence if needed.
- Commands: `metis gateway revoke-key <gateway_key_id>`, then
  `metis gateway issue-key --customer-tier trial --daily-cap-usd 50`
  with the same `user` / `team` tags.

Status-page branch when hosting is live:

- [ ] Create or update incident using the severity-mapped template.
- [ ] Set affected components from
      [`status-page-config.yaml`](status-page-config.yaml).
- [ ] Set next update time before publishing.
- [ ] Post resolution within 15 minutes of internal close.

Status-page branch when hosting is not provisioned:

- [ ] Record owner-side blocker in launch thread.
- [ ] Use the same template body in the buyer thread if needed.
- [ ] Keep [`status-page-config.yaml`](status-page-config.yaml) as the
      paste source for the owner.
- [ ] Do not claim `https://status.2sum.ai` is live.

---

## 13. T+2 Close Or Watch

Close launch mode if there is no active SEV1 / SEV2, buyer has the key
and install link, gateway is healthy, support has the next check time,
and the account owner knows the next concierge step.

Continue extended watch if buyer install help, possible incident, or
provider degradation is still active.

Post `Launch mode: CLOSED / EXTENDED WATCH` with reason, next owner,
next check, and open items.

---

## 14. Days 1, 3, 5, And 7

- **Day 1:** buyer installs, first request lands, `metis trial-status`
  shows non-zero calls.
- **Day 3:** snapshot trace DB, quote only trace-backed numbers, and say
  "no quality verdicts yet" if `quality_count` is zero.
- **Day 5:** generate the internal HTML report, inspect by-user /
  by-team rollups, decide Day 7 framing, and do not send the report.
- **Day 7:** generate final report, offer convert / extend / blocker,
  and issue the paid-tier replacement key only after conversion.

Use [`concierge-onboarding.md`](concierge-onboarding.md) for the exact
email templates and command forms.

---

## 15. Closeout

Launch owner posts a closeout with outcome, buyer state, gateway state,
status-page state, support volume, incidents, follow-ups, and owner
decisions needed.

Every follow-up needs owner, due date, and tracking link:

| Item | Owner | Due | Tracking |
|---|---|---|---|
| `<item>` | `<name>` | `<YYYY-MM-DD>` | `<link>` |

Common follow-ups: status-page DNS / TLS, synthetic probe coverage,
buyer docs clarification, quota cap adjustment, missing quality signal,
billing live-mode validation, or post-mortem action item.
