"""Stripe webhook dispatcher.

Routes the four event kinds Wave 15 cares about onto the matching
`BillingService` methods. Idempotency: every event id is recorded in
`BillingStore.processed_events`; replays of the same id are skipped.

The signature-verification step lives in `BillingClient.construct_webhook_event`
so the dispatch layer never touches raw HMAC code — easier to swap in a
different provider's signing scheme.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from metis_gateway.billing.client import WebhookEvent
from metis_gateway.billing.store import BillingStore
from metis_gateway.billing.subscriptions import BillingService

logger = logging.getLogger(__name__)

# The four event kinds Wave 15 handles end-to-end. Others are accepted +
# logged so Stripe sees a 200 (preventing retries) but ignored.
HANDLED_EVENT_KINDS: frozenset[str] = frozenset(
    {
        "customer.subscription.updated",
        "customer.subscription.deleted",
        "invoice.payment_succeeded",
        "invoice.payment_failed",
    }
)


def dispatch_webhook_event(
    *,
    service: BillingService,
    store: BillingStore,
    event: WebhookEvent,
) -> dict[str, Any]:
    """Apply a verified Stripe event, returning a small response dict.

    Idempotency: if the event id has been seen, returns `{"status":
    "duplicate"}` without re-running the side effect.
    """
    if store.has_processed(event.id):
        return {"status": "duplicate", "event_id": event.id}

    kind = event.kind
    if kind not in HANDLED_EVENT_KINDS:
        logger.info("ignoring webhook kind=%s id=%s", kind, event.id)
        store.mark_processed(
            stripe_event_id=event.id,
            kind=kind,
            processed_at=datetime.now(UTC),
        )
        return {"status": "ignored", "event_id": event.id, "kind": kind}

    if kind == "customer.subscription.updated":
        _apply_subscription_updated(service, event.data)
    elif kind == "customer.subscription.deleted":
        _apply_subscription_deleted(service, event.data)
    elif kind == "invoice.payment_succeeded":
        _apply_invoice_paid(service, event.data)
    elif kind == "invoice.payment_failed":
        _apply_invoice_payment_failed(service, event.data)

    store.mark_processed(
        stripe_event_id=event.id,
        kind=kind,
        processed_at=datetime.now(UTC),
    )
    return {"status": "ok", "event_id": event.id, "kind": kind}


def _apply_subscription_updated(service: BillingService, data: dict[str, Any]) -> None:
    subscription_id = str(data.get("id", ""))
    status = str(data.get("status", ""))
    if not subscription_id:
        logger.warning("subscription.updated missing id")
        return
    service.apply_subscription_updated(
        subscription_id=subscription_id,
        status=status,
    )


def _apply_subscription_deleted(service: BillingService, data: dict[str, Any]) -> None:
    subscription_id = str(data.get("id", ""))
    if not subscription_id:
        logger.warning("subscription.deleted missing id")
        return
    service.apply_subscription_deleted(subscription_id=subscription_id)


def _apply_invoice_paid(service: BillingService, data: dict[str, Any]) -> None:
    invoice_id = str(data.get("id", ""))
    subscription_id_raw = data.get("subscription")
    subscription_id = str(subscription_id_raw) if subscription_id_raw else ""
    amount_paid = int(data.get("amount_paid", 0))
    if not invoice_id or not subscription_id:
        logger.warning(
            "invoice.payment_succeeded missing fields: id=%r subscription=%r",
            invoice_id,
            subscription_id,
        )
        return
    service.apply_invoice_paid(
        subscription_id=subscription_id,
        invoice_id=invoice_id,
        amount_paid_cents=amount_paid,
    )


def _apply_invoice_payment_failed(service: BillingService, data: dict[str, Any]) -> None:
    invoice_id = str(data.get("id", ""))
    subscription_id_raw = data.get("subscription")
    subscription_id = str(subscription_id_raw) if subscription_id_raw else ""
    amount_due = int(data.get("amount_due", 0))
    attempt_count = int(data.get("attempt_count", 0))
    if not invoice_id or not subscription_id:
        logger.warning(
            "invoice.payment_failed missing fields: id=%r subscription=%r",
            invoice_id,
            subscription_id,
        )
        return
    service.apply_invoice_payment_failed(
        subscription_id=subscription_id,
        invoice_id=invoice_id,
        amount_due_cents=amount_due,
        attempt_count=attempt_count,
    )


__all__ = ["HANDLED_EVENT_KINDS", "dispatch_webhook_event"]
