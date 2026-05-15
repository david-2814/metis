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
# Extra args passed to `docker run` / `docker compose run` are forwarded
# verbatim after the env-derived flags so they can override the defaults.

set -eu

if [ "$#" -gt 0 ] && [ "$1" = "issue-key" ]; then
    shift
    exec metis gateway issue-key \
        --keystore "$METIS_GATEWAY_KEYSTORE" \
        "$@"
fi

exec metis gateway \
    --keystore "$METIS_GATEWAY_KEYSTORE" \
    --db-path "$METIS_GATEWAY_DB_PATH" \
    --global-default "$METIS_GATEWAY_GLOBAL_DEFAULT" \
    --host "$METIS_GATEWAY_HOST" \
    --port "$METIS_GATEWAY_PORT" \
    "$@"
