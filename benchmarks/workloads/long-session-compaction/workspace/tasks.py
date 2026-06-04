"""Task model — the core record this app is built around.

A `Task` is a thing the user wants to track: a title, a done-flag, and a
stable id assigned at creation. Everything else is built up over time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import count


_id_counter = count(start=1)


def _next_id() -> int:
    return next(_id_counter)


@dataclass
class Task:
    """One row in the user's task list."""

    title: str
    done: bool = False
    id: int = field(default_factory=_next_id)

    def mark_done(self) -> None:
        self.done = True

    def mark_undone(self) -> None:
        self.done = False
