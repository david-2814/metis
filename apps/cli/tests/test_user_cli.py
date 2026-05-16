"""CLI tests for `metis analytics user-export` / `metis user forget`."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from metis_cli.main import build_parser
from metis_cli.user import run_user_export_command, run_user_forget_command

_EVENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY,
  timestamp_us INTEGER NOT NULL,
  session_id TEXT NOT NULL,
  turn_id TEXT,
  parent_event_id TEXT,
  type TEXT NOT NULL,
  actor TEXT NOT NULL,
  sensitivity TEXT NOT NULL,
  payload_json TEXT NOT NULL
);
"""


def _to_micros(dt: datetime) -> int:
    epoch = datetime(1970, 1, 1, tzinfo=dt.tzinfo)
    delta = dt - epoch
    return delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds


def _seed_user_events(db: Path, user_id: str, *, count: int = 1) -> None:
    conn = sqlite3.connect(str(db), isolation_level=None)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(_EVENTS_SCHEMA)
    now = datetime.now(UTC)
    for i in range(count):
        conn.execute(
            "INSERT INTO events "
            "(id, timestamp_us, session_id, turn_id, parent_event_id, type, "
            " actor, sensitivity, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"01HZ{i:020d}",
                _to_micros(now),
                "sess_a",
                "turn_a",
                None,
                "llm.call_completed",
                "agent",
                "pseudonymous",
                json.dumps(
                    {
                        "model": "anthropic:claude-sonnet-4-6",
                        "provider": "anthropic",
                        "input_tokens": 100,
                        "output_tokens": 10,
                        "cached_input_tokens": 0,
                        "cache_creation_input_tokens": 0,
                        "cost_usd": "0.05",
                        "pricing_version": "test-1",
                        "latency_ms": 1000,
                        "stop_reason": "end_turn",
                        "produced_tool_calls": 0,
                        "produced_thinking_blocks": 0,
                        "user_id": user_id,
                    }
                ),
            ),
        )
    conn.close()


# ---- argparse wiring ------------------------------------------------------


def test_analytics_user_export_subcommand_parses():
    parser = build_parser()
    args = parser.parse_args(
        [
            "analytics",
            "user-export",
            "usr_alice",
            "--from",
            "2026-05-01T00:00:00Z",
            "--to",
            "2026-05-15T00:00:00Z",
            "--out",
            "/tmp/alice.jsonl",
            "--db-path",
            "/tmp/metis.db",
        ]
    )
    assert args.command == "analytics"
    assert args.analytics_command == "user-export"
    assert args.user_id == "usr_alice"
    assert args.from_ == "2026-05-01T00:00:00Z"
    assert args.to == "2026-05-15T00:00:00Z"
    assert args.out == "/tmp/alice.jsonl"
    assert args.db_path == "/tmp/metis.db"


def test_user_forget_subcommand_parses():
    parser = build_parser()
    args = parser.parse_args(
        [
            "user",
            "forget",
            "usr_alice",
            "--confirm",
            "--db-path",
            "/tmp/metis.db",
        ]
    )
    assert args.command == "user"
    assert args.user_command == "forget"
    assert args.user_id == "usr_alice"
    assert args.confirm is True
    assert args.db_path == "/tmp/metis.db"


def test_user_forget_without_confirm_still_parses():
    """argparse accepts the missing --confirm; the refusal lives in the handler."""
    parser = build_parser()
    args = parser.parse_args(["user", "forget", "usr_alice"])
    assert args.confirm is False


# ---- export handler ------------------------------------------------------


def test_user_export_writes_to_out_file(tmp_path: Path):
    db = tmp_path / "metis.db"
    _seed_user_events(db, "usr_alice", count=3)
    out = tmp_path / "alice.jsonl"

    rc = run_user_export_command(
        user_id="usr_alice",
        from_=None,
        to=None,
        out=str(out),
        db_path=str(db),
    )
    assert rc == 0
    lines = out.read_bytes().splitlines()
    assert len(lines) == 3
    for line in lines:
        obj = json.loads(line)
        assert obj["payload"]["user_id"] == "usr_alice"


def test_user_export_to_stdout_streams_bytes(tmp_path: Path, capsysbinary):
    db = tmp_path / "metis.db"
    _seed_user_events(db, "usr_alice", count=2)

    rc = run_user_export_command(
        user_id="usr_alice",
        from_=None,
        to=None,
        out=None,
        db_path=str(db),
    )
    assert rc == 0
    out_bytes = capsysbinary.readouterr().out
    assert out_bytes.count(b"\n") == 2
    assert b'"user_id":"usr_alice"' in out_bytes


def test_user_export_missing_db_returns_1(tmp_path: Path, capsys):
    rc = run_user_export_command(
        user_id="usr_alice",
        from_=None,
        to=None,
        out=None,
        db_path=str(tmp_path / "nope.db"),
    )
    assert rc == 1
    assert "trace DB not found" in capsys.readouterr().err


def test_user_export_invalid_window_returns_1(tmp_path: Path, capsys):
    db = tmp_path / "metis.db"
    _seed_user_events(db, "usr_alice", count=1)
    rc = run_user_export_command(
        user_id="usr_alice",
        from_="not-a-date",
        to=None,
        out=None,
        db_path=str(db),
    )
    assert rc == 1
    assert "invalid time window" in capsys.readouterr().err


# ---- forget handler ------------------------------------------------------


def test_user_forget_without_confirm_refuses(tmp_path: Path, capsys):
    db = tmp_path / "metis.db"
    _seed_user_events(db, "usr_alice", count=1)
    rc = run_user_forget_command(
        user_id="usr_alice",
        confirm=False,
        db_path=str(db),
    )
    assert rc == 2
    assert "--confirm" in capsys.readouterr().err


def test_user_forget_with_confirm_pseudonymizes_and_audits(tmp_path: Path, capsys):
    db = tmp_path / "metis.db"
    _seed_user_events(db, "usr_alice", count=2)

    rc = run_user_forget_command(
        user_id="usr_alice",
        confirm=True,
        db_path=str(db),
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "user-forget complete" in out
    assert "pseudonymized rows:     2" in out

    # An `analytics.user_forgotten` audit event landed.
    conn = sqlite3.connect(str(db), isolation_level=None)
    rows = list(
        conn.execute(
            "SELECT payload_json FROM events WHERE type = ?", ("analytics.user_forgotten",)
        )
    )
    conn.close()
    assert len(rows) == 1
    payload = json.loads(rows[0][0])
    assert payload["subject_user_id"] == "usr_alice"
    assert payload["pseudonymized_rows"] == 2


def test_user_forget_missing_db_returns_1(tmp_path: Path, capsys):
    rc = run_user_forget_command(
        user_id="usr_alice",
        confirm=True,
        db_path=str(tmp_path / "nope.db"),
    )
    assert rc == 1
    assert "trace DB not found" in capsys.readouterr().err
