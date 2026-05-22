# Compliance overview

One-page index for buyer compliance / procurement / legal teams.
Metis ships open-core software, not a managed service — what this
document maps is the *application-layer* control evidence Metis
contributes to a buyer-operated deployment. The buyer's cloud-
provider baseline (AWS / GCP / Azure SOC2 Type 2) covers the
infrastructure layer; the buyer's internal policy set covers the
organizational layer.

> **Not a certification.** Metis itself does not hold a SOC2, ISO
> 27001, or HIPAA attestation. See [`soc2-readiness.md`](soc2-readiness.md)
> §8 for the realistic cert-path timeline (Type 1 readiness in Q3 2026
> contingent on a buyer underwriting the audit cost).

---

## What's covered, what's not

| Framework | Status | Document | Buyer responsibility |
|-----------|--------|----------|----------------------|
| **SOC2** Trust Service Criteria (Security, Availability, Confidentiality, Processing Integrity, Privacy) | Gap audit shipped; no cert | [`soc2-readiness.md`](soc2-readiness.md) | Hosting in a SOC2-attested cloud account; CC8 change management; CC1 organizational controls; CC9 vendor management of upstream LLM providers. |
| **GDPR** (data protection, right-to-delete) | Article 15 (access) + Article 17 (erasure) shipped via Wave 12 `metis audit export` + `metis user forget`; pseudonymization-as-erasure rather than hard-delete | [`soc2-readiness.md` §6](soc2-readiness.md), [`../specs/redaction.md §5`](../specs/redaction.md), [`../specs/trace-retention.md`](../specs/trace-retention.md), [`../specs/multi-user.md §7.4`](../specs/multi-user.md) | Customer-facing privacy notice; sub-processor disclosure list; DPAs with each enabled upstream LLM provider; legal counsel signs off on pseudonymization-as-erasure (hard-delete is a buyer-side extension). |
| **HIPAA** (PHI handling, BAA) | **Out of scope for v1** | — | Metis is not a HIPAA-eligible posture in v1: upstream LLM providers vary in BAA availability; the workspace-scoped file API is not designed around PHI segregation. Buyers handling PHI should treat Metis as not-yet-ready and request a roadmap commitment before piloting. |
| **ISO 27001** | Out of scope for v1; substantial overlap with SOC2 controls | — | Buyers needing ISO 27001 attestation should run the gateway in an ISO-27001-attested account; the SOC2 gap audit is broadly translatable. |
| **PCI-DSS** | Out of scope (Metis does not process payment data) | — | If the buyer's workflow puts PCI data inside prompts, that data inherits the buyer's PCI scope — not recommended; no Metis-side primitives for PCI segregation. |
| **FedRAMP** | Out of scope for v1 | — | Buyers in regulated US-gov contexts should not deploy Metis in v1. |

The honest framing: **SOC2 + GDPR is the v1 compliance surface.**
Everything else is either out of scope (HIPAA, FedRAMP, PCI) or
inherited from the SOC2 work (ISO 27001).

---

## "Buyer asks X → read Y" quick reference

| Buyer question | Primary doc | Supporting docs |
|----------------|-------------|------------------|
| "Are you SOC2-certified?" | [`soc2-readiness.md` §8](soc2-readiness.md) — talking points (no, not yet; Type 1 Q3 2026 if you underwrite) | [`compliance-overview.md`](compliance-overview.md) (this file) |
| "Show me your SOC2 gap audit" | [`soc2-readiness.md`](soc2-readiness.md) — full TSC mapping | — |
| "What's your incident response process?" | [`incident-response.md`](incident-response.md) — SEV1–SEV4, first-hour playbook, post-mortem template | [`status-page.md`](status-page.md), [`sla-template.md`](sla-template.md) |
| "What's your SLA?" | [`sla-template.md`](sla-template.md) — 99.5% single-region template; service-credit math | [`incident-response.md`](incident-response.md), [`status-page.md`](status-page.md) |
| "How do you handle a key compromise?" | [`incident-response.md` "Gateway-key compromise"](incident-response.md) | [`../specs/gateway.md §11`](../specs/gateway.md) — key lifecycle CLI |
| "How do you handle PII / customer prompts?" | [`soc2-readiness.md` §4 / §6](soc2-readiness.md) — confidentiality + privacy | [`../specs/multi-user.md §3.3`](../specs/multi-user.md), [`../specs/redaction.md`](../specs/redaction.md), [`../specs/event-bus-and-trace-catalog.md §4.4`](../specs/event-bus-and-trace-catalog.md) |
| "Can a user delete their data?" | [`soc2-readiness.md` §6 P4](soc2-readiness.md) — `metis user forget <user_id> --confirm` (pseudonymization-as-erasure) | [`../specs/redaction.md §5`](../specs/redaction.md), [`../specs/multi-user.md §7.4`](../specs/multi-user.md), [`../specs/trace-retention.md`](../specs/trace-retention.md) |
| "How long do you keep logs?" | [`soc2-readiness.md` §2 CC7](soc2-readiness.md) — 90-day default, buyer-tunable via helm `traceRetention.days`; audit events exempt | [`../specs/trace-retention.md §2.1 / §5`](../specs/trace-retention.md), [`../specs/event-bus-and-trace-catalog.md §7.3`](../specs/event-bus-and-trace-catalog.md) |
| "Where's the audit log?" | [`soc2-readiness.md` §2 CC6](soc2-readiness.md) — `metis audit export` (JSONL / CSV; 9-event v1 subset) | [`../specs/audit-log.md`](../specs/audit-log.md), [`../specs/gateway.md §11.5`](../specs/gateway.md), [`../specs/multi-user.md §7.2`](../specs/multi-user.md) |
| "How do you encrypt data?" | [`soc2-readiness.md` §2 CC6.7](soc2-readiness.md) — in-transit = TLS terminator (buyer); at-rest = volume encryption (cloud) | [`../gateway-deployment.md` "TLS termination"](../gateway-deployment.md) |
| "Who has access to production?" | [`soc2-readiness.md` §2 CC6.1 / CC6.3](soc2-readiness.md) — gateway-key bearer auth + workspace-scoped tool API | [`../specs/gateway.md §3.3`](../specs/gateway.md), [`../specs/multi-user.md §3`](../specs/multi-user.md) |
| "What's your DR / backup story?" | [`soc2-readiness.md` §2 CC7.4](soc2-readiness.md) — `metis backup` / restore | [`../gateway-deployment.md` "Backup & restore"](../gateway-deployment.md), [`../specs/event-bus-and-trace-catalog.md §7.5`](../specs/event-bus-and-trace-catalog.md) |
| "How do you handle upgrades?" | [`upgrade-guide.md`](upgrade-guide.md) — rolling upgrade, schema migration, rollback | [`../specs/api-versioning.md`](../specs/api-versioning.md) |
| "Have you done a pentest?" | [`soc2-readiness.md` §7 item 2](soc2-readiness.md) — no; buyer-recommended | — |
| "What about GDPR right-to-delete?" | [`soc2-readiness.md` §6 P4](soc2-readiness.md) — `metis user forget` (pseudonymization-as-erasure) + Article 15 via `metis audit export --user-id <id> --redact passthrough` | [`../specs/redaction.md §5`](../specs/redaction.md), [`../specs/multi-user.md §7.4 item 4`](../specs/multi-user.md) |
| "Which upstream providers do you call?" | [`soc2-readiness.md` §6 P6](soc2-readiness.md) — Anthropic / OpenAI / OpenRouter | [`AGENTS.md`](../../AGENTS.md) provider-adapter list, [`../specs/provider-adapter-contract.md`](../specs/provider-adapter-contract.md) |
| "What permissions does Metis need on our workspace?" | [`../specs/tool-dispatcher.md §5.1`](../specs/tool-dispatcher.md) — workspace-scoped file API, `..` / out-of-root symlinks rejected | [`AGENTS.md "Workspace path security"`](../../AGENTS.md) |

---

## Layered shared-responsibility model

Metis sits in the middle of three layers. A buyer's SOC2 / GDPR /
compliance story composes them together:

```
┌─ Buyer organization layer ──────────────────────┐
│  Policies, HR, change management (CC1, CC8),    │
│  vendor management (CC9), customer DPAs,        │
│  privacy notice, pentest, SOC2 audit fee        │
└─────────────┬───────────────────────────────────┘
              │
┌─ Metis application layer ───────────────────────┐
│  Identity / keys (CC6), incident runbooks +     │
│  backup/restore (CC7), redaction + retention    │
│  + sensitivity classification (C1, P3),         │
│  canonical message format + cost-attribution    │
│  math (PI1)                                     │
└─────────────┬───────────────────────────────────┘
              │
┌─ Cloud-provider baseline layer ─────────────────┐
│  Physical security, network controls, hardware  │
│  encryption, hypervisor isolation, regional     │
│  redundancy (CC6.4, A1.2)                       │
│  AWS / GCP / Azure each carry SOC2 Type 2       │
└─────────────────────────────────────────────────┘
```

The buyer assembles the story by stacking attestations: their cloud
provider's SOC2 Type 2 report + the [`soc2-readiness.md`](soc2-readiness.md)
gap audit for the Metis application layer + the buyer's internal
control documentation for the organizational layer.

---

## Compliance posture by deployment shape

The engineering shape is unified
([`deployment-shape.md §6`](../specs/deployment-shape.md)). The
compliance story does differ by deployment posture — useful when the
buyer's compliance team asks which shape they're signing off on.

| Shape | Where it runs | Compliance posture |
|-------|---------------|---------------------|
| **Local-first** (dev laptop, `metis dev`) | Operator's workstation | Loopback-only; never crosses a network boundary; no SOC2 question because no shared infrastructure. Suitable for solo / pre-pilot evaluation. |
| **In-VPC** (buyer-operated helm chart) | Buyer's cloud account (AWS / GCP / Azure) | Buyer's cloud-provider SOC2 Type 2 covers infrastructure; [`soc2-readiness.md`](soc2-readiness.md) covers the application layer. **This is the v1 reference posture.** |
| **SaaS** (Metis-hosted, not v1) | Metis-operated cloud | Metis would need its own SOC2 attestation for the hosted product. **Not in scope for v1.** |

If the buyer is debating in-VPC vs SaaS, point at the table: in-VPC
inherits their existing cloud baseline; SaaS would require Metis's
own audit, which is not on the v1 roadmap.

---

## How this doc set evolves

Compliance documents drift faster than specs because they reflect
*operating* state, not *designed* state. Refresh cadence:

- **`soc2-readiness.md`** — re-read after every wave that touches CC6
  (identity, keys, access), CC7 (operations), C1 (confidentiality),
  or P-categories (privacy). Update the Wave-12-delta callouts as the
  in-flight specs land.
- **`compliance-overview.md`** (this file) — re-read when a new
  framework enters scope or a new compliance doc lands under
  `docs/operations/`.
- **`incident-response.md` / `sla-template.md` / `status-page.md` /
  `upgrade-guide.md`** — refresh on operator-facing surface changes
  (new event types, new CLI subcommands, new endpoints).

The CHANGES.md log ([`../specs/CHANGES.md`](../specs/CHANGES.md)) is
the trigger: any entry that touches access, operations, retention,
audit, or privacy should fan out into a review of this doc set.
