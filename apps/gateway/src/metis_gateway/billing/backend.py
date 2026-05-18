"""StripeBillingBackend — Protocol-shaped adapter over the billing module.

Implements ``metis_core.extensions.BillingBackend`` by delegating to the
existing ``BillingState`` (config + client + store + service). This is the
§4.2a "Protocol-ize without moving" intermediate: the Pro tier code still
lives in OSS, but the gateway calls it through the Protocol field on
``GatewayConfig`` rather than directly via ``_AppState.billing``.

The §4.2b migration step moves this file (plus the rest of the billing
module) to ``metis-pro/src/metis_pro/billing/``. OSS keeps only the
``NoopBillingBackend`` from ``metis_core.extensions``; Pro deployments
import this adapter from ``metis-pro``.

Route mounting happens in ``register_routes`` rather than the explicit list
that used to live in ``build_app``. Starlette's ``app.router.routes`` is a
mutable list; appending to it after construction works for the matcher, and
keeps the route inventory next to the rest of the billing surface.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from starlette.routing import Route

from metis_gateway.billing.routes import (
    billing_cancel_handler,
    billing_pause_handler,
    billing_payment_method_handler,
    billing_plan_handler,
    billing_portal_handler,
    billing_resume_handler,
    billing_status_handler,
    billing_subscribe_handler,
    stripe_webhook_handler,
)
from metis_gateway.billing.state import BillingState
from metis_gateway.billing.subscriptions import BillingError

if TYPE_CHECKING:  # pragma: no cover — type-only import
    from starlette.applications import Starlette


class StripeBillingBackend:
    """Adapter wrapping ``BillingState`` to satisfy ``BillingBackend``.

    Per-request methods (``record_usage`` / ``check_active`` / ``current_tier``)
    delegate to the underlying ``BillingService`` + ``BillingStore``.
    ``register_routes`` mounts the nine ``/account/billing/*`` +
    ``/webhooks/stripe`` routes onto the gateway's Starlette app at boot.
    """

    def __init__(self, state: BillingState) -> None:
        self._state = state

    async def record_usage(self, account_id: str, savings_usd: Decimal) -> None:
        """Record metered savings against the account's subscription.

        Delegates to ``BillingService.record_savings_usage``; swallows
        ``BillingError`` so a billing-side outage doesn't poison the
        request-completion path (the savings counterfactual still surfaces
        in the trace store regardless).
        """
        try:
            self._state.service.record_savings_usage(account_id, savings_usd)
        except BillingError:
            # Logged inside BillingService; deliberately non-fatal here.
            return None

    async def check_active(self, account_id: str) -> bool:
        """Return True iff the account is in good standing.

        Wraps ``BillingService.enforce_failed_payment_state`` (which raises
        on failed-payment grace expiry) into a Boolean.
        """
        try:
            self._state.service.enforce_failed_payment_state(account_id=account_id)
            return True
        except BillingError:
            return False

    async def current_tier(self, account_id: str) -> str:
        """Return the account's billing tier ("free" | "pro" | "enterprise").

        Reads from the billing store; falls back to "free" for accounts
        without a customer record.
        """
        customer = self._state.store.get_customer(account_id)
        if customer is None:
            return "free"
        return customer.tier

    def register_routes(self, app: Starlette) -> None:
        """Mount the nine Pro billing endpoints onto the gateway."""
        app.router.routes.extend(
            [
                Route("/account/billing", billing_status_handler, methods=["GET"]),
                Route("/account/billing/portal", billing_portal_handler, methods=["GET"]),
                Route("/account/billing/plan", billing_plan_handler, methods=["POST"]),
                Route(
                    "/account/billing/subscribe",
                    billing_subscribe_handler,
                    methods=["POST"],
                ),
                Route(
                    "/account/billing/payment-method",
                    billing_payment_method_handler,
                    methods=["POST"],
                ),
                Route("/account/billing/cancel", billing_cancel_handler, methods=["POST"]),
                Route("/account/billing/pause", billing_pause_handler, methods=["POST"]),
                Route("/account/billing/resume", billing_resume_handler, methods=["POST"]),
                Route("/webhooks/stripe", stripe_webhook_handler, methods=["POST"]),
            ]
        )


__all__ = ["StripeBillingBackend"]
