"""`metis gateway issue-key` CLI tests.

Covers the v1 keystore-append behavior and the multi-user.md §4.2 `--user` /
`--team` extension: tagged keys persist their identity, validation rejects
malformed tags, and existing un-tagged issuance is unaffected.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from metis_gateway.auth import Keystore
from metis_gateway.issue_key import IssueKeyError, issue_key, issue_key_command


def test_issue_key_creates_keystore_and_round_trips(tmp_path: Path) -> None:
    keystore_path = tmp_path / "keys.json"
    key_id, plaintext = issue_key(
        keystore_path=keystore_path,
        name="alpha",
        workspace_path=str(tmp_path),
    )
    assert key_id.startswith("gk_")
    assert plaintext.startswith("gw_")
    store = Keystore.from_file(keystore_path)
    matched = store.authenticate(plaintext)
    assert matched is not None
    assert matched.key_id == key_id
    assert matched.user_id is None
    assert matched.team_id is None


def test_issue_key_persists_user_and_team_tags(tmp_path: Path) -> None:
    """--user / --team values land in the keystore JSON and survive a reload."""
    keystore_path = tmp_path / "keys.json"
    key_id, plaintext = issue_key(
        keystore_path=keystore_path,
        name="alice-claude-code",
        workspace_path=str(tmp_path),
        user_id="alice",
        team_id="eng",
    )

    raw = json.loads(keystore_path.read_text(encoding="utf-8"))
    entry = next(k for k in raw["keys"] if k["key_id"] == key_id)
    assert entry["user_id"] == "alice"
    assert entry["team_id"] == "eng"

    store = Keystore.from_file(keystore_path)
    matched = store.authenticate(plaintext)
    assert matched is not None
    assert matched.user_id == "alice"
    assert matched.team_id == "eng"
    identity = store.identify(plaintext)
    assert identity is not None
    assert identity.user_id == "alice"
    assert identity.team_id == "eng"


def test_issue_key_only_writes_set_identity_fields(tmp_path: Path) -> None:
    """Untagged keys must not write `user_id: null` into the JSON — the file
    has to remain backwards-compatible with pre-multi-user readers."""
    keystore_path = tmp_path / "keys.json"
    issue_key(
        keystore_path=keystore_path,
        name="untagged",
        workspace_path=str(tmp_path),
    )
    raw = json.loads(keystore_path.read_text(encoding="utf-8"))
    entry = raw["keys"][0]
    assert "user_id" not in entry
    assert "team_id" not in entry


def test_issue_key_only_writes_team_when_only_team_set(tmp_path: Path) -> None:
    keystore_path = tmp_path / "keys.json"
    issue_key(
        keystore_path=keystore_path,
        name="team-only",
        workspace_path=str(tmp_path),
        team_id="eng",
    )
    entry = json.loads(keystore_path.read_text(encoding="utf-8"))["keys"][0]
    assert "user_id" not in entry
    assert entry["team_id"] == "eng"


def test_issue_key_rejects_invalid_user_id(tmp_path: Path) -> None:
    keystore_path = tmp_path / "keys.json"
    with pytest.raises(IssueKeyError, match="lowercase alphanumerics"):
        issue_key(
            keystore_path=keystore_path,
            name="bad",
            workspace_path=str(tmp_path),
            user_id="Alice Smith",
        )
    # Failed issuance must not have created/touched the keystore file when it
    # was the first issuance. We accept either "no file" or "empty keys list"
    # — the contract is just that we don't persist a malformed key.
    if keystore_path.exists():
        raw = json.loads(keystore_path.read_text(encoding="utf-8"))
        assert raw.get("keys") == []


def test_issue_key_rejects_invalid_team_id(tmp_path: Path) -> None:
    with pytest.raises(IssueKeyError, match="lowercase alphanumerics"):
        issue_key(
            keystore_path=tmp_path / "keys.json",
            name="bad",
            workspace_path=str(tmp_path),
            team_id="ENG.PLATFORM",
        )


def test_issue_key_appends_to_existing_keystore(tmp_path: Path) -> None:
    """Issuing a second tagged key alongside a v1 (untagged) key keeps both."""
    keystore_path = tmp_path / "keys.json"
    legacy_id, _ = issue_key(
        keystore_path=keystore_path,
        name="legacy",
        workspace_path=str(tmp_path),
    )
    tagged_id, _ = issue_key(
        keystore_path=keystore_path,
        name="new",
        workspace_path=str(tmp_path),
        user_id="bob",
        team_id="ops",
    )
    store = Keystore.from_file(keystore_path)
    legacy_key = store.get_by_id(legacy_id)
    tagged_key = store.get_by_id(tagged_id)
    assert legacy_key is not None
    assert legacy_key.user_id is None
    assert tagged_key is not None
    assert tagged_key.user_id == "bob"
    assert tagged_key.team_id == "ops"


def test_issue_key_command_prints_identity_when_set(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = issue_key_command(
        keystore_path=tmp_path / "keys.json",
        name="alice",
        workspace_path=str(tmp_path),
        user_id="alice",
        team_id="eng",
    )
    assert rc == 0
    captured = capsys.readouterr()
    # Identity fields surface in the post-issuance summary so the operator
    # can confirm at a glance that the binding stuck.
    assert "user:   alice" in captured.out
    assert "team:   eng" in captured.out


def test_issue_key_command_omits_identity_lines_for_untagged_keys(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = issue_key_command(
        keystore_path=tmp_path / "keys.json",
        name="legacy",
        workspace_path=str(tmp_path),
    )
    assert rc == 0
    captured = capsys.readouterr()
    assert "user:" not in captured.out
    assert "team:" not in captured.out


def test_issue_key_command_exits_nonzero_on_validation_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = issue_key_command(
        keystore_path=tmp_path / "keys.json",
        name="bad",
        workspace_path=str(tmp_path),
        user_id="Bad Tag",
    )
    assert rc == 1
    captured = capsys.readouterr()
    assert "error:" in captured.err


# ---------------------------------------------------------------------------
# multi-user.md §5 — daily / monthly cap CLI plumbing
# ---------------------------------------------------------------------------


def test_issue_key_persists_daily_and_monthly_caps_as_decimal(tmp_path: Path) -> None:
    """`--daily-cap-usd` / `--monthly-cap-usd` round-trip through the keystore
    JSON as Decimal-stable strings so reload doesn't drift via float."""
    from decimal import Decimal

    keystore_path = tmp_path / "keys.json"
    key_id, plaintext = issue_key(
        keystore_path=keystore_path,
        name="capped",
        workspace_path=str(tmp_path),
        daily_cap_usd="0.50",
        monthly_cap_usd="50.00",
    )

    raw = json.loads(keystore_path.read_text(encoding="utf-8"))
    entry = next(k for k in raw["keys"] if k["key_id"] == key_id)
    # Persisted as Decimal-as-string (analytics/store.py convention) so reload
    # via Decimal(str(value)) is exact.
    assert entry["daily_cap_usd"] == "0.50"
    assert entry["monthly_cap_usd"] == "50.00"

    store = Keystore.from_file(keystore_path)
    matched = store.authenticate(plaintext)
    assert matched is not None
    assert matched.daily_cap_usd == Decimal("0.50")
    assert matched.monthly_cap_usd == Decimal("50.00")


def test_issue_key_only_writes_set_cap_fields(tmp_path: Path) -> None:
    """An untagged key must not write `daily_cap_usd: null` — keystore stays
    forwards/backwards compatible with pre-quota readers."""
    keystore_path = tmp_path / "keys.json"
    issue_key(
        keystore_path=keystore_path,
        name="untouched",
        workspace_path=str(tmp_path),
    )
    entry = json.loads(keystore_path.read_text(encoding="utf-8"))["keys"][0]
    assert "daily_cap_usd" not in entry
    assert "monthly_cap_usd" not in entry


def test_issue_key_rejects_zero_or_negative_cap(tmp_path: Path) -> None:
    keystore_path = tmp_path / "keys.json"
    with pytest.raises(IssueKeyError, match="must be > 0"):
        issue_key(
            keystore_path=keystore_path,
            name="bad",
            workspace_path=str(tmp_path),
            daily_cap_usd="0",
        )
    with pytest.raises(IssueKeyError, match="must be > 0"):
        issue_key(
            keystore_path=keystore_path,
            name="bad",
            workspace_path=str(tmp_path),
            monthly_cap_usd="-1.5",
        )


def test_issue_key_rejects_unparseable_cap(tmp_path: Path) -> None:
    with pytest.raises(IssueKeyError, match="positive number"):
        issue_key(
            keystore_path=tmp_path / "keys.json",
            name="bad",
            workspace_path=str(tmp_path),
            daily_cap_usd="abc",
        )


def test_issue_key_command_prints_caps_when_set(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = issue_key_command(
        keystore_path=tmp_path / "keys.json",
        name="capped",
        workspace_path=str(tmp_path),
        daily_cap_usd="0.50",
        monthly_cap_usd="50.00",
    )
    assert rc == 0
    captured = capsys.readouterr()
    assert "daily_cap_usd: 0.50" in captured.out
    assert "monthly_cap_usd: 50.00" in captured.out


def test_keystore_back_compat_loads_legacy_float_daily_cap(tmp_path: Path) -> None:
    """A pre-quota keystore file that stored `daily_cap_usd` as a JSON number
    must still load (the field type widened from float to Decimal)."""
    from decimal import Decimal

    keystore_path = tmp_path / "keys.json"
    legacy = {
        "keys": [
            {
                "key_id": "gk_legacy",
                "secret_hash": "a" * 64,
                "name": "legacy",
                "workspace_path": str(tmp_path),
                "daily_cap_usd": 0.25,
            }
        ]
    }
    keystore_path.write_text(json.dumps(legacy), encoding="utf-8")
    store = Keystore.from_file(keystore_path)
    key = store.get_by_id("gk_legacy")
    assert key is not None
    # Legacy float coerces via Decimal(str(0.25)) -> exact "0.25".
    assert key.daily_cap_usd == Decimal("0.25")
    assert key.monthly_cap_usd is None
