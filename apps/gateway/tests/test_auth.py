"""Keystore + bearer-token parsing tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from metis_gateway.auth import (
    GatewayKey,
    Identity,
    Keystore,
    KeystoreError,
    extract_bearer_token,
    hash_bearer_token,
    identity_from_key,
    validate_identity_tag,
)


def test_extract_bearer_token_happy_path() -> None:
    assert extract_bearer_token("Bearer gw_abc123") == "gw_abc123"


def test_extract_bearer_token_case_insensitive_scheme() -> None:
    assert extract_bearer_token("bearer gw_abc") == "gw_abc"


def test_extract_bearer_token_rejects_other_schemes() -> None:
    assert extract_bearer_token("Basic abc") is None


def test_extract_bearer_token_missing_header() -> None:
    assert extract_bearer_token(None) is None
    assert extract_bearer_token("") is None


def test_extract_bearer_token_malformed() -> None:
    assert extract_bearer_token("Bearer") is None
    assert extract_bearer_token("Bearer ") is None


def test_authenticate_matches_hashed_secret() -> None:
    token = "gw_my_token"
    key = GatewayKey(
        key_id="gk_1",
        secret_hash=hash_bearer_token(token),
        name="t",
        workspace_path="/tmp",
    )
    store = Keystore([key])
    matched = store.authenticate(token)
    assert matched is not None
    assert matched.key_id == "gk_1"


def test_authenticate_rejects_wrong_token() -> None:
    key = GatewayKey(
        key_id="gk_1",
        secret_hash=hash_bearer_token("gw_real"),
        name="t",
        workspace_path="/tmp",
    )
    store = Keystore([key])
    assert store.authenticate("gw_wrong") is None
    assert store.authenticate("") is None


def test_keystore_from_dict_validates_required_fields() -> None:
    with pytest.raises(KeystoreError, match="must contain a non-empty 'keys'"):
        Keystore.from_dict({"keys": []})
    with pytest.raises(KeystoreError, match="missing required field"):
        Keystore.from_dict({"keys": [{"key_id": "x"}]})


def test_keystore_from_dict_rejects_duplicate_ids() -> None:
    with pytest.raises(KeystoreError, match="duplicate key_id"):
        Keystore.from_dict(
            {
                "keys": [
                    {
                        "key_id": "gk_dup",
                        "secret_hash": "a" * 64,
                        "workspace_path": "/tmp",
                    },
                    {
                        "key_id": "gk_dup",
                        "secret_hash": "b" * 64,
                        "workspace_path": "/tmp",
                    },
                ]
            }
        )


def test_keystore_from_dict_rejects_duplicate_hashes() -> None:
    with pytest.raises(KeystoreError, match="duplicate secret_hash"):
        Keystore.from_dict(
            {
                "keys": [
                    {
                        "key_id": "gk_a",
                        "secret_hash": "a" * 64,
                        "workspace_path": "/tmp",
                    },
                    {
                        "key_id": "gk_b",
                        "secret_hash": "a" * 64,
                        "workspace_path": "/tmp",
                    },
                ]
            }
        )


def test_keystore_from_file_happy_path(tmp_path: Path) -> None:
    keystore_path = tmp_path / "keys.json"
    keystore_path.write_text(
        json.dumps(
            {
                "keys": [
                    {
                        "key_id": "gk_one",
                        "secret_hash": hash_bearer_token("gw_one"),
                        "workspace_path": "/tmp",
                        "name": "alpha",
                        "allowed_models": ["anthropic:claude-haiku-4-5"],
                        "daily_cap_usd": 25.0,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    store = Keystore.from_file(keystore_path)
    matched = store.authenticate("gw_one")
    assert matched is not None
    assert matched.allowed_models == ("anthropic:claude-haiku-4-5",)
    assert matched.daily_cap_usd == 25.0


def test_keystore_from_file_missing(tmp_path: Path) -> None:
    with pytest.raises(KeystoreError, match="not found"):
        Keystore.from_file(tmp_path / "missing.json")


def test_keystore_from_file_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(KeystoreError, match="not valid JSON"):
        Keystore.from_file(path)


# ---------------------------------------------------------------------------
# Multi-user identity (multi-user.md §3, §4)
# ---------------------------------------------------------------------------


def test_validate_identity_tag_accepts_lowercase_slug() -> None:
    assert validate_identity_tag("alice", field_name="--user") == "alice"
    assert validate_identity_tag("eng-platform", field_name="--team") == "eng-platform"
    assert validate_identity_tag("user_42", field_name="--user") == "user_42"


def test_validate_identity_tag_rejects_uppercase() -> None:
    with pytest.raises(ValueError, match="lowercase alphanumerics"):
        validate_identity_tag("Alice", field_name="--user")


def test_validate_identity_tag_rejects_special_chars() -> None:
    for bad in ("alice@org", "alice.smith", "alice/bob", "alice space", "alice;DROP"):
        with pytest.raises(ValueError):
            validate_identity_tag(bad, field_name="--user")


def test_validate_identity_tag_rejects_empty() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        validate_identity_tag("", field_name="--user")


def test_validate_identity_tag_enforces_length_cap() -> None:
    with pytest.raises(ValueError, match="at most"):
        validate_identity_tag("a" * 201, field_name="--user")


def test_gateway_key_defaults_have_no_identity() -> None:
    """Existing v1 keys (no `--user` / `--team`) default to `None` on both."""
    key = GatewayKey(
        key_id="gk_legacy",
        secret_hash=hash_bearer_token("gw_legacy"),
        name="legacy",
        workspace_path="/tmp",
    )
    assert key.user_id is None
    assert key.team_id is None


def test_keystore_back_compat_old_keystore_loads_cleanly(tmp_path: Path) -> None:
    """multi-user.md §4.1 — pre-v1 keystores (no user_id/team_id) load cleanly,
    the identity fields default to `None`, and the key still authenticates."""
    path = tmp_path / "keys.json"
    path.write_text(
        json.dumps(
            {
                "keys": [
                    {
                        "key_id": "gk_legacy",
                        "secret_hash": hash_bearer_token("gw_legacy"),
                        "workspace_path": "/tmp",
                        "name": "legacy",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    store = Keystore.from_file(path)
    matched = store.authenticate("gw_legacy")
    assert matched is not None
    assert matched.user_id is None
    assert matched.team_id is None


def test_keystore_loads_keys_with_identity_tags(tmp_path: Path) -> None:
    """Tagged keys round-trip: load preserves `user_id` and `team_id`."""
    path = tmp_path / "keys.json"
    path.write_text(
        json.dumps(
            {
                "keys": [
                    {
                        "key_id": "gk_alice",
                        "secret_hash": hash_bearer_token("gw_alice"),
                        "workspace_path": "/tmp",
                        "name": "alice",
                        "user_id": "alice",
                        "team_id": "eng",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    store = Keystore.from_file(path)
    matched = store.authenticate("gw_alice")
    assert matched is not None
    assert matched.user_id == "alice"
    assert matched.team_id == "eng"


def test_keystore_rejects_invalid_user_id() -> None:
    """Identity-tag validation runs at keystore load time so a hand-edited
    file with a bad tag is caught instead of leaking into trace events."""
    with pytest.raises(KeystoreError, match="lowercase alphanumerics"):
        Keystore.from_dict(
            {
                "keys": [
                    {
                        "key_id": "gk_bad",
                        "secret_hash": "a" * 64,
                        "workspace_path": "/tmp",
                        "user_id": "Alice With Spaces",
                    }
                ]
            }
        )


def test_keystore_rejects_non_string_team_id() -> None:
    with pytest.raises(KeystoreError, match="must be a string"):
        Keystore.from_dict(
            {
                "keys": [
                    {
                        "key_id": "gk_bad",
                        "secret_hash": "a" * 64,
                        "workspace_path": "/tmp",
                        "team_id": 42,
                    }
                ]
            }
        )


def test_identify_returns_full_identity() -> None:
    """`Keystore.identify` projects the resolved key onto the request-scoped
    `Identity` (multi-user.md §3.2 — the `Principal` projection)."""
    token = "gw_alice_token"
    key = GatewayKey(
        key_id="gk_alice",
        secret_hash=hash_bearer_token(token),
        name="alice-claude-code",
        workspace_path="/Users/alice/repo",
        user_id="alice",
        team_id="eng",
    )
    store = Keystore([key])
    identity = store.identify(token)
    assert identity == Identity(
        gateway_key_id="gk_alice",
        workspace_path="/Users/alice/repo",
        user_id="alice",
        team_id="eng",
    )


def test_identify_returns_none_for_unknown_token() -> None:
    key = GatewayKey(
        key_id="gk_x",
        secret_hash=hash_bearer_token("gw_real"),
        name="x",
        workspace_path="/tmp",
    )
    store = Keystore([key])
    assert store.identify("gw_wrong") is None
    assert store.identify("") is None


def test_identify_preserves_null_identity_for_v1_keys() -> None:
    """A pre-multi-user key resolves to an Identity with `None` user/team."""
    token = "gw_legacy"
    key = GatewayKey(
        key_id="gk_legacy",
        secret_hash=hash_bearer_token(token),
        name="legacy",
        workspace_path="/tmp",
    )
    store = Keystore([key])
    identity = store.identify(token)
    assert identity is not None
    assert identity.gateway_key_id == "gk_legacy"
    assert identity.user_id is None
    assert identity.team_id is None


def test_identity_from_key_matches_identify() -> None:
    """The projection helper is the same shape as `Keystore.identify`."""
    key = GatewayKey(
        key_id="gk_one",
        secret_hash="a" * 64,
        name="one",
        workspace_path="/tmp",
        user_id="bob",
        team_id="ops",
    )
    assert identity_from_key(key) == Identity(
        gateway_key_id="gk_one",
        workspace_path="/tmp",
        user_id="bob",
        team_id="ops",
    )
