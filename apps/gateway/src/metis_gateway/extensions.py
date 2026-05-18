"""Gateway-specific extension Protocols.

Lives alongside the existing extension surface in ``metis_core.extensions``.
The metis-core file is the right home for Protocols whose signatures use
only stdlib types or rely on TYPE_CHECKING for Starlette (the common case
for boot-time `register_routes` hooks). Protocols whose signatures
reference gateway-private types (``GatewayKey``, ``TierCaps``) belong here
instead — keeping them out of metis-core preserves the layering invariant
that metis-core never references metis-gateway types.

§4.2c (2026-05-18) — added ``TierCapsResolver`` so the Pro overlay can
re-inject tier-axis quota composition without OSS having to know about
billing accounts / tiers. The OSS gateway calls
``state.tier_caps_resolver(key)`` once per inbound request just before
``enforce_quotas``; the noop returns ``None`` (OSS-only deployments have
no concept of "tier" so there's nothing to compose against).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from metis_gateway.auth import GatewayKey
from metis_gateway.quotas import TierCaps


@runtime_checkable
class TierCapsResolver(Protocol):
    """Pro overlay hook for tier-axis quota composition.

    Implementations walk ``key`` → ``account_id`` → billing customer record →
    tier; return a ``TierCaps`` instance when the account's tier composes a
    cap on top of the existing per-(user / team / key / workspace) quotas,
    or ``None`` otherwise. Called once per inbound request, just before
    ``enforce_quotas`` (see ``apps/gateway/.../app.py`` chat_completions /
    messages handlers).

    The OSS default ``NoopTierCapsResolver`` always returns ``None`` so
    the pre-§4.2b behavior (no tier-axis composition) is preserved when
    no Pro overlay is installed. ``metis_pro.quotas.ProTierCapsResolver``
    is the canonical implementation.
    """

    def __call__(self, key: GatewayKey) -> TierCaps | None: ...


class NoopTierCapsResolver:
    """OSS default — no tier-axis composition.

    Returns ``None`` for every key, matching the pre-§4.2b OSS behavior
    (the request flows through enforce_quotas with the per-key /
    per-user / per-team / per-workspace caps only).
    """

    def __call__(self, key: GatewayKey) -> TierCaps | None:
        return None


__all__ = [
    "NoopTierCapsResolver",
    "TierCapsResolver",
]
