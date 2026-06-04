"""CLI entrypoint — wire user commands to the TaskStore.

Built up over time: today it has `add` and `list`. Tomorrow it gets
priorities, due dates, search, persistence.
"""

from __future__ import annotations

import argparse
import sys

from store import TaskStore


def cmd_add(store: TaskStore, args: argparse.Namespace) -> int:
    task = store.add(args.title)
    print(f"#{task.id}: {task.title}")
    return 0


def cmd_list(store: TaskStore, args: argparse.Namespace) -> int:
    for task in store.list_all():
        marker = "[x]" if task.done else "[ ]"
        print(f"{marker} #{task.id} {task.title}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tasks")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="add a new task")
    p_add.add_argument("title")
    p_add.set_defaults(func=cmd_add)

    p_list = sub.add_parser("list", help="list all tasks")
    p_list.set_defaults(func=cmd_list)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    store = TaskStore()
    return args.func(store, args)


if __name__ == "__main__":
    sys.exit(main())
