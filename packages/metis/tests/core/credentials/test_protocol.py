"""Tests for the shared Protocol surface — truncation + enum membership."""

from __future__ import annotations

from urllib.parse import urlparse

from metis.core.credentials import (
    KNOWN_PROVIDERS,
    CredentialSource,
    provider_names,
    truncate_key,
)


def test_truncate_key_normal_case() -> None:
    assert truncate_key("sk-ant-1234567890wxyz") == "sk-ant-1...wxyz"


def test_truncate_key_short_strings_collapse_to_stars() -> None:
    assert truncate_key("short") == "***"
    assert truncate_key("12345678901") == "***"  # 11 chars (< 12 threshold)


def test_truncate_key_empty() -> None:
    assert truncate_key("") == "<empty>"


def test_truncate_key_minimum_full_form() -> None:
    # 12 chars exactly: first 8 + last 4, with overlap allowed (no requirement
    # of non-overlapping windows for truncation legibility).
    truncated = truncate_key("abcdefghwxyz")
    assert truncated.startswith("abcdefgh")
    assert truncated.endswith("wxyz")


def test_known_providers_table_includes_three_v1_providers() -> None:
    assert set(KNOWN_PROVIDERS.keys()) == {"anthropic", "openai", "openrouter"}
    assert provider_names() == ["anthropic", "openai", "openrouter"]


def test_provider_spec_carries_env_var_and_validate_endpoint() -> None:
    spec = KNOWN_PROVIDERS["anthropic"]
    assert spec.env_var == "ANTHROPIC_API_KEY"
    method, url, body = spec.validate_endpoint
    assert method == "POST"
    parsed = urlparse(url)
    assert parsed.scheme == "https"
    assert parsed.hostname == "api.anthropic.com"
    assert body is not None
    assert body["max_tokens"] == 1


def test_credential_source_enum_includes_keychain_for_forward_compat() -> None:
    # Spec §8 v1.0 acceptance criterion: KEYCHAIN exists as a value even
    # though v1 never emits it; future resolvers compose without breaking.
    assert "keychain" in {s.value for s in CredentialSource}
