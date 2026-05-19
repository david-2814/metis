"""Remote tool-confirmation handler.

When the server is running, tool dispatches that require confirmation
(per tool-dispatcher.md §5) emit `tool.confirmation_requested` on the bus
(which reaches connected clients over the WS) and then await a REST
response at `POST /sessions/{sid}/turns/{tid}/confirmations/{rid}`.

This module implements the handler that bridges the two: dispatcher →
handler.request() → asyncio.Event → REST endpoint → handler.resolve() →
the event fires → dispatcher proceeds.

The handler also enforces a single in-flight confirmation per request_id —
race resolution is "first-write-wins" so two clients answering produces
exactly one decision (the second gets `applied: false`).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Literal

from metis.core.tools.confirmation import ConfirmationDecision, ConfirmationRequest

logger = logging.getLogger(__name__)


@dataclass
class _PendingConfirmation:
    request_id: str
    tool_use_id: str
    decision: ConfirmationDecision | None = None
    scope: Literal["once", "session"] | None = None
    event: asyncio.Event = field(default_factory=asyncio.Event)


def _request_id_for(tool_use_id: str) -> str:
    """Derive the request_id from a tool_use_id (matches dispatcher §5.3)."""
    return f"conf_{tool_use_id}"


class RemoteConfirmationHandler:
    """ConfirmationHandler that waits for a REST response.

    Implements the `ConfirmationHandler` protocol from
    tools/confirmation.py. The dispatcher calls `request()` and awaits;
    the server's REST endpoint calls `resolve()` to unblock it.
    """

    def __init__(self) -> None:
        self._pending: dict[str, _PendingConfirmation] = {}
        self._lock = asyncio.Lock()

    async def request(self, req: ConfirmationRequest) -> ConfirmationDecision:
        """Block until a client resolves this confirmation (or it's
        cancelled). The dispatcher applies the timeout wrapper around this
        call, so we never time out here — the dispatcher's wait_for handles
        TIMEOUT semantics. We just wait."""
        request_id = _request_id_for(req.tool_use_id)
        pending = _PendingConfirmation(request_id=request_id, tool_use_id=req.tool_use_id)
        async with self._lock:
            self._pending[request_id] = pending
        try:
            await pending.event.wait()
            return pending.decision or ConfirmationDecision.TIMEOUT
        finally:
            async with self._lock:
                # Always clean up — even on cancellation — so a re-issued
                # confirmation for the same tool_use_id starts fresh.
                self._pending.pop(request_id, None)

    def resolve(
        self,
        request_id: str,
        *,
        decision: ConfirmationDecision,
        scope: Literal["once", "session"] | None = None,
    ) -> bool:
        """Set the decision for `request_id`. Returns True iff the request
        was pending and unanswered (first-write-wins); False if it was
        already resolved, expired, or never registered."""
        pending = self._pending.get(request_id)
        if pending is None:
            return False
        if pending.decision is not None:
            # Already resolved by another client.
            return False
        pending.decision = decision
        pending.scope = scope
        pending.event.set()
        return True

    def is_pending(self, request_id: str) -> bool:
        pending = self._pending.get(request_id)
        return pending is not None and pending.decision is None

    def pending_request_ids(self) -> list[str]:
        return [rid for rid, p in self._pending.items() if p.decision is None]
