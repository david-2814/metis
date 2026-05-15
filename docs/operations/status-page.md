# Status page

Public-facing uptime surface. Paired with
[`incident-response.md`](incident-response.md) (internal playbook)
and [`sla-template.md`](sla-template.md) (commitment).

Buyers expect "is it up right now, what broke recently, what's
scheduled" on day one. The recipes below cover that floor.

---

## Two-tier recipe

Pick one. Most buyers start with the external tier and add the
self-hosted tier once their compliance team asks where the status
data itself lives.

### Tier A — external (UptimeRobot / Statuspage.io / Better Stack)

Cheap, no infra, off your gateway's failure domain. Right for pre-pilot
and pilot. Free tiers (UptimeRobot 50 monitors / 5-min; Better Stack 10
monitors / 3-min) cover a single gateway; Atlassian Statuspage (~$30/mo)
is the polished public-page option.

Two probes, the only signals worth a status page on v1:

```
1. https://gateway.example.com/healthz                    expect 200
2. https://gateway.example.com/v1/messages  (synthetic)   expect 200/401
```

The second probe needs a real `POST /v1/messages` against a
dedicated synthetic-traffic key — issue one with
`--daily-cap-usd 0.50` and `--allow-model anthropic:claude-haiku-4-5`.
Send a 1-token request every 5 min; monthly cost lands under $1.

### Tier B — self-hosted (Uptime Kuma)

When the buyer's compliance team objects to "status reported by a SaaS
we don't control." Trade-off: a cluster-wide outage takes the status
page down too — fix is cross-region, at which point you're rebuilding
Statuspage.io for free.

```bash
helm repo add uptime-kuma https://uptime-kuma-helm.github.io/uptime-kuma
helm install kuma uptime-kuma/uptime-kuma \
    --namespace metis-ops --create-namespace \
    --set persistence.enabled=true --set persistence.size=1Gi
```

Expose on a separate hostname (`status.example.com`) with its own TLS
cert; do not co-locate with the gateway ingress. Same two probes as
Tier A, plus optionally a Kuma "Push" monitor wired to the trace-DB
SQL probe from
[`incident-response.md`](incident-response.md#on-call-alert-paths) —
the cron pushes only when the SQL probe returns clean. Restrict the
admin UI to a buyer VPN range via `NetworkPolicy`; the public read-only
view stays open.

---

## What to publish

- **Current overall status** — one of `operational` / `degraded` / `partial-outage` / `major-outage` / `maintenance`. Set by hand at triage.
- **Per-component status** — suggested components: `Gateway (OpenAI shape)`, `Gateway (Anthropic shape)`, `Analytics surface`, `Status page itself`.
- **Active incidents** — title, severity, current state, last-update timestamp.
- **Recent incidents (≤30 days)** — title, resolution timestamp, root-cause one-liner.
- **Scheduled maintenance** — posted ≥48h ahead (per [`sla-template.md`](sla-template.md) exclusions).
- **Uptime % rolling 90d** — auto-computed by the provider; do not hand-curate.
- **Component SLO targets** — only if your SLA references them; otherwise it invites litigation over numbers.

## What to redact

- **Always:** customer / tenant names ("one customer was affected"), internal hostnames or pod names, provider account IDs and billing, prompt and completion content, `gateway_key_id` values (side-channel tenant identifier), raw cost numbers in USD ("quota exceeded" is fine; "$8,432.10 over budget" is not).
- **Do publish:** upstream provider names — "Anthropic API degraded" is honest and useful, "an upstream LLM API" is corporate-speak.
- **Summarize:** root-cause detail at the layer that's true and useful. "WAL checkpoint stalled" is fine; line numbers are not.

---

## Communication templates

Plain text. Substitute the bracketed fields. Times in ISO 8601 UTC.

**Initial (within 15 min of detection):**
```
[INVESTIGATING] <Component> — <one-line user-visible symptom>
Posted: <YYYY-MM-DDTHH:MMZ>

Investigating reports of <symptom> affecting <component>. Customers
may experience <impact>. Next update by <YYYY-MM-DDTHH:MMZ>.
```

**Identified (within 1 hour):**
```
[IDENTIFIED] <Component> — <symptom>
Posted: <YYYY-MM-DDTHH:MMZ>

Cause: <plain-English summary; no internal component names>. We are
<mitigating action — failover / rollback / restart>. Next update by
<YYYY-MM-DDTHH:MMZ>.
```

**Mitigating (every 30 min until resolved):**
```
[MITIGATING] <Component> — <symptom>
Posted: <YYYY-MM-DDTHH:MMZ>

<Action in progress>. <Optional: % of traffic restored, ETA>. Next
update by <YYYY-MM-DDTHH:MMZ>.
```

**Resolution:**
```
[RESOLVED] <Component> — <symptom>
Posted: <YYYY-MM-DDTHH:MMZ>

Resolved as of <YYYY-MM-DDTHH:MMZ>. Duration: <HH:MM>. Root cause:
<one paragraph>. Post-mortem by <within 7 days>. Service-credit
claims per SLA at <link>.
```

**Scheduled maintenance (≥48h before):**
```
[SCHEDULED] <Component> — <e.g. "trace-DB upgrade">
Window: <YYYY-MM-DDTHH:MMZ> to <YYYY-MM-DDTHH:MMZ>
Expected impact: <none / brief degradation / brief unavailability>

During <unavailability window if any>, requests to <endpoint> will
<queue / fail / return 503>. Update posted when complete.
```

---

## Cadence

- **Initial:** within 15 min of SEV1; within 1 hour for SEV2. SEV3 / SEV4 are not status-page-worthy unless they cross into user-visible impact.
- **Updates:** every 30 min during an active SEV1 / SEV2, even if "still investigating." Silence erodes trust faster than the outage itself.
- **Resolution:** within 15 min of the incident closing internally.
- **Post-mortem link:** within 7 days, posted as the final update on the incident's detail page; structure per [`incident-response.md`](incident-response.md#post-mortem-template).

---

## See also

- [`incident-response.md`](incident-response.md), [`sla-template.md`](sla-template.md).
