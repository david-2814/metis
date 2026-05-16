"""Wave 15 billing module — Stripe-backed subscriptions per pricing.md §5.5.4.

The §5.5.4 hybrid lands as three tiers:

  - **Free**  : OSS gateway, no Stripe customer; a `$5/month` spend cap
                composes with the existing per-(user/team/key/workspace)
                quotas.
  - **Pro**   : Per-seat `Subscription` against a Stripe `Customer`;
                `SubscriptionItem.quantity` reflects developer count.
  - **Enterprise**: Pro plus a metered `SubscriptionItem` keyed on the
                cents-of-savings the buyer recouped this billing cycle
                (sourced from `AnalyticsStore.savings()` — the shipped
                counterfactual is the meter).

The module is opt-in: `BillingConfig.enabled=False` (default) leaves
the gateway byte-identical to pre-Wave-15 deployments. When enabled,
`/account/billing` and `/webhooks/stripe` are mounted; the keystore
admission path is unchanged.

Test substrate: `FakeBillingClient` records every call against an
in-memory event log and lets test_billing/ drive happy-path /
webhook-signature / idempotency without `stripe` installed.
Production deployments install the `[billing]` extra to pull
`stripe>=9` and pick up `StripeBillingClient` via the resolver.
"""

from __future__ import annotations

from metis_gateway.billing.client import (
    BillingClient,
    BillingClientError,
    Customer,
    FakeBillingClient,
    Invoice,
    PaymentMethod,
    Subscription,
    SubscriptionItem,
    WebhookEvent,
    WebhookSignatureError,
)
from metis_gateway.billing.config import BillingConfig
from metis_gateway.billing.routes import (
    billing_cancel_handler,
    billing_pause_handler,
    billing_payment_method_handler,
    billing_plan_handler,
    billing_portal_handler,
    billing_status_handler,
    stripe_webhook_handler,
)
from metis_gateway.billing.state import BillingState, build_billing_state
from metis_gateway.billing.store import (
    BillingStore,
    CustomerRecord,
    SubscriptionRecord,
)
from metis_gateway.billing.subscriptions import (
    BillingError,
    BillingService,
    SubscriptionSummary,
)

__all__ = [
    "BillingClient",
    "BillingClientError",
    "BillingConfig",
    "BillingError",
    "BillingService",
    "BillingState",
    "BillingStore",
    "Customer",
    "CustomerRecord",
    "FakeBillingClient",
    "Invoice",
    "PaymentMethod",
    "Subscription",
    "SubscriptionItem",
    "SubscriptionRecord",
    "SubscriptionSummary",
    "WebhookEvent",
    "WebhookSignatureError",
    "billing_cancel_handler",
    "billing_pause_handler",
    "billing_payment_method_handler",
    "billing_plan_handler",
    "billing_portal_handler",
    "billing_status_handler",
    "build_billing_state",
    "stripe_webhook_handler",
]
