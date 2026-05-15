# Gateway Hardening Specification

**Status:** Draft v1 — Wave 12+ commitment, not a v1 default
**Last updated:** 2026-05-15

> Documents the perimeter every buyer adds in front of the gateway before they
> let real internet traffic reach it. **The gateway itself remains loopback-only
> in v1** ([`gateway.md §3.2`](gateway.md), [`server-api.md §3.1`](server-api.md));
> nothing in this spec changes that. What this spec adds is the layered defense
> a buyer composes when they wire up a TLS terminator and an Ingress: which
> layer owns which threat, what defaults Metis ships, and where the v1
> deliberately stops.

This spec depends on:

- [`gateway.md`](gateway.md) — the loopback-only network posture this spec extends.
- [`multi-user.md`](multi-user.md) — per-key / per-user / per-team identity that
  rate limiting and abuse detection key off of.
- [`event-bus-and-trace-catalog.md`](event-bus-and-trace-catalog.md) — emits the
  rate-limit and leak-detection signals once they ship as catalog events.

---

## 1. Threat model

The gateway sits between an untrusted network and a fully-funded upstream
provider account. A leaked key is the headline risk: blanket spend authority
until detected and rotated. The v1 gateway already caps this at one layer
(daily / monthly spend caps in [`multi-user.md §5`](multi-user.md)); this spec
adds two more.

| Threat | v1 mitigation | This spec adds |
|---|---|---|
| Plaintext gateway exposed directly | Loopback-only bind (`gateway.md §3.2`) | TLS-termination posture (§2) |
| Leaked key burns daily cap in seconds | Daily/monthly cap | Per-key rate limit smooths spend pre-cap (§3) |
| Casual scrape from one bad IP | None | Per-IP rate limit independent of key (§3) |
| Leaked key spread to many machines | Daily cap (eventual) | Alert when >N distinct IPs hit the key (§5) |
| Sustained DDoS | None | **Out of scope.** Buyer fronts with WAF / CDN (§6) |

This spec does **not** make the gateway internet-safe on its own. It makes
the gateway *survivable* behind a buyer-owned perimeter.

---

## 2. TLS termination posture

The gateway terminates plaintext HTTP on loopback. TLS is a buyer-owned layer.

| Option | Where it terminates | When to pick it |
|---|---|---|
| **Caddy** (single VM) | In front of the gateway on the same host | Laptop / single-VM trials; Caddy auto-issues from Let's Encrypt and reverse-proxies to `127.0.0.1:8422`. |
| **nginx-ingress** (Kubernetes) | At the cluster edge | The shipped Helm chart's default. Ingress holds the TLS cert; the gateway Service forwards plaintext to the pod's loopback via the existing socat sidecar (§7). |
| **Cloud LB** (AWS ALB / GCP HTTPS LB / Azure App Gateway) | At the LB | Multi-region or autoscaled deployments where the buyer already has a cert provisioning workflow tied to the cloud account. |

Each option follows the same shape: the terminator owns the cert, the listener,
and the public socket; it forwards plaintext (over an authenticated network
boundary) to the gateway. The gateway never gets a certificate.

This avoids three bug classes the gateway would otherwise own: ALPN /
HTTP-2 frame parsing, cert renewal, TLS-version negotiation. All commodity
for the terminator; load-bearing for a solo-maintained codebase.

### 2.1 Required headers from the terminator

- `X-Forwarded-For` — per-IP bucket source (§3).
- `X-Forwarded-Proto` — so the gateway can refuse downgraded plaintext.
- `Authorization` / `x-api-key` — passed verbatim; terminator MUST NOT log.

The middleware reads the rightmost untrusted hop from `X-Forwarded-For` per
the `trusted_proxies` config (§3.5). When absent or unparseable, falls back
to the ASGI socket peer.

---

## 3. Rate-limit middleware

Two independent token-bucket limiters compose: a request passes only if
**both** the per-key and per-IP bucket admit it. The middleware lives at
[`apps/gateway/src/metis_gateway/middleware_ratelimit.py`](../../apps/gateway/src/metis_gateway/middleware_ratelimit.py)
and follows the pure-ASGI pattern from `middleware_versioning.py` (not
`BaseHTTPMiddleware`, which would buffer SSE response bodies).

### 3.1 Defaults

| Bucket | Capacity | Refill rate | Configurable in |
|---|---|---|---|
| Per-key | 60 tokens | 60 tokens / 60 seconds (1 req/sec sustained) | `RateLimitConfig.per_key_rpm`, or per-key override via the keystore in a future wave |
| Per-IP | 1000 tokens | 1000 tokens / 60 seconds (~17 req/sec sustained) | `RateLimitConfig.per_ip_rpm` |

Capacity equals the refill amount so the documented "RPM" is both the steady-
state ceiling and the burst budget — clients can spend a full minute's worth
of tokens at once and then must wait for refill.

### 3.2 Identification

Per-key bucket key: `SHA-256(bearer_token)` parsed from `Authorization:
Bearer …` (OpenAI shape) or `x-api-key` (Anthropic shape). The middleware
runs **before** auth — wrapping the app at the ASGI layer — but the
fingerprint is identical to the keystore's `secret_hash` field, so the
bucket id is stable and lookup-free. Requests with no bearer skip the
per-key bucket entirely; they short-circuit at 401 in the route handler.
Credential-stuffing attacks against bogus bearers still hit the per-IP
bucket.

Per-IP bucket key: the parsed client IP per §2.1. When `X-Forwarded-For`
yields an unparseable value, the middleware falls back to the ASGI peer.
Requests with no resolvable IP (rare; ASGI guarantees an HTTP peer) skip
the per-IP bucket.

### 3.3 Storage

In-process, per-bucket-key, bounded LRU (1000 entries per bucket type). A
single instance keeps all state in memory. Two-pod deployments see ~2× the
effective limit per key — acceptable in v1 since the daily cap is the
durable backstop and the limiter exists to smooth, not enforce. Redis-
backed shared state is Phase 4 (§8).

### 3.4 Response shape (HTTP 429)

When either bucket rejects the request, the middleware returns HTTP 429 with
the inbound-shape-matched envelope from `app.py`:

**OpenAI inbound (`/v1/chat/completions`):**

```json
{
  "error": {
    "code": "rate_limit_exceeded",
    "type": "rate_limit_error",
    "message": "per-key rate limit exceeded (60 rpm); retry in 3s",
    "scope": "per_key",
    "retry_after_seconds": 3
  }
}
```

**Anthropic inbound (`/v1/messages`):**

```json
{
  "error": {
    "type": "rate_limit_error",
    "message": "per-key rate limit exceeded (60 rpm); retry in 3s"
  }
}
```

Both responses set a `Retry-After: <seconds>` header (RFC 9110 §10.2.3,
integer seconds). The value is the number of whole seconds until the bucket
holds at least one token, rounded up; minimum value `1`.

Provider-shape paths (`/v1/chat/completions`, `/v1/messages`) are the only
paths the limiter applies to. `/healthz` and future Metis-owned paths are
exempt — they have their own auth posture and aren't billable.

### 3.5 Trusted proxies

`RateLimitConfig.trusted_proxies: tuple[str, ...]` lists CIDRs the
middleware treats as forwarders (and skips when parsing `X-Forwarded-For`).
Default `()`: no proxies trusted; read only the socket peer. Operators
behind nginx-ingress / Caddy set this to the controller's pod CIDR so
spoofed headers can't bypass the per-IP bucket.

### 3.6 Metrics

Reserved metric names — coordinated with `MetricsCollector` (which
already ships `metis_quota_used_ratio`, `metis_pattern_matches_total`,
etc. in `metis_core.observability`):

- `metis_ratelimit_requests_total{bucket="per_key|per_ip",result="allow|deny"}`
- `metis_ratelimit_tokens_available{bucket="per_key|per_ip",key="<id>"}` (gauge)

The middleware in this wave does **not** wire these into the prometheus
registry — `MetricsCollector` lives in `metis-core` and registering the
counters requires a follow-up wave there. v1 emits a structured WARN log
per 429 (with `bucket`, `rpm`, `retry_after`, `path`, fingerprint
prefix) so operators can still grep limit hits in the meantime. A bus
event `gateway.rate_limit_exceeded` (PSEUDONYMOUS floor) is reserved
for the same follow-up; per-request bus events for allowed traffic are
explicitly **not** planned — that volume would overwhelm the trace store.

---

## 4. Abuse protection (alert-only in v1)

Beyond rate limiting, the gateway runs lightweight outlier detection on
per-key and per-IP traffic. v1 is **alert-only**, not blocking — the
operator gets a signal; the middleware does not auto-revoke.

Two heuristics ship:

1. **Anomalous burst**: a key whose 5-minute request count exceeds 10× its
   trailing-1-hour median fires `gateway.abuse_signal`. The multiplier is
   the unit, not the absolute count.
2. **Pattern-match anomaly**: a key whose `metis_pattern_matches_total`
   1-hour window exceeds 100× the trailing daily median (suddenly hitting
   the routing cache far above baseline correlates with replay attacks)
   fires `gateway.abuse_signal`.

Both are advisory. The buyer's alerting layer (PagerDuty, Slack — Metis
ships none in v1) consumes the event stream and decides. Operator
mitigation: `metis gateway revoke-key <id>` ([`gateway.md §11.2`](gateway.md)).

Active blocking (auto-revoke on N signals / M minutes) is Wave 13+; needs a
loop with `gateway.key_revoked` to keep auto-revoke from ping-ponging oncall.

---

## 5. Gateway-key leak detection

A leaked key spreads. The signature: many distinct source IPs hitting the
same `gateway_key_id` in a short window — far more than one developer's
laptop + CI runner + maybe a phone hotspot.

### 5.1 Detection

Per-key sliding window (default 1 hour) of distinct source IPs. When the
cardinality exceeds the threshold (default 10), fire
`gateway.key_leak_suspected` once per key per window.

| Knob | Default | Notes |
|---|---|---|
| `leak_window_seconds` | 3600 | Sliding window. |
| `leak_distinct_ip_threshold` | 10 | Cardinality at which the alert fires. |
| `leak_alert_cooldown_seconds` | 3600 | Per-key suppression after firing. |

Storage: `dict[key_id, BoundedSet[ip]]` capped at 256 IPs per key (a key
past 256 distinct IPs already exceeded threshold by 25×; ~16 KB per key).

### 5.2 Response

Alert-only in v1; runbook is §4: investigate, then revoke if confirmed.
Wave 13+ candidate: soft-block mode that disables the key for a grace
period while paging operator.

### 5.3 False positives

Tolerated. The buyer is two events away from key rotation
(`metis gateway rotate-key`; predecessor stays live through the grace
period per [`gateway.md §11.3`](gateway.md)). False-positive alert: one
slack ping. Missed leak: daily cap drained before 9am.

---

## 6. DDoS posture

**Out of scope for v1.** No connection-rate limiting at the listener, no
SYN-cookie tuning, no slow-loris timeouts beyond uvicorn defaults. A
100k-request burst saturates the event loop.

This is correct: DDoS is the most commoditized perimeter problem and
buyers already pay for the answer. Recommended layering:

| Layer | Examples | Why |
|---|---|---|
| Edge CDN / WAF | Cloudflare, AWS WAF, Fastly | Volumetric / L7 attacks dropped before infra. |
| Cloud LB | AWS ALB, GCP HTTPS LB | Malformed-packet drop; listener rate-limit. |
| Ingress controller | nginx-ingress, Istio | App-level rate limiting; secondary backstop. |

The gateway's rate-limit middleware (§3) is the **last** line of defense,
not the first. It enforces per-key fairness and protects upstream spend;
it does not protect the gateway process from a flood.

---

## 7. Kubernetes integration

The Helm chart already terminates plaintext at the pod boundary via a
socat sidecar so the Service can reach the gateway's loopback. TLS
termination is the Ingress's job (already wired; off by default).

This spec adds:

- `values.yaml::rateLimit.enabled` (default `false` — opt-in until Wave 12+
  promotes it to the buyer-recommended default).
- `values.yaml::rateLimit.perKey.rpm` / `rateLimit.perIp.rpm` (forwarded as
  env vars; defaults match §3.1).
- `templates/ingress.yaml` gains commented-out Caddy / nginx-ingress
  annotations for edge-layer rate limiting in addition to the in-process
  middleware. Commented because the right annotation depends on the
  buyer's ingress controller class.

---

## 8. What v1 deliberately leaves out

| Gap | When it lands |
|---|---|
| Multi-instance enforcement (Redis-backed buckets) | Wave 13+ (Phase 4) — daily cap is the durable backstop until then |
| Active blocking on abuse signals | Wave 13+ — auto-revoke without operator has high false-positive cost |
| Soft-block on leak suspicion | Wave 13+ |
| Per-key custom RPMs from the keystore | Wave 12+ |
| Per-team / per-user rate limits | Wave 12+ — quotas exist there ([`multi-user.md §5`](multi-user.md)) but rate limits aren't wired |
| WAF-style request inspection | Never (delegated to buyer's CDN/WAF) |
| DDoS mitigation | Never (delegated to buyer's edge layer) |

The shipped v1 is "buyer adds a Caddy / nginx-ingress / cloud LB in front of
the loopback gateway; in-process per-key + per-IP buckets enforce fairness
inside the perimeter." Enough to lift the loopback-only bind **once a
terminator is in front**; not enough to expose the gateway directly.

---

## 9. References

- [`gateway.md`](gateway.md) — loopback-only posture and key lifecycle.
- [`multi-user.md`](multi-user.md) — spend quotas the rate limiter complements.
- [`server-api.md`](server-api.md) — loopback-only guarantee on the agent server.
- [`event-bus-and-trace-catalog.md`](event-bus-and-trace-catalog.md) — where
  `gateway.rate_limit_exceeded` / `abuse_signal` / `key_leak_suspected`
  payloads slot in when they ship.
