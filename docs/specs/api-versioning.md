# API Versioning Specification

**Status:** Draft v1
**Last updated:** 2026-05-15

> Pins the versioning posture for Metis's HTTP surfaces. Two surface categories:
> *provider-shape endpoints* (frozen by the upstream provider's SDK contract) and
> *Metis-owned endpoints* (versioned by us via the `Metis-API-Version` header).
> The split lets us evolve our own endpoints without breaking buyers mid-trial,
> while leaving the OpenAI / Anthropic-compatible paths untouched.

This spec depends on:

- [`gateway.md`](gateway.md) — defines the provider-shape endpoints
  (`/v1/chat/completions`, `/v1/messages`) this spec leaves frozen.
- [`analytics-api.md`](analytics-api.md) — defines the Metis-owned `/analytics/*`
  namespace this spec versions.
- [`server-api.md`](server-api.md) *(planned)* — base HTTP surface conventions.

---

## 1. Two surface categories

### 1.1 Provider-shape (frozen)

| Surface | Endpoint | Owned by |
|---|---|---|
| OpenAI Chat Completions | `POST /v1/chat/completions` | OpenAI's SDK contract |
| Anthropic Messages | `POST /v1/messages` | Anthropic's SDK contract |

Versioning is whatever the upstream provider says. The `/v1/` path segment is
**not** Metis's version — it's the provider's. When OpenAI ships a `/v2/`,
Metis adds a parallel route, not a sibling versioning scheme. Our buyers'
SDKs hardcode these paths; we don't get a vote.

These endpoints **ignore** the `Metis-API-Version` request header and never
stamp it on the response. Their response shape is the provider's contract.

### 1.2 Metis-owned (versioned by us)

| Surface | Endpoint(s) |
|---|---|
| Analytics | `/analytics/*` (analytics-api.md) |
| Sessions / turns / messages | `/sessions/*`, `/models`, `/turns/*` |
| Health / status | `/health`, `/healthz`, `/server/version` |
| Future Metis-specific surfaces | TBD |

Versioning is on us. Buyers in trial pin a version and get the documented
shape until at least the sunset date.

---

## 2. Versioning scheme

### 2.1 Header

| Direction | Header | Required |
|---|---|---|
| Request | `Metis-API-Version: 1.0` | no — absent → `CURRENT_VERSION` |
| Response (Metis-owned routes) | `Metis-API-Version: 1.0` | always |

The middleware echoes whatever the client requested back on the response so
clients can confirm the version that served them.

### 2.2 Versions

| Constant | Current value |
|---|---|
| `CURRENT_VERSION` | `1.0` |
| `MIN_SUPPORTED_VERSION` | `1.0` |
| `DEPRECATED_VERSIONS` | (empty) |

`CURRENT_VERSION` is what absent-header callers get. `MIN_SUPPORTED_VERSION`
is the floor below which a version is treated as deprecated. They match in
v1 (no deprecated versions exist yet).

### 2.3 Semver discipline

| Bump | When | Examples |
|---|---|---|
| Minor | Additive (new fields, new endpoints, looser validation) | `1.0 → 1.1` |
| Major | Breaking (removed fields, semantic changes, stricter validation) | `1.x → 2.0` |

Every breaking change bumps the major; every additive change bumps the minor.
Existing clients pinned to `1.0` keep `1.0` semantics for at least 6 months
(§3) after a `2.0` ships.

---

## 3. Deprecation policy

When a Metis-owned endpoint changes breakingly:

1. Bump major: `Metis-API-Version: 2.0` becomes the new `CURRENT_VERSION`.
2. Old version (`1.0`) added to `DEPRECATED_VERSIONS` with a sunset date
   ≥ 6 months out.
3. Responses to clients pinned to the deprecated version carry:
   - `Metis-API-Version: 1.0` (echoed)
   - `Deprecation: true` (RFC 8594 boolean form)
   - `Sunset: 2026-11-15` (ISO date — simplified profile of RFC 8594, which
     specifies HTTP-date; ISO is human-readable and parses cleanly in every
     dashboard / log surface we use)
4. After the sunset date, the version may be removed: requests pinning it
   resolve to current with a logged warning. Removal is its own decision —
   the sunset is a floor, not a trigger.

Six months is the floor, not a contract. A particularly disruptive break
(data loss, security) may justify a shorter window with explicit buyer
outreach. Always log to telemetry on the warning path so we can see who is
still pinned to the old version before removal.

---

## 4. Middleware

A pure ASGI middleware lives in:

- [`apps/gateway/src/metis_gateway/middleware_versioning.py`](../../apps/gateway/src/metis_gateway/middleware_versioning.py)
- [`apps/server/src/metis_server/middleware_versioning.py`](../../apps/server/src/metis_server/middleware_versioning.py)

Both files are near-identical; they're sibling apps and don't share a
parent module to import from. Pure ASGI rather than `BaseHTTPMiddleware` so
SSE / streaming responses aren't buffered.

Responsibilities:

1. Read `Metis-API-Version` from the request (default `CURRENT_VERSION`).
2. Stamp the resolved version on the response.
3. If the resolved version is in `DEPRECATED_VERSIONS` *or* parses below
   `MIN_SUPPORTED_VERSION`, stamp `Deprecation: true` and `Sunset: <date>`,
   and log a warning so operators see which buyer keys are still pinned.
4. **Skip provider-shape paths** entirely. The middleware is constructed
   with a `skip_path_prefixes` tuple; the gateway sets it to its two
   provider-shape paths; the server passes `()` (no provider-shape routes
   there).

No version-dispatch logic in v1. Every Metis-owned endpoint resolves to the
same handler regardless of `Metis-API-Version`. The scaffolding lets us add
version-conditional handlers later without changing the call surface.

---

## 5. Invariants

1. **Request `Metis-API-Version` is optional.** Absent → resolves to current.
2. **Response always carries `Metis-API-Version`** on Metis-owned routes.
   Provider-shape routes never carry it.
3. **Provider-shape routes pass the request through unchanged.** No header
   stripping, no header injection, no version logic.
4. **Deprecated responses carry both `Deprecation` and `Sunset` headers.** A
   `Sunset` without `Deprecation` is meaningless; never stamp one alone.
5. **No bus events emitted.** Versioning is a transport concern, not an
   audited operation.
6. **Streaming responses keep the headers.** The middleware is pure ASGI so
   SSE bodies stream uninterrupted.

---

## 6. Errors

The middleware does not reject requests on version grounds in v1.

- Unknown / future version → echoed back; warning logged.
- Malformed version (non-semver string) → echoed back; warning logged.
- Below `MIN_SUPPORTED_VERSION` → served with `Deprecation: true` (§3).

Future revs may add a `400 unsupported_version` for versions removed past
their sunset date. That decision is gated on real telemetry showing buyers
upgrade promptly enough that hard rejection is buyer-friendly.

---

## 7. Testing

1. Default header value when absent — resolves to `CURRENT_VERSION`.
2. Header round-trips on a response (request `1.0` → response `1.0`).
3. `Deprecation: true` + `Sunset: <date>` surface when the resolved version
   is in `DEPRECATED_VERSIONS` or parses below `MIN_SUPPORTED_VERSION`.
4. Provider-shape routes (`/v1/chat/completions`, `/v1/messages`) ignore
   the request `Metis-API-Version` and don't stamp it on the response.

---

## 8. Decision log

| Date | Decision | Rationale |
|---|---|---|
| 2026-05-15 | Header-based, not URL-based, for Metis-owned versioning | URL-based versioning composes badly with `/analytics/*`'s many endpoints; one header replaces N path prefixes. |
| 2026-05-15 | Provider-shape paths are not under our versioning scheme | They're frozen by the buyer's SDK contract. Forking them defeats the gateway's transparency. |
| 2026-05-15 | Pure ASGI middleware (not `BaseHTTPMiddleware`) | `BaseHTTPMiddleware` buffers streaming bodies; both gateway SSE and server WebSocket would break. |
| 2026-05-15 | Default to current when header absent | Buyers who don't care get the latest; buyers who do can pin. |
| 2026-05-15 | 6-month sunset minimum | Long enough that quarterly buyer release trains can adapt; short enough that we don't carry forever. |
| 2026-05-15 | No version-dispatch in v1 | Everything is `1.0`; the dispatch surface lands when `2.0` does, not preemptively. |
| 2026-05-15 | ISO date for `Sunset`, not RFC 8594 HTTP-date | Simpler to read in logs / dashboards; parses cleanly everywhere we consume it. Documented as a profile. |

---

## 9. References

- [`gateway.md`](gateway.md) — provider-shape endpoints (frozen).
- [`analytics-api.md`](analytics-api.md) — Metis-owned endpoints (versioned).
- [`server-api.md`](server-api.md) — base HTTP conventions this spec extends.
- [`apps/gateway/src/metis_gateway/middleware_versioning.py`](../../apps/gateway/src/metis_gateway/middleware_versioning.py) — gateway middleware.
- [`apps/server/src/metis_server/middleware_versioning.py`](../../apps/server/src/metis_server/middleware_versioning.py) — server middleware.
- [RFC 8594](https://datatracker.ietf.org/doc/html/rfc8594) — `Sunset` HTTP header.
