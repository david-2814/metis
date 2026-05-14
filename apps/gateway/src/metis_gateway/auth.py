"""Gateway-key authentication.

A keystore is a JSON file (default `~/.metis/gateway/keys.json`) listing the
gateway keys the operator has issued. Each entry records the SHA-256 hash of
the bearer token, a stable `key_id` (used in trace events for cost
attribution), and the workspace the key is scoped to.

v1 maps each key to exactly one workspace. Multi-workspace per key is
Phase 3 (gateway.md §11).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class GatewayKey:
    """A single configured gateway key.

    `secret_hash` is the SHA-256 hex digest of the full bearer token (the
    `gw_<ulid>` string the client sends in `Authorization: Bearer ...`).
    The plaintext token is never stored.
    """

    key_id: str
    secret_hash: str
    name: str
    workspace_path: str
    allowed_models: tuple[str, ...] | None = None
    daily_cap_usd: float | None = None


class KeystoreError(Exception):
    """Raised when the keystore file is missing, malformed, or empty."""


class Keystore:
    """In-memory index of issued gateway keys, looked up by bearer-token hash."""

    def __init__(self, keys: list[GatewayKey]) -> None:
        self._by_hash: dict[str, GatewayKey] = {k.secret_hash: k for k in keys}
        self._by_id: dict[str, GatewayKey] = {k.key_id: k for k in keys}

    @classmethod
    def from_file(cls, path: Path) -> Keystore:
        if not path.exists():
            raise KeystoreError(f"gateway keystore not found: {path}")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise KeystoreError(f"gateway keystore {path} is not valid JSON: {exc}") from exc
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Keystore:
        entries = raw.get("keys")
        if not isinstance(entries, list) or not entries:
            raise KeystoreError("keystore must contain a non-empty 'keys' array")
        keys: list[GatewayKey] = []
        seen_ids: set[str] = set()
        seen_hashes: set[str] = set()
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise KeystoreError(f"keystore keys[{index}] must be an object")
            try:
                key_id = str(entry["key_id"])
                secret_hash = str(entry["secret_hash"]).lower()
                name = str(entry.get("name", key_id))
                workspace_path = str(entry["workspace_path"])
            except KeyError as exc:
                raise KeystoreError(
                    f"keystore keys[{index}] missing required field {exc.args[0]!r}"
                ) from exc
            if not workspace_path:
                raise KeystoreError(f"keystore keys[{index}] workspace_path is empty")
            if key_id in seen_ids:
                raise KeystoreError(f"duplicate key_id {key_id!r} in keystore")
            if secret_hash in seen_hashes:
                raise KeystoreError(f"duplicate secret_hash for key {key_id!r}")
            seen_ids.add(key_id)
            seen_hashes.add(secret_hash)
            allowed = entry.get("allowed_models")
            allowed_tuple: tuple[str, ...] | None = None
            if allowed is not None:
                if not isinstance(allowed, list):
                    raise KeystoreError(f"keystore keys[{index}].allowed_models must be a list")
                allowed_tuple = tuple(str(m) for m in allowed)
            daily_cap = entry.get("daily_cap_usd")
            if daily_cap is not None and not isinstance(daily_cap, (int, float)):
                raise KeystoreError(f"keystore keys[{index}].daily_cap_usd must be numeric")
            keys.append(
                GatewayKey(
                    key_id=key_id,
                    secret_hash=secret_hash,
                    name=name,
                    workspace_path=workspace_path,
                    allowed_models=allowed_tuple,
                    daily_cap_usd=float(daily_cap) if daily_cap is not None else None,
                )
            )
        return cls(keys)

    def authenticate(self, bearer_token: str) -> GatewayKey | None:
        if not bearer_token:
            return None
        digest = hashlib.sha256(bearer_token.encode("utf-8")).hexdigest()
        return self._by_hash.get(digest)

    def get_by_id(self, key_id: str) -> GatewayKey | None:
        return self._by_id.get(key_id)

    def __len__(self) -> int:
        return len(self._by_hash)


def hash_bearer_token(token: str) -> str:
    """Compute the SHA-256 hex digest used as `secret_hash` in the keystore."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def extract_bearer_token(authorization_header: str | None) -> str | None:
    """Parse an `Authorization: Bearer <token>` header.

    Returns the raw token or None when the header is missing or shaped wrong.
    The token's `gw_` prefix is not enforced here; the keystore lookup is the
    authority on whether a string is a configured key.
    """
    if not authorization_header:
        return None
    parts = authorization_header.split(None, 1)
    if len(parts) != 2:
        return None
    scheme, value = parts
    if scheme.lower() != "bearer":
        return None
    return value.strip() or None
