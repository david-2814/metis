"""Per-session in-flight turn registry.

`POST /sessions/{id}/turns` returns 202 immediately and schedules the turn
as a background task. The TurnExecutor enforces "at most one turn in flight
per session" (server-api.md §4.2 → 409 `turn_in_flight`) and exposes
cancellation hooks for `POST /sessions/{id}/turns/{turn_id}/cancel`.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from metis.sessions.manager import SessionManager, TurnResult

logger = logging.getLogger(__name__)


@dataclass
class InFlightTurn:
    session_id: str
    turn_id: str
    task: asyncio.Task[TurnResult]


class TurnExecutor:
    """Schedules turns as background tasks and tracks the active turn per
    session. Cancellation propagates by cancelling the task — the session
    manager catches asyncio.CancelledError and emits `turn.cancelled`."""

    def __init__(self, manager: SessionManager) -> None:
        self._manager = manager
        self._in_flight: dict[str, InFlightTurn] = {}

    def has_in_flight(self, session_id: str) -> bool:
        return session_id in self._in_flight

    def submit(self, session_id: str, user_text: str) -> str:
        """Schedule a turn; return a synthetic turn_id used for status lookup.

        Note: the real turn_id is minted inside SessionManager.submit_turn
        and only known after the task has started. For the REST contract we
        return a placeholder until the task runs; clients should subscribe
        to the WebSocket to discover the real id from `turn.started`.
        """
        # Generate a placeholder turn id so the REST response is non-empty.
        # The session manager mints the real id internally; the WS stream
        # carries the canonical id on turn.started.
        from ulid import ULID

        placeholder_turn_id = str(ULID())

        async def runner() -> TurnResult:
            try:
                return await self._manager.submit_turn(session_id, user_text)
            finally:
                # Always clear in-flight tracking — successful or not.
                if self._in_flight.get(session_id, None) is not None:
                    if self._in_flight[session_id].turn_id == placeholder_turn_id:
                        self._in_flight.pop(session_id, None)

        task = asyncio.create_task(runner(), name=f"turn-{session_id}-{placeholder_turn_id}")
        self._in_flight[session_id] = InFlightTurn(
            session_id=session_id,
            turn_id=placeholder_turn_id,
            task=task,
        )
        return placeholder_turn_id

    def cancel(self, session_id: str, turn_id: str) -> bool:
        """Attempt to cancel the in-flight turn. Returns True if a task was
        cancelled, False if no matching turn was active."""
        entry = self._in_flight.get(session_id)
        if entry is None or entry.turn_id != turn_id:
            return False
        entry.task.cancel()
        return True

    async def shutdown(self) -> None:
        """Cancel all in-flight turns; wait for them to finish."""
        tasks = [e.task for e in self._in_flight.values()]
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._in_flight.clear()
