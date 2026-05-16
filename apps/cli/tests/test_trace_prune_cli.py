"""CLI tests for `metis trace prune` per trace-retention.md §9."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from metis_cli.main import build_parser, main
from metis_core.events.envelope import Actor
from metis_core.events.payloads import GatewayKeyIssued, SessionCreated, make_event
from metis_core.trace.store import TraceStore


def _seed(path: Path, *, old_count: int, new_count: int, audit_count: int = 0) -> None:
    """Seed `path` with `old_count` >120-day events, `new_count` <1-day events,
    and `audit_count` >120-day audit-flagged events."""
    store = TraceStore(path)
    try:
        now = datetime.now(UTC)
        old_ts = now - timedelta(days=120)
        new_ts = now - timedelta(hours=1)
        for i in range(old_count):
            store.write(
                make_event(
                    type="session.created",
                    session_id=f"old_{i}",
                    actor=Actor.SYSTEM,
                    payload=SessionCreated(
                        workspace_path="/x",
                        workspace_hash="h",
                        initial_active_model=None,
                        routing_policy_version="v",
                    ),
                    timestamp=old_ts,
                )
            )
        for i in range(new_count):
            store.write(
                make_event(
                    type="session.created",
                    session_id=f"new_{i}",
                    actor=Actor.SYSTEM,
                    payload=SessionCreated(
                        workspace_path="/y",
                        workspace_hash="h2",
                        initial_active_model=None,
                        routing_policy_version="v",
                    ),
                    timestamp=new_ts,
                )
            )
        for i in range(audit_count):
            store.write(
                make_event(
                    type="gateway.key_issued",
                    session_id="system",
                    actor=Actor.SYSTEM,
                    payload=GatewayKeyIssued(
                        gateway_key_id=f"gk_audit_{i}",
                        name="legacy",
                        workspace_path="/x",
                        issued_at=old_ts,
                    ),
                    timestamp=old_ts,
                )
            )
    finally:
        store.close()


def test_trace_prune_subcommand_parses():
    parser = build_parser()
    args = parser.parse_args(
        ["trace", "prune", "--days", "30", "--db-path", "/tmp/metis.db", "--dry-run"]
    )
    assert args.command == "trace"
    assert args.trace_command == "prune"
    assert args.days == 30
    assert args.db_path == "/tmp/metis.db"
    assert args.dry_run is True


def test_trace_prune_default_days_is_90():
    parser = build_parser()
    args = parser.parse_args(["trace", "prune"])
    assert args.days == 90
    assert args.dry_run is False


def test_trace_prune_dry_run_reports_without_deleting(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    db = tmp_path / "metis.db"
    _seed(db, old_count=3, new_count=2)

    rc = main(["trace", "prune", "--days", "90", "--db-path", str(db), "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "trace prune complete (dry_run=true)" in out
    # Original session.created rows still there. Note: the CLI attaches the
    # trace store to the bus to persist `trace.swept` in apply mode, which
    # also lands `bus.subscriber_*` lifecycle events in the same DB — those
    # are unrelated to the prune contract and excluded from this check.
    store = TraceStore(db)
    try:
        (count,) = store._conn.execute(
            "SELECT COUNT(*) FROM events WHERE type = 'session.created'"
        ).fetchone()
    finally:
        store.close()
    assert count == 5


def test_trace_prune_apply_deletes_and_preserves_audit_events(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    db = tmp_path / "metis.db"
    _seed(db, old_count=3, new_count=2, audit_count=1)

    rc = main(["trace", "prune", "--days", "90", "--db-path", str(db)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "trace prune complete (dry_run=false)" in out
    assert "rows_deleted:          3" in out

    store = TraceStore(db)
    try:
        types = {r[0] for r in store._conn.execute("SELECT type FROM events").fetchall()}
        # Old non-audit rows gone.
        rows_old = store._conn.execute(
            "SELECT COUNT(*) FROM events WHERE session_id LIKE 'old_%'"
        ).fetchone()[0]
        # Audit row survives.
        rows_audit = store._conn.execute(
            "SELECT COUNT(*) FROM events WHERE type = 'gateway.key_issued'"
        ).fetchone()[0]
        # `trace.swept` from the sweep itself persisted.
        rows_swept = store._conn.execute(
            "SELECT COUNT(*) FROM events WHERE type = 'trace.swept'"
        ).fetchone()[0]
    finally:
        store.close()

    assert rows_old == 0
    assert rows_audit == 1
    assert rows_swept == 1
    assert "gateway.key_issued" in types
    assert "trace.swept" in types


def test_trace_prune_missing_db_returns_nonzero(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    rc = main(["trace", "prune", "--db-path", str(tmp_path / "does-not-exist.db")])
    assert rc != 0
    err = capsys.readouterr().err
    assert "trace prune failed" in err


def test_trace_prune_invalid_days_rejected(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    db = tmp_path / "metis.db"
    _seed(db, old_count=0, new_count=1)
    rc = main(["trace", "prune", "--days", "0", "--db-path", str(db)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "--days must be positive" in err
