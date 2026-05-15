"""CLI tests for `metis backup` / `metis restore`."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from metis_cli.main import build_parser, main
from metis_core.events.envelope import Actor
from metis_core.events.payloads import SessionCreated, make_event
from metis_core.trace.store import TraceStore


def _seed(path: Path, n: int) -> None:
    store = TraceStore(path)
    try:
        for i in range(n):
            store.write(
                make_event(
                    type="session.created",
                    session_id=f"sess_{i}",
                    actor=Actor.SYSTEM,
                    payload=SessionCreated(
                        workspace_path="/x",
                        workspace_hash="h",
                        initial_active_model=None,
                        routing_policy_version="v",
                    ),
                    timestamp=datetime.now(UTC),
                )
            )
    finally:
        store.close()


def test_backup_subcommand_parses():
    parser = build_parser()
    args = parser.parse_args(["backup", "/tmp/snap.db", "--db-path", "/tmp/metis.db"])
    assert args.command == "backup"
    assert args.dest == "/tmp/snap.db"
    assert args.db_path == "/tmp/metis.db"


def test_restore_subcommand_parses_with_force():
    parser = build_parser()
    args = parser.parse_args(["restore", "/tmp/snap.db", "--db-path", "/tmp/metis.db", "--force"])
    assert args.command == "restore"
    assert args.source == "/tmp/snap.db"
    assert args.db_path == "/tmp/metis.db"
    assert args.force is True


def test_backup_then_restore_via_cli(tmp_path: Path, capsys):
    src_db = tmp_path / "metis.db"
    backup_path = tmp_path / "snap.db"
    restored_db = tmp_path / "restored.db"
    _seed(src_db, n=3)

    rc = main(["backup", str(backup_path), "--db-path", str(src_db)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "backup complete" in out
    assert "events:         3" in out
    assert backup_path.exists()

    rc = main(["restore", str(backup_path), "--db-path", str(restored_db)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "restore complete" in out
    assert "events:         3" in out


def test_restore_failure_emits_diagnostic_and_nonzero_exit(tmp_path: Path, capsys):
    """Restoring a nonexistent backup prints a diagnostic to stderr and exits 1."""
    rc = main(["restore", str(tmp_path / "nope.db"), "--db-path", str(tmp_path / "dest.db")])
    assert rc == 1
    err = capsys.readouterr().err
    assert "restore failed" in err


def test_backup_failure_when_dest_exists(tmp_path: Path, capsys):
    src_db = tmp_path / "metis.db"
    _seed(src_db, n=1)
    target = tmp_path / "snap.db"
    target.write_bytes(b"already-here")

    rc = main(["backup", str(target), "--db-path", str(src_db)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "backup failed" in err
    assert target.read_bytes() == b"already-here"
