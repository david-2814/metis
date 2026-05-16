#!/usr/bin/env sh
# Translate METIS_GATEWAY_* env vars into `metis gateway` CLI flags.
#
# Two modes:
#   1. `issue-key` — forwards to `metis gateway issue-key`, passing the
#      keystore from $METIS_GATEWAY_KEYSTORE and all remaining args verbatim
#      (so the operator supplies --name / --workspace / --allow-model /
#      --daily-cap-usd).
#   2. default — runs the gateway server.
#
# Wave 13 (gateway-hardening.md §2.1) — optional env vars threaded into
# `metis gateway` when set:
#   METIS_GATEWAY_MAX_CONNECTIONS  (default 1000; uvicorn limit_concurrency)
#   METIS_GATEWAY_REUSE_PORT       (set to any non-empty value for SO_REUSEPORT)
#   METIS_GATEWAY_TLS_CERT         (path to PEM cert; enables in-process TLS)
#   METIS_GATEWAY_TLS_KEY          (path to PEM key; required with TLS_CERT)
#
# Extra args passed to `docker run` / `docker compose run` are forwarded
# verbatim after the env-derived flags so they can override the defaults.

set -eu

if [ "$#" -gt 0 ] && [ "$1" = "issue-key" ]; then
    shift
    exec metis gateway issue-key \
        --keystore "$METIS_GATEWAY_KEYSTORE" \
        "$@"
fi

# Build the flag list incrementally so optional flags only appear when set.
set -- \
    --keystore "$METIS_GATEWAY_KEYSTORE" \
    --db-path "$METIS_GATEWAY_DB_PATH" \
    --global-default "$METIS_GATEWAY_GLOBAL_DEFAULT" \
    --host "$METIS_GATEWAY_HOST" \
    --port "$METIS_GATEWAY_PORT" \
    "$@"

if [ -n "${METIS_GATEWAY_MAX_CONNECTIONS:-}" ]; then
    set -- --max-connections "$METIS_GATEWAY_MAX_CONNECTIONS" "$@"
fi
if [ -n "${METIS_GATEWAY_REUSE_PORT:-}" ]; then
    set -- --reuse-port "$@"
fi
if [ -n "${METIS_GATEWAY_TLS_CERT:-}" ]; then
    set -- --tls-cert "$METIS_GATEWAY_TLS_CERT" "$@"
fi
if [ -n "${METIS_GATEWAY_TLS_KEY:-}" ]; then
    set -- --tls-key "$METIS_GATEWAY_TLS_KEY" "$@"
fi

exec metis gateway "$@"
