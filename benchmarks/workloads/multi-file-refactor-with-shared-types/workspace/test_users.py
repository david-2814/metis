"""End-to-end tests that import from every module in the workspace.

Touching every module is deliberate: a rename that misses any file produces
an ImportError here, which is what the benchmark's substring assertion
checks for ("2 passed" in the final pytest output).
"""

from __future__ import annotations

from domain import UserId, UserMap, WorkspaceId
from handlers.user_handler import handle_user_request
from handlers.workspace_handler import handle_workspace_link
from legacy import coerce, is_user
from users import index_users, make_user
from workspaces import link_user_to_workspace


def test_make_user_roundtrip() -> None:
    uid = make_user("alice")
    assert isinstance(uid, UserId)
    assert uid.value == "alice"
    indexed: UserMap = index_users(["alice", "bob"])
    assert len(indexed) == 2
    assert indexed[uid] == "alice"


def test_handlers_compose() -> None:
    uid = UserId(value="alice")
    wsid = WorkspaceId(value="ws1")
    pair = link_user_to_workspace(uid, wsid)
    assert pair == (uid, wsid)
    body_summary = handle_user_request(uid, {"k": 1})
    link_summary = handle_workspace_link(uid, wsid)
    assert "alice" in body_summary
    assert link_summary == "alice->ws1"


def test_legacy_shim() -> None:
    uid = coerce("alice")
    assert is_user(uid)
    assert isinstance(uid, UserId)
