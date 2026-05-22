"""Smoke tests for the CLI entry point."""

from __future__ import annotations

import pytest
from metis.cli.main import build_parser, main


def test_help_returns_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "metis" in out.lower()
    # `dev` is the advised command; `chat` is a kept alias.
    assert "dev" in out


@pytest.mark.parametrize("command", ["dev", "chat"])
def test_dev_subcommand_requires_workspace(command):
    with pytest.raises(SystemExit) as exc:
        main([command])
    # argparse exits with code 2 on missing required args
    assert exc.value.code == 2


@pytest.mark.parametrize("command", ["dev", "chat"])
def test_dev_subcommand_parses_args(command):
    """`dev` and its `chat` alias parse `<workspace> --model X` identically."""
    parser = build_parser()
    args = parser.parse_args([command, "/some/dir", "--model", "sonnet"])
    # argparse records the invoked name; the router treats both the same.
    assert args.command == command
    assert args.workspace == "/some/dir"
    assert args.model == "sonnet"


@pytest.mark.parametrize("command", ["dev", "chat"])
def test_dev_default_global_default_model(command):
    parser = build_parser()
    args = parser.parse_args([command, "/some/dir"])
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


# ---------------------------------------------------------------------------
# Wave 13 — `metis gateway` bind / TLS / connection-cap flag wiring
# (gateway-hardening.md §2.1).
# ---------------------------------------------------------------------------


def test_gateway_default_host_is_loopback_preserved():
    """Default `--host` remains 127.0.0.1 even though Wave 13 lifts the
    forced rewrite. Back-compat is the load-bearing property."""
    parser = build_parser()
    args = parser.parse_args(["gateway"])
    assert args.host == "127.0.0.1"
    assert args.port == 8422
    assert args.tls_cert is None
    assert args.tls_key is None
    assert args.max_connections == 1000
    assert args.reuse_port is False


def test_gateway_accepts_zero_zero_zero_zero_host():
    """`--host 0.0.0.0` parses; the silent loopback rewrite is gone."""
    parser = build_parser()
    args = parser.parse_args(["gateway", "--host", "0.0.0.0"])
    assert args.host == "0.0.0.0"


def test_gateway_parses_tls_flags():
    parser = build_parser()
    args = parser.parse_args(
        [
            "gateway",
            "--tls-cert",
            "/etc/metis/tls/cert.pem",
            "--tls-key",
            "/etc/metis/tls/key.pem",
        ]
    )
    assert args.tls_cert == "/etc/metis/tls/cert.pem"
    assert args.tls_key == "/etc/metis/tls/key.pem"


def test_gateway_parses_max_connections_override():
    parser = build_parser()
    args = parser.parse_args(["gateway", "--max-connections", "5000"])
    assert args.max_connections == 5000


def test_gateway_parses_reuse_port_flag():
    parser = build_parser()
    args = parser.parse_args(["gateway", "--reuse-port"])
    assert args.reuse_port is True
