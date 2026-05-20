"""DefaultCredentialResolver: walks the spec §3 resolution chain.

Single-pass per `get(provider)`:
  1. CLI flag override (constructor-injected dict; per-process)
  2. `${PROVIDER}_API_KEY` env var
  3. `~/.metis/credentials.yaml` (mode 0o600 enforced)
  4. `~/.metis/.env` (legacy dotenv; loaded lazily on first lookup)
  5. OS keychain — deferred (spec §8); the chain has a hook but v1 doesn't
     instantiate any keychain backend.

The resolver is intentionally side-effect-free except for caching the
credentials-file load (parsing YAML on every adapter registration would
be wasteful) and the legacy-dotenv parse. Both caches invalidate on
explicit `reload()`.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from pathlib import Path

from dotenv import dotenv_values

from metis.core.credentials.errors import (
    CredentialsFileInsecure,
    CredentialsFileSchemaUnknown,
)
from metis.core.credentials.file import CredentialsFile
from metis.core.credentials.protocol import (
    ConfiguredCredential,
    CredentialSource,
    truncate_key,
)
from metis.core.credentials.providers import KNOWN_PROVIDERS, provider_names

logger = logging.getLogger(__name__)


def default_credentials_file_path() -> Path:
    return Path.home() / ".metis" / "credentials.yaml"


def default_legacy_dotenv_path() -> Path:
    return Path.home() / ".metis" / ".env"


class DefaultCredentialResolver:
    """Concrete `CredentialResolver` implementing the spec §3 chain.

    All input paths and dictionaries are constructor-injected so tests can
    pin a temporary file path, supply a fake environ, and exercise the
    chain without touching the user's real `~/.metis/`.
    """

    def __init__(
        self,
        *,
        cli_overrides: Mapping[str, str] | None = None,
        env: Mapping[str, str] | None = None,
        file_path: Path | None = None,
        legacy_dotenv_path: Path | None = None,
    ) -> None:
        self._cli = dict(cli_overrides or {})
        self._env: Mapping[str, str] = env if env is not None else os.environ
        self._file_path = (
            file_path.expanduser() if file_path is not None else default_credentials_file_path()
        )
        self._legacy_dotenv_path = (
            legacy_dotenv_path.expanduser()
            if legacy_dotenv_path is not None
            else default_legacy_dotenv_path()
        )
        # Caches; populated lazily so import-time has no I/O.
        self._file_cache: CredentialsFile | None = None
        self._file_loaded: bool = False
        self._file_load_error: Exception | None = None
        self._legacy_env_cache: dict[str, str | None] | None = None

    # ---- Public API per CredentialResolver Protocol --------------------

    def get(self, provider: str) -> str | None:
        key, _ = self._resolve(provider)
        return key

    def list_configured(self) -> list[ConfiguredCredential]:
        """One entry per KNOWN_PROVIDERS provider that resolves to a key.

        Order follows `providers.provider_names()` (insertion order of
        `KNOWN_PROVIDERS`) for stable CLI output. Providers that miss every
        source are omitted; surfacing "(not configured)" is the CLI's job
        (it knows about KNOWN_PROVIDERS too).
        """
        out: list[ConfiguredCredential] = []
        for provider in provider_names():
            key, provenance = self._resolve(provider)
            if key is None or provenance is None:
                continue
            source, source_detail = provenance
            out.append(
                ConfiguredCredential(
                    provider=provider,
                    source=source,
                    source_detail=source_detail,
                    key_truncated=truncate_key(key),
                )
            )
        return out

    # ---- Inspection helpers (used by `metis auth doctor`) --------------

    @property
    def file_path(self) -> Path:
        return self._file_path

    @property
    def legacy_dotenv_path(self) -> Path:
        return self._legacy_dotenv_path

    def file_status(self) -> tuple[bool, str]:
        """Return `(loadable, detail_message)` for the credentials file.

        Used by `metis auth doctor` to surface mode / schema problems
        without raising on the doctor's happy path. `loadable` is True iff
        the file exists, has mode 0o600, and parses; False otherwise.
        `detail_message` is human-readable.
        """
        try:
            self._load_file()
        except FileNotFoundError:
            return False, "(not present)"
        except CredentialsFileInsecure as exc:
            return False, f"insecure mode {exc.mode:04o}"
        except CredentialsFileSchemaUnknown as exc:
            return False, f"unsupported schema_version={exc.schema_version!r}"
        except (ValueError, OSError) as exc:
            return False, f"unreadable: {exc}"
        return True, "readable (mode 0o600)"

    def reload(self) -> None:
        """Drop cached file + legacy-env state. Next `get()` re-reads from disk."""
        self._file_cache = None
        self._file_loaded = False
        self._file_load_error = None
        self._legacy_env_cache = None

    def loaded_file(self) -> CredentialsFile | None:
        """Return the cached credentials file if it loaded cleanly, else None.

        Public accessor for the doctor surface so it can render the
        `default_provider` field without poking at the underscored cache.
        """
        try:
            return self._load_file()
        except (
            FileNotFoundError,
            CredentialsFileInsecure,
            CredentialsFileSchemaUnknown,
            ValueError,
            OSError,
        ):
            return None

    # ---- Resolution chain ---------------------------------------------

    def _resolve(self, provider: str) -> tuple[str | None, tuple[CredentialSource, str] | None]:
        # 1. CLI override
        if provider in self._cli:
            value = self._cli[provider]
            if value:
                return value, (CredentialSource.CLI_FLAG, "cli")

        spec = KNOWN_PROVIDERS.get(provider)
        env_var = spec.env_var if spec is not None else f"{provider.upper()}_API_KEY"

        # 2. Environment variable
        env_value = self._env.get(env_var)
        if env_value:
            return env_value, (CredentialSource.ENV_VAR, env_var)

        # 3. ~/.metis/credentials.yaml
        try:
            file = self._load_file()
        except (
            FileNotFoundError,
            CredentialsFileInsecure,
            CredentialsFileSchemaUnknown,
            ValueError,
            OSError,
        ):
            # File problems are surfaced via `file_status()` for the doctor;
            # the resolver itself just skips the file source and continues
            # the chain. `metis auth list` will reveal nothing-configured
            # if no other source matches.
            file = None
        if file is not None:
            file_key = file.get(provider)
            if file_key:
                detail = self._render_path(file.path)
                return file_key, (CredentialSource.FILE, detail)

        # 4. ~/.metis/.env (legacy dotenv)
        legacy = self._load_legacy_env()
        if legacy is not None:
            legacy_value = legacy.get(env_var)
            if legacy_value:
                detail = self._render_path(self._legacy_dotenv_path)
                return legacy_value, (CredentialSource.LEGACY_DOTENV, detail)

        # 5. Keychain — deferred (spec §8). Hook lives here.
        return None, None

    def _load_file(self) -> CredentialsFile | None:
        """Memoized credentials-file loader.

        Re-raises the first failure encountered (mode / schema / parse) so
        the doctor surface can see it; the resolution chain treats any
        exception as "skip this source".
        """
        if self._file_loaded:
            if self._file_load_error is not None:
                raise self._file_load_error
            return self._file_cache
        self._file_loaded = True
        try:
            self._file_cache = CredentialsFile.load(self._file_path)
            return self._file_cache
        except FileNotFoundError as exc:
            self._file_load_error = exc
            raise
        except (
            CredentialsFileInsecure,
            CredentialsFileSchemaUnknown,
            ValueError,
            OSError,
        ) as exc:
            self._file_load_error = exc
            raise

    def _load_legacy_env(self) -> dict[str, str | None] | None:
        if self._legacy_env_cache is not None:
            return self._legacy_env_cache
        if not self._legacy_dotenv_path.exists():
            return None
        try:
            self._legacy_env_cache = dict(dotenv_values(self._legacy_dotenv_path))
        except OSError as exc:
            logger.warning("failed to read legacy dotenv %s: %s", self._legacy_dotenv_path, exc)
            self._legacy_env_cache = {}
        return self._legacy_env_cache

    @staticmethod
    def _render_path(path: Path) -> str:
        """Render `path` as `~/...` when under the home dir for compact display."""
        try:
            home = Path.home()
            return f"~/{path.relative_to(home)}"
        except (ValueError, RuntimeError):
            return str(path)


def hint_for_missing_provider(provider: str) -> str:
    """Build the canonical 'how to fix it' hint used in error paths."""
    spec = KNOWN_PROVIDERS.get(provider)
    env_var = spec.env_var if spec is not None else f"{provider.upper()}_API_KEY"
    return f"Run `metis auth add {provider}` (or set {env_var} in env / .env)."
