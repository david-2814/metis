"""Self-serve signup + account management for the gateway.

A new buyer arrives at a hosted gateway, posts to ``/signup`` with an email
and a workspace name, receives a magic-link verification URL (in v1 the
link is logged to stdout — Wave 15 wires a real SES/SendGrid transport),
and on verification gets a gateway key plus an account session token
they can use to manage their keys via ``/account/keys``.

Storage shape (sibling to ``keys.json``):

    ~/.metis/gateway/accounts.json   (mode 0o600)

This module owns only the account + magic-link + session bookkeeping; the
gateway key it issues lands in ``keys.json`` via the existing
``metis_gateway.issue_key.build_new_key_record`` factory + the shared
``atomic_write_keystore`` helper.

Wave 14 deliberately omits real email transport, password auth, OIDC SSO,
and billing — those are Wave 15 / 16 territory. Magic links printed to
stdout are enough to validate the signup contract end-to-end in tests.
The signup surface is opt-in via ``SignupConfig.enabled=True`` so in-VPC
deployments (which provision accounts out-of-band via the CLI) can keep
the open endpoints disabled.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import msgspec
from metis_core.canonical.ids import next_monotonic_ulid
from starlette.requests import Request
from starlette.responses import Response

from metis_gateway.auth import extract_bearer_token, validate_identity_tag
from metis_gateway.issue_key import IssueKeyError, build_new_key_record
from metis_gateway.keystore_admin import (
    KeystoreAdminError,
    atomic_write_keystore,
)
from metis_gateway.keystore_admin import (
    revoke_key as keystore_revoke_key,
)

logger = logging.getLogger(__name__)


# Loose email check — we don't try to be RFC-perfect; we reject obvious
# garbage and let the eventual SES bounce surface real problems.
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
_WORKSPACE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,127}$")
_MAX_EMAIL_LEN = 254

DEFAULT_MAGIC_LINK_TTL = timedelta(minutes=30)
DEFAULT_SESSION_TTL = timedelta(hours=24)
MAGIC_LINK_TOKEN_PREFIX = "mlk_"
SESSION_TOKEN_PREFIX = "sess_"
ACCOUNT_ID_PREFIX = "acc_"


class SignupError(Exception):
    """HTTP-visible signup / account failure.

    `status` and `code` are surfaced verbatim in the JSON error envelope so
    handlers don't need to translate twice.
    """

    def __init__(self, message: str, *, status: int = 400, code: str = "invalid_request"):
        super().__init__(message)
        self.status = status
        self.code = code


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Account:
    """A signed-up buyer.

    `email_sha256` mirrors `multi-user.md §3.3`: plaintext email lives in
    `accounts.json` only; the trace store never sees it. `verified_at` is
    `None` between signup and magic-link consumption — un-verified accounts
    can be cleaned up by a future sweep job.
    """

    account_id: str
    email: str
    email_sha256: str
    workspace_path: str
    user_id: str | None
    team_id: str | None
    created_at: datetime
    verified_at: datetime | None
    key_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class MagicLink:
    token_sha256: str
    account_id: str
    purpose: Literal["signup", "login"]
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class AccountSession:
    token_sha256: str
    account_id: str
    created_at: datetime
    expires_at: datetime


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class SignupConfig:
    """Per-deployment signup posture.

    `enabled=False` (default) means `/signup` and `/account/*` return 404 —
    suitable for in-VPC deployments where accounts are provisioned via the
    CLI. SaaS deployments flip `enabled=True` and point `dashboard_base_url`
    at the public hostname so the magic-link URLs are useful out of the box.
    """

    enabled: bool = False
    accounts_path: Path | None = None
    keystore_path: Path | None = None
    dashboard_base_url: str = "http://127.0.0.1:8422"
    magic_link_ttl: timedelta = field(default_factory=lambda: DEFAULT_MAGIC_LINK_TTL)
    session_ttl: timedelta = field(default_factory=lambda: DEFAULT_SESSION_TTL)
    db_path: Path | None = None

    def resolved_accounts_path(self) -> Path:
        return (self.accounts_path or _default_accounts_path()).expanduser()

    def resolved_keystore_path(self) -> Path:
        return (self.keystore_path or _default_keystore_path()).expanduser()


def _default_accounts_path() -> Path:
    return Path.home() / ".metis" / "gateway" / "accounts.json"


def _default_keystore_path() -> Path:
    return Path.home() / ".metis" / "gateway" / "keys.json"


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class AccountStore:
    """Combined accounts + magic-links + sessions persistence.

    Single JSON file with three top-level arrays. Atomic write-temp-then-
    rename for every mutation. Concurrent readers see either the pre- or
    post-mutation state, never a half-written file (same discipline as
    `keystore_admin.atomic_write_keystore`).
    """

    def __init__(
        self,
        path: Path,
        *,
        accounts: list[Account] | None = None,
        magic_links: list[MagicLink] | None = None,
        sessions: list[AccountSession] | None = None,
    ) -> None:
        self._path = path
        self._accounts: dict[str, Account] = {a.account_id: a for a in (accounts or [])}
        self._accounts_by_email_hash: dict[str, str] = {
            a.email_sha256: a.account_id for a in self._accounts.values()
        }
        self._magic_links: dict[str, MagicLink] = {m.token_sha256: m for m in (magic_links or [])}
        self._sessions: dict[str, AccountSession] = {s.token_sha256: s for s in (sessions or [])}

    @classmethod
    def load(cls, path: Path) -> AccountStore:
        path = path.expanduser()
        if not path.exists():
            return cls(path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SignupError(
                f"accounts file {path} is not valid JSON: {exc}",
                status=500,
                code="accounts_corrupt",
            ) from exc
        if not isinstance(raw, dict):
            raise SignupError(
                f"accounts file {path} root must be a JSON object",
                status=500,
                code="accounts_corrupt",
            )
        accounts = [_account_from_dict(d) for d in raw.get("accounts", [])]
        magic_links = [_magic_link_from_dict(d) for d in raw.get("magic_links", [])]
        sessions = [_session_from_dict(d) for d in raw.get("sessions", [])]
        return cls(path, accounts=accounts, magic_links=magic_links, sessions=sessions)

    def _persist(self) -> None:
        raw = {
            "accounts": [_account_to_dict(a) for a in self._accounts.values()],
            "magic_links": [_magic_link_to_dict(m) for m in self._magic_links.values()],
            "sessions": [_session_to_dict(s) for s in self._sessions.values()],
        }
        _atomic_write(self._path, raw)

    # ----- accounts ---------------------------------------------------------

    def get_by_email(self, email: str) -> Account | None:
        digest = _email_hash(email)
        account_id = self._accounts_by_email_hash.get(digest)
        return self._accounts.get(account_id) if account_id else None

    def get_by_id(self, account_id: str) -> Account | None:
        return self._accounts.get(account_id)

    def account_for_key(self, key_id: str) -> Account | None:
        """Reverse-lookup: which account owns a given gateway key?

        Returns `None` for keys that pre-date the signup flow (CLI-issued
        keys) or whose account binding was stripped. Linear scan over
        `self._accounts` — fine for v1 (≤ a few hundred accounts);
        production-scale deployments would want an inverted index.
        """
        for account in self._accounts.values():
            if key_id in account.key_ids:
                return account
        return None

    def accounts(self) -> list[Account]:
        return list(self._accounts.values())

    def create_pending_account(
        self,
        *,
        account_id: str,
        email: str,
        workspace_path: str,
        user_id: str | None,
        team_id: str | None,
        now: datetime,
    ) -> Account:
        if self.get_by_email(email) is not None:
            raise SignupError(
                f"an account already exists for {email}",
                status=409,
                code="account_exists",
            )
        if account_id in self._accounts:
            raise SignupError(
                f"account_id {account_id!r} collides with an existing account",
                status=500,
                code="account_id_collision",
            )
        account = Account(
            account_id=account_id,
            email=email,
            email_sha256=_email_hash(email),
            workspace_path=workspace_path,
            user_id=user_id,
            team_id=team_id,
            created_at=now,
            verified_at=None,
        )
        self._accounts[account.account_id] = account
        self._accounts_by_email_hash[account.email_sha256] = account.account_id
        self._persist()
        return account

    def _replace_account(self, account: Account) -> None:
        self._accounts[account.account_id] = account
        self._accounts_by_email_hash[account.email_sha256] = account.account_id

    def mark_verified(self, account_id: str, *, now: datetime) -> Account:
        account = self._accounts.get(account_id)
        if account is None:
            raise SignupError("account not found", status=404, code="account_not_found")
        if account.verified_at is not None:
            return account
        updated = Account(
            account_id=account.account_id,
            email=account.email,
            email_sha256=account.email_sha256,
            workspace_path=account.workspace_path,
            user_id=account.user_id,
            team_id=account.team_id,
            created_at=account.created_at,
            verified_at=now,
            key_ids=account.key_ids,
        )
        self._replace_account(updated)
        self._persist()
        return updated

    def add_key(self, account_id: str, key_id: str) -> Account:
        account = self._accounts.get(account_id)
        if account is None:
            raise SignupError("account not found", status=404, code="account_not_found")
        if key_id in account.key_ids:
            return account
        updated = Account(
            account_id=account.account_id,
            email=account.email,
            email_sha256=account.email_sha256,
            workspace_path=account.workspace_path,
            user_id=account.user_id,
            team_id=account.team_id,
            created_at=account.created_at,
            verified_at=account.verified_at,
            key_ids=(*account.key_ids, key_id),
        )
        self._replace_account(updated)
        self._persist()
        return updated

    def remove_key(self, account_id: str, key_id: str) -> Account:
        account = self._accounts.get(account_id)
        if account is None:
            raise SignupError("account not found", status=404, code="account_not_found")
        if key_id not in account.key_ids:
            return account
        updated = Account(
            account_id=account.account_id,
            email=account.email,
            email_sha256=account.email_sha256,
            workspace_path=account.workspace_path,
            user_id=account.user_id,
            team_id=account.team_id,
            created_at=account.created_at,
            verified_at=account.verified_at,
            key_ids=tuple(k for k in account.key_ids if k != key_id),
        )
        self._replace_account(updated)
        self._persist()
        return updated

    # ----- magic links ------------------------------------------------------

    def issue_magic_link(
        self,
        *,
        account_id: str,
        purpose: Literal["signup", "login"],
        now: datetime,
        ttl: timedelta,
    ) -> str:
        """Mint a new magic-link token and return the plaintext.

        Only the SHA-256 digest is persisted, same shape as gateway keys.
        """
        plaintext = f"{MAGIC_LINK_TOKEN_PREFIX}{secrets.token_urlsafe(32)}"
        digest = _token_hash(plaintext)
        record = MagicLink(
            token_sha256=digest,
            account_id=account_id,
            purpose=purpose,
            created_at=now,
            expires_at=now + ttl,
        )
        self._magic_links[digest] = record
        self._persist()
        return plaintext

    def consume_magic_link(self, token: str, *, now: datetime) -> MagicLink:
        digest = _token_hash(token)
        record = self._magic_links.get(digest)
        if record is None:
            raise SignupError(
                "magic link is invalid or already used",
                status=401,
                code="invalid_magic_link",
            )
        if now >= record.expires_at:
            del self._magic_links[digest]
            self._persist()
            raise SignupError(
                "magic link has expired; request a new one",
                status=401,
                code="magic_link_expired",
            )
        # Single-use: consume on success.
        del self._magic_links[digest]
        self._persist()
        return record

    # ----- sessions ---------------------------------------------------------

    def issue_session(
        self,
        *,
        account_id: str,
        now: datetime,
        ttl: timedelta,
    ) -> str:
        plaintext = f"{SESSION_TOKEN_PREFIX}{secrets.token_urlsafe(32)}"
        digest = _token_hash(plaintext)
        record = AccountSession(
            token_sha256=digest,
            account_id=account_id,
            created_at=now,
            expires_at=now + ttl,
        )
        self._sessions[digest] = record
        self._persist()
        return plaintext

    def resolve_session(self, token: str, *, now: datetime) -> Account | None:
        digest = _token_hash(token)
        record = self._sessions.get(digest)
        if record is None:
            return None
        if now >= record.expires_at:
            del self._sessions[digest]
            self._persist()
            return None
        return self._accounts.get(record.account_id)


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def _validate_email(value: Any) -> str:
    if not isinstance(value, str):
        raise SignupError("email must be a string")
    email = value.strip().lower()
    if not email:
        raise SignupError("email is required")
    if len(email) > _MAX_EMAIL_LEN:
        raise SignupError(f"email must be at most {_MAX_EMAIL_LEN} characters")
    if not _EMAIL_RE.match(email):
        raise SignupError("email is not a valid address")
    return email


def _validate_workspace_name(value: Any) -> str:
    if not isinstance(value, str):
        raise SignupError("workspace_name must be a string")
    name = value.strip()
    if not _WORKSPACE_NAME_RE.match(name):
        raise SignupError(
            "workspace_name must match ^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$",
            code="invalid_workspace_name",
        )
    return name


def _validate_optional_identity(value: Any, *, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SignupError(f"{field_name} must be a string", code=f"invalid_{field_name}")
    try:
        return validate_identity_tag(value, field_name=field_name)
    except ValueError as exc:
        raise SignupError(str(exc), code=f"invalid_{field_name}") from exc


def _build_workspace_path(workspace_name: str, account_id: str) -> str:
    """Construct the synthetic workspace path for a signup-issued key.

    The gateway uses `workspace_path` only as a routing-policy lookup key
    and as the keystore's foreign-key shape — it does not have to exist on
    disk (the gateway is per-request stateless per gateway.md §2). We
    prefix with the account_id so two accounts can pick the same
    workspace_name without colliding.
    """
    return f"/metis/accounts/{account_id}/{workspace_name}"


# ---------------------------------------------------------------------------
# HTTP handlers
# ---------------------------------------------------------------------------


async def signup_handler(request: Request) -> Response:
    """POST /signup — create a pending account; mint + log a magic link."""
    config, account_store = _state(request)
    body = await _read_json_body(request)

    email = _validate_email(body.get("email"))
    workspace_name = _validate_workspace_name(body.get("workspace_name"))
    user_id = _validate_optional_identity(body.get("user_id"), field_name="user_id")
    team_id = _validate_optional_identity(body.get("team_id"), field_name="team_id")

    now = datetime.now(UTC)
    # Build a stable account_id up front so the workspace_path can embed it.
    # We don't write the account until validation has passed.
    existing = account_store.get_by_email(email)
    if existing is not None:
        if existing.verified_at is None:
            # Re-mint a magic link for the pending account; lets a user who
            # lost the first email recover without manual cleanup.
            magic_link_token = account_store.issue_magic_link(
                account_id=existing.account_id,
                purpose="signup",
                now=now,
                ttl=config.magic_link_ttl,
            )
            _log_magic_link(
                email=existing.email,
                token=magic_link_token,
                config=config,
                purpose="signup",
            )
            return _json(
                {
                    "account_id": existing.account_id,
                    "email": existing.email,
                    "status": "pending_verification",
                    "message": "we re-sent the verification email; check your inbox",
                }
            )
        raise SignupError(
            f"an account already exists for {email}",
            status=409,
            code="account_exists",
        )

    account_id = f"{ACCOUNT_ID_PREFIX}{next_monotonic_ulid()}"
    workspace_path = _build_workspace_path(workspace_name, account_id)
    account = account_store.create_pending_account(
        account_id=account_id,
        email=email,
        workspace_path=workspace_path,
        user_id=user_id,
        team_id=team_id,
        now=now,
    )

    magic_link_token = account_store.issue_magic_link(
        account_id=account.account_id,
        purpose="signup",
        now=now,
        ttl=config.magic_link_ttl,
    )
    _log_magic_link(email=account.email, token=magic_link_token, config=config, purpose="signup")

    return _json(
        {
            "account_id": account.account_id,
            "email": account.email,
            "workspace_path": account.workspace_path,
            "status": "pending_verification",
            "message": "check your inbox for the verification link",
        },
        status=201,
    )


async def signup_verify_handler(request: Request) -> Response:
    """POST /signup/verify — consume the magic link; issue the first key."""
    config, account_store = _state(request)
    body = await _read_json_body(request)
    token = body.get("magic_link_token")
    if not isinstance(token, str) or not token:
        raise SignupError("magic_link_token is required", code="missing_magic_link_token")

    now = datetime.now(UTC)
    record = account_store.consume_magic_link(token, now=now)
    account = account_store.mark_verified(record.account_id, now=now)

    # Issue the first gateway key on the account's workspace.
    key_id, plaintext = _issue_key_for_account(
        config=config,
        account=account,
        name=f"{account.email}/default",
    )
    account_store.add_key(account.account_id, key_id)

    session_token = account_store.issue_session(
        account_id=account.account_id,
        now=now,
        ttl=config.session_ttl,
    )

    return _json(
        {
            "account_id": account.account_id,
            "email": account.email,
            "verified_at": account.verified_at.isoformat() if account.verified_at else None,
            "key_id": key_id,
            "token": plaintext,
            "session_token": session_token,
            "dashboard_url": _dashboard_url_for(config, account),
            "message": "save the token now — only the hash is persisted",
        }
    )


async def account_keys_list_handler(request: Request) -> Response:
    """GET /account/keys — list this account's gateway keys."""
    from metis_gateway.keystore_admin import list_keys

    config, account_store = _state(request)
    account = _require_session(request, account_store)

    try:
        listings = list_keys(keystore_path=config.resolved_keystore_path())
    except KeystoreAdminError as exc:
        raise SignupError(str(exc), status=500, code="keystore_unavailable") from exc

    own = [k for k in listings if k.key_id in account.key_ids]
    return _json(
        {
            "account_id": account.account_id,
            "keys": [
                {
                    "key_id": k.key_id,
                    "name": k.name,
                    "status": k.effective_status,
                    "workspace_path": k.workspace_path,
                    "user_id": k.user_id,
                    "team_id": k.team_id,
                    "allowed_models": list(k.allowed_models) if k.allowed_models else None,
                    "daily_cap_usd": k.daily_cap_usd,
                    "monthly_cap_usd": k.monthly_cap_usd,
                    "created_at": k.created_at.isoformat() if k.created_at else None,
                    "revoked_at": k.revoked_at.isoformat() if k.revoked_at else None,
                }
                for k in own
            ],
        }
    )


async def account_keys_create_handler(request: Request) -> Response:
    """POST /account/keys — issue a new key under this account."""
    config, account_store = _state(request)
    account = _require_session(request, account_store)

    body = await _read_json_body(request, allow_empty=True)
    name_raw = body.get("name")
    name = name_raw.strip() if isinstance(name_raw, str) else ""
    if not name:
        name = f"{account.email}/key"

    key_id, plaintext = _issue_key_for_account(config=config, account=account, name=name)
    account_store.add_key(account.account_id, key_id)
    return _json(
        {
            "account_id": account.account_id,
            "key_id": key_id,
            "name": name,
            "token": plaintext,
            "message": "save the token now — only the hash is persisted",
        },
        status=201,
    )


async def account_keys_revoke_handler(request: Request) -> Response:
    """DELETE /account/keys/{key_id} — revoke a key this account owns."""
    config, account_store = _state(request)
    account = _require_session(request, account_store)
    key_id = request.path_params["key_id"]
    if key_id not in account.key_ids:
        raise SignupError(
            "key not found on this account",
            status=404,
            code="key_not_found",
        )
    try:
        revoked_at = keystore_revoke_key(
            keystore_path=config.resolved_keystore_path(),
            key_id=key_id,
            db_path=config.db_path,
        )
    except KeystoreAdminError as exc:
        raise SignupError(str(exc), status=500, code="revoke_failed") from exc
    account_store.remove_key(account.account_id, key_id)
    return _json(
        {
            "key_id": key_id,
            "revoked_at": revoked_at.astimezone(UTC).isoformat(),
        }
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _state(request: Request) -> tuple[SignupConfig, AccountStore]:
    app_state = request.app.state.app_state
    signup_state = getattr(app_state, "signup", None)
    if signup_state is None or not signup_state.config.enabled:
        raise SignupError("signup is disabled on this deployment", status=404, code="not_found")
    return signup_state.config, signup_state.store


def _require_session(request: Request, account_store: AccountStore) -> Account:
    token = extract_bearer_token(request.headers.get("authorization"))
    if not token or not token.startswith(SESSION_TOKEN_PREFIX):
        raise SignupError(
            "missing or invalid session token",
            status=401,
            code="invalid_session",
        )
    now = datetime.now(UTC)
    account = account_store.resolve_session(token, now=now)
    if account is None:
        raise SignupError(
            "session is invalid or has expired",
            status=401,
            code="invalid_session",
        )
    return account


async def _read_json_body(request: Request, *, allow_empty: bool = False) -> dict[str, Any]:
    raw = await request.body()
    if not raw:
        if allow_empty:
            return {}
        raise SignupError("request body is empty", code="empty_body")
    try:
        decoded = msgspec.json.decode(raw)
    except Exception as exc:
        raise SignupError(f"invalid JSON body: {exc}", code="invalid_json") from exc
    if not isinstance(decoded, dict):
        raise SignupError("request body must be a JSON object", code="invalid_body")
    return decoded


def _issue_key_for_account(
    *,
    config: SignupConfig,
    account: Account,
    name: str,
) -> tuple[str, str]:
    try:
        record, plaintext = build_new_key_record(
            name=name,
            workspace_path=account.workspace_path,
            user_id=account.user_id,
            team_id=account.team_id,
        )
    except IssueKeyError as exc:
        raise SignupError(str(exc), status=500, code="key_issue_failed") from exc

    keystore_path = config.resolved_keystore_path().expanduser()
    raw: dict[str, Any]
    if keystore_path.exists():
        try:
            raw = json.loads(keystore_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SignupError(
                f"keystore {keystore_path} is not valid JSON: {exc}",
                status=500,
                code="keystore_corrupt",
            ) from exc
        if not isinstance(raw, dict):
            raise SignupError(
                f"keystore {keystore_path} root must be a JSON object",
                status=500,
                code="keystore_corrupt",
            )
        keys = raw.get("keys")
        if keys is None:
            raw["keys"] = []
        elif not isinstance(keys, list):
            raise SignupError(
                f"keystore {keystore_path} 'keys' field must be an array",
                status=500,
                code="keystore_corrupt",
            )
    else:
        raw = {"keys": []}
    raw["keys"].append(record)
    atomic_write_keystore(keystore_path, raw)
    return record["key_id"], plaintext


def _dashboard_url_for(config: SignupConfig, account: Account) -> str:
    base = config.dashboard_base_url.rstrip("/")
    return f"{base}/account/{account.account_id}"


def _log_magic_link(
    *,
    email: str,
    token: str,
    config: SignupConfig,
    purpose: str,
) -> None:
    """Wave 14 stub: log the magic-link URL to stdout.

    Wave 15 replaces this with the real SES/SendGrid transport. Tests and
    local development read stdout for the link; production deployments
    with `enabled=True` MUST swap in an email transport before exposing
    /signup publicly (the link in stdout is a development affordance,
    not a security feature).
    """
    base = config.dashboard_base_url.rstrip("/")
    verify_url = f"{base}/signup/verify?token={token}"
    sys.stdout.write(
        f"[magic-link {purpose}] email={email} url={verify_url} expires_in_seconds="
        f"{int(config.magic_link_ttl.total_seconds())}\n"
    )
    sys.stdout.flush()
    logger.info("issued magic link purpose=%s email=%s", purpose, email)


def _email_hash(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _json(body: dict[str, Any], *, status: int = 200) -> Response:
    return Response(
        content=msgspec.json.encode(body),
        media_type="application/json",
        status_code=status,
    )


def signup_error_response(exc: SignupError) -> Response:
    return _json(
        {"error": {"code": exc.code, "message": str(exc)}},
        status=exc.status,
    )


# ---------------------------------------------------------------------------
# Disk shape
# ---------------------------------------------------------------------------


def _atomic_write(path: Path, raw: dict[str, Any]) -> None:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".accounts.", suffix=".json.tmp", dir=str(path.parent))
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


def _account_to_dict(account: Account) -> dict[str, Any]:
    return {
        "account_id": account.account_id,
        "email": account.email,
        "email_sha256": account.email_sha256,
        "workspace_path": account.workspace_path,
        "user_id": account.user_id,
        "team_id": account.team_id,
        "created_at": account.created_at.astimezone(UTC).isoformat(),
        "verified_at": account.verified_at.astimezone(UTC).isoformat()
        if account.verified_at
        else None,
        "key_ids": list(account.key_ids),
    }


def _account_from_dict(raw: dict[str, Any]) -> Account:
    return Account(
        account_id=str(raw["account_id"]),
        email=str(raw["email"]),
        email_sha256=str(raw["email_sha256"]),
        workspace_path=str(raw["workspace_path"]),
        user_id=raw.get("user_id"),
        team_id=raw.get("team_id"),
        created_at=datetime.fromisoformat(raw["created_at"]),
        verified_at=datetime.fromisoformat(raw["verified_at"]) if raw.get("verified_at") else None,
        key_ids=tuple(raw.get("key_ids", [])),
    )


def _magic_link_to_dict(record: MagicLink) -> dict[str, Any]:
    return {
        "token_sha256": record.token_sha256,
        "account_id": record.account_id,
        "purpose": record.purpose,
        "created_at": record.created_at.astimezone(UTC).isoformat(),
        "expires_at": record.expires_at.astimezone(UTC).isoformat(),
    }


def _magic_link_from_dict(raw: dict[str, Any]) -> MagicLink:
    return MagicLink(
        token_sha256=str(raw["token_sha256"]),
        account_id=str(raw["account_id"]),
        purpose=raw["purpose"],
        created_at=datetime.fromisoformat(raw["created_at"]),
        expires_at=datetime.fromisoformat(raw["expires_at"]),
    )


def _session_to_dict(record: AccountSession) -> dict[str, Any]:
    return {
        "token_sha256": record.token_sha256,
        "account_id": record.account_id,
        "created_at": record.created_at.astimezone(UTC).isoformat(),
        "expires_at": record.expires_at.astimezone(UTC).isoformat(),
    }


def _session_from_dict(raw: dict[str, Any]) -> AccountSession:
    return AccountSession(
        token_sha256=str(raw["token_sha256"]),
        account_id=str(raw["account_id"]),
        created_at=datetime.fromisoformat(raw["created_at"]),
        expires_at=datetime.fromisoformat(raw["expires_at"]),
    )


# ---------------------------------------------------------------------------
# App-state wiring
# ---------------------------------------------------------------------------


@dataclass
class SignupState:
    config: SignupConfig
    store: AccountStore


def build_signup_state(config: SignupConfig | None) -> SignupState | None:
    if config is None or not config.enabled:
        return None
    store = AccountStore.load(config.resolved_accounts_path())
    return SignupState(config=config, store=store)


__all__ = [
    "ACCOUNT_ID_PREFIX",
    "DEFAULT_MAGIC_LINK_TTL",
    "DEFAULT_SESSION_TTL",
    "MAGIC_LINK_TOKEN_PREFIX",
    "SESSION_TOKEN_PREFIX",
    "Account",
    "AccountSession",
    "AccountStore",
    "MagicLink",
    "SignupConfig",
    "SignupError",
    "SignupState",
    "account_keys_create_handler",
    "account_keys_list_handler",
    "account_keys_revoke_handler",
    "build_signup_state",
    "signup_error_response",
    "signup_handler",
    "signup_verify_handler",
]
