"""Smoke tests for the CLI entry point."""

from __future__ import annotations

import pytest
from metis_cli.main import build_parser, main


def test_help_returns_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "metis" in out.lower()
    assert "chat" in out


def test_chat_subcommand_requires_workspace(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["chat"])
    # argparse exits with code 2 on missing required args
    assert exc.value.code == 2


def test_chat_subcommand_parses_args():
    """We can parse `chat <workspace> --model X` without executing."""
    parser = build_parser()
    args = parser.parse_args(["chat", "/some/dir", "--model", "sonnet"])
    assert args.command == "chat"
    assert args.workspace == "/some/dir"
    assert args.model == "sonnet"


def test_chat_default_global_default_model():
    parser = build_parser()
    args = parser.parse_args(["chat", "/some/dir"])
    assert args.global_default == "anthropic:claude-sonnet-4-6"


def test_gateway_issue_key_parses_user_and_team_flags():
    """multi-user.md §4.2 — `--user` / `--team` thread through the parser."""
    parser = build_parser()
    args = parser.parse_args(
        [
            "gateway",
            "issue-key",
            "--name",
            "alice-claude-code",
            "--workspace",
            "/Users/alice/repo",
            "--user",
            "alice",
            "--team",
            "eng",
        ]
    )
    assert args.gateway_command == "issue-key"
    assert args.user == "alice"
    assert args.team == "eng"


def test_gateway_issue_key_user_and_team_default_to_none():
    parser = build_parser()
    args = parser.parse_args(
        [
            "gateway",
            "issue-key",
            "--name",
            "legacy",
            "--workspace",
            "/tmp",
        ]
    )
    assert args.user is None
    assert args.team is None
