"""Tests for canonical id generation."""

from metis.core.canonical.ids import new_message_id, new_session_id, new_tool_use_id


def test_session_id_format():
    sid = new_session_id()
    assert sid.startswith("sess_")
    # ULID body is 26 chars.
    assert len(sid) == len("sess_") + 26


def test_tool_use_id_format():
    tid = new_tool_use_id()
    assert tid.startswith("tu_")
    assert len(tid) == len("tu_") + 26


def test_message_id_is_bare_ulid():
    mid = new_message_id()
    assert not mid.startswith(("sess_", "tu_"))
    assert len(mid) == 26


def test_ids_are_unique_and_monotonic():
    ids = [new_message_id() for _ in range(100)]
    assert len(set(ids)) == 100
    # ULIDs are sortable; within a process, generated in order = lexicographic.
    assert ids == sorted(ids)
