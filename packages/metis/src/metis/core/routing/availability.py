"""Provider availability state machine.

See routing-engine.md §4.5. Availability is tracked at two granularities:

1. **Per-(provider, model)** — the default. Most outages affect a single
   model (a hot model rate-limited, a deprecated checkpoint returning errors).
2. **Per-provider** — escalated when failures suggest a provider-wide problem.

Each scope is binary (Healthy / Unavailable) in v1; Degraded is reserved for
Phase 2.

Triggers (§4.5.1):

- ≥5 consecutive failures on one ``(provider, model)`` within 2 minutes →
  that ``(provider, model)`` Unavailable.
- ≥3 distinct models from one provider hit Unavailable within 2 minutes →
  the whole provider Unavailable.
- Any AUTH error on any model from a provider → the whole provider
  Unavailable immediately. A misconfigured key affects every model.
- ≥2 NETWORK errors on a provider within 30 seconds → the whole provider
  Unavailable. A single transient SSL / connection hiccup only contributes
  to the per-(provider, model) counter; the 2-within-30s requirement
  distinguishes a real provider-side outage from a one-off TLS renegotiation
  glitch.

Auto-clear after 5 minutes of no attempts (§4.5.2). A successful call against
a ``(provider, model)`` clears that scope's Unavailable state immediately;
a successful call against any model from a provider clears the provider-wide
Unavailable state and the NETWORK-failure window.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum

from metis.core.adapters.errors import ErrorClass

_CONSECUTIVE_FAILURE_THRESHOLD = 5
_FAILURE_WINDOW_SECONDS = 2 * 60.0
_MULTI_MODEL_ESCALATION_THRESHOLD = 3
_RECOVERY_TIMEOUT_SECONDS = 5 * 60.0

# NETWORK-class escalation: ≥N failures within W seconds → provider-wide.
# One isolated SSL handshake / connection error must not blackout the
# whole provider for 5 minutes; require a second NETWORK failure inside
# a short window to confirm a sustained provider-side problem.
_NETWORK_PROVIDER_ESCALATION_THRESHOLD = 2
_NETWORK_PROVIDER_ESCALATION_WINDOW_SECONDS = 30.0


class AvailabilityState(StrEnum):
    HEALTHY = "healthy"
    UNAVAILABLE = "unavailable"


@dataclass
class _ModelState:
    state: AvailabilityState = AvailabilityState.HEALTHY
    consecutive_failures: int = 0
    last_failure_at: float = 0.0
    last_call_at: float = 0.0
    # Time of the transition into UNAVAILABLE (used for multi-model escalation).
    unavailable_since: float = 0.0


@dataclass
class _ProviderState:
    state: AvailabilityState = AvailabilityState.HEALTHY
    last_call_at: float = 0.0
    # When each model from this provider entered UNAVAILABLE; pruned to the
    # last 2 minutes when consulted.
    recent_model_unavailables: dict[str, float] = field(default_factory=dict)
    # Timestamps of recent NETWORK-class failures; pruned to the
    # _NETWORK_PROVIDER_ESCALATION_WINDOW_SECONDS window when consulted.
    recent_network_failures: list[float] = field(default_factory=list)


class ProviderAvailability:
    """Tracks per-(provider, model) and per-provider availability state."""

    def __init__(self, *, time_fn=time.monotonic) -> None:
        self._models: dict[tuple[str, str], _ModelState] = {}
        self._providers: dict[str, _ProviderState] = {}
        self._time = time_fn

    # ---- Inspection ----------------------------------------------------

    def state(self, provider: str, model: str | None = None) -> AvailabilityState:
        """Return the effective availability state.

        With ``model=None`` returns the provider-wide state. With ``model``
        set returns Unavailable if either the provider-wide state or the
        ``(provider, model)`` state is Unavailable.
        """
        now = self._time()
        prov = self._providers.get(provider)
        if prov is not None and prov.state == AvailabilityState.UNAVAILABLE:
            # Auto-recover after recovery window of no calls (§4.5.2).
            if now - prov.last_call_at >= _RECOVERY_TIMEOUT_SECONDS:
                prov.state = AvailabilityState.HEALTHY
                prov.recent_model_unavailables.clear()
                prov.recent_network_failures.clear()
            else:
                return AvailabilityState.UNAVAILABLE
        if model is None:
            return AvailabilityState.HEALTHY
        m = self._models.get((provider, model))
        if m is None:
            return AvailabilityState.HEALTHY
        if (
            m.state == AvailabilityState.UNAVAILABLE
            and now - m.last_call_at >= _RECOVERY_TIMEOUT_SECONDS
        ):
            m.state = AvailabilityState.HEALTHY
            m.consecutive_failures = 0
        return m.state

    def is_available(self, provider: str, model: str | None = None) -> bool:
        return self.state(provider, model) == AvailabilityState.HEALTHY

    # ---- Mutation ------------------------------------------------------

    def mark_success(self, provider: str, model: str | None = None) -> None:
        """A successful call clears both the (provider, model) and provider scopes."""
        now = self._time()
        prov = self._providers.setdefault(provider, _ProviderState())
        prov.state = AvailabilityState.HEALTHY
        prov.last_call_at = now
        prov.recent_model_unavailables.clear()
        prov.recent_network_failures.clear()
        if model is not None:
            m = self._models.setdefault((provider, model), _ModelState())
            m.state = AvailabilityState.HEALTHY
            m.consecutive_failures = 0
            m.last_call_at = now
            m.unavailable_since = 0.0

    def mark_failure(
        self,
        provider: str,
        model: str | None,
        error_class: ErrorClass,
    ) -> None:
        """Record a failure against ``(provider, model)``.

        Routing rules per §4.5.1:

        - AUTH → whole provider Unavailable immediately. A misconfigured key
          affects every model.
        - NETWORK → register the failure on the provider's sliding window;
          escalate to provider-wide Unavailable only on the
          ``_NETWORK_PROVIDER_ESCALATION_THRESHOLD``th failure within
          ``_NETWORK_PROVIDER_ESCALATION_WINDOW_SECONDS``. A single transient
          SSL / connection hiccup still contributes to the per-(provider,
          model) counter below — it just can't blackout the whole provider
          on its own.
        - Otherwise the (provider, model) counter increments; ≥5 within 2
          minutes flips that scope to Unavailable. The counter resets to 1
          if the previous failure is older than the 2-minute window.
        - When a third distinct (provider, model) from one provider goes
          Unavailable within 2 minutes, escalate to provider-wide.
        """
        now = self._time()
        prov = self._providers.setdefault(provider, _ProviderState())
        prov.last_call_at = now

        if error_class == ErrorClass.AUTH:
            prov.state = AvailabilityState.UNAVAILABLE
            return

        if error_class == ErrorClass.NETWORK:
            cutoff = now - _NETWORK_PROVIDER_ESCALATION_WINDOW_SECONDS
            prov.recent_network_failures = [t for t in prov.recent_network_failures if t >= cutoff]
            prov.recent_network_failures.append(now)
            if len(prov.recent_network_failures) >= _NETWORK_PROVIDER_ESCALATION_THRESHOLD:
                prov.state = AvailabilityState.UNAVAILABLE
                return
            # Single NETWORK error: fall through to the per-(provider, model)
            # counter so a model that keeps producing NETWORK errors still
            # trips itself via the standard 5-within-2-min threshold.

        if model is None:
            # No per-model context; treat as a provider-level signal but
            # don't escalate. Update last_call_at so auto-recovery is sane.
            return

        m = self._models.setdefault((provider, model), _ModelState())
        # Sliding-window reset: if the previous failure is outside the 2-min
        # window (or there isn't one), start a fresh streak.
        if m.consecutive_failures == 0 or now - m.last_failure_at > _FAILURE_WINDOW_SECONDS:
            m.consecutive_failures = 1
        else:
            m.consecutive_failures += 1
        m.last_failure_at = now
        m.last_call_at = now

        if (
            m.state != AvailabilityState.UNAVAILABLE
            and m.consecutive_failures >= _CONSECUTIVE_FAILURE_THRESHOLD
        ):
            m.state = AvailabilityState.UNAVAILABLE
            m.unavailable_since = now
            self._record_model_unavailable(prov, model, now)

    def force_recovery(self, provider: str, model: str | None = None) -> None:
        """Reset state to HEALTHY (used after explicit /routing/reload etc.)."""
        prov = self._providers.setdefault(provider, _ProviderState())
        prov.state = AvailabilityState.HEALTHY
        prov.recent_model_unavailables.clear()
        prov.recent_network_failures.clear()
        if model is not None:
            m = self._models.setdefault((provider, model), _ModelState())
            m.state = AvailabilityState.HEALTHY
            m.consecutive_failures = 0
            m.unavailable_since = 0.0
        else:
            # Clear all models for this provider too.
            for (p, _), st in self._models.items():
                if p == provider:
                    st.state = AvailabilityState.HEALTHY
                    st.consecutive_failures = 0
                    st.unavailable_since = 0.0

    # ---- Internal ------------------------------------------------------

    def _record_model_unavailable(self, prov: _ProviderState, model: str, now: float) -> None:
        """Track this model's transition to Unavailable; escalate provider-wide
        when ≥3 distinct models from this provider hit Unavailable within 2
        minutes."""
        prov.recent_model_unavailables[model] = now
        # Prune entries outside the 2-min window.
        cutoff = now - _FAILURE_WINDOW_SECONDS
        stale = [k for k, t in prov.recent_model_unavailables.items() if t < cutoff]
        for k in stale:
            del prov.recent_model_unavailables[k]
        if len(prov.recent_model_unavailables) >= _MULTI_MODEL_ESCALATION_THRESHOLD:
            prov.state = AvailabilityState.UNAVAILABLE
