# Status page

Public-facing uptime surface. Paired with
[`incident-response.md`](incident-response.md) (internal playbook)
and [`sla-template.md`](sla-template.md) (commitment).

Buyers expect "is it up right now, what broke recently, what's
scheduled" on day one. The recipes below cover that floor.

---

## Live deployment

**Status:** recipe ready-to-apply; hosting-account provisioning
remains owner-side. The helm sidecar option (below) lands the
self-hosted path as a single `values.yaml` toggle; the UptimeRobot
path is fully scripted as curl recipes. The two outstanding manual
steps are (a) flipping the toggle / running the curl against a real
account, and (b) pointing DNS at the result.

**Target hostname:** `https://status.2sum.ai` — agreed in Wave 14
(product-site nav badge + footer link already point here per the
`Wave 14 product-site GA polish` entry in CLAUDE.md / AGENTS.md).
DNS, TLS cert, and the actual deploy do not exist yet.

**Selected path (per Wave 11):** Tier B — Uptime Kuma self-hosted,
deployed via the helm chart's `statusPage.enabled` toggle. Rationale:
matches the open-core posture ("you run it"); avoids per-deployment
SaaS account provisioning; the documented Tier B trade-off
(cluster-wide outage takes the status page down) is acceptable for a
v1 single-region deployment.

Tier A (UptimeRobot / Better Stack) remains documented as the
off-failure-domain alternative for buyers whose compliance posture
requires it. The two paths are not mutually exclusive — running both
in parallel doubles cost (~$30/mo) and removes the failure-domain
risk.

### Provisioning checklist (owner-side)

Steps NOT automated by this chart/doc; the operator runs them once
per deployment:

1. **Decide the path** — flip `statusPage.enabled: true` in
   `values.yaml` (Tier B) OR provision an UptimeRobot / Better Stack
   account (Tier A). Both can coexist.
2. **DNS** — point `status.<your-domain>` at the chosen surface
   (Ingress for Tier B; the SaaS provider's CNAME for Tier A).
3. **TLS** — cert-manager / cloud-LB cert for Tier B; the SaaS
   provider issues for Tier A.
4. **Synthetic-traffic key** (probe #2 below) — issue with
   `metis gateway issue-key --daily-cap-usd 0.50 --allow-model
   anthropic:claude-haiku-4-5` and bundle into the probe.
5. **First-boot configuration** — paste the monitor + incident
   templates from the "Helm sidecar option" + "Monitoring checks"
   sections below into the Kuma UI (or post via UptimeRobot's API
   v2 — recipes below). If the hosting account is not provisioned yet,
   use [`status-page-config.yaml`](status-page-config.yaml) as the exact
   paste artifact for the owner-side setup ticket.

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

Two install paths — both ship Uptime Kuma; pick by who owns the
release:

**A. Helm sidecar (recommended — bundled with the gateway release).**

Wave 11 ships an opt-in `statusPage.enabled` toggle on the gateway
chart. Single helm release, one upgrade story, the status page lives
or dies with the gateway pod's cluster:

```bash
helm upgrade --install metis-gateway ./infra/gateway/helm/ \
    --namespace metis-gateway --create-namespace \
    --set provider.existingSecret=metis-providers \
    --set keystore.existingSecret=metis-keystore \
    --set statusPage.enabled=true \
    --set statusPage.ingress.enabled=true \
    --set statusPage.ingress.host=status.example.com \
    --set statusPage.ingress.className=nginx \
    --set 'statusPage.ingress.tls[0].secretName=status-page-tls' \
    --set 'statusPage.ingress.tls[0].hosts[0]=status.example.com'
```

What you get: a sibling Deployment + Service + PVC (and optional
Ingress) carrying `app.kubernetes.io/name=<release>-status-page` so
the gateway's Service can't accidentally route to the Kuma pod. PVC
defaults to 1Gi; Kuma's SQLite DB + monitor configs land there.
Resource defaults are conservative (100m CPU / 128Mi memory request)
because a status page is rarely a hot path. All knobs under
`statusPage.*` in [`infra/gateway/helm/values.yaml`](../../infra/gateway/helm/values.yaml).

Verify the install renders:

```bash
helm template test ./infra/gateway/helm/ \
    --set provider.anthropicApiKey=sk-test \
    --set statusPage.enabled=true \
    | grep -E "^(kind:|  name:)" | grep -A1 status-page
```

You should see PVC + Service + Deployment all with the `-status-page`
suffix. The `Recreate` strategy on the Deployment is deliberate —
Uptime Kuma's SQLite DB doesn't tolerate two concurrent writers
during a rolling upgrade.

**B. Upstream chart (alternative — separate release, separate
release cadence).**

```bash
helm repo add uptime-kuma https://uptime-kuma-helm.github.io/uptime-kuma
helm install kuma uptime-kuma/uptime-kuma \
    --namespace metis-ops --create-namespace \
    --set persistence.enabled=true --set persistence.size=1Gi
```

Use this when the status page is owned by a separate ops team or
when you need features (e.g. clustered HA) the sidecar chart doesn't
expose. The Wave 11 helm-sidecar option is a strict subset of what
the upstream chart can do; pick the upstream path if you outgrow it.

In both paths, expose on a separate hostname (`status.example.com`)
with its own TLS cert; do not co-locate with the gateway ingress.
Same probes as Tier A — see "Monitoring checks" below. Optionally
wire a Kuma "Push" monitor to the trace-DB SQL probe from
[`incident-response.md`](incident-response.md#on-call-alert-paths) —
the cron pushes only when the SQL probe returns clean. Restrict the
admin UI to a buyer VPN range via `NetworkPolicy`; the public
read-only view stays open.

---

## Monitoring checks

Four probes, paste these into the Kuma UI on first-boot (or post
via the UptimeRobot v2 API — recipes at the end of this section).
Each probe maps to a status-page component listed under "What to
publish" below; the gateway-key liveness check is new for Wave 10/11
and assumes the Prometheus `/metrics` endpoint shipped in Wave 11.

### Probe 1 — Gateway HTTP liveness (`/healthz`)

The canonical "is the gateway up" signal. Two consecutive failures
in 60s = SEV1 per
[`incident-response.md §On-call`](incident-response.md#on-call-alert-paths).

| Field             | Value                                       |
|-------------------|---------------------------------------------|
| Monitor type      | HTTP(s)                                     |
| URL               | `https://gateway.example.com/healthz`       |
| Method            | GET                                         |
| Interval          | 60s                                         |
| Timeout           | 10s                                         |
| Accepted statuses | 200                                         |
| Retries           | 1 (≥2 failures = page)                      |
| Component         | `Gateway (HTTP liveness)`                   |

### Probe 2 — Synthetic request (round-trip)

Catches the failure mode `/healthz` misses: the process is up but
the routing chain or provider adapter is broken. Costs $1/mo at 1
call per 5 min on haiku.

| Field             | Value                                                                  |
|-------------------|------------------------------------------------------------------------|
| Monitor type      | HTTP(s) keyword OR Kuma "Push" with curl cron                          |
| URL               | `https://gateway.example.com/v1/messages`                              |
| Method            | POST                                                                   |
| Headers           | `x-api-key: $SYNTHETIC_KEY`, `anthropic-version: 2023-06-01`, `content-type: application/json` |
| Body              | `{"model": "anthropic:claude-haiku-4-5", "max_tokens": 1, "messages": [{"role":"user","content":"ping"}]}` |
| Keyword           | `"type":"message"`                                                     |
| Interval          | 300s                                                                   |
| Accepted statuses | 200, 401                                                               |
| Component         | `Gateway (Anthropic shape)`                                            |

A second copy hitting `/v1/chat/completions` with an OpenAI-shape
body covers `Gateway (OpenAI shape)`.

### Probe 3 — `/metrics` heartbeat (Wave 11)

The gateway's Prometheus exposition endpoint is also the cheapest
"the process is healthy enough to report metrics" canary —
`/healthz` only checks the ASGI app boots; `/metrics` exercises the
event-bus subscriber path and the `MetricsCollector` registry. If
the bus has stalled, this 200s.

| Field             | Value                                       |
|-------------------|---------------------------------------------|
| Monitor type      | HTTP(s) keyword                             |
| URL               | `https://gateway.example.com/metrics`       |
| Method            | GET                                         |
| Keyword           | `metis_gateway_keys_active`                 |
| Interval          | 60s                                         |
| Timeout           | 10s                                         |
| Accepted statuses | 200                                         |
| Component         | `Gateway (metrics surface)`                 |

Note: production deployments should put `/metrics` behind a
NetworkPolicy that only allows scraping from the Prometheus pod's
namespace. The status-page probe lives inside that namespace, so
this is compatible — what's not compatible is exposing `/metrics`
to the public internet just so an external Tier-A probe can read
it. For Tier A, lift this probe out and rely on probes 1, 2, 4 only.

### Probe 4 — Gateway-key liveness (Wave 10 + 11)

Catches the "keystore is empty / corrupt" failure mode where the
gateway boots, `/healthz` returns 200, but no key resolves so every
real client request 401s. Reads `metis_gateway_keys_active` (the
Wave 11 metric, gauge of `status="active"` keys in the keystore).

| Field             | Value                                                |
|-------------------|------------------------------------------------------|
| Monitor type      | Kuma "Push" wired to a cron, OR HTTP keyword         |
| Push interval     | 60s                                                  |
| Cron probe        | see SQL recipe below                                 |
| Component         | `Keystore (active keys)`                             |

Cron recipe (paste under a Kuma Push monitor's "Push URL" callback,
or run from the same node that hosts the Prometheus scrape target):

```bash
* * * * * gateway-ops \
  ACTIVE=$(curl -fsS http://metis-gateway.metis-gateway.svc:8422/metrics \
    | awk '/^metis_gateway_keys_active /{print $2}' | head -1) ; \
  if [ "${ACTIVE:-0}" -lt 1 ]; then \
    curl -fsS -X POST "$SLACK_HOOK" -d '{"text":"gateway keystore empty"}'; \
  else \
    curl -fsS "$KUMA_PUSH_URL?status=up&msg=keys=$ACTIVE"; \
  fi
```

Threshold: `< 1` active key = SEV1 (gateway is effectively offline
for paying tenants). `metis_gateway_keys_active` dropping by ≥ 50%
within 5 min without a corresponding `gateway.key_rotated` audit
event = SEV2 (unexpected mass-revocation; check
[`audit-log.md`](../specs/audit-log.md) for the cause).

### Configuration via UptimeRobot API v2 (Tier A only)

If you flip Tier A on instead of (or alongside) Tier B, the four
probes above can be created in one shot via curl. Stash
`UPTIMEROBOT_API_KEY` from your account's "API Settings" page first:

```bash
# Probe 1 — /healthz
curl -X POST https://api.uptimerobot.com/v2/newMonitor \
  -d "api_key=$UPTIMEROBOT_API_KEY&format=json&type=1" \
  -d "url=https://gateway.example.com/healthz" \
  -d "friendly_name=Metis%20Gateway%20-%20healthz" \
  -d "interval=60"

# Probe 3 — /metrics keyword
curl -X POST https://api.uptimerobot.com/v2/newMonitor \
  -d "api_key=$UPTIMEROBOT_API_KEY&format=json&type=2" \
  -d "url=https://gateway.example.com/metrics" \
  -d "keyword_type=1&keyword_value=metis_gateway_keys_active" \
  -d "friendly_name=Metis%20Gateway%20-%20metrics%20heartbeat" \
  -d "interval=60"
```

Probes 2 (synthetic POST with body + headers) and 4 (gateway-key
liveness) need a paid UptimeRobot plan for POST-with-body and
keyword monitors over headers; on the free tier, run them as Kuma
Push monitors driven by a curl cron on a workstation. Better Stack
supports POST-with-body on its free 10-monitor tier.

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

## Severity-mapped templates (pre-load these)

The templates above are by *stage* (initial / identified / etc.).
The four below are the same content pre-instantiated for each
severity from [`incident-response.md §Severity levels`](incident-response.md#severity-levels).
Paste them as saved incident templates in your status-page provider
(Statuspage.io / Better Stack support named templates; Uptime Kuma
1.x doesn't — keep them as a copy-paste cheat-sheet in the operator
runbook). Overall status maps strictly:

| Severity | Overall status        | Cadence    | Initial-update target |
|----------|------------------------|------------|------------------------|
| SEV1     | `major-outage`         | 30 min     | 15 min from detection  |
| SEV2     | `partial-outage`       | 30 min     | 1 hour from detection  |
| SEV3     | `degraded`             | 4 hours    | 1 business day         |
| SEV4     | (not status-page-worthy unless user-visible impact)            |

### SEV1 template (`major-outage`)

```
[INVESTIGATING] <Component> — <user-visible symptom>
Posted: <YYYY-MM-DDTHH:MMZ>

Investigating reports of <symptom> affecting <component>. Customers
are unable to <e.g. "complete LLM requests via the Anthropic-shape
gateway endpoint">. We are <mitigating action — rollback / failover /
restart>. Next update by <YYYY-MM-DDTHH:MMZ (set 30 min out)>.

Overall status set to: major-outage
Affected components: <list — Gateway (HTTP liveness), Gateway
(Anthropic shape), etc.>
```

SEV1 triggers per [`incident-response.md`](incident-response.md):
total gateway outage, irrecoverable trace-DB corruption, suspected
key compromise with active exploitation, prompt/completion exposure,
provider bill threshold breached.

### SEV2 template (`partial-outage`)

```
[INVESTIGATING] <Component> — <user-visible symptom>
Posted: <YYYY-MM-DDTHH:MMZ>

Investigating elevated <error rate / latency> on <component — e.g.
"the OpenAI-shape inbound" or "Anthropic upstream calls">. Customers
using <impact scope — e.g. "the OpenAI shape" or "models routed to
Anthropic"> may experience <impact>. Other components are operating
normally. Next update by <YYYY-MM-DDTHH:MMZ (set 30 min out)>.

Overall status set to: partial-outage
Affected components: <one-of, not all>
```

SEV2 triggers: one inbound shape down; one upstream provider down
with no failover; per-key analytics rollup wrong by ≥10%; ingress
TLS expired.

### SEV3 template (`degraded`)

```
[INVESTIGATING] <Component> — <user-visible symptom>
Posted: <YYYY-MM-DDTHH:MMZ>

Elevated <latency / non-default-model unavailability / quota-alert
volume> on <component>. Customer impact is <minimal / single-tenant
/ confined to non-default routes>. Working on <mitigation>. Next
update by <YYYY-MM-DDTHH:MMZ (set 4 hours out)>.

Overall status set to: degraded
Affected components: <component>
```

SEV3 triggers: elevated latency (p95 > 2× baseline); trace-DB near
disk-full; a non-default model unavailable; one tenant's quota-alert
spamming.

### SEV4 (do NOT post)

SEV4 is cosmetic / log noise / doc errors. It is not status-page-
worthy unless it crosses into user-visible impact, at which point
re-evaluate per
[`incident-response.md`](incident-response.md#severity-levels) —
typical reclassification is SEV4 → SEV3.

If you find yourself reaching for "publish a SEV4," the post itself
will erode trust faster than the issue does. Internal channel only.

---

### Pre-load these incident templates (provider-specific)

- **Statuspage.io**: Settings → Page → Incident Templates → "Create
  template." Name them `metis-sev1-major-outage` /
  `metis-sev2-partial-outage` / `metis-sev3-degraded`; paste each
  body. Bind each template to the matching default impact (Major /
  Partial / Minor) and the affected components below ("What to
  publish").
- **Better Stack**: Status Pages → your page → Incidents → "New
  Template." Same naming.
- **Uptime Kuma (1.x)**: no first-class incident-template surface.
  Save the four bodies above in the operator runbook (e.g. as a
  pinned channel message in `#metis-incidents`) and copy-paste into
  the "Add Incident" form on the status page when posting. v2.x
  reportedly adds templates; check the release notes when you
  upgrade.
- **UptimeRobot**: paid plans expose `https://api.uptimerobot.com/v2/
  setPSPMessage` for status-page text; on free / Solo tiers, paste
  manually.

---

## Cadence

- **Initial:** within 15 min of SEV1; within 1 hour for SEV2. SEV3 / SEV4 are not status-page-worthy unless they cross into user-visible impact.
- **Updates:** every 30 min during an active SEV1 / SEV2, even if "still investigating." Silence erodes trust faster than the outage itself.
- **Resolution:** within 15 min of the incident closing internally.
- **Post-mortem link:** within 7 days, posted as the final update on the incident's detail page; structure per [`incident-response.md`](incident-response.md#post-mortem-template).

---

## See also

- [`incident-response.md`](incident-response.md), [`sla-template.md`](sla-template.md).
