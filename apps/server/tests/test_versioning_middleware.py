"""Tests for `Metis-API-Version` middleware on the agent server.

The server has no provider-shape routes (those live on the gateway), so
every endpoint is Metis-owned and the header always round-trips. Covers
points 1-3 of api-versioning.md §7; point 4 is exercised in the gateway
tests since only the gateway has a provider-shape surface.
"""

from __future__ import annotations

import httpx
import pytest
from metis_server.app import build_app
from metis_server.middleware_versioning import (
    CURRENT_VERSION,
    DEFAULT_BELOW_MIN_SUNSET,
    DEPRECATED_VERSIONS,
    resolve_version,
)


@pytest.fixture
async def client(runtime):
    app = build_app(runtime)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


# ---------------------------------------------------------------------------
# `resolve_version` unit tests.
# ---------------------------------------------------------------------------


def test_resolve_absent_header_returns_current() -> None:
    resolved, deprecated, sunset = resolve_version(None)
    assert resolved == CURRENT_VERSION
    assert deprecated is False
    assert sunset is None


def test_resolve_below_min_marks_deprecated() -> None:
    resolved, deprecated, sunset = resolve_version("0.9")
    assert resolved == "0.9"
    assert deprecated is True
    assert sunset == DEFAULT_BELOW_MIN_SUNSET


def test_resolve_explicitly_deprecated_uses_mapped_sunset(monkeypatch) -> None:
    monkeypatch.setitem(DEPRECATED_VERSIONS, "1.0", "2027-01-01")
    try:
        resolved, deprecated, sunset = resolve_version("1.0")
    finally:
        DEPRECATED_VERSIONS.pop("1.0", None)
    assert resolved == "1.0"
    assert deprecated is True
    assert sunset == "2027-01-01"


# ---------------------------------------------------------------------------
# HTTP round-trip tests against /health and /analytics/cost.
# ---------------------------------------------------------------------------


async def test_health_default_version_when_header_absent(client) -> None:
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.headers.get("metis-api-version") == CURRENT_VERSION
    assert "deprecation" not in r.headers
    assert "sunset" not in r.headers


async def test_health_echoes_requested_version(client) -> None:
    r = await client.get("/health", headers={"Metis-API-Version": "1.0"})
    assert r.status_code == 200
    assert r.headers["metis-api-version"] == "1.0"
    assert "deprecation" not in r.headers


async def test_analytics_endpoint_carries_version_header(client) -> None:
    """`/analytics/*` is the headline Metis-owned namespace; the header must
    surface there too, not just on `/health`."""
    r = await client.get("/analytics/cost")
    assert r.status_code == 200
    assert r.headers["metis-api-version"] == CURRENT_VERSION


async def test_below_min_stamps_deprecation_and_sunset(client) -> None:
    r = await client.get("/health", headers={"Metis-API-Version": "0.9"})
    assert r.status_code == 200
    assert r.headers["metis-api-version"] == "0.9"
    assert r.headers["deprecation"] == "true"
    assert r.headers["sunset"] == DEFAULT_BELOW_MIN_SUNSET


async def test_explicitly_deprecated_version_stamps_mapped_sunset(client, monkeypatch) -> None:
    monkeypatch.setitem(DEPRECATED_VERSIONS, "1.0", "2027-01-01")
    r = await client.get("/health", headers={"Metis-API-Version": "1.0"})
    assert r.status_code == 200
    assert r.headers["metis-api-version"] == "1.0"
    assert r.headers["deprecation"] == "true"
    assert r.headers["sunset"] == "2027-01-01"
