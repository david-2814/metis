"""HTTP-ish handler for workspace-membership requests."""

from __future__ import annotations

from domain import UserId, WorkspaceId
from workspaces import link_user_to_workspace


def handle_workspace_link(uid: UserId, wsid: WorkspaceId) -> str:
    """Link a UserId into a WorkspaceId and return a printable summary."""
    pair = link_user_to_workspace(uid, wsid)
    return f"{pair[0].value}->{pair[1].value}"
