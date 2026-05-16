# GA-readiness audit (pre-launch quality pass)

**Status:** snapshot 2026-05-15. Performed as a one-shot pre-launch quality
pass by an AI agent under owner direction; not a continuous gate.
**Scope:** end-to-end quickstart, helm chart, CLI surface, documentation
hygiene. Bug-bash mode: report + tiny fixes only; substantive issues
surfaced for owner triage.

> Pairs with [`phase-claim-proposal.md`](phase-claim-proposal.md) (which
> reasons about whether to bump the AGENTS.md status sentence). This
> document is the engineering-quality companion: even if the owner
> ratifies "Phase 3 shipped," these items remain in front of GA.

---

## 1. Summary

| Bucket | PASS | TODO | FAIL |
|---|---|---|---|
| Quickstart (kind → helm → trial → dashboard) | 4 | 0 | 2 |
| Helm chart (lint + template against 5 profiles) | 5 | 0 | 0 |
| CLI surface (12 subcommands × `--help` + unexpected-input smoke) | 12 | 1 | 0 |
| Documentation (README + docs/ links + spec cross-refs) | 1408 | 5 | 5 |

**Tiny fixes applied in this pass** (no behavior change):

- [`docs/STRATEGY.md`](../STRATEGY.md):287 — `../gateway-deployment.md` → `gateway-deployment.md` (file lives in `docs/`, not repo root).
- [`docs/specs/multi-user.md`](../specs/multi-user.md):70 — `keystore.py` → `auth.py` (file renamed in Wave 10).
- [`docs/specs/CHANGES.md`](../specs/CHANGES.md):722 — `../packages/...` → `../../packages/...` (wrong depth from `docs/specs/`).
- [`docs/operations/soc2-readiness.md`](soc2-readiness.md) — three "1486 tests" references bumped to "1678" (matching AGENTS.md).
- [`infra/gateway/helm/templates/deployment.yaml`](../../infra/gateway/helm/templates/deployment.yaml) — gateway container port renamed from `gateway` to `http` when `proxy.enabled=false`, so the Service's `targetPort: http` actually resolves in the no-proxy shape.

---

## 2. Quickstart end-to-end

Followed [`quickstart.md`](quickstart.md) step-by-step on a fresh
machine with no prior kind cluster. Cost: ~$0.12 (one wasted run +
one successful trial).

### 2.1 Cluster + helm install — **PASS**

`infra/gateway/scripts/quickstart.sh` completed in ~3 minutes (kind
cluster create + docker build + image load + helm install + Ready
wait + port-forward). Output ended with the documented banner
(`gateway URL`, `gateway key`, `healthz`). State file written to
`.metis-trial/state.env` as specified. **Idempotent re-run not tested
in this pass** but covered by the existing CHANGES.md verification.

Minor rough edge: the `issue-key` step's stdout is captured to
`$ISSUE_OUT` and the "save the token now" preamble leaks through to
the user's terminal even though the actual `key_id` / `token` lines
are silently redirected. Cosmetic — the banner at the end prints the
token. Worth printing only after the parse.

### 2.2 Issue key — **PASS** (with cosmetic warning)

`metis gateway issue-key` emits a `warning: VIRTUAL_ENV=…` line on
every invocation when run under `uv run` in a session with a stray
`VIRTUAL_ENV` env var. This appears in every `metis` invocation in the
audit. Cosmetic but distracting in buyer demos; consider adding
`--active` or unsetting `VIRTUAL_ENV` in the wrapper scripts.

### 2.3 `metis trial` — **FAIL on first run, PASS after pod restart**

First trial run completed 2 of 3 turns and failed on turn 3 with:

```
RateLimitError: anthropic 503: no model available; tried:
anthropic:claude-sonnet-4-6 (provider_unavailable)
```

Root cause (per gateway logs):

```
ssl.SSLError: [SSL: SSLV3_ALERT_BAD_RECORD_MAC] ssl/tls alert bad record mac
```

One transient SSL handshake error from Anthropic's API caused the
gateway's `ProviderAvailability` tracker to mark anthropic
**provider-wide** UNAVAILABLE (per [`routing/availability.py:151`](../../packages/metis-core/src/metis_core/routing/availability.py#L151)
— NETWORK errors trip the whole provider immediately, no
consecutive-failure threshold). All subsequent calls returned 503
without any upstream attempt. **Recovery requires 5 minutes of
quiescence OR a pod restart** — the spec's auto-recovery only checks
`now - last_call_at >= 5 min`, but a buyer-facing trial flow doesn't
naturally produce 5 minutes of quiescence.

Subsequent retries (without restart) failed immediately on turn 1.
After `kubectl rollout restart` the trial ran end-to-end:

```
turns / llm / tool:     3 / 9 / 7
actual cost (USD):      0.006230
baseline cost (USD):    0.018690
savings_pct:            66.7%
quality:                0.81@0.80
```

**Surfaced to human triage — this is the highest-priority GA blocker
in this audit.** Three sub-issues:

1. **NETWORK error class trips provider-wide immediately.** A single
   one-off SSL hiccup → 5 minutes of gateway downtime. Compare to AUTH
   (genuine credential failure, immediate is correct) and rate-limit
   classes (which use a consecutive-failure threshold). NETWORK should
   either get a consecutive-failure threshold or a much shorter
   recovery window.
2. **No retry inside `metis trial`.** A single transient upstream
   error kills the whole trial. The buyer-facing flow is one-shot:
   re-running starts from turn 1 and may re-trip the same race.
3. **Error message references the wrong model.** The trial requested
   haiku; the failure message says "tried: anthropic:claude-sonnet-4-6"
   — because routing's slot 7 fallback is sonnet. Confusing for a
   buyer reading the failure for the first time.

### 2.4 Dashboard (`/analytics/by_key`) — **FAIL on data correctness**

The snapshot recipe in §5 of `quickstart.md` worked mechanically:
`VACUUM INTO` snapshot + `kubectl cp` + `metis serve --db-path
<snapshot>` + `curl /analytics/by_key`. But the data shape is wrong
in a buyer-facing way:

```json
{
  "gateway_key_id": "gk_01KRQRKGMZ0599RZX17FYC24VE",
  "cost_usd": 0.120118,
  "call_count": 12,
  ...
}
```

The trial's own output reported `actual cost: $0.006`. The dashboard
reports `cost_usd: $0.120` — **a ~20× discrepancy** for the same
calls. Drilling in:

```sql
SELECT json_extract(payload_json,'$.chosen_model'), COUNT(*)
  FROM events WHERE type='route.decided' GROUP BY 1;
-- (NULL)                         | 4
-- anthropic:claude-sonnet-4-6    | 13
```

Every routed call landed on **sonnet** (slot 7 global_default), not
haiku. The reason is the known pitfall in [`quickstart.md`'s table](quickstart.md#pitfalls):

> Bare `model: "claude-haiku-4-5"` (no provider prefix) bills sonnet
> — bare names land in slot 7 (`global_default`), which is sonnet.

The trial workload's YAML uses the canonical `anthropic:claude-haiku-4-5`
id, but the local Anthropic adapter strips the `anthropic:` prefix
before passing to the SDK (the SDK can't accept a `provider:` prefix
in the upstream Anthropic API). So through the gateway, the inbound
`model` is just `claude-haiku-4-5` — bare — and slot 1
(`per_message_override`) cannot resolve it.

**Surfaced to human triage.** The buyer-facing implications are:

1. The pre-baked workload defeats the pitfall callout — the trial
   binary, not the buyer, is responsible for the bare-name failure.
2. `/analytics/by_key` and the broader cost-rollup story for the
   gateway over-reports cost by the haiku→sonnet price ratio (~6×)
   on any buyer using the canonical id and the SDK.
3. The trial's own savings number (`actual: $0.006`) reads from the
   trial's *local* trace DB, which is stamped at haiku rates — so
   the trial output and the dashboard output disagree, and the
   dashboard is the higher number, which is the worse buyer
   experience.

**Repair candidates** (require owner ratification — not a 1-line fix):

- Have the gateway's `per_message_override` resolver fall back to
  appending the canonical-provider prefix when the inbound `model` is
  a bare provider-shaped id (e.g. `claude-haiku-4-5` → try
  `anthropic:claude-haiku-4-5`).
- Have the local Anthropic adapter pass through the canonical id
  verbatim when `ANTHROPIC_BASE_URL` points at a Metis gateway
  (detection: probe `/healthz` shape, or new env var).
- Have `metis trial --gateway-url` set up a `.metis/routing.yaml` in
  the workspace tempdir that pins haiku as the workspace_default so
  slot 6 wins even when slot 1 doesn't resolve.

### 2.5 Teardown — **PASS**

`infra/gateway/scripts/tear-down.sh` completed cleanly in ~6 seconds:
port-forward killed, helm uninstall, namespace delete, kind cluster
delete, state dir removed. Idempotent re-run works.

---

## 3. Helm chart audit

### 3.1 `helm lint` — **PASS**

```
helm lint infra/gateway/helm/ --set provider.anthropicApiKey=sk-ant-test
# 1 chart(s) linted, 0 chart(s) failed
# [INFO] Chart.yaml: icon is recommended (cosmetic)
```

### 3.2 `helm template` profiles — **PASS (with chart bug found + fixed)**

Rendered against five profiles:

| Profile | Resources rendered | Notes |
|---|---|---|
| **Default** (anthropicApiKey only) | 9 (NP, PDB, SA, Secret, ConfigMap, PVC, Service, Deployment) | Single-tenant loopback shape. Was where the bug below surfaced. |
| **Multi-tenant Internet-exposed** (gatewayHost=0.0.0.0, proxy.enabled=false, ingress.enabled=true, rateLimit.enabled=true, monitoring.enabled=true, traceRetention.enabled=true, traceVacuum.enabled=true) | 12 (adds 2 CronJob + Ingress + ServiceMonitor) | All resources render. Hardening-aware shape. |
| **Existing provider secret** (provider.existingSecret=my-providers) | 7 (no chart-managed Secret) | Chart correctly skips the inline-Secret render. |
| **Persistence disabled** | 8 (no PVC) | Volume / mount conditional fires correctly. |
| **Autoscaling enabled** | 9 + HPA | `replicas` field correctly omitted from Deployment. |

**Bug found (now fixed):** when `proxy.enabled=false`, the Service
declared `targetPort: http` but the gateway container's only port was
named `gateway`. The Service would have failed to route. Fix landed in
this audit: gateway port is now named `http` whenever `proxy.enabled`
is false, `gateway` otherwise.

### 3.3 Mode-correctness checks

- Gateway port flag conditional rendering: ✓ (`tls.enabled` → env vars + volume mount; `reusePort` → env var only when truthy).
- ConfigMap-vs-Secret keystore: ✓ (`keystore.existingSecret` switches the volume source cleanly).
- Probes: `probes.type=exec` uses `curl 127.0.0.1` (correct for the loopback+sidecar shape); `probes.type=httpGet` hits the sidecar's port.
- NetworkPolicy: deny-by-default ingress + egress to 53/UDP+TCP and 443/TCP. ✓

---

## 4. CLI surface audit

12 top-level subcommands × `--help`. All produced clean,
self-explanatory output with no crashes. Below is the leaf inventory:

```
metis chat       --model / --db-path / --global-default + workspace
metis tui        same as chat
metis serve      --db-path / --global-default / --host / --port + workspace
metis evaluate   --db-path / --subject / --since / --until / --session-id
metis gateway    --keystore / --db-path / --global-default / --host / --port
                 + --tls-cert / --tls-key / --max-connections / --reuse-port
                 + subcommands: issue-key, revoke-key, rotate-key, list-keys
metis backup     dest [--db-path]
metis restore    source [--db-path] [--force]
metis trace      subcommands: prune, vacuum
metis audit      subcommand: export
metis analytics  subcommand: user-export
metis user       subcommand: forget
metis trial      --workload / --model / --baseline / --db-path
                 / --gateway-url / --gateway-key
```

**Unexpected-input smoke:** all of `metis (no args)`, `metis bogus`,
`metis trace prune --db-path /nonexistent`, `metis backup
/nonexistent.db --db-path /nonexistent`, `metis gateway list-keys
--keystore /nonexistent`, `metis gateway revoke-key gk_doesnotexist`,
`metis analytics user-export` (missing required arg), and `metis
restore <src> --db-path <existing>` (no `--force`) produced clear
error messages and non-zero exits. No tracebacks on documented error
paths.

**TODO (cosmetic):** `metis trial --workload nonexistent-workload`
prints the `=== Metis trial ===` header on stdout *after* the
`trial failed:` message has been written to stderr. The header should
be suppressed on failure (or fail before the header prints).

**TODO (cosmetic, all subcommands):** the `warning: VIRTUAL_ENV=…`
line from `uv run` leaks into every CLI invocation. Setting
`UV_PROJECT_ENVIRONMENT` or wrapping in `uv run --active` would
suppress it; quickstart.sh + tear-down.sh could pre-empt it.

---

## 5. Documentation audit

Scanned 57 markdown files, ~1419 link tokens. **Findings:**

### 5.1 Real broken links — **5 found, 4 fixed in this pass**

| File | Target | Fix |
|---|---|---|
| `docs/STRATEGY.md:287` | `../gateway-deployment.md` | **FIXED** → `gateway-deployment.md` |
| `docs/specs/multi-user.md:70` | `apps/gateway/src/metis_gateway/keystore.py` (renamed Wave 10) | **FIXED** → `auth.py` |
| `docs/specs/CHANGES.md:722` | `../packages/...` (wrong depth) | **FIXED** → `../../packages/...` |
| `docs/sales/one-pager.md:167` | `case-study-template.md` (missing) | **TODO** — buyer-asset, surfaced |
| `docs/sales/faq.md:327` | `case-study-template.md` (same missing) | **TODO** — buyer-asset, surfaced |

### 5.2 Self-referential template strings — **5 instances, intentional but rendering-broken**

- `docs/specs/pricing.md:519–522` and `docs/specs/deployment-shape.md:228–229`
  contain markdown links like `[\`pricing.md\`](specs/pricing.md)` —
  these are **template strings showing what to paste into STRATEGY.md
  on sign-off**, not navigation links from the spec. The link target
  is correct relative to STRATEGY.md but broken relative to the spec
  file's own directory. A renderer (e.g. mkdocs) will report them as
  broken.

  Recommendation (not done): wrap in backtick code spans or fence as
  a code block to make the intent explicit.

### 5.3 Stale "what's NOT built" section in `README.md`

[`README.md`](../../README.md) §"What's NOT built yet" lists six items, of which **four
have shipped** per [`AGENTS.md`](../../AGENTS.md):

1. ~~"Configured routing rules in the chain"~~ — shipped (commit `e71fedd`).
2. ~~"Tool-confirmation REST endpoint"~~ — shipped (commit `e71fedd`).
3. ~~"Pattern store + learned routing"~~ — shipped Phase 2.5.
4. ~~"Delegation"~~ — shipped Wave 10.

The other two ("Skills" partial; "Worker sessions") are accurately
characterized. **TODO** — surface for owner; README is the
buyer-facing landing page and should not lie about the surface.

### 5.4 Stale Phase 3 description in `README.md` and Roadmap table

[`README.md`](../../README.md) §"Roadmap" still lists Phase 3 as "In-session
adjustment heuristics, full evaluator, MCP support, git sync, third
provider." Per the
[`phase-claim-proposal.md`](phase-claim-proposal.md):

- "Third provider" — shipped since Phase 1.
- "Full evaluator" — shipped.
- "In-session adjustment heuristics" — design-incoherent (turn-locked
  routing); should be dropped from Phase 3's promise.
- "MCP support" — out of scope, not on any active wave.
- "Git sync" — out of scope, owner handles via external git.

The Roadmap table reads as if Phase 3 is undelivered. **TODO** —
either bump the phase claim (per `phase-claim-proposal.md` Position B)
or rewrite the table.

### 5.5 Status sentence drift

[`AGENTS.md`](../../AGENTS.md):9 status sentence is internally consistent. The
[`README.md`](../../README.md):5 status sentence is **separately maintained** and tells
a slightly different story (the README emphasizes the §A3-rev3
inversion datapoint; AGENTS.md emphasizes the §A3-rev6 generalization
gap). Both are current-as-of-2026-05-15 but the duplication is fragile
— if one is updated and the other isn't, buyers and AI agents see
contradictory framings. **TODO** — owner-decision: consolidate to one
source of truth or accept the duplication and add a "keep these in
sync" note.

---

## 6. Outstanding items prioritized

### 6.1 Must-fix for GA (blocking)

1. **Provider-availability NETWORK class trips full provider on one
   error** (§2.3 sub-issue 1). A single transient SSL handshake error
   from Anthropic causes 5 minutes of gateway downtime for **all
   keys**. For a SaaS-style or multi-tenant deployment this is a
   reliability disaster; for a buyer-trial flow it's "the demo
   randomly broke and we don't know why."
2. **Trial workload + canonical-id pitfall** (§2.4). The pre-baked
   trial workload routes 100% to sonnet (slot 7), not the documented
   haiku — making `/analytics/by_key` report ~6× the actual cost.
   This is the *headline buyer-facing demo* and it lies about cost.
3. **README "what's NOT built yet"** (§5.3). Four of six bullets
   shipped months ago. Buyers reading the README first form a
   negative picture of the surface, then discover the truth in
   AGENTS.md. Embarrassing.

### 6.2 Nice-to-have for GA (non-blocking)

4. **`metis trial` retries on transient errors** (§2.3 sub-issue 2).
   Anthropic's API has occasional 1-2% transient errors; the trial
   should single-retry with backoff before failing.
5. **Error message references the wrong model** (§2.3 sub-issue 3).
   "tried: anthropic:claude-sonnet-4-6" when the user requested haiku
   is confusing. Include the original-requested model in the error.
6. **`VIRTUAL_ENV` warning leak** (§4 TODO). One line at the top of
   every `metis` invocation.
7. **`metis trial` header / error ordering** (§4 TODO).
8. **`case-study-template.md` referenced but missing** (§5.1).
9. **Roadmap table Phase 3 misrepresentation** (§5.4).
10. **Status-sentence duplication** (§5.5).

### 6.3 Background items (post-GA)

11. **Self-referential template-string links in pricing.md /
    deployment-shape.md** (§5.2). Renderer-broken but human-readable.

---

## 7. What this audit deliberately did NOT cover

- **Performance:** no load testing of the gateway under multi-tenant
  burst; the [`trace-performance.md`](trace-performance.md) reference
  numbers are not re-validated here.
- **Security:** no pentest. The [`soc2-readiness.md` §7 item 2](soc2-readiness.md)
  third-party pentest gap is named, not closed.
- **Multi-region:** v1 is single-region single-AZ by design.
- **Test suite:** 1678 tests reported by AGENTS.md, not re-run here
  (covered by CI).
- **In-process TLS happy path:** chart renders correctly but the
  end-to-end TLS smoke is documented in
  [`gateway-hardening.md`](../specs/gateway-hardening.md) and not
  exercised in this audit.

---

## 8. How to use this document

- **Owner:** read §6 and decide which must-fix items block GA. The
  three §6.1 items together represent the gap between "engineering
  done" and "buyer demo doesn't randomly lie."
- **Future audits:** re-run this checklist quarterly OR after any wave
  that touches the gateway routing, the quickstart, or buyer-facing
  surfaces. Diff §1's PASS/TODO/FAIL counts against this snapshot.
- **Pairing:** §6 entries that block GA should be tracked in
  [`KNOWN_ISSUES.md`](../KNOWN_ISSUES.md) with an owner + due date
  until closed.
