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

Wave 10 (gateway.md §11) added two side effects: keystore writes are now
atomic (write-temp-then-rename so a concurrent `metis gateway` reader never
sees a partial JSON file) and successful issuance emits one
`gateway.key_issued` audit event when a trace DB is configured.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from metis_core.canonical.ids import next_monotonic_ulid
from metis_core.events.envelope import Actor
from metis_core.events.payloads import GatewayKeyIssued, make_event

from metis_gateway.auth import hash_bearer_token, validate_cap_usd, validate_identity_tag


class IssueKeyError(Exception):
    """Raised when keystore I/O or input validation fails."""


def build_new_key_record(
    *,
    name: str,
    workspace_path: str,
    allowed_models: tuple[str, ...] | None = None,
    daily_cap_usd: Decimal | float | str | None = None,
    monthly_cap_usd: Decimal | float | str | None = None,
    user_id: str | None = None,
    team_id: str | None = None,
    now: datetime | None = None,
) -> tuple[dict[str, Any], str]:
    """Mint a new key record + plaintext token (without touching the keystore file).

    Returns `(record_dict, plaintext_token)`. The record dict is the JSON
    shape `Keystore.from_dict` expects — caller appends it to the keystore
    and persists. `now` defaults to the current UTC time; callers that want
    a deterministic `created_at` (e.g. rotate-key tests) can supply one.
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

    key_ulid = next_monotonic_ulid()
    key_id = f"gk_{key_ulid}"
    plaintext = f"gw_{next_monotonic_ulid()}"
    issued_at = (now if now is not None else datetime.now(UTC)).astimezone(UTC)
    record: dict[str, Any] = {
        "key_id": key_id,
        "secret_hash": hash_bearer_token(plaintext),
        "name": name,
        "workspace_path": workspace,
        "created_at": issued_at.isoformat(),
    }
    if allowed_models:
        record["allowed_models"] = list(allowed_models)
    if daily_cap_decimal is not None:
        # Persist as the canonical Decimal-as-string shape so reload via
        # `Keystore.from_dict` round-trips without float drift.
        record["daily_cap_usd"] = format(daily_cap_decimal, "f")
    if monthly_cap_decimal is not None:
        record["monthly_cap_usd"] = format(monthly_cap_decimal, "f")
    if user_id is not None:
        record["user_id"] = user_id
    if team_id is not None:
        record["team_id"] = team_id

    return record, plaintext


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
    now: datetime | None = None,
    db_path: Path | None = None,
) -> tuple[str, str]:
    """Append a new key to the keystore and return `(key_id, plaintext_token)`.

    The keystore file is created (with mode 0o600) when missing. Writes go
    through an atomic write-temp-then-rename so a concurrent reader (the
    running gateway) cannot observe a partial file.
    """
    # Lazy import — keystore_admin imports issue_key for the rotation path,
    # so we sidestep the import cycle.
    from metis_gateway.keystore_admin import atomic_write_keystore

    record, plaintext = build_new_key_record(
        name=name,
        workspace_path=workspace_path,
        allowed_models=allowed_models,
        daily_cap_usd=daily_cap_usd,
        monthly_cap_usd=monthly_cap_usd,
        user_id=user_id,
        team_id=team_id,
        now=now,
    )

    keystore_path = keystore_path.expanduser()
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

    keys_list = raw["keys"]
    assert isinstance(keys_list, list)
    keys_list.append(record)
    atomic_write_keystore(keystore_path, raw)

    if db_path is not None:
        _emit_issued_event(record=record, db_path=db_path)

    return record["key_id"], plaintext


def _emit_issued_event(*, record: dict[str, Any], db_path: Path) -> None:
    """Best-effort emit of `gateway.key_issued` to the trace DB.

    Failures don't bubble: issuance has already succeeded by the time we
    reach this path, and the keystore file is the durable record.
    """
    from metis_gateway.keystore_admin import _ADMIN_SESSION_ID, _emit_audit_event

    issued_at_raw = record.get("created_at")
    issued_at = (
        datetime.fromisoformat(issued_at_raw)
        if isinstance(issued_at_raw, str)
        else datetime.now(UTC)
    )

    daily_cap = record.get("daily_cap_usd")
    monthly_cap = record.get("monthly_cap_usd")
    payload = GatewayKeyIssued(
        gateway_key_id=str(record["key_id"]),
        name=str(record.get("name", record["key_id"])),
        workspace_path=str(record["workspace_path"]),
        issued_at=issued_at,
        user_id=record.get("user_id"),
        team_id=record.get("team_id"),
        allowed_models=list(record["allowed_models"]) if record.get("allowed_models") else None,
        daily_cap_usd=Decimal(str(daily_cap)) if daily_cap is not None else None,
        monthly_cap_usd=Decimal(str(monthly_cap)) if monthly_cap is not None else None,
    )
    _emit_audit_event(
        db_path=db_path,
        event=make_event(
            type="gateway.key_issued",
            session_id=_ADMIN_SESSION_ID,
            actor=Actor.SYSTEM,
            timestamp=issued_at,
            payload=payload,
        ),
    )


def _serialize_entry(record: dict[str, Any]) -> dict[str, Any]:
    """Back-compat shim — `keystore_admin.rotate_key` imports this.

    The record is already JSON-shaped (build_new_key_record returns a dict).
    Kept as a passthrough so the import contract is stable.
    """
    return record


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
    db_path: Path | None = None,
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
            db_path=db_path,
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
