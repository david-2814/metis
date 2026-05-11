"""`metis` CLI entry point."""

from __future__ import annotations

import argparse
import asyncio
import sys

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
                "Sets the manual sticky for the session."
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
            help="Model used when no sticky / override is set. "
            "Default: anthropic:claude-sonnet-4-6",
            default="anthropic:claude-sonnet-4-6",
        )

    chat = sub.add_parser("chat", help="Start an interactive line-based REPL.")
    _add_session_args(chat)

    tui = sub.add_parser("tui", help="Start the Textual TUI.")
    _add_session_args(tui)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
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
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
