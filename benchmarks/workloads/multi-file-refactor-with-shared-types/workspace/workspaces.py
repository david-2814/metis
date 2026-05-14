"""Workspace membership helpers. Pairs UserId with WorkspaceId."""

from __future__ import annotations

from domain import UserId, WorkspaceId


def link_user_to_workspace(uid: UserId, wsid: WorkspaceId) -> tuple[UserId, WorkspaceId]:
    """Return the (UserId, WorkspaceId) tuple for downstream auditing."""
    return (uid, wsid)
