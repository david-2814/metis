"""Tests for SqliteSessionStore — persistence across reopen."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from metis_core.canonical.content import TextBlock, ToolResultBlock, ToolUseBlock
from metis_core.canonical.ids import new_message_id, new_tool_use_id
from metis_core.canonical.messages import (
    Message,
    MessageMetadata,
    MessageStatus,
    Role,
    RoutingDecisionRecord,
    RoutingMode,
    Usage,
)
from metis_core.sessions.sqlite_store import SqliteSessionStore


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "sessions.db"


def _now() -> datetime:
    return datetime.now(UTC)


# ---- Schema -----------------------------------------------------------


def test_schema_creates_required_tables(db_path: Path):
    SqliteSessionStore(db_path).close()
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    names = {r[0] for r in rows}
    assert "sessions" in names
    assert "messages" in names
    conn.close()


def test_schema_creates_message_index(db_path: Path):
    SqliteSessionStore(db_path).close()
    conn = sqlite3.connect(db_path)
    indexes = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='messages'"
    ).fetchall()
    assert any("idx_messages_session_created" == r[0] for r in indexes)
    conn.close()


def test_wal_mode_enabled(db_path: Path):
    store = SqliteSessionStore(db_path)
    cursor = store._conn.execute("PRAGMA journal_mode")
    assert cursor.fetchone()[0].lower() == "wal"
    store.close()


# ---- Session lifecycle ------------------------------------------------


def test_create_and_get_session(db_path: Path):
    store = SqliteSessionStore(db_path)
    s = store.create_session(workspace_path="/x", active_model="anthropic:claude-haiku-4-5")
    fetched = store.get_session(s.id)
    assert fetched.id == s.id
    assert fetched.workspace_path == "/x"
    assert fetched.active_model == "anthropic:claude-haiku-4-5"
    assert fetched.cost_so_far_usd == 0.0
    assert fetched.turn_count == 0
    store.close()


def test_get_unknown_session_raises(db_path: Path):
    store = SqliteSessionStore(db_path)
    with pytest.raises(KeyError):
        store.get_session("never_created")
    store.close()


def test_update_session_persists_changes(db_path: Path):
    store = SqliteSessionStore(db_path)
    s = store.create_session(workspace_path="/x", active_model=None)
    s.active_model = "anthropic:claude-sonnet-4-6"
    s.cost_so_far_usd = 0.0123
    s.turn_count = 5
    store.update_session(s)
    refreshed = store.get_session(s.id)
    assert refreshed.active_model == "anthropic:claude-sonnet-4-6"
    assert refreshed.cost_so_far_usd == pytest.approx(0.0123)
    assert refreshed.turn_count == 5
    store.close()


def test_list_sessions_returns_recent_first(db_path: Path):
    store = SqliteSessionStore(db_path)
    s1 = store.create_session(workspace_path="/a")
    s2 = store.create_session(workspace_path="/b")
    rows = store.list_sessions()
    assert [r.id for r in rows] == [s2.id, s1.id]
    store.close()


# ---- Message round-trip -----------------------------------------------


def test_message_roundtrip_simple_text(db_path: Path):
    store = SqliteSessionStore(db_path)
    s = store.create_session(workspace_path="/x")
    msg = Message(
        id=new_message_id(),
        session_id=s.id,
        role=Role.USER,
        content=[TextBlock(text="hello")],
        created_at=_now(),
    )
    store.add_message(s.id, msg)
    msgs = store.get_messages(s.id)
    assert len(msgs) == 1
    assert msgs[0].id == msg.id
    assert msgs[0].role == Role.USER
    assert isinstance(msgs[0].content[0], TextBlock)
    assert msgs[0].content[0].text == "hello"
    store.close()


def test_message_roundtrip_assistant_with_full_metadata(db_path: Path):
    store = SqliteSessionStore(db_path)
    s = store.create_session(workspace_path="/x")
    msg = Message(
        id=new_message_id(),
        session_id=s.id,
        role=Role.ASSISTANT,
        content=[
            TextBlock(text="I'll read it."),
            ToolUseBlock(id=new_tool_use_id(), name="read_file", input={"path": "x"}),
        ],
        created_at=_now(),
        metadata=MessageMetadata(
            model="anthropic:claude-sonnet-4-6",
            provider="anthropic",
            routing=RoutingDecisionRecord(
                mode=RoutingMode.MANUAL,
                chosen_model="anthropic:claude-sonnet-4-6",
                reason="sticky",
            ),
            usage=Usage(
                input_tokens=10,
                output_tokens=5,
                cached_input_tokens=0,
                cache_creation_input_tokens=0,
                cost_usd=Decimal("0.001234"),
                pricing_version="2026-05-08",
                latency_ms=42,
            ),
            status=MessageStatus.COMPLETE,
        ),
    )
    store.add_message(s.id, msg)
    msgs = store.get_messages(s.id)
    assert len(msgs) == 1
    fetched = msgs[0]
    # Content with tagged union round-trips correctly.
    assert isinstance(fetched.content[0], TextBlock)
    assert isinstance(fetched.content[1], ToolUseBlock)
    assert fetched.content[1].input == {"path": "x"}
    # Metadata round-trips including Decimal cost.
    assert fetched.metadata.usage is not None
    assert fetched.metadata.usage.cost_usd == Decimal("0.001234")
    assert fetched.metadata.routing is not None
    assert fetched.metadata.routing.mode == RoutingMode.MANUAL
    store.close()


def test_message_roundtrip_tool_message(db_path: Path):
    store = SqliteSessionStore(db_path)
    s = store.create_session(workspace_path="/x")
    tool_use_id = new_tool_use_id()
    msg = Message(
        id=new_message_id(),
        session_id=s.id,
        role=Role.TOOL,
        content=[ToolResultBlock(tool_use_id=tool_use_id, content=[TextBlock(text="result")])],
        created_at=_now(),
        metadata=MessageMetadata(parent_tool_use_id=tool_use_id),
    )
    store.add_message(s.id, msg)
    fetched = store.get_messages(s.id)[0]
    assert isinstance(fetched.content[0], ToolResultBlock)
    assert fetched.content[0].tool_use_id == tool_use_id
    assert fetched.metadata.parent_tool_use_id == tool_use_id
    store.close()


def test_messages_isolated_per_session(db_path: Path):
    store = SqliteSessionStore(db_path)
    a = store.create_session(workspace_path="/a")
    b = store.create_session(workspace_path="/b")
    store.add_message(
        a.id,
        Message(
            id=new_message_id(),
            session_id=a.id,
            role=Role.USER,
            content=[TextBlock(text="for a")],
            created_at=_now(),
        ),
    )
    assert len(store.get_messages(a.id)) == 1
    assert len(store.get_messages(b.id)) == 0
    store.close()


def test_messages_returned_in_order(db_path: Path):
    store = SqliteSessionStore(db_path)
    s = store.create_session(workspace_path="/x")
    for i in range(5):
        store.add_message(
            s.id,
            Message(
                id=new_message_id(),
                session_id=s.id,
                role=Role.USER,
                content=[TextBlock(text=f"msg {i}")],
                created_at=_now(),
            ),
        )
    msgs = store.get_messages(s.id)
    assert [m.content[0].text for m in msgs] == [f"msg {i}" for i in range(5)]
    store.close()


# ---- Persistence across reopen ----------------------------------------


def test_session_survives_store_reopen(db_path: Path):
    store = SqliteSessionStore(db_path)
    s = store.create_session(workspace_path="/x", active_model="anthropic:claude-haiku-4-5")
    store.add_message(
        s.id,
        Message(
            id=new_message_id(),
            session_id=s.id,
            role=Role.USER,
            content=[TextBlock(text="hello")],
            created_at=_now(),
        ),
    )
    store.close()

    # Reopen — message history should still be there.
    reopened = SqliteSessionStore(db_path)
    fetched = reopened.get_session(s.id)
    assert fetched.workspace_path == "/x"
    assert fetched.active_model == "anthropic:claude-haiku-4-5"
    msgs = reopened.get_messages(s.id)
    assert len(msgs) == 1
    assert msgs[0].content[0].text == "hello"
    reopened.close()


def test_message_id_primary_key_uniqueness(db_path: Path):
    store = SqliteSessionStore(db_path)
    s = store.create_session(workspace_path="/x")
    msg = Message(
        id=new_message_id(),
        session_id=s.id,
        role=Role.USER,
        content=[TextBlock(text="x")],
        created_at=_now(),
    )
    store.add_message(s.id, msg)
    with pytest.raises(sqlite3.IntegrityError):
        store.add_message(s.id, msg)
    store.close()
