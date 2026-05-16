# syntax=docker/dockerfile:1.7
#
# Multi-stage build for the Metis documentation site (mkdocs-material).
#
# - Builder: installs mkdocs-material into a pinned venv, copies docs/
#   + mkdocs.yml, runs `mkdocs build` → /build/site.
# - Runtime: python:3.13-slim with only the built static site and a
#   minimal Python stdlib http.server. Non-root user. Healthcheck against
#   the index page.
#
# Mirrors the gateway image shape (infra/gateway/Dockerfile) — same base
# image, same non-root uid/gid (1000:1000), same entrypoint indirection
# pattern, same healthcheck cadence. Docs are public content; no
# loopback-only constraint applies.

ARG PYTHON_VERSION=3.13-slim

# ---------------------------------------------------------------------------
# Builder — install mkdocs-material and render the static site.
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION} AS builder

# Pin mkdocs-material to a known-good release. Bump when a feature in
# the docs (admonitions, tabbed code blocks, social-card plugin, etc.)
# pulls in a newer version.
ARG MKDOCS_MATERIAL_VERSION=9.5.39

WORKDIR /build

RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/venv/bin/pip install --no-cache-dir \
        "mkdocs-material==${MKDOCS_MATERIAL_VERSION}"

# Copy only the files mkdocs needs. docs/ is the content tree;
# mkdocs.yml is the config at repo root.
COPY mkdocs.yml ./mkdocs.yml
COPY docs/ ./docs/

# Render. --strict turns broken links / missing nav targets into a
# build failure so a bad reorg fails the image build instead of
# silently shipping a 404.
RUN /opt/venv/bin/mkdocs build --strict --site-dir /build/site

# ---------------------------------------------------------------------------
# Runtime — serve the static site with the stdlib http.server.
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION} AS runtime

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 1000 metis \
    && useradd --uid 1000 --gid 1000 --home-dir /home/metis --create-home metis

# Copy the rendered site into a stable location.
COPY --from=builder --chown=metis:metis /build/site /srv/docs

COPY --chown=root:root infra/docs/entrypoint.sh /usr/local/bin/metis-docs-entrypoint
RUN chmod 0755 /usr/local/bin/metis-docs-entrypoint

ENV PYTHONUNBUFFERED=1 \
    METIS_DOCS_HOST=0.0.0.0 \
    METIS_DOCS_PORT=8423 \
    METIS_DOCS_ROOT=/srv/docs

USER metis
WORKDIR /srv/docs

EXPOSE 8423

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl --fail --silent --show-error \
        "http://127.0.0.1:${METIS_DOCS_PORT}/index.html" \
        || exit 1

ENTRYPOINT ["metis-docs-entrypoint"]
