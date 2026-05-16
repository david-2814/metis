"""End-to-end happy-path tests for the Wave-15 billing subscription flow.

Each test drives the gateway HTTP surface (signed-up account → POST
/account/billing/subscribe → state assertions) against the
`FakeBillingClient` substrate. The FakeBillingClient records every
call so we can assert on the Stripe-side intent without standing up
a real Stripe sandbox.
"""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_subscribe_pro_creates_customer_and_subscription(
    billing_client_http,
    signed_up_account,
    fake_billing_client,
):
    """A signed-up account can subscribe to Pro and get a Stripe customer + sub."""
    resp = await billing_client_http.post(
        "/account/billing/subscribe",
        headers={"Authorization": f"Bearer {signed_up_account['session_token']}"},
        json={"seats": 3, "enterprise_addon": False},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["account_id"] == signed_up_account["account_id"]
    assert body["tier"] == "pro"
    assert body["pro_seats"] == 3
    assert body["enterprise_addon"] is False
    assert body["subscription_id"].startswith("sub_")
    assert body["status"] == "active"

    # FakeBillingClient should have logged the two Stripe API calls.
    call_names = [c["name"] for c in fake_billing_client.calls]
    assert "create_customer" in call_names
    assert "create_subscription" in call_names


@pytest.mark.asyncio
async def test_subscribe_with_enterprise_addon_attaches_metered_item(
    billing_client_http,
    signed_up_account,
    fake_billing_client,
):
    resp = await billing_client_http.post(
        "/account/billing/subscribe",
        headers={"Authorization": f"Bearer {signed_up_account['session_token']}"},
        json={"seats": 5, "enterprise_addon": True},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["tier"] == "enterprise"
    assert body["enterprise_addon"] is True
    # The fake client received both items in the subscription.
    create_calls = [c for c in fake_billing_client.calls if c["name"] == "create_subscription"]
    assert len(create_calls) == 1
    assert create_calls[0]["enterprise_metered_price_id"] == "price_test_enterprise_metered"


@pytest.mark.asyncio
async def test_subscribe_with_payment_method_attaches_first(
    billing_client_http,
    signed_up_account,
    fake_billing_client,
):
    resp = await billing_client_http.post(
        "/account/billing/subscribe",
        headers={"Authorization": f"Bearer {signed_up_account['session_token']}"},
        json={"seats": 2, "payment_method_id": "pm_test_xyz"},
    )
    assert resp.status_code == 201, resp.text
    call_names = [c["name"] for c in fake_billing_client.calls]
    # Must attach PM BEFORE creating the subscription.
    assert call_names.index("attach_payment_method") < call_names.index("create_subscription")


@pytest.mark.asyncio
async def test_subscribe_twice_is_rejected_as_conflict(
    billing_client_http,
    signed_up_account,
):
    first = await billing_client_http.post(
        "/account/billing/subscribe",
        headers={"Authorization": f"Bearer {signed_up_account['session_token']}"},
        json={"seats": 1},
    )
    assert first.status_code == 201
    second = await billing_client_http.post(
        "/account/billing/subscribe",
        headers={"Authorization": f"Bearer {signed_up_account['session_token']}"},
        json={"seats": 2},
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "subscription_exists"


@pytest.mark.asyncio
async def test_billing_status_summarizes_subscription(
    billing_client_http,
    signed_up_account,
):
    # Before subscribing: free tier with no subscription.
    resp_before = await billing_client_http.get(
        "/account/billing",
        headers={"Authorization": f"Bearer {signed_up_account['session_token']}"},
    )
    assert resp_before.status_code == 200
    body_before = resp_before.json()
    assert body_before["tier"] == "free"
    assert body_before["stripe_subscription_id"] is None

    # Subscribe → status reflects pro.
    await billing_client_http.post(
        "/account/billing/subscribe",
        headers={"Authorization": f"Bearer {signed_up_account['session_token']}"},
        json={"seats": 2},
    )
    resp_after = await billing_client_http.get(
        "/account/billing",
        headers={"Authorization": f"Bearer {signed_up_account['session_token']}"},
    )
    assert resp_after.status_code == 200
    body_after = resp_after.json()
    assert body_after["tier"] == "pro"
    assert body_after["pro_seats"] == 2
    assert body_after["status"] == "active"


@pytest.mark.asyncio
async def test_cancel_at_period_end_preserves_access(
    billing_client_http,
    signed_up_account,
):
    await billing_client_http.post(
        "/account/billing/subscribe",
        headers={"Authorization": f"Bearer {signed_up_account['session_token']}"},
        json={"seats": 1},
    )
    resp = await billing_client_http.post(
        "/account/billing/cancel",
        headers={"Authorization": f"Bearer {signed_up_account['session_token']}"},
        json={"at_period_end": True},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["cancel_at_period_end"] is True
    # Still active until the period ends.
    assert body["status"] == "active"


@pytest.mark.asyncio
async def test_cancel_immediate_drops_to_free(
    billing_client_http,
    signed_up_account,
):
    await billing_client_http.post(
        "/account/billing/subscribe",
        headers={"Authorization": f"Bearer {signed_up_account['session_token']}"},
        json={"seats": 1},
    )
    resp = await billing_client_http.post(
        "/account/billing/cancel",
        headers={"Authorization": f"Bearer {signed_up_account['session_token']}"},
        json={"at_period_end": False},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "canceled"
    # Status now shows free.
    status = await billing_client_http.get(
        "/account/billing",
        headers={"Authorization": f"Bearer {signed_up_account['session_token']}"},
    )
    assert status.json()["tier"] == "free"


@pytest.mark.asyncio
async def test_pause_and_resume_round_trip(
    billing_client_http,
    signed_up_account,
):
    await billing_client_http.post(
        "/account/billing/subscribe",
        headers={"Authorization": f"Bearer {signed_up_account['session_token']}"},
        json={"seats": 1},
    )
    pause = await billing_client_http.post(
        "/account/billing/pause",
        headers={"Authorization": f"Bearer {signed_up_account['session_token']}"},
    )
    assert pause.status_code == 200
    assert pause.json()["pause_collection"] is True
    assert pause.json()["status"] == "paused"

    resume = await billing_client_http.post(
        "/account/billing/resume",
        headers={"Authorization": f"Bearer {signed_up_account['session_token']}"},
    )
    assert resume.status_code == 200
    assert resume.json()["pause_collection"] is False
    assert resume.json()["status"] == "active"


@pytest.mark.asyncio
async def test_update_payment_method_requires_existing_customer(
    billing_client_http,
    signed_up_account,
):
    """Updating the PM before subscribing fails — no Stripe customer yet."""
    resp = await billing_client_http.post(
        "/account/billing/payment-method",
        headers={"Authorization": f"Bearer {signed_up_account['session_token']}"},
        json={"payment_method_id": "pm_test_zzz"},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "no_customer"


@pytest.mark.asyncio
async def test_billing_endpoints_require_session(billing_client_http):
    """No session → 401 invalid_session."""
    resp = await billing_client_http.get("/account/billing")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_session"


@pytest.mark.asyncio
async def test_invalid_seats_rejected(billing_client_http, signed_up_account):
    resp = await billing_client_http.post(
        "/account/billing/subscribe",
        headers={"Authorization": f"Bearer {signed_up_account['session_token']}"},
        json={"seats": 0},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_seats"


@pytest.mark.asyncio
async def test_subscribe_emits_audit_events(
    billing_client_http,
    signed_up_account,
    runtime,
):
    """A successful subscribe emits billing.customer_created + billing.subscription_created."""
    await billing_client_http.post(
        "/account/billing/subscribe",
        headers={"Authorization": f"Bearer {signed_up_account['session_token']}"},
        json={"seats": 2},
    )
    await runtime.bus.drain()
    assert runtime.trace.count_by_type("billing.customer_created") >= 1
    assert runtime.trace.count_by_type("billing.subscription_created") >= 1
