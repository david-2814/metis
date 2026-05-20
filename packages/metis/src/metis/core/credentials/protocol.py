"""CredentialResolver Protocol + data shapes shared across the module.

Per `docs/specs/credentials.md §6.1`, the resolver is a small Protocol the
runtime depends on; the default implementation walks the spec §3 resolution
chain. Provider adapters never see the resolver — they receive the resolved
`str` key from whichever runtime instantiated them.

`ConfiguredCredential` and `ProviderSpec` are msgspec frozen Structs per
this repo's convention (AGENTS.md: msgspec, not Pydantic).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol, runtime_checkable

import msgspec


class CredentialSource(StrEnum):
    """Where a resolved key came from. Closed set per spec §3 + §8.

    `KEYCHAIN` is reserved for the future OS-keychain resolver (spec §8);
    v1 never emits it, but the enum value exists so a downstream
    `metis auth list` rendering knows the vocabulary in advance.
    """

    CLI_FLAG = "cli_flag"
    ENV_VAR = "env_var"
    FILE = "file"
    LEGACY_DOTENV = "legacy_dotenv"
    KEYCHAIN = "keychain"


class ConfiguredCredential(msgspec.Struct, frozen=True):
    """One row in `metis auth list` / `doctor`: provider + provenance, no full key.

    `source_detail` describes the specific origin for human-readable output —
    the env-var name (`ANTHROPIC_API_KEY`), the file path
    (`~/.metis/credentials.yaml`), `cli` for a per-invocation override, etc.
    The full key is NEVER stored on this struct (spec §5.2 / §7.2);
    `key_truncated` holds the first-8 + last-4 form for display.
    """

    provider: str
    source: CredentialSource
    source_detail: str
    key_truncated: str


class ProviderSpec(msgspec.Struct, frozen=True):
    """Per-provider metadata: env-var name + validation endpoint.

    `validate_endpoint` is a tuple `(method, url, body_or_none)` — body is a
    JSON dict for POST validations (Anthropic's 1-token messages probe) and
    `None` for GET probes (OpenAI's `/v1/models`, OpenRouter's
    `/api/v1/auth/key`).

    Auth-header shape varies by provider:
    - Anthropic uses `x-api-key: <key>` + `anthropic-version` header
    - OpenAI / OpenRouter use `Authorization: Bearer <key>`

    `auth_header_name` + `auth_header_value_template` encode that split
    without forcing the resolver to know provider-specific HTTP shapes;
    extra_headers carries any provider-required boilerplate
    (`anthropic-version`, etc.).
    """

    env_var: str
    validate_endpoint: tuple[str, str, dict | None]
    auth_header_name: str
    auth_header_value_template: str
    extra_headers: dict[str, str] = msgspec.field(default_factory=dict)


@runtime_checkable
class CredentialResolver(Protocol):
    """Returns API keys for LLM providers. See `docs/specs/credentials.md §6.1`.

    `get(provider)` returns the first non-None match walking the resolution
    chain in spec §3 order. Never raises on missing — callers decide whether
    absence is fatal.

    `list_configured()` returns one entry per provider in `KNOWN_PROVIDERS`
    that resolves to a non-None key, with source provenance but never the
    full key. Drives `metis auth list` and `metis auth doctor` output.
    """

    def get(self, provider: str) -> str | None: ...

    def list_configured(self) -> list[ConfiguredCredential]: ...


# ---------------------------------------------------------------------------
# Display helper. Centralized here so every surface (CLI list, doctor, audit,
# error messages) uses the same truncation rule per spec §5.2 / §7.2.
# ---------------------------------------------------------------------------

_MIN_KEY_LEN_FOR_TRUNCATION = 12  # 8 prefix + 4 suffix


def truncate_key(key: str) -> str:
    """Render `key` as `<first 8>...<last 4>` for human-readable output.

    Keys shorter than 12 characters are rendered as `***` rather than a
    near-complete echo; in practice all real provider keys are well above
    that floor (Anthropic `sk-ant-...`, OpenAI `sk-...`, OpenRouter
    `sk-or-...` are all 50+ chars). Empty input renders as `<empty>`.
    """
    if not key:
        return "<empty>"
    if len(key) < _MIN_KEY_LEN_FOR_TRUNCATION:
        return "***"
    return f"{key[:8]}...{key[-4:]}"
