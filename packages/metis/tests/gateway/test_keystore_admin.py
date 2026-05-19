"""`metis gateway revoke-key` / `rotate-key` / `list-keys` tests.

Covers Wave 10 (gateway.md §11) key-lifecycle ops on top of the v1
keystore. The single trace DB is also exercised so the
`gateway.key_issued` / `gateway.key_revoked` / `gateway.key_rotated`
audit events are persisted with the right payload shape.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from metis.gateway.auth import Keystore, hash_bearer_token
from metis.gateway.issue_key import issue_key
from metis.gateway.keystore_admin import (
    DEFAULT_GRACE_PERIOD,
    KeystoreAdminError,
    list_keys,
    list_keys_command,
    parse_duration,
    revoke_key,
    revoke_key_command,
    rotate_key,
    rotate_key_command,
    sweep_expired_grace_periods,
)


def _events_of_type(db_path: Path, event_type: str) -> list[dict]:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT payload_json FROM events WHERE type = ? ORDER BY id",
            (event_type,),
        ).fetchall()
    finally:
        conn.close()
    return [json.loads(r[0]) for r in rows]


# ---------------------------------------------------------------------------
# revoke-key
# ---------------------------------------------------------------------------


def test_revoke_key_marks_status_and_stamps_revoked_at(tmp_path: Path) -> None:
    keystore_path = tmp_path / "keys.json"
    key_id, _plaintext = issue_key(
        keystore_path=keystore_path,
        name="alice",
        workspace_path=str(tmp_path),
    )

    revoked_at = revoke_key(keystore_path=keystore_path, key_id=key_id)

    raw = json.loads(keystore_path.read_text(encoding="utf-8"))
    entry = next(k for k in raw["keys"] if k["key_id"] == key_id)
    assert entry["status"] == "revoked"
    assert entry["revoked_at"] == revoked_at.astimezone(UTC).isoformat()
    # Reload through Keystore so we exercise the back-compat path.
    store = Keystore.from_file(keystore_path)
    reloaded = store.get_by_id(key_id)
    assert reloaded is not None
    assert reloaded.status == "revoked"
    assert reloaded.revoked_at == revoked_at


def test_revoke_key_emits_audit_event(tmp_path: Path) -> None:
    keystore_path = tmp_path / "keys.json"
    db_path = tmp_path / "metis.db"
    key_id, _ = issue_key(
        keystore_path=keystore_path,
        name="alice",
        workspace_path=str(tmp_path),
        db_path=db_path,
    )
    issued = _events_of_type(db_path, "gateway.key_issued")
    assert len(issued) == 1
    assert issued[0]["gateway_key_id"] == key_id

    revoke_key(keystore_path=keystore_path, key_id=key_id, db_path=db_path)
    revoked = _events_of_type(db_path, "gateway.key_revoked")
    assert len(revoked) == 1
    assert revoked[0]["gateway_key_id"] == key_id
    assert revoked[0]["reason"] == "admin_revoke"
    assert "revoked_at" in revoked[0]


def test_revoke_key_idempotent(tmp_path: Path) -> None:
    keystore_path = tmp_path / "keys.json"
    db_path = tmp_path / "metis.db"
    key_id, _ = issue_key(
        keystore_path=keystore_path,
        name="alice",
        workspace_path=str(tmp_path),
    )
    first = revoke_key(keystore_path=keystore_path, key_id=key_id, db_path=db_path)
    second = revoke_key(keystore_path=keystore_path, key_id=key_id, db_path=db_path)
    assert first == second
    # The second call must NOT emit another audit event.
    assert len(_events_of_type(db_path, "gateway.key_revoked")) == 1


def test_revoke_key_unknown_key_id(tmp_path: Path) -> None:
    keystore_path = tmp_path / "keys.json"
    issue_key(
        keystore_path=keystore_path,
        name="alice",
        workspace_path=str(tmp_path),
    )
    with pytest.raises(KeystoreAdminError, match="not found"):
        revoke_key(keystore_path=keystore_path, key_id="gk_nope")


def test_revoke_key_command_prints_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    keystore_path = tmp_path / "keys.json"
    key_id, _ = issue_key(
        keystore_path=keystore_path,
        name="alice",
        workspace_path=str(tmp_path),
    )
    rc = revoke_key_command(keystore_path=keystore_path, key_id=key_id)
    captured = capsys.readouterr()
    assert rc == 0
    assert f"revoked: {key_id}" in captured.out
    assert "revoked_at:" in captured.out


# ---------------------------------------------------------------------------
# rotate-key
# ---------------------------------------------------------------------------


def test_rotate_key_inherits_metadata_and_emits_audit_event(tmp_path: Path) -> None:
    keystore_path = tmp_path / "keys.json"
    db_path = tmp_path / "metis.db"
    old_key_id, _ = issue_key(
        keystore_path=keystore_path,
        name="alice-claude-code",
        workspace_path=str(tmp_path),
        allowed_models=("anthropic:claude-haiku-4-5",),
        daily_cap_usd="5.00",
        monthly_cap_usd="50.00",
        user_id="alice",
        team_id="eng",
        db_path=db_path,
    )

    new_key_id, new_plaintext, grace_until = rotate_key(
        keystore_path=keystore_path,
        key_id=old_key_id,
        db_path=db_path,
    )

    assert new_key_id != old_key_id
    assert new_plaintext.startswith("gw_")

    store = Keystore.from_file(keystore_path)
    old_key = store.get_by_id(old_key_id)
    new_key = store.get_by_id(new_key_id)
    assert old_key is not None
    assert new_key is not None
    # Predecessor stays active until grace lapses.
    assert old_key.status == "active"
    assert old_key.grace_period_until == grace_until
    # Successor inherits all the metadata.
    assert new_key.name == old_key.name
    assert new_key.workspace_path == old_key.workspace_path
    assert new_key.allowed_models == old_key.allowed_models
    assert new_key.daily_cap_usd == old_key.daily_cap_usd
    assert new_key.monthly_cap_usd == old_key.monthly_cap_usd
    assert new_key.user_id == old_key.user_id
    assert new_key.team_id == old_key.team_id
    assert new_key.status == "active"
    assert new_key.grace_period_until is None

    rotated = _events_of_type(db_path, "gateway.key_rotated")
    assert len(rotated) == 1
    assert rotated[0]["old_gateway_key_id"] == old_key_id
    assert rotated[0]["new_gateway_key_id"] == new_key_id
    assert rotated[0]["user_id"] == "alice"
    assert rotated[0]["team_id"] == "eng"
    assert rotated[0]["workspace_path"] == old_key.workspace_path


def test_rotate_key_default_grace_period_is_24h(tmp_path: Path) -> None:
    keystore_path = tmp_path / "keys.json"
    key_id, _ = issue_key(
        keystore_path=keystore_path,
        name="alice",
        workspace_path=str(tmp_path),
    )
    now = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)
    _, _, grace_until = rotate_key(
        keystore_path=keystore_path,
        key_id=key_id,
        now=now,
    )
    assert grace_until == now + DEFAULT_GRACE_PERIOD


def test_rotate_key_custom_grace_period(tmp_path: Path) -> None:
    keystore_path = tmp_path / "keys.json"
    key_id, _ = issue_key(
        keystore_path=keystore_path,
        name="alice",
        workspace_path=str(tmp_path),
    )
    now = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)
    _, _, grace_until = rotate_key(
        keystore_path=keystore_path,
        key_id=key_id,
        grace_period=timedelta(minutes=30),
        now=now,
    )
    assert grace_until == now + timedelta(minutes=30)


def test_rotate_key_refuses_revoked_predecessor(tmp_path: Path) -> None:
    keystore_path = tmp_path / "keys.json"
    key_id, _ = issue_key(
        keystore_path=keystore_path,
        name="alice",
        workspace_path=str(tmp_path),
    )
    revoke_key(keystore_path=keystore_path, key_id=key_id)
    with pytest.raises(KeystoreAdminError, match="already revoked"):
        rotate_key(keystore_path=keystore_path, key_id=key_id)


def test_rotate_key_refuses_zero_or_negative_grace(tmp_path: Path) -> None:
    keystore_path = tmp_path / "keys.json"
    key_id, _ = issue_key(
        keystore_path=keystore_path,
        name="alice",
        workspace_path=str(tmp_path),
    )
    with pytest.raises(KeystoreAdminError, match="must be > 0"):
        rotate_key(
            keystore_path=keystore_path,
            key_id=key_id,
            grace_period=timedelta(0),
        )


def test_rotate_key_command_prints_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    keystore_path = tmp_path / "keys.json"
    old_key_id, _ = issue_key(
        keystore_path=keystore_path,
        name="alice",
        workspace_path=str(tmp_path),
    )
    rc = rotate_key_command(
        keystore_path=keystore_path,
        key_id=old_key_id,
        grace_period="1h",
    )
    captured = capsys.readouterr()
    assert rc == 0
    assert "old_key_id:" in captured.out
    assert "new_key_id:" in captured.out
    assert "new_token:" in captured.out
    assert "grace_period_until:" in captured.out


# ---------------------------------------------------------------------------
# Grace-period lifecycle
# ---------------------------------------------------------------------------


def test_grace_period_both_keys_active_during_window(tmp_path: Path) -> None:
    """During the grace period, both predecessor and successor must work."""
    keystore_path = tmp_path / "keys.json"
    old_key_id, old_token = issue_key(
        keystore_path=keystore_path,
        name="alice",
        workspace_path=str(tmp_path),
    )
    now = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)
    new_key_id, new_token, _grace_until = rotate_key(
        keystore_path=keystore_path,
        key_id=old_key_id,
        grace_period=timedelta(hours=1),
        now=now,
    )
    store = Keystore.from_file(keystore_path)
    old_key = store.authenticate(old_token)
    new_key = store.authenticate(new_token)
    assert old_key is not None
    assert new_key is not None
    # 30 minutes into the grace window — both still active.
    mid_window = now + timedelta(minutes=30)
    assert old_key.is_active(now=mid_window)
    assert new_key.is_active(now=mid_window)
    # The trace events stamp the key_id used so operators can see the migration.
    assert old_key.key_id == old_key_id
    assert new_key.key_id == new_key_id


def test_grace_period_old_key_auto_revokes_after_window(tmp_path: Path) -> None:
    """Past the grace boundary, `is_active` returns False for the predecessor."""
    keystore_path = tmp_path / "keys.json"
    old_key_id, old_token = issue_key(
        keystore_path=keystore_path,
        name="alice",
        workspace_path=str(tmp_path),
    )
    now = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)
    _, _, grace_until = rotate_key(
        keystore_path=keystore_path,
        key_id=old_key_id,
        grace_period=timedelta(hours=1),
        now=now,
    )
    store = Keystore.from_file(keystore_path)
    old_key = store.authenticate(old_token)
    assert old_key is not None
    # One second after grace lapses.
    after = grace_until + timedelta(seconds=1)
    assert not old_key.is_active(now=after)
    # The 401 body reports `revoked_at == grace_period_until`.
    assert old_key.effective_revoked_at(now=after) == grace_until


def test_sweep_expired_grace_periods_persists_revocation(tmp_path: Path) -> None:
    keystore_path = tmp_path / "keys.json"
    db_path = tmp_path / "metis.db"
    old_key_id, _ = issue_key(
        keystore_path=keystore_path,
        name="alice",
        workspace_path=str(tmp_path),
    )
    now = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)
    rotate_key(
        keystore_path=keystore_path,
        key_id=old_key_id,
        grace_period=timedelta(hours=1),
        now=now,
    )

    # Within the grace window the sweep is a no-op.
    transitioned = sweep_expired_grace_periods(
        keystore_path=keystore_path,
        now=now + timedelta(minutes=30),
        db_path=db_path,
    )
    assert transitioned == []

    # Past the boundary, the sweep auto-revokes the predecessor.
    transitioned = sweep_expired_grace_periods(
        keystore_path=keystore_path,
        now=now + timedelta(hours=2),
        db_path=db_path,
    )
    assert transitioned == [old_key_id]
    store = Keystore.from_file(keystore_path)
    revoked = store.get_by_id(old_key_id)
    assert revoked is not None
    assert revoked.status == "revoked"

    audit_events = _events_of_type(db_path, "gateway.key_revoked")
    assert len(audit_events) == 1
    assert audit_events[0]["reason"] == "grace_period_expired"
    assert audit_events[0]["gateway_key_id"] == old_key_id


def test_sweep_idempotent(tmp_path: Path) -> None:
    keystore_path = tmp_path / "keys.json"
    old_key_id, _ = issue_key(
        keystore_path=keystore_path,
        name="alice",
        workspace_path=str(tmp_path),
    )
    now = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)
    rotate_key(
        keystore_path=keystore_path,
        key_id=old_key_id,
        grace_period=timedelta(hours=1),
        now=now,
    )
    after = now + timedelta(hours=2)
    sweep_expired_grace_periods(keystore_path=keystore_path, now=after)
    second_pass = sweep_expired_grace_periods(keystore_path=keystore_path, now=after)
    assert second_pass == []


# ---------------------------------------------------------------------------
# list-keys
# ---------------------------------------------------------------------------


def test_list_keys_shape_stable_across_rotation(tmp_path: Path) -> None:
    keystore_path = tmp_path / "keys.json"
    old_key_id, _ = issue_key(
        keystore_path=keystore_path,
        name="alice",
        workspace_path=str(tmp_path),
        user_id="alice",
        team_id="eng",
    )
    listings = list_keys(keystore_path=keystore_path)
    assert len(listings) == 1
    assert listings[0].key_id == old_key_id
    assert listings[0].status == "active"
    assert listings[0].effective_status == "active"
    assert listings[0].user_id == "alice"
    assert listings[0].team_id == "eng"

    now = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)
    new_key_id, _, _ = rotate_key(
        keystore_path=keystore_path,
        key_id=old_key_id,
        grace_period=timedelta(hours=1),
        now=now,
    )

    # During the grace window both keys are listed as active.
    listings = list_keys(keystore_path=keystore_path, now=now + timedelta(minutes=30))
    assert {k.key_id for k in listings} == {old_key_id, new_key_id}
    by_id = {k.key_id: k for k in listings}
    assert by_id[old_key_id].effective_status == "active"
    assert by_id[old_key_id].grace_period_until == now + timedelta(hours=1)
    assert by_id[new_key_id].effective_status == "active"

    # After the boundary the predecessor reads as effectively revoked even
    # before the sweep persists the transition.
    after = now + timedelta(hours=2)
    listings = list_keys(keystore_path=keystore_path, now=after)
    by_id = {k.key_id: k for k in listings}
    assert by_id[old_key_id].status == "active"
    assert by_id[old_key_id].effective_status == "revoked"
    assert by_id[new_key_id].effective_status == "active"


def test_list_keys_empty_keystore(tmp_path: Path) -> None:
    keystore_path = tmp_path / "keys.json"
    keystore_path.write_text(json.dumps({"keys": []}), encoding="utf-8")
    listings = list_keys(keystore_path=keystore_path)
    assert listings == []


def test_list_keys_command_text_output_includes_revoked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    keystore_path = tmp_path / "keys.json"
    key_id, _ = issue_key(
        keystore_path=keystore_path,
        name="alice",
        workspace_path=str(tmp_path),
    )
    revoke_key(keystore_path=keystore_path, key_id=key_id)
    rc = list_keys_command(keystore_path=keystore_path)
    assert rc == 0
    captured = capsys.readouterr()
    assert key_id in captured.out
    assert "revoked" in captured.out


def test_list_keys_command_json_output_is_parseable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    keystore_path = tmp_path / "keys.json"
    issue_key(
        keystore_path=keystore_path,
        name="alice",
        workspace_path=str(tmp_path),
    )
    rc = list_keys_command(keystore_path=keystore_path, output_format="json")
    assert rc == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert isinstance(payload, list)
    assert len(payload) == 1
    assert payload[0]["status"] == "active"
    # Required fields for the JSON output contract.
    for required in (
        "key_id",
        "name",
        "workspace_path",
        "status",
        "effective_status",
        "user_id",
        "team_id",
        "allowed_models",
        "daily_cap_usd",
        "monthly_cap_usd",
        "created_at",
        "revoked_at",
        "grace_period_until",
    ):
        assert required in payload[0]


# ---------------------------------------------------------------------------
# Parsing + utilities
# ---------------------------------------------------------------------------


def test_parse_duration_supports_common_suffixes() -> None:
    assert parse_duration("30s") == timedelta(seconds=30)
    assert parse_duration("5m") == timedelta(minutes=5)
    assert parse_duration("24h") == timedelta(hours=24)
    assert parse_duration("7d") == timedelta(days=7)
    assert parse_duration("2w") == timedelta(weeks=2)


def test_parse_duration_treats_bare_digits_as_seconds() -> None:
    assert parse_duration("60") == timedelta(seconds=60)


def test_parse_duration_rejects_garbage() -> None:
    for bad in ("", "abc", "1.5h", "h", "1y"):
        with pytest.raises(ValueError):
            parse_duration(bad)


def test_atomic_write_does_not_leave_partial_file(tmp_path: Path) -> None:
    """The temp-then-rename pattern guarantees readers see a complete file."""
    from metis.gateway.keystore_admin import atomic_write_keystore

    keystore_path = tmp_path / "keys.json"
    atomic_write_keystore(keystore_path, {"keys": [{"key_id": "gk_a", "name": "a"}]})
    raw = json.loads(keystore_path.read_text(encoding="utf-8"))
    assert raw["keys"][0]["key_id"] == "gk_a"
    # No stray temp files (the mkstemp / replace cycle either commits or unlinks).
    leftover = [p for p in tmp_path.iterdir() if p.name.startswith(".keys.")]
    assert leftover == []


# ---------------------------------------------------------------------------
# Audit-event payload sanity
# ---------------------------------------------------------------------------


def test_issue_key_emits_audit_event_with_metadata(tmp_path: Path) -> None:
    keystore_path = tmp_path / "keys.json"
    db_path = tmp_path / "metis.db"
    key_id, _ = issue_key(
        keystore_path=keystore_path,
        name="alice-claude-code",
        workspace_path=str(tmp_path),
        allowed_models=("anthropic:claude-haiku-4-5",),
        daily_cap_usd="0.50",
        user_id="alice",
        team_id="eng",
        db_path=db_path,
    )
    events = _events_of_type(db_path, "gateway.key_issued")
    assert len(events) == 1
    e = events[0]
    assert e["gateway_key_id"] == key_id
    assert e["name"] == "alice-claude-code"
    assert e["user_id"] == "alice"
    assert e["team_id"] == "eng"
    assert e["allowed_models"] == ["anthropic:claude-haiku-4-5"]
    assert e["daily_cap_usd"] == "0.50"


def test_back_compat_old_keystore_loads_with_default_status(tmp_path: Path) -> None:
    """Pre-Wave-10 keystores (no `status` field) load as active."""
    keystore_path = tmp_path / "keys.json"
    keystore_path.write_text(
        json.dumps(
            {
                "keys": [
                    {
                        "key_id": "gk_legacy",
                        "secret_hash": hash_bearer_token("gw_legacy"),
                        "workspace_path": str(tmp_path),
                        "name": "legacy",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    store = Keystore.from_file(keystore_path)
    legacy = store.get_by_id("gk_legacy")
    assert legacy is not None
    assert legacy.status == "active"
    assert legacy.revoked_at is None
    assert legacy.grace_period_until is None
    assert legacy.is_active(now=datetime.now(UTC))
