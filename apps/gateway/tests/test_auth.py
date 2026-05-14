"""Keystore + bearer-token parsing tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from metis_gateway.auth import (
    GatewayKey,
    Keystore,
    KeystoreError,
    extract_bearer_token,
    hash_bearer_token,
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
