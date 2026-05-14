"""Legacy shim kept around for the deprecated v1 dispatcher.

Imports the user identifier under a short local alias (`UID`) because the
v1 dispatcher's call sites all expect a single-letter type. Keep the alias
local; don't rename `UID` itself when renaming the shared type.
"""

from __future__ import annotations

from domain import UserId as UID


def is_user(x: object) -> bool:
    """True iff `x` is the canonical user identifier."""
    return isinstance(x, UID)


def coerce(name: str) -> UID:
    """Wrap a raw name in the canonical user identifier (legacy entry point)."""
    return UID(value=name)
