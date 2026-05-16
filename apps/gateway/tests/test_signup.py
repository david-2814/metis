"""Tests for the self-serve signup + account-management surface.

Covers the gateway.md §"Self-serve signup" contract: a buyer arrives at
``POST /signup`` with an email and a workspace name, gets a logged magic
link, posts the link to ``/signup/verify`` to claim a session token + a
first gateway key, then manages keys via ``/account/keys``.

Magic links are observed via stdout capture — Wave 14 logs them rather
than emailing.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
import pytest
from metis_gateway.app import build_app
from metis_gateway.auth import Keystore
from metis_gateway.signup import (
    MAGIC_LINK_TOKEN_PREFIX,
    SESSION_TOKEN_PREFIX,
    AccountStore,
    SignupConfig,
)

_MAGIC_LINK_TOKEN_RE = re.compile(r"url=\S+token=(" + MAGIC_LINK_TOKEN_PREFIX + r"\S+)")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def signup_config(tmp_path: Path) -> SignupConfig:
    return SignupConfig(
        enabled=True,
        accounts_path=tmp_path / "accounts.json",
        keystore_path=tmp_path / "keys.json",
        dashboard_base_url="https://test.example.com",
    )


@pytest.fixture
async def signup_client(runtime, signup_config: SignupConfig):
    """httpx client bound to a gateway app with signup turned on."""
    app = build_app(runtime, signup=signup_config)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


@pytest.fixture
async def signup_disabled_client(runtime):
    """httpx client bound to a gateway with signup OFF (the default posture)."""
    app = build_app(runtime)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


def _extract_magic_link_token(captured: str) -> str:
    match = _MAGIC_LINK_TOKEN_RE.search(captured)
    if not match:
        raise AssertionError(f"no magic link found in stdout: {captured!r}")
    return match.group(1)


async def _signup_and_verify(
    client: httpx.AsyncClient,
    capsys: pytest.CaptureFixture[str],
    *,
    email: str = "alice@example.com",
    workspace_name: str = "default",
    user_id: str | None = None,
    team_id: str | None = None,
) -> dict:
    payload: dict = {"email": email, "workspace_name": workspace_name}
    if user_id is not None:
        payload["user_id"] = user_id
    if team_id is not None:
        payload["team_id"] = team_id
    signup_response = await client.post("/signup", json=payload)
    assert signup_response.status_code == 201, signup_response.text
    captured = capsys.readouterr()
    token = _extract_magic_link_token(captured.out)
    verify_response = await client.post("/signup/verify", json={"magic_link_token": token})
    assert verify_response.status_code == 200, verify_response.text
    return verify_response.json()


# ---------------------------------------------------------------------------
# End-to-end signup flow
# ---------------------------------------------------------------------------


async def test_signup_creates_pending_account_and_logs_magic_link(
    signup_client, capsys, signup_config
) -> None:
    r = await signup_client.post(
        "/signup",
        json={"email": "alice@example.com", "workspace_name": "myproj"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "pending_verification"
    assert body["email"] == "alice@example.com"
    assert body["account_id"].startswith("acc_")
    # Workspace path embeds the account_id so two accounts can pick the
    # same workspace_name without colliding.
    assert body["workspace_path"].endswith("/myproj")
    assert body["account_id"] in body["workspace_path"]

    captured = capsys.readouterr()
    assert "[magic-link signup]" in captured.out
    assert "alice@example.com" in captured.out
    # The accounts file landed with the pending-verification record.
    store = AccountStore.load(signup_config.resolved_accounts_path())
    account = store.get_by_email("alice@example.com")
    assert account is not None
    assert account.verified_at is None
    assert account.key_ids == ()


async def test_signup_verify_issues_key_and_session(signup_client, capsys, signup_config) -> None:
    body = await _signup_and_verify(
        signup_client, capsys, email="alice@example.com", workspace_name="myproj"
    )
    assert body["key_id"].startswith("gk_")
    assert body["token"].startswith("gw_")
    assert body["session_token"].startswith(SESSION_TOKEN_PREFIX)
    assert body["dashboard_url"].startswith("https://test.example.com/account/")
    # The key landed in the keystore and authenticates the printed token.
    keystore = Keystore.from_file(signup_config.resolved_keystore_path())
    key = keystore.authenticate(body["token"])
    assert key is not None
    assert key.key_id == body["key_id"]
    # The account is now marked verified and remembers the key id.
    store = AccountStore.load(signup_config.resolved_accounts_path())
    account = store.get_by_id(body["account_id"])
    assert account is not None
    assert account.verified_at is not None
    assert key.key_id in account.key_ids


async def test_signup_verify_persists_user_and_team_tags_on_key(
    signup_client, capsys, signup_config
) -> None:
    body = await _signup_and_verify(
        signup_client,
        capsys,
        email="bob@example.com",
        workspace_name="ops",
        user_id="bob",
        team_id="eng",
    )
    keystore = Keystore.from_file(signup_config.resolved_keystore_path())
    key = keystore.get_by_id(body["key_id"])
    assert key is not None
    assert key.user_id == "bob"
    assert key.team_id == "eng"


async def test_signup_verify_consumes_magic_link_only_once(signup_client, capsys) -> None:
    r = await signup_client.post(
        "/signup",
        json={"email": "alice@example.com", "workspace_name": "myproj"},
    )
    assert r.status_code == 201
    token = _extract_magic_link_token(capsys.readouterr().out)
    first = await signup_client.post("/signup/verify", json={"magic_link_token": token})
    assert first.status_code == 200
    second = await signup_client.post("/signup/verify", json={"magic_link_token": token})
    assert second.status_code == 401
    assert second.json()["error"]["code"] == "invalid_magic_link"


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


async def test_signup_rejects_bad_email(signup_client) -> None:
    r = await signup_client.post(
        "/signup",
        json={"email": "not-an-email", "workspace_name": "myproj"},
    )
    assert r.status_code == 400
    assert "valid address" in r.json()["error"]["message"]


async def test_signup_rejects_bad_workspace_name(signup_client) -> None:
    r = await signup_client.post(
        "/signup",
        json={"email": "alice@example.com", "workspace_name": "../escape"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_workspace_name"


async def test_signup_rejects_duplicate_verified_email(signup_client, capsys) -> None:
    await _signup_and_verify(signup_client, capsys, email="alice@example.com")
    r = await signup_client.post(
        "/signup",
        json={"email": "alice@example.com", "workspace_name": "other"},
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "account_exists"


async def test_signup_resends_magic_link_when_account_is_still_pending(
    signup_client, capsys
) -> None:
    r1 = await signup_client.post(
        "/signup",
        json={"email": "alice@example.com", "workspace_name": "myproj"},
    )
    assert r1.status_code == 201
    first_token = _extract_magic_link_token(capsys.readouterr().out)
    # Same email comes back before verifying — we re-mint instead of erroring.
    r2 = await signup_client.post(
        "/signup",
        json={"email": "alice@example.com", "workspace_name": "myproj"},
    )
    assert r2.status_code == 200
    assert r2.json()["status"] == "pending_verification"
    second_token = _extract_magic_link_token(capsys.readouterr().out)
    assert first_token != second_token
    # The fresh token verifies; the stale one is gone (single-use storage).
    # We don't strictly invalidate prior tokens — but issuing a second one
    # is enough for the "I lost the email" flow.
    verify = await signup_client.post("/signup/verify", json={"magic_link_token": second_token})
    assert verify.status_code == 200


async def test_signup_rejects_unknown_magic_link(signup_client) -> None:
    r = await signup_client.post("/signup/verify", json={"magic_link_token": "mlk_made_up"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "invalid_magic_link"


# ---------------------------------------------------------------------------
# Account / keys management
# ---------------------------------------------------------------------------


async def test_account_keys_endpoints_require_session(signup_client) -> None:
    r = await signup_client.get("/account/keys")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "invalid_session"


async def test_account_keys_endpoints_reject_random_bearer(signup_client) -> None:
    r = await signup_client.get(
        "/account/keys",
        headers={"Authorization": "Bearer sess_madeupgarbage"},
    )
    assert r.status_code == 401


async def test_account_keys_list_returns_only_account_keys(
    signup_client, capsys, signup_config
) -> None:
    alice = await _signup_and_verify(signup_client, capsys, email="alice@example.com")
    # Issue an out-of-band key for someone else; it shouldn't appear in
    # alice's listing.
    from datetime import UTC, datetime

    from metis_gateway.issue_key import issue_key

    issue_key(
        keystore_path=signup_config.resolved_keystore_path(),
        name="stranger",
        workspace_path="/tmp/stranger",
        now=datetime.now(UTC),
    )

    r = await signup_client.get(
        "/account/keys",
        headers={"Authorization": f"Bearer {alice['session_token']}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["account_id"] == alice["account_id"]
    assert len(body["keys"]) == 1
    assert body["keys"][0]["key_id"] == alice["key_id"]


async def test_account_keys_create_issues_additional_key(
    signup_client, capsys, signup_config
) -> None:
    alice = await _signup_and_verify(signup_client, capsys, email="alice@example.com")
    r = await signup_client.post(
        "/account/keys",
        headers={"Authorization": f"Bearer {alice['session_token']}"},
        json={"name": "ci-pipeline"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["key_id"].startswith("gk_")
    assert body["token"].startswith("gw_")
    assert body["name"] == "ci-pipeline"

    # The new key is in the account's key_ids and authenticates.
    store = AccountStore.load(signup_config.resolved_accounts_path())
    account = store.get_by_id(alice["account_id"])
    assert account is not None
    assert body["key_id"] in account.key_ids
    keystore = Keystore.from_file(signup_config.resolved_keystore_path())
    key = keystore.authenticate(body["token"])
    assert key is not None
    assert key.key_id == body["key_id"]


async def test_account_keys_create_works_with_empty_body(signup_client, capsys) -> None:
    alice = await _signup_and_verify(signup_client, capsys, email="alice@example.com")
    r = await signup_client.post(
        "/account/keys",
        headers={"Authorization": f"Bearer {alice['session_token']}"},
    )
    assert r.status_code == 201
    assert r.json()["name"].endswith("/key")


async def test_account_keys_delete_revokes_key(signup_client, capsys, signup_config) -> None:
    alice = await _signup_and_verify(signup_client, capsys, email="alice@example.com")
    r = await signup_client.delete(
        f"/account/keys/{alice['key_id']}",
        headers={"Authorization": f"Bearer {alice['session_token']}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["key_id"] == alice["key_id"]
    assert body["revoked_at"] is not None

    keystore = Keystore.from_file(signup_config.resolved_keystore_path())
    key = keystore.get_by_id(alice["key_id"])
    assert key is not None
    assert key.status == "revoked"

    # The key is dropped from the account's key list.
    store = AccountStore.load(signup_config.resolved_accounts_path())
    account = store.get_by_id(alice["account_id"])
    assert account is not None
    assert alice["key_id"] not in account.key_ids


async def test_account_keys_delete_rejects_foreign_key(
    signup_client, capsys, signup_config
) -> None:
    alice = await _signup_and_verify(signup_client, capsys, email="alice@example.com")
    # Issue an unrelated key not owned by alice's account.
    from metis_gateway.issue_key import issue_key

    foreign_key_id, _ = issue_key(
        keystore_path=signup_config.resolved_keystore_path(),
        name="stranger",
        workspace_path="/tmp/stranger",
    )
    r = await signup_client.delete(
        f"/account/keys/{foreign_key_id}",
        headers={"Authorization": f"Bearer {alice['session_token']}"},
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "key_not_found"


# ---------------------------------------------------------------------------
# Disabled-signup posture (in-VPC default)
# ---------------------------------------------------------------------------


async def test_signup_endpoints_404_when_disabled(signup_disabled_client) -> None:
    r = await signup_disabled_client.post(
        "/signup", json={"email": "alice@example.com", "workspace_name": "myproj"}
    )
    assert r.status_code == 404


async def test_account_endpoints_404_when_disabled(signup_disabled_client) -> None:
    r = await signup_disabled_client.get("/account/keys")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Storage shape
# ---------------------------------------------------------------------------


async def test_accounts_file_is_chmod_600(signup_client, capsys, signup_config) -> None:
    """The accounts file holds plaintext email; mirror the keystore's mode."""
    import os
    import stat

    await _signup_and_verify(signup_client, capsys, email="alice@example.com")
    path = signup_config.resolved_accounts_path()
    assert path.exists()
    mode = stat.S_IMODE(os.stat(path).st_mode)
    # On some test platforms chmod can be a no-op (e.g. inside containers
    # without permissioned filesystems). We accept either the exact 0o600
    # or any mode that masks group + other.
    assert mode == 0o600 or (mode & 0o077) == 0, oct(mode)


async def test_accounts_file_is_valid_json_after_mutations(
    signup_client, capsys, signup_config
) -> None:
    alice = await _signup_and_verify(signup_client, capsys, email="alice@example.com")
    await signup_client.post(
        "/account/keys",
        headers={"Authorization": f"Bearer {alice['session_token']}"},
        json={"name": "second"},
    )
    raw = json.loads(signup_config.resolved_accounts_path().read_text(encoding="utf-8"))
    assert "accounts" in raw
    assert "magic_links" in raw
    assert "sessions" in raw
    assert isinstance(raw["accounts"], list)
    # Token plaintexts are never persisted — only their SHA-256 digests.
    raw_str = json.dumps(raw)
    assert MAGIC_LINK_TOKEN_PREFIX not in raw_str
    assert SESSION_TOKEN_PREFIX not in raw_str
