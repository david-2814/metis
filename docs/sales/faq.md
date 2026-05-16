# Buyer FAQ

> Questions a buyer asks before signing. Honest answers; specific
> numbers; pointers to the load-bearing docs. Update when a new
> question hits us in the wild.

For longer treatments of competitive questions, see
[`competitive-comparison.md`](competitive-comparison.md). For objections
worded as pushback rather than questions, see
[`objection-handling.md`](objection-handling.md).

---

## What problem does Metis solve?

Three problems that compound:

1. **LLM cost is hard to attribute and hard to lower.** Most teams see
   one Anthropic line item and one OpenAI line item per month. Per-dev,
   per-team, per-project breakdown takes work. Picking the cheapest
   model that succeeds per task takes more work.
2. **Routing products lose information.** LiteLLM has known bugs on
   `cache_control`, thinking blocks, and tool-use round-trip. OpenAI-
   shape internal IRs can't represent Anthropic-native blocks losslessly.
3. **Compliance work is shifting left.** SOC2 / GDPR / audit-log
   asks are arriving with the procurement form, not after the pilot.

Metis is a transparent HTTP gateway that addresses all three: lossless
canonical IR, learned routing over task fingerprints, per-user / per-team
attribution, and the audit-log + retention + redaction + GDPR-forget
triad shipped in v1.

---

## How does it work?

Devs flip one environment variable (`ANTHROPIC_BASE_URL` for Claude
Code, `OPENAI_BASE_URL` for Cursor / openai-python, etc.) to point at
the Metis gateway. The gateway:

1. Authenticates via a gateway-issued `gw_…` token.
2. Maps the token to a key with `user_id` / `team_id` / `workspace_path`
   tags.
3. Routes via the 7-slot chain (manual override / sticky / YAML rules /
   learned pattern / delegate / workspace default / global default).
4. Calls the upstream provider with the chosen model.
5. Stamps every `route.decided` / `llm.call_started` / `llm.call_completed`
   / `turn.completed` event with `gateway_key_id`, `user_id`, `team_id`,
   `inbound_shape`, `cost_usd`, token counts, cache fields.

The trace DB is on-disk SQLite; analytics endpoints (`/analytics/cost`,
`/analytics/by_user`, `/analytics/by_team`, `/analytics/savings`,
`/analytics/quality`) read from it.

Full spec: [`docs/specs/gateway.md`](../specs/gateway.md).

---

## How much does it cost?

Pricing is open per [`docs/STRATEGY.md §6.8`](../STRATEGY.md);
specced at [`docs/specs/pricing.md`](../specs/pricing.md) — open-core
gateway + per-seat Pro tier + reserved enterprise %-of-savings add-on.
The owner has not ratified the model yet.

What's true today:
- The gateway is open-source and BYO-infra.
- Source is the deliverable.
- Buyer trials don't have a price tag — talk to us.

---

## How does Metis compare to LiteLLM?

Short version: LiteLLM has 10× our stars and we are happy to lose deals
to it on shape (more provider adapters, more mature dashboards). We win
on canonical-IR fidelity (LiteLLM has 6+ open bugs on Anthropic-native
features as of 2026-05-09), learned routing (LiteLLM has none), and
per-user / per-team attribution (LiteLLM rolls up per key only).

Full comparison: [`competitive-comparison.md`](competitive-comparison.md).

---

## How does Metis compare to Portkey?

Portkey has the more polished SaaS dashboard and a more expressive
conditional-routing rule language. Metis has canonical-IR fidelity,
learned routing over outcome history (not rules), per-user attribution,
and a BYO-infra-only path with no SaaS dependency.

If you want polished SaaS dashboards *today*, Portkey is the right pick.
If you want learned routing and canonical-IR fidelity *today*, Metis is
the right pick.

---

## How does Metis compare to Helicone?

Helicone is observability-first with a light routing surface. Metis is
routing-first with the observability surface as a deliverable
(`/analytics/*`, `/metrics`, audit log) but without the SaaS dashboard
polish Helicone has. If your need is "see what's going on with our LLM
traffic," Helicone is shipped. If your need is "pick the cheaper model
per task," Metis is shipped.

---

## Will my devs have to change their tools?

No. The gateway is shaped exactly like Anthropic's `/v1/messages` and
OpenAI's `/v1/chat/completions` (sync + SSE). Claude Code, Cursor,
anthropic-python, openai-python, and any raw-SDK client work with one
env var flipped. End-to-end recipe at
[`docs/gateway-client-quickstart.md`](../gateway-client-quickstart.md).

---

## What providers does Metis support?

Three adapters today:
- **Anthropic** (Opus 4.7, Sonnet 4.6, Haiku 4.5)
- **OpenAI** (GPT-5, GPT-5-mini)
- **OpenRouter** (catalog fetched at startup; brings the long tail at
  one extra hop)

Adding a provider is writing an adapter against the canonical IR
([`provider-adapter-contract.md`](../specs/provider-adapter-contract.md))
+ registering per-model `AdapterCapabilities` for the routing
validation gate. ~1–2 engineer-weeks per provider.

---

## How do I evaluate the savings on my own workload?

Three paths, depending on time budget:

| Time | Path |
|---|---|
| 1 hour | [`operations/quickstart.md`](../operations/quickstart.md) — kind + helm + pre-baked workload through `metis trial` |
| 1 day | [`customer-trial-recipe.md`](../customer-trial-recipe.md) Path A — devs use existing tools through the gateway for a week; read `/analytics/by_team` |
| 1 week | [`customer-trial-recipe.md`](../customer-trial-recipe.md) Path B — pick 5–10 of your prompts, write a rubric, compare cost-per-quality |

Path B is what produced the §A3-rev3 demo. It's the rigorous path; Path
A is the "is this saving money" sniff test.

---

## What's the savings number on your benchmark?

§A3-rev3 (the canonical demo) on a 6-workload suite:
- haiku pinned: $0.0383 per quality unit.
- sonnet pinned: $0.1176 per quality unit.
- slot 4 routing: **$0.0477 per quality unit** (8% more quality than
  haiku-only at 40% of sonnet-only cost).

Delegation (sonnet planner + haiku workers on a fan-out workload):
**8.3% – 26.1% better cost-per-quality** across two reproducible runs
(§A3-rev5 and §A3-rev6).

Prompt caching: 100% cache-fire rate on the 49-call benchmark suite,
22.8% same-workload cost reduction (`§Run 3`).

**Caveats:** all numbers from [`benchmarks/RESULTS.md`](../../benchmarks/RESULTS.md).
The routing inversion is N=1 in v1; magnitude on your workload depends
on what fraction of turns are "hard." Three workload shapes where Metis
won't move the needle on routing are listed in
[`customer-trial-recipe.md §6`](../customer-trial-recipe.md).

---

## What's the SOC2 / compliance story?

Full audit: [`docs/operations/soc2-readiness.md`](../operations/soc2-readiness.md).
Trust Service Criteria CC1–CC9, A1, C1, PI1, P1–P8 mapped against
shipped + buyer-responsibility evidence.

Shipped in v1:
- Audit log (12-event subset, JSONL/CSV deterministic export).
- Trace retention (90-day default sweep, audit-event exemption,
  `metis trace prune`, helm CronJob template).
- Redaction layer (4-mode: passthrough / pseudonymize / redact-private
  / aggregate-only).
- GDPR Article 17 (`metis user forget` — pseudonymization-as-erasure).
- GDPR Article 15 (`metis analytics user-export` — portability JSONL).

Gaps named honestly:
- CC8 change management — not a formal process yet.
- Third-party pentest — not done.
- Vendor review — not done.
- SOC2 auditor engagement — not engaged.

Type 1 readiness target Q3 2026 contingent on buyer underwriting the
audit fee. Type 2 Q4 2026 / Q1 2027.

---

## What about GDPR?

- **Article 15 (portability):** `metis analytics user-export <user_id>`
  streams every trace event stamped with `user_id` as JSONL. HTTP twin
  at `GET /analytics/user/{user_id}/export`.
- **Article 17 (erasure):** `metis user forget <user_id>` pseudonymizes
  identity fields in place across the trace DB. Aggregate analytics
  survives; the link to the natural person doesn't. HTTP twin at
  `POST /analytics/user/{user_id}/forget`.

Both audit-flagged: `analytics.user_exported` / `analytics.user_forgotten`.
`metis user forget` without `--confirm` runs a dry-run preflight printing
"this would pseudonymize N event(s)" so operators can validate scope.

---

## Where does my data go?

- **Trace DB:** on-disk SQLite in the workspace you point Metis at
  (default `~/.metis/metis.db`).
- **API keys:** the gateway holds upstream provider keys. Gateway-issued
  tokens (`gw_…`) are SHA-256-hashed in the keystore; the plaintext
  token is printed once at issuance.
- **Telemetry:** none. No phone-home, no usage reporting to us.
- **Embeddings (pattern store v2):** computed at the embedding provider
  you configure (`openai:text-embedding-3-small`, `cohere:embed-multilingual-v3.0`,
  or `local:sentence-transformers:all-MiniLM-L6-v2`). v1 fingerprint is
  structural-only and never leaves your machine.

---

## What's the deployment shape?

Three supported postures:

| Posture | What it looks like |
|---|---|
| **Docker compose** | Single-host; `docker compose up -d`; loopback by default |
| **In-cluster (helm)** | Single chart at [`infra/gateway/helm/`](../../infra/gateway/helm/); ServiceMonitor template; loopback by default, opt-in to `--host 0.0.0.0` + in-process TLS or upstream Caddy / nginx-ingress / cloud LB |
| **SaaS** | Not shipped. Buyer-conversation evidence will decide priority — see [`STRATEGY.md §6.3`](../STRATEGY.md) |

Helm values gain `gatewayHost`, `maxConnections`, `workers`,
`reusePort`, `tls.{enabled, secretName, mountPath}` plus three
documented deployment recipes (Ingress / LoadBalancer / LoadBalancer +
in-process TLS). Migration recipe at
[`docs/operations/upgrade-guide.md §6`](../operations/upgrade-guide.md).

---

## What's the operational load?

Single Python process, sub-millisecond fast-path on the trace store
(WAL + `synchronous=NORMAL`). Reference throughput: ~4,800 events/sec
on Apple M-series / Python 3.13 / SQLite 3.50.4 (see
[`docs/operations/trace-performance.md`](../operations/trace-performance.md)).

Operational doc set ships in v1:
- [`incident-response.md`](../operations/incident-response.md) — SEV1–SEV4
  criteria, first-hour playbook, per-failure-mode playbooks (upstream
  LLM outage, trace-DB corruption, key compromise, quota runaway).
- [`sla-template.md`](../operations/sla-template.md) — 99.5% single-region
  template; service-credit math; force-majeure stub deferred to legal.
- [`status-page.md`](../operations/status-page.md) — two-tier recipe
  (external UptimeRobot / Statuspage.io / Better Stack against
  `/healthz`, plus self-hosted Uptime Kuma in-cluster).
- [`upgrade-guide.md`](../operations/upgrade-guide.md) — rolling-upgrade
  procedure; backup-before-upgrade via `metis backup`; rollback.

Realistic ongoing load: one engineer-day to deploy, ~zero ongoing.

---

## How do you handle rate limits, key rotation, and quotas?

- **Rate limits:** per-key + per-IP token-bucket middleware
  ([`gateway-hardening.md §3`](../specs/gateway-hardening.md)).
  Off by default; opt-in via `RateLimitConfig(enabled=True)`. 429 with
  `Retry-After` on exhaustion.
- **Key rotation:** `metis gateway rotate-key <key_id>
  [--grace-period 24h]`. Both old and new keys valid during grace;
  predecessor auto-revokes at the boundary. Audit-flagged.
- **Key revocation:** `metis gateway revoke-key <key_id>`. Audit-flagged.
- **Quotas:** budget enforcement is on the in-progress list. The
  evaluator's `BudgetTracker` primitive (per-session $0.10 / per-day
  $1.00 defaults) is in place for LLM-judge calls; per-team quotas at
  the gateway are spec-drafted but not yet shipped.

---

## What's the licensing posture?

TBD per [`README.md`](../../README.md). The strategic intent is
**open-core gateway + per-seat Pro tier + reserved enterprise %-of-
savings add-on** per [`docs/specs/pricing.md`](../specs/pricing.md). Open
question — see [`STRATEGY.md §6.8`](../STRATEGY.md).

---

## Can I see the trace events?

Yes. Trace DB is plain SQLite. Example queries:

```bash
# Cost per user, last 7 days
sqlite3 ~/.metis/metis.db "
  SELECT json_extract(payload_json, '$.user_id') AS user_id,
         ROUND(SUM(json_extract(payload_json, '$.cost_usd')), 4) AS cost_usd
  FROM events WHERE type = 'llm.call_completed'
    AND timestamp_us > strftime('%s', 'now', '-7 days') * 1000000
  GROUP BY user_id ORDER BY cost_usd DESC;"

# Routing chain for a specific turn
sqlite3 ~/.metis/metis.db "
  SELECT json_extract(payload_json, '$.chosen_model') AS model,
         payload_json
  FROM events WHERE type = 'route.decided'
    AND json_extract(payload_json, '$.turn_id') = 'turn_...'
  LIMIT 1;"
```

Or via the analytics endpoints — see
[`docs/specs/analytics-api.md`](../specs/analytics-api.md).

---

## Who runs this in production today?

GA pilot conversations are in progress. We don't have a customer logo
to show yet. The case-study template at
[`case-study-template.md`](case-study-template.md) is the slot the
first GA customer will fill.

---

## What's the roadmap?

| Phase | Status | Headline |
|---|---|---|
| 1 | shipped | Two providers, canonical format, event bus, file/shell tools, manual routing |
| 2 | shipped | Hand-written skills, bounded memory, analytics surface, evaluator heuristic tier |
| 2.5 | shipped | Pattern fingerprints, pattern-store v1 wired to routing slot 4 |
| 3 | in flight | Gateway, multi-user identity, evaluator hybrid+LLM tiers, compliance triad |
| 4 | drafted | Skill curator, delegation v2 (async workers, recursive delegation), tauri desktop |

Open strategic questions at [`STRATEGY.md §6`](../STRATEGY.md):
- Buyer profile (startup-CTO vs enterprise-eng-leader).
- Local-first vs SaaS deployment.
- Pricing model ratification.
- Context-assembler v3 skill activation.

---

## How can I get help?

- **Onboarding:** [`operations/quickstart.md`](../operations/quickstart.md)
  — 1-hour kind + helm + pre-baked workload path.
- **Trial recipe:** [`customer-trial-recipe.md`](../customer-trial-recipe.md).
- **Operational issues:** [`operations/incident-response.md`](../operations/incident-response.md).
- **Upgrades:** [`operations/upgrade-guide.md`](../operations/upgrade-guide.md).
- **Sales / pilot conversations:** the email on `README.md` once it's
  live, or open a GitHub issue.
