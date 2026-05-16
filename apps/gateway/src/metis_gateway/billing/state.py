"""Billing app-state assembly.

`build_billing_state(config, bus, *, client=...)` instantiates the
`BillingClient` (Stripe by default, Fake when injected) + `BillingStore`
+ `BillingService` and bundles them onto `BillingState` for the
gateway's `_AppState`.

The `client` kwarg is the test-injection seam: pass a `FakeBillingClient`
to bypass the StripeBillingClient lazy import.
"""

from __future__ import annotations

from dataclasses import dataclass

from metis_core.events.bus import EventBus

from metis_gateway.billing.client import BillingClient, StripeBillingClient
from metis_gateway.billing.config import BillingConfig
from metis_gateway.billing.store import BillingStore
from metis_gateway.billing.subscriptions import BillingService


class BillingConfigError(ValueError):
    """Raised when a BillingConfig has `enabled=True` but missing creds."""


@dataclass
class BillingState:
    config: BillingConfig
    client: BillingClient
    store: BillingStore
    service: BillingService


def build_billing_state(
    config: BillingConfig | None,
    bus: EventBus,
    *,
    client: BillingClient | None = None,
) -> BillingState | None:
    """Materialize the billing layer, or `None` when disabled.

    Validates that `enabled=True` carries real Stripe creds (api key +
    webhook secret) unless a client is injected — the test path injects
    `FakeBillingClient` and skips the cred check.
    """
    if config is None or not config.enabled:
        return None

    if client is None:
        if not config.stripe_api_key:
            raise BillingConfigError(
                "BillingConfig.enabled=True requires stripe_api_key (or pass client=...)"
            )
        if not config.stripe_webhook_secret:
            raise BillingConfigError("BillingConfig.enabled=True requires stripe_webhook_secret")
        client = StripeBillingClient(
            api_key=config.stripe_api_key,
            webhook_secret=config.stripe_webhook_secret,
        )

    store = BillingStore(config.resolved_store_path())
    service = BillingService(config=config, client=client, store=store, bus=bus)
    return BillingState(config=config, client=client, store=store, service=service)


__all__ = [
    "BillingConfigError",
    "BillingState",
    "build_billing_state",
]
