"""Tests for TaskStore CRUD."""

from __future__ import annotations

import pytest

from store import TaskNotFound, TaskStore


def test_add_returns_task_with_title():
    store = TaskStore()
    task = store.add("write tests")
    assert task.title == "write tests"
    assert store.size() == 1


def test_get_returns_task_by_id():
    store = TaskStore()
    created = store.add("ping a teammate")
    fetched = store.get(created.id)
    assert fetched is created


def test_get_missing_raises():
    store = TaskStore()
    with pytest.raises(TaskNotFound):
        store.get(9999)


def test_remove_drops_from_store():
    store = TaskStore()
    task = store.add("buy paper")
    store.remove(task.id)
    assert store.size() == 0
    with pytest.raises(TaskNotFound):
        store.get(task.id)


def test_list_all_returns_in_insert_order():
    store = TaskStore()
    a = store.add("first")
    b = store.add("second")
    listed = store.list_all()
    assert [t.title for t in listed] == [a.title, b.title]
