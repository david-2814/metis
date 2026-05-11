"""Tests for ProviderAvailability state machine."""

from __future__ import annotations

from metis.adapters.errors import ErrorClass
from metis.routing.availability import AvailabilityState, ProviderAvailability


class _Clock:
    """Manual clock for deterministic time-based tests."""

    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def test_starts_healthy():
    a = ProviderAvailability()
    assert a.is_available("anthropic")
    assert a.state("anthropic") == AvailabilityState.HEALTHY


def test_consecutive_failures_below_threshold_stay_healthy():
    a = ProviderAvailability()
    for _ in range(4):
        a.mark_failure("anthropic", ErrorClass.SERVER_ERROR)
    assert a.is_available("anthropic")


def test_five_consecutive_failures_mark_unavailable():
    a = ProviderAvailability()
    for _ in range(5):
        a.mark_failure("anthropic", ErrorClass.SERVER_ERROR)
    assert not a.is_available("anthropic")
    assert a.state("anthropic") == AvailabilityState.UNAVAILABLE


def test_success_resets_consecutive_count():
    a = ProviderAvailability()
    for _ in range(4):
        a.mark_failure("anthropic", ErrorClass.SERVER_ERROR)
    a.mark_success("anthropic")
    # Now 4 more failures should not yet trip the threshold.
    for _ in range(4):
        a.mark_failure("anthropic", ErrorClass.SERVER_ERROR)
    assert a.is_available("anthropic")


def test_auth_error_marks_immediate_unavailable():
    a = ProviderAvailability()
    a.mark_failure("anthropic", ErrorClass.AUTH)
    assert not a.is_available("anthropic")


def test_auto_recovery_after_timeout():
    clock = _Clock()
    a = ProviderAvailability(time_fn=clock)
    a.mark_failure("anthropic", ErrorClass.AUTH)
    assert not a.is_available("anthropic")
    # Just before timeout: still unavailable.
    clock.advance(4 * 60.0)
    assert not a.is_available("anthropic")
    # After 5-minute window: recovers.
    clock.advance(60.0 + 1.0)
    assert a.is_available("anthropic")


def test_success_recovers_from_unavailable():
    a = ProviderAvailability()
    for _ in range(5):
        a.mark_failure("anthropic", ErrorClass.SERVER_ERROR)
    assert not a.is_available("anthropic")
    a.mark_success("anthropic")
    assert a.is_available("anthropic")


def test_force_recovery():
    a = ProviderAvailability()
    a.mark_failure("anthropic", ErrorClass.AUTH)
    assert not a.is_available("anthropic")
    a.force_recovery("anthropic")
    assert a.is_available("anthropic")


def test_independent_providers():
    a = ProviderAvailability()
    a.mark_failure("anthropic", ErrorClass.AUTH)
    assert not a.is_available("anthropic")
    assert a.is_available("openai")
