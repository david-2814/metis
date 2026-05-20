"""`~/.metis/credentials.yaml` read / write / mutate.

The on-disk format is YAML per spec §4. `CredentialsFile` is a
load-mutate-save abstraction; the resolver consumes it read-only and the
`metis auth` CLI uses the write path. Writes go through
write-temp-then-rename for atomicity (mirroring `keystore_admin.atomic_write_keystore`)
and re-chmod to 0o600 on each save.

Reads enforce mode 0o600 BEFORE parsing — a credentials file with looser
perms raises `CredentialsFileInsecure` and the resolver propagates that to
the user so they see a `chmod` hint instead of silently leaking keys.
"""

from __future__ import annotations

import os
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from metis.core.credentials.errors import (
    CredentialsFileInsecure,
    CredentialsFileSchemaUnknown,
)

# Schema versions this code understands. Forward-only migration: a v2 file
# refuses to load under a v1-only build (spec §4 + §9 Q4).
SUPPORTED_SCHEMA_VERSIONS: tuple[int, ...] = (1,)
CURRENT_SCHEMA_VERSION: int = 1

REQUIRED_FILE_MODE: int = 0o600


@dataclass
class CredentialsFile:
    """In-memory representation of `~/.metis/credentials.yaml`.

    Mutable (CLI add/remove flows mutate then `save()`); the resolver only
    reads via `get(provider)`. `path` is the source location and gets reused
    by `save()` so callers don't accidentally write to a different file
    than they loaded from.
    """

    path: Path
    schema_version: int = CURRENT_SCHEMA_VERSION
    providers: dict[str, str] = field(default_factory=dict)
    default_provider: str | None = None
    # Reserved for the future OS-keychain resolver (spec §8). Parsed and
    # round-tripped so v1.x can flip it without a schema bump; v1 ignores
    # it for resolution ordering.
    prefer_keychain: bool = False

    # ---- Reads ---------------------------------------------------------

    @classmethod
    def load(cls, path: Path) -> CredentialsFile:
        """Load and validate a credentials file.

        Raises:
            FileNotFoundError: file does not exist.
            CredentialsFileInsecure: mode is wider than 0o600.
            CredentialsFileSchemaUnknown: unsupported schema_version.
            ValueError: malformed YAML or schema.
        """
        path = path.expanduser()
        if not path.exists():
            raise FileNotFoundError(path)
        _enforce_secure_mode(path)
        raw_text = path.read_text(encoding="utf-8")
        try:
            data = yaml.safe_load(raw_text) or {}
        except yaml.YAMLError as exc:
            raise ValueError(f"credentials file {path} is not valid YAML: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError(f"credentials file {path} root must be a YAML mapping")

        schema_version = data.get("schema_version")
        if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
            raise CredentialsFileSchemaUnknown(
                path,
                schema_version=schema_version,
                supported=SUPPORTED_SCHEMA_VERSIONS,
            )

        providers_raw = data.get("providers", {})
        if not isinstance(providers_raw, dict):
            raise ValueError(f"credentials file {path} `providers` must be a mapping")
        providers: dict[str, str] = {}
        for name, entry in providers_raw.items():
            if not isinstance(entry, dict):
                raise ValueError(f"credentials file {path}: providers.{name} must be a mapping")
            key = entry.get("api_key")
            if key is None:
                # Allow declaring a provider with no key (e.g. via the CLI
                # creating a stub); resolver simply won't return one. Keeps
                # `metis auth add` idempotent across partial states.
                continue
            if not isinstance(key, str) or not key:
                raise ValueError(
                    f"credentials file {path}: providers.{name}.api_key must be a non-empty string"
                )
            providers[name] = key

        default_provider = data.get("default_provider")
        if default_provider is not None and not isinstance(default_provider, str):
            raise ValueError(f"credentials file {path}: default_provider must be a string")
        prefer_keychain = bool(data.get("prefer_keychain", False))

        return cls(
            path=path,
            schema_version=int(schema_version),
            providers=providers,
            default_provider=default_provider,
            prefer_keychain=prefer_keychain,
        )

    def get(self, provider: str) -> str | None:
        """Return the api_key for `provider`, or None if not configured."""
        return self.providers.get(provider)

    # ---- Mutations -----------------------------------------------------

    def upsert(self, provider: str, api_key: str) -> None:
        if not provider:
            raise ValueError("provider name is required")
        if not api_key:
            raise ValueError("api_key must be a non-empty string")
        self.providers[provider] = api_key

    def remove(self, provider: str) -> bool:
        """Remove `provider` if present. Returns True if anything was removed."""
        return self.providers.pop(provider, None) is not None

    # ---- Writes --------------------------------------------------------

    def save(self) -> None:
        """Persist to `self.path` via write-temp-then-rename (mode 0o600).

        Pattern mirrors `gateway.keystore_admin.atomic_write_keystore`:
        os.replace is atomic on POSIX so a concurrent reader sees either
        the old file or the new one, never a partial write.
        """
        data: dict[str, Any] = {
            "schema_version": self.schema_version,
            "providers": {name: {"api_key": key} for name, key in sorted(self.providers.items())},
        }
        if self.default_provider is not None:
            data["default_provider"] = self.default_provider
        if self.prefer_keychain:
            data["prefer_keychain"] = True

        path = self.path.expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=".credentials.", suffix=".yaml.tmp", dir=str(path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                yaml.safe_dump(data, fh, sort_keys=True, default_flow_style=False)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        try:
            os.chmod(path, REQUIRED_FILE_MODE)
        except OSError:
            # Best-effort chmod; on filesystems that don't honor it (e.g.
            # some bind mounts, FAT volumes) we'd rather succeed than
            # refuse the write entirely. The next `load()` will surface
            # the mode mismatch via CredentialsFileInsecure.
            pass

    # ---- Helpers -------------------------------------------------------

    @classmethod
    def empty(cls, path: Path) -> CredentialsFile:
        """Build an in-memory empty file at `path` (no I/O)."""
        return cls(path=path.expanduser())


def _enforce_secure_mode(path: Path) -> None:
    """Refuse to read a credentials file with permissions wider than 0o600.

    Mirrors `~/.ssh/id_*` / `~/.aws/credentials` posture (spec §7.1). We
    check the mode bits only — ownership and ACLs are out of scope for v1.
    """
    info = os.stat(path)
    mode = stat.S_IMODE(info.st_mode)
    if mode != REQUIRED_FILE_MODE:
        raise CredentialsFileInsecure(path, mode=mode)
