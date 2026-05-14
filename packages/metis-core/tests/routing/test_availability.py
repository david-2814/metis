"""Tests for ProviderAvailability state machine.

Covers routing-engine.md §4.5 / §4.5.1 — per-(provider, model) tracking with
provider-wide promotion on AUTH, NETWORK, and the multi-model escalation rule.
"""

from __future__ import annotations

from metis_core.adapters.errors import ErrorClass
from metis_core.routing.availability import AvailabilityState, ProviderAvailability


class _Clock:
    """Manual clock for deterministic time-based tests."""

    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


OPUS = "anthropic:claude-opus-4-7"
SONNET = "anthropic:claude-sonnet-4-6"
HAIKU = "anthropic:claude-haiku-4-5"


# ---- Baseline behavior -------------------------------------------------


def test_starts_healthy():
    a = ProviderAvailability()
    assert a.is_available("anthropic")
    assert a.is_available("anthropic", OPUS)
    assert a.state("anthropic", OPUS) == AvailabilityState.HEALTHY


def test_consecutive_failures_below_threshold_stay_healthy():
    a = ProviderAvailability()
    for _ in range(4):
        a.mark_failure("anthropic", OPUS, ErrorClass.SERVER_ERROR)
    assert a.is_available("anthropic", OPUS)


def test_five_consecutive_failures_mark_model_unavailable():
    a = ProviderAvailability()
    for _ in range(5):
        a.mark_failure("anthropic", OPUS, ErrorClass.SERVER_ERROR)
    assert not a.is_available("anthropic", OPUS)
    assert a.state("anthropic", OPUS) == AvailabilityState.UNAVAILABLE


def test_success_resets_consecutive_count():
    a = ProviderAvailability()
    for _ in range(4):
        a.mark_failure("anthropic", OPUS, ErrorClass.SERVER_ERROR)
    a.mark_success("anthropic", OPUS)
    # Now 4 more failures should not yet trip the threshold.
    for _ in range(4):
        a.mark_failure("anthropic", OPUS, ErrorClass.SERVER_ERROR)
    assert a.is_available("anthropic", OPUS)


# ---- Bug 1: per-(provider, model) granularity --------------------------


def test_auth_marks_whole_provider_unavailable_even_when_model_passed():
    """An AUTH error against any one model blacks out the whole provider."""
    a = ProviderAvailability()
    a.mark_failure("anthropic", OPUS, ErrorClass.AUTH)
    assert not a.is_available("anthropic")
    assert not a.is_available("anthropic", OPUS)
    # Sonnet is now also unreachable because the provider scope dominates.
    assert not a.is_available("anthropic", SONNET)


def test_server_error_on_opus_does_not_blackout_sonnet():
    """Spec test §10.1.19: per-(provider, model) Unavailable on Opus
    must NOT mark Sonnet Unavailable."""
    a = ProviderAvailability()
    for _ in range(5):
        a.mark_failure("anthropic", OPUS, ErrorClass.SERVER_ERROR)
    assert not a.is_available("anthropic", OPUS)
    # Provider-wide remains Healthy (only one model is down).
    assert a.is_available("anthropic")
    # Sonnet was never failed; still Healthy.
    assert a.is_available("anthropic", SONNET)


def test_success_on_one_model_does_not_clear_another_models_failures():
    a = ProviderAvailability()
    for _ in range(4):
        a.mark_failure("anthropic", OPUS, ErrorClass.SERVER_ERROR)
    a.mark_success("anthropic", SONNET)
    # One more failure on Opus should still trip its threshold.
    a.mark_failure("anthropic", OPUS, ErrorClass.SERVER_ERROR)
    assert not a.is_available("anthropic", OPUS)


# ---- Bug 2: sliding-window reset on consecutive-failure counter --------


def test_five_failures_spaced_outside_window_do_not_trip():
    """Failures more than 2 minutes apart should not accumulate into a
    five-strike breaker — the counter resets each time."""
    clock = _Clock()
    a = ProviderAvailability(time_fn=clock)
    for _ in range(10):
        a.mark_failure("anthropic", OPUS, ErrorClass.SERVER_ERROR)
        clock.advance(121.0)  # > 2 min, just past the window
    assert a.is_available("anthropic", OPUS)


def test_five_failures_within_window_do_trip():
    clock = _Clock()
    a = ProviderAvailability(time_fn=clock)
    for _ in range(5):
        a.mark_failure("anthropic", OPUS, ErrorClass.SERVER_ERROR)
        clock.advance(20.0)  # 5 failures across 100s, all in-window
    assert not a.is_available("anthropic", OPUS)


def test_failure_after_long_gap_starts_fresh_streak():
    clock = _Clock()
    a = ProviderAvailability(time_fn=clock)
    # 4 failures back-to-back — would be one short of tripping.
    for _ in range(4):
        a.mark_failure("anthropic", OPUS, ErrorClass.SERVER_ERROR)
        clock.advance(10.0)
    # Wait past the window. The next failure should reset the streak to 1.
    clock.advance(200.0)
    a.mark_failure("anthropic", OPUS, ErrorClass.SERVER_ERROR)
    assert a.is_available("anthropic", OPUS)


# ---- Bug 3: NETWORK error triggers immediate Unavailable --------------


def test_network_error_marks_provider_unavailable_immediately():
    a = ProviderAvailability()
    a.mark_failure("anthropic", OPUS, ErrorClass.NETWORK)
    # Whole provider should be Unavailable on the first network error.
    assert not a.is_available("anthropic")
    assert not a.is_available("anthropic", SONNET)


def test_network_error_without_model_still_marks_provider():
    """Some callers may not have a model attributed (e.g. DNS failure
    before request build); provider-wide should still flip."""
    a = ProviderAvailability()
    a.mark_failure("anthropic", None, ErrorClass.NETWORK)
    assert not a.is_available("anthropic")


# ---- Multi-model escalation (§4.5.1) ----------------------------------


def test_three_distinct_models_unavailable_escalates_to_provider():
    """≥3 distinct (provider, model) Unavailable within 2 minutes →
    the whole provider Unavailable."""
    clock = _Clock()
    a = ProviderAvailability(time_fn=clock)
    for model in (OPUS, SONNET, HAIKU):
        for _ in range(5):
            a.mark_failure("anthropic", model, ErrorClass.SERVER_ERROR)
        clock.advance(10.0)
    # The third model's transition should flip the provider scope.
    assert not a.is_available("anthropic")


def test_three_model_unavailables_spread_over_more_than_two_minutes_do_not_escalate():
    clock = _Clock()
    a = ProviderAvailability(time_fn=clock)
    # Opus down at t=0.
    for _ in range(5):
        a.mark_failure("anthropic", OPUS, ErrorClass.SERVER_ERROR)
    # Sonnet down a little later — still in window.
    clock.advance(30.0)
    for _ in range(5):
        a.mark_failure("anthropic", SONNET, ErrorClass.SERVER_ERROR)
    # Haiku trips well outside the 2-min window since Opus.
    clock.advance(200.0)
    for _ in range(5):
        a.mark_failure("anthropic", HAIKU, ErrorClass.SERVER_ERROR)
    # Only two model-transitions are inside any 2-minute window with the
    # third → no provider-wide escalation.
    assert a.is_available("anthropic")


# ---- Auto-recovery -----------------------------------------------------


def test_auto_recovery_after_timeout():
    clock = _Clock()
    a = ProviderAvailability(time_fn=clock)
    a.mark_failure("anthropic", OPUS, ErrorClass.AUTH)
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
        a.mark_failure("anthropic", OPUS, ErrorClass.SERVER_ERROR)
    assert not a.is_available("anthropic", OPUS)
    a.mark_success("anthropic", OPUS)
    assert a.is_available("anthropic", OPUS)


def test_success_on_any_model_clears_provider_wide():
    """Per §4.5.1: a successful call against any model from a provider
    clears the provider-wide Unavailable state."""
    a = ProviderAvailability()
    a.mark_failure("anthropic", OPUS, ErrorClass.AUTH)
    assert not a.is_available("anthropic")
    a.mark_success("anthropic", SONNET)
    assert a.is_available("anthropic")


def test_force_recovery():
    a = ProviderAvailability()
    a.mark_failure("anthropic", OPUS, ErrorClass.AUTH)
    assert not a.is_available("anthropic")
    a.force_recovery("anthropic")
    assert a.is_available("anthropic")
    assert a.is_available("anthropic", OPUS)


def test_independent_providers():
    a = ProviderAvailability()
    a.mark_failure("anthropic", OPUS, ErrorClass.AUTH)
    assert not a.is_available("anthropic")
    assert a.is_available("openai")
    assert a.is_available("openai", "openai:gpt-5")
