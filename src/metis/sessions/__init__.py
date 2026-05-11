"""Session store and turn-loop session manager.

For the Phase 1 prototype the store is in-memory. The canonical-format §9.1
SQLite schema can drop in later without changing the SessionManager surface.
"""

from metis.sessions.manager import (
    SessionManager,
    TurnResult,
    UnknownAliasError,
)
from metis.sessions.store import InMemorySessionStore, Session, SessionStore

__all__ = [
    "InMemorySessionStore",
    "Session",
    "SessionManager",
    "SessionStore",
    "TurnResult",
    "UnknownAliasError",
]
