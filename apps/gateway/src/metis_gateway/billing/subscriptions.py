"""BillingService — composes BillingClient + BillingStore + event bus.

Public surface is the verbs the routes + webhooks call:

  - `create_pro_subscription(account, seats)` → creates Stripe customer
    (if absent), then Stripe Subscription with the per-seat line.
    Emits `billing.customer_created` + `billing.subscription_created`.
  - `attach_enterprise_addon(account, savings_rate_pct)` → adds the
    metered SubscriptionItem (no-op if already present).
  - `record_savings_usage(account, savings_usd, period_anchor)` → posts
    a Stripe usage record keyed on the (account, period) tuple so
    Stripe dedupes if the sweep runs twice in the same period.
  - `update_payment_method(account, payment_method_id)`.
  - `cancel_subscription(account, at_period_end=True)` /
    `pause_subscription(account)` / `resume_subscription(account)`.
  - `summary(account)` → buyer-facing dict for `GET /account/billing`.

The service emits audit events for every state transition. Events ride
the existing gateway runtime's event bus; the trace store sinks them
alongside the rest of the catalog.

Errors surface as `BillingError` so the route handler can render an
inbound-shape-matched JSON envelope. The underlying `BillingClient`
provider errors are caught + re-raised as `BillingError` with the
status code that maps cleanly to the HTTP response.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal

from metis_core.canonical.ids import new_message_id
from metis_core.events.bus import EventBus
from metis_core.events.envelope import Actor
from metis_core.events.payloads import (
    BillingCustomerCreated,
    BillingInvoicePaid,
    BillingInvoicePaymentFailed,
    BillingSubscriptionCanceled,
    BillingSubscriptionCreated,
    BillingSubscriptionUpdated,
    make_event,
)

from metis_gateway.billing.client import (
    BillingClient,
    BillingClientError,
    deterministic_idempotency_key,
)
from metis_gateway.billing.config import BillingConfig
from metis_gateway.billing.store import (
    BillingStore,
    BillingTier,
    CustomerRecord,
    SubscriptionRecord,
    SubscriptionStatus,
)

logger = logging.getLogger(__name__)


class BillingError(Exception):
    """HTTP-visible billing failure (raised by BillingService)."""

    def __init__(self, message: str, *, status: int = 400, code: str = "billing_error") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


@dataclass(frozen=True)
class SubscriptionSummary:
    """Buyer-facing projection for `GET /account/billing`."""

    account_id: str
    tier: BillingTier
    subscription_tier: BillingTier | None
    status: SubscriptionStatus | None
    stripe_subscription_id: str | None
    pro_seats: int
    enterprise_addon: bool
    current_period_end: datetime | None
    cancel_at_period_end: bool
    pause_collection: bool
    payment_state: Literal["current", "grace", "frozen"]
    payment_failed_at: datetime | None
    payment_grace_until: datetime | None
    access_frozen_at: datetime | None
    free_daily_cap_usd: Decimal | None
    free_monthly_cap_usd: Decimal | None
    payment_method_brand: str | None
    payment_method_last4: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "tier": self.tier,
            "subscription_tier": self.subscription_tier,
            "status": self.status,
            "stripe_subscription_id": self.stripe_subscription_id,
            "pro_seats": self.pro_seats,
            "enterprise_addon": self.enterprise_addon,
            "current_period_end": (
                self.current_period_end.astimezone(UTC).isoformat()
                if self.current_period_end
                else None
            ),
            "cancel_at_period_end": self.cancel_at_period_end,
            "pause_collection": self.pause_collection,
            "payment_state": self.payment_state,
            "payment_failed_at": _iso_or_none(self.payment_failed_at),
            "payment_grace_until": _iso_or_none(self.payment_grace_until),
            "access_frozen_at": _iso_or_none(self.access_frozen_at),
            "free_tier_cap": {
                "daily_cap_usd": _decimal_string_or_none(self.free_daily_cap_usd),
                "monthly_cap_usd": _decimal_string_or_none(self.free_monthly_cap_usd),
            },
            "payment_method": (
                {"brand": self.payment_method_brand, "last4": self.payment_method_last4}
                if self.payment_method_brand or self.payment_method_last4
                else None
            ),
        }


@dataclass
class BillingService:
    config: BillingConfig
    client: BillingClient
    store: BillingStore
    bus: EventBus

    # ----- Customer / subscription create ----------------------------------

    def _ensure_customer(self, *, account_id: str, email: str) -> CustomerRecord:
        existing = self.store.get_customer(account_id)
        if existing is not None:
            return existing
        customer = self.client.create_customer(
            email=email,
            metadata={"account_id": account_id},
        )
        record = CustomerRecord(
            account_id=account_id,
            stripe_customer_id=customer.id,
            tier="free",
            email_sha256=hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest(),
            created_at=datetime.now(UTC),
        )
        self.store.upsert_customer(record)
        self._emit(
            "billing.customer_created",
            BillingCustomerCreated(
                account_id=account_id,
                stripe_customer_id=customer.id,
                email_sha256=record.email_sha256,
                created_at=record.created_at,
            ),
        )
        return record

    def create_pro_subscription(
        self,
        *,
        account_id: str,
        email: str,
        seats: int,
        attach_enterprise_addon: bool = False,
    ) -> SubscriptionRecord:
        if seats < 1:
            raise BillingError("seats must be >= 1", status=400, code="invalid_seats")
        existing_sub = self.store.get_subscription(account_id)
        if existing_sub is not None and existing_sub.status not in {
            "canceled",
            "incomplete_expired",
        }:
            raise BillingError(
                "account already has an active subscription",
                status=409,
                code="subscription_exists",
            )
        customer = self._ensure_customer(account_id=account_id, email=email)
        enterprise_price_id = (
            self.config.enterprise_metered_price_id if attach_enterprise_addon else None
        )
        if attach_enterprise_addon and enterprise_price_id is None:
            raise BillingError(
                "enterprise add-on requested but enterprise_metered_price_id is unset",
                status=400,
                code="enterprise_unconfigured",
            )
        try:
            sub = self.client.create_subscription(
                customer_id=customer.stripe_customer_id,
                pro_price_id=self.config.pro_price_id,
                pro_seats=seats,
                enterprise_metered_price_id=enterprise_price_id,
                metadata={"account_id": account_id},
            )
        except BillingClientError as exc:
            raise BillingError(
                f"stripe rejected subscription creation: {exc}",
                status=502,
                code="stripe_error",
            ) from exc

        pro_item = next((i for i in sub.items if not i.metered), None)
        metered_item = next((i for i in sub.items if i.metered), None)
        if pro_item is None:
            raise BillingError(
                "stripe returned a subscription without a per-seat item",
                status=502,
                code="stripe_inconsistency",
            )

        tier: BillingTier = "enterprise" if attach_enterprise_addon else "pro"
        now = datetime.now(UTC)
        record = SubscriptionRecord(
            account_id=account_id,
            stripe_subscription_id=sub.id,
            tier=tier,
            status=sub.status,
            pro_seats=seats,
            pro_item_id=pro_item.id,
            enterprise_metered_item_id=metered_item.id if metered_item else None,
            current_period_end=sub.current_period_end,
            cancel_at_period_end=sub.cancel_at_period_end,
            pause_collection=sub.pause_collection,
            created_at=now,
            updated_at=now,
        )
        self.store.upsert_subscription(record)
        self.store.set_tier(account_id, tier)

        self._emit(
            "billing.subscription_created",
            BillingSubscriptionCreated(
                account_id=account_id,
                stripe_customer_id=customer.stripe_customer_id,
                stripe_subscription_id=sub.id,
                tier=tier,
                pro_seats=seats,
                enterprise_addon=attach_enterprise_addon,
                current_period_end=sub.current_period_end,
                created_at=now,
            ),
        )
        return record

    # ----- Buyer self-service plan changes --------------------------------

    def create_billing_portal_link(
        self,
        *,
        account_id: str,
        email: str,
        return_url: str,
    ) -> str:
        """Create a Stripe-hosted customer-portal session for this account."""
        customer = self._ensure_customer(account_id=account_id, email=email)
        try:
            return self.client.create_billing_portal_session(
                customer_id=customer.stripe_customer_id,
                return_url=return_url,
            )
        except BillingClientError as exc:
            raise BillingError(
                f"stripe rejected billing portal session: {exc}",
                status=502,
                code="stripe_error",
            ) from exc

    def change_plan(
        self,
        *,
        account_id: str,
        email: str,
        plan: BillingTier,
        seats: int | None = None,
        payment_method_id: str | None = None,
    ) -> SubscriptionSummary:
        """Move an account between Free, Pro, and Enterprise.

        Plan changes reuse the six Wave-15 audit events: new paid plans
        emit `billing.subscription_created`, paid-plan mutations emit
        `billing.subscription_updated`, and downgrades to Free emit
        `billing.subscription_canceled`.
        """
        if plan == "free":
            record = self.store.get_subscription(account_id)
            if record is not None and record.status not in {"canceled", "incomplete_expired"}:
                self.cancel_subscription(account_id=account_id, at_period_end=False)
            else:
                customer = self.store.get_customer(account_id)
                if customer is not None and customer.tier != "free":
                    self.store.set_tier(account_id, "free")
            return self.summary(account_id=account_id)

        target_seats = seats if seats is not None else self._default_seats(account_id)
        if target_seats < 1:
            raise BillingError("seats must be >= 1", status=400, code="invalid_seats")

        if payment_method_id is not None:
            self.update_payment_method(
                account_id=account_id,
                payment_method_id=payment_method_id,
                email=email,
            )

        attach_enterprise = plan == "enterprise"
        existing = self.store.get_subscription(account_id)
        if existing is None or existing.status in {"canceled", "incomplete_expired"}:
            self.create_pro_subscription(
                account_id=account_id,
                email=email,
                seats=target_seats,
                attach_enterprise_addon=attach_enterprise,
            )
            return self.summary(account_id=account_id)

        updated = existing
        if existing.pro_seats != target_seats:
            updated = self._update_seats(existing, seats=target_seats)

        if plan == "enterprise" and updated.enterprise_metered_item_id is None:
            updated = self._add_enterprise_addon(updated)
        elif plan == "pro" and updated.enterprise_metered_item_id is not None:
            updated = self._remove_enterprise_addon(updated)
        elif updated.tier != plan:
            updated = self._record_local_plan_change(updated, tier=plan)

        self.store.set_tier(account_id, plan)
        return self.summary(account_id=account_id)

    # ----- Payment-method ---------------------------------------------------

    def ensure_customer(self, *, account_id: str, email: str) -> CustomerRecord:
        """Public entry point for the route handler to pre-create a customer.

        Used by the subscribe-with-PM flow, where the PM must be attached
        before `create_pro_subscription` runs Stripe's invoice math.
        """
        return self._ensure_customer(account_id=account_id, email=email)

    def update_payment_method(
        self,
        *,
        account_id: str,
        payment_method_id: str,
        email: str | None = None,
    ) -> dict[str, Any]:
        """Attach (or replace) a payment method on the account.

        Lazy-creates a Stripe customer when `email` is provided and no
        customer exists yet — that's the right product shape: "I want to
        attach a payment method" implies "I want to be a customer."
        Without `email`, raises `no_customer` so the standalone PUT-PM
        endpoint surfaces a clear error before subscribing.
        """
        customer = self.store.get_customer(account_id)
        if customer is None:
            if email is None:
                raise BillingError(
                    "no Stripe customer for this account",
                    status=404,
                    code="no_customer",
                )
            customer = self._ensure_customer(account_id=account_id, email=email)
        try:
            pm = self.client.attach_payment_method(
                customer_id=customer.stripe_customer_id,
                payment_method_id=payment_method_id,
            )
        except BillingClientError as exc:
            raise BillingError(
                f"stripe rejected payment method: {exc}",
                status=400,
                code="invalid_payment_method",
            ) from exc
        return {"payment_method_id": pm.id, "brand": pm.brand, "last4": pm.last4}

    # ----- Cancel / pause / resume -----------------------------------------

    def cancel_subscription(
        self,
        *,
        account_id: str,
        at_period_end: bool = True,
    ) -> SubscriptionRecord:
        record = self._require_subscription(account_id)
        try:
            updated_sub = self.client.cancel_subscription(
                subscription_id=record.stripe_subscription_id,
                at_period_end=at_period_end,
            )
        except BillingClientError as exc:
            raise BillingError(
                f"stripe rejected cancellation: {exc}",
                status=502,
                code="stripe_error",
            ) from exc
        now = datetime.now(UTC)
        # We deliberately preserve the local tier ("pro" / "enterprise")
        # until the period actually ends + Stripe sends the
        # `customer.subscription.deleted` webhook. The user keeps access
        # they paid for through `current_period_end`.
        updated = SubscriptionRecord(
            account_id=record.account_id,
            stripe_subscription_id=record.stripe_subscription_id,
            tier=record.tier,
            status=updated_sub.status,
            pro_seats=record.pro_seats,
            pro_item_id=record.pro_item_id,
            enterprise_metered_item_id=record.enterprise_metered_item_id,
            current_period_end=updated_sub.current_period_end,
            cancel_at_period_end=updated_sub.cancel_at_period_end,
            pause_collection=updated_sub.pause_collection,
            created_at=record.created_at,
            updated_at=now,
            payment_failed_at=None,
            payment_grace_until=None,
            access_frozen_at=None,
        )
        self.store.upsert_subscription(updated)

        if not at_period_end:
            # Immediate cancellation — drop the local tier back to free.
            self.store.set_tier(account_id, "free")
            self._emit(
                "billing.subscription_canceled",
                BillingSubscriptionCanceled(
                    account_id=account_id,
                    stripe_subscription_id=record.stripe_subscription_id,
                    canceled_at=now,
                    reason="user_requested",
                    final_period_end=updated_sub.current_period_end,
                ),
            )
        else:
            self._emit(
                "billing.subscription_updated",
                BillingSubscriptionUpdated(
                    account_id=account_id,
                    stripe_subscription_id=record.stripe_subscription_id,
                    previous_status=record.status,
                    status=updated_sub.status,
                    previous_tier=record.tier,
                    tier=record.tier,
                    pro_seats=record.pro_seats,
                    current_period_end=updated_sub.current_period_end,
                    updated_at=now,
                ),
            )
        return updated

    def pause_subscription(self, *, account_id: str) -> SubscriptionRecord:
        record = self._require_subscription(account_id)
        try:
            updated_sub = self.client.pause_subscription(
                subscription_id=record.stripe_subscription_id,
            )
        except BillingClientError as exc:
            raise BillingError(
                f"stripe rejected pause: {exc}",
                status=502,
                code="stripe_error",
            ) from exc
        return self._record_status_change(record, updated_sub, previous_status=record.status)

    def resume_subscription(self, *, account_id: str) -> SubscriptionRecord:
        record = self._require_subscription(account_id)
        try:
            updated_sub = self.client.resume_subscription(
                subscription_id=record.stripe_subscription_id,
            )
        except BillingClientError as exc:
            raise BillingError(
                f"stripe rejected resume: {exc}",
                status=502,
                code="stripe_error",
            ) from exc
        return self._record_status_change(record, updated_sub, previous_status=record.status)

    # ----- Metered usage post -----------------------------------------------

    def record_savings_usage(
        self,
        *,
        account_id: str,
        savings_usd: Decimal,
        period_anchor: datetime,
    ) -> int:
        """Post a Stripe usage record keyed on (account, period).

        Quantity is the cents-of-savings the buyer recouped, multiplied
        by the configured `enterprise_savings_rate_pct`. Stripe dedupes
        on the idempotency key so a retry inside the same period is a
        no-op on the line item.

        Returns the quantity (in cents) we posted, for the caller's audit log.
        """
        if savings_usd < 0:
            raise BillingError(
                "savings_usd must be >= 0",
                status=400,
                code="invalid_savings",
            )
        record = self._require_subscription(account_id)
        if record.enterprise_metered_item_id is None:
            raise BillingError(
                "account has no enterprise add-on",
                status=400,
                code="no_enterprise_addon",
            )
        rate = Decimal(self.config.enterprise_savings_rate_pct) / Decimal("100")
        quantity_cents = int(
            (savings_usd * rate * Decimal("100")).quantize(
                Decimal("1"),
                rounding=ROUND_HALF_UP,
            )
        )
        idempotency = deterministic_idempotency_key(
            account_id,
            period_anchor.astimezone(UTC).strftime("%Y-%m"),
        )
        try:
            self.client.record_metered_usage(
                subscription_item_id=record.enterprise_metered_item_id,
                quantity=quantity_cents,
                timestamp=period_anchor,
                idempotency_key=idempotency,
            )
        except BillingClientError as exc:
            raise BillingError(
                f"stripe rejected usage record: {exc}",
                status=502,
                code="stripe_error",
            ) from exc
        return quantity_cents

    # ----- Webhook handlers (called from webhooks.py) ----------------------

    def apply_subscription_updated(self, *, subscription_id: str, status: str) -> None:
        record = self.store.get_subscription_by_stripe_id(subscription_id)
        if record is None:
            logger.warning("subscription.updated for unknown subscription %s", subscription_id)
            return
        try:
            stripe_sub = self.client.get_subscription(subscription_id=subscription_id)
        except BillingClientError:
            logger.exception("failed to refresh subscription %s after webhook", subscription_id)
            return
        now = datetime.now(UTC)
        payment_failed_at = record.payment_failed_at
        payment_grace_until = record.payment_grace_until
        access_frozen_at = record.access_frozen_at
        if stripe_sub.status == "active":
            payment_failed_at = None
            payment_grace_until = None
            access_frozen_at = None
            self.store.set_tier(record.account_id, record.tier)
        elif stripe_sub.status == "past_due" and payment_grace_until is None:
            payment_failed_at = now
            payment_grace_until = self._payment_grace_until(now)
            access_frozen_at = None
        elif stripe_sub.status in {"unpaid", "canceled", "incomplete_expired"}:
            payment_failed_at = payment_failed_at or now
            payment_grace_until = payment_grace_until or now
            access_frozen_at = access_frozen_at or now
            self.store.set_tier(record.account_id, "free")
        updated = SubscriptionRecord(
            account_id=record.account_id,
            stripe_subscription_id=record.stripe_subscription_id,
            tier=record.tier,
            status=stripe_sub.status,
            pro_seats=record.pro_seats,
            pro_item_id=record.pro_item_id,
            enterprise_metered_item_id=record.enterprise_metered_item_id,
            current_period_end=stripe_sub.current_period_end,
            cancel_at_period_end=stripe_sub.cancel_at_period_end,
            pause_collection=stripe_sub.pause_collection,
            created_at=record.created_at,
            updated_at=now,
            payment_failed_at=payment_failed_at,
            payment_grace_until=payment_grace_until,
            access_frozen_at=access_frozen_at,
        )
        self.store.upsert_subscription(updated)
        self._emit(
            "billing.subscription_updated",
            BillingSubscriptionUpdated(
                account_id=record.account_id,
                stripe_subscription_id=record.stripe_subscription_id,
                previous_status=record.status,
                status=stripe_sub.status,
                previous_tier=record.tier,
                tier=record.tier,
                pro_seats=record.pro_seats,
                current_period_end=stripe_sub.current_period_end,
                updated_at=now,
            ),
        )

    def apply_subscription_deleted(self, *, subscription_id: str) -> None:
        record = self.store.get_subscription_by_stripe_id(subscription_id)
        if record is None:
            logger.warning("subscription.deleted for unknown subscription %s", subscription_id)
            return
        now = datetime.now(UTC)
        # Drop the local tier to free; record stays for auditability.
        self.store.set_tier(record.account_id, "free")
        deleted = SubscriptionRecord(
            account_id=record.account_id,
            stripe_subscription_id=record.stripe_subscription_id,
            tier=record.tier,
            status="canceled",
            pro_seats=record.pro_seats,
            pro_item_id=record.pro_item_id,
            enterprise_metered_item_id=record.enterprise_metered_item_id,
            current_period_end=record.current_period_end,
            cancel_at_period_end=False,
            pause_collection=False,
            created_at=record.created_at,
            updated_at=now,
            payment_failed_at=None,
            payment_grace_until=None,
            access_frozen_at=None,
        )
        self.store.upsert_subscription(deleted)
        self._emit(
            "billing.subscription_canceled",
            BillingSubscriptionCanceled(
                account_id=record.account_id,
                stripe_subscription_id=record.stripe_subscription_id,
                canceled_at=now,
                reason="user_requested",
                final_period_end=record.current_period_end,
            ),
        )

    def apply_invoice_paid(
        self,
        *,
        subscription_id: str,
        invoice_id: str,
        amount_paid_cents: int,
    ) -> None:
        record = self.store.get_subscription_by_stripe_id(subscription_id)
        if record is None:
            logger.warning("invoice.paid for unknown subscription %s", subscription_id)
            return
        now = datetime.now(UTC)
        if record.status not in {"canceled", "incomplete_expired"}:
            self.store.set_tier(record.account_id, record.tier)
            self.store.upsert_subscription(
                SubscriptionRecord(
                    account_id=record.account_id,
                    stripe_subscription_id=record.stripe_subscription_id,
                    tier=record.tier,
                    status="active",
                    pro_seats=record.pro_seats,
                    pro_item_id=record.pro_item_id,
                    enterprise_metered_item_id=record.enterprise_metered_item_id,
                    current_period_end=record.current_period_end,
                    cancel_at_period_end=record.cancel_at_period_end,
                    pause_collection=record.pause_collection,
                    created_at=record.created_at,
                    updated_at=now,
                    payment_failed_at=None,
                    payment_grace_until=None,
                    access_frozen_at=None,
                )
            )
        self._emit(
            "billing.invoice_paid",
            BillingInvoicePaid(
                account_id=record.account_id,
                stripe_subscription_id=subscription_id,
                stripe_invoice_id=invoice_id,
                amount_paid_cents=amount_paid_cents,
                paid_at=now,
            ),
        )

    def apply_invoice_payment_failed(
        self,
        *,
        subscription_id: str,
        invoice_id: str,
        amount_due_cents: int,
        attempt_count: int,
    ) -> None:
        record = self.store.get_subscription_by_stripe_id(subscription_id)
        if record is None:
            logger.warning("invoice.payment_failed for unknown subscription %s", subscription_id)
            return
        now = datetime.now(UTC)
        grace_until = self._payment_grace_until(now)
        self.store.upsert_subscription(
            SubscriptionRecord(
                account_id=record.account_id,
                stripe_subscription_id=record.stripe_subscription_id,
                tier=record.tier,
                status="past_due" if record.status == "active" else record.status,
                pro_seats=record.pro_seats,
                pro_item_id=record.pro_item_id,
                enterprise_metered_item_id=record.enterprise_metered_item_id,
                current_period_end=record.current_period_end,
                cancel_at_period_end=record.cancel_at_period_end,
                pause_collection=record.pause_collection,
                created_at=record.created_at,
                updated_at=now,
                payment_failed_at=now,
                payment_grace_until=grace_until,
                access_frozen_at=None,
            )
        )
        self._emit(
            "billing.invoice_payment_failed",
            BillingInvoicePaymentFailed(
                account_id=record.account_id,
                stripe_subscription_id=subscription_id,
                stripe_invoice_id=invoice_id,
                amount_due_cents=amount_due_cents,
                attempt_count=attempt_count,
                failed_at=now,
            ),
        )

    # ----- Summary ---------------------------------------------------------

    def summary(self, *, account_id: str) -> SubscriptionSummary:
        self.enforce_failed_payment_state(account_id=account_id)
        customer = self.store.get_customer(account_id)
        if customer is None:
            return SubscriptionSummary(
                account_id=account_id,
                tier="free",
                subscription_tier=None,
                status=None,
                stripe_subscription_id=None,
                pro_seats=0,
                enterprise_addon=False,
                current_period_end=None,
                cancel_at_period_end=False,
                pause_collection=False,
                payment_state="current",
                payment_failed_at=None,
                payment_grace_until=None,
                access_frozen_at=None,
                free_daily_cap_usd=self.config.free_daily_cap_usd,
                free_monthly_cap_usd=self.config.free_monthly_cap_usd,
                payment_method_brand=None,
                payment_method_last4=None,
            )
        record = self.store.get_subscription(account_id)
        try:
            stripe_customer = self.client.get_customer(customer_id=customer.stripe_customer_id)
        except BillingClientError:
            stripe_customer = None
        pm = stripe_customer.payment_method if stripe_customer else None
        return SubscriptionSummary(
            account_id=account_id,
            tier=customer.tier,
            subscription_tier=record.tier if record else None,
            status=record.status if record else None,
            stripe_subscription_id=record.stripe_subscription_id if record else None,
            pro_seats=record.pro_seats if record else 0,
            enterprise_addon=(record is not None and record.enterprise_metered_item_id is not None),
            current_period_end=record.current_period_end if record else None,
            cancel_at_period_end=record.cancel_at_period_end if record else False,
            pause_collection=record.pause_collection if record else False,
            payment_state=_payment_state(customer.tier, record),
            payment_failed_at=record.payment_failed_at if record else None,
            payment_grace_until=record.payment_grace_until if record else None,
            access_frozen_at=record.access_frozen_at if record else None,
            free_daily_cap_usd=self.config.free_daily_cap_usd,
            free_monthly_cap_usd=self.config.free_monthly_cap_usd,
            payment_method_brand=pm.brand if pm else None,
            payment_method_last4=pm.last4 if pm else None,
        )

    # ----- Helpers ---------------------------------------------------------

    def _require_subscription(self, account_id: str) -> SubscriptionRecord:
        record = self.store.get_subscription(account_id)
        if record is None:
            raise BillingError(
                "no subscription on this account",
                status=404,
                code="no_subscription",
            )
        return record

    def _record_status_change(
        self,
        record: SubscriptionRecord,
        updated_sub,
        *,
        previous_status: SubscriptionStatus,
    ) -> SubscriptionRecord:
        now = datetime.now(UTC)
        updated = SubscriptionRecord(
            account_id=record.account_id,
            stripe_subscription_id=record.stripe_subscription_id,
            tier=record.tier,
            status=updated_sub.status,
            pro_seats=record.pro_seats,
            pro_item_id=record.pro_item_id,
            enterprise_metered_item_id=record.enterprise_metered_item_id,
            current_period_end=updated_sub.current_period_end,
            cancel_at_period_end=updated_sub.cancel_at_period_end,
            pause_collection=updated_sub.pause_collection,
            created_at=record.created_at,
            updated_at=now,
            payment_failed_at=None if updated_sub.status == "active" else record.payment_failed_at,
            payment_grace_until=None
            if updated_sub.status == "active"
            else record.payment_grace_until,
            access_frozen_at=None if updated_sub.status == "active" else record.access_frozen_at,
        )
        self.store.upsert_subscription(updated)
        self._emit(
            "billing.subscription_updated",
            BillingSubscriptionUpdated(
                account_id=record.account_id,
                stripe_subscription_id=record.stripe_subscription_id,
                previous_status=previous_status,
                status=updated_sub.status,
                previous_tier=record.tier,
                tier=record.tier,
                pro_seats=record.pro_seats,
                current_period_end=updated_sub.current_period_end,
                updated_at=now,
            ),
        )
        return updated

    def enforce_failed_payment_state(
        self,
        *,
        account_id: str,
        now: datetime | None = None,
    ) -> SubscriptionRecord | None:
        """Apply the local 7-day failed-payment grace policy.

        During grace the paid tier remains active. Once grace expires,
        the account's effective tier drops to Free; if its existing spend
        already exceeds the Free cap, the normal tier-cap path blocks
        requests with 429. A later `invoice.payment_succeeded` restores
        the paid tier and clears the frozen marker.
        """
        record = self.store.get_subscription(account_id)
        if record is None or record.payment_grace_until is None:
            return record
        if record.access_frozen_at is not None:
            return record
        now = now or datetime.now(UTC)
        if now < record.payment_grace_until:
            return record
        frozen = SubscriptionRecord(
            account_id=record.account_id,
            stripe_subscription_id=record.stripe_subscription_id,
            tier=record.tier,
            status="unpaid" if record.status in {"active", "past_due"} else record.status,
            pro_seats=record.pro_seats,
            pro_item_id=record.pro_item_id,
            enterprise_metered_item_id=record.enterprise_metered_item_id,
            current_period_end=record.current_period_end,
            cancel_at_period_end=record.cancel_at_period_end,
            pause_collection=record.pause_collection,
            created_at=record.created_at,
            updated_at=now,
            payment_failed_at=record.payment_failed_at,
            payment_grace_until=record.payment_grace_until,
            access_frozen_at=now,
        )
        self.store.upsert_subscription(frozen)
        self.store.set_tier(account_id, "free")
        self._emit(
            "billing.subscription_updated",
            BillingSubscriptionUpdated(
                account_id=record.account_id,
                stripe_subscription_id=record.stripe_subscription_id,
                previous_status=record.status,
                status=frozen.status,
                previous_tier=record.tier,
                tier="free",
                pro_seats=record.pro_seats,
                current_period_end=record.current_period_end,
                updated_at=now,
            ),
        )
        return frozen

    def _default_seats(self, account_id: str) -> int:
        existing = self.store.get_subscription(account_id)
        return existing.pro_seats if existing is not None and existing.pro_seats > 0 else 1

    def _payment_grace_until(self, now: datetime) -> datetime:
        from datetime import timedelta

        return now + timedelta(days=self.config.failed_payment_grace_days)

    def _update_seats(self, record: SubscriptionRecord, *, seats: int) -> SubscriptionRecord:
        try:
            updated_sub = self.client.update_subscription_seats(
                subscription_id=record.stripe_subscription_id,
                pro_item_id=record.pro_item_id,
                seats=seats,
            )
        except BillingClientError as exc:
            raise BillingError(
                f"stripe rejected seat update: {exc}",
                status=502,
                code="stripe_error",
            ) from exc
        return self._record_subscription_update(
            record,
            updated_sub,
            tier=record.tier,
            pro_seats=seats,
            enterprise_metered_item_id=record.enterprise_metered_item_id,
        )

    def _add_enterprise_addon(self, record: SubscriptionRecord) -> SubscriptionRecord:
        price_id = self.config.enterprise_metered_price_id
        if price_id is None:
            raise BillingError(
                "enterprise plan requested but enterprise_metered_price_id is unset",
                status=400,
                code="enterprise_unconfigured",
            )
        try:
            updated_sub = self.client.add_subscription_item(
                subscription_id=record.stripe_subscription_id,
                price_id=price_id,
                metered=True,
            )
        except BillingClientError as exc:
            raise BillingError(
                f"stripe rejected enterprise add-on: {exc}",
                status=502,
                code="stripe_error",
            ) from exc
        metered_item = _metered_item_id(updated_sub, fallback_price_id=price_id)
        if metered_item is None:
            raise BillingError(
                "stripe returned a subscription without a metered enterprise item",
                status=502,
                code="stripe_inconsistency",
            )
        return self._record_subscription_update(
            record,
            updated_sub,
            tier="enterprise",
            pro_seats=record.pro_seats,
            enterprise_metered_item_id=metered_item,
        )

    def _remove_enterprise_addon(self, record: SubscriptionRecord) -> SubscriptionRecord:
        if record.enterprise_metered_item_id is None:
            return record
        try:
            updated_sub = self.client.remove_subscription_item(
                subscription_id=record.stripe_subscription_id,
                subscription_item_id=record.enterprise_metered_item_id,
            )
        except BillingClientError as exc:
            raise BillingError(
                f"stripe rejected enterprise add-on removal: {exc}",
                status=502,
                code="stripe_error",
            ) from exc
        return self._record_subscription_update(
            record,
            updated_sub,
            tier="pro",
            pro_seats=record.pro_seats,
            enterprise_metered_item_id=None,
        )

    def _record_local_plan_change(
        self,
        record: SubscriptionRecord,
        *,
        tier: BillingTier,
    ) -> SubscriptionRecord:
        try:
            updated_sub = self.client.get_subscription(
                subscription_id=record.stripe_subscription_id
            )
        except BillingClientError as exc:
            raise BillingError(
                f"stripe rejected subscription refresh: {exc}",
                status=502,
                code="stripe_error",
            ) from exc
        return self._record_subscription_update(
            record,
            updated_sub,
            tier=tier,
            pro_seats=record.pro_seats,
            enterprise_metered_item_id=record.enterprise_metered_item_id,
        )

    def _record_subscription_update(
        self,
        record: SubscriptionRecord,
        updated_sub,
        *,
        tier: BillingTier,
        pro_seats: int,
        enterprise_metered_item_id: str | None,
    ) -> SubscriptionRecord:
        now = datetime.now(UTC)
        updated = SubscriptionRecord(
            account_id=record.account_id,
            stripe_subscription_id=record.stripe_subscription_id,
            tier=tier,
            status=updated_sub.status,
            pro_seats=pro_seats,
            pro_item_id=record.pro_item_id,
            enterprise_metered_item_id=enterprise_metered_item_id,
            current_period_end=updated_sub.current_period_end,
            cancel_at_period_end=updated_sub.cancel_at_period_end,
            pause_collection=updated_sub.pause_collection,
            created_at=record.created_at,
            updated_at=now,
            payment_failed_at=None if updated_sub.status == "active" else record.payment_failed_at,
            payment_grace_until=None
            if updated_sub.status == "active"
            else record.payment_grace_until,
            access_frozen_at=None if updated_sub.status == "active" else record.access_frozen_at,
        )
        self.store.upsert_subscription(updated)
        self._emit(
            "billing.subscription_updated",
            BillingSubscriptionUpdated(
                account_id=record.account_id,
                stripe_subscription_id=record.stripe_subscription_id,
                previous_status=record.status,
                status=updated.status,
                previous_tier=record.tier,
                tier=tier,
                pro_seats=pro_seats,
                current_period_end=updated.current_period_end,
                updated_at=now,
            ),
        )
        return updated

    def _emit(self, event_type: str, payload) -> None:
        try:
            self.bus.emit(
                make_event(
                    type=event_type,
                    session_id=f"gw_{new_message_id()}",
                    actor=Actor.SYSTEM,
                    payload=payload,
                    timestamp=datetime.now(UTC),
                )
            )
        except Exception:
            logger.warning("failed to emit %s", event_type, exc_info=True)


def _metered_item_id(subscription, *, fallback_price_id: str) -> str | None:
    for item in subscription.items:
        if item.metered or item.price_id == fallback_price_id:
            return item.id
    return None


def _payment_state(
    customer_tier: BillingTier,
    record: SubscriptionRecord | None,
) -> Literal["current", "grace", "frozen"]:
    if record is None:
        return "current"
    if record.access_frozen_at is not None:
        return "frozen"
    if customer_tier == "free" and record.payment_grace_until is not None:
        return "frozen"
    if record.payment_grace_until is not None:
        return "grace"
    return "current"


def _iso_or_none(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value is not None else None


def _decimal_string_or_none(value: Decimal | None) -> str | None:
    return format(value, "f") if value is not None else None


__all__ = [
    "BillingError",
    "BillingService",
    "SubscriptionSummary",
]
