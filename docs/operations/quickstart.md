# Quickstart: helm install → first savings number in &lt;1 hour

> Target audience: a buyer evaluating Metis. You have 60 minutes, a docker
> daemon, and one Anthropic API key. By the end, you have a per-key cost
> number on a real workload through the gateway.

This is the smoothest path. For depth, jump to
[`gateway-deployment.md`](../gateway-deployment.md) (helm reference) or
[`customer-trial-recipe.md`](../customer-trial-recipe.md) (your-workload
trial). For the savings story behind the demo, see
[`savings-demo.md`](../savings-demo.md).

## What you get

1. A local kind cluster with the gateway helm-installed and Ready.
2. A gateway-issued bearer token (`gw_…`).
3. A pre-baked workload run through the gateway, with a buyer-facing
   `actual / baseline / savings_pct` block printed.
4. Per-key cost rolled up via `/analytics/by_key`.

**Honest framing.** The savings number you get is per-key cost vs a
counterfactual baseline (default sonnet) re-priced from the same trace
under the canonical `PriceTable` — this answers "what would this turn
have cost on the bigger model?" The **cost-per-quality** column is
populated only when the workload opts into `evaluate.rubric: hybrid`
(the trial workload does); to compare quality across models on **your**
workload, follow [`customer-trial-recipe.md`](../customer-trial-recipe.md)
Path B.

## Prereqs

- Docker (Linux) or Docker Desktop ≥ 4.29 (macOS / Windows).
- [kind](https://kind.sigs.k8s.io/) ≥ 0.31, [helm](https://helm.sh/) ≥ 3.14, kubectl, [uv](https://docs.astral.sh/uv/) ≥ 0.4.
- An Anthropic API key. Drop it in `.env` at the repo root or export it.

```bash
echo "ANTHROPIC_API_KEY=sk-ant-..." > .env
```

## 1. helm install (5 minutes)

The convenience script automates: kind cluster create, image build, image
load, key issuance, Secret wrap, helm install, port-forward.

```bash
infra/gateway/scripts/quickstart.sh
```

Output ends with:

```
==> Trial gateway ready
    gateway URL:  http://127.0.0.1:18422
    gateway key:  gw_01HXY...
    healthz:      curl http://127.0.0.1:18422/healthz
```

The state (token, port, cluster name) is captured under `.metis-trial/`
so subsequent commands can read it without re-typing.

## 2. Issue an additional key (30 seconds, optional)

The script issued one key already. To add another (e.g. for a per-team
breakout), run:

```bash
uv run metis gateway issue-key \
    --keystore .metis-trial/keys.json \
    --name "alice-laptop" --workspace "$PWD" \
    --user alice --team eng
```

Then re-create the Secret and roll the deployment so the gateway picks
up the new entry — gateway v1 reads `keys.json` at startup; live reload
is tracked under
[`gateway-deployment.md "Keystore rotation without a restart"`](../gateway-deployment.md#keystore-rotation-without-a-restart).

## 3. Flip the client pointer (30 seconds)

Either point an existing SDK client at the gateway or skip ahead to step 4
to use the pre-baked workload. **curl** smoke:

```bash
TOKEN="gw_..."   # from step 1
curl http://127.0.0.1:18422/v1/messages \
    -H "x-api-key: $TOKEN" \
    -H "anthropic-version: 2023-06-01" \
    -H "content-type: application/json" \
    -d '{"model":"anthropic:claude-haiku-4-5","max_tokens":32,
         "messages":[{"role":"user","content":"respond with OK"}]}'
```

**anthropic-python:**

```python
import anthropic
client = anthropic.Anthropic(
    base_url="http://127.0.0.1:18422",
    api_key="gw_...",
)
client.messages.create(
    model="anthropic:claude-haiku-4-5",
    max_tokens=32,
    messages=[{"role": "user", "content": "respond with OK"}],
)
```

Full Claude Code / Cursor / OpenAI-SDK matrix:
[`gateway-client-quickstart.md`](../gateway-client-quickstart.md).

## 4. Run the pre-baked workload (5 minutes)

The trial workload is a small refactoring task (extract a duplicated
helper from `prices.py`) under `benchmarks/workloads-trial/`. Runs in
&lt; 2 minutes against haiku, costs &lt; $0.10.

```bash
# Read the state the script wrote in step 1.
source .metis-trial/state.env

uv run metis trial \
    --gateway-url "$METIS_TRIAL_GATEWAY_URL" \
    --gateway-key "$METIS_TRIAL_GATEWAY_KEY"
```

Final block:

```
=== Trial result ===
workload:               refactor-extract-helper
actual model:           anthropic:claude-haiku-4-5
baseline model:         anthropic:claude-sonnet-4-6
turns / llm / tool:     3 / 5 / 6
actual cost (USD):      0.024981
baseline cost (USD):    0.099842
savings (USD):          0.074861
savings_pct:            74.9%
quality:                0.95@0.80
cost-per-quality (USD): 0.026296
```

(Numbers are illustrative — actual values vary with model output. Quality
is the workload-level hybrid-judge verdict; "@0.80" is judge confidence.)

## 5. Per-key dashboard view (30 seconds)

`/analytics/*` lives on `metis-server`, not the gateway. Snapshot the
gateway's trace DB and point `metis serve` at the snapshot:

```bash
POD=$(kubectl -n "$METIS_TRIAL_NAMESPACE" get pod \
    -l app.kubernetes.io/name=metis-gateway -o jsonpath='{.items[0].metadata.name}')
mkdir -p .metis-trial/snapshot
kubectl -n "$METIS_TRIAL_NAMESPACE" exec "$POD" -c gateway -- \
    python3 -c "import sqlite3; c=sqlite3.connect('/var/lib/metis/metis.db'); \
c.execute('PRAGMA wal_checkpoint(TRUNCATE)'); c.execute('VACUUM INTO \"/tmp/snap.db\"')"
kubectl -n "$METIS_TRIAL_NAMESPACE" cp "$POD:/tmp/snap.db" \
    .metis-trial/snapshot/metis.db -c gateway

uv run metis serve "$PWD" --port 18421 --db-path .metis-trial/snapshot/metis.db &
sleep 3
curl -s 'http://127.0.0.1:18421/analytics/by_key' | python3 -m json.tool
```

The result is one row per `gateway_key_id` with cost, token counts, and
a `by_inbound_shape` sub-array. To re-sample after more traffic, re-run
the snapshot block; analytics-api reads whatever DB you point it at.

## 6. Tear down

```bash
infra/gateway/scripts/tear-down.sh
```

Stops the port-forward, uninstalls the helm release, drops the namespace,
deletes the kind cluster, and clears `.metis-trial/`. Idempotent.

## Pitfalls

These are the rough edges we hit during validation. None require source
changes.

| Pitfall | What happens | Workaround |
|---|---|---|
| `quickstart.sh` fails with "ANTHROPIC_API_KEY not set" | Script reads `.env` only if the var is unset in env, but `.env` is missing or doesn't define it. | `export ANTHROPIC_API_KEY=…` or add the line to `.env`. |
| `kind load docker-image` fails because the image isn't in the local docker daemon | First run after a fresh `docker system prune` — the build step may have used a different daemon (e.g. via remote builder). | Re-run `quickstart.sh`; the build step is idempotent and rebuilds locally. |
| `metis trial --gateway-url …` returns 404 from the gateway on the first call | Service / port-forward isn't fully up yet (the script does up to 5 retries on `/healthz`, but the kubelet readiness check has its own grace period). | `curl http://127.0.0.1:18422/healthz` until 200, then retry. |
| Trial reports `actual_repriced_usd: 0.0` and `savings_pct: 0.0` | The trial DB had no `llm.call_completed` events in the run window — usually means the upstream provider rejected every call. | Check `/tmp/metis.log` for adapter errors; verify `ANTHROPIC_API_KEY` is valid. |
| Trial reports a number but `/analytics/by_key` returns `[]` | You snapshotted the gateway's DB *before* the trial ran, or the gateway was bypassed (no `--gateway-url`). | Re-snapshot after the trial, and confirm the trial command was passed `--gateway-url` + `--gateway-key`. |
| `port-forward` dies between commands | kubectl port-forward exits if the pod restarts. | Re-run `quickstart.sh` — it stops any prior port-forward and starts a fresh one. |
| Bare `model: "claude-haiku-4-5"` (no provider prefix) bills sonnet | Per `gateway-deployment.md`'s Pitfalls table — bare names land in slot 7 (`global_default`), which is sonnet. | The trial workload uses the canonical `anthropic:claude-haiku-4-5` id. For your own clients, send the canonical id or set a workspace default in `.metis/routing.yaml`. |

## Loopback-only default (gateway v1)

The gateway forces `host=127.0.0.1` inside the pod (v1 safety guarantee
per [`specs/gateway.md`](../specs/gateway.md) §3.2). The chart bridges
this with a socat sidecar so the cluster Service can reach it — that's
why the quickstart uses `kubectl port-forward`. The migration path to a
non-loopback bind ships behind TLS termination + rate-limit hardening
(see [`specs/gateway-hardening.md`](../specs/gateway-hardening.md)); for
production, terminate TLS in front of the Service per
[`gateway-deployment.md "Production-readiness audit"`](../gateway-deployment.md#production-readiness-audit).

## Where this fits

This doc is the smoothest landing path. From here:

- **Run your own workload through the gateway.** Follow
  [`customer-trial-recipe.md`](../customer-trial-recipe.md). The
  per-key cost numbers are the same; the per-quality numbers come
  from your rubric.
- **Drop the helm install in front of an existing tool.** Follow
  [`gateway-client-quickstart.md`](../gateway-client-quickstart.md).
  Flip `ANTHROPIC_BASE_URL` (Claude Code) or `OPENAI_BASE_URL` (Cursor)
  on devs' machines; their existing tools route through the gateway
  unchanged.
- **Read the savings story.** [`savings-demo.md`](../savings-demo.md)
  for the §A3-rev3 inversion that proved the routing wedge.
