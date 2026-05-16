"""CLI tests for `metis trace vacuum` per docs/operations/trace-performance.md §4."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from metis_cli.main import build_parser, main
from metis_core.events.envelope import Actor
from metis_core.events.payloads import SessionCreated, make_event
from metis_core.trace.store import TraceStore


def _seed(path: Path, *, count: int) -> None:
    store = TraceStore(path)
    try:
        now = datetime.now(UTC)
        for i in range(count):
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
                    timestamp=now,
                )
            )
    finally:
        store.close()


def test_trace_vacuum_subcommand_parses():
    parser = build_parser()
    args = parser.parse_args(["trace", "vacuum", "--db-path", "/tmp/metis.db"])
    assert args.command == "trace"
    assert args.trace_command == "vacuum"
    assert args.db_path == "/tmp/metis.db"


def test_trace_vacuum_runs_and_reports(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    db = tmp_path / "metis.db"
    _seed(db, count=200)
    # Delete most rows so VACUUM has slack to reclaim.
    store = TraceStore(db)
    store._conn.execute("DELETE FROM events WHERE rowid % 2 = 0")
    store.close()

    rc = main(["trace", "vacuum", "--db-path", str(db)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "trace vacuum complete" in out
    assert "bytes_reclaimed:" in out
    # Surviving rows still present after VACUUM.
    store = TraceStore(db)
    try:
        (count,) = store._conn.execute(
            "SELECT COUNT(*) FROM events WHERE type = 'session.created'"
        ).fetchone()
    finally:
        store.close()
    assert count == 100


def test_trace_vacuum_missing_db_returns_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    rc = main(["trace", "vacuum", "--db-path", str(tmp_path / "missing.db")])
    assert rc == 1
    err = capsys.readouterr().err
    assert "trace vacuum failed" in err
