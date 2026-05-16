"""Wave 16 billing self-service UX tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from metis_gateway.billing import BillingStore, SubscriptionRecord

from apps.gateway.tests.test_billing.conftest import make_stripe_event


def _auth(account: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {account['session_token']}"}


async def _post_plan(client, account: dict, body: dict) -> dict:
    resp = await client.post("/account/billing/plan", headers=_auth(account), json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_status_for_new_signup_exposes_free_tier_cap(
    billing_client_http,
    signed_up_account,
):
    resp = await billing_client_http.get("/account/billing", headers=_auth(signed_up_account))
    assert resp.status_code == 200
    body = resp.json()
    assert body["tier"] == "free"
    assert body["free_tier_cap"]["monthly_cap_usd"] == "5.00"
    assert body["free_tier_cap"]["daily_cap_usd"] is None


@pytest.mark.asyncio
async def test_billing_portal_creates_customer_and_returns_link(
    billing_client_http,
    signed_up_account,
    fake_billing_client,
):
    resp = await billing_client_http.get(
        "/account/billing/portal",
        headers=_auth(signed_up_account),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["account_id"] == signed_up_account["account_id"]
    assert body["url"].startswith("https://billing.stripe.test/session/")
    assert body["return_url"] == "http://127.0.0.1:8422/account/billing"
    assert [c["name"] for c in fake_billing_client.calls] == [
        "create_customer",
        "create_billing_portal_session",
    ]


@pytest.mark.asyncio
async def test_plan_endpoint_changes_free_pro_enterprise_and_emits_audit_events(
    billing_client_http,
    signed_up_account,
    fake_billing_client,
    runtime,
):
    pro = await _post_plan(
        billing_client_http,
        signed_up_account,
        {"plan": "pro", "seats": 2, "payment_method_id": "pm_test_first"},
    )
    assert pro["tier"] == "pro"
    assert pro["pro_seats"] == 2
    assert pro["enterprise_addon"] is False

    enterprise = await _post_plan(
        billing_client_http,
        signed_up_account,
        {"plan": "enterprise", "seats": 4},
    )
    assert enterprise["tier"] == "enterprise"
    assert enterprise["subscription_tier"] == "enterprise"
    assert enterprise["pro_seats"] == 4
    assert enterprise["enterprise_addon"] is True

    back_to_pro = await _post_plan(
        billing_client_http,
        signed_up_account,
        {"plan": "pro"},
    )
    assert back_to_pro["tier"] == "pro"
    assert back_to_pro["enterprise_addon"] is False

    free = await _post_plan(
        billing_client_http,
        signed_up_account,
        {"plan": "free"},
    )
    assert free["tier"] == "free"
    assert free["status"] == "canceled"
    assert free["free_tier_cap"]["monthly_cap_usd"] == "5.00"

    call_names = [c["name"] for c in fake_billing_client.calls]
    assert "update_subscription_seats" in call_names
    assert "add_subscription_item" in call_names
    assert "remove_subscription_item" in call_names
    assert "cancel_subscription" in call_names
    await runtime.bus.drain()
    assert runtime.trace.count_by_type("billing.subscription_created") == 1
    assert runtime.trace.count_by_type("billing.subscription_updated") >= 2
    assert runtime.trace.count_by_type("billing.subscription_canceled") == 1


@pytest.mark.asyncio
async def test_plan_endpoint_rejects_invalid_plan(
    billing_client_http,
    signed_up_account,
):
    resp = await billing_client_http.post(
        "/account/billing/plan",
        headers=_auth(signed_up_account),
        json={"plan": "team"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_plan"


@pytest.mark.asyncio
async def test_failed_payment_gets_grace_then_freezes_to_free_tier(
    billing_client_http,
    signed_up_account,
    fake_billing_client,
    billing_config,
    runtime,
):
    pro = await _post_plan(
        billing_client_http,
        signed_up_account,
        {"plan": "pro", "seats": 2},
    )
    payload = make_stripe_event(
        kind="invoice.payment_failed",
        event_id="evt_self_service_failed",
        object_data={
            "id": "in_failed_self_service",
            "subscription": pro["stripe_subscription_id"],
            "amount_due": 5000,
            "attempt_count": 1,
        },
    )
    resp = await billing_client_http.post(
        "/webhooks/stripe",
        content=payload,
        headers={"stripe-signature": fake_billing_client.sign_payload(payload)},
    )
    assert resp.status_code == 200, resp.text

    grace = await billing_client_http.get("/account/billing", headers=_auth(signed_up_account))
    assert grace.status_code == 200
    grace_body = grace.json()
    assert grace_body["tier"] == "pro"
    assert grace_body["payment_state"] == "grace"
    assert grace_body["payment_grace_until"] is not None

    store = BillingStore(billing_config.resolved_store_path())
    try:
        record = store.get_subscription(signed_up_account["account_id"])
        assert record is not None
        store.upsert_subscription(
            SubscriptionRecord(
                account_id=record.account_id,
                stripe_subscription_id=record.stripe_subscription_id,
                tier=record.tier,
                status=record.status,
                pro_seats=record.pro_seats,
                pro_item_id=record.pro_item_id,
                enterprise_metered_item_id=record.enterprise_metered_item_id,
                current_period_end=record.current_period_end,
                cancel_at_period_end=record.cancel_at_period_end,
                pause_collection=record.pause_collection,
                created_at=record.created_at,
                updated_at=datetime.now(UTC),
                payment_failed_at=record.payment_failed_at,
                payment_grace_until=datetime.now(UTC) - timedelta(seconds=1),
                access_frozen_at=None,
            )
        )
    finally:
        store.close()

    frozen = await billing_client_http.get("/account/billing", headers=_auth(signed_up_account))
    assert frozen.status_code == 200
    frozen_body = frozen.json()
    assert frozen_body["tier"] == "free"
    assert frozen_body["subscription_tier"] == "pro"
    assert frozen_body["payment_state"] == "frozen"
    assert frozen_body["status"] == "unpaid"

    paid_payload = make_stripe_event(
        kind="invoice.payment_succeeded",
        event_id="evt_self_service_paid",
        object_data={
            "id": "in_paid_self_service",
            "subscription": pro["stripe_subscription_id"],
            "amount_paid": 5000,
        },
    )
    paid = await billing_client_http.post(
        "/webhooks/stripe",
        content=paid_payload,
        headers={"stripe-signature": fake_billing_client.sign_payload(paid_payload)},
    )
    assert paid.status_code == 200, paid.text
    restored = await billing_client_http.get("/account/billing", headers=_auth(signed_up_account))
    restored_body = restored.json()
    assert restored_body["tier"] == "pro"
    assert restored_body["payment_state"] == "current"
    assert restored_body["status"] == "active"

    await runtime.bus.drain()
    assert runtime.trace.count_by_type("billing.invoice_payment_failed") == 1
    assert runtime.trace.count_by_type("billing.invoice_paid") == 1
    assert runtime.trace.count_by_type("billing.subscription_updated") >= 1
