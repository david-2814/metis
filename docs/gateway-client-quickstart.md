# Gateway client quickstart

> Point your existing AI dev tool at the Metis gateway. Get a per-dev,
> per-project cost breakdown without your devs changing anything they do.

This doc is for the **client side** of the gateway: how a dev (or their
agent / IDE / script) gets traffic flowing through Metis. For the
**server side** — installing, running, and operating `metis gateway`
itself — see [gateway-deployment.md](gateway-deployment.md). The
expected sequence is: deploy the gateway, hand a gateway key to each
dev, point each dev's tool at the gateway URL.

The gateway speaks two inbound shapes, each on its own URL path:

| Inbound shape  | Path                       | Env var the client expects                  |
| -------------- | -------------------------- | ------------------------------------------- |
| Anthropic      | `POST /v1/messages`        | `ANTHROPIC_BASE_URL` (+ `ANTHROPIC_API_KEY`) |
| OpenAI         | `POST /v1/chat/completions`| `OPENAI_BASE_URL` (+ `OPENAI_API_KEY`)       |

Both paths route through the same engine, the same adapter set, and
the same trace store. Cost and token data appears in
`/analytics/cost` on the `metis serve` instance reading the same
SQLite database (default: `~/.metis/metis.db`).

---

## Prerequisites

Before any client work, you need a running gateway and a gateway key
issued for the workspace you want traces attributed to:

```bash
# One time: issue a key. The plaintext token prints exactly once.
uv run metis gateway issue-key \
  --name "alice-laptop" \
  --workspace /path/to/buyer-project
# key_id: gk_01J...
# token:  gw_01J...   <- save this; only the hash is persisted

# Start the gateway (default: 127.0.0.1:8422)
uv run metis gateway --port 8422
```

The gateway binds loopback-only in v1. For a remote dev to reach it,
the operator puts a TLS terminator in front; see
[gateway-deployment.md](gateway-deployment.md).

For the rest of this doc:

- `GATEWAY_URL` = `http://127.0.0.1:8422` (or whatever your operator gave you)
- `GATEWAY_KEY` = the `gw_…` token you saved above

---

## 1. Claude Code (Anthropic-shape)

Claude Code reads `ANTHROPIC_BASE_URL` for the API endpoint and
`ANTHROPIC_API_KEY` for auth. Point both at the gateway:

```bash
export ANTHROPIC_BASE_URL="http://127.0.0.1:8422"
export ANTHROPIC_API_KEY="gw_01J..."   # the gateway token, not your Anthropic key
claude  # or whatever invocation you use
```

The gateway's `/v1/messages` endpoint accepts the gateway token via
either `x-api-key` (which Claude Code / the Anthropic SDK sends) or
`Authorization: Bearer …`.

**Expected behavior on the client:** none. Claude Code behaves
identically — same models, same tool use, same streaming, same
cancellation. The only difference is that every turn is now stamped
with your `gateway_key_id` in the Metis trace store.

**Where to look for the trace:**

```bash
# Against the metis serve instance that shares the gateway's SQLite db
curl http://127.0.0.1:8421/analytics/cost?window=24h | jq
```

The per-key roll-up (`group_by=gateway_key`) is a follow-on per
`gateway.md §V`; for now, group by `model` or `none` and filter by
your key's workspace.

---

## 2. Cursor (OpenAI-shape)

Cursor → Settings → Models → "OpenAI API Key" panel:

| Field         | Value                                           |
| ------------- | ----------------------------------------------- |
| Base URL      | `http://127.0.0.1:8422/v1`                      |
| API Key       | `gw_01J...` (the gateway token)                 |
| Model         | a model id the key allows (e.g. `gpt-5-mini`, or any Anthropic alias) |

The trailing `/v1` matters — Cursor appends `/chat/completions` to the
base URL, so `http://127.0.0.1:8422/v1` resolves to
`http://127.0.0.1:8422/v1/chat/completions`.

Anything that Cursor was doing against `api.openai.com` it now does
against the gateway. Because Metis can route an OpenAI-shape request
to any registered model — including Anthropic — you can put e.g.
`anthropic:claude-haiku-4-5` (or the `haiku` alias) into the Cursor
model field and get a Claude model through an OpenAI-shape client.
This is the universal-IR wedge from `deployment-shape.md §3.4` in
practice.

**Smoke check:** send one Cursor request, then run the curl in §4.

---

## 3. openai-python / anthropic-python (raw SDK)

Minimal working examples are in [examples/gateway/](../examples/gateway/):

- [openai-python.py](../examples/gateway/openai-python.py)
- [anthropic-python.py](../examples/gateway/anthropic-python.py)
- [curl-smoke.sh](../examples/gateway/curl-smoke.sh)

The SDK pattern is uniform: pass the gateway URL as `base_url` and
the gateway token as the API key argument the SDK already expects.

```python
# openai-python
from openai import OpenAI
client = OpenAI(
    base_url="http://127.0.0.1:8422/v1",
    api_key="gw_01J...",
)
```

```python
# anthropic-python
from anthropic import Anthropic
client = Anthropic(
    base_url="http://127.0.0.1:8422",
    api_key="gw_01J...",
)
```

Note the path difference: the Anthropic SDK already includes
`/v1/messages` in its hardcoded path, so the base URL is the gateway
root. The OpenAI SDK appends `/chat/completions` to the base URL, so
the base URL is the gateway root plus `/v1`.

---

## 4. Curl smoke check

The minimal one-turn sanity check, no client SDK required:

```bash
curl http://127.0.0.1:8422/v1/messages \
  -H "x-api-key: gw_01J..." \
  -H "anthropic-version: 2023-06-01" \
  -H "content-type: application/json" \
  -d '{
    "model": "haiku",
    "max_tokens": 64,
    "messages": [{"role": "user", "content": "Say hello in one word."}]
  }'
```

Expected: a JSON body with `content[].text` populated and a
`usage` block. The same call appears in the gateway's stderr log
and, within a second, in the SQLite trace as an
`llm.call_completed` event with `gateway_key_id` populated.

To confirm the trace landed:

```bash
sqlite3 ~/.metis/metis.db \
  "SELECT json_extract(payload_json, '$.gateway_key_id'),
          json_extract(payload_json, '$.model'),
          json_extract(payload_json, '$.cost_usd')
   FROM events
   WHERE type = 'llm.call_completed'
   ORDER BY timestamp_us DESC LIMIT 1;"
```

You should see your `gk_…` key id, the resolved model id, and a
sub-cent cost for the one haiku call.

---

## 5. Pitfalls

### Anthropic SDK client-side retry

`anthropic-python` defaults to `max_retries=2`, and so do most agent
loops that wrap it. The gateway *already* retries upstream provider
errors with `retry_after` honoring; client-side retries on top of
gateway retries waste budget and inflate trace counts. For
interactive use the default is fine, but for batch / CI clients
prefer `Anthropic(max_retries=0, ...)` and let the gateway own retry
policy.

### OpenAI SDK timeout

`openai-python` defaults to a 10-minute request timeout. For sync,
non-streaming Anthropic-via-OpenAI-shape requests against a slow
model (Opus, large context), this is the right ceiling — don't lower
it below ~2 minutes or you'll start cancelling legitimate completions
mid-flight. For streaming requests the timeout applies to the
*connection*, not the total stream duration.

### Mid-stream cancellation

Both shapes propagate client disconnects: if you abort an HTTP
request mid-SSE, the gateway notices via Starlette's
`request.is_disconnected`, raises `ClientDisconnected` through the
harness, and stops the upstream provider call. The `llm.call_completed`
event still fires but with `error_class: "CANCELLED"` and partial
usage. Don't expect "cancel was clean" without checking the trace —
the upstream provider may have already billed for tokens generated
before the abort.

### Tool use round-trips

The gateway is per-request stateless (gateway.md §2). The agent
loop — i.e. who decides to call a tool, who runs it, who folds the
result back into the next turn — lives on the client side. The
gateway only sees one HTTP call at a time. If your client doesn't
implement the tool-result resubmit loop, tool use will appear to
"work" (the assistant emits a `tool_use` block) but the conversation
will not progress. This is true of every transparent gateway in the
LiteLLM / Portkey / Helicone lane.

### Model the client passes wins routing

OpenAI / Anthropic SDKs always include `model` in the request body.
The routing chain treats that as a per-message override (slot 1) and
short-circuits the rest of the chain. Configured rules
(`.metis/routing.yaml`), pattern routing, and workspace defaults are
not exercised on gateway traffic unless a client deliberately omits
`model` — which most SDKs make hard. This is per
`gateway.md §V` and not a bug; if you want server-side routing
policy to win on gateway traffic, either build a client that omits
`model` or use the agent surface (`metis chat` / `metis serve`)
instead.

---

## 6. Verifying end-to-end in under a minute

The whole loop, on one machine, against a real provider:

```bash
# 1. issue a key for this workspace, save the printed gw_… token
uv run metis gateway issue-key --name "smoke" --workspace .

# 2. start the gateway
uv run metis gateway --port 8422 &

# 3. one-turn smoke (Anthropic shape; ~$0.0001 with haiku)
GW_TOKEN="gw_01J..."  # paste from step 1
curl -s http://127.0.0.1:8422/v1/messages \
  -H "x-api-key: $GW_TOKEN" \
  -H "anthropic-version: 2023-06-01" \
  -H "content-type: application/json" \
  -d '{
    "model": "haiku",
    "max_tokens": 32,
    "messages": [{"role": "user", "content": "ping"}]
  }' | jq .content

# 4. confirm the trace landed
sqlite3 ~/.metis/metis.db \
  "SELECT type, json_extract(payload_json, '$.model'),
          json_extract(payload_json, '$.cost_usd')
   FROM events
   WHERE type IN ('route.decided', 'llm.call_completed', 'turn.completed')
   ORDER BY timestamp_us DESC LIMIT 5;"
```

If you see three event rows for the call (`route.decided`,
`llm.call_completed`, `turn.completed`) with the same monotonic ULID
ordering and a cost in fractions of a cent, the gateway is wired
end-to-end.
