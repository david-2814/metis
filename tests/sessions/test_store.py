"""Tests for InMemorySessionStore."""

from __future__ import annotations

from datetime import UTC, datetime

from metis.canonical.content import TextBlock
from metis.canonical.ids import new_message_id
from metis.canonical.messages import Message, Role
from metis.sessions.store import InMemorySessionStore


def test_create_session_assigns_id():
    store = InMemorySessionStore()
    s1 = store.create_session(workspace_path="/x", active_model=None)
    s2 = store.create_session(workspace_path="/y", active_model=None)
    assert s1.id != s2.id


def test_get_session_returns_same_object_with_updates():
    store = InMemorySessionStore()
    s = store.create_session(workspace_path="/x", active_model=None)
    s.active_model = "anthropic:claude-haiku-4-5"
    store.update_session(s)
    fetched = store.get_session(s.id)
    assert fetched.active_model == "anthropic:claude-haiku-4-5"


def test_messages_preserved_in_order():
    store = InMemorySessionStore()
    s = store.create_session(workspace_path="/x", active_model=None)
    for i in range(5):
        msg = Message(
            id=new_message_id(),
            session_id=s.id,
            role=Role.USER,
            content=[TextBlock(text=f"msg {i}")],
            created_at=datetime.now(UTC),
        )
        store.add_message(s.id, msg)
    msgs = store.get_messages(s.id)
    assert [m.content[0].text for m in msgs] == [f"msg {i}" for i in range(5)]


def test_get_messages_isolates_sessions():
    store = InMemorySessionStore()
    a = store.create_session(workspace_path="/a", active_model=None)
    b = store.create_session(workspace_path="/b", active_model=None)
    store.add_message(
        a.id,
        Message(
            id=new_message_id(),
            session_id=a.id,
            role=Role.USER,
            content=[TextBlock(text="a only")],
            created_at=datetime.now(UTC),
        ),
    )
    assert len(store.get_messages(a.id)) == 1
    assert len(store.get_messages(b.id)) == 0


def test_list_sessions_returns_recent_first():
    store = InMemorySessionStore()
    s1 = store.create_session(workspace_path="/x", active_model=None)
    s2 = store.create_session(workspace_path="/y", active_model=None)
    listed = store.list_sessions()
    # Two created sessions exist, most recent first.
    assert len(listed) == 2
    assert listed[0].id == s2.id
    assert listed[1].id == s1.id
