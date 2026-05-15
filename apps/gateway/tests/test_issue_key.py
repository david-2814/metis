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
