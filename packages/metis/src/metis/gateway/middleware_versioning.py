"""Pure ASGI middleware that stamps `Metis-API-Version` on responses and
enforces the deprecation policy (api-versioning.md §3 / §6).

Two surface categories:

* **Provider-shape paths** (`/v1/chat/completions`, `/v1/messages`) — passed
  through untouched. Their version is whatever the upstream provider's SDK
  contract says; Metis doesn't get a vote.
* **Metis-owned paths** (everything else served by the gateway, e.g.
  `/healthz`) — the middleware resolves a version from the request header
  (default `CURRENT_VERSION`), echoes it on the response, stamps the
  comma-separated `Metis-API-Versions-Supported` discovery header, and:
    * rejects below-`MIN_SUPPORTED_VERSION` or past-sunset versions with
      HTTP 410 + the documented `version_unsupported` body;
    * stamps `Deprecation: true` + `Sunset: <date>` on within-window
      deprecated versions;
    * short-circuits `OPTIONS` requests with 204 + the version-negotiation
      headers so clients can pre-flight discover what's supported.

The resolved version is also exposed via ``request.state.metis_api_version``
so downstream handlers can branch on it (`if request.state.metis_api_version
< "1.1": ...`).

Pure ASGI rather than ``BaseHTTPMiddleware`` so SSE responses don't get
buffered (api-versioning.md §4 / §5).
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime

import msgspec
from starlette.datastructures import State
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

#: Current version of the Metis-owned HTTP surface. Absent-header requests
#: resolve to this.
CURRENT_VERSION = "1.0"

#: Floor of unconditionally supported versions. A request pinning a version
#: below this floor is rejected with HTTP 410 `version_unsupported`.
MIN_SUPPORTED_VERSION = "1.0"

#: Versions explicitly marked deprecated, mapping `version → ISO sunset date`.
#: Empty in v1; populated when a major bump ships. A version listed here is
#: served (with `Deprecation` + `Sunset` headers) until the sunset date passes;
#: after that the same version is rejected with HTTP 410 just like a
#: below-`MIN_SUPPORTED_VERSION` pin.
DEPRECATED_VERSIONS: dict[str, str] = {}

#: The advertised supported-versions list. The middleware emits this
#: comma-separated on every Metis-owned response under
#: `Metis-API-Versions-Supported`, so clients can pre-flight discover
#: what the server currently accepts without parsing 410 bodies.
SUPPORTED_VERSIONS: tuple[str, ...] = ("1.0",)

#: Default `Sunset` header value for below-`MIN_SUPPORTED_VERSION` pins that
#: aren't explicitly listed in `DEPRECATED_VERSIONS`. Currently unused
#: at the header level (below-min returns 410 outright) but kept as the
#: canonical "below the floor was deprecated through" date for the 410 body.
DEFAULT_BELOW_MIN_SUNSET = "2026-11-15"

#: Provider-shape paths (gateway.md §3.1). The middleware passes these
#: through untouched.
PROVIDER_SHAPE_PREFIXES: tuple[str, ...] = (
    "/v1/chat/completions",
    "/v1/messages",
)

VERSION_HEADER_NAME = b"metis-api-version"
VERSIONS_SUPPORTED_HEADER_NAME = b"metis-api-versions-supported"
DEPRECATION_HEADER_NAME = b"deprecation"
SUNSET_HEADER_NAME = b"sunset"


@dataclass(frozen=True)
class VersionResolution:
    """Outcome of resolving a client-supplied ``Metis-API-Version``.

    * ``resolved`` — the version string to echo back on the response.
    * ``is_deprecated`` — True if served-but-deprecated (within the
      `DEPRECATED_VERSIONS` window). Triggers ``Deprecation: true`` +
      ``Sunset`` headers; the request is still served.
    * ``sunset`` — ISO date this version sunsets, when applicable.
    * ``is_unsupported`` — True if the request must be rejected with 410.
    * ``reason`` — ``None`` | ``"below_min"`` | ``"past_sunset"``; populated
      only when ``is_unsupported`` is True.
    """

    resolved: str
    is_deprecated: bool
    sunset: str | None
    is_unsupported: bool
    reason: str | None


def _parse_semver(value: str) -> tuple[int, ...] | None:
    """Best-effort numeric tuple for comparison; ``None`` if it doesn't parse."""
    try:
        return tuple(int(part) for part in value.split("."))
    except ValueError:
        return None


def _parse_iso_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _today_utc(now: datetime | None = None) -> date:
    if now is None:
        return datetime.now(UTC).date()
    if now.tzinfo is None:
        return now.replace(tzinfo=UTC).date()
    return now.astimezone(UTC).date()


def _is_past_sunset(sunset_iso: str, *, now: datetime | None = None) -> bool:
    sunset_date = _parse_iso_date(sunset_iso)
    if sunset_date is None:
        return False
    return _today_utc(now) > sunset_date


def resolve_version(
    requested: str | None,
    *,
    now: datetime | None = None,
) -> VersionResolution:
    """Resolve a requested ``Metis-API-Version`` into a :class:`VersionResolution`.

    Resolution order:

    * Absent / empty header → ``CURRENT_VERSION``, ok.
    * Listed in ``DEPRECATED_VERSIONS`` with ``today > sunset_date`` →
      unsupported (``reason="past_sunset"``); response is 410.
    * Listed in ``DEPRECATED_VERSIONS`` and within window → deprecated;
      response is 200 with ``Deprecation: true`` + ``Sunset: <date>``.
    * Parses below ``MIN_SUPPORTED_VERSION`` → unsupported
      (``reason="below_min"``); response is 410.
    * Otherwise → echoed back as-is, ok (unknown future versions pass through).
    """
    value = (requested or "").strip()
    if not value:
        return VersionResolution(
            resolved=CURRENT_VERSION,
            is_deprecated=False,
            sunset=None,
            is_unsupported=False,
            reason=None,
        )
    if value in DEPRECATED_VERSIONS:
        sunset_iso = DEPRECATED_VERSIONS[value]
        if _is_past_sunset(sunset_iso, now=now):
            return VersionResolution(
                resolved=value,
                is_deprecated=False,
                sunset=sunset_iso,
                is_unsupported=True,
                reason="past_sunset",
            )
        return VersionResolution(
            resolved=value,
            is_deprecated=True,
            sunset=sunset_iso,
            is_unsupported=False,
            reason=None,
        )
    requested_tuple = _parse_semver(value)
    floor_tuple = _parse_semver(MIN_SUPPORTED_VERSION)
    if requested_tuple is not None and floor_tuple is not None and requested_tuple < floor_tuple:
        return VersionResolution(
            resolved=value,
            is_deprecated=False,
            sunset=DEFAULT_BELOW_MIN_SUNSET,
            is_unsupported=True,
            reason="below_min",
        )
    return VersionResolution(
        resolved=value,
        is_deprecated=False,
        sunset=None,
        is_unsupported=False,
        reason=None,
    )


def _versions_supported_value() -> bytes:
    return ", ".join(SUPPORTED_VERSIONS).encode("latin-1")


def _is_skipped_path(path: str, prefixes: tuple[str, ...]) -> bool:
    return any(path == prefix or path.startswith(prefix + "/") for prefix in prefixes)


def _request_version_header(scope: Scope) -> str | None:
    for name, value in scope.get("headers", []):
        if name == VERSION_HEADER_NAME:
            return value.decode("latin-1")
    return None


def _caller_fingerprint(scope: Scope) -> str:
    """Return a short, log-safe identifier for whoever made the request.

    Looks for ``Authorization: Bearer <token>`` (gateway clients) or
    ``x-api-key`` (Anthropic SDK clients) and returns the first 12 hex
    chars of the bearer's SHA-256. The keystore persists the same hash,
    so an operator can grep ``keys.json`` for the value to find the
    buyer to notify. Returns ``"<no-auth>"`` when no token is present.
    """
    for name, value in scope.get("headers", []):
        if name == b"authorization":
            decoded = value.decode("latin-1")
            if decoded.startswith("Bearer "):
                token = decoded[len("Bearer ") :].strip()
                if token:
                    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]
        elif name == b"x-api-key":
            token = value.decode("latin-1").strip()
            if token:
                return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]
    return "<no-auth>"


def _attach_state(scope: Scope, resolved: str) -> None:
    """Stash the resolved version on ``scope["state"]`` so handlers can read it.

    Starlette's ``Request.state`` reads from ``scope["state"]`` lazily;
    initializing it here gives downstream handlers a consistent
    ``request.state.metis_api_version`` accessor (api-versioning.md §4).
    """
    state = scope.get("state")
    if state is None:
        state = State()
        scope["state"] = state
    if isinstance(state, State):
        state.metis_api_version = resolved
    elif isinstance(state, dict):
        state["metis_api_version"] = resolved


async def _send_unsupported_response(send: Send, resolution: VersionResolution) -> None:
    """Emit a 410 Gone with the documented ``version_unsupported`` body."""
    body = {
        "error": {
            "code": "version_unsupported",
            "requested": resolution.resolved,
            "min_supported": MIN_SUPPORTED_VERSION,
            "current": CURRENT_VERSION,
            "reason": resolution.reason,
            "message": (
                f"Metis-API-Version {resolution.resolved!r} is no longer supported "
                f"(reason={resolution.reason}, min_supported={MIN_SUPPORTED_VERSION}, "
                f"current={CURRENT_VERSION})"
            ),
        }
    }
    payload = msgspec.json.encode(body)
    headers: list[tuple[bytes, bytes]] = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(payload)).encode("latin-1")),
        (VERSION_HEADER_NAME, resolution.resolved.encode("latin-1")),
        (VERSIONS_SUPPORTED_HEADER_NAME, _versions_supported_value()),
    ]
    if resolution.sunset:
        headers.append((SUNSET_HEADER_NAME, resolution.sunset.encode("latin-1")))
    await send(
        {
            "type": "http.response.start",
            "status": 410,
            "headers": headers,
        }
    )
    await send({"type": "http.response.body", "body": payload})


async def _send_options_response(send: Send, resolution: VersionResolution) -> None:
    """Pre-flight: 204 with version-negotiation headers; no body."""
    headers: list[tuple[bytes, bytes]] = [
        (VERSION_HEADER_NAME, resolution.resolved.encode("latin-1")),
        (VERSIONS_SUPPORTED_HEADER_NAME, _versions_supported_value()),
    ]
    if resolution.is_deprecated:
        headers.append((DEPRECATION_HEADER_NAME, b"true"))
        if resolution.sunset:
            headers.append((SUNSET_HEADER_NAME, resolution.sunset.encode("latin-1")))
    await send({"type": "http.response.start", "status": 204, "headers": headers})
    await send({"type": "http.response.body", "body": b""})


class VersioningMiddleware:
    """Stamps `Metis-API-Version` on Metis-owned responses and enforces
    the deprecation policy.

    Provider-shape paths matching ``skip_path_prefixes`` are passed through
    untouched (api-versioning.md §1.1). The gateway constructs the middleware
    with the default ``PROVIDER_SHAPE_PREFIXES``; the server passes ``()``
    because it has no provider-shape surface.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        skip_path_prefixes: tuple[str, ...] = PROVIDER_SHAPE_PREFIXES,
    ) -> None:
        self.app = app
        self.skip_path_prefixes = skip_path_prefixes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if _is_skipped_path(path, self.skip_path_prefixes):
            await self.app(scope, receive, send)
            return

        requested = _request_version_header(scope)
        resolution = resolve_version(requested)

        _attach_state(scope, resolution.resolved)

        if resolution.is_unsupported:
            logger.warning(
                "rejecting Metis-API-Version=%r (path=%s, reason=%s, caller=%s)",
                resolution.resolved,
                path,
                resolution.reason,
                _caller_fingerprint(scope),
            )
            await _send_unsupported_response(send, resolution)
            return

        if resolution.is_deprecated:
            logger.warning(
                "client pinned to deprecated Metis-API-Version=%r (path=%s, sunset=%s, caller=%s)",
                resolution.resolved,
                path,
                resolution.sunset,
                _caller_fingerprint(scope),
            )

        method = scope.get("method", "")
        if method == "OPTIONS":
            await _send_options_response(send, resolution)
            return

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((VERSION_HEADER_NAME, resolution.resolved.encode("latin-1")))
                headers.append((VERSIONS_SUPPORTED_HEADER_NAME, _versions_supported_value()))
                if resolution.is_deprecated:
                    headers.append((DEPRECATION_HEADER_NAME, b"true"))
                    if resolution.sunset:
                        headers.append((SUNSET_HEADER_NAME, resolution.sunset.encode("latin-1")))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)


__all__ = [
    "CURRENT_VERSION",
    "DEFAULT_BELOW_MIN_SUNSET",
    "DEPRECATED_VERSIONS",
    "DEPRECATION_HEADER_NAME",
    "MIN_SUPPORTED_VERSION",
    "PROVIDER_SHAPE_PREFIXES",
    "SUNSET_HEADER_NAME",
    "SUPPORTED_VERSIONS",
    "VERSIONS_SUPPORTED_HEADER_NAME",
    "VERSION_HEADER_NAME",
    "VersionResolution",
    "VersioningMiddleware",
    "resolve_version",
]
