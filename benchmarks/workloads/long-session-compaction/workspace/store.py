"""In-memory task store with simple CRUD.

Persistence, search, and querying are deliberately not built yet —
features land turn-by-turn as the user asks for them.
"""

from __future__ import annotations

from tasks import Task


class TaskNotFound(Exception):
    """Raised when looking up a task id that isn't in the store."""


class TaskStore:
    def __init__(self) -> None:
        self._tasks: dict[int, Task] = {}

    def add(self, title: str) -> Task:
        task = Task(title=title)
        self._tasks[task.id] = task
        return task

    def get(self, task_id: int) -> Task:
        if task_id not in self._tasks:
            raise TaskNotFound(f"no task with id={task_id}")
        return self._tasks[task_id]

    def remove(self, task_id: int) -> None:
        if task_id not in self._tasks:
            raise TaskNotFound(f"no task with id={task_id}")
        del self._tasks[task_id]

    def list_all(self) -> list[Task]:
        return list(self._tasks.values())

    def size(self) -> int:
        return len(self._tasks)
