# SOC2 readiness — gap audit

Not a certification. Not legal or audit advice. This document maps the
SOC2 Trust Service Criteria (TSC) against Metis as shipped and as it
will ship at the close of Wave 12, so a buyer's compliance team has a
plain-text answer to "what's there, what's not, who's responsible for
the rest." Pair with [`compliance-overview.md`](compliance-overview.md)
for the one-page index.

> **Posture.** Metis is open-core software, not a managed service.
> Metis does not hold a SOC2 attestation. A buyer who wants a SOC2-
> audited *deployment* runs the gateway inside their own SOC2-audited
> cloud account (AWS / GCP / Azure all carry SOC2 Type 2 reports
> covering physical security, network controls, and infrastructure
> availability), layers TLS and identity in front, and presents this
> document plus the supporting spec set as the application-layer
> control evidence. The cert path for *Metis itself* is post-GA work.

---

## 1. How to read the table

Each criterion gets four columns:

- **Status** — one of:
  - **implemented** — shipped behavior, evidence pointer exists.
  - **partial** — primitives exist; an auditor would want more.
  - **gap** — nothing shipped; named so the buyer can scope it.
  - **buyer-responsibility** — outside Metis's control surface.
- **Evidence** — file path, spec section, runbook, or CLI command.
- **Buyer additions** — what the operator must layer (TLS terminator,
  cloud baseline, IdP, etc.).
- **Wave 12 delta** — whether the Wave 12 spec triad
  ([`audit-log.md`](../specs/audit-log.md),
  [`trace-retention.md`](../specs/trace-retention.md),
  [`redaction.md`](../specs/redaction.md) — drafted by sibling 12a-1 /
  12a-2 / 12a-3, all shipped 2026-05-15) changes the status.

Criteria are grouped by TSC category. The Trust Service Categories
referenced are **Security (the Common Criteria CC1–CC9)**,
**Availability (A1)**, **Confidentiality (C1)**, **Processing
Integrity (PI1)**, and **Privacy (P1–P8)** — the standard SOC2 2017
TSC set.

---

## 2. Common Criteria (CC1–CC9)

### CC1 — Control Environment

Organizational-level controls (board oversight, code of conduct, HR
policies, competence). Almost entirely buyer-responsibility: Metis is
software, not an organization.

| Criterion | Status | Evidence | Buyer additions |
|-----------|--------|----------|-----------------|
| CC1.1 Integrity and ethical values | buyer-responsibility | — | Code of conduct, ethics policy, background-check program. |
| CC1.2 Board oversight | buyer-responsibility | — | Board / audit-committee charter. |
| CC1.3 Organizational structure | buyer-responsibility | — | Org chart, reporting lines, hosting account org units. |
| CC1.4 Commitment to competence | buyer-responsibility | — | Hiring / training records for the team operating the gateway. |
| CC1.5 Accountability | buyer-responsibility | — | RACI for incident response, key rotation, deploys. |

Metis side: the operations docs ([`incident-response.md`](incident-response.md),
[`sla-template.md`](sla-template.md), [`upgrade-guide.md`](upgrade-guide.md))
name *roles* (on-call, operator) without binding them to people — that
binding is the buyer's HR system's job.

### CC2 — Communication and Information

| Criterion | Status | Evidence | Buyer additions |
|-----------|--------|----------|-----------------|
| CC2.1 Internal information for control | partial | [`AGENTS.md`](../../AGENTS.md), [`docs/specs/`](../specs/). Specs-first discipline (`AGENTS.md` "Specs-first"). | Buyer-internal runbooks layered on these. |
| CC2.2 Internal communication of controls | partial | [`incident-response.md`](incident-response.md) post-mortem template; [`upgrade-guide.md`](upgrade-guide.md). | Internal training cadence; control-change-notification process. |
| CC2.3 External communication | partial | [`status-page.md`](status-page.md) cadence + redaction rules; [`sla-template.md`](sla-template.md); [`docs/gateway-client-quickstart.md`](../gateway-client-quickstart.md). | Customer-facing terms of service, privacy notice, DPA. |

### CC3 — Risk Assessment

Buyer-responsibility. Metis cannot make risk-assessment decisions for
an organization it does not know.

| Criterion | Status |
|-----------|--------|
| CC3.1–CC3.4 (objectives, identification, fraud, change-driven risk) | buyer-responsibility |

Metis side: [`docs/KNOWN_ISSUES.md`](../KNOWN_ISSUES.md) is the carry-over
review log; [`docs/specs/CHANGES.md`](../specs/CHANGES.md) tracks
cross-spec drift. Both are useful inputs to a buyer's CC3.4 (change-
driven risk re-assessment) but neither substitutes for it.

### CC4 — Monitoring Activities

| Criterion | Status | Evidence | Buyer additions |
|-----------|--------|----------|-----------------|
| CC4.1 Ongoing / periodic evaluation | partial | `/metrics` Prometheus surface ([`observability.md`](../specs/observability.md), Wave 11); `/healthz` / `/health` probes; trace-DB SQL probes documented in [`incident-response.md`](incident-response.md) "On-call alert paths". | Prometheus / Alertmanager / paging stack; quarterly control walk-through. |
| CC4.2 Communicate deficiencies | partial | Post-mortem template ([`incident-response.md`](incident-response.md) "Post-mortem template"); status-page cadence. | Internal deficiency-tracking system (Jira / Linear / similar). |

### CC5 — Control Activities

Metis ships *control primitives* (CLI commands, configuration knobs);
the *control activities* (segregation of duties, periodic review) are
buyer-side composition.

| Criterion | Status | Evidence | Buyer additions |
|-----------|--------|----------|-----------------|
| CC5.1 Selection / development of controls | partial | CLI primitives: `metis gateway issue-key` / `revoke-key` / `rotate-key` / `list-keys` ([`gateway.md §11`](../specs/gateway.md)). | Control-selection rationale documented for the auditor. |
| CC5.2 Technology controls | partial | Workspace-scoped tool API ([`tool-dispatcher.md §5.1`](../specs/tool-dispatcher.md)); JSON-Schema validation; trust.yaml `always_allow` / `always_deny`. | Cloud-baseline controls (IAM, KMS, VPC). |
| CC5.3 Policies and procedures | partial | Spec set under [`docs/specs/`](../specs/). | Buyer-internal policy set (acceptable-use, retention, BCP). |

### CC6 — Logical and Physical Access Controls

**Where Wave 9 / 10 / 12 close major gaps.** This is the most
auditor-scrutinized category for an LLM gateway because keys =
spending power.

| Criterion | Status | Evidence | Buyer additions |
|-----------|--------|----------|-----------------|
| CC6.1 Restrict logical access | implemented | Gateway-key bearer auth, SHA-256 hash persistence (plaintext printed once), `0o600` keystore mode ([`gateway.md §3.3`](../specs/gateway.md)). Workspace-scoped file API rejects `..` / out-of-root symlinks ([`tool-dispatcher.md §5.1`](../specs/tool-dispatcher.md)). | Network ACLs in front of the loopback bind. |
| CC6.2 Register / authorize new users | partial | `metis gateway issue-key --user <id> --team <id>` ([`multi-user.md §3 / §4.2`](../specs/multi-user.md), Wave 9). `users.json` carries the identity layer; per-user / per-team rollups via `/analytics/by_user`, `/analytics/by_team`. | IdP / SSO integration (deliberately deferred in [`multi-user.md §8.1`](../specs/multi-user.md)); HR offboarding hook. |
| CC6.3 Credential modification / removal | implemented | `metis gateway rotate-key --grace-period 24h` (default 24h grace); `revoke-key` (immediate, 401 `code=key_revoked` per [`gateway.md §11`](../specs/gateway.md)); audit-event emission (`gateway.key_issued` / `gateway.key_revoked` / `gateway.key_rotated`, Wave 10). | Quarterly key-rotation calendar; secure plaintext-token handoff (1Password / Signal — never email). |
| CC6.4 Restrict physical access | buyer-responsibility | — | Cloud provider's SOC2 Type 2 covers physical (AWS / GCP / Azure all carry attestations). |
| CC6.5 Protect against unauthorized access | partial | Loopback-only bind enforced ([`gateway.md §3.2`](../specs/gateway.md), [`server-api.md §3.1`](../specs/server-api.md) — non-loopback host silently rewritten to `127.0.0.1`). | TLS terminator in front (Caddy / nginx-ingress / cloud LB — pattern in [`gateway-deployment.md`](../gateway-deployment.md) "TLS termination"); CIDR allowlists if exposed beyond a single VM. |
| CC6.6 Boundary protection | partial | Loopback bind + TLS-terminator pattern; rate-limit middleware opt-in ([`gateway-hardening.md §3`](../specs/gateway-hardening.md), Wave 11; `enabled=False` default). | Firewall / WAF / DDoS edge (Cloudflare / AWS Shield); enable `RateLimitConfig` per buyer's policy. |
| CC6.7 Transmission, removal, disposal of data | partial | At-rest: single SQLite file on operator-provided volume — encryption is the volume's job. In-transit: TLS-terminator's job. Disposal: `metis backup` / `metis restore` + manual `DELETE FROM events WHERE timestamp < …` recipe ([`gateway-deployment.md`](../gateway-deployment.md) "Trace DB size management"). | Volume encryption (AWS EBS at-rest is on by default; GCP PD encrypts by default; Azure Managed Disks encrypt by default — all SOC2-attested at the storage layer). |
| CC6.8 Prevent / detect malicious software | gap | No CVE scanning of the Docker image in v1; no malware policy on workspace file ops. | Image scanning (Trivy / Snyk / Anchore); EDR on the host. |

**Wave 12 delta (CC6):** [`audit-log.md`](../specs/audit-log.md)
v1 (Wave 12, 12a-1) lifts CC6.1 / CC6.2 / CC6.3 from "primitives
shipped" to "primitives shipped + an auditor-readable export." The
v1 audit subset is 9 event types ([§4](../specs/audit-log.md)):
`gateway.key_issued`, `gateway.key_revoked`, `gateway.key_rotated`,
`gateway.quota_exceeded`, `quota.alert`, `routing.policy_invalid`,
`memory.eviction`, `pattern.evicted`, `tool.confirmation_resolved` —
all flagged `audit: True` in the payload registry, all
retention-exempt by virtue of the [`trace-retention.md §5`](../specs/trace-retention.md)
audit-event exemption, all exportable via `metis audit export
<dest>` ([§9](../specs/audit-log.md)) in JSONL or CSV with
deterministic byte-for-byte output for diff-based auditor review.

### CC7 — System Operations

**Where Wave 10 / 11 / 12 close major gaps.** Day-2 operations are
the second-most-scrutinized category — backup, monitoring, incident
response, recovery.

| Criterion | Status | Evidence | Buyer additions |
|-----------|--------|----------|-----------------|
| CC7.1 Detect vulnerabilities | gap | No automated CVE / dep-scan pipeline in v1. Specs-first review discipline catches design-level issues. | Image scanning, SCA (Software Composition Analysis), `pip-audit` in CI. |
| CC7.2 Monitor for anomalies | partial | `/metrics` (Wave 11, [`observability.md`](../specs/observability.md)) — 10 metric series including `metis_llm_calls_total{provider,model,status}` / `metis_quota_used_ratio{identity_kind,identity_id}`. `quota.alert` event + per-key `daily_cap_usd` / `monthly_cap_usd` ([`multi-user.md §5.1`](../specs/multi-user.md)). Trace-DB SQL probe pattern ([`incident-response.md`](incident-response.md)). | Prometheus + Alertmanager (or vendor); on-call rotation; anomaly thresholds. |
| CC7.3 Evaluate and respond to incidents | implemented | [`incident-response.md`](incident-response.md) — SEV1–SEV4 criteria, first-hour playbook, per-failure-mode playbooks (upstream LLM outage, trace-DB corruption, gateway-key compromise, quota runaway), blameless post-mortem template. | Buyer-side on-call schedule, paging provider, war-room channel. |
| CC7.4 Disaster recovery | implemented | `metis backup` / `metis restore` (Wave 10, [`event-bus-and-trace-catalog.md §7.5`](../specs/event-bus-and-trace-catalog.md)); `VACUUM INTO`-based hot snapshots, schema-version guarded restore; helm rolling-update + `--atomic` rollback ([`upgrade-guide.md`](upgrade-guide.md)). Restore drill recipe in [`gateway-deployment.md`](../gateway-deployment.md). | Off-site backup target; restore-drill cadence (quarterly minimum); RPO / RTO commitment to *their* downstream users. |
| CC7.5 Recovery from identified events | implemented | Per-failure-mode playbooks in [`incident-response.md`](incident-response.md). Rollback recipe in [`upgrade-guide.md`](upgrade-guide.md). | Tabletop exercises against the playbooks. |

**Wave 12 delta (CC7):** [`trace-retention.md`](../specs/trace-retention.md)
v1 (Wave 12, 12a-2) replaces the v1 manual recipe ("`DELETE FROM
events WHERE timestamp < …; VACUUM`") with a configurable sweep
that runs a single SQL `DELETE` constrained by `timestamp_us <
cutoff_us AND type NOT IN (<AUDIT_EVENT_TYPES>)` per
[`§3.1`](../specs/trace-retention.md). Default `retention_days=90`
([`§2.1`](../specs/trace-retention.md)) covers any realistic
billing / debugging window; helm `traceRetention.days` overrides;
audit-exempt event types ([`§5`](../specs/trace-retention.md))
are preserved unconditionally. `metis trace prune --apply` is the
CLI surface ([`§7.1`](../specs/trace-retention.md)) and a
Kubernetes `CronJob` template ([`§8`](../specs/trace-retention.md))
ships in the helm chart. SOC2 expects a documented retention
policy with a defensible justification — Wave 12 ships the
scaffold; the buyer ratifies the number. [`audit-log.md`](../specs/audit-log.md)
v1 extends the CC7.3 evidence pointer: the post-incident audit
trail becomes a deterministic JSONL / CSV export via `metis audit
export`, not an SRE manually running SQL.

### CC8 — Change Management

| Criterion | Status | Evidence | Buyer additions |
|-----------|--------|----------|-----------------|
| CC8.1 Authorize, design, develop, configure, document, test, approve, implement changes | **gap (honest)** | PR-based workflow + test suite (1678 tests passing); specs-first discipline ([`docs/specs/CHANGES.md`](../specs/CHANGES.md) cross-spec change log). No formal change-advisory board, no CAB approval workflow, no separated dev / test / prod environments at the *Metis project* level. | Buyer wraps deploys in their own change-management process; staging vs prod cluster split; CAB approvals if their compliance team requires. |

**Honest framing.** Metis is maintained by a small part-time team.
A formal change-management process at the project layer would be
ceremony without substance. SOC2 auditors will accept "the deploying
organization owns CC8" provided the buyer's deploy pipeline (`helm
upgrade --atomic` per [`upgrade-guide.md`](upgrade-guide.md)) sits
inside a buyer-side change-management workflow.

### CC9 — Risk Mitigation

| Criterion | Status | Evidence | Buyer additions |
|-----------|--------|----------|-----------------|
| CC9.1 Risk mitigation activities | partial | `metis backup` / restore; loopback bind; per-key spend caps; rate-limit middleware. | Cyber-insurance policy; BCP plan; tabletop schedule. |
| CC9.2 Vendor / business-partner management | **gap (honest)** | No formal vendor security review of Anthropic / OpenAI / OpenRouter. Public DPAs exist (Anthropic's [`anthropic.com/trust`](https://www.anthropic.com/trust); OpenAI's enterprise terms; OpenRouter's terms) but Metis has not signed them on behalf of buyers — the buyer's account at the upstream provider is the contracting party. | Buyer signs DPA / BAA / security questionnaire with each LLM provider they enable. Review providers' SOC2 reports under NDA. |

---

## 3. Availability (A1)

| Criterion | Status | Evidence | Buyer additions |
|-----------|--------|----------|-----------------|
| A1.1 Capacity planning | partial | Per-request stateless gateway ([`gateway.md §2`](../specs/gateway.md)); helm `replicas` / `resources` knobs ([`upgrade-guide.md`](upgrade-guide.md)); trace-DB pruning recipes. | Right-sizing for buyer's traffic; PVC monitoring (80% alert threshold documented in [`incident-response.md`](incident-response.md)). |
| A1.2 Environmental protections | buyer-responsibility | — | Cloud provider's data-center attestation. |
| A1.3 Recovery and continuity | implemented | Backup / restore; rolling upgrade; `--atomic` rollback; 99.5% SLA template ([`sla-template.md`](sla-template.md)). | Off-site backup; multi-AZ (helm chart is single-region, single-cluster — multi-region is buyer composition). |

**Honest scope note.** The SLA template is **99.5% single-region**
explicitly because the v1 deployment is single-pod / single-PVC /
single-AZ at most buyer scales. A buyer signing 99.99% with their
downstream users will not get there with the shipped helm chart
unchanged — they need active-passive multi-region or active-active
read-only-replicas, neither of which is documented in v1.
[`sla-template.md`](sla-template.md) flags this directly.

---

## 4. Confidentiality (C1)

**Where Wave 12 redaction closes major gaps.**

| Criterion | Status | Evidence | Buyer additions |
|-----------|--------|----------|-----------------|
| C1.1 Identify and maintain confidential information | partial | Sensitivity classification on every event type — `private` / `user_controlled` / `pseudonymous` / `aggregatable` ([`event-bus-and-trace-catalog.md §4.4`](../specs/event-bus-and-trace-catalog.md)). Catalog declares the *floor* (worst-case privacy); actual sensitivity may downgrade based on opt-in fields (§4.4.1). | Buyer-side data-classification policy mapping the four classes to their internal taxonomy. |
| C1.2 Dispose of confidential information | partial | Trace-DB pruning (`DELETE FROM events WHERE timestamp < …; VACUUM`). Memory store has eviction events but is append-only on disk per [`memory-store.md`](../specs/memory-store.md). | Retention policy ratification; secure disposal of decommissioned volumes. |

**Wave 12 delta (C1):** [`redaction.md`](../specs/redaction.md)
v1 (Wave 12, 12a-3) introduces an **export-time** `EventRedactor`
that runs *at export*, not at recording — the append-only trace
store invariant is preserved at recording per
[`§1`](../specs/redaction.md). Four modes ([`§2`](../specs/redaction.md)):

- `passthrough` — no redaction (single-tenant self-hosted default).
- `pseudonymize` — SHA-256-hashes identity fields (`user_id`,
  `team_id`, `session_id`, `parent_session_id`, `workspace_*`,
  `gateway_key_id`); costs / tokens / model names verbatim
  ([`§3.1`](../specs/redaction.md)).
- `redact_private` — `pseudonymize` plus replaces `PRIVATE`-tier
  text fields (`turn.started.user_message_text_redacted`,
  `tool.completed.files_modified` / `command_executed`,
  `tool.failed.error_message`, `llm.call_failed.error_message_redacted`,
  `turn.completed.signals_extra` keys `user_prompt_text` /
  `assistant_response_text`) with the `"[REDACTED]"` sentinel
  ([`§3.2`](../specs/redaction.md)).
- `aggregate_only` — drops per-row payloads; emits a single
  aggregate JSON object (counts, sums, distinct sessions / users).

The CLI surface is `metis audit export --redact <mode>`. The C1
evidence pointer is now "events are sensitivity-tagged AND every
export path runs them through a redactor whose mode is auditable
in the export header."

**Loopback-only posture is the load-bearing C1 control today.** The
trace DB never crosses a network boundary except via operator-
controlled `metis backup` / SQL export; raw prompts and completions
do not leave the operator's host without explicit operator action.
This is the same posture as [`gateway.md §3.2`](../specs/gateway.md)
and [`server-api.md §3.1`](../specs/server-api.md).

---

## 5. Processing Integrity (PI1)

| Criterion | Status | Evidence | Buyer additions |
|-----------|--------|----------|-----------------|
| PI1.1 Define processing requirements | implemented | Canonical message format ([`canonical-message-format.md`](../specs/canonical-message-format.md)) — load-bearing data contract; `msgspec`-validated, JSON-roundtrippable. | — |
| PI1.2 Inputs are complete, valid, authorized | implemented | JSON-Schema validation on tool inputs ([`tool-dispatcher.md`](../specs/tool-dispatcher.md)); adapter capability gates ([`provider-adapter-contract.md`](../specs/provider-adapter-contract.md)); per-key `allowed_models` check ([`gateway.md §5.5`](../specs/gateway.md)). | — |
| PI1.3 System processing produces complete, accurate outputs | implemented | Per-turn `route.decided` chain trace (every decision recorded); `Decimal` cost math with `pricing_version` stamped ([`AGENTS.md "Implementation conventions"`](../../AGENTS.md)); 1678-test suite covering canonical round-trips, role-content invariants, retry / cancellation, cross-provider continuity. | — |
| PI1.4 Outputs are complete, accurate, distributed | implemented | Append-only trace store (WAL); 31-event closed catalog ([`event-bus-and-trace-catalog.md §6`](../specs/event-bus-and-trace-catalog.md)); causal-chain integrity test (`tests/events/`). | — |
| PI1.5 Stored data is complete, accurate, secured | partial | SQLite WAL + `synchronous=NORMAL`; schema-versioned restore ([`event-bus-and-trace-catalog.md §7.5`](../specs/event-bus-and-trace-catalog.md)); per-(provider, model) availability tracking surfaces error rates. | At-rest encryption (cloud-provider volume encryption); backup integrity verification. |

PI1 is **the strongest TSC for Metis** because the canonical message
format + event-bus catalog + cost-attribution math are the load-
bearing engineering output of the project. The discipline that makes
the canonical format trustworthy (`msgspec.Struct(frozen=True)`, no
Pydantic; closed catalog; specs-first PRs; cross-provider conformance
tests) maps cleanly onto PI1's audit ask.

---

## 6. Privacy (P1–P8)

The Privacy category is **opt-in** in SOC2 — only relevant if the
deploying organization explicitly asserts privacy controls. Buyers
who do **not** assert P1–P8 can skip this section; the row stays as
"buyer-elected scope."

| Criterion | Status | Evidence | Buyer additions |
|-----------|--------|----------|-----------------|
| P1 Notice | buyer-responsibility | — | Customer-facing privacy notice naming Metis as a sub-processor; sub-processor list. |
| P2 Choice and consent | partial | Sensitivity floor on `turn.started.user_message_text_redacted` (opt-in field per [`event-bus-and-trace-catalog.md §4.4.1`](../specs/event-bus-and-trace-catalog.md)); CLI confirmation handler with `trust.yaml` `always_allow` / `always_deny` for write / execute / network side effects. | Consent collection at the buyer's product layer. |
| P3 Collection | implemented | Identity model collects only what's needed for cost attribution: `user_id` / `team_id` are operator-assigned tags, no plaintext PII in the keystore ([`multi-user.md §2.1.6 / §3.3`](../specs/multi-user.md)). Plaintext email lives only in `users.json` (separate file from trace events); the trace store carries the stable `user_id` only. `email_sha256` for future SSO-bridge correlation never crosses into the trace events either. Wave 12 `EventRedactor.pseudonymize` mode ([`redaction.md §3.1`](../specs/redaction.md)) hashes identity fields at export so even pseudonymous ids don't cross the export boundary verbatim. | Buyer-side opt-in collection workflows for any additional PII layered on top. |
| P4 Use, retention, disposal | implemented (post-Wave 12) | `metis trace prune` ([`trace-retention.md §7.1`](../specs/trace-retention.md), 90-day default, audit-exempt); `metis user forget` ([`redaction.md §5`](../specs/redaction.md), GDPR Article 17 pseudonymization-as-erasure); `metis audit export --redact` ([`redaction.md §2`](../specs/redaction.md), four modes). | Buyer ratifies `retention_days`; legal counsel signs off on pseudonymization-as-erasure vs hard-delete. |
| P5 Access | partial | `/analytics/by_user`, `/analytics/by_team` rollups ([`analytics-api.md §4`](../specs/analytics-api.md)) give a user / team their own cost / token / call rollup. No "show me every prompt I sent" surface in v1. | Self-service data-subject-access workflow on the buyer's surface. |
| P6 Disclosure to third parties | partial | Upstream LLM providers (Anthropic / OpenAI / OpenRouter) receive prompts as part of `llm.call_*`. Provider names are documented (gateway / server / CLI logs); content is what the user sends. | DPA / sub-processor agreement with each enabled upstream provider; sub-processor disclosure list. |
| P7 Quality | buyer-responsibility | — | Process for correcting / refreshing personal data the buyer holds. |
| P8 Monitoring and enforcement | partial | Audit events for identity / quota actions ([`gateway.md §11.5`](../specs/gateway.md), [`multi-user.md §7.2`](../specs/multi-user.md)). | Privacy-incident-response playbook (extension of [`incident-response.md`](incident-response.md)). |

**GDPR right-to-delete (P4) — closed by Wave 12.**
[`multi-user.md §7.4 item 4`](../specs/multi-user.md) had named the
three surfaces a right-to-delete touches (`users.json`, all keys for
the user, trace store under the append-only invariant) and explicitly
did not commit to a pathway in v1. [`redaction.md §5`](../specs/redaction.md)
closes the gap with `metis user forget <user_id> --confirm`: a CLI
wrapper that runs `UPDATE events SET payload_json = ... WHERE
json_extract(payload_json, '$.user_id') = ?` inside a single SQL
transaction, replacing every matching `user_id` with the deterministic
`pseudonym_for(user_id)` hash. This is **the one documented exception
to the append-only invariant** ([`§5`](../specs/redaction.md)) — it
touches only the `user_id` JSON field on rows that carry it; costs,
tokens, hashes, and `team_id` are unchanged so per-team aggregates
survive while the bridge back to the natural person is severed. An
`analytics.user_forgotten` audit event records every call (including
idempotent re-runs with `pseudonymized_rows = 0`) for buyer-side
chain-of-custody.

The four-leg P4 stack now reads:

1. **Pseudonymization at collection** — `user_id` is operator-assigned
   and never plaintext-PII; plaintext email is segregated to
   `users.json` ([`multi-user.md §3.3`](../specs/multi-user.md)).
2. **Pseudonymization at export** — `metis audit export --redact
   pseudonymize` hashes identity fields before they leave the box
   ([`redaction.md §3.1`](../specs/redaction.md)).
3. **Retention expiry** — events past `retention_days` (default 90)
   are deleted on the batch sweep, audit events exempt
   ([`trace-retention.md §3.1 / §5`](../specs/trace-retention.md)).
4. **Right-to-be-forgotten** — `metis user forget <user_id>`
   pseudonymizes in place, audit-event recorded
   ([`redaction.md §5`](../specs/redaction.md)).

A full GDPR Article 17 ("right to erasure" with full row deletion vs
pseudonymization) is a buyer-policy decision — the spec ships
pseudonymization-as-erasure because the alternative (row deletion)
destroys the cost-attribution audit trail. Buyers whose legal counsel
requires hard deletion run `metis audit export --user-id <id> --redact
passthrough` to satisfy Article 15, then `DELETE FROM events WHERE
json_extract(payload_json, '$.user_id') = ?` as a buyer-side
extension.

---

## 7. Honest gaps (named explicitly)

These are the items the buyer should expect a SOC2 auditor to flag.
None of them are surprises — they're scoped for the buyer's
remediation plan, not hidden.

1. **No formal change management.** Metis ships under
   specs-first-with-PR-tests, not a CAB. CC8 is buyer-owned in the
   deployment.
2. **No third-party penetration test.** No external pentest has been
   commissioned against the gateway. The shipped surface is small
   (loopback + bearer auth + workspace-scoped file API), but "small"
   is not the same as "tested." Recommended buyer action: a
   black-box pentest against the deployed gateway, scoped to the
   buyer's exposure (post-TLS-terminator surface).
3. **No formal vendor security review** of upstream LLM providers.
   Each buyer signs their own DPA / questionnaire with Anthropic /
   OpenAI / OpenRouter; Metis does not aggregate those.
4. **No SOC2 auditor engagement on the Metis project itself.** The
   cert path for *the open-core software* is post-GA. The buyer's
   *deployment* can be SOC2-audited inside the buyer's existing
   audited cloud account.
5. **No automated CVE scanning** of the Docker image. Buyer wraps
   the image with their own SCA / image-scanning step (Trivy, Snyk,
   Anchore).
6. **No tamper-evident audit log.** The trace store is append-only at
   the SQL layer (WAL) but not cryptographically signed —
   [`multi-user.md §7.4 item 2`](../specs/multi-user.md) names this as
   a follow-on. Hash-chained event ids are a candidate; treating
   operator-controlled access as sufficient is the v1 posture.
7. **No SSO / SAML / OIDC integration.** Deliberately deferred in
   [`multi-user.md §8.1`](../specs/multi-user.md); v1 ships manually
   issued user records in `users.json`. Buyers using SSO for
   everything else wrap the gateway with an OAuth2 proxy until the
   IdP-bridge spec lands.
8. **No RBAC inside the deployment.** Whoever can run `metis gateway
   issue-key` can also `revoke-key`. Split roles via OS-level
   permissions on the keystore file or a buyer-side IAM wrapper.
9. **No automatic background-check policy.** Org-side; cannot be
   shipped by software.
10. **No `metis serve` (agent path) quota enforcement.** Agent-path
    traffic is loopback-only and operator-trusted; budget enforcement
    only applies to the gateway path ([`multi-user.md §8 item 7`](../specs/multi-user.md)).

---

## 8. Buyer-conversation talking points

When the buyer's procurement / compliance team asks about SOC2,
respond at three levels:

**Level 1 — "Is Metis SOC2-certified?"**
No. Metis is open-core software. The deployed gateway runs inside the
buyer's SOC2-audited cloud account (AWS / GCP / Azure all carry SOC2
Type 2). This document is the gap audit for the application-layer
controls Metis contributes; the buyer's cloud baseline covers the
infrastructure layer. **Frame it as a layered shared-responsibility
model**, not a yes/no.

**Level 2 — "Show me the controls you do ship."**
Walk through CC6 (access — multi-user identity + key rotation + audit
events), CC7 (operations — incident runbooks + backup/restore + rolling
upgrade), C1 (confidentiality — sensitivity classification +
loopback-only + Wave 12 redaction), PI1 (processing integrity — the
strongest TSC, backed by canonical-format + cost-attribution + 1678
tests). Point at the evidence column.

**Level 3 — "When can you commit to a Type 1 / Type 2?"**

- **Type 1** (control design as of a point in time) is achievable in
  Q3 2026 *if a buyer commits to underwriting the audit fee* (typical
  range: $15k–$40k from a CPA firm like Prescient Assurance, Sensiba,
  or a Big-Four). Three months of evidence-gathering with a tool like
  Vanta / Drata / Secureframe (~$8k/yr) plus a Type 1 readiness
  assessment.
- **Type 2** (operating effectiveness over 6–12 months) is feasible
  Q4 2026 / Q1 2027 *if the same buyer commits to the longer
  evidence window* — Type 2 requires actual operating logs across
  the audit period, not just designed controls.

The honest answer is: **Type 1 readiness in Q3, Type 2 in Q4 if a
buyer pre-pays the audit cost.** Metis will not pay for the audit
speculatively. If a deal is contingent on the cert, the deal funds
the cert.

**Anti-pattern to avoid.** Promising a SOC2 cert as a feature delivery
is the same trap as promising "GDPR compliance" — both are programs of
ongoing control operation, not engineering deliverables. The buyer's
compliance team will respect "here's our gap audit and our cert
timeline" more than "yes, we'll be SOC2-compliant by [date]."

---

## 9. See also

- [`compliance-overview.md`](compliance-overview.md) — one-page index
  to every compliance doc; quick-reference table for "buyer asks X →
  read Y."
- [`incident-response.md`](incident-response.md),
  [`sla-template.md`](sla-template.md),
  [`status-page.md`](status-page.md),
  [`upgrade-guide.md`](upgrade-guide.md) — Wave 11 operations docs,
  evidence for CC7.
- [`../specs/multi-user.md §7`](../specs/multi-user.md) — identity-
  relevant audit-event catalog, SOC2-relevant questions surfaced for
  the owner.
- [`../specs/gateway.md §11`](../specs/gateway.md) — key lifecycle CLI
  + audit events (`gateway.key_issued` / `key_revoked` / `key_rotated`).
- [`../specs/event-bus-and-trace-catalog.md §4.4`](../specs/event-bus-and-trace-catalog.md)
  — sensitivity classification taxonomy (`private` /
  `user_controlled` / `pseudonymous` / `aggregatable`).
- [`../specs/audit-log.md`](../specs/audit-log.md) — Wave 12 (12a-1)
  audit-event taxonomy + `metis audit export` JSONL / CSV shape.
- [`../specs/trace-retention.md`](../specs/trace-retention.md) — Wave
  12 (12a-2) retention sweep (`metis trace prune`, 90-day default,
  audit-exempt).
- [`../specs/redaction.md`](../specs/redaction.md) — Wave 12 (12a-3)
  `EventRedactor` modes + `metis user forget` GDPR Article 17 path.
