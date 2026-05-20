"""CLI tests for `metis auth` (add / list / remove / test / doctor)."""

from __future__ import annotations

import io
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from metis.cli.auth import (
    ValidateResult,
    run_auth_add,
    run_auth_doctor,
    run_auth_list,
    run_auth_remove,
    run_auth_test,
)
from metis.cli.main import build_parser
from metis.core.credentials import CredentialsFile, truncate_key

# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------


def test_auth_subcommands_parse() -> None:
    parser = build_parser()
    for argv in (
        ["auth", "list"],
        ["auth", "add", "anthropic"],
        ["auth", "add", "anthropic", "--no-validate"],
        ["auth", "remove", "anthropic"],
        ["auth", "test"],
        ["auth", "test", "openai"],
        ["auth", "doctor"],
    ):
        args = parser.parse_args(argv)
        assert args.command == "auth"
        assert args.auth_command == argv[1]


def test_bare_auth_prints_help_instead_of_erroring() -> None:
    """Spec §5: `metis auth` without a subcommand is a discoverability
    affordance, not an error. argparse's `--help` action raises SystemExit(0)
    after printing — main() never returns, so we assert on that exit code."""
    import pytest
    from metis.cli.main import main

    with pytest.raises(SystemExit) as exc:
        main(["auth"])
    assert exc.value.code == 0


# ---------------------------------------------------------------------------
# `metis auth add`
# ---------------------------------------------------------------------------


def test_auth_add_no_validate_writes_file(tmp_path: Path) -> None:
    creds = tmp_path / "credentials.yaml"
    out = io.StringIO()
    code = run_auth_add(
        provider="anthropic",
        validate=False,
        file_path=creds,
        api_key="sk-ant-test-1234567890",
        output_stream=out,
    )
    assert code == 0
    assert creds.exists()
    file = CredentialsFile.load(creds)
    assert file.providers["anthropic"] == "sk-ant-test-1234567890"
    # Output is truncated, not the raw key.
    assert "sk-ant-test-1234567890" not in out.getvalue()
    assert truncate_key("sk-ant-test-1234567890") in out.getvalue()


def test_auth_add_rejects_unknown_provider(tmp_path: Path) -> None:
    out = io.StringIO()
    code = run_auth_add(
        provider="madeup-provider",
        validate=False,
        file_path=tmp_path / "credentials.yaml",
        api_key="x" * 30,
        output_stream=out,
    )
    assert code == 2


def test_auth_add_validates_when_validate_true(tmp_path: Path, monkeypatch) -> None:
    creds = tmp_path / "credentials.yaml"
    calls: list[tuple[str, str]] = []

    def fake_validate(provider: str, key: str) -> ValidateResult:
        calls.append((provider, key))
        return ValidateResult(ok=True, latency_ms=42, status_code=200, message="ok")

    monkeypatch.setattr("metis.cli.auth.validate_provider", fake_validate)
    out = io.StringIO()
    code = run_auth_add(
        provider="openai",
        validate=True,
        file_path=creds,
        api_key="sk-test-abcdef1234567890",
        output_stream=out,
    )
    assert code == 0
    assert calls == [("openai", "sk-test-abcdef1234567890")]
    assert "ok (42 ms)" in out.getvalue()


def test_auth_add_aborts_when_validation_fails(tmp_path: Path, monkeypatch) -> None:
    creds = tmp_path / "credentials.yaml"

    def fake_validate(provider: str, key: str) -> ValidateResult:
        return ValidateResult(ok=False, latency_ms=100, status_code=401, message="AUTH error")

    monkeypatch.setattr("metis.cli.auth.validate_provider", fake_validate)
    out = io.StringIO()
    code = run_auth_add(
        provider="openai",
        validate=True,
        file_path=creds,
        api_key="sk-bad-abcdef1234567890",
        output_stream=out,
    )
    assert code == 1
    assert not creds.exists()


# ---------------------------------------------------------------------------
# `metis auth list`
# ---------------------------------------------------------------------------


def test_auth_list_renders_truncated_keys(tmp_path: Path) -> None:
    creds = tmp_path / "credentials.yaml"
    CredentialsFile(
        path=creds,
        providers={"anthropic": "sk-ant-abcdefghij1234"},
    ).save()
    out = io.StringIO()
    code = run_auth_list(
        file_path=creds,
        legacy_dotenv_path=tmp_path / "missing.env",
        env={"OPENAI_API_KEY": "sk-openai-zyxwvutsr987"},
        output_stream=out,
    )
    assert code == 0
    body = out.getvalue()
    # Full keys never appear.
    assert "sk-ant-abcdefghij1234" not in body
    assert "sk-openai-zyxwvutsr987" not in body
    # Truncated forms do.
    assert truncate_key("sk-ant-abcdefghij1234") in body
    assert truncate_key("sk-openai-zyxwvutsr987") in body
    # The provider not configured anywhere has a sentinel.
    assert "(not configured)" in body
    # Provenance label includes the env-var name for env-sourced rows.
    assert "OPENAI_API_KEY" in body


def test_auth_list_with_no_sources_shows_all_unconfigured(
    tmp_path: Path,
) -> None:
    out = io.StringIO()
    code = run_auth_list(
        file_path=tmp_path / "missing.yaml",
        legacy_dotenv_path=tmp_path / "missing.env",
        env={},
        output_stream=out,
    )
    assert code == 0
    body = out.getvalue()
    assert body.count("(not configured)") == 3


# ---------------------------------------------------------------------------
# `metis auth remove`
# ---------------------------------------------------------------------------


def test_auth_remove_is_idempotent_when_missing(tmp_path: Path) -> None:
    out = io.StringIO()
    code = run_auth_remove(
        provider="anthropic",
        file_path=tmp_path / "missing.yaml",
        output_stream=out,
    )
    assert code == 0
    assert "nothing to remove" in out.getvalue()


def test_auth_remove_drops_entry_from_file(tmp_path: Path) -> None:
    creds = tmp_path / "credentials.yaml"
    CredentialsFile(
        path=creds,
        providers={"anthropic": "x" * 30, "openai": "y" * 30},
    ).save()
    out = io.StringIO()
    code = run_auth_remove(provider="anthropic", file_path=creds, output_stream=out)
    assert code == 0
    after = CredentialsFile.load(creds)
    assert "anthropic" not in after.providers
    assert "openai" in after.providers


# ---------------------------------------------------------------------------
# `metis auth test`
# ---------------------------------------------------------------------------


def test_auth_test_calls_validate_for_each_configured_provider(
    tmp_path: Path,
) -> None:
    creds = tmp_path / "credentials.yaml"
    CredentialsFile(
        path=creds,
        providers={"anthropic": "sk-ant-1234567890"},
    ).save()
    calls: list[str] = []

    def fake_validate(provider: str, key: str) -> ValidateResult:
        calls.append(provider)
        return ValidateResult(ok=True, latency_ms=10, status_code=200, message="ok")

    out = io.StringIO()
    code = run_auth_test(
        file_path=creds,
        legacy_dotenv_path=tmp_path / "missing.env",
        env={"OPENAI_API_KEY": "sk-test-1234567890"},
        validate_fn=fake_validate,
        output_stream=out,
    )
    assert code == 0
    assert set(calls) == {"anthropic", "openai"}
    body = out.getvalue()
    assert "anthropic" in body and "openai" in body


def test_auth_test_failure_returns_nonzero(tmp_path: Path) -> None:
    creds = tmp_path / "credentials.yaml"
    CredentialsFile(
        path=creds,
        providers={"anthropic": "sk-ant-1234567890"},
    ).save()

    def fake_validate(provider: str, key: str) -> ValidateResult:
        return ValidateResult(ok=False, latency_ms=200, status_code=401, message="AUTH error")

    out = io.StringIO()
    code = run_auth_test(
        file_path=creds,
        legacy_dotenv_path=tmp_path / "missing.env",
        env={},
        validate_fn=fake_validate,
        output_stream=out,
    )
    assert code == 1
    assert "FAIL" in out.getvalue()


def test_auth_test_with_no_configured_providers_returns_nonzero(
    tmp_path: Path,
) -> None:
    out = io.StringIO()
    code = run_auth_test(
        file_path=tmp_path / "missing.yaml",
        legacy_dotenv_path=tmp_path / "missing.env",
        env={},
        validate_fn=lambda *_: ValidateResult(True, 0, 200, "ok"),
        output_stream=out,
    )
    assert code == 1
    assert "no providers configured" in out.getvalue()


def test_auth_test_specific_provider(tmp_path: Path) -> None:
    creds = tmp_path / "credentials.yaml"
    CredentialsFile(
        path=creds,
        providers={"anthropic": "sk-ant-1234567890"},
    ).save()
    calls: list[str] = []

    def fake_validate(provider: str, key: str) -> ValidateResult:
        calls.append(provider)
        return ValidateResult(ok=True, latency_ms=5, status_code=200, message="ok")

    code = run_auth_test(
        provider="anthropic",
        file_path=creds,
        legacy_dotenv_path=tmp_path / "missing.env",
        env={"OPENAI_API_KEY": "x" * 30},
        validate_fn=fake_validate,
        output_stream=io.StringIO(),
    )
    assert code == 0
    assert calls == ["anthropic"]


# ---------------------------------------------------------------------------
# `metis auth doctor`
# ---------------------------------------------------------------------------


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


def _seed_trace(db: Path, *, provider: str, completed_at: datetime, auth_fails: int = 0) -> None:
    conn = sqlite3.connect(str(db), isolation_level=None)
    conn.executescript(_EVENTS_SCHEMA)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    ts_us = int((completed_at - epoch).total_seconds() * 1_000_000)
    conn.execute(
        "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "01HZ00000000000000000001",
            ts_us,
            "sess",
            None,
            None,
            "llm.call_completed",
            "agent",
            "pseudonymous",
            f'{{"provider":"{provider}","model":"m","input_tokens":1,'
            '"output_tokens":1,"cached_input_tokens":0,"cache_creation_input_tokens":0,'
            '"cost_usd":0.001,"pricing_version":"v1","latency_ms":10,'
            '"stop_reason":"end_turn","produced_tool_calls":0,"produced_thinking_blocks":0}',
        ),
    )
    for i in range(auth_fails):
        conn.execute(
            "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"01HZ0000000000000000010{i}",
                ts_us + i,
                "sess",
                None,
                None,
                "llm.call_failed",
                "agent",
                "pseudonymous",
                f'{{"provider":"{provider}","model":"m","error_class":"auth",'
                '"error_message_redacted":"401","retry_count":0,"latency_ms":10}',
            ),
        )
    conn.close()


def test_auth_doctor_renders_resolver_state_and_trace_history(
    tmp_path: Path,
) -> None:
    creds = tmp_path / "credentials.yaml"
    CredentialsFile(
        path=creds,
        providers={"anthropic": "sk-ant-1234567890"},
        default_provider="anthropic",
    ).save()
    db = tmp_path / "metis.db"
    now = datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC)
    _seed_trace(db, provider="anthropic", completed_at=now - timedelta(hours=1), auth_fails=2)

    out = io.StringIO()
    code = run_auth_doctor(
        file_path=creds,
        legacy_dotenv_path=tmp_path / "missing.env",
        env={},
        db_path=db,
        now=now,
        output_stream=out,
    )
    body = out.getvalue()
    assert code == 0
    assert "Credential resolver:" in body
    assert "anthropic" in body
    assert "last successful call:" in body
    assert "recent AUTH errors:    2 (last 24h)" in body
    # Default provider rendered.
    assert "Default provider: anthropic" in body
    # Unconfigured providers carry an actionable hint.
    assert "metis auth add openai" in body
    assert "metis auth add openrouter" in body


def test_auth_doctor_handles_missing_trace_db(tmp_path: Path) -> None:
    out = io.StringIO()
    code = run_auth_doctor(
        file_path=tmp_path / "missing.yaml",
        legacy_dotenv_path=tmp_path / "missing.env",
        env={"ANTHROPIC_API_KEY": "sk-ant-1234567890"},
        db_path=tmp_path / "nonexistent.db",
        output_stream=out,
    )
    assert code == 0
    body = out.getvalue()
    assert "ANTHROPIC_API_KEY" in body
    # No history block when the DB doesn't exist.
    assert "last successful call:" not in body


def test_auth_doctor_flags_insecure_credentials_file(tmp_path: Path) -> None:
    creds = tmp_path / "credentials.yaml"
    CredentialsFile(path=creds, providers={"anthropic": "x" * 30}).save()
    os.chmod(creds, 0o644)
    out = io.StringIO()
    code = run_auth_doctor(
        file_path=creds,
        legacy_dotenv_path=tmp_path / "missing.env",
        env={},
        output_stream=out,
    )
    assert code == 0
    assert "insecure mode 0644" in out.getvalue()
