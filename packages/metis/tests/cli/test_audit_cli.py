"""CLI tests for `metis audit export`."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from metis.cli.main import build_parser, main
from metis.core.events.envelope import Actor
from metis.core.events.payloads import (
    GatewayKeyIssued,
    LLMCallCompleted,
    make_event,
)
from metis.core.trace.store import TraceStore


def _seed_audit_and_operational(path: Path) -> None:
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
                ),
                timestamp=datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC),
            )
        )
        # An operational event that must NOT appear in the export.
        store.write(
            make_event(
                type="llm.call_completed",
                session_id="sess",
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
                ),
                timestamp=datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC),
            )
        )
    finally:
        store.close()


def test_audit_export_subcommand_parses():
    parser = build_parser()
    args = parser.parse_args(
        [
            "audit",
            "export",
            "/tmp/x.jsonl",
            "--db-path",
            "/tmp/metis.db",
            "--format",
            "csv",
            "--since",
            "2026-05-01T00:00:00+00:00",
            "--until",
            "2026-06-01T00:00:00+00:00",
            "--event-type",
            "gateway.key_issued",
        ]
    )
    assert args.command == "audit"
    assert args.audit_command == "export"
    assert args.dest == "/tmp/x.jsonl"
    assert args.format == "csv"
    assert args.since == "2026-05-01T00:00:00+00:00"
    assert args.event_types == ["gateway.key_issued"]


def test_audit_export_jsonl_end_to_end(tmp_path: Path, capsys):
    src_db = tmp_path / "metis.db"
    dest = tmp_path / "audit.jsonl"
    _seed_audit_and_operational(src_db)

    rc = main(
        [
            "audit",
            "export",
            str(dest),
            "--db-path",
            str(src_db),
            "--format",
            "jsonl",
            "--since",
            "2026-05-01T00:00:00+00:00",
            "--until",
            "2026-06-01T00:00:00+00:00",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "audit export complete" in out
    assert "events:         1" in out
    assert dest.exists()

    lines = dest.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert '"type":"gateway.key_issued"' in lines[0]
    # The operational event must NOT be in the export.
    assert "llm.call_completed" not in lines[0]


def test_audit_export_missing_db_emits_diagnostic(tmp_path: Path, capsys):
    rc = main(
        [
            "audit",
            "export",
            str(tmp_path / "out.jsonl"),
            "--db-path",
            str(tmp_path / "missing.db"),
        ]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "trace DB not found" in err


def test_audit_export_unknown_event_type_rejects(tmp_path: Path, capsys):
    src_db = tmp_path / "metis.db"
    _seed_audit_and_operational(src_db)
    rc = main(
        [
            "audit",
            "export",
            str(tmp_path / "out.jsonl"),
            "--db-path",
            str(src_db),
            "--event-type",
            "fake.event_type",
        ]
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "unknown event type" in err


def test_audit_export_warns_on_non_audit_event_type(tmp_path: Path, capsys):
    """Non-audit types in --event-type produce a stderr warning but the
    export still runs and filters them out (spec §8.1 lax filter)."""
    src_db = tmp_path / "metis.db"
    _seed_audit_and_operational(src_db)
    rc = main(
        [
            "audit",
            "export",
            str(tmp_path / "out.jsonl"),
            "--db-path",
            str(src_db),
            "--event-type",
            "llm.call_completed",
            "--since",
            "2026-05-01T00:00:00+00:00",
            "--until",
            "2026-06-01T00:00:00+00:00",
        ]
    )
    # Export succeeds even though the only requested type was non-audit;
    # the result is an empty file.
    assert rc == 0
    err = capsys.readouterr().err
    assert "ignoring non-audit event type" in err


def test_audit_export_refuses_existing_file(tmp_path: Path, capsys):
    src_db = tmp_path / "metis.db"
    dest = tmp_path / "out.jsonl"
    dest.write_text("pre-existing", encoding="utf-8")
    _seed_audit_and_operational(src_db)
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
        ]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "already exists" in err
    assert dest.read_text(encoding="utf-8") == "pre-existing"


def test_audit_export_default_format_is_jsonl(tmp_path: Path, capsys):
    src_db = tmp_path / "metis.db"
    dest = tmp_path / "audit.jsonl"
    _seed_audit_and_operational(src_db)

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
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "format:         jsonl" in out
