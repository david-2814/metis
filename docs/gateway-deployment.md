# Gateway deployment

How to run the Metis transparent gateway from a container image. This is
the server-side counterpart to the [gateway client quickstart](#) — point
your existing OpenAI / Anthropic SDK clients at this URL, let Metis route
and meter, and read the cost attribution out of the trace DB.

The gateway is per-request stateless (no session, no tools, no memory —
see [`specs/gateway.md`](specs/gateway.md) §2). One request = one HTTP
call routed through the canonical IR to a provider adapter and back. The
container packages that loop into a slim runtime image you can deploy on
a laptop or a single VM.

> **Loopback-only bind, by design.** The gateway forces `host=127.0.0.1`
> inside the container (v1 safety guarantee per [`specs/gateway.md`](specs/gateway.md)
> §3.2 and [`specs/server-api.md`](specs/server-api.md) §3.1). Reach it
> from outside the container with `network_mode: host` (laptop / single-VM)
> or with a TLS terminator that shares the gateway's network namespace
> (production). Standard `docker run -p` port-mapping does **not** work
> against a loopback bind. See [Production checklist](#production-checklist).

---

## 5-minute quickstart

Requires Docker (Linux) or Docker Desktop ≥4.29 (macOS / Windows). On
macOS / Windows you must also **enable host networking** in Docker
Desktop → Settings → Resources → Network → "Enable host networking" —
it's a beta toggle introduced in 4.29 and is off by default. Without
it, the host can reach the gateway's loopback only from inside the
container (via `docker exec`), not from the macOS / Windows shell. On
Linux, `network_mode: host` works out of the box.

```bash
# 1. Configure your provider key(s). At least one of ANTHROPIC_API_KEY,
#    OPENAI_API_KEY, or OPENROUTER_API_KEY MUST be set — the gateway
#    refuses to start without one (it cannot reach any provider).
cp .env.example .env
$EDITOR .env   # set ANTHROPIC_API_KEY (and/or OPENAI_API_KEY / OPENROUTER_API_KEY)

# 2. Build the image and issue your first gateway key. The keystore must
#    exist before `metis gateway` (server) starts — issue-key creates it.
docker compose build gateway
mkdir -p .metis-gateway/keys .metis-gateway/data
docker compose run --rm gateway issue-key \
    --name "my-client" \
    --workspace /workspace
# → prints `token: gw_…` once. Save it now — only the hash is persisted.

# 3. Start the gateway.
docker compose up -d

# 4. Verify (Linux, or macOS/Windows with host networking enabled).
curl http://127.0.0.1:8422/healthz
# → {"status":"ok","uptime_seconds":…}
#
# If you're on macOS/Windows and host networking is not enabled:
docker compose exec gateway curl --silent http://127.0.0.1:8422/healthz
```

Send a real LLM call (requires `ANTHROPIC_API_KEY` in `.env`):

```bash
curl http://127.0.0.1:8422/v1/messages \
    -H "x-api-key: gw_…paste_the_token_from_step_2…" \
    -H "anthropic-version: 2023-06-01" \
    -H "content-type: application/json" \
    -d '{
      "model": "claude-haiku-4-5",
      "max_tokens": 128,
      "messages": [{"role": "user", "content": "say hi"}]
    }'
```

…and verify the spend was attributed to the key (until
`/analytics/cost?group_by=gateway_key` ships — tracked in
[`specs/gateway.md §V`](specs/gateway.md), the data is already on the
trace DB):

```bash
docker compose exec gateway sqlite3 /var/lib/metis/metis.db \
  "SELECT gateway_key_id, inbound_shape, ROUND(SUM(cost_usd),6) AS cost
   FROM events
   WHERE type = 'llm.call_completed'
   GROUP BY gateway_key_id, inbound_shape;"
```

---

## Reference

### Image

Built from [`infra/gateway/Dockerfile`](../infra/gateway/Dockerfile).
Multi-stage: builder runs `uv sync --frozen --no-dev` against the workspace
lockfile; runtime is `python:3.13-slim` plus the resolved venv. Non-root
user (`metis`). The image installs `curl` only as a healthcheck dependency.

| Tag                  | Contents                                                                                |
|----------------------|-----------------------------------------------------------------------------------------|
| `metis-gateway:latest` | The default tag produced by `docker compose build gateway` (or `docker build .`).     |

### Environment variables

| Variable                       | Default                                | What it does                                                                                  |
|--------------------------------|----------------------------------------|-----------------------------------------------------------------------------------------------|
| `ANTHROPIC_API_KEY`            | _(unset)_                              | Provider key used by the Anthropic adapter for outbound calls.                                |
| `OPENAI_API_KEY`               | _(unset)_                              | Provider key used by the OpenAI adapter.                                                      |
| `OPENROUTER_API_KEY`           | _(unset)_                              | Provider key used by the OpenRouter adapter.                                                  |
| `METIS_GATEWAY_HOST`           | `127.0.0.1`                            | Bind host. Non-loopback values are rewritten to `127.0.0.1` (v1 safety guarantee).            |
| `METIS_GATEWAY_PORT`           | `8422`                                 | Bind port. Matches `metis gateway --port` default.                                            |
| `METIS_GATEWAY_KEYSTORE`       | `/etc/metis/keys.json`                 | Path inside the container to the gateway keystore. Mount a host volume here to persist keys. |
| `METIS_GATEWAY_DB_PATH`        | `/var/lib/metis/metis.db`              | Path inside the container to the SQLite trace DB. Mount a host volume here to persist traces.|
| `METIS_GATEWAY_GLOBAL_DEFAULT` | `anthropic:claude-sonnet-4-6`          | Model used when routing finds no other slot win (clients passing `model` always win slot 1). |

### Port

| Port   | Direction | Purpose                                                                          |
|--------|-----------|----------------------------------------------------------------------------------|
| `8422` | inbound   | OpenAI-shape (`POST /v1/chat/completions`), Anthropic-shape (`POST /v1/messages`), `GET /healthz`. |

### Volumes

| Container path                | Purpose                                                                                        |
|-------------------------------|------------------------------------------------------------------------------------------------|
| `/workspace`                  | The workspace the issued keys are scoped to. The gateway is read-only against it.              |
| `/etc/metis/keys.json`        | Keystore. SHA-256 hashes only; plaintext tokens are never persisted. Created on first `issue-key`. |
| `/var/lib/metis/metis.db`     | Trace DB. Holds `route.decided` / `llm.call_completed` / `turn.completed` events with `gateway_key_id` + `inbound_shape` stamped on each LLM/turn payload. |

### Key management

Keys are issued by running the image with the `issue-key` first arg, which
the entrypoint dispatches to `metis gateway issue-key`:

```bash
# One-shot via compose (uses the keystore volume from docker-compose.yml).
docker compose run --rm gateway issue-key \
    --name "ci-bot" \
    --workspace /workspace \
    --allow-model anthropic:claude-haiku-4-5 \
    --daily-cap-usd 5.00
```

The plaintext `gw_…` token is printed once and cannot be recovered. The
keystore stores `{key_id, secret_hash, name, workspace_path, allowed_models?, daily_cap_usd?}`
keyed on the SHA-256 of the token.

To revoke a key, remove its entry from `./.metis-gateway/keys/keys.json`
and restart the container. There is no online revocation API in v1.

### Logs

The gateway logs to stdout/stderr via uvicorn. Tail with:

```bash
docker compose logs -f gateway
```

There is no log rotation in v1; for long-running deployments, configure
the container runtime's logging driver (`--log-driver json-file --log-opt max-size=10m`).

### Observability hooks

| Surface                                                | What it reports                                                                                    |
|--------------------------------------------------------|----------------------------------------------------------------------------------------------------|
| `GET /healthz`                                         | Liveness + uptime. Used by the container healthcheck.                                              |
| Trace DB at `/var/lib/metis/metis.db`                  | Full event stream: `route.decided`, `llm.call_started`, `llm.call_completed`, `turn.completed`. Tagged with `gateway_key_id` and `inbound_shape`. |
| `/analytics/cost`                                      | Per-model / per-time-window cost roll-up via the `metis-server` analytics surface (separate app — not exposed by the gateway image; spin up `metis serve` against the same DB to read it). Accepts `?gateway_key=<id>` to filter to one tenant. |
| `/analytics/cost?group_by=gateway_key`                 | Per-key roll-up dimension on `/analytics/cost`.                                                                            |
| `/analytics/by_key`                                    | Per-key cost / call / inbound-shape roll-up (analytics-api.md §4.8) — the dedicated buyer surface. Accepts `?gateway_key=<id>` for exact-match filter. |
| `metis serve` dashboard `Gateway keys` tab             | Visual surface over `/analytics/by_key` — sortable per-key table, top-spender callout, click-through drill-down into the Cost view. |

---

## Production checklist

The gateway image as shipped is appropriate for a single-tenant developer
laptop or a single internal VM. Production deployment is operator
responsibility; the spec deliberately stops at "loopback-bound, drop a
TLS terminator in front" so that authentication / rate-limiting / audit
remain TLS-terminator concerns rather than gateway-app concerns.

### TLS termination

Put Caddy or nginx in front of the gateway. The cleanest pattern in
Docker is a **sidecar** that shares the gateway's network namespace so
both processes see `127.0.0.1`:

```yaml
# docker-compose.prod.yml (sketch — not shipped, write to taste)
services:
  gateway:
    extends:
      file: docker-compose.yml
      service: gateway
    # Drop network_mode: host so the gateway is reachable only via the
    # sidecar. The gateway still binds 127.0.0.1 inside its namespace.
    network_mode: ""

  caddy:
    image: caddy:2
    network_mode: "service:gateway"   # share the gateway's namespace
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy-data:/data
      - caddy-config:/config
    ports:
      - "443:443"
```

With a minimal `Caddyfile`:

```
gateway.example.com {
    reverse_proxy 127.0.0.1:8422
}
```

Caddy terminates TLS on `0.0.0.0:443` and forwards plaintext to the
gateway's loopback inside the shared namespace.

### Keystore rotation

The keystore is a single JSON file. Rotation in v1 is manual:

1. `docker compose run --rm gateway issue-key --name "<new-name>" ...` — issue the replacement.
2. Distribute the new token to the client.
3. Delete the old entry from `./.metis-gateway/keys/keys.json`.
4. `docker compose restart gateway` — the gateway re-reads the keystore at startup.

There is no key TTL or scheduled rotation in v1. If you need scheduled
rotation, manage it externally (cron + the `issue-key` subcommand).

### Trace DB size management

The trace DB grows linearly with traffic. SQLite WAL mode means writes
are append-mostly. Two knobs:

1. **Periodically `VACUUM`** to reclaim space from deleted/checkpointed
   rows: `docker compose exec gateway sqlite3 /var/lib/metis/metis.db 'VACUUM;'`.
2. **Prune old events** before they bloat the DB:

   ```sql
   DELETE FROM events WHERE timestamp < '2026-04-01T00:00:00Z';
   ```

   Run inside a `BEGIN; … COMMIT;` if you want it atomic with a `VACUUM`.

The trace DB is the source of truth for cost attribution — delete only
after you've rolled the data into whatever billing system you actually
charge from.

### Cost attribution conventions

Every `llm.call_completed` and `turn.completed` event carries the
`gateway_key_id` that authorized the request and the `inbound_shape`
(`openai` or `anthropic`) the client used. Recommended tagging:

| Attribution dimension       | Where it lives                                                                            |
|-----------------------------|-------------------------------------------------------------------------------------------|
| Per-tenant / per-customer   | Issue one key per tenant. `name` on the key is your free-text label.                      |
| Per-environment (dev/staging/prod) | Issue separate keys; use the `name` to encode the env.                              |
| Per-application-feature     | Issue separate keys per feature surface. Aggregating across features is a SQL `GROUP BY`. |

These dimensions roll up through `/analytics/by_key` (one row per key,
with a per-inbound-shape sub-array) and `/analytics/cost?group_by=gateway_key`
(plain cost rows keyed by `gateway_key_id`). For a buyer-facing visual
view, point a browser at the `metis serve` dashboard's **Spend by
identity** tab — same DB, same numbers, no extra wiring. The tab
ships three rollups in one place: **Per-team** (`/analytics/by_team`,
with an expand-on-click per-user breakdown), **Per-user**
(`/analytics/cost?group_by=user`), and **Per-key** (the original
Wave-6 view, `/analytics/by_key`). Click-through filters the **Cost**
and **Activity** views to that identity via `?team=<id>` / `?user=<id>` /
`?gateway_key=<id>` — letting an operator monitor spend per tenant
(team), per developer (user), or per credential (key) with the same
chrome and no separate report tooling.

Per-team and per-user attribution requires `--user` / `--team` on
`issue-key` (multi-user.md §4.2); pre-multi-user keys roll up under
the `untagged` bucket in those tiles. The per-key tile works on every
key regardless of whether it carries identity tags.

### Non-loopback bind (deferred)

The gateway will refuse `--host 0.0.0.0` in v1 — the value is silently
rewritten to `127.0.0.1` with a warning log. This is the documented v1
safety posture (auth / rate-limiting / audit hardening lands before the
gateway accepts non-loopback). If you need an externally-reachable
listener, terminate TLS in front per the section above; do not patch
the bind check.

---

## Smoke test recipe

For a client buyer to verify the gateway end-to-end:

```bash
# 1. Build + run.
docker compose up -d gateway
sleep 2
curl --fail http://127.0.0.1:8422/healthz

# 2. Issue a key.
TOKEN=$(docker compose run --rm gateway issue-key \
    --name "smoke" --workspace /workspace \
    2>/dev/null | awk '/^token:/ {print $2}')

# 3. Hit the OpenAI shape.
curl http://127.0.0.1:8422/v1/chat/completions \
    -H "Authorization: Bearer $TOKEN" \
    -H "content-type: application/json" \
    -d '{
      "model": "claude-haiku-4-5",
      "messages": [{"role": "user", "content": "respond with the word OK"}],
      "max_tokens": 16
    }'

# 4. Confirm the spend was attributed to the key.
docker compose exec gateway sqlite3 /var/lib/metis/metis.db \
    "SELECT key_id, COUNT(*) AS calls, ROUND(SUM(cost_usd),6) AS cost_usd
     FROM (
       SELECT json_extract(payload, '\$.gateway_key_id') AS key_id,
              json_extract(payload, '\$.usage.cost_usd') AS cost_usd
       FROM events
       WHERE type = 'llm.call_completed'
     )
     GROUP BY key_id;"
```

Step 3 should return an OpenAI-shape `chat.completion` body; step 4
should report `calls=1` against the key issued in step 2.

---

## Kubernetes via helm

The Docker quickstart above is single-node by design — for buyers running
in-cluster, the chart at [`infra/gateway/helm/`](../infra/gateway/helm/)
packages the same image into a deployable bundle. The chart is single-tenant
v1 (one workspace per gateway key) and the same posture as the Docker shape:
loopback bind inside the pod, TLS termination is the buyer's responsibility.
See the [Production-readiness audit](#production-readiness-audit) below
before any non-laptop deployment.

### What the chart ships

| Resource                 | Default                                                                                 |
|--------------------------|------------------------------------------------------------------------------------------|
| `Deployment`             | 1 replica, 250m CPU / 256Mi memory requested, RollingUpdate (maxSurge 1, maxUnavailable 0). |
| `Service`                | ClusterIP on 8422, targets the proxy sidecar's `http` port.                              |
| `Deployment.proxy`       | Sidecar (`alpine/socat:1.8.0.0`) listens on 0.0.0.0:8423 inside the pod and forwards to 127.0.0.1:8422. This bridges the gateway's loopback bind (v1 safety guarantee) so the Service can reach it. |
| `Ingress`                | OFF. Enable explicitly and provide a TLS cert (cert-manager / cloud LB / sealed Secret). |
| `Secret` (providers)     | Chart-managed by default with inline keys, OR `provider.existingSecret` to consume one you manage. The chart fails install if no provider key is provided either way (the gateway refuses to start without one). |
| `ConfigMap` (keystore)   | Seeded empty `{ "keys": [] }`. **The gateway rejects an empty keystore at startup**, so the seed is for `helm template` rendering only — for a real install, issue at least one key out-of-band and pass it via `keystore.existingSecret` (recipe in [Quickstart](#quickstart)). |
| `PersistentVolumeClaim`  | 1Gi RWO for the trace DB. The cluster default StorageClass is used unless `persistence.storageClass` is set. |
| `HorizontalPodAutoscaler`| OFF. CPU-based scaling 1→3 when enabled (see caveat below on shared trace DB).         |
| `PodDisruptionBudget`    | `minAvailable: 1` so cluster autoscalers / upgrade tools wait for a replacement before evicting. |
| `NetworkPolicy`          | Deny-by-default. Ingress from any in-namespace pod by default; egress to TCP 443 to any IP (provider APIs cannot be matched by NetworkPolicy DNS-wise) and cluster DNS. |
| `ServiceAccount`         | Chart-managed, no extra RBAC. Reuse an existing one via `serviceAccount.name` + `serviceAccount.create=false`. |

### Quickstart

The gateway refuses to start with an empty `keys.json` (auth.py rejects
`{"keys": []}`), so the working order is **issue a key out-of-band, then
install**. The chart's seed ConfigMap is fine for `helm template`
rendering but is not a valid install-time keystore.

```bash
# 1. Build + push the gateway image to a registry your cluster can pull.
docker build -t your-registry.example.com/metis-gateway:0.1.0 \
    -f infra/gateway/Dockerfile .
docker push your-registry.example.com/metis-gateway:0.1.0

# 2. Create a namespace.
kubectl create namespace metis-gateway

# 3. Issue your first gateway key BEFORE the install. Save the printed
#    token — only the SHA-256 hash is persisted. Either run the CLI
#    locally against a uv workspace…
mkdir -p ./.metis-gateway
uv run metis gateway issue-key \
    --keystore ./.metis-gateway/keys.json \
    --name "my-client" --workspace /workspace
# → prints `token: gw_…` once.

#    …or use the gateway image's issue-key subcommand if you don't have
#    uv installed:
# docker run --rm -v "$PWD/.metis-gateway:/etc/metis" \
#     your-registry.example.com/metis-gateway:0.1.0 issue-key \
#         --name "my-client" --workspace /workspace

# 4. Wrap the keystore in a Secret. (A ConfigMap also works, but Secret
#    matches how keys.json is treated in production paths.)
kubectl -n metis-gateway create secret generic metis-gateway-keystore \
    --from-file=keys.json=./.metis-gateway/keys.json

# 5. Install the chart. Pin a real image tag, NOT `latest` (the chart
#    ships with `latest` as a placeholder).
helm install metis-gateway ./infra/gateway/helm/ \
    --namespace metis-gateway \
    --set image.repository=your-registry.example.com/metis-gateway \
    --set image.tag=0.1.0 \
    --set provider.anthropicApiKey="${ANTHROPIC_API_KEY}" \
    --set keystore.existingSecret=metis-gateway-keystore

# 6. Wait for the pod to come up.
kubectl -n metis-gateway wait deploy/metis-gateway --for=condition=Available --timeout=120s

# 7. Smoke-test over a port-forward.
kubectl -n metis-gateway port-forward svc/metis-gateway 8422:8422 &
curl http://127.0.0.1:8422/healthz
```

To rotate or add keys later, regenerate `keys.json` with
`metis gateway issue-key`, recreate the Secret (`kubectl create secret
... --dry-run=client -o yaml | kubectl apply -f -`), and
`kubectl rollout restart deploy/metis-gateway`. See
[Keystore rotation without a restart](#keystore-rotation-without-a-restart).

### Common values.yaml overrides

**Private registry with pull secrets:**

```yaml
image:
  repository: ghcr.io/your-org/metis-gateway
  tag: "0.1.0"
  pullSecrets:
    - name: ghcr-pull-secret
```

**TLS via nginx-ingress + cert-manager:**

```yaml
ingress:
  enabled: true
  className: nginx
  annotations:
    cert-manager.io/cluster-issuer: letsencrypt-prod
    nginx.ingress.kubernetes.io/proxy-body-size: 10m
  hosts:
    - host: gateway.example.com
      paths:
        - path: /
          pathType: Prefix
  tls:
    - secretName: metis-gateway-tls
      hosts:
        - gateway.example.com
```

**Provider keys from External Secrets Operator:**

```yaml
provider:
  existingSecret: metis-gateway-providers
# (out-of-band) create an ExternalSecret that materializes a Secret
# named metis-gateway-providers with keys ANTHROPIC_API_KEY / etc.
```

**Keystore from a sealed-Secret bundle:**

```yaml
keystore:
  existingSecret: metis-gateway-keystore
# Secret must have a key named "keys.json" whose value is the keystore JSON.
# kubectl create secret generic metis-gateway-keystore \
#     --from-file=keys.json=./keys.json
```

**LoadBalancer (only with TLS in front):**

```yaml
service:
  type: LoadBalancer
  annotations:
    # AWS NLB with ACM cert + TLS termination at LB:
    service.beta.kubernetes.io/aws-load-balancer-type: nlb
    service.beta.kubernetes.io/aws-load-balancer-ssl-cert: arn:aws:acm:...
    service.beta.kubernetes.io/aws-load-balancer-ssl-ports: "8422"
    service.beta.kubernetes.io/aws-load-balancer-backend-protocol: tcp
```

**Tightening NetworkPolicy ingress to a specific client namespace:**

```yaml
networkPolicy:
  ingressFromSelector:
    matchLabels:
      app.kubernetes.io/name: my-client-app
```

### Validation

The chart was validated with helm 4.2.0:

```bash
helm lint infra/gateway/helm/
# → 1 chart(s) linted, 0 chart(s) failed

helm template test infra/gateway/helm/ \
    --set provider.anthropicApiKey=sk-ant-stub
# → 8 manifests rendered (NetworkPolicy / PDB / ServiceAccount / Secret /
#   ConfigMap / PVC / Service / Deployment)

helm template test infra/gateway/helm/ \
    --set provider.anthropicApiKey=sk-ant-stub \
    --set keystore.existingSecret=metis-gateway-keystore
# → 7 manifests rendered (the seed ConfigMap drops out when a Secret
#   keystore is supplied)
```

Do this before merging chart changes. End-to-end install validation is
captured in [First production smoke](#first-production-smoke-kind-2026-05-15)
below.

### First production smoke (kind, 2026-05-15)

The chart was deployed end-to-end against a `kind` 0.31.0 cluster
(`kindest/node:v1.35.0`) on macOS / Docker Desktop 29.2.0, using helm
4.2.0 and kubectl v1.34.1. Cluster spinup → first 200 OK on `/healthz`
took ~3 minutes after the first image build (Docker layer cache cold).
Full transcript:

```bash
# 1. Create the cluster + load the locally-built image.
kind create cluster --name metis-gateway-smoke --wait 2m
docker build -t metis-gateway:dev -f infra/gateway/Dockerfile .
kind load docker-image metis-gateway:dev --name metis-gateway-smoke

# 2. Issue a key out-of-band, wrap it in a Secret.
kubectl create namespace metis-gateway
mkdir -p /tmp/metis-gateway-smoke
uv run metis gateway issue-key \
    --keystore /tmp/metis-gateway-smoke/keys.json \
    --name "smoke-client" --workspace /workspace \
  | grep -E "^(key_id|token):" > /tmp/metis-gateway-smoke/issue.out
TOKEN=$(awk '/^token:/ {print $2}' /tmp/metis-gateway-smoke/issue.out)
kubectl -n metis-gateway create secret generic metis-gateway-keystore \
    --from-file=keys.json=/tmp/metis-gateway-smoke/keys.json

# 3. Install with dev overrides.
helm install metis-gateway ./infra/gateway/helm/ \
    --namespace metis-gateway \
    --set image.repository=metis-gateway \
    --set image.tag=dev \
    --set image.pullPolicy=Never \
    --set provider.anthropicApiKey="$ANTHROPIC_API_KEY" \
    --set keystore.existingSecret=metis-gateway-keystore

# 4. Wait + port-forward.
kubectl -n metis-gateway wait deploy/metis-gateway \
    --for=condition=Available --timeout=120s
kubectl -n metis-gateway port-forward svc/metis-gateway 18422:8422 &
curl --silent http://127.0.0.1:18422/healthz
# → {"status":"ok","uptime_seconds":…}

# 5. Real-API smoke: 4 calls (OpenAI sync + SSE, Anthropic sync + SSE)
#    against the canonical haiku id so routing slot 1 actually wins
#    (see "Bare model names route to global_default" pitfall below).
for shape in chat messages; do
  for stream in false true; do
    case "$shape:$stream" in
      chat:false)
        curl -sf http://127.0.0.1:18422/v1/chat/completions \
          -H "Authorization: Bearer $TOKEN" -H "content-type: application/json" \
          -d '{"model":"anthropic:claude-haiku-4-5","max_tokens":16,
               "messages":[{"role":"user","content":"respond with OK"}]}' ;;
      chat:true)
        curl -sNf http://127.0.0.1:18422/v1/chat/completions \
          -H "Authorization: Bearer $TOKEN" -H "content-type: application/json" \
          -d '{"model":"anthropic:claude-haiku-4-5","max_tokens":16,"stream":true,
               "messages":[{"role":"user","content":"respond with OK"}]}' ;;
      messages:false)
        curl -sf http://127.0.0.1:18422/v1/messages \
          -H "x-api-key: $TOKEN" -H "anthropic-version: 2023-06-01" \
          -H "content-type: application/json" \
          -d '{"model":"anthropic:claude-haiku-4-5","max_tokens":16,
               "messages":[{"role":"user","content":"respond with OK"}]}' ;;
      messages:true)
        curl -sNf http://127.0.0.1:18422/v1/messages \
          -H "x-api-key: $TOKEN" -H "anthropic-version: 2023-06-01" \
          -H "content-type: application/json" \
          -d '{"model":"anthropic:claude-haiku-4-5","max_tokens":16,"stream":true,
               "messages":[{"role":"user","content":"respond with OK"}]}' ;;
    esac
    echo
  done
done

# 6. Per-key spend rollup (the gateway image does not expose /analytics/*;
#    point `metis serve` at a VACUUM INTO snapshot of the same trace DB).
POD=$(kubectl -n metis-gateway get pod -o name | head -1 | sed 's|pod/||')
kubectl -n metis-gateway exec $POD -c gateway -- \
    python3 -c "import sqlite3; con=sqlite3.connect('/var/lib/metis/metis.db');
con.execute('PRAGMA wal_checkpoint(TRUNCATE)');
con.execute('VACUUM INTO \"/tmp/snapshot.db\"')"
kubectl -n metis-gateway cp metis-gateway/$POD:/tmp/snapshot.db \
    /tmp/metis-gateway-smoke/metis.db -c gateway
uv run metis serve /tmp/metis-gateway-smoke \
    --port 18430 --db-path /tmp/metis-gateway-smoke/metis.db &
sleep 3
curl -sf http://127.0.0.1:18430/analytics/by_key | python3 -m json.tool
```

Measured outcome:

- 4 haiku calls spent **$0.00012** total ($3e-5 each) at pricing
  version `2026-05-08+openrouter-e7aa08510daa`.
- `/analytics/by_key` returned a single row keyed on the issued
  `gateway_key_id`, with a `by_inbound_shape` sub-array showing 2 calls
  per shape, matching the wire mix.
- All 6 events fired on every call: `route.decided`,
  `llm.call_started`, `llm.call_completed`, `turn.completed`, plus
  `bus.subscriber_registered` at startup. `gateway_key_id` and
  `inbound_shape` were stamped on every `llm.call_completed` and
  `turn.completed` payload.

Two chart changes landed during this validation:

- **Dockerfile uid pinned to 1000.** The image previously created the
  `metis` user with `useradd --system` (dynamic uid, observed as 999
  in 3.13-slim). The chart's default `runAsUser: 1000` then could not
  read the `/etc/metis/keys.json` mount because `/etc/metis` is mode
  0750 owned by uid 999. Pinning the image uid/gid to 1000 in
  [`infra/gateway/Dockerfile`](../infra/gateway/Dockerfile) keeps the
  image and the chart's documented default in sync.
- **NOTES.txt + Quickstart reordered.** Both used to recommend
  `kubectl exec deploy/metis-gateway -c gateway -- metis gateway
  issue-key …` *after* `helm install`. The pod will not reach Ready
  with the seed `{"keys": []}` keystore (the gateway rejects an empty
  keys array at startup), so the exec recipe is unreachable. The flow
  is now: issue out-of-band → Secret → install.

### Pitfalls a buyer will hit

These are the rough edges to expect when doing the install yourself.
None of them require source changes; they're a function of v1
gateway semantics and chart defaults.

| Pitfall                                                             | What happens                                                                                                                                                                                                                                              | Workaround                                                                                                                                                                                                              |
|---------------------------------------------------------------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| **Empty seed keystore blocks first start**                          | `CrashLoopBackOff` with `keystore must contain a non-empty 'keys' array`. The chart's default ConfigMap is `{"keys": []}`, and the gateway refuses to start on it.                                                                                        | Issue at least one key out-of-band (`uv run metis gateway issue-key …` or `docker run … issue-key`), wrap it in a Secret, install with `--set keystore.existingSecret=metis-gateway-keystore`.                          |
| **Bare model names route to global_default**                        | A client sending `model: "claude-haiku-4-5"` (the public Anthropic name) lands in slot 7 (`global_default = anthropic:claude-sonnet-4-6`). The response body still echoes `claude-haiku-4-5` because translators echo the client's `requested_model`, so the discrepancy is invisible client-side — but the upstream call (and the billed cost) is sonnet. | Send the canonical id (`anthropic:claude-haiku-4-5`) or one of the gateway-side aliases (`haiku`, `fast`, `sonnet`, `balanced`, `opus`, `deep`, `gpt5`, `mini`) — both win routing slot 1. Or set a `workspace_default` in `.metis/routing.yaml` to anchor the chosen model when bare names are used. |
| **`/analytics/*` is not on the gateway image**                      | `curl http://gateway/analytics/by_key` → 404. The gateway is per-request stateless (gateway.md §2) and does not host the analytics surface.                                                                                                              | Spin up `metis serve` against the same DB to expose `/analytics/cost` / `/analytics/by_key`. Easiest: `kubectl exec … 'PRAGMA wal_checkpoint(TRUNCATE); VACUUM INTO /tmp/snapshot.db'`, `kubectl cp` it out, `metis serve --db-path` against the snapshot. |
| **Raw `kubectl cp` of `metis.db` returns stale data**               | If you `kubectl cp` the trace DB while the gateway is taking traffic, SQLite WAL writes that haven't been checkpointed yet stay in `metis.db-wal`. The copied `.db` shows older events than the live one.                                                | Force a checkpoint and snapshot first: `kubectl exec … python3 -c "import sqlite3; sqlite3.connect('/var/lib/metis/metis.db').execute('PRAGMA wal_checkpoint(TRUNCATE)')"` then `VACUUM INTO /tmp/snapshot.db`, then `kubectl cp`. |
| **PVC `ReadWriteOnce` blocks horizontal scaling**                   | The default PVC is `ReadWriteOnce`. Setting `replicaCount > 1` or enabling `autoscaling` will get a second replica stuck `Pending` ("volume already attached to a node").                                                                                | Stay at `replicaCount: 1` (the documented v1 shape) or switch the storage class to one that supports `ReadWriteMany` and accept SQLite-on-network-FS caveats. Better long-term fix is to externalize the trace DB, tracked under [Observability](#observability). |
| **NetworkPolicy is silently ignored on plain kind**                 | kind's default CNI (kindnet) does not enforce `NetworkPolicy` egress. The chart's deny-by-default policy renders but has no effect; calls still reach Anthropic.                                                                                          | Test the NetworkPolicy on a cluster with Calico / Cilium / Antrea. On kind, install Calico (`kubectl apply -f …`) before relying on the policy. The egress rule itself is correct (TCP 443 to any IP plus DNS to kube-system). |

### Cleanup

```bash
# Tear down the helm release + namespace.
helm uninstall metis-gateway --namespace metis-gateway
kubectl delete namespace metis-gateway

# Drop the kind cluster.
kind delete cluster --name metis-gateway-smoke

# Local files.
rm -rf /tmp/metis-gateway-smoke ./.metis-gateway
```

### The loopback-bind tax in Kubernetes

The gateway forces `host=127.0.0.1` (v1 safety guarantee per
[`specs/server-api.md`](specs/server-api.md) §3.1). In Kubernetes that
means **a Service cannot route to the gateway directly** — `targetPort`
hits the pod IP, which the gateway does not listen on. Two consequences
the chart bridges automatically:

1. **A `socat` sidecar runs in the gateway pod by default**, listening on
   `0.0.0.0:8423` and forwarding to `127.0.0.1:8422`. Sidecars in a pod
   share the network namespace, so the proxy's loopback is the gateway's
   loopback. The Service targets the sidecar's port. To swap socat for
   Caddy / nginx (e.g. to add TLS at the pod boundary), override
   `proxy.image`, `proxy.command`, `proxy.args` and mount the config via
   `extraVolumes` / `extraVolumeMounts`.
2. **Liveness / readiness probes use `exec curl 127.0.0.1`**, not HTTP
   probes against the pod IP — kubelet HTTP probes run from the node's
   network namespace and cannot reach the gateway's loopback. The image
   already includes curl for the Docker healthcheck.

If you turn off the socat sidecar (`proxy.enabled=false`) without
providing your own proxy via `extraContainers`-style customization, the
Service will not reach the gateway. The probes will keep working.

### Failure modes worth knowing

| Symptom                                                              | Cause / fix                                                                              |
|----------------------------------------------------------------------|------------------------------------------------------------------------------------------|
| `helm install` fails with "set provider.existingSecret OR at least one of provider.\*ApiKey" | None of the three inline provider keys is set AND no existing Secret is referenced. The gateway refuses to start without a key (runtime.py:84), so the chart fails install early. |
| Pod stuck `CrashLoopBackOff` with "gateway keystore not found"      | `keystore.existingSecret` is set but the Secret doesn't have a key named `keys.json`. Recreate with `--from-file=keys.json=./keys.json`. |
| Pod stuck `CrashLoopBackOff` with `keystore must contain a non-empty 'keys' array` | The chart's seed keystore is `{ "keys": [] }`, and the gateway rejects an empty array at startup. Issue a key out-of-band, wrap it in a Secret, and reinstall with `--set keystore.existingSecret=…` — see the [Quickstart](#quickstart). |
| Service connects but every request returns 401 from gateway          | The keystore Secret you bundled doesn't have an entry matching the bearer token the client is sending. Re-issue the key against the same keystore file you bundled, recreate the Secret (`--dry-run=client -o yaml \| kubectl apply -f -`), and `kubectl rollout restart deploy/metis-gateway`. |
| Port collision: gateway pod stuck `Error`, "address already in use" | You set `proxy.listenPort` equal to `gatewayPort`. The proxy binds `0.0.0.0:listenPort` and the gateway binds `127.0.0.1:gatewayPort` in the same pod network namespace — a wildcard bind claims every interface. Keep the two ports different (defaults are 8423 / 8422). |
| NetworkPolicy blocks all egress, including provider APIs              | Your cluster CNI does not enforce NetworkPolicy egress (some default to ingress-only). Verify with `kubectl describe networkpolicy metis-gateway`; if your CNI ignores egress rules, the NetworkPolicy is advisory. Calico / Cilium enforce both. |

---

## Production-readiness audit

The single-tenant gateway plus this helm chart is appropriate for: one
buyer running their own devs through the gateway, one internal team with
trusted-network access to the cluster, or a pre-pilot deployment that
attributes cost back to a known set of gateway keys you issue manually.

It is **not yet** appropriate for: shared SaaS multi-tenancy, exposed
public ingress without a TLS terminator, team-level cost rollups, or any
deployment where a key compromise must trigger automated rotation.

The list below catalogs what the chart inherits from gateway v1, what
needs to be the operator's responsibility, and what's tracked for future
spec work.

### TLS termination

**What the gateway provides:** plaintext HTTP on a loopback bind inside
the pod (and pod-IP via the socat sidecar in the cluster network).

**What the operator must add:** TLS termination in front of the gateway.
The chart does not ship a TLS terminator because the choice is
deployment-shape-specific. Three good options:

1. **Ingress controller + cert-manager** — `ingress.enabled=true` with a
   `cert-manager.io/cluster-issuer` annotation. nginx-ingress, Traefik,
   and the AWS load-balancer controller all work; the chart's Ingress
   resource is shape-compatible.
2. **Cloud L7 LB with managed cert** — `service.type=LoadBalancer` with
   the cloud provider's TLS annotations (AWS NLB + ACM, GCP cloud-LB +
   Google-managed cert, Azure App Gateway). The LB terminates TLS and
   forwards plaintext to the Service.
3. **Caddy or nginx sidecar inside the gateway pod** — replace the socat
   sidecar via `proxy.image` + `proxy.args` + a mounted config file.
   Lowest blast radius; the TLS terminator and the gateway share a
   network namespace and the gateway cannot be reached by skipping the
   sidecar. Pattern documented under [Production checklist](#production-checklist)
   for the Docker shape.

Do not point untrusted clients at a plaintext Service. The gateway has
no auth on the wire other than the `Authorization: Bearer gw_…` header,
and the bearer token is transmitted in plaintext if TLS is missing.

### Observability

**What's shipped:**

- `GET /healthz` — used by the chart's liveness / readiness probes and
  by the Docker healthcheck.
- Trace DB at `/var/lib/metis/metis.db` — `route.decided`,
  `llm.call_started`, `llm.call_completed`, `turn.completed` events
  tagged with `gateway_key_id` and `inbound_shape`. Persisted on the
  PVC (or pod-ephemeral if `persistence.enabled=false`).
- `stdout` / `stderr` from uvicorn — pick up with your cluster's log
  aggregator (Loki, CloudWatch, Stackdriver, …) via the kubelet's
  container log driver.

**What's missing (Phase 3 work, flagged here):**

- **No `/metrics` Prometheus endpoint.** The gateway does not expose a
  metrics surface; rate / latency / error-rate dashboards have to read
  from the trace DB or be inferred from logs. Spinning up `metis serve`
  against the same DB exposes the `/analytics/cost` HTTP surface, but
  that's a separate app — not the gateway. A native `/metrics`
  endpoint is tracked as Phase-3 work in
  [`specs/gateway.md`](specs/gateway.md). Workaround: run a
  `sidecar` Prometheus exporter that reads the trace DB on a schedule.
- **No per-key `/analytics/cost?group_by=gateway_key`.** Per the
  AGENTS.md status, the data is on the trace DB and direct SQL works
  (recipe under [5-minute quickstart](#5-minute-quickstart)), but the
  HTTP surface for the rollup hasn't shipped. Until it does, dashboards
  read SQL.
- **Trace DB sizing.** The chart's default PVC is 1 GiB, sized for weeks
  of single-tenant developer traffic. Plan for ~5–20 MiB / 1k requests
  depending on tool-use density. Prune per the recipe in
  [Trace DB size management](#trace-db-size-management); the chart does
  not schedule a prune CronJob (intentional — owner of the data is the
  buyer, not the chart).

### Keystore rotation without a restart

**Today's behavior.** The gateway reads `keys.json` at startup. There is
no live-reload watcher. A rotation needs:

1. `metis gateway issue-key …` to produce the new key entry.
2. The updated `keys.json` deployed back into the source of truth
   (ConfigMap or `existingSecret`).
3. A pod restart so the gateway re-reads it.

**With the helm chart:**

```bash
# Issue the new key inside a running pod.
kubectl -n metis-gateway exec deploy/metis-gateway -c gateway -- \
    metis gateway issue-key --keystore /tmp/keys.json \
        --name "client-v2" --workspace /workspace
# Copy out the resulting keys.json (the chart's mounted keystore is
# ConfigMap-backed and read-only inside the pod by default — issue keys
# against a writable path like /tmp, then re-bundle).
kubectl -n metis-gateway cp deploy/metis-gateway:tmp/keys.json ./keys.json -c gateway

# Roll the new keystore into the chart-managed Secret.
kubectl -n metis-gateway create secret generic metis-gateway-keystore \
    --from-file=keys.json=./keys.json \
    --dry-run=client -o yaml | kubectl apply -f -

# Tell the chart to mount the Secret instead of the seed ConfigMap.
helm upgrade metis-gateway ./infra/gateway/helm/ \
    --namespace metis-gateway --reuse-values \
    --set keystore.existingSecret=metis-gateway-keystore

# Roll the pods so the new keystore is read.
kubectl -n metis-gateway rollout restart deploy/metis-gateway
```

**Caveat.** During the rollout there's a brief window (typically
seconds) where old pods accept the old key and new pods accept the new
key. To revoke a compromised key cleanly: remove the old entry from
`keys.json` first, roll, then add the new entry and roll again. The
chart uses `maxUnavailable: 0` / `maxSurge: 1` so there's always at
least one Ready pod throughout.

**Future spec work.** Live keystore reload (file-watch or HTTP control
plane) is not specified yet. Tracked against the multi-user follow-on
in [`specs/multi-user.md`](specs/multi-user.md), which will define how
key issuance and revocation work in a team / SaaS context.

### Multi-tenant safety

The gateway v1 is **single-tenant in shape**: one gateway key maps to
one workspace, and "tenancy" is whatever convention the operator
encodes in the key name (`provider.existingSecret` is one Secret for
the whole deployment; provider API keys are not per-tenant).

**What's safe today:**

- **Per-key cost attribution.** Every `llm.call_completed` and
  `turn.completed` event carries `gateway_key_id`. You can roll up cost
  per tenant by issuing one key per tenant and aggregating via SQL
  (recipe in [5-minute quickstart](#5-minute-quickstart)).
- **Provider-key isolation from clients.** Clients send the gateway's
  `gw_…` token, not the upstream provider's API key. Rotating the
  provider key requires only updating the chart-managed Secret and
  rolling pods; clients see no change.
- **Per-key scoping.** `metis gateway issue-key` supports
  `--allow-model` and `--daily-cap-usd`, both enforced inside the
  gateway runtime. A key with `--allow-model anthropic:claude-haiku-4-5`
  cannot route to `claude-opus-4-7` even if the client requests it.

**What's NOT safe today (operator must compensate or wait for
multi-user.md):**

- **No team / tenant rollups in the analytics surface.** The
  per-`gateway_key` HTTP rollup is unshipped (see
  [Observability](#observability) above). For now, attribution is per
  individual key — group by key naming convention in SQL.
- **No multi-workspace per key.** A key is locked to one workspace at
  issuance time (`gateway.md §11`). Teams that route across workspaces
  need multiple keys.
- **No RBAC / role-scoping at the gateway.** Every authenticated key
  has equal authority within its workspace. Differential access
  (read-only / pro-tier / restricted-model) beyond `--allow-model`
  needs the auth model from `multi-user.md`.
- **No automatic revocation on compromise.** Key revocation is manual
  and requires a pod roll (see
  [Keystore rotation without a restart](#keystore-rotation-without-a-restart)).
- **No tenant-scoped rate limiting.** The gateway has no built-in rate
  limiter; you can add one at the TLS terminator (nginx
  `limit_req_zone`, Caddy `rate_limit` plugin) but it won't be per-key
  unless the terminator parses the bearer token.

**Phase-3+ work.** The multi-user upgrade path — team-level secrets,
RBAC on gateway keys, per-tenant analytics rollups, live keystore
rotation — is being drafted in parallel as
[`specs/multi-user.md`](specs/multi-user.md). The chart's parameterization
(`provider.existingSecret`, `keystore.existingSecret`, the optional
sidecar slot) is deliberately shaped so the same chart can adopt those
features without breaking existing deployments. Once multi-user.md
lands, expect new chart values for `team:` / `tenant:` blocks and a
chart-managed CRD or controller for key lifecycle. Until then, this
chart targets the single-tenant shipping shape.

---

## See also

- [`specs/gateway.md`](specs/gateway.md) — the gateway contract (inbound shapes, auth, cost attribution).
- [`specs/deployment-shape.md`](specs/deployment-shape.md) — why the gateway exists (hybrid: foot-in-the-door for the agent upgrade).
- [`specs/server-api.md`](specs/server-api.md) — the `metis serve` (agent-mode) surface; same `metis-core` substrate, different front door.
- [`KNOWN_ISSUES.md`](KNOWN_ISSUES.md) — gateway-related gaps tracked but not yet fixed.
