"""Gateway-key lifecycle operations: revoke, rotate, list.

Mirrors `issue_key.py` for the post-issuance side of the keystore. Each
mutating helper does an atomic write-temp-then-rename so a concurrent reader
(the running gateway) never observes a partial JSON file.

Audit-trail events (`gateway.key_issued` / `gateway.key_revoked` /
`gateway.key_rotated`) are emitted to the trace DB when a `db_path` is
supplied. Emission failures don't abort the keystore mutation — the
keystore file is the durable record; the trace event is a follow-on for
operators.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from metis.core.events.envelope import Actor
from metis.core.events.payloads import (
    GatewayKeyRevoked,
    GatewayKeyRotated,
    make_event,
)
from metis.core.trace.store import TraceStore

# Sentinel session id for gateway admin events. Matches the bus-lifecycle
# convention (`metis.core.events.bus._BUS_SESSION_ID`) — these events aren't
# scoped to any user-visible session, and consumers filter on type.
_ADMIN_SESSION_ID = "system"

# Default grace period for `rotate-key` when the caller doesn't pass one.
# 24h matches the spec: long enough for buyers to roll the new token to
# every client, short enough that a leaked old key isn't a long-term hole.
DEFAULT_GRACE_PERIOD = timedelta(hours=24)


class KeystoreAdminError(Exception):
    """Raised by revoke/rotate/list when the operation can't proceed."""


@dataclass(frozen=True)
class KeyListing:
    """Single-key projection used by `metis gateway list-keys`.

    A subset of the on-disk record, plus a computed `effective_status` that
    treats lapsed grace periods as revoked even before the next sweep
    persists the transition (matches `GatewayKey.is_active`).
    """

    key_id: str
    name: str
    workspace_path: str
    status: str
    effective_status: str
    user_id: str | None
    team_id: str | None
    allowed_models: tuple[str, ...] | None
    daily_cap_usd: str | None
    monthly_cap_usd: str | None
    created_at: datetime | None
    revoked_at: datetime | None
    grace_period_until: datetime | None
    customer_tier: str | None = None


# ---------------------------------------------------------------------------
# Public operations
# ---------------------------------------------------------------------------


def revoke_key(
    *,
    keystore_path: Path,
    key_id: str,
    now: datetime | None = None,
    db_path: Path | None = None,
    reason: str = "admin_revoke",
) -> datetime:
    """Mark a key revoked. Returns the `revoked_at` timestamp on success.

    Idempotent against an already-revoked key: returns the existing
    `revoked_at` and emits no new audit event. Atomic write — concurrent
    readers either see the pre-revoke state or the post-revoke state, never
    a half-written file.
    """
    now = _coerce_now(now)
    raw, entries = _load_raw(keystore_path)
    index, entry = _find_entry(entries, key_id)

    existing_status = entry.get("status", "active")
    if existing_status == "revoked":
        existing_revoked_at = _parse_iso(entry.get("revoked_at"))
        if existing_revoked_at is None:
            raise KeystoreAdminError(
                f"key {key_id!r} is already revoked but has no revoked_at timestamp"
            )
        return existing_revoked_at

    entries[index] = {
        **entry,
        "status": "revoked",
        "revoked_at": _iso(now),
    }
    # Once the key is fully revoked we drop any lingering grace_period_until
    # so list-keys reads cleanly. The audit event keeps the predecessor link.
    entries[index].pop("grace_period_until", None)
    raw["keys"] = entries
    _atomic_write(keystore_path, raw)

    if db_path is not None:
        _emit_audit_event(
            db_path=db_path,
            event=make_event(
                type="gateway.key_revoked",
                session_id=_ADMIN_SESSION_ID,
                actor=Actor.SYSTEM,
                timestamp=now,
                payload=GatewayKeyRevoked(
                    gateway_key_id=key_id,
                    revoked_at=now,
                    reason=reason,
                ),
            ),
        )

    return now


def rotate_key(
    *,
    keystore_path: Path,
    key_id: str,
    grace_period: timedelta | None = None,
    now: datetime | None = None,
    db_path: Path | None = None,
) -> tuple[str, str, datetime]:
    """Issue a successor key that inherits the old key's metadata.

    Returns `(new_key_id, new_plaintext_token, grace_period_until)`. The
    plaintext token is only returned through the call result — the keystore
    only persists the SHA-256 hash, same as `issue_key`. After
    `grace_period_until` lapses, the predecessor is auto-revoked on the next
    admin sweep (or via the explicit `prune-keys` operation when added).
    """
    # Imported lazily to avoid a cycle (issue_key imports auth helpers).
    from metis.gateway.issue_key import _serialize_entry as _issue_entry_serializer  # noqa: F401
    from metis.gateway.issue_key import build_new_key_record

    now = _coerce_now(now)
    grace = grace_period if grace_period is not None else DEFAULT_GRACE_PERIOD
    if grace <= timedelta(0):
        raise KeystoreAdminError("--grace-period must be > 0 (use revoke-key for immediate cutoff)")

    raw, entries = _load_raw(keystore_path)
    index, old_entry = _find_entry(entries, key_id)

    if old_entry.get("status", "active") == "revoked":
        raise KeystoreAdminError(
            f"key {key_id!r} is already revoked; rotate-key requires an active predecessor"
        )

    grace_until = now + grace
    successor_record, new_plaintext = build_new_key_record(
        name=str(old_entry.get("name", key_id)),
        workspace_path=str(old_entry["workspace_path"]),
        allowed_models=(
            tuple(old_entry["allowed_models"]) if old_entry.get("allowed_models") else None
        ),
        daily_cap_usd=old_entry.get("daily_cap_usd"),
        monthly_cap_usd=old_entry.get("monthly_cap_usd"),
        user_id=old_entry.get("user_id"),
        team_id=old_entry.get("team_id"),
    )

    # Stamp the grace boundary on the predecessor; the successor inherits
    # all metadata but starts with no grace constraints of its own.
    entries[index] = {
        **old_entry,
        "grace_period_until": _iso(grace_until),
    }
    entries.append(successor_record)
    raw["keys"] = entries
    _atomic_write(keystore_path, raw)

    if db_path is not None:
        _emit_audit_event(
            db_path=db_path,
            event=make_event(
                type="gateway.key_rotated",
                session_id=_ADMIN_SESSION_ID,
                actor=Actor.SYSTEM,
                timestamp=now,
                payload=GatewayKeyRotated(
                    old_gateway_key_id=key_id,
                    new_gateway_key_id=successor_record["key_id"],
                    grace_period_until=grace_until,
                    workspace_path=str(old_entry["workspace_path"]),
                    user_id=old_entry.get("user_id"),
                    team_id=old_entry.get("team_id"),
                ),
            ),
        )

    return successor_record["key_id"], new_plaintext, grace_until


def list_keys(*, keystore_path: Path, now: datetime | None = None) -> list[KeyListing]:
    """Return every key currently in the keystore, including revoked ones.

    `effective_status` reads the same rule as `GatewayKey.is_active`: an
    active key whose grace period has lapsed is reported as `revoked` even
    if the on-disk `status` is still `active` (the next admin sweep will
    persist the transition).
    """
    now = _coerce_now(now)
    _raw, entries = _load_raw(keystore_path)
    listings: list[KeyListing] = []
    for entry in entries:
        status = entry.get("status", "active")
        revoked_at = _parse_iso(entry.get("revoked_at"))
        grace_until = _parse_iso(entry.get("grace_period_until"))
        if status == "revoked":
            effective = "revoked"
        elif grace_until is not None and now >= grace_until:
            effective = "revoked"
        else:
            effective = "active"
        listings.append(
            KeyListing(
                key_id=str(entry["key_id"]),
                name=str(entry.get("name", entry["key_id"])),
                workspace_path=str(entry["workspace_path"]),
                status=status,
                effective_status=effective,
                user_id=entry.get("user_id"),
                team_id=entry.get("team_id"),
                allowed_models=(
                    tuple(entry["allowed_models"]) if entry.get("allowed_models") else None
                ),
                daily_cap_usd=(
                    str(entry["daily_cap_usd"]) if entry.get("daily_cap_usd") is not None else None
                ),
                monthly_cap_usd=(
                    str(entry["monthly_cap_usd"])
                    if entry.get("monthly_cap_usd") is not None
                    else None
                ),
                created_at=_parse_iso(entry.get("created_at")),
                revoked_at=revoked_at,
                grace_period_until=grace_until,
                customer_tier=entry.get("customer_tier"),
            )
        )
    return listings


def sweep_expired_grace_periods(
    *,
    keystore_path: Path,
    now: datetime | None = None,
    db_path: Path | None = None,
) -> list[str]:
    """Auto-revoke any keys whose grace period has elapsed.

    Returns the list of key_ids that were transitioned `active → revoked`.
    Idempotent — running it twice in a row only writes on the first call.
    Emits one `gateway.key_revoked` event per transitioned key when
    `db_path` is supplied (`reason="grace_period_expired"`).
    """
    now = _coerce_now(now)
    raw, entries = _load_raw(keystore_path)
    transitioned: list[str] = []
    for index, entry in enumerate(entries):
        if entry.get("status", "active") != "active":
            continue
        grace_until = _parse_iso(entry.get("grace_period_until"))
        if grace_until is None or now < grace_until:
            continue
        entries[index] = {
            **entry,
            "status": "revoked",
            "revoked_at": _iso(grace_until),
        }
        entries[index].pop("grace_period_until", None)
        transitioned.append(str(entry["key_id"]))
    if not transitioned:
        return []
    raw["keys"] = entries
    _atomic_write(keystore_path, raw)
    if db_path is not None:
        for kid in transitioned:
            _emit_audit_event(
                db_path=db_path,
                event=make_event(
                    type="gateway.key_revoked",
                    session_id=_ADMIN_SESSION_ID,
                    actor=Actor.SYSTEM,
                    timestamp=now,
                    payload=GatewayKeyRevoked(
                        gateway_key_id=kid,
                        revoked_at=now,
                        reason="grace_period_expired",
                    ),
                ),
            )
    return transitioned


# ---------------------------------------------------------------------------
# Shared helpers (also used by issue_key.py for the atomic-write path)
# ---------------------------------------------------------------------------


def atomic_write_keystore(path: Path, raw: dict[str, Any]) -> None:
    """Write the keystore JSON atomically (write-temp-then-rename).

    Public so `issue_key.py` can share the same code path. `os.replace` on
    POSIX is an atomic rename within a filesystem; concurrent readers
    observe either the old or new file, never a partial write.
    """
    _atomic_write(path, raw)


def _atomic_write(path: Path, raw: dict[str, Any]) -> None:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".keys.", suffix=".json.tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(raw, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _load_raw(keystore_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    keystore_path = keystore_path.expanduser()
    if not keystore_path.exists():
        raise KeystoreAdminError(f"keystore not found: {keystore_path}")
    try:
        raw = json.loads(keystore_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise KeystoreAdminError(f"keystore {keystore_path} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise KeystoreAdminError(f"keystore {keystore_path} root must be a JSON object")
    entries = raw.get("keys")
    if not isinstance(entries, list):
        raise KeystoreAdminError(f"keystore {keystore_path} 'keys' field must be an array")
    return raw, entries


def _find_entry(entries: Iterable[dict[str, Any]], key_id: str) -> tuple[int, dict[str, Any]]:
    for index, entry in enumerate(entries):
        if entry.get("key_id") == key_id:
            return index, entry
    raise KeystoreAdminError(f"key {key_id!r} not found in keystore")


def _coerce_now(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None:
        raise KeystoreAdminError("`now` must be timezone-aware")
    return value


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _parse_iso(raw_value: Any) -> datetime | None:
    if raw_value is None:
        return None
    if not isinstance(raw_value, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw_value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _emit_audit_event(*, db_path: Path, event) -> None:
    """Open the trace DB, write the audit event, close.

    A best-effort path — failures don't abort the keystore mutation. The
    keystore file is the source of truth; the audit event is for operators.
    """
    try:
        store = TraceStore(db_path)
    except Exception:
        return
    try:
        store.write(event)
    finally:
        try:
            store.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# CLI shims
# ---------------------------------------------------------------------------


_DURATION_RE = re.compile(r"^(\d+)([smhdw])$")
_DURATION_UNIT_SECONDS = {
    "s": 1,
    "m": 60,
    "h": 60 * 60,
    "d": 24 * 60 * 60,
    "w": 7 * 24 * 60 * 60,
}


def parse_duration(text: str) -> timedelta:
    """Parse `30m` / `24h` / `7d` / `2w` into a `timedelta`.

    Bare digits are interpreted as seconds. Raises `ValueError` on a bad
    input so the CLI surface can report a deterministic error.
    """
    if not text:
        raise ValueError("duration must be non-empty")
    if text.isdigit():
        return timedelta(seconds=int(text))
    match = _DURATION_RE.match(text)
    if match is None:
        raise ValueError(
            f"could not parse duration {text!r}; use forms like '30m', '24h', '7d', '2w'"
        )
    count = int(match.group(1))
    unit = match.group(2)
    return timedelta(seconds=count * _DURATION_UNIT_SECONDS[unit])


def revoke_key_command(
    *,
    keystore_path: Path,
    key_id: str,
    db_path: Path | None = None,
) -> int:
    """CLI shim for `metis gateway revoke-key`."""
    try:
        revoked_at = revoke_key(
            keystore_path=keystore_path,
            key_id=key_id,
            db_path=db_path,
        )
    except KeystoreAdminError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"revoked: {key_id}")
    print(f"revoked_at: {_iso(revoked_at)}")
    return 0


def rotate_key_command(
    *,
    keystore_path: Path,
    key_id: str,
    grace_period: str | None = None,
    db_path: Path | None = None,
) -> int:
    """CLI shim for `metis gateway rotate-key`."""
    try:
        grace = parse_duration(grace_period) if grace_period else DEFAULT_GRACE_PERIOD
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        new_key_id, new_plaintext, grace_until = rotate_key(
            keystore_path=keystore_path,
            key_id=key_id,
            grace_period=grace,
            db_path=db_path,
        )
    except KeystoreAdminError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"old_key_id: {key_id}")
    print(f"new_key_id: {new_key_id}")
    print(f"new_token:  {new_plaintext}")
    print(f"grace_period_until: {_iso(grace_until)}")
    print(
        "the old key remains active until grace_period_until; "
        "the new token is only printed once and cannot be recovered.",
        file=sys.stderr,
    )
    return 0


def list_keys_command(
    *,
    keystore_path: Path,
    output_format: str = "text",
) -> int:
    """CLI shim for `metis gateway list-keys`.

    `output_format="json"` dumps the full record list (suitable for `jq`);
    `"text"` prints a fixed-column summary intended for terminal reading.
    """
    try:
        listings = list_keys(keystore_path=keystore_path)
    except KeystoreAdminError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if output_format == "json":
        payload = [
            {
                "key_id": k.key_id,
                "name": k.name,
                "workspace_path": k.workspace_path,
                "status": k.status,
                "effective_status": k.effective_status,
                "user_id": k.user_id,
                "team_id": k.team_id,
                "allowed_models": list(k.allowed_models) if k.allowed_models else None,
                "daily_cap_usd": k.daily_cap_usd,
                "monthly_cap_usd": k.monthly_cap_usd,
                "created_at": _iso(k.created_at) if k.created_at else None,
                "revoked_at": _iso(k.revoked_at) if k.revoked_at else None,
                "grace_period_until": (
                    _iso(k.grace_period_until) if k.grace_period_until else None
                ),
                "customer_tier": k.customer_tier,
            }
            for k in listings
        ]
        print(json.dumps(payload, indent=2))
        return 0
    if not listings:
        print("(no keys in keystore)")
        return 0
    header = (
        f"{'KEY_ID':<32} {'STATUS':<8} {'USER':<12} {'TEAM':<12} "
        f"{'CAP':>10} {'CREATED':<20} {'NAME'}"
    )
    print(header)
    print("-" * len(header))
    for k in listings:
        status = k.effective_status
        if k.status == "active" and k.effective_status == "revoked":
            status = "expired"
        cap = k.daily_cap_usd or k.monthly_cap_usd or "-"
        created = _iso(k.created_at)[:19] if k.created_at else "-"
        print(
            f"{k.key_id:<32} {status:<8} "
            f"{(k.user_id or '-'):<12} {(k.team_id or '-'):<12} "
            f"{cap:>10} {created:<20} {k.name}"
        )
    return 0


__all__ = [
    "DEFAULT_GRACE_PERIOD",
    "KeyListing",
    "KeystoreAdminError",
    "atomic_write_keystore",
    "list_keys",
    "list_keys_command",
    "parse_duration",
    "revoke_key",
    "revoke_key_command",
    "rotate_key",
    "rotate_key_command",
    "sweep_expired_grace_periods",
]
