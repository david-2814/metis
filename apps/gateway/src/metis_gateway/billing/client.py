"""Billing-provider client abstraction.

`BillingClient` is the Protocol every billing backend implements; only
two concrete shapes ship in v1:

  - `FakeBillingClient` — in-memory event log; the test substrate. No
    `stripe` install required.
  - `StripeBillingClient` — wraps the official `stripe>=9` SDK. Lazily
    imported on first instantiation so the gateway package keeps a
    clean import graph when billing is disabled.

Everything else (`Customer`, `Subscription`, `Invoice`, `WebhookEvent`)
is a small frozen dataclass — the gateway speaks these, not Stripe
Resource objects. Mapping from Stripe → these dataclasses lives in
the concrete client; that's the seam any future Lago / Orb / Paddle
backend would slot into.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol, runtime_checkable

# Stripe's API uses integer cents end-to-end to avoid float drift; we
# follow the same convention all the way through the BillingClient surface.
# Decimal would be more rigorous but Stripe doesn't accept it on the wire.
Cents = int

# Stripe subscription statuses. Kept open (Literal here is documentary)
# so a future Stripe API addition doesn't break this module — the
# webhook handler treats unknown statuses as no-op.
StripeSubscriptionStatus = Literal[
    "trialing",
    "active",
    "past_due",
    "unpaid",
    "canceled",
    "incomplete",
    "incomplete_expired",
    "paused",
]


class BillingClientError(Exception):
    """Provider-side error surfaced to the gateway."""


class WebhookSignatureError(Exception):
    """Webhook payload failed signature verification.

    Distinct from `BillingClientError` so the route handler can return
    400 instead of treating it as an upstream Stripe error.
    """


# ---------------------------------------------------------------------------
# DTOs the gateway speaks
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PaymentMethod:
    id: str
    brand: str | None
    last4: str | None


@dataclass(frozen=True)
class Customer:
    id: str
    email: str
    payment_method: PaymentMethod | None = None


@dataclass(frozen=True)
class SubscriptionItem:
    id: str
    price_id: str
    quantity: int | None
    # When True the item is metered (no quantity at creation; usage records
    # post against it). Required for the §5.5.4 Enterprise add-on.
    metered: bool = False


@dataclass(frozen=True)
class Subscription:
    id: str
    customer_id: str
    status: StripeSubscriptionStatus
    items: tuple[SubscriptionItem, ...]
    current_period_end: datetime
    cancel_at_period_end: bool = False
    pause_collection: bool = False


@dataclass(frozen=True)
class Invoice:
    id: str
    subscription_id: str
    amount_due_cents: Cents
    amount_paid_cents: Cents
    status: Literal["draft", "open", "paid", "uncollectible", "void"]
    attempt_count: int = 0


@dataclass(frozen=True)
class WebhookEvent:
    """Verified Stripe webhook event.

    `kind` is Stripe's `type` (e.g. `invoice.payment_succeeded`); `data`
    is the parsed event object dict. Kept thin on purpose — the
    subscription / invoice handlers project it down.
    """

    id: str
    kind: str
    data: dict[str, Any]
    livemode: bool


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class BillingClient(Protocol):
    """Provider-agnostic surface the BillingService talks to."""

    def create_customer(self, *, email: str, metadata: dict[str, str]) -> Customer: ...

    def attach_payment_method(
        self, *, customer_id: str, payment_method_id: str
    ) -> PaymentMethod: ...

    def create_subscription(
        self,
        *,
        customer_id: str,
        pro_price_id: str,
        pro_seats: int,
        enterprise_metered_price_id: str | None,
        metadata: dict[str, str],
    ) -> Subscription: ...

    def update_subscription_seats(
        self,
        *,
        subscription_id: str,
        pro_item_id: str,
        seats: int,
    ) -> Subscription: ...

    def record_metered_usage(
        self,
        *,
        subscription_item_id: str,
        quantity: Cents,
        timestamp: datetime,
        idempotency_key: str,
    ) -> None: ...

    def cancel_subscription(
        self,
        *,
        subscription_id: str,
        at_period_end: bool,
    ) -> Subscription: ...

    def pause_subscription(self, *, subscription_id: str) -> Subscription: ...

    def resume_subscription(self, *, subscription_id: str) -> Subscription: ...

    def get_subscription(self, *, subscription_id: str) -> Subscription: ...

    def get_customer(self, *, customer_id: str) -> Customer: ...

    def construct_webhook_event(self, *, payload: bytes, signature: str) -> WebhookEvent:
        """Verify the Stripe signature and parse the event.

        Raises `WebhookSignatureError` on failed verification.
        """
        ...


# ---------------------------------------------------------------------------
# FakeBillingClient — the test substrate
# ---------------------------------------------------------------------------


@dataclass
class FakeBillingClient:
    """In-memory implementation for tests.

    Every mutation appends to `calls` (an audit log the tests assert
    against). Webhook signature verification is a constant-time HMAC
    against `webhook_secret` so the signature-verification tests
    exercise real cryptographic comparison, not a string match.

    `seats_to_unit_price_cents` controls invoice math for happy-path
    tests; default 50 cents/seat keeps invoice amounts small.
    """

    webhook_secret: str = "whsec_test_fake"
    seats_to_unit_price_cents: Cents = 50

    customers: dict[str, Customer] = field(default_factory=dict)
    subscriptions: dict[str, Subscription] = field(default_factory=dict)
    invoices: dict[str, Invoice] = field(default_factory=dict)
    metered_usage_records: list[dict[str, Any]] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)

    _next_seq: int = 0

    def _next_id(self, prefix: str) -> str:
        self._next_seq += 1
        return f"{prefix}_{self._next_seq:06d}"

    def _log(self, name: str, **kwargs: Any) -> None:
        self.calls.append({"name": name, **kwargs})

    # --- Customer / payment-method ----------------------------------------

    def create_customer(self, *, email: str, metadata: dict[str, str]) -> Customer:
        self._log("create_customer", email=email, metadata=dict(metadata))
        customer = Customer(id=self._next_id("cus"), email=email)
        self.customers[customer.id] = customer
        return customer

    def attach_payment_method(self, *, customer_id: str, payment_method_id: str) -> PaymentMethod:
        self._log(
            "attach_payment_method",
            customer_id=customer_id,
            payment_method_id=payment_method_id,
        )
        existing = self.customers.get(customer_id)
        if existing is None:
            raise BillingClientError(f"unknown customer {customer_id}")
        pm = PaymentMethod(id=payment_method_id, brand="visa", last4="4242")
        self.customers[customer_id] = Customer(
            id=existing.id,
            email=existing.email,
            payment_method=pm,
        )
        return pm

    def get_customer(self, *, customer_id: str) -> Customer:
        existing = self.customers.get(customer_id)
        if existing is None:
            raise BillingClientError(f"unknown customer {customer_id}")
        return existing

    # --- Subscription -----------------------------------------------------

    def create_subscription(
        self,
        *,
        customer_id: str,
        pro_price_id: str,
        pro_seats: int,
        enterprise_metered_price_id: str | None,
        metadata: dict[str, str],
    ) -> Subscription:
        self._log(
            "create_subscription",
            customer_id=customer_id,
            pro_price_id=pro_price_id,
            pro_seats=pro_seats,
            enterprise_metered_price_id=enterprise_metered_price_id,
            metadata=dict(metadata),
        )
        if pro_seats < 1:
            raise BillingClientError("pro_seats must be >= 1")
        items: list[SubscriptionItem] = [
            SubscriptionItem(
                id=self._next_id("si"),
                price_id=pro_price_id,
                quantity=pro_seats,
                metered=False,
            )
        ]
        if enterprise_metered_price_id is not None:
            items.append(
                SubscriptionItem(
                    id=self._next_id("si"),
                    price_id=enterprise_metered_price_id,
                    quantity=None,
                    metered=True,
                )
            )
        sub = Subscription(
            id=self._next_id("sub"),
            customer_id=customer_id,
            status="active",
            items=tuple(items),
            current_period_end=datetime.now(UTC) + timedelta(days=30),
        )
        self.subscriptions[sub.id] = sub
        return sub

    def update_subscription_seats(
        self,
        *,
        subscription_id: str,
        pro_item_id: str,
        seats: int,
    ) -> Subscription:
        self._log(
            "update_subscription_seats",
            subscription_id=subscription_id,
            pro_item_id=pro_item_id,
            seats=seats,
        )
        if seats < 1:
            raise BillingClientError("seats must be >= 1")
        sub = self.get_subscription(subscription_id=subscription_id)
        new_items: list[SubscriptionItem] = []
        found = False
        for item in sub.items:
            if item.id == pro_item_id:
                new_items.append(
                    SubscriptionItem(
                        id=item.id,
                        price_id=item.price_id,
                        quantity=seats,
                        metered=item.metered,
                    )
                )
                found = True
            else:
                new_items.append(item)
        if not found:
            raise BillingClientError(f"item {pro_item_id} not on subscription {subscription_id}")
        updated = Subscription(
            id=sub.id,
            customer_id=sub.customer_id,
            status=sub.status,
            items=tuple(new_items),
            current_period_end=sub.current_period_end,
            cancel_at_period_end=sub.cancel_at_period_end,
            pause_collection=sub.pause_collection,
        )
        self.subscriptions[sub.id] = updated
        return updated

    def record_metered_usage(
        self,
        *,
        subscription_item_id: str,
        quantity: Cents,
        timestamp: datetime,
        idempotency_key: str,
    ) -> None:
        self._log(
            "record_metered_usage",
            subscription_item_id=subscription_item_id,
            quantity=quantity,
            timestamp=timestamp.isoformat(),
            idempotency_key=idempotency_key,
        )
        if quantity < 0:
            raise BillingClientError("quantity must be >= 0")
        # Idempotency: skip the duplicate, matching Stripe semantics.
        for record in self.metered_usage_records:
            if record["idempotency_key"] == idempotency_key:
                return
        self.metered_usage_records.append(
            {
                "subscription_item_id": subscription_item_id,
                "quantity": quantity,
                "timestamp": timestamp,
                "idempotency_key": idempotency_key,
            }
        )

    def cancel_subscription(
        self,
        *,
        subscription_id: str,
        at_period_end: bool,
    ) -> Subscription:
        self._log(
            "cancel_subscription",
            subscription_id=subscription_id,
            at_period_end=at_period_end,
        )
        sub = self.get_subscription(subscription_id=subscription_id)
        if at_period_end:
            updated = Subscription(
                id=sub.id,
                customer_id=sub.customer_id,
                status=sub.status,
                items=sub.items,
                current_period_end=sub.current_period_end,
                cancel_at_period_end=True,
                pause_collection=sub.pause_collection,
            )
        else:
            updated = Subscription(
                id=sub.id,
                customer_id=sub.customer_id,
                status="canceled",
                items=sub.items,
                current_period_end=sub.current_period_end,
                cancel_at_period_end=False,
                pause_collection=sub.pause_collection,
            )
        self.subscriptions[sub.id] = updated
        return updated

    def pause_subscription(self, *, subscription_id: str) -> Subscription:
        self._log("pause_subscription", subscription_id=subscription_id)
        sub = self.get_subscription(subscription_id=subscription_id)
        updated = Subscription(
            id=sub.id,
            customer_id=sub.customer_id,
            status="paused",
            items=sub.items,
            current_period_end=sub.current_period_end,
            cancel_at_period_end=sub.cancel_at_period_end,
            pause_collection=True,
        )
        self.subscriptions[sub.id] = updated
        return updated

    def resume_subscription(self, *, subscription_id: str) -> Subscription:
        self._log("resume_subscription", subscription_id=subscription_id)
        sub = self.get_subscription(subscription_id=subscription_id)
        updated = Subscription(
            id=sub.id,
            customer_id=sub.customer_id,
            status="active",
            items=sub.items,
            current_period_end=sub.current_period_end,
            cancel_at_period_end=sub.cancel_at_period_end,
            pause_collection=False,
        )
        self.subscriptions[sub.id] = updated
        return updated

    def get_subscription(self, *, subscription_id: str) -> Subscription:
        existing = self.subscriptions.get(subscription_id)
        if existing is None:
            raise BillingClientError(f"unknown subscription {subscription_id}")
        return existing

    # --- Webhook ---------------------------------------------------------

    def construct_webhook_event(self, *, payload: bytes, signature: str) -> WebhookEvent:
        if not _verify_fake_signature(payload, signature, self.webhook_secret):
            raise WebhookSignatureError("signature mismatch")
        import json

        body = json.loads(payload.decode("utf-8"))
        return WebhookEvent(
            id=str(body.get("id", "")),
            kind=str(body.get("type", "")),
            data=dict(body.get("data", {}).get("object", {})),
            livemode=bool(body.get("livemode", False)),
        )

    # --- Test affordances ------------------------------------------------

    def sign_payload(self, payload: bytes) -> str:
        """Produce a fake-format signature the constructor will accept."""
        return _sign_fake(payload, self.webhook_secret)

    def simulate_invoice_paid(
        self,
        *,
        subscription_id: str,
        amount_paid_cents: Cents = 5000,
    ) -> Invoice:
        sub = self.get_subscription(subscription_id=subscription_id)
        invoice = Invoice(
            id=self._next_id("in"),
            subscription_id=sub.id,
            amount_due_cents=amount_paid_cents,
            amount_paid_cents=amount_paid_cents,
            status="paid",
            attempt_count=1,
        )
        self.invoices[invoice.id] = invoice
        return invoice


def _sign_fake(payload: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    timestamp = int(datetime.now(UTC).timestamp())
    return f"t={timestamp},v1={digest}"


def _verify_fake_signature(payload: bytes, signature: str, secret: str) -> bool:
    parts = dict(p.split("=", 1) for p in signature.split(",") if "=" in p)
    received = parts.get("v1")
    if not received:
        return False
    expected = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, received)


# ---------------------------------------------------------------------------
# StripeBillingClient — real Stripe wrapper (only imported when used)
# ---------------------------------------------------------------------------


class StripeBillingClient:
    """Production client. Pulls in `stripe>=9` on first instantiation.

    Install via the `[billing]` extra; without it, instantiating this
    class raises a clean ImportError with the install hint. The runtime
    fallback discipline is intentional — the gateway can ship without
    the `stripe` dep when billing is disabled.
    """

    def __init__(self, *, api_key: str, webhook_secret: str) -> None:
        try:
            self._stripe = importlib.import_module("stripe")
        except ImportError as exc:
            raise ImportError(
                "metis-gateway[billing] is required for StripeBillingClient. "
                "Install with: uv pip install metis-gateway[billing]"
            ) from exc
        self._stripe.api_key = api_key
        self._webhook_secret = webhook_secret

    def create_customer(self, *, email: str, metadata: dict[str, str]) -> Customer:
        obj = self._stripe.Customer.create(email=email, metadata=metadata)
        return Customer(id=obj.id, email=obj.email)

    def attach_payment_method(self, *, customer_id: str, payment_method_id: str) -> PaymentMethod:
        pm = self._stripe.PaymentMethod.attach(payment_method_id, customer=customer_id)
        self._stripe.Customer.modify(
            customer_id,
            invoice_settings={"default_payment_method": payment_method_id},
        )
        card = getattr(pm, "card", None)
        return PaymentMethod(
            id=pm.id,
            brand=getattr(card, "brand", None) if card else None,
            last4=getattr(card, "last4", None) if card else None,
        )

    def get_customer(self, *, customer_id: str) -> Customer:
        obj = self._stripe.Customer.retrieve(customer_id)
        pm: PaymentMethod | None = None
        invoice_settings = getattr(obj, "invoice_settings", None)
        default_pm = (
            getattr(invoice_settings, "default_payment_method", None) if invoice_settings else None
        )
        if default_pm:
            try:
                pm_obj = self._stripe.PaymentMethod.retrieve(default_pm)
                card = getattr(pm_obj, "card", None)
                pm = PaymentMethod(
                    id=pm_obj.id,
                    brand=getattr(card, "brand", None) if card else None,
                    last4=getattr(card, "last4", None) if card else None,
                )
            except Exception:
                pm = None
        return Customer(id=obj.id, email=obj.email, payment_method=pm)

    def create_subscription(
        self,
        *,
        customer_id: str,
        pro_price_id: str,
        pro_seats: int,
        enterprise_metered_price_id: str | None,
        metadata: dict[str, str],
    ) -> Subscription:
        items: list[dict[str, Any]] = [{"price": pro_price_id, "quantity": pro_seats}]
        if enterprise_metered_price_id is not None:
            items.append({"price": enterprise_metered_price_id})
        obj = self._stripe.Subscription.create(
            customer=customer_id,
            items=items,
            metadata=metadata,
        )
        return _to_subscription_dto(obj)

    def update_subscription_seats(
        self,
        *,
        subscription_id: str,
        pro_item_id: str,
        seats: int,
    ) -> Subscription:
        self._stripe.SubscriptionItem.modify(pro_item_id, quantity=seats)
        obj = self._stripe.Subscription.retrieve(subscription_id)
        return _to_subscription_dto(obj)

    def record_metered_usage(
        self,
        *,
        subscription_item_id: str,
        quantity: Cents,
        timestamp: datetime,
        idempotency_key: str,
    ) -> None:
        self._stripe.SubscriptionItem.create_usage_record(
            subscription_item_id,
            quantity=quantity,
            timestamp=int(timestamp.timestamp()),
            action="set",
            idempotency_key=idempotency_key,
        )

    def cancel_subscription(
        self,
        *,
        subscription_id: str,
        at_period_end: bool,
    ) -> Subscription:
        if at_period_end:
            obj = self._stripe.Subscription.modify(subscription_id, cancel_at_period_end=True)
        else:
            obj = self._stripe.Subscription.delete(subscription_id)
        return _to_subscription_dto(obj)

    def pause_subscription(self, *, subscription_id: str) -> Subscription:
        obj = self._stripe.Subscription.modify(
            subscription_id,
            pause_collection={"behavior": "void"},
        )
        return _to_subscription_dto(obj)

    def resume_subscription(self, *, subscription_id: str) -> Subscription:
        obj = self._stripe.Subscription.modify(subscription_id, pause_collection="")
        return _to_subscription_dto(obj)

    def get_subscription(self, *, subscription_id: str) -> Subscription:
        obj = self._stripe.Subscription.retrieve(subscription_id)
        return _to_subscription_dto(obj)

    def construct_webhook_event(self, *, payload: bytes, signature: str) -> WebhookEvent:
        try:
            event = self._stripe.Webhook.construct_event(payload, signature, self._webhook_secret)
        except Exception as exc:
            raise WebhookSignatureError(str(exc)) from exc
        data_object = event.get("data", {}).get("object", {}) if isinstance(event, dict) else {}
        return WebhookEvent(
            id=str(event.get("id", "")),
            kind=str(event.get("type", "")),
            data=dict(data_object),
            livemode=bool(event.get("livemode", False)),
        )


def _to_subscription_dto(obj: Any) -> Subscription:
    items: list[SubscriptionItem] = []
    raw_items = getattr(obj, "items", None)
    data_iter = getattr(raw_items, "data", []) if raw_items is not None else []
    for it in data_iter:
        price = getattr(it, "price", None)
        recurring = getattr(price, "recurring", None) if price else None
        usage_type = getattr(recurring, "usage_type", "licensed") if recurring else "licensed"
        items.append(
            SubscriptionItem(
                id=it.id,
                price_id=price.id if price else "",
                quantity=getattr(it, "quantity", None),
                metered=(usage_type == "metered"),
            )
        )
    return Subscription(
        id=obj.id,
        customer_id=obj.customer,
        status=obj.status,
        items=tuple(items),
        current_period_end=datetime.fromtimestamp(obj.current_period_end, tz=UTC),
        cancel_at_period_end=getattr(obj, "cancel_at_period_end", False),
        pause_collection=bool(getattr(obj, "pause_collection", None)),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def deterministic_idempotency_key(*parts: str) -> str:
    """Build a Stripe idempotency key from a tuple of stable inputs.

    `record_metered_usage` keys on (account_id, billing_period, ...) so
    a retry of the same sweep produces the same key and Stripe dedupes.
    """
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]
    return f"metis_{digest}"


def random_payment_method_id() -> str:
    """Generate a fake payment-method id for tests that need a new one."""
    return f"pm_test_{secrets.token_hex(8)}"
