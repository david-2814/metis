# Gateway Hardening Specification

**Status:** v1 — shipped (Wave 13). Loopback-only constraint lifted; non-loopback bind is an explicit operator opt-in.
**Last updated:** 2026-05-15

> Documents the perimeter every buyer composes around the gateway before
> letting real internet traffic reach it. **Wave 13 lifts the loopback-only
> bind constraint** — the gateway now defaults to `127.0.0.1` (back-compat)
> but accepts `--host 0.0.0.0` once the rate-limit middleware (§3), audit
> logging ([`audit-log.md`](audit-log.md)), and TLS termination (§2) are
> in place. What this spec adds is the layered defense a buyer composes
> when they wire up a TLS terminator and an Ingress: which layer owns
> which threat, what defaults Metis ships, and where the v1 deliberately
> stops.

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

The gateway terminates plaintext HTTP on whatever interface `--host` selects.
TLS is **either** a buyer-owned sidecar (recommended) **or** an in-process
option for buyers who don't want a sidecar.

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

### 2.1 Bind posture (Wave 13)

The gateway defaults to `--host 127.0.0.1` (loopback). Pre-Wave-13 the
process silently rewrote any non-loopback host to `127.0.0.1`; that
constraint is **lifted** — the operator opts into a public bind explicitly
via `--host 0.0.0.0`. The lift comes with hardening Wave 11 shipped
([`audit-log.md`](audit-log.md), rate-limit middleware §3) plus this
wave's additions (connection-rate cap, in-process TLS, `SO_REUSEPORT`).

| Mode | Command | When to use |
|---|---|---|
| Loopback (default) | `metis gateway` | Single host, no public traffic; the original v1 default and still the safe one for laptops / CI / single-VM smoke. |
| Internet-exposed via sidecar | `metis gateway --host 0.0.0.0` behind nginx-ingress / Caddy / cloud LB | Production. The sidecar owns TLS; the gateway speaks plaintext on the pod IP. |
| Internet-exposed without sidecar | `metis gateway --host 0.0.0.0 --tls-cert … --tls-key …` | Production for buyers who don't want a sidecar; uvicorn terminates TLS in-process. Same security properties; one less moving piece in the topology. |

The hardening checklist the operator owns when binding non-loopback:

1. **TLS termination** — either in-process (§2.3) or upstream (§2.4 below).
   The gateway logs a one-time `WARN` at boot summarizing whether
   in-process TLS is on; if it's off, the operator must verify the
   upstream terminator is wired.
2. **Rate-limit middleware** — enable via `RateLimitConfig(enabled=True)`
   in code or the helm `rateLimit.enabled` value (§3).
3. **Audit logging** — `metis audit export` emits the credential
   lifecycle + quota + retention sweep subset; SIEM-ingest the JSONL/CSV
   on a schedule ([`audit-log.md §9`](audit-log.md)).

The gateway does **not** refuse a non-loopback bind without TLS or rate
limiting — the operator's call. The boot-time `WARN` is the in-process
nudge to keep the checklist honest.

### 2.2 Connection-rate hardening (Wave 13)

A leaked key or a casual scraper can saturate the event loop before the
per-key rate limit (§3) catches up. Wave 13 caps connections at the
process level:

| Knob | Default | Notes |
|---|---|---|
| `max_concurrent_connections` (CLI `--max-connections`) | 1000 | Uvicorn `limit_concurrency`. Excess connections return HTTP 503 immediately rather than queuing; right shape for a transparent proxy under a leaked-key flood. |
| `backlog` | 2048 | Listen-socket queue depth; uvicorn's default, restated as a config knob so graceful-restart tuning has one place. |
| `reuse_port` (CLI `--reuse-port`) | False | When True, the listen socket carries `SO_REUSEPORT` so two gateway processes can hold the same `(host, port)`. Enables blue-green / rolling restart at the process level. Single-process operation does not need it. |

This is in-process backstop, not the first line of defense. Volumetric
DDoS still belongs to the buyer's edge (§6).

### 2.3 In-process TLS

`metis gateway --tls-cert /path/to/cert.pem --tls-key /path/to/key.pem`
enables uvicorn's TLS termination on the bound socket. The cert must
match the public hostname clients connect to; the gateway does not
auto-issue or rotate certs (the buyer composes that with cert-manager,
ACM, or manual rotation).

| Field | Type | Notes |
|---|---|---|
| `tls_cert` | `Path | None` | PEM-encoded certificate chain. Must exist on disk; `GatewayConfigError` at startup if missing. |
| `tls_key` | `Path | None` | PEM-encoded private key. Must be set if `tls_cert` is set; the converse also holds (both-or-neither validation). |

When both are set, the boot log prints `https://…` instead of `http://…`
and the boot-time hardening WARN drops the `tls_in_process=off` flag.

### 2.4 Required headers from the upstream terminator (sidecar mode)

When a buyer composes an upstream terminator (nginx-ingress / Caddy /
cloud LB) instead of using in-process TLS, the terminator forwards
plaintext to the gateway. The terminator must set:

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

**Mostly out of scope for v1.** Wave 13 added a per-process connection
cap (§2.2 `max_concurrent_connections`, default 1000) so a flood doesn't
saturate the event loop — excess connections return HTTP 503 immediately.
That's a backstop, not a defense. No SYN-cookie tuning, no slow-loris
timeouts beyond uvicorn defaults, no per-source connection rate limiting
at the listener.

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

The shipped v1 (post-Wave-13) is "operator explicitly opts into a
non-loopback bind via `--host 0.0.0.0`, with either an upstream
terminator (Caddy / nginx-ingress / cloud LB) or in-process TLS; the
per-process connection cap, per-key + per-IP token buckets, audit log,
and key-rotation primitives are the in-process backstops." The boot-time
hardening-checklist `WARN` keeps the operator honest about what's wired
upstream. The gateway no longer refuses a public bind — it documents
what the operator is now on the hook for.

---

## 9. References

- [`gateway.md`](gateway.md) — loopback-only posture and key lifecycle.
- [`multi-user.md`](multi-user.md) — spend quotas the rate limiter complements.
- [`server-api.md`](server-api.md) — loopback-only guarantee on the agent server.
- [`event-bus-and-trace-catalog.md`](event-bus-and-trace-catalog.md) — where
  `gateway.rate_limit_exceeded` / `abuse_signal` / `key_leak_suspected`
  payloads slot in when they ship.
