"""Event bus exception types.

See event-bus-and-trace-catalog.md §3.2, §3.5.
"""

from __future__ import annotations


class EventValidationError(ValueError):
    """Raised in strict mode when a payload doesn't match the type's schema."""

    def __init__(self, event_type: str, errors: list[str]) -> None:
        super().__init__(f"{event_type}: {'; '.join(errors)}")
        self.event_type = event_type
        self.errors = errors


class UnknownEventTypeError(ValueError):
    """Raised when an event type isn't in the catalog registry."""

    def __init__(self, event_type: str) -> None:
        super().__init__(f"unknown event type: {event_type!r}")
        self.event_type = event_type


class EventBusOverflowError(RuntimeError):
    """Raised when the dispatch queue is full and emit cannot enqueue."""

    def __init__(self, queue_size: int, rejected_type: str) -> None:
        super().__init__(
            f"event bus dispatch queue full ({queue_size} pending); rejected {rejected_type!r}"
        )
        self.queue_size = queue_size
        self.rejected_type = rejected_type


class FastPathHandlerError(RuntimeError):
    """Raised when a slow handler tries to register on the fast path."""

    def __init__(self, subscription_name: str) -> None:
        super().__init__(
            f"subscription {subscription_name!r} is annotated @slow but registered with "
            "fast_path=True; slow handlers stall event dispatch"
        )
