"""`metis gateway issue-key` — create a gateway key and append it to the keystore.

Generates a `gw_<ulid>` bearer token, hashes it, appends an entry to the
keystore JSON (creating the file on first use), and prints the plaintext
token to stdout exactly once. The plaintext is never persisted — only the
SHA-256 digest is stored.

Per `gateway.md §V`: each key maps to one workspace (v1) and may optionally
carry an `allowed_models` list and a `daily_cap_usd`. Per `multi-user.md §4`
the key may also carry optional `user_id` / `team_id` identity tags; both
are validated against the same `^[a-z0-9_-]+$` shape used elsewhere on the
identity surface.
"""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

from metis_core.canonical.ids import next_monotonic_ulid

from metis_gateway.auth import hash_bearer_token, validate_cap_usd, validate_identity_tag


class IssueKeyError(Exception):
    """Raised when keystore I/O or input validation fails."""


def issue_key(
    *,
    keystore_path: Path,
    name: str,
    workspace_path: str,
    allowed_models: tuple[str, ...] | None = None,
    daily_cap_usd: Decimal | float | None = None,
    monthly_cap_usd: Decimal | float | None = None,
    user_id: str | None = None,
    team_id: str | None = None,
) -> tuple[str, str]:
    """Append a new key to the keystore and return `(key_id, plaintext_token)`.

    The keystore file is created (with mode 0o600) when missing.
    """
    if not name:
        raise IssueKeyError("--name is required and must be non-empty")
    workspace = str(Path(workspace_path).expanduser().resolve())
    if not workspace:
        raise IssueKeyError("--workspace is required")

    if user_id is not None:
        try:
            user_id = validate_identity_tag(user_id, field_name="--user")
        except ValueError as exc:
            raise IssueKeyError(str(exc)) from exc
    if team_id is not None:
        try:
            team_id = validate_identity_tag(team_id, field_name="--team")
        except ValueError as exc:
            raise IssueKeyError(str(exc)) from exc

    daily_cap_decimal: Decimal | None = None
    if daily_cap_usd is not None:
        try:
            daily_cap_decimal = validate_cap_usd(daily_cap_usd, field_name="--daily-cap-usd")
        except ValueError as exc:
            raise IssueKeyError(str(exc)) from exc
    monthly_cap_decimal: Decimal | None = None
    if monthly_cap_usd is not None:
        try:
            monthly_cap_decimal = validate_cap_usd(monthly_cap_usd, field_name="--monthly-cap-usd")
        except ValueError as exc:
            raise IssueKeyError(str(exc)) from exc

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
    if daily_cap_decimal is not None:
        # Persist as the canonical Decimal-as-string shape so reload via
        # `Keystore.from_dict` round-trips without float drift.
        entry["daily_cap_usd"] = format(daily_cap_decimal, "f")
    if monthly_cap_decimal is not None:
        entry["monthly_cap_usd"] = format(monthly_cap_decimal, "f")
    if user_id is not None:
        entry["user_id"] = user_id
    if team_id is not None:
        entry["team_id"] = team_id

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
    daily_cap_usd: Decimal | float | None = None,
    monthly_cap_usd: Decimal | float | None = None,
    user_id: str | None = None,
    team_id: str | None = None,
) -> int:
    """CLI shim: prints the plaintext token once and returns a Unix exit code."""
    try:
        key_id, plaintext = issue_key(
            keystore_path=keystore_path,
            name=name,
            workspace_path=workspace_path,
            allowed_models=allowed_models,
            daily_cap_usd=daily_cap_usd,
            monthly_cap_usd=monthly_cap_usd,
            user_id=user_id,
            team_id=team_id,
        )
    except IssueKeyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"key_id: {key_id}")
    print(f"token:  {plaintext}")
    if user_id is not None:
        print(f"user:   {user_id}")
    if team_id is not None:
        print(f"team:   {team_id}")
    if daily_cap_usd is not None:
        print(f"daily_cap_usd: {daily_cap_usd}")
    if monthly_cap_usd is not None:
        print(f"monthly_cap_usd: {monthly_cap_usd}")
    print(
        "save the token now — only the hash is persisted, and it cannot be recovered.",
        file=sys.stderr,
    )
    return 0
