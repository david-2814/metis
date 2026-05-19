"""Gateway-key authentication.

A keystore is a JSON file (default `~/.metis/gateway/keys.json`) listing the
gateway keys the operator has issued. Each entry records the SHA-256 hash of
the bearer token, a stable `key_id` (used in trace events for cost
attribution), and the workspace the key is scoped to.

v1 maps each key to exactly one workspace. Multi-workspace per key is
Phase 3 (gateway.md §11).

Keys may optionally carry `user_id` and `team_id` strings (multi-user.md §4)
so that trace stamping and analytics can roll up cost by developer or team.
Existing keys issued without those fields keep working — they auth exactly
as before and their traffic rolls up under the `null` bucket.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

KeyStatus = Literal["active", "revoked"]
CustomerTier = Literal["trial", "paid", "internal"]
_CUSTOMER_TIER_VALUES: tuple[CustomerTier, ...] = ("trial", "paid", "internal")

# multi-user.md §3.4 — stable id format for `--user` / `--team` flags.
# Matches the shipped `^[A-Za-z0-9_-]{1,200}$` defense used by analytics-api
# filters, but tightened to lowercase per the spec's CLI examples (`alice`,
# `eng`) so trace dumps stay normalized.
_IDENTITY_TAG_RE = re.compile(r"^[a-z0-9_-]+$")
_MAX_IDENTITY_TAG_LEN = 200


def validate_identity_tag(value: str, *, field_name: str) -> str:
    """Validate a `user_id` / `team_id` tag per multi-user.md §3.4.

    Returns the value unchanged on success; raises `ValueError` with a
    deterministic message on failure so CLI and keystore loaders share the
    same rejection text.
    """
    if not value:
        raise ValueError(f"{field_name} must be non-empty")
    if len(value) > _MAX_IDENTITY_TAG_LEN:
        raise ValueError(f"{field_name} must be at most {_MAX_IDENTITY_TAG_LEN} characters")
    if not _IDENTITY_TAG_RE.match(value):
        raise ValueError(
            f"{field_name} must match {_IDENTITY_TAG_RE.pattern} "
            "(lowercase alphanumerics, underscore, hyphen)"
        )
    return value


@dataclass(frozen=True)
class GatewayKey:
    """A single configured gateway key.

    `secret_hash` is the SHA-256 hex digest of the full bearer token (the
    `gw_<ulid>` string the client sends in `Authorization: Bearer ...`).
    The plaintext token is never stored.

    `user_id` / `team_id` are the optional identity tags from multi-user.md
    §4.2; both default to `None` for v1 keys issued before the field landed.

    `daily_cap_usd` / `monthly_cap_usd` are optional spend caps per
    `multi-user.md §5.1`. `Decimal` end-to-end so the quota tracker can
    compare against summed cost without float drift. Pre-quota keys load
    with both fields `None` (no cap → no enforcement, no soft alert).
    """

    key_id: str
    secret_hash: str
    name: str
    workspace_path: str
    allowed_models: tuple[str, ...] | None = None
    daily_cap_usd: Decimal | None = None
    monthly_cap_usd: Decimal | None = None
    user_id: str | None = None
    team_id: str | None = None
    # Lifecycle fields (Wave 10 — gateway.md §11 key rotation/revocation).
    # `status` defaults to "active"; keystores written before Wave 10 omit it
    # and the loader fills the default. `revoked_at` is set when the key is
    # marked revoked (explicitly via `metis gateway revoke-key`, or
    # implicitly when a rotation's `grace_period_until` lapses and the next
    # admin sweep auto-revokes the predecessor). `grace_period_until` is set
    # by `metis gateway rotate-key`; while the key is still `active`, an
    # `auth` check past that timestamp treats it as effectively revoked even
    # before disk reflects it (see `is_active`).
    status: KeyStatus = "active"
    revoked_at: datetime | None = None
    grace_period_until: datetime | None = None
    # Concierge-onboarding tag for support / report-formatting. Optional;
    # keys issued before Wave 14b have `None` and behave identically. Not
    # an entitlement field — the gateway does not gate behavior on tier —
    # but the `metis customer-report` / `metis trial-status` flows pivot
    # on it for headline framing (trial vs paid copy).
    customer_tier: CustomerTier | None = None

    def is_active(self, *, now: datetime) -> bool:
        """True if the key authenticates right now.

        Returns False when (a) the key has been explicitly revoked or (b) a
        rotation grace period has lapsed (`now >= grace_period_until`). The
        grace-period check is read-only — persisting the revocation is the
        admin sweep's job (`sweep_expired_grace_periods`), not auth's.
        """
        if self.status == "revoked":
            return False
        if self.grace_period_until is not None and now >= self.grace_period_until:
            return False
        return True

    def effective_revoked_at(self, *, now: datetime) -> datetime | None:
        """Timestamp surfaced on the 401 `key_revoked` body for this key.

        For explicitly-revoked keys this is `revoked_at`; for grace-period
        lapses we report the grace boundary so clients see *when* the key
        stopped working, not the wall-clock of the next failed request.
        """
        if self.status == "revoked":
            return self.revoked_at
        if self.grace_period_until is not None and now >= self.grace_period_until:
            return self.grace_period_until
        return None


@dataclass(frozen=True)
class Identity:
    """Request-scoped principal resolved from the keystore at auth time.

    multi-user.md §3.2 calls this `Principal`; the v1 name is `Identity` so
    the harness/auth surface reads naturally. The fields match: the gateway
    key is the auth artifact; `(user_id, team_id, workspace_path)` is what
    the request bills to. `user_id` / `team_id` are `None` for keys issued
    without `--user` / `--team`, matching the null-bucket convention used
    by `gateway_key_id` for agent-loop traffic.
    """

    gateway_key_id: str
    workspace_path: str
    user_id: str | None = None
    team_id: str | None = None


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
            daily_cap = _parse_cap_field(entry, index=index, field_name="daily_cap_usd")
            monthly_cap = _parse_cap_field(entry, index=index, field_name="monthly_cap_usd")
            user_id = _parse_identity_field(entry, index=index, field_name="user_id")
            team_id = _parse_identity_field(entry, index=index, field_name="team_id")
            status = _parse_status_field(entry, index=index)
            revoked_at = _parse_datetime_field(entry, index=index, field_name="revoked_at")
            grace_period_until = _parse_datetime_field(
                entry, index=index, field_name="grace_period_until"
            )
            customer_tier = _parse_customer_tier_field(entry, index=index)
            if status == "revoked" and revoked_at is None:
                raise KeystoreError(
                    f"keystore keys[{index}] status='revoked' requires a `revoked_at` timestamp"
                )
            keys.append(
                GatewayKey(
                    key_id=key_id,
                    secret_hash=secret_hash,
                    name=name,
                    workspace_path=workspace_path,
                    allowed_models=allowed_tuple,
                    daily_cap_usd=daily_cap,
                    monthly_cap_usd=monthly_cap,
                    user_id=user_id,
                    team_id=team_id,
                    status=status,
                    revoked_at=revoked_at,
                    grace_period_until=grace_period_until,
                    customer_tier=customer_tier,
                )
            )
        return cls(keys)

    def authenticate(self, bearer_token: str) -> GatewayKey | None:
        if not bearer_token:
            return None
        digest = hashlib.sha256(bearer_token.encode("utf-8")).hexdigest()
        return self._by_hash.get(digest)

    def identify(self, bearer_token: str) -> Identity | None:
        """Authenticate and return the request-scoped `Identity`.

        Returns `None` when the token does not match a known key. Callers
        that need the raw `GatewayKey` (e.g. to read `allowed_models` /
        `daily_cap_usd`) can still call `authenticate()` directly.
        """
        key = self.authenticate(bearer_token)
        if key is None:
            return None
        return Identity(
            gateway_key_id=key.key_id,
            workspace_path=key.workspace_path,
            user_id=key.user_id,
            team_id=key.team_id,
        )

    def get_by_id(self, key_id: str) -> GatewayKey | None:
        return self._by_id.get(key_id)

    def __len__(self) -> int:
        return len(self._by_hash)

    def keys(self) -> list[GatewayKey]:
        """Return the configured keys in insertion order (stable for listings)."""
        return list(self._by_id.values())


def validate_cap_usd(value: Decimal | float | int | str, *, field_name: str) -> Decimal:
    """Coerce a cap value to a strictly-positive `Decimal`.

    multi-user.md §5.1 — caps are USD amounts; zero or negative is rejected
    so a misconfigured "0.0" cap can't masquerade as "always blocked." Used
    by both the issue-key CLI and the keystore loader so the rejection
    message is identical at both entry points.
    """
    try:
        if isinstance(value, Decimal):
            decimal_value = value
        elif isinstance(value, bool):
            raise ValueError(f"{field_name} must be a positive number")
        elif isinstance(value, (int, float)):
            decimal_value = Decimal(str(value))
        elif isinstance(value, str):
            decimal_value = Decimal(value)
        else:
            raise ValueError(f"{field_name} must be a positive number")
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} must be a positive number") from exc
    if decimal_value <= 0:
        raise ValueError(f"{field_name} must be > 0")
    return decimal_value


def _parse_cap_field(entry: dict[str, Any], *, index: int, field_name: str) -> Decimal | None:
    raw_value = entry.get(field_name)
    if raw_value is None:
        return None
    if not isinstance(raw_value, (int, float, str)) or isinstance(raw_value, bool):
        raise KeystoreError(f"keystore keys[{index}].{field_name} must be numeric")
    try:
        return validate_cap_usd(raw_value, field_name=f"keys[{index}].{field_name}")
    except ValueError as exc:
        raise KeystoreError(str(exc)) from exc


def _parse_identity_field(entry: dict[str, Any], *, index: int, field_name: str) -> str | None:
    raw_value = entry.get(field_name)
    if raw_value is None:
        return None
    if not isinstance(raw_value, str):
        raise KeystoreError(f"keystore keys[{index}].{field_name} must be a string")
    try:
        return validate_identity_tag(raw_value, field_name=f"keys[{index}].{field_name}")
    except ValueError as exc:
        raise KeystoreError(str(exc)) from exc


def _parse_customer_tier_field(entry: dict[str, Any], *, index: int) -> CustomerTier | None:
    raw_value = entry.get("customer_tier")
    if raw_value is None:
        return None
    if raw_value not in _CUSTOMER_TIER_VALUES:
        raise KeystoreError(
            f"keystore keys[{index}].customer_tier must be one of "
            f"{list(_CUSTOMER_TIER_VALUES)} (got {raw_value!r})"
        )
    return raw_value  # type: ignore[return-value]


def _parse_status_field(entry: dict[str, Any], *, index: int) -> KeyStatus:
    raw_value = entry.get("status")
    if raw_value is None:
        return "active"
    if raw_value not in ("active", "revoked"):
        raise KeystoreError(
            f"keystore keys[{index}].status must be 'active' or 'revoked' (got {raw_value!r})"
        )
    return raw_value  # type: ignore[return-value]


def _parse_datetime_field(entry: dict[str, Any], *, index: int, field_name: str) -> datetime | None:
    raw_value = entry.get(field_name)
    if raw_value is None:
        return None
    if not isinstance(raw_value, str):
        raise KeystoreError(f"keystore keys[{index}].{field_name} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(raw_value)
    except ValueError as exc:
        raise KeystoreError(
            f"keystore keys[{index}].{field_name} is not a valid ISO-8601 timestamp: {exc}"
        ) from exc
    if parsed.tzinfo is None:
        raise KeystoreError(
            f"keystore keys[{index}].{field_name} must be timezone-aware (include `Z` or offset)"
        )
    return parsed


def identity_from_key(key: GatewayKey) -> Identity:
    """Project a `GatewayKey` onto the request-scoped `Identity`.

    Exposed for the harness and tests so they don't have to reconstruct the
    projection manually. multi-user.md §3.2 — `Identity` is the per-request
    view of the keystore; the key remains the durable record.
    """
    return Identity(
        gateway_key_id=key.key_id,
        workspace_path=key.workspace_path,
        user_id=key.user_id,
        team_id=key.team_id,
    )


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
