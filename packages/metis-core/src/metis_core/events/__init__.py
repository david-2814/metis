"""Event bus and trace catalog.

See docs/specs/event-bus-and-trace-catalog.md for the full specification.
"""

from metis_core.events.bus import (
    EventBus,
    EventFilter,
    Subscription,
    SubscriptionHandle,
    ValidationMode,
)
from metis_core.events.envelope import Actor, Event, Sensitivity, new_event_id
from metis_core.events.errors import (
    EventBusOverflowError,
    EventValidationError,
    UnknownEventTypeError,
)
from metis_core.events.payloads import PAYLOAD_REGISTRY, make_event, payload_for_type

__all__ = [
    "PAYLOAD_REGISTRY",
    "Actor",
    "Event",
    "EventBus",
    "EventBusOverflowError",
    "EventFilter",
    "EventValidationError",
    "Sensitivity",
    "Subscription",
    "SubscriptionHandle",
    "UnknownEventTypeError",
    "ValidationMode",
    "make_event",
    "new_event_id",
    "payload_for_type",
]
