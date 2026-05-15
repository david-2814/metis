"""Event envelope, Actor, Sensitivity.

See event-bus-and-trace-catalog.md §4.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

import msgspec

from metis_core.canonical.ids import next_monotonic_ulid


class Actor(StrEnum):
    USER = "user"
    AGENT = "agent"
    SYSTEM = "system"
    TOOL = "tool"
    WORKER = "worker"


class Sensitivity(StrEnum):
    PRIVATE = "private"
    USER_CONTROLLED = "user_controlled"
    PSEUDONYMOUS = "pseudonymous"
    AGGREGATABLE = "aggregatable"


_SENSITIVITY_RANK: dict[Sensitivity, int] = {
    Sensitivity.PRIVATE: 0,
    Sensitivity.USER_CONTROLLED: 1,
    Sensitivity.PSEUDONYMOUS: 2,
    Sensitivity.AGGREGATABLE: 3,
}


def sensitivity_rank(value: Sensitivity) -> int:
    """Order from most-private (0) to least-private (3). See §4.4.1."""
    return _SENSITIVITY_RANK[value]


def new_event_id() -> str:
    """ULID, monotonic per process (see §4.2)."""
    return str(next_monotonic_ulid())


class Event(msgspec.Struct, frozen=True):
    """Bus event envelope.

    The catalog (§6) defines per-type payload schemas. `payload` here is a
    dict because storage and validation operate on dicts; typed payload
    structs live in metis_core.events.payloads and convert via `make_event`.
    """

    id: str
    timestamp: datetime
    session_id: str
    type: str
    actor: Actor
    payload: dict
    sensitivity: Sensitivity
    turn_id: str | None = None
    parent_event_id: str | None = None
