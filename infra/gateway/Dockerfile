# syntax=docker/dockerfile:1.7
#
# Multi-stage build for the Metis transparent gateway (apps/gateway).
#
# - Builder: installs uv, resolves the workspace, builds a non-editable venv.
# - Runtime: python:3.13-slim + the resolved venv. Non-root user. Healthcheck
#   against /healthz. Entrypoint translates METIS_GATEWAY_* env vars into
#   `metis gateway` CLI flags.
#
# The gateway binds 127.0.0.1 inside the container (v1 safety guarantee per
# gateway.md §3.2 / server-api.md §3.1). To reach it from outside the
# container, run with `--network host` (Linux + Docker Desktop ≥4.29) or
# put a TLS terminator in the same network namespace. See
# docs/gateway-deployment.md for the production checklist.

ARG PYTHON_VERSION=3.13-slim

# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION} AS builder

# Pin uv to a known-good release. Bump when uv has a feature we need.
COPY --from=ghcr.io/astral-sh/uv:0.5.13 /uv /usr/local/bin/uv

WORKDIR /app

# Manifests first for layer caching when only source changes.
COPY pyproject.toml uv.lock ./
COPY packages/metis-core/pyproject.toml packages/metis-core/
COPY apps/server/pyproject.toml apps/server/
COPY apps/gateway/pyproject.toml apps/gateway/
COPY apps/cli/pyproject.toml apps/cli/

# Workspace source. uv resolves member dists from these trees.
COPY packages/ packages/
COPY apps/ apps/

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_NO_CACHE=1

RUN uv sync --frozen --no-dev

# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION} AS runtime

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system metis \
    && useradd --system --gid metis --home-dir /home/metis --create-home metis

# Copy the resolved venv and the workspace source. The venv contains
# editable-install .pth entries pointing at /app/packages and /app/apps,
# so both must be present in the runtime image.
COPY --from=builder --chown=metis:metis /app /app

ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    METIS_GATEWAY_HOST=127.0.0.1 \
    METIS_GATEWAY_PORT=8422 \
    METIS_GATEWAY_KEYSTORE=/etc/metis/keys.json \
    METIS_GATEWAY_DB_PATH=/var/lib/metis/metis.db \
    METIS_GATEWAY_GLOBAL_DEFAULT=anthropic:claude-sonnet-4-6

# Writable directories the gateway needs at runtime.
RUN install -d -m 0750 -o metis -g metis /workspace /etc/metis /var/lib/metis

COPY --chown=root:root infra/gateway/entrypoint.sh /usr/local/bin/metis-gateway-entrypoint
RUN chmod 0755 /usr/local/bin/metis-gateway-entrypoint

USER metis
WORKDIR /home/metis

EXPOSE 8422

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl --fail --silent --show-error \
        "http://127.0.0.1:${METIS_GATEWAY_PORT}/healthz" \
        || exit 1

ENTRYPOINT ["metis-gateway-entrypoint"]
