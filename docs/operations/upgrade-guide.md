# Upgrade guide

How to roll a running Metis gateway to a newer image without dropping
in-flight requests, and what to verify after the upgrade lands.

Scope: the helm chart at [`infra/gateway/helm/`](../../infra/gateway/helm/).
For docker-compose, swap `helm upgrade` for `docker compose pull &&
docker compose up -d` — the schema-migration and backup recipes apply
identically.

---

## 1. Pre-upgrade checklist

1. **Pin the new image tag.** `latest` is a placeholder in the chart.
   Confirm the tag exists: `docker manifest inspect <repo>/metis-gateway:<tag>`.
2. **Capture the current helm release.** `helm history metis-gateway -n
   metis-gateway` — the revision number is what you'll pass to
   `helm rollback`.
3. **Take a trace-DB backup.** Wave 10 added `metis backup` (see
   [`event-bus-and-trace-catalog.md §7.5`](../specs/event-bus-and-trace-catalog.md);
   recipe in
   [`gateway-deployment.md` → Backup & restore](../gateway-deployment.md#backup--restore)).
   The shipped recipe wraps `VACUUM INTO`, which is WAL-safe and atomic
   against a live gateway:

   ```bash
   POD=$(kubectl -n metis-gateway get pod -o name | head -1 | sed 's|pod/||')
   kubectl -n metis-gateway exec "$POD" -c gateway -- \
       metis backup /var/lib/metis/snapshots/metis-pre-upgrade.db \
       --db-path /var/lib/metis/metis.db
   kubectl -n metis-gateway cp \
       "metis-gateway/$POD:/var/lib/metis/snapshots/metis-pre-upgrade.db" \
       ./metis-pre-upgrade.db -c gateway
   ```

   Save the deterministic metadata block (source / dest / byte count /
   schema version / event count / oldest+newest event timestamp)
   alongside the file — `metis restore` checks the schema_version line
   before a restore runs.
4. **Read the changelog for the new tag.** Breaking schema changes ship
   with a migration tool (see §3.4). As of Wave 10 none exists because
   no breaking change has shipped.

---

## 2. Upgrade recipe

Helm's `--atomic` flag rolls back automatically if any resource in the
release fails to become Ready inside the timeout. `--wait` blocks until
the rollout converges. Together they let you run the upgrade as a
single command and trust the exit code.

```bash
helm upgrade metis-gateway ./infra/gateway/helm/ \
    --namespace metis-gateway \
    --reuse-values \
    --set image.tag=<new-tag> \
    --atomic \
    --timeout 5m
```

The chart already ships zero-downtime defaults
([`infra/gateway/helm/values.yaml`](../../infra/gateway/helm/values.yaml),
[`templates/deployment.yaml`](../../infra/gateway/helm/templates/deployment.yaml)):

| Knob | Default | Effect during upgrade |
|------|---------|------------------------|
| `strategy.type` | `RollingUpdate` | Pods replaced one at a time |
| `strategy.rollingUpdate.maxSurge` | `1` | One extra pod above replicas during the roll |
| `strategy.rollingUpdate.maxUnavailable` | `0` | At least every old replica stays Ready until its replacement is Ready |
| `podDisruptionBudget.minAvailable` | `1` | Voluntary evictions (node drain, autoscaler) wait for a replacement |
| `readinessProbe` | `exec curl 127.0.0.1:8422/healthz` every 10s | New pod is not added to the Service until it returns 200 |

The gateway is per-request stateless ([`gateway.md §2`](../specs/gateway.md))
— no session manager, no tool cycles, no memory store. One request =
one HTTP call owned by one pod. No shared state crosses the boundary,
so the rolling-update guarantee is trivial.

### 2.1 Verify after upgrade

```bash
kubectl -n metis-gateway rollout status deploy/metis-gateway --timeout=2m
kubectl -n metis-gateway port-forward svc/metis-gateway 18422:8422 &
curl --fail http://127.0.0.1:18422/healthz
# Confirm schema version matches the binary + traces are still flowing:
POD=$(kubectl -n metis-gateway get pod -o name | head -1 | sed 's|pod/||')
kubectl -n metis-gateway exec "$POD" -c gateway -- sqlite3 /var/lib/metis/metis.db \
    'PRAGMA user_version; SELECT COUNT(*) FROM events
     WHERE timestamp_us > (strftime("%s","now")-300)*1000000;'
# → 1   (TRACE_SCHEMA_VERSION)
# → <n> (events in last 5min; non-zero proves the new pod is writing)
```

---

## 3. Schema-migration notes per store

Three SQLite stores: trace events, sessions, per-workspace patterns.
Contracts in
[`event-bus-and-trace-catalog.md §7.5`](../specs/event-bus-and-trace-catalog.md)
and [`pattern-store.md §16.6 / §16.14`](../specs/pattern-store.md).

### 3.1 Trace store

`TraceStore` stamps `PRAGMA user_version = TRACE_SCHEMA_VERSION` (=1
as of Wave 10) on every open
([`trace/store.py`](../../packages/metis-core/src/metis_core/trace/store.py)).
The events table is unchanged across Waves 5-10, so a pre-Wave-10 DB
upgrades trivially. Covered by
[`tests/trace/test_forward_compat.py`](../../packages/metis-core/tests/trace/test_forward_compat.py).
`metis restore` refuses backups whose `user_version` doesn't match
the binary — the breaking-change canary.

### 3.2 Session store

`SqliteSessionStore._migrate_sessions_table()` runs on every open
([`sessions/sqlite_store.py`](../../packages/metis-core/src/metis_core/sessions/sqlite_store.py)).
Wave 10 added three additive columns (`parent_session_id`,
`parent_tool_use_id`, `is_worker`) for delegation; the migration uses
`PRAGMA table_info` + `ALTER TABLE ADD COLUMN` for missing ones. The
`idx_sessions_parent` partial index is created by the migration
*after* the columns exist, so a pre-Wave-10 DB doesn't fault during
open. Forward-compat covered by
[`tests/sessions/test_sqlite_store_forward_compat.py`](../../packages/metis-core/tests/sessions/test_sqlite_store_forward_compat.py).

### 3.3 Pattern store

Workspace-local at `<workspace>/.metis/patterns.db`. Wave 10 v2 bumped
`store_meta.schema_version` from `"1"` to `"2"` and added the
`embedding_cache` table. The bump is monotonic (`WHERE value <
excluded.value`), so a v1 process opening a v2 DB never downgrades.

**Documented limitation.** A v1 `patterns.db` opened by v2 code bumps
`schema_version` to `"2"`, creates `embedding_cache`, and preserves
historical rows — but does **not** backfill the new `embedding_blob`
/ `embedding_provider` / `embedding_dim` columns on `fingerprints`.
`CREATE TABLE IF NOT EXISTS` is a no-op when the table already
exists, so the columns are silently skipped, and the next `record()`
call raises `OperationalError: no such column: embedding_provider`.
Captured in
[`tests/patterns/test_forward_compat.py`](../../packages/metis-core/tests/patterns/test_forward_compat.py)
(`test_legacy_v1_db_record_path_breaks_documented_gap`).

**Operator workaround** (until the impl grows an `ALTER TABLE`
migration): delete `<workspace>/.metis/patterns.db` before upgrade.
Slot 4 rebuilds the store from trace events as new turns land —
pattern store is a derived projection per
[`pattern-store.md §2.2`](../specs/pattern-store.md) non-goal 3.
Gateway-only deployments are unaffected (the gateway is per-request
stateless and never instantiates `PatternStore`).

### 3.4 Breaking-change policy

Every breaking schema change ships with a migration tool (`metis trace
migrate` / `metis sessions migrate` / `metis patterns migrate`) and a
release note. As of Wave 10 none exists — no breaking change has
shipped. The policy stays so the next one has a rehearsed path.

---

## 4. Rollback recipe

`--atomic` rolls a failed upgrade back automatically. For a *successful*
upgrade that later turns out to be wrong:

```bash
# 1. Roll the helm release back.
helm rollback metis-gateway -n metis-gateway --wait

# 2. If schema mismatch or corruption is suspected, restore the
#    pre-upgrade backup. Stop the writer first — VACUUM INTO snapshots
#    are crash-consistent, but restoring under an active writer is not.
kubectl -n metis-gateway scale deploy/metis-gateway --replicas=0
kubectl -n metis-gateway cp ./metis-pre-upgrade.db \
    "metis-gateway/$POD:/var/lib/metis/restore.db" -c gateway
kubectl -n metis-gateway exec "$POD" -c gateway -- \
    metis restore /var/lib/metis/restore.db \
    --db-path /var/lib/metis/metis.db --force
kubectl -n metis-gateway scale deploy/metis-gateway --replicas=1
```

`metis restore` refuses to clobber unless `--force` and refuses on
`PRAGMA user_version` mismatch. If the post-rollback binary is older
than the backup, downgrade the image first.

---

## 5. Local smoke recipe (kind)

Rehearse the upgrade against a `kind` cluster before running it for
real. Same shape as
[`gateway-deployment.md` → First production smoke](../gateway-deployment.md#first-production-smoke-kind-2026-05-15)
plus an upgrade step.

> Validated 2026-05-15 (kind v0.31.0, helm 4.2.0): v0 install Ready in
> 41s; `helm upgrade --atomic` to v1 completed in 53s with the new pod
> Running before the old pod Terminated (`maxSurge=1`/`maxUnavailable=0`
> honored). Post-upgrade `/healthz` ok, `PRAGMA user_version` = 1, 4
> events written across the upgrade window.

```bash
# 1. Cluster + two image tags pointing at the same code (the rolling
#    update is what we're testing, not a code delta).
kind create cluster --name metis-gateway-upgrade --wait 2m
docker build -t metis-gateway:v0 -f infra/gateway/Dockerfile .
docker tag metis-gateway:v0 metis-gateway:v1
kind load docker-image metis-gateway:v0 --name metis-gateway-upgrade
kind load docker-image metis-gateway:v1 --name metis-gateway-upgrade

# 2. Issue a key + install at v0 (full recipe in gateway-deployment.md).
kubectl create namespace metis-gateway
mkdir -p /tmp/metis-upgrade-smoke
uv run metis gateway issue-key \
    --keystore /tmp/metis-upgrade-smoke/keys.json \
    --name upgrade-smoke --workspace /workspace
kubectl -n metis-gateway create secret generic metis-gateway-keystore \
    --from-file=keys.json=/tmp/metis-upgrade-smoke/keys.json
helm install metis-gateway ./infra/gateway/helm/ \
    --namespace metis-gateway \
    --set image.repository=metis-gateway --set image.tag=v0 \
    --set image.pullPolicy=Never \
    --set provider.anthropicApiKey="$ANTHROPIC_API_KEY" \
    --set keystore.existingSecret=metis-gateway-keystore \
    --wait --timeout 2m

# 3. Pre-upgrade backup.
POD=$(kubectl -n metis-gateway get pod -o name | head -1 | sed 's|pod/||')
kubectl -n metis-gateway exec "$POD" -c gateway -- \
    metis backup /var/lib/metis/pre-upgrade.db --db-path /var/lib/metis/metis.db

# 4. Upgrade with --atomic. Watch in another terminal:
#    `kubectl -n metis-gateway get pods -w` shows the surge pod come up,
#    pass readiness, then the old pod terminate.
helm upgrade metis-gateway ./infra/gateway/helm/ \
    --namespace metis-gateway --reuse-values \
    --set image.tag=v1 --atomic --timeout 5m

# 5. Verify + cleanup.
kubectl -n metis-gateway port-forward svc/metis-gateway 18422:8422 &
curl --fail http://127.0.0.1:18422/healthz
helm uninstall metis-gateway -n metis-gateway
kind delete cluster --name metis-gateway-upgrade
```

If step 4 fails or the new pod never reaches Ready, `--atomic` rolls
the release back and the v0 pod stays in service. `helm history`
records both the failed upgrade and the rollback.
