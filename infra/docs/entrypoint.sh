#!/usr/bin/env sh
# Serve the rendered mkdocs site with the stdlib http.server.
#
# All knobs are env-driven so docker-compose / helm values can override
# without rebuilding the image:
#   METIS_DOCS_HOST   bind address (default 0.0.0.0; docs are public)
#   METIS_DOCS_PORT   listen port  (default 8423; gateway is 8422, +1)
#   METIS_DOCS_ROOT   site root    (default /srv/docs)
#
# Extra args passed to `docker run` / `docker compose run` are forwarded
# verbatim to python -m http.server.

set -eu

cd "$METIS_DOCS_ROOT"

exec python -m http.server \
    --bind "$METIS_DOCS_HOST" \
    "$METIS_DOCS_PORT" \
    "$@"
