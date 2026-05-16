# API Versioning Specification

**Status:** v1 enforcement live (Wave 11)
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
| Response (Metis-owned routes) | `Metis-API-Versions-Supported: 1.0` | always — comma-separated supported list |
| Response (deprecated versions only) | `Deprecation: true` + `Sunset: <ISO date>` | conditional |

The middleware echoes whatever the client requested back on the response so
clients can confirm the version that served them. The
`Metis-API-Versions-Supported` header advertises the comma-separated list of
versions the server currently accepts so clients can discover what's available
without parsing 410 bodies (see §3.3 for pre-flight discovery via `OPTIONS`).

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

### 3.1 Lifecycle

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
   - The middleware logs a warning that includes the caller's bearer-hash
     fingerprint (first 12 hex chars of SHA-256(token)) so the operator
     can grep `keys.json` for the buyer to notify before sunset.
4. After the sunset date passes, the version is automatically rejected at
   the middleware boundary: requests pinning it get HTTP 410 with the
   `version_unsupported` body (§3.2). The sunset date arithmetic runs
   per-request — no scheduled job, no operator action required to flip
   the cutover.

Six months is the floor, not a contract. A particularly disruptive break
(data loss, security) may justify a shorter window with explicit buyer
outreach. Always log to telemetry on the warning path so we can see who is
still pinned to the old version before sunset.

### 3.2 Below-min and past-sunset rejection (HTTP 410)

Two conditions trigger automatic rejection:

| Condition | `reason` |
|---|---|
| Request `Metis-API-Version` parses below `MIN_SUPPORTED_VERSION` | `below_min` |
| Request `Metis-API-Version` is in `DEPRECATED_VERSIONS` and `today > sunset_date` (UTC) | `past_sunset` |

Both return:

```http
HTTP/1.1 410 Gone
Content-Type: application/json
Metis-API-Version: 0.9                  ← echoed (the requested value)
Metis-API-Versions-Supported: 1.0
Sunset: 2026-11-15                      ← when applicable

{
  "error": {
    "code": "version_unsupported",
    "requested": "0.9",
    "min_supported": "1.0",
    "current": "1.0",
    "reason": "below_min",
    "message": "Metis-API-Version '0.9' is no longer supported (reason=below_min, min_supported=1.0, current=1.0)"
  }
}
```

The body shape is fixed; clients should branch on `error.code` (and
optionally `error.reason`), not parse the prose `error.message`.

**Sunset comparison is strict (`today > sunset_date`, UTC).** A request on
the sunset date itself is still served as deprecated; the rejection kicks
in the day after. This is intentional: the buyer gets the full announced
window, and operators can reproduce edge cases by freezing the clock to the
boundary date.

### 3.3 Pre-flight discovery via `OPTIONS`

Clients can pre-flight check supported versions without committing to a
real request. An `OPTIONS` request to any Metis-owned path short-circuits
through the middleware and returns:

```http
HTTP/1.1 204 No Content
Metis-API-Version: 1.0
Metis-API-Versions-Supported: 1.0
```

If the client pins a deprecated version on the `OPTIONS`, the response
also carries `Deprecation: true` + `Sunset: <date>` so the client knows
to upgrade before the boundary. (`OPTIONS` for an unsupported version
returns 410, same as a real request — pre-flight isn't a back door.)

This is loopback-only in v1; there's no CORS pre-flight to coordinate
with, so the OPTIONS short-circuit doesn't conflict with browser
semantics.

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
2. Resolve to a `VersionResolution` (resolved/deprecated/unsupported/sunset/reason).
3. Stash the resolved version on `scope["state"].metis_api_version` so
   downstream handlers can read it via `request.state.metis_api_version`.
4. If unsupported (below-min or past-sunset), emit HTTP 410 with the
   documented `version_unsupported` body (§3.2). Skip the route handler.
5. If within-window deprecated, stamp `Deprecation: true` + `Sunset: <date>`
   on the response and log a warning with the caller's bearer-hash
   fingerprint (first 12 hex chars of SHA-256(token), matching what the
   keystore persists, so the operator can identify the buyer to notify).
6. Stamp `Metis-API-Version: <resolved>` and `Metis-API-Versions-Supported:
   <comma-separated list>` on every Metis-owned response.
7. Short-circuit `OPTIONS` requests with 204 + the version-negotiation
   headers (§3.3).
8. **Skip provider-shape paths** entirely. The middleware is constructed
   with a `skip_path_prefixes` tuple; the gateway sets it to its two
   provider-shape paths; the server passes `()` (no provider-shape routes
   there). The skip runs first, so even auth-failing or version-unsupported
   provider-shape requests pass through unmodified.

### 4.1 Version-specific dispatch (light scaffolding)

The middleware doesn't dispatch to per-version handlers — every route
resolves to the same handler. Per-version behavior is achieved by
handlers reading `request.state.metis_api_version` and branching.
Worked example for a hypothetical 1.1 minor that adds a `by_workspace`
field to `/analytics/cost`:

```python
# apps/server/src/metis_server/analytics.py
async def cost(request: Request) -> Response:
    body = {"by_model": _by_model(...), "window": _window(...)}
    # 1.1 adds the per-workspace rollup. 1.0 callers don't see it.
    if request.state.metis_api_version >= "1.1":
        body["by_workspace"] = _by_workspace(...)
    return _json(body)
```

Two caveats when comparing version strings directly:

1. **String comparison is OK for single-digit minors** (`"1.0" < "1.1"`)
   but breaks for two-digit minors (`"1.10" < "1.2"` lexically). When
   that becomes a real concern, switch to the middleware's `_parse_semver`
   helper.
2. **The string is always the *resolved* version**, not necessarily what
   the client requested — absent header resolves to `CURRENT_VERSION`,
   so a handler branching on `>= "1.1"` will pick the new behavior for
   no-header callers as soon as `CURRENT_VERSION` bumps. That's usually
   what you want.

No version-dispatch logic is wired beyond this in v1; every Metis-owned
endpoint is `1.0` today. The scaffolding lets future majors land without
churning the call surface.

---

## 5. Invariants

1. **Request `Metis-API-Version` is optional.** Absent → resolves to current.
2. **Response always carries `Metis-API-Version`** on Metis-owned routes.
   Provider-shape routes never carry it.
3. **Response always carries `Metis-API-Versions-Supported`** on Metis-owned
   routes (200, 204, 410 alike). Provider-shape routes never carry it.
4. **Provider-shape routes pass the request through unchanged.** No header
   stripping, no header injection, no version logic, no 410 enforcement.
5. **Below-min and past-sunset are rejected with HTTP 410**, never silently
   downgraded. The caller must explicitly upgrade.
6. **Deprecated responses carry both `Deprecation` and `Sunset` headers.** A
   `Sunset` without `Deprecation` is meaningless; never stamp one alone.
7. **`Sunset` comparison is strict (`today > sunset_date`, UTC).** The
   sunset date itself is still served — boundary day is part of the window.
8. **No bus events emitted.** Versioning is a transport concern, not an
   audited operation.
9. **Streaming responses keep the headers.** The middleware is pure ASGI so
   SSE bodies stream uninterrupted.
10. **`request.state.metis_api_version` is always set** on Metis-owned
    routes before the handler runs, even for unsupported requests (though
    those don't reach the handler).

---

## 6. Errors

| Request version | Outcome |
|---|---|
| Absent / empty | Resolves to `CURRENT_VERSION`; no header reject |
| Listed in `DEPRECATED_VERSIONS`, within window | 200 served with `Deprecation: true` + `Sunset: <date>`; warning logged |
| Listed in `DEPRECATED_VERSIONS`, past sunset | **HTTP 410** `version_unsupported` (`reason="past_sunset"`); warning logged |
| Parses below `MIN_SUPPORTED_VERSION` | **HTTP 410** `version_unsupported` (`reason="below_min"`); warning logged |
| Unknown / future version (above min, not deprecated) | Echoed back, no rejection |
| Malformed version (non-semver string) | Echoed back, no rejection (defensive — better than 410'ing on a typo) |

The 410 body shape is documented in §3.2. Below-min and past-sunset are
the only two rejection paths in v1. The "echo unknown / malformed" choice
keeps the middleware forward-compatible: a 1.1 client hitting a 1.0 server
gets served at 1.0 rather than rejected, which matches semver minor-bump
expectations.

---

## 7. Testing

1. Default header value when absent — resolves to `CURRENT_VERSION`.
2. Header round-trips on a response (request `1.0` → response `1.0`).
3. `Metis-API-Versions-Supported` round-trips on every Metis-owned response.
4. `Deprecation: true` + `Sunset: <date>` surface on within-window
   deprecated versions; the response stays 200.
5. Below-`MIN_SUPPORTED_VERSION` requests return **HTTP 410** with the
   documented `version_unsupported` body and `reason="below_min"`.
6. Past-sunset deprecated versions return **HTTP 410** with
   `reason="past_sunset"`, exercised with a frozen-clock `now=` parameter
   to `resolve_version()` so the test is deterministic.
7. Boundary case: `today == sunset_date` still serves the deprecated
   response (strict `>` comparison).
8. `OPTIONS` requests return 204 with the version-negotiation headers;
   `OPTIONS` for a deprecated version also carries `Deprecation` + `Sunset`.
9. `request.state.metis_api_version` is set before the handler runs
   (exercised via a dummy ASGI app driven directly through the middleware).
10. Provider-shape routes (`/v1/chat/completions`, `/v1/messages`) ignore
    the request `Metis-API-Version`, don't stamp the response headers, and
    aren't subject to 410 enforcement.

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
| 2026-05-15 | HTTP 410 (Gone), not 400, for below-min / past-sunset | "Gone" matches the semantics — the version was supported, isn't anymore. 400 would suggest a request shape error; 410 tells the client to upgrade. |
| 2026-05-15 | Strict `>` comparison for sunset (today on the boundary still serves) | Buyers get the full announced window. Operators can reproduce edge cases by freezing the clock to the boundary date. |
| 2026-05-15 | Pre-flight via `OPTIONS` short-circuit (204) | Loopback-only in v1 so no CORS pre-flight to coordinate with. Returning the negotiation headers on OPTIONS is the cleanest discovery affordance. |
| 2026-05-15 | `request.state.metis_api_version` for handler dispatch (not router-level version dispatch) | Per-version handlers would proliferate route registrations for every minor bump; a single handler with a version branch keeps the codebase flat. |
| 2026-05-15 | Bearer-hash fingerprint in deprecation warning logs (first 12 hex chars of SHA-256) | Operator needs to identify the buyer to notify pre-sunset. Keystore persists the same hash, so a `grep` against `keys.json` resolves the fingerprint. Truncated to 12 chars to keep logs readable; collision risk over a single keystore is negligible. |

---

## 9. References

- [`gateway.md`](gateway.md) — provider-shape endpoints (frozen).
- [`analytics-api.md`](analytics-api.md) — Metis-owned endpoints (versioned).
- [`server-api.md`](server-api.md) — base HTTP conventions this spec extends.
- [`apps/gateway/src/metis_gateway/middleware_versioning.py`](../../apps/gateway/src/metis_gateway/middleware_versioning.py) — gateway middleware.
- [`apps/server/src/metis_server/middleware_versioning.py`](../../apps/server/src/metis_server/middleware_versioning.py) — server middleware.
- [RFC 8594](https://datatracker.ietf.org/doc/html/rfc8594) — `Sunset` HTTP header.
