"""`metis` CLI entry point."""

from __future__ import annotations

import argparse
import asyncio
import sys

from dotenv import load_dotenv

from metis.cli.chat import run_chat


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
            from metis.tui.app import run_tui

            return asyncio.run(
                run_tui(
                    workspace_path=args.workspace,
                    initial_model=args.model,
                    db_path=args.db_path,
                    global_default_model=args.global_default,
                )
            )
        if args.command == "serve":
            from metis.cli.serve import run_serve

            return asyncio.run(
                run_serve(
                    workspace_path=args.workspace,
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
