"""Pure ASGI middleware that stamps `Metis-API-Version` on responses.

See `docs/specs/api-versioning.md` for the contract. Two surface categories:

* **Provider-shape paths** (`/v1/chat/completions`, `/v1/messages`) — passed
  through untouched. Their version is whatever the upstream provider's SDK
  contract says; Metis doesn't get a vote.
* **Metis-owned paths** (everything else served by the gateway, e.g.
  `/healthz`) — the middleware resolves a version from the request header
  (default `CURRENT_VERSION`), echoes it on the response, and stamps
  `Deprecation` / `Sunset` headers when the resolved version is end-of-life.

Pure ASGI rather than `BaseHTTPMiddleware` so SSE responses don't get
buffered (api-versioning.md §4 / §5).
"""

from __future__ import annotations

import logging

from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

#: Current version of the Metis-owned HTTP surface. Absent-header requests
#: resolve to this.
CURRENT_VERSION = "1.0"

#: Floor of unconditionally supported versions. A request pinning a version
#: below this floor is still served, but the response carries
#: `Deprecation: true`.
MIN_SUPPORTED_VERSION = "1.0"

#: Versions explicitly marked deprecated, mapping `version → ISO sunset date`.
#: Empty in v1; populated when a major bump ships.
DEPRECATED_VERSIONS: dict[str, str] = {}

#: Sunset stamped on responses to versions that parse below
#: `MIN_SUPPORTED_VERSION` but aren't explicitly listed in
#: `DEPRECATED_VERSIONS`. Six months past the v1 spec date.
DEFAULT_BELOW_MIN_SUNSET = "2026-11-15"

#: Provider-shape paths (gateway.md §3.1). The middleware passes these
#: through untouched.
PROVIDER_SHAPE_PREFIXES: tuple[str, ...] = (
    "/v1/chat/completions",
    "/v1/messages",
)

VERSION_HEADER_NAME = b"metis-api-version"
DEPRECATION_HEADER_NAME = b"deprecation"
SUNSET_HEADER_NAME = b"sunset"


def _parse_semver(value: str) -> tuple[int, ...] | None:
    """Best-effort numeric tuple for comparison; ``None`` if it doesn't parse."""
    try:
        return tuple(int(part) for part in value.split("."))
    except ValueError:
        return None


def resolve_version(requested: str | None) -> tuple[str, bool, str | None]:
    """Return ``(resolved_version, is_deprecated, sunset_date_iso)``.

    Resolution order:

    * Absent / empty header → ``CURRENT_VERSION``, not deprecated.
    * Listed in ``DEPRECATED_VERSIONS`` → echoed, deprecated, mapped sunset.
    * Parses below ``MIN_SUPPORTED_VERSION`` → echoed, deprecated, generic sunset.
    * Otherwise → echoed back as-is, not deprecated.
    """
    value = (requested or "").strip()
    if not value:
        return CURRENT_VERSION, False, None
    if value in DEPRECATED_VERSIONS:
        return value, True, DEPRECATED_VERSIONS[value]
    requested_tuple = _parse_semver(value)
    floor_tuple = _parse_semver(MIN_SUPPORTED_VERSION)
    if requested_tuple is not None and floor_tuple is not None and requested_tuple < floor_tuple:
        return value, True, DEFAULT_BELOW_MIN_SUNSET
    return value, False, None


def _is_skipped_path(path: str, prefixes: tuple[str, ...]) -> bool:
    return any(path == prefix or path.startswith(prefix + "/") for prefix in prefixes)


def _request_version_header(scope: Scope) -> str | None:
    for name, value in scope.get("headers", []):
        if name == VERSION_HEADER_NAME:
            return value.decode("latin-1")
    return None


class VersioningMiddleware:
    """Stamps `Metis-API-Version` on Metis-owned responses.

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
        resolved, is_deprecated, sunset = resolve_version(requested)

        if is_deprecated:
            logger.warning(
                "client pinned to deprecated Metis-API-Version=%r (path=%s, sunset=%s)",
                resolved,
                path,
                sunset,
            )

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((VERSION_HEADER_NAME, resolved.encode("latin-1")))
                if is_deprecated:
                    headers.append((DEPRECATION_HEADER_NAME, b"true"))
                    if sunset:
                        headers.append((SUNSET_HEADER_NAME, sunset.encode("latin-1")))
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
    "VERSION_HEADER_NAME",
    "VersioningMiddleware",
    "resolve_version",
]
