"""Shared domain types used across the user / workspace surface.

We're renaming `UserId` -> `AccountId` repo-wide. Update every reference
(class definition, type annotations, imports, tests) and the type alias's
referent below.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class UserId:
    """Stable per-account identifier. Used as a dict key, so frozen."""

    value: str


@dataclass(frozen=True)
class WorkspaceId:
    value: str


UserMap = dict[UserId, str]
