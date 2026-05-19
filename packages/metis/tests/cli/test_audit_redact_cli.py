"""CLI tests for `metis audit export --redact <mode>` and the enhanced
`metis user forget` dry-run behavior.

See `docs/specs/redaction.md`. Lives in apps/cli/tests because both
exercises drive `metis.cli.main.main()` end-to-end.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from metis.cli.main import build_parser, main
from metis.core.events.envelope import Actor
from metis.core.events.payloads import (
    GatewayKeyIssued,
    LLMCallCompleted,
    make_event,
)
from metis.core.redaction import PSEUDONYM_PREFIX
from metis.core.trace.store import TraceStore


def _seed_db(path: Path) -> None:
    store = TraceStore(path)
    try:
        store.write(
            make_event(
                type="gateway.key_issued",
                session_id="sess",
                actor=Actor.SYSTEM,
                payload=GatewayKeyIssued(
                    gateway_key_id="gk_1",
                    name="alice",
                    workspace_path="/srv/alice",
                    issued_at=datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC),
                    user_id="alice",
                    team_id="team_a",
                ),
                timestamp=datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC),
            )
        )
        store.write(
            make_event(
                type="llm.call_completed",
                session_id="sess",
                turn_id="turn-1",
                actor=Actor.AGENT,
                payload=LLMCallCompleted(
                    model="anthropic:claude-haiku-4-5",
                    provider="anthropic",
                    input_tokens=10,
                    output_tokens=10,
                    cached_input_tokens=0,
                    cache_creation_input_tokens=0,
                    cost_usd=0.001,
                    pricing_version="v1",
                    latency_ms=100,
                    stop_reason="end_turn",
                    produced_tool_calls=0,
                    produced_thinking_blocks=0,
                    user_id="alice",
                    team_id="team_a",
                ),
                timestamp=datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC),
            )
        )
    finally:
        store.close()


def test_audit_export_redact_arg_parses():
    parser = build_parser()
    args = parser.parse_args(
        [
            "audit",
            "export",
            "/tmp/x.jsonl",
            "--redact",
            "pseudonymize",
        ]
    )
    assert args.redact == "pseudonymize"


def test_audit_export_redact_default_is_passthrough():
    parser = build_parser()
    args = parser.parse_args(["audit", "export", "/tmp/x.jsonl"])
    assert args.redact == "passthrough"


def test_audit_export_pseudonymize_end_to_end(tmp_path: Path, capsys):
    src_db = tmp_path / "metis.db"
    dest = tmp_path / "out.jsonl"
    _seed_db(src_db)

    rc = main(
        [
            "audit",
            "export",
            str(dest),
            "--db-path",
            str(src_db),
            "--since",
            "2026-05-01T00:00:00+00:00",
            "--until",
            "2026-06-01T00:00:00+00:00",
            "--redact",
            "pseudonymize",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "redact mode:    pseudonymize" in out

    lines = dest.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1  # only gateway.key_issued is audit-flagged
    obj = json.loads(lines[0])
    # session_id is pseudonymized
    assert obj["session_id"].startswith(PSEUDONYM_PREFIX)
    # user_id in payload is pseudonymized
    assert obj["payload"]["user_id"].startswith(PSEUDONYM_PREFIX)
    # workspace_path is pseudonymized
    assert obj["payload"]["workspace_path"].startswith(PSEUDONYM_PREFIX)


def test_audit_export_aggregate_only_writes_single_json(tmp_path: Path):
    src_db = tmp_path / "metis.db"
    dest = tmp_path / "agg.json"
    _seed_db(src_db)

    rc = main(
        [
            "audit",
            "export",
            str(dest),
            "--db-path",
            str(src_db),
            "--since",
            "2026-05-01T00:00:00+00:00",
            "--until",
            "2026-06-01T00:00:00+00:00",
            "--redact",
            "aggregate_only",
        ]
    )
    assert rc == 0
    text = dest.read_text(encoding="utf-8").strip()
    agg = json.loads(text)
    assert "event_count" in agg
    assert agg["events_by_type"] == {"gateway.key_issued": 1}


def test_audit_export_unknown_redact_mode_rejects(tmp_path: Path, capsys):
    """argparse rejects the choice before main() runs and exits with SystemExit."""
    src_db = tmp_path / "metis.db"
    _seed_db(src_db)
    import pytest

    with pytest.raises(SystemExit):
        main(
            [
                "audit",
                "export",
                str(tmp_path / "out.jsonl"),
                "--db-path",
                str(src_db),
                "--redact",
                "bogus_mode",
            ]
        )
    err = capsys.readouterr().err
    assert "bogus_mode" in err or "invalid choice" in err


def test_user_forget_dry_run_prints_would_be_affected_count(tmp_path: Path, capsys):
    src_db = tmp_path / "metis.db"
    _seed_db(src_db)
    rc = main(
        [
            "user",
            "forget",
            "alice",
            "--db-path",
            str(src_db),
        ]
    )
    # --confirm missing → returns 2 and tells the operator what would happen
    assert rc == 2
    err = capsys.readouterr().err
    assert "would pseudonymize" in err
    # 2 events stamped with user_id=alice
    assert "2 event(s)" in err
    # Still gives the --confirm hint
    assert "--confirm" in err


def test_user_forget_with_confirm_completes(tmp_path: Path, capsys):
    src_db = tmp_path / "metis.db"
    _seed_db(src_db)
    rc = main(
        [
            "user",
            "forget",
            "alice",
            "--confirm",
            "--db-path",
            str(src_db),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "user-forget complete" in out
    assert "pseudonymized rows:     2" in out
