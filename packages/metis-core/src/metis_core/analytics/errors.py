"""Exceptions raised by the analytics store.

The HTTP layer maps these onto error codes from `analytics-api.md §6`.
"""

from __future__ import annotations


class AnalyticsError(Exception):
    """Base class for analytics-store errors."""


class InvalidTimeWindowError(AnalyticsError):
    """Raised when `from`/`to` are malformed or `from > to`."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class InvalidGroupByError(AnalyticsError):
    """Raised when `group_by` is not in the allowed set for the endpoint."""

    def __init__(self, group_by: str, allowed: tuple[str, ...]) -> None:
        super().__init__(f"group_by={group_by!r} is not allowed; expected one of {sorted(allowed)}")
        self.group_by = group_by
        self.allowed = allowed


class InvalidOrderError(AnalyticsError):
    """Raised when `order` is not in the allowed set."""

    def __init__(self, order: str, allowed: tuple[str, ...]) -> None:
        super().__init__(f"order={order!r} is not allowed; expected one of {sorted(allowed)}")
        self.order = order
        self.allowed = allowed


class UnknownBaselineModelError(AnalyticsError):
    """Raised when the savings baseline isn't in the current PriceTable."""

    def __init__(self, model_id: str) -> None:
        super().__init__(f"baseline model {model_id!r} is not in the current price table")
        self.model_id = model_id


class TurnNotFoundError(AnalyticsError):
    """Raised when no events exist for the requested turn_id."""

    def __init__(self, turn_id: str) -> None:
        super().__init__(f"no events for turn_id={turn_id!r}")
        self.turn_id = turn_id
