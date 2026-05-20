"""Credential resolution: per-provider API-key lookup chain.

See `docs/specs/credentials.md`. Provider adapters never touch this
module — the runtime (`metis.cli.runtime.setup_runtime` /
`metis.gateway.runtime.setup_gateway_runtime`) calls `resolver.get(name)`
once per provider at startup and threads the resolved `str` key into the
appropriate adapter constructor.
"""

from __future__ import annotations

from metis.core.credentials.errors import (
    CredentialError,
    CredentialNotFoundError,
    CredentialsFileInsecure,
    CredentialsFileSchemaUnknown,
)
from metis.core.credentials.file import (
    CURRENT_SCHEMA_VERSION,
    REQUIRED_FILE_MODE,
    SUPPORTED_SCHEMA_VERSIONS,
    CredentialsFile,
)
from metis.core.credentials.protocol import (
    ConfiguredCredential,
    CredentialResolver,
    CredentialSource,
    ProviderSpec,
    truncate_key,
)
from metis.core.credentials.providers import KNOWN_PROVIDERS, provider_names
from metis.core.credentials.resolver import (
    DefaultCredentialResolver,
    default_credentials_file_path,
    default_legacy_dotenv_path,
    hint_for_missing_provider,
)

__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "KNOWN_PROVIDERS",
    "REQUIRED_FILE_MODE",
    "SUPPORTED_SCHEMA_VERSIONS",
    "ConfiguredCredential",
    "CredentialError",
    "CredentialNotFoundError",
    "CredentialResolver",
    "CredentialSource",
    "CredentialsFile",
    "CredentialsFileInsecure",
    "CredentialsFileSchemaUnknown",
    "DefaultCredentialResolver",
    "ProviderSpec",
    "default_credentials_file_path",
    "default_legacy_dotenv_path",
    "hint_for_missing_provider",
    "provider_names",
    "truncate_key",
]
