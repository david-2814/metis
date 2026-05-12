"""Session model + in-memory store.

Phase 1 prototype keeps everything in process memory. The SessionStore
Protocol is the boundary so the SQLite-backed version per canonical-format
§9.1 can slot in later without touching SessionManager.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from metis.canonical.ids import new_session_id
from metis.canonical.messages import Message


@dataclass
class Session:
    """Active session metadata. Messages live in the store separately."""

    id: str
    workspace_path: str
    active_model: str | None  # MANUAL_STICKY value; None means rule-based routing
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    cost_so_far_usd: float = 0.0
    turn_count: int = 0


class SessionStore(Protocol):
    """Persistence boundary. Implementations: InMemorySessionStore (v1),
    SqliteSessionStore (later)."""

    def create_session(
        self, *, workspace_path: str, active_model: str | None = None
    ) -> Session: ...

    def get_session(self, session_id: str) -> Session: ...

    def list_sessions(self) -> list[Session]: ...

    def update_session(self, session: Session) -> None: ...

    def add_message(self, session_id: str, message: Message) -> None: ...

    def get_messages(self, session_id: str) -> list[Message]: ...


class InMemorySessionStore:
    """Process-local session store. Lost on restart."""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._messages: dict[str, list[Message]] = {}

    def create_session(self, *, workspace_path: str, active_model: str | None = None) -> Session:
        session = Session(
            id=new_session_id(),
            workspace_path=workspace_path,
            active_model=active_model,
        )
        self._sessions[session.id] = session
        self._messages[session.id] = []
        return session

    def get_session(self, session_id: str) -> Session:
        return self._sessions[session_id]

    def list_sessions(self) -> list[Session]:
        return sorted(self._sessions.values(), key=lambda s: s.created_at, reverse=True)

    def update_session(self, session: Session) -> None:
        self._sessions[session.id] = session

    def add_message(self, session_id: str, message: Message) -> None:
        self._messages.setdefault(session_id, []).append(message)

    def get_messages(self, session_id: str) -> list[Message]:
        return list(self._messages.get(session_id, []))
