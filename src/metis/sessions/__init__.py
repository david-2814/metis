"""Session store and turn-loop session manager.

Two store implementations: `InMemorySessionStore` for ephemeral / test use, and
`SqliteSessionStore` for persistence across restarts (canonical-format §9.1).
"""

from metis.sessions.manager import (
    AmbiguousModelError,
    SessionManager,
    TurnResult,
    UnknownAliasError,
)
from metis.sessions.sqlite_store import SqliteSessionStore
from metis.sessions.store import InMemorySessionStore, Session, SessionStore

__all__ = [
    "AmbiguousModelError",
    "InMemorySessionStore",
    "Session",
    "SessionManager",
    "SessionStore",
    "SqliteSessionStore",
    "TurnResult",
    "UnknownAliasError",
]
