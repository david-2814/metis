# Repo Split — `metis` (OSS) / `metis-pro` (Private) Migration Plan

**Status:** Plan. The decision is recorded in [`pricing.md §12`](../specs/pricing.md) and [`STRATEGY.md §5`](../STRATEGY.md) (2026-05-17). The execution is **not yet started**; this doc is the checklist that will drive it.
**Last updated:** 2026-05-17

> Executable migration plan for splitting the current monorepo into a public Apache-2.0 `metis` repo and a private `metis-pro` repo. Reads top-to-bottom: principle → scope → extension Protocols → file moves → repo setup → test compatibility → migration order → risk callouts.
>
> If you only read one section, read [§3 Extension Protocols](#3-extension-protocols-the-boundary). Everything else follows from getting that boundary right.

---

## 1. Principle

The OSS substrate is **standalone-usable** — a CTO clones `metis`, runs `metis trial` on their own workload, sees real `savings_pct`, never needs `metis-pro`. The private repo holds the operationally-sensitive surfaces that only matter at team / SaaS scale.

The boundary discipline forces the OSS substrate to expose **clean extension Protocols** so `metis-pro` plugs in at runtime without source-level coupling. The OSS substrate ships with **noop defaults** for every Protocol — zero crippleware, zero "feature flags hiding things from you."

## 2. Scope

### 2.1 What stays in `metis` (OSS, Apache-2.0)

Everything currently in the repo *except* the items in §2.2 below. Concretely:

| Path | Notes |
|---|---|
| [packages/metis-core/](../../packages/metis-core/) | Entire library — canonical IR, adapters, routing, patterns, memory, tools, skills, eval (heuristic only), pricing, analytics, trace, events, observability, audit, redaction, sessions, workers |
| [apps/gateway/](../../apps/gateway/) | Gateway substrate **minus** `billing/` and `signup.py` (see §2.2) |
| [apps/server/](../../apps/server/) | HTTP/WS agent surface |
| [apps/cli/](../../apps/cli/) | `metis chat`, `tui`, `serve`, `gateway`, `evaluate`, `trial`, `customer-report`, `trial-status`, `audit export`, `trace prune/vacuum`, `backup/restore`, `user export/forget` |
| [packages/metis-core/eval/](../../packages/metis-core/src/metis_core/eval/) | Includes `LLMJudge` + `HybridJudge` *classes* (the substrate is generic). The **rubric prompt library** moves to `metis-pro` (see §2.2). |
| Multi-user identity fields on `GatewayKey` (`user_id`, `team_id`, `customer_tier`) | Stay in OSS as forward-compatible schema. Generic, useful for self-hosters; no enforcement logic depends on them being Pro-only. |
| `metis audit export` CLI | The export *mechanism* is generic SQL projection; not Pro-sensitive. |
| Concierge CLIs (`metis trial`, `customer-report`, `trial-status`) | Buyer-trial-recipe tooling; a buyer should be able to run them on their own deployment. |
| Per-key analytics (`/analytics/cost`, `/analytics/by_key`, `/analytics/savings`, `/analytics/quality`) | Per-key is the Community-tier identity dimension. |
| All operations docs (`docs/operations/`), specs (`docs/specs/`), sales toolkit (`docs/sales/`), strategy (`docs/STRATEGY.md`), business model (`docs/business-model.md`) | Public. The thinking is part of the trust signal. |

### 2.2 What moves to `metis-pro` (private, all-rights-reserved)

Only the operationally-sensitive surfaces. Smaller than `pricing.md §7.2` implies because most Pro features are *access*-restricted, not *code*-restricted.

| Current path | Destination in `metis-pro/` | Reason |
|---|---|---|
| [apps/gateway/src/metis_gateway/billing/](../../apps/gateway/src/metis_gateway/billing/) | `metis_pro/billing/` | Stripe integration, subscription lifecycle, webhook handling, plan transitions, failed-payment grace. Operationally sensitive. |
| [apps/gateway/src/metis_gateway/signup.py](../../apps/gateway/src/metis_gateway/signup.py) | `metis_pro/signup/` | Magic-link flow, account creation, key issuance UX. |
| Accounts store logic (`~/.metis/gateway/accounts.json` handling) | `metis_pro/accounts/` | PII handling (emails, names); deserves to be private regardless of license. |
| Route handlers for `/account/*`, `/signup`, `/signup/verify`, `/webhooks/stripe`, `/account/billing/*` | `metis_pro/routes/` | Mounted via `AnalyticsExtension` Protocol from OSS. |
| Route handlers for `/analytics/by_user`, `/analytics/by_team` | `metis_pro/analytics_overlays/` | The **rollup SQL** stays in OSS (`packages/metis-core/analytics/`); only the *HTTP route mounting* moves. Self-hosters can call the rollup function directly; only Pro buyers get the hosted endpoint. |
| **Curated LLM-judge rubric library** (the prompt templates, not the `LLMJudge` class itself) | `metis_pro/judges/rubrics/` | The actual evaluator IP; this is what makes the LLM-judge tier worth paying for. |
| Hosted dashboard UI (when built) | `metis_pro/dashboard/` | Web UI surface for Pro buyers. |
| Enterprise SAML / OIDC / SCIM glue (when built) | `metis_pro/enterprise/` | Procurement-grade integrations. |

**Not moving** (despite living near Pro features today):

- The `auth.py` keystore — schema stays in OSS; `metis-pro` only adds enforcement logic if needed
- The Wave-15 quota / tier-axis composition primitives — generic, useful for self-hosters
- The `gateway.key_issued` / `_revoked` / `_rotated` / billing audit event types — closed event catalog, stays canonical in OSS

## 3. Extension Protocols — the boundary

The OSS substrate exposes a small number of `Protocol`s in [packages/metis-core/src/metis_core/extensions.py](../../packages/metis-core/src/metis_core/extensions.py) (**new file**). `metis-pro` implements them. The gateway / server / CLI consume the Protocol, never the concrete implementation.

Draft contract (subject to refinement during execution):

```python
# packages/metis-core/src/metis_core/extensions.py  -- NEW FILE

from __future__ import annotations
from decimal import Decimal
from typing import Protocol, runtime_checkable
from starlette.applications import Starlette


@runtime_checkable
class BillingBackend(Protocol):
    """Optional billing surface. OSS default is a noop; metis-pro provides Stripe-backed."""

    async def record_usage(self, account_id: str, savings_usd: Decimal) -> None: ...
    async def check_active(self, account_id: str) -> bool: ...
    async def current_tier(self, account_id: str) -> str: ...  # "free" | "pro" | "enterprise"


@runtime_checkable
class SignupBackend(Protocol):
    """Optional self-serve signup. OSS default returns 404; metis-pro mounts /signup, /signup/verify."""

    def register_routes(self, app: Starlette) -> None: ...


@runtime_checkable
class AnalyticsExtension(Protocol):
    """Optional per-user / per-team rollup endpoints. OSS exposes per-key; Pro overlays add per-user/team."""

    def register_routes(self, app: Starlette) -> None: ...


@runtime_checkable
class JudgeRubricProvider(Protocol):
    """Provides curated rubric prompts to LLMJudge / HybridJudge. OSS ships a noop; metis-pro provides the curated library."""

    def rubric_for(self, subject_kind: str, workload_id: str | None) -> str | None: ...
    def rubric_version(self) -> str: ...


# Noop defaults — OSS ships these
class NoopBillingBackend:
    async def record_usage(self, account_id: str, savings_usd: Decimal) -> None: pass
    async def check_active(self, account_id: str) -> bool: return True
    async def current_tier(self, account_id: str) -> str: return "free"

class NoopSignupBackend:
    def register_routes(self, app: Starlette) -> None: pass

class NoopAnalyticsExtension:
    def register_routes(self, app: Starlette) -> None: pass

class NoopJudgeRubricProvider:
    def rubric_for(self, subject_kind: str, workload_id: str | None) -> str | None: return None
    def rubric_version(self) -> str: return "noop-1.0"
```

The gateway and server compose these at startup:

```python
# apps/gateway/src/metis_gateway/runtime.py — UPDATED
from metis_core.extensions import (
    BillingBackend, SignupBackend, AnalyticsExtension, JudgeRubricProvider,
    NoopBillingBackend, NoopSignupBackend, NoopAnalyticsExtension, NoopJudgeRubricProvider,
)

@dataclass
class GatewayConfig:
    # ...
    billing: BillingBackend = field(default_factory=NoopBillingBackend)
    signup: SignupBackend = field(default_factory=NoopSignupBackend)
    analytics: AnalyticsExtension = field(default_factory=NoopAnalyticsExtension)
    judge_rubrics: JudgeRubricProvider = field(default_factory=NoopJudgeRubricProvider)
```

`metis-pro` wires real implementations:

```python
# metis-pro/src/metis_pro/setup.py
from metis_gateway.app import GatewayConfig
from metis_pro.billing import StripeBillingBackend
from metis_pro.signup import MagicLinkSignupBackend
from metis_pro.analytics_overlays import ProAnalyticsRoutes
from metis_pro.judges import CuratedRubricLibrary

def build_pro_config(...) -> GatewayConfig:
    return GatewayConfig(
        # ...
        billing=StripeBillingBackend(...),
        signup=MagicLinkSignupBackend(...),
        analytics=ProAnalyticsRoutes(...),
        judge_rubrics=CuratedRubricLibrary(...),
    )
```

## 4. File-by-file migration checklist

Sequenced so each step leaves the OSS repo with a green test suite. Do NOT batch these — each is its own PR.

### 4.1 Pre-work in OSS (no code moves yet) — **DONE 2026-05-17**

- [x] Create `packages/metis-core/src/metis_core/extensions.py` with the four Protocols + noop defaults
- [x] Update `GatewayConfig` ([apps/gateway/.../app.py](../../apps/gateway/src/metis_gateway/app.py)) to accept the four extension fields with noop defaults
- [x] Update `ServerConfig` ([apps/server/.../app.py](../../apps/server/src/metis_server/app.py)) similarly (analytics_extension only — gateway-only Protocols stay on the gateway side)
- [x] Add `tests/test_extension_contract.py` in OSS with a `FakePro` implementation that exercises every Protocol — catches breaking changes before `metis-pro` consumes them
- [x] Verify `uv run pytest` is green (1858 passed — was 1841 + 17 extension-contract tests)
- [x] Single PR; review checkpoint: "no behavior change for any existing call path"

### 4.2a Refactor (Protocol-ize, no code move yet) — **DONE 2026-05-17**

- [x] Extend `BillingBackend` Protocol with `register_routes(app)` (mirroring SignupBackend / AnalyticsExtension) so the boot-time route-mount surface is part of the contract
- [x] Add `StripeBillingBackend` adapter in `apps/gateway/src/metis_gateway/billing/backend.py` implementing `BillingBackend` Protocol; record_usage / check_active / current_tier / register_routes dispatch into the existing `BillingService` / `BillingState`
- [x] Wire `build_app` to instantiate `StripeBillingBackend` when `BillingConfig.enabled=True` and pass it via `GatewayConfig.billing_backend`; mount routes via `billing_backend.register_routes(app)` instead of explicit Route() construction
- [x] Update `test_extensions.py` contract tests to cover `BillingBackend.register_routes`
- [x] Verify 1859 passed / 1 skipped (full OSS suite still green; one billing test skipped pre-existing)

### 4.2b Physical move — `billing/` to `metis-pro` — **DONE 2026-05-18**

- [x] Stand up the `metis-pro` private repo with pyproject + ruff + pytest config matching OSS conventions; uv `tool.uv.sources` overrides `metis-core` / `metis-gateway` to a sibling `~/git/metis/` checkout for local dev
- [x] Copy [apps/gateway/src/metis_gateway/billing/](../../apps/gateway/src/metis_gateway/billing/) → `metis-pro/src/metis_pro/billing/` (9 modules, 3054 LOC)
- [x] Copy `apps/gateway/tests/test_billing/` → `metis-pro/tests/billing/` (7 test files, 57 tests)
- [x] Copy `apps/gateway/tests/conftest.py` → `metis-pro/tests/conftest.py` (shared `runtime` + `scripted_adapter` fixtures)
- [x] Copy `apps/cli/src/metis_cli/billing_admin.py` → `metis-pro/src/metis_pro/cli/billing_admin.py` (operator-side `metis billing status` / `usage-record` subcommands)
- [x] Bulk-rewrite imports: `metis_gateway.billing.*` → `metis_pro.billing.*` (36 references); cross-test imports `apps.gateway.tests.test_billing.conftest` → `tests.billing.conftest` (2 references)
- [x] Delete `apps/gateway/src/metis_gateway/billing/` from OSS
- [x] Delete `apps/gateway/tests/test_billing/` from OSS
- [x] Delete `apps/cli/src/metis_cli/billing_admin.py` from OSS
- [x] Refactor OSS `apps/gateway/src/metis_gateway/app.py`: remove all `from metis_gateway.billing` imports; remove `BillingConfig` field on `GatewayConfig`; remove `BillingState` field on `_AppState`; remove `BillingError` exception handler + `_billing_err_handler`; remove `_resolve_tier_caps` function; hardcode `tier_caps=None` at the 2 `enforce_quotas` call sites
- [x] Refactor OSS `apps/gateway/src/metis_gateway/cli.py`: remove `BillingConfig` import + all `--enable-billing` / `--billing-*` flags + the `billing_cfg` construction block
- [x] Refactor OSS `apps/cli/src/metis_cli/main.py`: remove `metis billing` subparser + the `args.command == "billing"` handler block + `--enable-billing` flag block on the gateway subcommand
- [x] Mark 3 tier-axis cap-blocking tests in `metis-pro/tests/billing/test_quota_composition.py` with `pytest.mark.skip` (see §4.2c follow-on below); the 2 non-cap-blocking tests stay active
- [x] Verify OSS `uv run pytest` green: **1801 passed, 1 skipped** (was 1858, -57 billing tests)
- [x] Verify metis-pro `uv run pytest` green: **54 passed, 3 skipped**
- [x] Ruff clean on both repos (1 unused-import in OSS app.py auto-fixed; 5 import-ordering + 2 format fixes in metis-pro auto-fixed)

### 4.2c Tier-axis quota composition follow-on — **DONE 2026-05-18**

Closes the 3 cap-blocking tests in `metis-pro/tests/billing/test_quota_composition.py` that were skipped at the end of §4.2b. Implementation: **Option A** — a new `TierCapsResolver` Protocol; Pro overlay provides the real implementation; OSS keeps a noop.

The Protocol landed in **`apps/gateway/src/metis_gateway/extensions.py`** (NEW file) rather than `metis_core.extensions.py`. Reason: the signature references gateway-private types (`GatewayKey` from `metis_gateway.auth`, `TierCaps` from `metis_gateway.quotas`); keeping those types out of metis-core preserves the layering invariant that metis-core never references metis-gateway. The convention for the broader split is: Protocols whose signatures only need stdlib types (or Starlette via TYPE_CHECKING) live in `metis_core.extensions`; Protocols with gateway-private types live in `metis_gateway.extensions`.

OSS changes:

- [x] `apps/gateway/src/metis_gateway/extensions.py` — new module exporting `TierCapsResolver` (runtime-checkable) and `NoopTierCapsResolver` (OSS default, returns `None` for every key).
- [x] `apps/gateway/src/metis_gateway/app.py` — `GatewayConfig` gains a `tier_caps_resolver: TierCapsResolver` field with `NoopTierCapsResolver` as the default factory. `_AppState` carries the same field. `build_app(..., tier_caps_resolver=...)` accepts an override. Both `enforce_quotas` call sites (`chat_completions` line 428, `messages` line 641) now call `st.tier_caps_resolver(key)` instead of the §4.2b-era hardcoded `tier_caps=None`.
- [x] `run_gateway` detects noop vs real `TierCapsResolver` (mirroring the billing / signup forwarding pattern) and passes through.

metis-pro changes:

- [x] `src/metis_pro/quotas.py` — new module with `ProTierCapsResolver`. The class wraps `(SignupState, BillingState)` captured at composition time. Its `__call__(key)` lifts the pre-§4.2b `_resolve_tier_caps` body verbatim: walks `key → account_for_key → enforce_failed_payment_state → get_customer → tier`; returns the `TierCaps(account_id, key_ids, daily_cap_usd, monthly_cap_usd)` from `BillingConfig` only when the resolved tier is `"free"`. Non-free tiers and unknown keys return `None`.
- [x] `tests/billing/conftest.py` — `billing_client_http` now constructs `ProTierCapsResolver(signup_state, billing_state)` alongside the `MagicLinkSignupBackend` + `StripeBillingBackend`, and passes all three to `build_app`.
- [x] `tests/billing/test_quota_composition.py` — the 3 cap-blocking tests are un-skipped (the `_TIER_AXIS_INJECTION_PENDING` decorator is removed from all three).

Verification:

- [x] OSS `uv run pytest`: **1781 passed, 1 skipped** (unchanged from §4.3; the noop preserves the pre-§4.2c behavior, so no test results moved).
- [x] metis-pro `uv run pytest`: **77 passed, 0 skipped** (was 74 + 3 skip; the 3 tier-axis tests are now active and green).
- [x] Ruff clean on both repos (1 long-line fix in `metis_pro/quotas.py` auto-formatted).

### 4.3 Second move — `signup.py` + accounts store — **DONE 2026-05-18**

- [x] Copy `apps/gateway/.../signup.py` → `metis-pro/src/metis_pro/signup.py` (984 LOC, single flat module — promoting to a package directory is a future refactor if it grows)
- [x] Copy `apps/gateway/tests/test_signup.py` → `metis-pro/tests/test_signup.py`
- [x] Add `MagicLinkSignupBackend` adapter at the end of `metis_pro/signup.py` implementing `SignupBackend` Protocol: wraps `SignupState`, stashes it on `app.state.signup`, mounts the 5 routes (`POST /signup`, `POST /signup/verify`, `GET /account/keys`, `POST /account/keys`, `DELETE /account/keys/{key_id}`), and registers the `SignupError → 4xx envelope` exception handler via `app.add_exception_handler`
- [x] Update `_state(request)` in metis-pro's signup.py to read from `app.state.signup` (the new Pro stash) instead of the legacy `app.state.app_state.signup` (OSS's _AppState which no longer carries the field); legacy fallback preserved for callers that still pass the old shape
- [x] Bulk-rewrite imports: `metis_gateway.signup` → `metis_pro.signup` (5 references — billing/routes.py + tests/billing/conftest.py)
- [x] Delete `apps/gateway/src/metis_gateway/signup.py` from OSS
- [x] Delete `apps/gateway/tests/test_signup.py` from OSS
- [x] Refactor OSS `apps/gateway/src/metis_gateway/app.py`: remove the `from metis_gateway.signup import (...)` block (10 symbols); remove `signup: SignupConfig | None = None` from `GatewayConfig`; remove `signup: SignupState | None = None` from `_AppState`; remove `_resolve_signup_config` helper; remove `signup_state` construction + explicit signup route list + the `SignupError` exception handler. Replace the explicit signup route mounting with `signup_backend.register_routes(app)` called after Starlette construction (mirroring the §4.2a billing pattern)
- [x] Refactor OSS `run_gateway`: detect noop vs real `SignupBackend` (mirror the billing pattern); pass through to `build_app(signup_backend=...)`
- [x] Refactor OSS `apps/gateway/src/metis_gateway/cli.py`: remove `from metis_gateway.signup import SignupConfig`; remove `signup_enabled` / `signup_dashboard_url` / `signup_accounts_path` parameters; remove the `signup_cfg` construction block
- [x] Refactor OSS `apps/cli/src/metis_cli/main.py`: remove the three `--enable-signup` / `--signup-dashboard-url` / `--signup-accounts-path` flags from the `gateway` subparser; remove the three matching args from the `run_gateway_command(...)` call
- [x] Fix metis-pro test fixtures: `tests/test_signup.py::signup_client` + `tests/billing/conftest.py::billing_client_http` now compose `MagicLinkSignupBackend(build_signup_state(signup_config))` and pass it via `build_app(signup_backend=...)` — the Pro composition pattern
- [x] Verify OSS `uv run pytest` green: **1781 passed, 1 skipped** (was 1801, -20 signup tests)
- [x] Verify metis-pro `uv run pytest` green: **74 passed, 3 skipped** (was 54 + 3, +20 signup tests)
- [x] Ruff clean on both repos (3 import-ordering fixes in metis-pro auto-fixed)

### 4.4 Third move — per-user / per-team analytics route handlers

- [ ] Identify the *route handler* code (vs. the rollup SQL — the latter stays OSS)
- [ ] Move only the route mounting to `metis-pro/src/metis_pro/analytics_overlays/`
- [ ] Wrap under `AnalyticsExtension.register_routes()`
- [ ] Leave the rollup functions in [packages/metis-core/analytics/](../../packages/metis-core/src/metis_core/analytics/) — they're library calls, not endpoints, and self-hosters benefit from them
- [ ] Delete the route handlers from OSS; verify OSS gateway exposes only `/analytics/cost`, `/analytics/by_key`, `/analytics/savings`, `/analytics/quality` (per-key is Community)

### 4.5 Fourth move — curated LLM-judge rubric library

- [ ] Audit [packages/metis-core/eval/](../../packages/metis-core/src/metis_core/eval/) for embedded rubric strings / prompt templates
- [ ] Extract them to `metis-pro/src/metis_pro/judges/rubrics/`
- [ ] Replace with `JudgeRubricProvider` Protocol calls in OSS
- [ ] OSS evaluator ships a minimal example rubric (one workload, low-quality) so the substrate is demonstrably functional standalone
- [ ] Move rubric-specific tests; OSS retains generic `LLMJudge` / `HybridJudge` substrate tests

### 4.6 Future moves (after dashboard / enterprise glue is built)

- [ ] Hosted dashboard UI → `metis-pro/dashboard/`
- [ ] SAML / OIDC / SCIM → `metis-pro/enterprise/`

## 5. Repo setup

### 5.1 `metis` (the OSS repo)

- **License:** Apache-2.0. Add `LICENSE` file at repo root. Update `README.md:367` ("License: TBD") to point at the LICENSE.
- **Visibility:** Public (when ready to publish)
- **PyPI:** Each `metis-core`, `metis-server`, `metis-gateway`, `metis-cli` package publishes independently. Semver discipline.
- **CI:** GitHub Actions as today. The extension-contract test suite gates `metis-pro` compatibility.
- **Contributor docs:** `CONTRIBUTING.md` (new), `CODE_OF_CONDUCT.md` (new — standard CC by Contributor Covenant). The existing `AGENTS.md` stays.

### 5.2 `metis-pro` (the private repo)

- **License:** All rights reserved. No explicit license file (closed source by default).
- **Visibility:** Private GitHub repo.
- **Layout:** Mirror the OSS workspace shape (`pyproject.toml`, `src/metis_pro/`, `tests/`).
- **Dependency on OSS:** Pin `metis-core==X.Y.Z` etc. in `pyproject.toml`. Local dev uses a `uv` workspace that overrides the pin with a path dep pointing at a sibling `metis/` checkout.
- **CI:** Separate GitHub Actions; tests run against the pinned OSS version.
- **Secrets:** All Stripe / SES / etc. credentials live in CI secret store, never in code. (Same as today — the move doesn't change this; it makes it more obvious.)

### 5.3 Versioning + release coordination

- OSS publishes to PyPI on its own cadence (`metis-core 1.0.0`, `1.1.0`, …)
- `metis-pro` bumps `metis-core` in its `pyproject.toml` after each OSS release
- Breaking changes to extension Protocols require:
  - OSS minor-version bump
  - `metis-pro` PR consuming the new minor version (gated until the Protocol change is merged)
  - Optional: a deprecation warning cycle if the change is significant

## 6. Test compatibility

The extension-contract test suite (§4.1) is the safety net. It lives in OSS and verifies:

1. **The Protocols exist with the documented method signatures.** Catches accidental Protocol changes.
2. **The noop defaults satisfy the Protocols.** Catches "I added a method but forgot the noop."
3. **A `FakePro` implementation exercises every endpoint mount path.** Verifies the gateway correctly calls the Protocol at the documented points.
4. **The OSS gateway boots and serves traffic with all-noop config.** No silent dependency on Pro implementations.

In `metis-pro`, an integration test suite exercises the real Stripe / signup / analytics overlays against the pinned OSS gateway. This is what catches "OSS Protocol change broke Pro."

## 7. Migration order — the safe sequence

Each step is a complete unit of work; don't start the next until the previous is green in CI on both repos.

1. **Pre-work in OSS** (§4.1) — extension Protocols + tests. ~1 day.
2. **Stand up `metis-pro`** (§5.2) — empty private repo, CI scaffold. ~0.5 day.
3. **Migrate billing** (§4.2) — first real move; biggest blast radius. ~2-3 days.
4. **Migrate signup + accounts** (§4.3) — ~1-2 days.
5. **Migrate analytics overlays** (§4.4) — ~1 day.
6. **Migrate rubric library** (§4.5) — ~1-2 days; depends on how embedded the rubric prompts are in the current eval/ code.
7. **Switch OSS to Apache-2.0 + add LICENSE** — once §3-6 are stable. ~0.5 day.
8. **Public announcement / publish** — separate decision; not part of this plan.

Total estimate: **~7-10 engineering days at part-time pace**, spread over 3-4 calendar weeks. Significantly cheaper than the build-up. Most of the budget goes to billing — the rest are mechanical moves once the Protocols stabilize.

## 8. Risk callouts

1. **Rubric library extraction is the riskiest move.** If LLM-judge prompts are deeply embedded in the `eval/` code paths, the carve-out gets messy. Mitigation: audit `eval/` *before* committing to §4.5; if the prompts are scattered, refactor them into a single `rubrics.py` module *in OSS* first (no Pro coupling), then move that module wholesale.

2. **Extension Protocols are now a load-bearing public API.** Any breaking change forces a coordinated two-repo release. Mitigation: keep the Protocol surface *small* — four Protocols, ~12 methods total. Resist temptation to add more.

3. **Self-hosters now have a feature-degraded experience.** They get OSS-only: no LLM judge prompts library, no per-user/team route handlers, no signup. Mitigation: be deliberate about what OSS Defaults provide so the degradation is "no Pro features," not "broken." Concierge tooling (`metis trial`, `customer-report`) stays in OSS specifically so self-hosters can still demonstrate the savings story.

4. **Cross-cutting changes still happen.** Adding a new event type that Pro analytics consumes is now 2 PRs. Mitigation: batch related Pro changes; lean on the extension-contract test suite to catch issues at the OSS PR stage.

5. **The "OSS is hollow shell" failure mode.** If, over time, every interesting feature gets pushed to Pro, the OSS adoption funnel breaks. Mitigation: **invariant** — pricing.md §11.1 ("Free tier remains usable single-user without a credit card") applies to the OSS repo too. Every PR that proposes pushing a feature to Pro must justify why it isn't single-user-useful.

6. **License confusion at the boundary.** `metis-pro` depends on Apache-2.0 `metis` — that's legal and standard. But contributors to `metis` need to understand their contributions are Apache-2.0, not Pro-licensed. Mitigation: `CONTRIBUTING.md` explicitly states this; PR template includes a contributor-agreement checkbox.

## 9. What this plan deliberately doesn't decide

- **The OSS publication date.** This plan makes the repo *ready* to publish; the actual "make repo public" decision is the owner's separate call, gated on first-buyer feedback / GA-readiness comfort / pre-publish security review.
- **Whether `metis-pro` ever becomes its own commercial product line.** Today it's the private code that powers Pro deployments. A future decision could split it further (e.g., a `metis-pro-cloud` for SaaS, `metis-pro-enterprise` for VPC). Out of scope.
- **Contributor governance for OSS.** Maintainer model, RFC process, release cadence — all standard OSS-project decisions; pick patterns from comparable projects at publish time.
- **Whether to relicense in the future.** Apache-2.0 → BUSL is reversible if a fork-and-SaaS threat materializes (probably 2028+); not planning for it now.

## 10. Reversibility — what the rollback looks like

If the split proves too painful within ~3 months:

- Merge `metis-pro` back into the OSS repo as a `private/` subdirectory under a different license header
- Single `pyproject.toml` again, single CI, single PR per cross-cutting change
- Retire extension Protocols (or keep them, they remain useful for testability)
- Re-pin pricing.md §9.5 as "decided differently"

This isn't free — there's two-repo overhead to unwind — but it's not catastrophic. The Protocols themselves are valuable independent of the split.

---

## Pointers

- [`docs/specs/pricing.md`](../specs/pricing.md) — the ratified pricing spec; §9.5 retired and §12 decision log updated 2026-05-17.
- [`docs/STRATEGY.md §5`](../STRATEGY.md) — 2026-05-17 dated decision entry.
- [`docs/business-model.md`](../business-model.md) — business-model synthesis.
- [`packages/metis-core/src/metis_core/extensions.py`](../../packages/metis-core/src/metis_core/extensions.py) — the four extension Protocols (created in §4.1).
- [`AGENTS.md`](../../AGENTS.md) — current repo state and conventions; will need a status entry once the split is executed.
