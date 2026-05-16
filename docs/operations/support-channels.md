# Support channels

Operational support routing for launch day and the first paid customer:
one buyer-facing door, one private support room, one incident room, and
a clean handoff to the status page.

Pair with [`incident-response.md`](incident-response.md),
[`status-page.md`](status-page.md), and
[`concierge-onboarding.md`](concierge-onboarding.md). Use
[`first-customer-runbook.md`](first-customer-runbook.md) for
customer-specific names, dates, and account contacts.

---

## Channel Map

| Channel | Audience | Purpose | Owner |
|---|---|---|---|
| `support@<domain>` or shared inbox | Buyer | Public door for questions and breakage reports | Primary support owner |
| `#metis-support` | Internal | Triage non-incident customer questions | Primary support owner |
| `#metis-incidents` | Internal | Active SEV1-SEV3 coordination | Incident commander |
| Status page | Public | User-visible incidents and maintenance | Status-page operator |
| Customer thread | Buyer + owner | Concierge trial and conversion flow | Account owner |

Do not create a channel per bug. First-customer support needs fewer
places to look, not more.

---

## Roles

| Role | Owns |
|---|---|
| Primary support owner | Inbox, buyer thread, first label |
| Incident commander | Severity, cadence, mitigation priority |
| Gateway operator | Health checks, key ops, trace queries, rollback |
| Status-page operator | Public updates from [`status-page.md`](status-page.md#communication-templates) |
| Scribe | UTC timeline, commands, decisions, action items |

---

## Intake Labels

| Label | Meaning | Next step |
|---|---|---|
| `question` | How-to, install confusion, report interpretation | Answer in buyer thread |
| `task` | Non-urgent request, docs clarification, config help | Track as follow-up |
| `possible-incident` | User-visible breakage not yet reproduced | Run first-hour triage |
| `incident` | Confirmed SEV1-SEV3 | Move to `#metis-incidents` |

Support owner owns the label until incident commander accepts an
incident. After that, the incident commander owns severity and cadence.

---

## Routing Rules

Buyer-facing thread:

- Acknowledge the report.
- Ask for timestamp, endpoint shape, and whether the client sent `model`.
- Never ask for a full gateway key.
- Send resolution notes and next steps.

Private support channel:

- Triage notes, doc links, non-incident follow-ups, and ownership
  decisions.

Incident channel:

- Severity, current owner, commands run, mitigation decision,
  public-update drafts, and UTC timeline.

Status page:

- User-visible incidents and scheduled maintenance only.
- No tenant names, key ids, prompt content, or raw overage dollars.

---

## Launch-Day Coverage

| Window | Required coverage |
|---|---|
| T-60 to T+120 min | Primary support owner, gateway operator, status-page operator |
| First business day | Primary support owner checks inbox every 30 min |
| First 7 days | Daily trial-status check per concierge onboarding |
| Outside business hours | SEV1 wake path only unless SLA says otherwise |

If one human covers multiple roles, write that down before launch.

---

## Response Templates

Initial acknowledgement: `Thanks for the report. I am checking the
gateway now. Please send timestamp, endpoint shape, and whether this
affects one user or everyone. Do not paste the full gateway token.`

Possible incident: `We can reproduce the symptom and are treating this
as a possible incident. Current scope: <scope>. Next update by <time>.`

Resolved: `Resolved as of <YYYY-MM-DDTHH:MMZ>. Impact: <plain-English
impact>. Mitigation: <rollback / failover / restart / config change>.`

---

## Escalation

Escalate from support to incident if any are true:

- `/healthz` fails twice in 60 seconds.
- Either inbound shape returns sustained 5xx or timeouts.
- Provider auth or quota issue affects all keys.
- A paying customer's key appears compromised.
- A customer reports prompt or completion exposure.
- Trace DB integrity check fails.
- Spend projection crosses 2x the agreed daily budget.

Escalate SEV2 to SEV1 if blast radius grows to all gateway traffic,
security moves from suspected to active exploitation, or no mitigation is
available inside the SEV2 mitigation window.

---

## Redaction

Do not paste into shared channels:

- Full `gw_...` bearer tokens.
- Full provider API keys.
- Prompt or completion content.
- Customer emails unless the channel is account-private.
- Provider invoice screenshots.
- Raw stack traces that include request bodies.

Safe in private incident channels:

- `gateway_key_id`
- Pseudonymous `user_id` / `team_id`
- Trace event ids
- Provider names and model ids

When in doubt, summarize and offer a private handoff.

---

## First-Customer Handoff

Before the first buyer gets a key:

- [ ] Account owner is named.
- [ ] Primary support owner is named.
- [ ] Buyer thread is created and pinned.
- [ ] Day 0 opening email from
      [`concierge-onboarding.md`](concierge-onboarding.md#day-0-intake)
      is ready.
- [ ] Trial key is tagged `customer_tier=trial`.
- [ ] Daily cap is set.
- [ ] Buyer knows where to report install failures.
- [ ] Status-page target is known, even if DNS is not live.

During the trial, the buyer thread owns normal updates. The status page
owns incidents.

---

## End-Of-Day Close

The primary support owner posts a closeout with open buyer questions,
open incidents, trial traffic, follow-ups, and next coverage window.

Keep it boring and factual.
