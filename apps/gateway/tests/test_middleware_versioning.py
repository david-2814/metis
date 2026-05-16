"""Tests for `Metis-API-Version` middleware on the gateway.

Covers api-versioning.md §7:

1. Default header value when absent → resolves to ``CURRENT_VERSION``.
2. Header round-trips on a response.
3. Deprecation header surfaces on within-window deprecated versions.
4. Below-``MIN_SUPPORTED_VERSION`` requests are rejected with HTTP 410 +
   the documented ``version_unsupported`` body.
5. Past-sunset versions are rejected with HTTP 410 even when above min.
6. ``Metis-API-Versions-Supported`` header round-trips on every response.
7. ``OPTIONS`` pre-flight returns 204 + version-negotiation headers.
8. Provider-shape routes (``/v1/chat/completions``, ``/v1/messages``)
   ignore the header on the request and don't stamp it on the response.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import msgspec
import pytest
from metis_gateway.app import build_app
from metis_gateway.middleware_versioning import (
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
# `resolve_version` unit tests — guard the resolution logic in isolation.
# ---------------------------------------------------------------------------


def test_resolve_absent_header_returns_current() -> None:
    r = resolve_version(None)
    assert r.resolved == CURRENT_VERSION
    assert r.is_deprecated is False
    assert r.is_unsupported is False
    assert r.sunset is None
    assert r.reason is None


def test_resolve_empty_header_returns_current() -> None:
    r = resolve_version("   ")
    assert r.resolved == CURRENT_VERSION
    assert r.is_deprecated is False
    assert r.is_unsupported is False


def test_resolve_below_min_marks_unsupported() -> None:
    r = resolve_version("0.9")
    assert r.resolved == "0.9"
    assert r.is_unsupported is True
    assert r.reason == "below_min"
    assert r.sunset == DEFAULT_BELOW_MIN_SUNSET


def test_resolve_explicitly_deprecated_within_window_marks_deprecated(monkeypatch) -> None:
    monkeypatch.setitem(DEPRECATED_VERSIONS, "1.0", "2027-01-01")
    # Frozen clock well before sunset.
    r = resolve_version("1.0", now=datetime(2026, 5, 15, tzinfo=UTC))
    assert r.resolved == "1.0"
    assert r.is_deprecated is True
    assert r.is_unsupported is False
    assert r.sunset == "2027-01-01"


def test_resolve_past_sunset_marks_unsupported(monkeypatch) -> None:
    """A version listed in DEPRECATED_VERSIONS with a past sunset → 410.

    Uses a "9.0" sentinel so the `below_min` branch can't accidentally win;
    9.0 > 1.0, so the only way it returns unsupported is via the
    past-sunset branch."""
    monkeypatch.setitem(DEPRECATED_VERSIONS, "9.0", "2026-01-01")
    r = resolve_version("9.0", now=datetime(2026, 5, 15, tzinfo=UTC))
    assert r.is_unsupported is True
    assert r.reason == "past_sunset"
    assert r.sunset == "2026-01-01"


def test_resolve_sunset_boundary_today_still_served(monkeypatch) -> None:
    """Sunset arithmetic: today == sunset_date → still served (strict `>` only)."""
    monkeypatch.setitem(DEPRECATED_VERSIONS, "9.0", "2026-05-15")
    r = resolve_version("9.0", now=datetime(2026, 5, 15, 23, 59, tzinfo=UTC))
    assert r.is_unsupported is False
    assert r.is_deprecated is True


def test_resolve_unknown_future_version_passes_through() -> None:
    r = resolve_version("2.5")
    assert r.resolved == "2.5"
    assert r.is_deprecated is False
    assert r.is_unsupported is False


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


async def test_versions_supported_header_round_trips(client) -> None:
    """Every Metis-owned response advertises the supported-versions list."""
    r = await client.get("/healthz")
    assert r.status_code == 200
    advertised = r.headers["metis-api-versions-supported"]
    assert advertised == ", ".join(SUPPORTED_VERSIONS)
    # And still present when the client pins a specific version.
    r2 = await client.get("/healthz", headers={"Metis-API-Version": "1.0"})
    assert r2.headers["metis-api-versions-supported"] == advertised


async def test_below_min_returns_410_unsupported(client) -> None:
    """Below-min pin → 410 with the documented `version_unsupported` body."""
    r = await client.get("/healthz", headers={"Metis-API-Version": "0.9"})
    assert r.status_code == 410
    body = msgspec.json.decode(r.content)
    assert body["error"]["code"] == "version_unsupported"
    assert body["error"]["requested"] == "0.9"
    assert body["error"]["min_supported"] == MIN_SUPPORTED_VERSION
    assert body["error"]["current"] == CURRENT_VERSION
    assert body["error"]["reason"] == "below_min"
    # The negotiation headers still surface so a client can recover.
    assert r.headers["metis-api-version"] == "0.9"
    assert r.headers["metis-api-versions-supported"] == ", ".join(SUPPORTED_VERSIONS)


async def test_explicitly_deprecated_within_window_stamps_sunset(client, monkeypatch) -> None:
    """A version in DEPRECATED_VERSIONS with a future sunset is served as deprecated."""
    monkeypatch.setitem(DEPRECATED_VERSIONS, "1.0", "2027-01-01")
    r = await client.get("/healthz", headers={"Metis-API-Version": "1.0"})
    assert r.status_code == 200
    assert r.headers["metis-api-version"] == "1.0"
    assert r.headers["deprecation"] == "true"
    assert r.headers["sunset"] == "2027-01-01"


async def test_past_sunset_returns_410(client, monkeypatch) -> None:
    """A version whose sunset date has passed is rejected with 410.

    Uses "9.0" so the `below_min` branch can't win; this isolates the
    past-sunset enforcement."""
    monkeypatch.setitem(DEPRECATED_VERSIONS, "9.0", "2020-01-01")  # long past
    r = await client.get("/healthz", headers={"Metis-API-Version": "9.0"})
    assert r.status_code == 410
    body = msgspec.json.decode(r.content)
    assert body["error"]["code"] == "version_unsupported"
    assert body["error"]["reason"] == "past_sunset"
    # Sunset is still advertised so the client knows the boundary.
    assert r.headers["sunset"] == "2020-01-01"


async def test_options_preflight_returns_204_with_version_headers(client) -> None:
    """OPTIONS short-circuits with 204 + the negotiation headers."""
    r = await client.request("OPTIONS", "/healthz")
    assert r.status_code == 204
    assert r.headers["metis-api-version"] == CURRENT_VERSION
    assert r.headers["metis-api-versions-supported"] == ", ".join(SUPPORTED_VERSIONS)
    assert r.content == b""


async def test_options_preflight_deprecated_carries_deprecation_headers(
    client, monkeypatch
) -> None:
    """Pre-flight for a deprecated version still tells the client it's deprecated."""
    monkeypatch.setitem(DEPRECATED_VERSIONS, "1.0", "2027-01-01")
    r = await client.request("OPTIONS", "/healthz", headers={"Metis-API-Version": "1.0"})
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

    mw = VersioningMiddleware(dummy_app, skip_path_prefixes=())
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/healthz",
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
            "Metis-API-Version": "0.9",  # would-be unsupported; must be ignored
        },
        json={"model": "haiku", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200
    assert "metis-api-version" not in r.headers
    assert "metis-api-versions-supported" not in r.headers
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
    assert "metis-api-versions-supported" not in r.headers
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
    assert "metis-api-versions-supported" not in r.headers


async def test_provider_shape_below_min_is_not_rejected_by_versioning(
    client, bearer_token, scripted_adapter
) -> None:
    """The 410 enforcement must not touch provider-shape paths.

    A buyer's SDK could theoretically send `Metis-API-Version: 0.9` (e.g.
    via a misconfigured proxy). The provider-shape skip must take priority
    so the buyer's API call still completes."""
    scripted_adapter.push_response(text="hi")
    r = await client.post(
        "/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {bearer_token}",
            "Metis-API-Version": "0.9",
        },
        json={"model": "haiku", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200
