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
| `/analytics/cost`                                      | Per-model / per-time-window cost roll-up via the `metis-server` analytics surface (separate app — not exposed by the gateway image; spin up `metis serve` against the same DB to read it). |
| `/analytics/cost?group_by=gateway_key` (not yet wired) | Per-key roll-up. Tracked in [`specs/gateway.md §V`](specs/gateway.md) — direct SQL works today, see the smoke recipe above. |

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

When `/analytics/cost?group_by=gateway_key` lands (tracked in
[`specs/gateway.md §V`](specs/gateway.md)), these dimensions roll up
through the same HTTP surface that backs the savings dashboard.

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

## See also

- [`specs/gateway.md`](specs/gateway.md) — the gateway contract (inbound shapes, auth, cost attribution).
- [`specs/deployment-shape.md`](specs/deployment-shape.md) — why the gateway exists (hybrid: foot-in-the-door for the agent upgrade).
- [`specs/server-api.md`](specs/server-api.md) — the `metis serve` (agent-mode) surface; same `metis-core` substrate, different front door.
- [`KNOWN_ISSUES.md`](KNOWN_ISSUES.md) — gateway-related gaps tracked but not yet fixed.
