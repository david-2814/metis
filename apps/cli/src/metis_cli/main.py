"""`metis` CLI entry point."""

from __future__ import annotations

import argparse
import asyncio
import sys

from dotenv import load_dotenv

from metis_cli.chat import run_chat


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="metis",
        description="Local-first AI agent CLI.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def _add_session_args(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "workspace",
            help="Workspace directory the agent operates in.",
        )
        p.add_argument(
            "--model",
            help=(
                "Initial model alias or canonical id "
                "(e.g. 'sonnet' or 'anthropic:claude-sonnet-4-6'). "
                "Sets the Active model for the session."
            ),
            default=None,
        )
        p.add_argument(
            "--db-path",
            help="SQLite path for trace + sessions. Default: ~/.metis/metis.db",
            default=None,
        )
        p.add_argument(
            "--global-default",
            help="Model used when no Active model / override is set. "
            "Default: anthropic:claude-sonnet-4-6",
            default="anthropic:claude-sonnet-4-6",
        )

    chat = sub.add_parser("chat", help="Start an interactive line-based REPL.")
    _add_session_args(chat)

    tui = sub.add_parser("tui", help="Start the Textual TUI.")
    _add_session_args(tui)

    serve = sub.add_parser("serve", help="Run the HTTP/WebSocket server (loopback only).")
    serve.add_argument(
        "workspace",
        help="Workspace directory the agent operates in.",
    )
    serve.add_argument(
        "--db-path",
        help="SQLite path for trace + sessions. Default: ~/.metis/metis.db",
        default=None,
    )
    serve.add_argument(
        "--global-default",
        help="Model used when no Active model / override is set. Default: anthropic:claude-sonnet-4-6",
        default="anthropic:claude-sonnet-4-6",
    )
    serve.add_argument("--host", default="127.0.0.1", help="Bind host (loopback-only in v1).")
    serve.add_argument("--port", type=int, default=8421, help="Bind port.")

    evaluate = sub.add_parser(
        "evaluate",
        help="Re-evaluate trace-DB subjects with the heuristic judge (evaluator.md §6.2).",
    )
    evaluate.add_argument("--db-path", required=True, help="Trace DB path.")
    evaluate.add_argument(
        "--subject",
        choices=("turn", "tool_cycle", "session"),
        default="turn",
        help="Subject kind to re-evaluate (default: turn).",
    )
    evaluate.add_argument("--since", help="ISO 8601 UTC start of window (inclusive).")
    evaluate.add_argument("--until", help="ISO 8601 UTC end of window (inclusive).")
    evaluate.add_argument(
        "--session-id",
        help="Restrict to a single session (default: all sessions in window).",
    )

    gateway = sub.add_parser(
        "gateway",
        help="Run the transparent OpenAI/Anthropic-shape HTTP gateway, or issue keys.",
    )
    gateway_sub = gateway.add_subparsers(dest="gateway_command", required=False)

    # `metis gateway` with no subcommand = start the server.
    gateway.add_argument(
        "--keystore",
        help="Gateway keystore path. Default: ~/.metis/gateway/keys.json",
        default=None,
    )
    gateway.add_argument(
        "--db-path",
        help="SQLite path for trace + sessions. Default: ~/.metis/metis.db",
        default=None,
    )
    gateway.add_argument(
        "--global-default",
        help="Model used when routing finds no other slot win. "
        "Default: anthropic:claude-sonnet-4-6",
        default="anthropic:claude-sonnet-4-6",
    )
    gateway.add_argument("--host", default="127.0.0.1", help="Bind host (loopback-only in v1).")
    gateway.add_argument("--port", type=int, default=8422, help="Bind port.")

    issue = gateway_sub.add_parser(
        "issue-key",
        help="Create a new gateway key and append it to the keystore (prints token once).",
    )
    issue.add_argument(
        "--keystore",
        help="Gateway keystore path. Default: ~/.metis/gateway/keys.json",
        default=None,
    )
    issue.add_argument("--name", required=True, help="Display name for the key.")
    issue.add_argument(
        "--workspace",
        required=True,
        help="Workspace path the key is scoped to.",
    )
    issue.add_argument(
        "--allow-model",
        action="append",
        default=None,
        help="Restrict the key to a model id or alias (repeat for multiple).",
    )
    issue.add_argument(
        "--daily-cap-usd",
        type=str,
        default=None,
        help=(
            "Optional per-day spend cap (USD). Hard breaker per multi-user.md §5.1. "
            "Must parse as a positive number."
        ),
    )
    issue.add_argument(
        "--monthly-cap-usd",
        type=str,
        default=None,
        help=(
            "Optional per-calendar-month spend cap (USD). Hard breaker per "
            "multi-user.md §5.1. Must parse as a positive number."
        ),
    )
    issue.add_argument(
        "--user",
        default=None,
        help=(
            "Optional user_id tag for per-developer cost attribution "
            "(multi-user.md §4.2). Lowercase [a-z0-9_-]+."
        ),
    )
    issue.add_argument(
        "--team",
        default=None,
        help=(
            "Optional team_id tag for per-team cost attribution "
            "(multi-user.md §4.2). Lowercase [a-z0-9_-]+."
        ),
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    # Load .env from the current working directory (or any parent) before
    # we touch os.environ for API keys. Idempotent; no-op if no .env exists.
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args(argv)
    # Ctrl-C arrives while the REPL is awaiting `asyncio.to_thread(input, ...)`,
    # which means SIGINT can't be seen by the blocked worker thread. The
    # resulting KeyboardInterrupt propagates out of asyncio.run; catch it here
    # so the user sees a clean exit instead of a traceback. 130 is the
    # conventional SIGINT exit code.
    try:
        if args.command == "chat":
            return asyncio.run(
                run_chat(
                    workspace_path=args.workspace,
                    initial_model=args.model,
                    db_path=args.db_path,
                    global_default_model=args.global_default,
                )
            )
        if args.command == "tui":
            # Import lazily so users without the textual extra don't pay
            # import cost when running `metis chat`.
            from metis_cli.tui.app import run_tui

            return asyncio.run(
                run_tui(
                    workspace_path=args.workspace,
                    initial_model=args.model,
                    db_path=args.db_path,
                    global_default_model=args.global_default,
                )
            )
        if args.command == "serve":
            from metis_cli.serve import run_serve

            return asyncio.run(
                run_serve(
                    workspace_path=args.workspace,
                    db_path=args.db_path,
                    global_default_model=args.global_default,
                    host=args.host,
                    port=args.port,
                )
            )
        if args.command == "evaluate":
            from metis_core.eval import evaluate_main

            evaluate_argv: list[str] = ["--db-path", args.db_path, "--subject", args.subject]
            if args.since:
                evaluate_argv.extend(["--since", args.since])
            if args.until:
                evaluate_argv.extend(["--until", args.until])
            if args.session_id:
                evaluate_argv.extend(["--session-id", args.session_id])
            return evaluate_main(evaluate_argv)
        if args.command == "gateway":
            if args.gateway_command == "issue-key":
                from pathlib import Path

                from metis_gateway.issue_key import issue_key_command
                from metis_gateway.runtime import default_keystore_path

                keystore = (
                    Path(args.keystore).expanduser() if args.keystore else default_keystore_path()
                )
                allowed = tuple(args.allow_model) if args.allow_model else None
                return issue_key_command(
                    keystore_path=keystore,
                    name=args.name,
                    workspace_path=args.workspace,
                    allowed_models=allowed,
                    daily_cap_usd=args.daily_cap_usd,
                    monthly_cap_usd=args.monthly_cap_usd,
                    user_id=args.user,
                    team_id=args.team,
                )
            # Default: run the gateway server.
            from metis_gateway.cli import run_gateway_command

            return asyncio.run(
                run_gateway_command(
                    keystore_path=args.keystore,
                    db_path=args.db_path,
                    global_default_model=args.global_default,
                    host=args.host,
                    port=args.port,
                )
            )
    except KeyboardInterrupt:
        print()
        return 130
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
