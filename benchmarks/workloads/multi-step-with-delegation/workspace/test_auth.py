"""Tests for the auth/ module. Importable from workspace root.

After the refactor, every provider's `authenticate(credentials)` should
still return an `AuthResult` whose fields match these expectations. The
tests do NOT assert on internal class hierarchy, so any of {shared base
class / shared Protocol / shared helper function} satisfies them.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from auth.registry import ProviderRegistry  # noqa: E402


def test_password_success() -> None:
    reg = ProviderRegistry()
    r = reg.authenticate("password", {"username": "alice", "password": "hunter2"})
    assert r.success is True
    assert r.user_id == "alice"


def test_password_bad_password() -> None:
    reg = ProviderRegistry()
    r = reg.authenticate("password", {"username": "alice", "password": "wrong"})
    assert r.success is False
    assert r.error == "bad_password"


def test_password_missing_field() -> None:
    reg = ProviderRegistry()
    r = reg.authenticate("password", {"username": "alice"})
    assert r.success is False
    assert r.error == "missing_fields"


def test_oauth_success() -> None:
    reg = ProviderRegistry()
    r = reg.authenticate("oauth", {"token": "tok_alice_001"})
    assert r.success is True
    assert r.user_id == "alice"


def test_oauth_expired() -> None:
    reg = ProviderRegistry()
    r = reg.authenticate("oauth", {"token": "tok_expired_999"})
    assert r.success is False
    assert r.error == "expired_token"


def test_apikey_success() -> None:
    reg = ProviderRegistry()
    r = reg.authenticate("apikey", {"api_key": "ak_alice_live_001"})
    assert r.success is True
    assert r.user_id == "alice"


def test_apikey_unknown() -> None:
    reg = ProviderRegistry()
    r = reg.authenticate("apikey", {"api_key": "ak_nobody"})
    assert r.success is False
    assert r.error == "unknown_api_key"


def test_invalid_shape() -> None:
    reg = ProviderRegistry()
    r = reg.authenticate("password", "not a dict")
    assert r.success is False
    assert r.error == "invalid_credentials_shape"
