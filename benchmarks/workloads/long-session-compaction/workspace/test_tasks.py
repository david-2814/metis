"""Tests for the Task dataclass."""

from __future__ import annotations

from tasks import Task


def test_task_defaults_to_not_done():
    t = Task(title="buy milk")
    assert t.title == "buy milk"
    assert t.done is False


def test_task_assigns_ids_in_order():
    a = Task(title="first")
    b = Task(title="second")
    assert b.id > a.id


def test_mark_done_flips_flag():
    t = Task(title="ship it")
    assert t.done is False
    t.mark_done()
    assert t.done is True


def test_mark_undone_restores():
    t = Task(title="oops")
    t.mark_done()
    t.mark_undone()
    assert t.done is False
