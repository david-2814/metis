"""Provider availability state machine.

See routing-engine.md §4.5. Tracks per-provider health based on recent call
outcomes. Phase 1 implements the simple binary state machine:

- Healthy → Unavailable on ≥5 consecutive failures, or AUTH error, or DNS error.
- Unavailable → Healthy on first successful call OR after 5 minutes of no calls.

Per-model granularity is acknowledged in the spec but deferred; v1 tracks
per-provider only.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import StrEnum

from metis.adapters.errors import ErrorClass

_CONSECUTIVE_FAILURE_THRESHOLD = 5
_RECOVERY_TIMEOUT_SECONDS = 5 * 60.0

# Error classes that immediately mark the provider unavailable.
_IMMEDIATE_UNAVAILABLE_CLASSES: frozenset[ErrorClass] = frozenset({ErrorClass.AUTH})


class AvailabilityState(StrEnum):
    HEALTHY = "healthy"
    UNAVAILABLE = "unavailable"


@dataclass
class _ProviderState:
    state: AvailabilityState = AvailabilityState.HEALTHY
    consecutive_failures: int = 0
    last_failure_at: float = 0.0
    last_call_at: float = 0.0


class ProviderAvailability:
    """Tracks per-provider availability state."""

    def __init__(self, *, time_fn=time.monotonic) -> None:
        self._states: dict[str, _ProviderState] = {}
        self._time = time_fn

    def state(self, provider: str) -> AvailabilityState:
        s = self._states.get(provider)
        if s is None:
            return AvailabilityState.HEALTHY
        # Auto-recover after recovery window of no calls (§4.5).
        if (
            s.state == AvailabilityState.UNAVAILABLE
            and self._time() - s.last_call_at >= _RECOVERY_TIMEOUT_SECONDS
        ):
            s.state = AvailabilityState.HEALTHY
            s.consecutive_failures = 0
        return s.state

    def is_available(self, provider: str) -> bool:
        return self.state(provider) == AvailabilityState.HEALTHY

    def mark_success(self, provider: str) -> None:
        s = self._states.setdefault(provider, _ProviderState())
        s.consecutive_failures = 0
        s.last_call_at = self._time()
        s.state = AvailabilityState.HEALTHY

    def mark_failure(self, provider: str, error_class: ErrorClass) -> None:
        s = self._states.setdefault(provider, _ProviderState())
        now = self._time()
        s.last_call_at = now
        s.last_failure_at = now
        s.consecutive_failures += 1
        if (
            error_class in _IMMEDIATE_UNAVAILABLE_CLASSES
            or s.consecutive_failures >= _CONSECUTIVE_FAILURE_THRESHOLD
        ):
            s.state = AvailabilityState.UNAVAILABLE

    def force_recovery(self, provider: str) -> None:
        """Reset state to HEALTHY (used after explicit /routing/reload etc.)."""
        s = self._states.setdefault(provider, _ProviderState())
        s.state = AvailabilityState.HEALTHY
        s.consecutive_failures = 0
