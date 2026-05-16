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


# ---- NETWORK class escalation (refined 2026-05-16) --------------------
#
# A single transient SSL / TCP error must NOT blackout the whole provider:
# routing-engine.md §4.5.1 now requires ≥2 NETWORK failures within 30s for
# provider-wide escalation. AUTH still escalates on the first error.


def test_single_network_error_does_not_blackout_provider():
    """One transient SSL / connection hiccup must not mark the whole
    provider Unavailable. Sibling models stay reachable."""
    a = ProviderAvailability()
    a.mark_failure("anthropic", OPUS, ErrorClass.NETWORK)
    assert a.is_available("anthropic")
    assert a.is_available("anthropic", SONNET)


def test_two_network_errors_within_30s_blackout_provider():
    """Two NETWORK errors inside the 30-second window → the whole
    provider flips to Unavailable. This is what a real provider-side
    outage looks like."""
    clock = _Clock()
    a = ProviderAvailability(time_fn=clock)
    a.mark_failure("anthropic", OPUS, ErrorClass.NETWORK)
    clock.advance(15.0)
    a.mark_failure("anthropic", SONNET, ErrorClass.NETWORK)
    assert not a.is_available("anthropic")
    # Other provider unaffected.
    assert a.is_available("openai")


def test_two_network_errors_31s_apart_do_not_blackout_provider():
    """If the second NETWORK error falls outside the 30-second window
    the first error has aged out — the second arrives alone and is
    treated as a fresh one-off, not an outage."""
    clock = _Clock()
    a = ProviderAvailability(time_fn=clock)
    a.mark_failure("anthropic", OPUS, ErrorClass.NETWORK)
    clock.advance(31.0)
    a.mark_failure("anthropic", OPUS, ErrorClass.NETWORK)
    assert a.is_available("anthropic")


def test_two_network_errors_with_model_none_still_escalate():
    """A NETWORK error without per-model context (e.g. DNS failure
    before request build) is still a real signal. Two of them inside
    30s flip the provider."""
    clock = _Clock()
    a = ProviderAvailability(time_fn=clock)
    a.mark_failure("anthropic", None, ErrorClass.NETWORK)
    assert a.is_available("anthropic")
    clock.advance(5.0)
    a.mark_failure("anthropic", None, ErrorClass.NETWORK)
    assert not a.is_available("anthropic")


def test_single_network_error_advances_per_model_counter():
    """A single NETWORK error doesn't escalate provider-wide, but it
    still contributes to the per-(provider, model) 5-within-2-min
    breaker so a model that keeps producing NETWORK errors eventually
    trips itself."""
    clock = _Clock()
    a = ProviderAvailability(time_fn=clock)
    # Five NETWORK errors against the same model, spaced 35s apart so
    # the 30-second provider-escalation window expires between each
    # but the 2-minute per-model window does not. The per-model streak
    # only requires the gap between consecutive failures to stay below
    # 120s; 35s satisfies that.
    for _ in range(5):
        a.mark_failure("anthropic", OPUS, ErrorClass.NETWORK)
        clock.advance(35.0)
    # Per-model breaker should have tripped; provider-wide should not
    # (no two failures landed within 30s).
    assert not a.is_available("anthropic", OPUS)
    assert a.is_available("anthropic", SONNET)


def test_success_clears_network_failure_window():
    """A successful call resets the NETWORK sliding window, so a later
    isolated NETWORK error doesn't pair with one from before the
    success."""
    clock = _Clock()
    a = ProviderAvailability(time_fn=clock)
    a.mark_failure("anthropic", OPUS, ErrorClass.NETWORK)
    clock.advance(5.0)
    a.mark_success("anthropic", OPUS)
    clock.advance(5.0)
    a.mark_failure("anthropic", SONNET, ErrorClass.NETWORK)
    # The first failure was cleared by the success; this is a fresh
    # one-off, not the second of a pair.
    assert a.is_available("anthropic")


def test_auth_error_still_escalates_immediately():
    """Regression net: AUTH still trips provider-wide on the first
    error. A misconfigured key cannot be a one-off."""
    a = ProviderAvailability()
    a.mark_failure("anthropic", OPUS, ErrorClass.AUTH)
    assert not a.is_available("anthropic")
    assert not a.is_available("anthropic", SONNET)


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
