"""HTTP handlers for the billing surface.

  GET    /account/billing                 → SubscriptionSummary
  POST   /account/billing/subscribe       → create_pro_subscription
  POST   /account/billing/payment-method  → update_payment_method
  POST   /account/billing/cancel          → cancel_subscription (at-period-end by default)
  POST   /account/billing/pause           → pause_subscription
  POST   /account/billing/resume          → resume_subscription
  POST   /webhooks/stripe                 → dispatch_webhook_event

All `/account/billing/*` handlers require a valid signup-session token
(`require_session` from `signup.py`) — the same surface that gates
`/account/keys`. Stripe webhook auth is signature-based, not session-
based.

JSON errors follow the same envelope the signup module uses:
`{"error": {"code": ..., "message": ...}}`.
"""

from __future__ import annotations

import logging
from typing import Any

import msgspec
from starlette.requests import Request
from starlette.responses import Response

from metis_gateway.billing.client import WebhookSignatureError
from metis_gateway.billing.state import BillingState
from metis_gateway.billing.subscriptions import BillingError
from metis_gateway.billing.webhooks import dispatch_webhook_event
from metis_gateway.signup import (
    SignupError,
    _require_session,  # type: ignore[attr-defined]
)
from metis_gateway.signup import (
    _state as signup_state,  # type: ignore[attr-defined]
)

logger = logging.getLogger(__name__)


def _billing_state(request: Request) -> BillingState:
    app_state = request.app.state.app_state
    billing = getattr(app_state, "billing", None)
    if billing is None:
        raise BillingError(
            "billing is disabled on this deployment",
            status=404,
            code="not_found",
        )
    return billing


def _require_account(request: Request):
    """Resolve the signup-session-bound account; raises SignupError on 401."""
    _, account_store = signup_state(request)
    return _require_session(request, account_store)


async def _read_json(request: Request, *, allow_empty: bool = False) -> dict[str, Any]:
    raw = await request.body()
    if not raw:
        if allow_empty:
            return {}
        raise BillingError("request body is empty", code="empty_body")
    try:
        decoded = msgspec.json.decode(raw)
    except Exception as exc:
        raise BillingError(f"invalid JSON body: {exc}", code="invalid_json") from exc
    if not isinstance(decoded, dict):
        raise BillingError("request body must be a JSON object", code="invalid_body")
    return decoded


def _json(body: dict[str, Any], *, status: int = 200) -> Response:
    return Response(
        content=msgspec.json.encode(body),
        media_type="application/json",
        status_code=status,
    )


def billing_error_response(exc: BillingError) -> Response:
    return _json(
        {"error": {"code": exc.code, "message": str(exc)}},
        status=exc.status,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


async def billing_status_handler(request: Request) -> Response:
    """GET /account/billing — current subscription summary."""
    billing = _billing_state(request)
    account = _require_account(request)
    summary = billing.service.summary(account_id=account.account_id)
    return _json(summary.to_dict())


async def billing_subscribe_handler(request: Request) -> Response:
    """POST /account/billing/subscribe — create a Pro subscription.

    Body: `{"seats": int, "enterprise_addon": bool, "payment_method_id":
    str | null}`. When `payment_method_id` is set, the PM is attached
    to the customer before the subscription is created.
    """
    billing = _billing_state(request)
    account = _require_account(request)
    body = await _read_json(request)

    seats_raw = body.get("seats", 1)
    if not isinstance(seats_raw, int) or seats_raw < 1:
        raise BillingError(
            "seats must be a positive integer",
            status=400,
            code="invalid_seats",
        )

    enterprise_addon = bool(body.get("enterprise_addon", False))
    payment_method_id_raw = body.get("payment_method_id")
    payment_method_id: str | None = None
    if payment_method_id_raw is not None:
        if not isinstance(payment_method_id_raw, str) or not payment_method_id_raw:
            raise BillingError(
                "payment_method_id must be a non-empty string",
                status=400,
                code="invalid_payment_method",
            )
        payment_method_id = payment_method_id_raw

    if payment_method_id is not None:
        # Pre-attach so the subscription create finds a default PM. The
        # service lazily creates the Stripe customer if needed (this is
        # the buyer's first interaction). If attachment fails, the
        # subscription is not yet created and the buyer can retry.
        billing.service.update_payment_method(
            account_id=account.account_id,
            payment_method_id=payment_method_id,
            email=account.email,
        )

    record = billing.service.create_pro_subscription(
        account_id=account.account_id,
        email=account.email,
        seats=seats_raw,
        attach_enterprise_addon=enterprise_addon,
    )
    return _json(
        {
            "account_id": account.account_id,
            "subscription_id": record.stripe_subscription_id,
            "tier": record.tier,
            "status": record.status,
            "pro_seats": record.pro_seats,
            "enterprise_addon": record.enterprise_metered_item_id is not None,
            "current_period_end": record.current_period_end.isoformat(),
        },
        status=201,
    )


async def billing_payment_method_handler(request: Request) -> Response:
    """POST /account/billing/payment-method — attach / replace the PM."""
    billing = _billing_state(request)
    account = _require_account(request)
    body = await _read_json(request)
    payment_method_id = body.get("payment_method_id")
    if not isinstance(payment_method_id, str) or not payment_method_id:
        raise BillingError(
            "payment_method_id is required",
            status=400,
            code="missing_payment_method",
        )
    result = billing.service.update_payment_method(
        account_id=account.account_id,
        payment_method_id=payment_method_id,
    )
    return _json({"account_id": account.account_id, **result})


async def billing_cancel_handler(request: Request) -> Response:
    """POST /account/billing/cancel — cancel-at-period-end by default.

    Body: `{"at_period_end": bool}` (default True). Cancellation
    preserves access through `current_period_end`; immediate hard
    cancel is opt-in.
    """
    billing = _billing_state(request)
    account = _require_account(request)
    body = await _read_json(request, allow_empty=True)
    at_period_end = bool(body.get("at_period_end", True))
    record = billing.service.cancel_subscription(
        account_id=account.account_id,
        at_period_end=at_period_end,
    )
    return _json(
        {
            "account_id": account.account_id,
            "subscription_id": record.stripe_subscription_id,
            "status": record.status,
            "cancel_at_period_end": record.cancel_at_period_end,
            "current_period_end": record.current_period_end.isoformat(),
        }
    )


async def billing_pause_handler(request: Request) -> Response:
    """POST /account/billing/pause — pause collection (Stripe-side void)."""
    billing = _billing_state(request)
    account = _require_account(request)
    record = billing.service.pause_subscription(account_id=account.account_id)
    return _json(
        {
            "account_id": account.account_id,
            "subscription_id": record.stripe_subscription_id,
            "status": record.status,
            "pause_collection": record.pause_collection,
        }
    )


async def billing_resume_handler(request: Request) -> Response:
    """POST /account/billing/resume — undo pause."""
    billing = _billing_state(request)
    account = _require_account(request)
    record = billing.service.resume_subscription(account_id=account.account_id)
    return _json(
        {
            "account_id": account.account_id,
            "subscription_id": record.stripe_subscription_id,
            "status": record.status,
            "pause_collection": record.pause_collection,
        }
    )


async def stripe_webhook_handler(request: Request) -> Response:
    """POST /webhooks/stripe — verify + dispatch Stripe webhook events."""
    billing = _billing_state(request)
    signature = request.headers.get("stripe-signature")
    if not signature:
        return _json(
            {"error": {"code": "missing_signature", "message": "Stripe-Signature header required"}},
            status=400,
        )
    raw = await request.body()
    try:
        event = billing.client.construct_webhook_event(payload=raw, signature=signature)
    except WebhookSignatureError as exc:
        return _json(
            {"error": {"code": "signature_invalid", "message": str(exc)}},
            status=400,
        )
    result = dispatch_webhook_event(
        service=billing.service,
        store=billing.store,
        event=event,
    )
    return _json(result)


# ---------------------------------------------------------------------------
# Error responses (re-exported for app.py's exception handlers)
# ---------------------------------------------------------------------------


def billing_or_signup_error_response(exc: Exception) -> Response | None:
    """Helper for app.py's catch-all: route billing/signup errors to JSON."""
    if isinstance(exc, BillingError):
        return billing_error_response(exc)
    if isinstance(exc, SignupError):
        from metis_gateway.signup import signup_error_response

        return signup_error_response(exc)
    return None


__all__ = [
    "billing_cancel_handler",
    "billing_error_response",
    "billing_or_signup_error_response",
    "billing_pause_handler",
    "billing_payment_method_handler",
    "billing_resume_handler",
    "billing_status_handler",
    "billing_subscribe_handler",
    "stripe_webhook_handler",
]
