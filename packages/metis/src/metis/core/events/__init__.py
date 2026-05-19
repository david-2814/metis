"""Event bus and trace catalog.

See docs/specs/event-bus-and-trace-catalog.md for the full specification.
"""

from metis.core.events.bus import (
    EventBus,
    EventFilter,
    Subscription,
    SubscriptionHandle,
    ValidationMode,
    slow,
)
from metis.core.events.envelope import Actor, Event, Sensitivity, new_event_id
from metis.core.events.errors import (
    EventBusOverflowError,
    EventValidationError,
    FastPathHandlerError,
    UnknownEventTypeError,
)
from metis.core.events.payloads import PAYLOAD_REGISTRY, make_event, payload_for_type

__all__ = [
    "PAYLOAD_REGISTRY",
    "Actor",
    "Event",
    "EventBus",
    "EventBusOverflowError",
    "EventFilter",
    "EventValidationError",
    "FastPathHandlerError",
    "Sensitivity",
    "Subscription",
    "SubscriptionHandle",
    "UnknownEventTypeError",
    "ValidationMode",
    "make_event",
    "new_event_id",
    "payload_for_type",
    "slow",
]
