#!/usr/bin/env bash
# Curl-only smoke check against a running Metis gateway.
# Usage:
#   export METIS_GATEWAY_TOKEN=gw_...
#   ./examples/gateway/curl-smoke.sh
# Exits 0 on HTTP 200, non-zero otherwise. Suitable for CI / health checks.

set -euo pipefail

GATEWAY_URL="${METIS_GATEWAY_URL:-http://127.0.0.1:8422}"
TOKEN="${METIS_GATEWAY_TOKEN:?set METIS_GATEWAY_TOKEN to a gw_... key from \`metis gateway issue-key\`}"

response="$(curl -sS -w '\n%{http_code}' "${GATEWAY_URL}/v1/messages" \
  -H "x-api-key: ${TOKEN}" \
  -H "anthropic-version: 2023-06-01" \
  -H "content-type: application/json" \
  -d '{"model":"haiku","max_tokens":32,"messages":[{"role":"user","content":"ping"}]}')"

http_code="$(printf '%s' "$response" | tail -n1)"
body="$(printf '%s' "$response" | sed '$d')"

if [[ "$http_code" != "200" ]]; then
  echo "gateway returned HTTP $http_code" >&2
  echo "$body" >&2
  exit 1
fi
echo "$body"
