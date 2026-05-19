"""Session store and turn-loop session manager.

Two store implementations: `InMemorySessionStore` for ephemeral / test use, and
`SqliteSessionStore` for persistence across restarts (canonical-format §9.1).
"""

from metis.core.sessions.manager import (
    AmbiguousModelError,
    OverrideError,
    SessionManager,
    TurnResult,
    UnknownAliasError,
    UserExplicitModelRejectedError,
)
from metis.core.sessions.sqlite_store import SqliteSessionStore
from metis.core.sessions.store import InMemorySessionStore, Session, SessionStore

__all__ = [
    "AmbiguousModelError",
    "InMemorySessionStore",
    "OverrideError",
    "Session",
    "SessionManager",
    "SessionStore",
    "SqliteSessionStore",
    "TurnResult",
    "UnknownAliasError",
    "UserExplicitModelRejectedError",
]
