"""`metis gateway issue-key` — create a gateway key and append it to the keystore.

Generates a `gw_<ulid>` bearer token, hashes it, appends an entry to the
keystore JSON (creating the file on first use), and prints the plaintext
token to stdout exactly once. The plaintext is never persisted — only the
SHA-256 digest is stored.

Per `gateway.md §V`: each key maps to one workspace (v1) and may optionally
carry an `allowed_models` list and a `daily_cap_usd`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from metis_core.canonical.ids import next_monotonic_ulid

from metis_gateway.auth import hash_bearer_token


class IssueKeyError(Exception):
    """Raised when keystore I/O or input validation fails."""


def issue_key(
    *,
    keystore_path: Path,
    name: str,
    workspace_path: str,
    allowed_models: tuple[str, ...] | None = None,
    daily_cap_usd: float | None = None,
) -> tuple[str, str]:
    """Append a new key to the keystore and return `(key_id, plaintext_token)`.

    The keystore file is created (with mode 0o600) when missing.
    """
    if not name:
        raise IssueKeyError("--name is required and must be non-empty")
    workspace = str(Path(workspace_path).expanduser().resolve())
    if not workspace:
        raise IssueKeyError("--workspace is required")

    keystore_path = keystore_path.expanduser()
    keystore_path.parent.mkdir(parents=True, exist_ok=True)

    raw: dict[str, Any]
    if keystore_path.exists():
        try:
            raw = json.loads(keystore_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise IssueKeyError(f"keystore {keystore_path} is not valid JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise IssueKeyError(f"keystore {keystore_path} root must be a JSON object")
        keys = raw.get("keys")
        if keys is None:
            raw["keys"] = []
        elif not isinstance(keys, list):
            raise IssueKeyError(f"keystore {keystore_path} 'keys' field must be an array")
    else:
        raw = {"keys": []}

    key_ulid = next_monotonic_ulid()
    key_id = f"gk_{key_ulid}"
    plaintext = f"gw_{next_monotonic_ulid()}"
    entry: dict[str, Any] = {
        "key_id": key_id,
        "secret_hash": hash_bearer_token(plaintext),
        "name": name,
        "workspace_path": workspace,
    }
    if allowed_models:
        entry["allowed_models"] = list(allowed_models)
    if daily_cap_usd is not None:
        entry["daily_cap_usd"] = float(daily_cap_usd)

    keys_list = raw["keys"]
    assert isinstance(keys_list, list)
    keys_list.append(entry)

    keystore_path.write_text(json.dumps(raw, indent=2, sort_keys=True), encoding="utf-8")
    try:
        keystore_path.chmod(0o600)
    except OSError:
        pass

    return key_id, plaintext


def issue_key_command(
    *,
    keystore_path: Path,
    name: str,
    workspace_path: str,
    allowed_models: tuple[str, ...] | None = None,
    daily_cap_usd: float | None = None,
) -> int:
    """CLI shim: prints the plaintext token once and returns a Unix exit code."""
    try:
        key_id, plaintext = issue_key(
            keystore_path=keystore_path,
            name=name,
            workspace_path=workspace_path,
            allowed_models=allowed_models,
            daily_cap_usd=daily_cap_usd,
        )
    except IssueKeyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"key_id: {key_id}")
    print(f"token:  {plaintext}")
    print(
        "save the token now — only the hash is persisted, and it cannot be recovered.",
        file=sys.stderr,
    )
    return 0
