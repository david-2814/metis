"""Exception types raised by the credential resolver.

Kept separate from `protocol.py` so callers can `except CredentialsFileInsecure`
without pulling in the Protocol surface.
"""

from __future__ import annotations

from pathlib import Path


class CredentialError(Exception):
    """Base for every credentials-module failure."""


class CredentialNotFoundError(CredentialError):
    """No source on the resolution chain provided a key for the requested provider.

    Carries the provider name so the caller can render a targeted hint
    (`metis auth add <provider>`).
    """

    def __init__(self, provider: str, *, hint: str | None = None) -> None:
        message = f"no credentials configured for {provider}."
        if hint is not None:
            message = f"{message} {hint}"
        super().__init__(message)
        self.provider = provider


class CredentialsFileInsecure(CredentialError):
    """The credentials file exists but its mode is wider than 0o600.

    Raised by `CredentialsFile.load(path)` BEFORE the file is read — we
    refuse to expose keys from a world-readable file. The exception message
    includes the path and a `chmod` hint so the user can fix it without
    consulting the spec.
    """

    def __init__(self, path: Path, *, mode: int) -> None:
        super().__init__(
            f"credentials file {path} has insecure permissions (mode {mode:04o}); "
            f"run `chmod 600 {path}` and retry"
        )
        self.path = path
        self.mode = mode


class CredentialsFileSchemaUnknown(CredentialError):
    """The credentials file's `schema_version` is not a version this code understands.

    Forward-only migration: v1 code refuses to load a v2 file (per spec §4 +
    §9 Q4). This is preferable to silently dropping unknown fields.
    """

    def __init__(self, path: Path, *, schema_version: object, supported: tuple[int, ...]) -> None:
        super().__init__(
            f"credentials file {path} has schema_version={schema_version!r}; "
            f"this build only understands schema_version in {list(supported)}. "
            "Upgrade Metis or remove the file."
        )
        self.path = path
        self.schema_version = schema_version
        self.supported = supported
