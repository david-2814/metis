"""Tests for `Metis-API-Version` middleware on the gateway.

Covers the four points called out in api-versioning.md §7:

1. Default header value when absent → resolves to ``CURRENT_VERSION``.
2. Header round-trips on a response.
3. Deprecation header surfaces when the resolved version is below
   ``MIN_SUPPORTED_VERSION`` or listed in ``DEPRECATED_VERSIONS``.
4. Provider-shape routes (``/v1/chat/completions``, ``/v1/messages``)
   ignore the header on the request and don't stamp it on the response.
"""

from __future__ import annotations

import httpx
import pytest
from metis_gateway.app import build_app
from metis_gateway.middleware_versioning import (
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
# `resolve_version` unit tests — guard the resolution logic in isolation.
# ---------------------------------------------------------------------------


def test_resolve_absent_header_returns_current() -> None:
    resolved, deprecated, sunset = resolve_version(None)
    assert resolved == CURRENT_VERSION
    assert deprecated is False
    assert sunset is None


def test_resolve_empty_header_returns_current() -> None:
    resolved, deprecated, sunset = resolve_version("   ")
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


def test_resolve_unknown_future_version_passes_through() -> None:
    resolved, deprecated, sunset = resolve_version("2.5")
    assert resolved == "2.5"
    assert deprecated is False
    assert sunset is None


# ---------------------------------------------------------------------------
# HTTP round-trip tests against /healthz (the gateway's only Metis-owned route).
# ---------------------------------------------------------------------------


async def test_metis_owned_route_default_version_when_header_absent(client) -> None:
    r = await client.get("/healthz")
    assert r.status_code == 200
    assert r.headers.get("metis-api-version") == CURRENT_VERSION
    assert "deprecation" not in r.headers
    assert "sunset" not in r.headers


async def test_metis_owned_route_echoes_requested_version(client) -> None:
    r = await client.get("/healthz", headers={"Metis-API-Version": "1.0"})
    assert r.status_code == 200
    assert r.headers["metis-api-version"] == "1.0"
    assert "deprecation" not in r.headers


async def test_metis_owned_route_below_min_stamps_deprecation(client) -> None:
    r = await client.get("/healthz", headers={"Metis-API-Version": "0.9"})
    assert r.status_code == 200
    assert r.headers["metis-api-version"] == "0.9"
    assert r.headers["deprecation"] == "true"
    assert r.headers["sunset"] == DEFAULT_BELOW_MIN_SUNSET


async def test_metis_owned_route_explicitly_deprecated_stamps_mapped_sunset(
    client, monkeypatch
) -> None:
    monkeypatch.setitem(DEPRECATED_VERSIONS, "1.0", "2027-01-01")
    r = await client.get("/healthz", headers={"Metis-API-Version": "1.0"})
    assert r.status_code == 200
    assert r.headers["metis-api-version"] == "1.0"
    assert r.headers["deprecation"] == "true"
    assert r.headers["sunset"] == "2027-01-01"


# ---------------------------------------------------------------------------
# Provider-shape routes must pass through untouched.
# ---------------------------------------------------------------------------


async def test_chat_completions_does_not_stamp_metis_version(
    client, bearer_token, scripted_adapter
) -> None:
    """Provider-shape route — the response is OpenAI's contract, no Metis header."""
    scripted_adapter.push_response(text="hi")
    r = await client.post(
        "/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {bearer_token}",
            "Metis-API-Version": "0.9",  # would-be deprecated; must be ignored
        },
        json={"model": "haiku", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200
    assert "metis-api-version" not in r.headers
    assert "deprecation" not in r.headers
    assert "sunset" not in r.headers


async def test_messages_does_not_stamp_metis_version(
    client, bearer_token, scripted_adapter
) -> None:
    """Anthropic-shape route — same provider-frozen-contract guarantee."""
    scripted_adapter.push_response(text="hi")
    r = await client.post(
        "/v1/messages",
        headers={
            "x-api-key": bearer_token,
            "Metis-API-Version": "0.9",
        },
        json={
            "model": "haiku",
            "max_tokens": 32,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert r.status_code == 200
    assert "metis-api-version" not in r.headers
    assert "deprecation" not in r.headers


async def test_provider_shape_auth_failure_still_skips_versioning(client) -> None:
    """Even an auth-failing provider-shape request must not gain the header.

    Skip-by-path runs *before* the route handler, so 401 responses from the
    handler don't pick up `Metis-API-Version` either. Guards against a
    refactor that pushes the skip check into the handler instead of the
    middleware.
    """
    r = await client.post(
        "/v1/chat/completions",
        json={"model": "haiku", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 401
    assert "metis-api-version" not in r.headers
