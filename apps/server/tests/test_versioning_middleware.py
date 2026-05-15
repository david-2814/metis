"""Tests for `Metis-API-Version` middleware on the agent server.

The server has no provider-shape routes (those live on the gateway), so
every endpoint is Metis-owned and the header always round-trips. Covers
api-versioning.md §7; the provider-shape skip test only runs on the
gateway side."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import msgspec
import pytest
from metis_server.app import build_app
from metis_server.middleware_versioning import (
    CURRENT_VERSION,
    DEFAULT_BELOW_MIN_SUNSET,
    DEPRECATED_VERSIONS,
    MIN_SUPPORTED_VERSION,
    SUPPORTED_VERSIONS,
    VersioningMiddleware,
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
    r = resolve_version(None)
    assert r.resolved == CURRENT_VERSION
    assert r.is_deprecated is False
    assert r.is_unsupported is False
    assert r.sunset is None
    assert r.reason is None


def test_resolve_below_min_marks_unsupported() -> None:
    r = resolve_version("0.9")
    assert r.resolved == "0.9"
    assert r.is_unsupported is True
    assert r.reason == "below_min"
    assert r.sunset == DEFAULT_BELOW_MIN_SUNSET


def test_resolve_explicitly_deprecated_within_window_marks_deprecated(monkeypatch) -> None:
    monkeypatch.setitem(DEPRECATED_VERSIONS, "1.0", "2027-01-01")
    r = resolve_version("1.0", now=datetime(2026, 5, 15, tzinfo=UTC))
    assert r.resolved == "1.0"
    assert r.is_deprecated is True
    assert r.is_unsupported is False
    assert r.sunset == "2027-01-01"


def test_resolve_past_sunset_marks_unsupported(monkeypatch) -> None:
    """Past-sunset arithmetic on a "9.0" sentinel (above MIN_SUPPORTED) so the
    `below_min` branch can't accidentally claim the verdict."""
    monkeypatch.setitem(DEPRECATED_VERSIONS, "9.0", "2026-01-01")
    r = resolve_version("9.0", now=datetime(2026, 5, 15, tzinfo=UTC))
    assert r.is_unsupported is True
    assert r.reason == "past_sunset"
    assert r.sunset == "2026-01-01"


def test_resolve_sunset_boundary_today_still_served(monkeypatch) -> None:
    """today == sunset_date → still served (strict `>` comparison)."""
    monkeypatch.setitem(DEPRECATED_VERSIONS, "9.0", "2026-05-15")
    r = resolve_version("9.0", now=datetime(2026, 5, 15, 23, 59, tzinfo=UTC))
    assert r.is_unsupported is False
    assert r.is_deprecated is True


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


async def test_versions_supported_header_round_trips(client) -> None:
    """Every Metis-owned response advertises the supported-versions list."""
    r = await client.get("/health")
    assert r.status_code == 200
    advertised = r.headers["metis-api-versions-supported"]
    assert advertised == ", ".join(SUPPORTED_VERSIONS)


async def test_below_min_returns_410_unsupported(client) -> None:
    """Below-min pin → 410 with the documented `version_unsupported` body."""
    r = await client.get("/health", headers={"Metis-API-Version": "0.9"})
    assert r.status_code == 410
    body = msgspec.json.decode(r.content)
    assert body["error"]["code"] == "version_unsupported"
    assert body["error"]["requested"] == "0.9"
    assert body["error"]["min_supported"] == MIN_SUPPORTED_VERSION
    assert body["error"]["current"] == CURRENT_VERSION
    assert body["error"]["reason"] == "below_min"
    assert r.headers["metis-api-version"] == "0.9"
    assert r.headers["metis-api-versions-supported"] == ", ".join(SUPPORTED_VERSIONS)


async def test_explicitly_deprecated_within_window_stamps_sunset(client, monkeypatch) -> None:
    monkeypatch.setitem(DEPRECATED_VERSIONS, "1.0", "2027-01-01")
    r = await client.get("/health", headers={"Metis-API-Version": "1.0"})
    assert r.status_code == 200
    assert r.headers["metis-api-version"] == "1.0"
    assert r.headers["deprecation"] == "true"
    assert r.headers["sunset"] == "2027-01-01"


async def test_past_sunset_returns_410(client, monkeypatch) -> None:
    """Past-sunset deprecation → 410 even when the version is above MIN."""
    monkeypatch.setitem(DEPRECATED_VERSIONS, "9.0", "2020-01-01")
    r = await client.get("/health", headers={"Metis-API-Version": "9.0"})
    assert r.status_code == 410
    body = msgspec.json.decode(r.content)
    assert body["error"]["code"] == "version_unsupported"
    assert body["error"]["reason"] == "past_sunset"
    assert r.headers["sunset"] == "2020-01-01"


async def test_options_preflight_returns_204_with_version_headers(client) -> None:
    r = await client.request("OPTIONS", "/health")
    assert r.status_code == 204
    assert r.headers["metis-api-version"] == CURRENT_VERSION
    assert r.headers["metis-api-versions-supported"] == ", ".join(SUPPORTED_VERSIONS)
    assert r.content == b""


async def test_options_preflight_deprecated_carries_deprecation_headers(
    client, monkeypatch
) -> None:
    monkeypatch.setitem(DEPRECATED_VERSIONS, "1.0", "2027-01-01")
    r = await client.request("OPTIONS", "/health", headers={"Metis-API-Version": "1.0"})
    assert r.status_code == 204
    assert r.headers["metis-api-version"] == "1.0"
    assert r.headers["deprecation"] == "true"
    assert r.headers["sunset"] == "2027-01-01"


# ---------------------------------------------------------------------------
# `request.state.metis_api_version` is exposed to handlers.
# ---------------------------------------------------------------------------


async def test_middleware_stamps_state_for_downstream_handlers() -> None:
    """A handler can read ``request.state.metis_api_version`` via scope["state"]."""
    captured: dict = {}

    async def dummy_app(scope, receive, send):
        captured["state"] = scope.get("state")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    mw = VersioningMiddleware(dummy_app)
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/health",
        "headers": [(b"metis-api-version", b"1.0")],
    }
    sent: list = []

    async def send(m):
        sent.append(m)

    async def receive():
        return {"type": "http.disconnect"}

    await mw(scope, receive, send)

    state = captured["state"]
    assert state is not None
    assert state.metis_api_version == "1.0"
