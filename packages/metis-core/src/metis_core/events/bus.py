"""In-process async event bus.

See event-bus-and-trace-catalog.md §3 and §5.

emit() is synchronous from the caller's perspective: it enqueues onto a
dispatch queue and returns. A background dispatch worker drains the queue and
fans out to matching subscribers. Fast-path subscribers are awaited inline;
non-fast-path subscribers run as scheduled tasks.

Bus diagnostics (overflow, handler errors) are written to the stdlib logger,
not back through the bus — routing them through the bus they describe creates
recursive amplification (§3.5).
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal
from uuid import uuid4

import msgspec

from metis_core.events.envelope import Actor, Event
from metis_core.events.errors import (
    EventBusOverflowError,
    EventValidationError,
    FastPathHandlerError,
    UnknownEventTypeError,
)
from metis_core.events.payloads import (
    PAYLOAD_REGISTRY,
    BusSubscriberRegistered,
    BusSubscriberUnregistered,
    make_event,
)

logger = logging.getLogger(__name__)

DEFAULT_QUEUE_SIZE = 10_000

# Sentinel session id for bus lifecycle events that aren't scoped to a real
# session (e.g. global subscribers like the trace store). Spec §4.1 requires
# session_id on every Event; we mint a stable sentinel rather than threading
# scope through every call site. Consumers reading these events filter on
# the type, not the session.
_BUS_SESSION_ID = "system"

UnsubscribeReason = Literal["explicit", "client_disconnect", "shutdown", "removed_after_errors"]

Handler = Callable[[Event], Awaitable[None]]


def slow(handler: Handler) -> Handler:
    """Mark a handler as slow — must not register on the fast path.

    Spec §3.4 / §9.1 test 4: handlers tagged @slow cannot register with
    `fast_path=True`. The bus enforces this on `subscribe()` by raising
    `FastPathHandlerError`. Used by tests and as a self-documenting marker
    for known-slow handlers (anything doing disk I/O beyond the WAL+NORMAL
    SQLite append, network calls, or non-trivial CPU work).
    """
    handler.__metis_slow__ = True  # type: ignore[attr-defined]
    return handler


class ValidationMode(StrEnum):
    """Strict raises on bad payload; lenient logs and drops the event.

    Default in development is strict (tests must run strict); default in
    production is lenient. Selectable via env var METIS_EVENT_BUS_MODE.
    """

    STRICT = "strict"
    LENIENT = "lenient"

    @classmethod
    def from_env(cls, default: ValidationMode | None = None) -> ValidationMode:
        raw = os.environ.get("METIS_EVENT_BUS_MODE")
        if raw is None:
            return default if default is not None else cls.STRICT
        try:
            return cls(raw.lower())
        except ValueError:
            logger.warning("invalid METIS_EVENT_BUS_MODE=%r; falling back to strict", raw)
            return cls.STRICT


@dataclass(frozen=True)
class EventFilter:
    """Per-subscription filter. None means accept all in that dimension."""

    session_ids: frozenset[str] | None = None
    event_types: frozenset[str] | None = None
    actors: frozenset[Actor] | None = None

    def matches(self, event: Event) -> bool:
        if self.session_ids is not None and event.session_id not in self.session_ids:
            return False
        if self.event_types is not None and event.type not in self.event_types:
            return False
        if self.actors is not None and event.actor not in self.actors:
            return False
        return True

    def to_dict(self) -> dict:
        return {
            "session_ids": sorted(self.session_ids) if self.session_ids else None,
            "event_types": sorted(self.event_types) if self.event_types else None,
            "actors": sorted(a.value for a in self.actors) if self.actors else None,
        }


@dataclass
class Subscription:
    filter: EventFilter
    handler: Handler
    name: str
    fast_path: bool = False


@dataclass(frozen=True)
class SubscriptionHandle:
    id: str
    subscription: Subscription = field(compare=False)


class EventBus:
    """In-process async event bus."""

    def __init__(
        self,
        *,
        queue_size: int = DEFAULT_QUEUE_SIZE,
        mode: ValidationMode | None = None,
    ) -> None:
        self._queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=queue_size)
        self._queue_size = queue_size
        self._mode = mode if mode is not None else ValidationMode.from_env()
        self._subscriptions: dict[str, Subscription] = {}
        self._dispatch_task: asyncio.Task | None = None
        self._stopping = False
        # For drain() in tests: count pending dispatches across both queue
        # and in-flight handler tasks.
        self._pending_tasks: set[asyncio.Task] = set()

    @property
    def mode(self) -> ValidationMode:
        return self._mode

    @property
    def queue_size(self) -> int:
        return self._queue_size

    # ---- Lifecycle -----------------------------------------------------

    def start(self) -> None:
        """Start the dispatch worker. Idempotent."""
        if self._dispatch_task is None or self._dispatch_task.done():
            self._stopping = False
            self._dispatch_task = asyncio.create_task(
                self._dispatch_loop(), name="event-bus-dispatch"
            )

    async def stop(self) -> None:
        """Stop the dispatch worker after draining outstanding events."""
        self._stopping = True
        await self.drain()
        if self._dispatch_task is not None:
            self._dispatch_task.cancel()
            try:
                await self._dispatch_task
            except asyncio.CancelledError:
                pass
            self._dispatch_task = None

    async def drain(self) -> None:
        """Wait for the queue and all in-flight handler tasks to complete."""
        await self._queue.join()
        if self._pending_tasks:
            await asyncio.gather(*self._pending_tasks, return_exceptions=True)

    # ---- Subscription --------------------------------------------------

    def subscribe(self, sub: Subscription) -> SubscriptionHandle:
        """Register a subscription.

        Raises FastPathHandlerError if `sub.fast_path=True` and `sub.handler`
        is marked `@slow` (spec §9.1 test 4). Emits `bus.subscriber_registered`
        after successful registration; emission failures are logged but do
        not undo the registration.
        """
        if sub.fast_path and getattr(sub.handler, "__metis_slow__", False):
            raise FastPathHandlerError(sub.name)
        handle = SubscriptionHandle(id=str(uuid4()), subscription=sub)
        self._subscriptions[handle.id] = sub
        self._emit_lifecycle(
            type="bus.subscriber_registered",
            payload=BusSubscriberRegistered(
                subscription_name=sub.name,
                filter=sub.filter.to_dict(),
                fast_path=sub.fast_path,
            ),
        )
        return handle

    def unsubscribe(
        self, handle: SubscriptionHandle, *, reason: UnsubscribeReason = "explicit"
    ) -> None:
        """Remove a subscription. Idempotent.

        Emits `bus.subscriber_unregistered` only when a subscription was
        actually removed (so repeated calls don't produce spurious events).
        """
        sub = self._subscriptions.pop(handle.id, None)
        if sub is None:
            return
        self._emit_lifecycle(
            type="bus.subscriber_unregistered",
            payload=BusSubscriberUnregistered(
                subscription_name=sub.name,
                reason=reason,
            ),
        )

    def _emit_lifecycle(self, *, type: str, payload: msgspec.Struct) -> None:
        """Emit a bus-lifecycle event, swallowing emission failures.

        Lifecycle events are diagnostic: a malformed catalog entry or a full
        queue should not break the underlying subscribe/unsubscribe call.
        """
        try:
            event = make_event(
                type=type,
                session_id=_BUS_SESSION_ID,
                actor=Actor.SYSTEM,
                payload=payload,
                timestamp=datetime.now(UTC),
            )
            self.emit(event)
        except Exception:
            logger.warning("failed to emit bus lifecycle event %r", type, exc_info=True)

    # ---- Emit ----------------------------------------------------------

    def emit(self, event: Event) -> None:
        """Validate and enqueue an event for dispatch.

        Raises EventBusOverflowError if the queue is full. Raises
        EventValidationError in strict mode if the payload doesn't match
        the registered schema; lenient mode logs and drops.
        """
        try:
            self._validate(event)
        except EventValidationError:
            if self._mode == ValidationMode.STRICT:
                raise
            logger.warning("dropped invalid event type=%r id=%r", event.type, event.id)
            return
        except UnknownEventTypeError:
            if self._mode == ValidationMode.STRICT:
                raise
            logger.warning("dropped unknown event type=%r id=%r", event.type, event.id)
            return

        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull as exc:
            # Bus overflow: structured log, never an event (§3.5).
            logger.error(
                "event bus dispatch queue full (size=%d); rejected event type=%r id=%r",
                self._queue_size,
                event.type,
                event.id,
            )
            raise EventBusOverflowError(self._queue_size, event.type) from exc

    def _validate(self, event: Event) -> None:
        if event.type not in PAYLOAD_REGISTRY:
            raise UnknownEventTypeError(event.type)
        payload_class, _ = PAYLOAD_REGISTRY[event.type]
        # Round-trip through msgspec to validate field shapes against the
        # registered struct.
        try:
            msgspec.convert(event.payload, payload_class)
        except msgspec.ValidationError as exc:
            raise EventValidationError(event.type, [str(exc)]) from exc

    # ---- Dispatch loop -------------------------------------------------

    async def _dispatch_loop(self) -> None:
        while not self._stopping:
            try:
                event = await self._queue.get()
            except asyncio.CancelledError:
                break
            try:
                await self._fan_out(event)
            finally:
                self._queue.task_done()

    async def _fan_out(self, event: Event) -> None:
        for sub in list(self._subscriptions.values()):
            if not sub.filter.matches(event):
                continue
            if sub.fast_path:
                await self._invoke_handler_safely(sub, event)
            else:
                task = asyncio.create_task(
                    self._invoke_handler_safely(sub, event),
                    name=f"event-bus-handler:{sub.name}",
                )
                self._pending_tasks.add(task)
                task.add_done_callback(self._pending_tasks.discard)

    async def _invoke_handler_safely(self, sub: Subscription, event: Event) -> None:
        try:
            await sub.handler(event)
        except Exception:
            # Handler errors are logged, not re-emitted as events (§5.2).
            logger.warning(
                "event handler raised: subscription=%r event_type=%r event_id=%r",
                sub.name,
                event.type,
                event.id,
                exc_info=True,
            )
