"""Canonical id generation.

ULIDs are monotonic per process and sortable. Tool-use ids carry a `tu_`
prefix per canonical-message-format.md §6.2; session ids use `sess_`.

The python-ulid stdlib `ULID()` is timestamp-prefixed but not strictly
monotonic within a millisecond (the random suffix can sort in any order).
We wrap it with a process-wide lock that bumps the integer value by 1 when
two ids land in the same millisecond, preserving the spec's "monotonic per
process" guarantee (event-bus-and-trace-catalog.md §4.2).
"""

from __future__ import annotations

import threading

from ulid import ULID

_lock = threading.Lock()
_last: ULID | None = None


def next_monotonic_ulid() -> ULID:
    """Process-wide monotonic ULID. Two calls in the same millisecond are
    guaranteed to return increasing ids by bumping the integer value."""
    global _last
    with _lock:
        candidate = ULID()
        if _last is not None and int(candidate) <= int(_last):
            candidate = ULID.from_int(int(_last) + 1)
        _last = candidate
        return candidate


def new_session_id() -> str:
    return f"sess_{next_monotonic_ulid()}"


def new_message_id() -> str:
    return str(next_monotonic_ulid())


def new_tool_use_id() -> str:
    return f"tu_{next_monotonic_ulid()}"
