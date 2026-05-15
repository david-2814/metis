# SLA template

A starting-point SLA for a buyer to commit to *their* downstream users.
Metis ships open-core — you (the buyer) run the gateway and sign SLAs
with the teams you serve through it.

**Not legal advice.** Engineering-grade template only. Force majeure,
indemnity, liability caps, jurisdiction — defer to legal counsel before
signing. Paired with [`incident-response.md`](incident-response.md) and
[`status-page.md`](status-page.md).

---

## Service definition

The Service is the LLM gateway operated by `<Provider>` at
`<gateway-url>`: the gateway accepting `/v1/chat/completions` and
`/v1/messages`; the analytics surface (`/analytics/*`) if exposed; the
status page at `<status-url>`.

**Out of scope:** upstream LLM providers (Anthropic, OpenAI, OpenRouter),
Customer-side network, Customer client applications.

---

## Availability commitment (v1 single-region)

`<Provider>` commits to **99.5% monthly availability**, measured per
calendar month, UTC. 99.5% allows ~3h 39m of unavailability per 30-day
month. Do not commit to 99.99% (~4 min/month) without multi-region
failover, 24/7 on-call, and cost structure to back it — numbers higher
than a single-region SQLite + uvicorn stack can deliver are
litigation-bait.

`availability = 1 - (unavailable_minutes / total_minutes_in_month)`. A
minute is **unavailable** if `GET /healthz` returns non-2xx, times out
(>5 s), or `POST /v1/chat/completions` / `POST /v1/messages` with a
valid bearer returns 5xx for more than 50% of synthetic probes in that
minute. Measured by `<external probe source>`.

---

## Service credits

Customer's exclusive remedy for missing the commitment is a credit
against the following month's invoice:

| Monthly availability      | Credit (% of monthly fee) |
|---------------------------|----------------------------|
| ≥ 99.5%                   | 0%                         |
| 99.0% to < 99.5%          | 10%                        |
| 95.0% to < 99.0%          | 25%                        |
| < 95.0%                   | 50%                        |

Only the matching row applies (not cumulative). Capped at 50% of the
monthly fee, no roll-over, no cash value. Claim in writing within 30
days, referencing status-page incidents.

---

## Exclusions

Unavailability from any of these does **not** count:

1. **Scheduled maintenance** announced ≥ 48h in advance, inside a published window. Cap: ≤ 4 hours / month.
2. **Upstream LLM provider outages** (Anthropic, OpenAI, OpenRouter). Each has its own SLA; the gateway cannot exceed its dependencies' floor.
3. **Customer-induced** — exceeded `daily_cap_usd` / `monthly_cap_usd`; revoked or rotated keys past grace; Customer network failures; content-policy violations.
4. **Force majeure** — natural disasters, acts of war, regional cloud outages, government action. (Terms drawn by legal counsel.)
5. **Beta / preview features** flagged on the status page or release notes — excluded until GA.
6. **Security-driven downtime** — emergency patching for a high/critical CVE where exposure exceeds 1 hour. Capped at 2 hours / month.

---

## Support response

Severity definitions in [`incident-response.md`](incident-response.md#severity-levels). "Initial response" is on-call ack, not resolution. Resolution targets are best-effort; the contractual obligation is availability above.

| Severity | Initial response | Update cadence       | Resolution target |
|----------|------------------|----------------------|--------------------|
| SEV1     | 15 min, 24/7     | Every 30 min         | 4 hours            |
| SEV2     | 1 hour, 24/7     | Every 1 hour         | 1 business day     |
| SEV3     | 1 business day   | Every 1 business day | 1 week             |
| SEV4     | Best effort      | On request           | Next sprint        |

---

## Reporting, term, amendments

`<Provider>` publishes a monthly availability report by the 10th —
percentage, incident summaries with timestamps, excluded minutes by
category. Last 12 archived on the status page. SLA takes effect on
Customer's first authenticated request and runs with the underlying
service agreement. `<Provider>` may amend with 30 days' written notice;
amendments are not retroactive. Service-credit claims survive
termination for 30 days.

---

See also: [`incident-response.md`](incident-response.md), [`status-page.md`](status-page.md), [`../gateway-deployment.md`](../gateway-deployment.md).
