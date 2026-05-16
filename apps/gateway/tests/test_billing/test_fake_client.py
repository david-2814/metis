"""Tests for FakeBillingClient — the test substrate itself.

We test the fake separately from the service so we can trust it as a
truth-source for the service tests. Webhook signature verification is
checked here too since it's the only crypto in the module.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from metis_gateway.billing import (
    BillingClientError,
    FakeBillingClient,
    WebhookSignatureError,
)


def test_create_customer_assigns_unique_ids() -> None:
    fake = FakeBillingClient()
    a = fake.create_customer(email="a@a", metadata={})
    b = fake.create_customer(email="b@b", metadata={})
    assert a.id != b.id


def test_attach_payment_method_requires_customer() -> None:
    fake = FakeBillingClient()
    with pytest.raises(BillingClientError):
        fake.attach_payment_method(customer_id="cus_missing", payment_method_id="pm_x")


def test_create_subscription_rejects_zero_seats() -> None:
    fake = FakeBillingClient()
    cust = fake.create_customer(email="a@a", metadata={})
    with pytest.raises(BillingClientError):
        fake.create_subscription(
            customer_id=cust.id,
            pro_price_id="price_pro",
            pro_seats=0,
            enterprise_metered_price_id=None,
            metadata={},
        )


def test_create_subscription_with_enterprise_attaches_metered_item() -> None:
    fake = FakeBillingClient()
    cust = fake.create_customer(email="a@a", metadata={})
    sub = fake.create_subscription(
        customer_id=cust.id,
        pro_price_id="price_pro",
        pro_seats=2,
        enterprise_metered_price_id="price_meter",
        metadata={},
    )
    items = list(sub.items)
    assert len(items) == 2
    assert any(i.metered for i in items)
    assert all(i.quantity == 2 or i.metered for i in items)


def test_record_metered_usage_is_idempotent() -> None:
    fake = FakeBillingClient()
    cust = fake.create_customer(email="a@a", metadata={})
    sub = fake.create_subscription(
        customer_id=cust.id,
        pro_price_id="price_pro",
        pro_seats=1,
        enterprise_metered_price_id="price_meter",
        metadata={},
    )
    metered_item = next(i for i in sub.items if i.metered)
    when = datetime.now(UTC)
    fake.record_metered_usage(
        subscription_item_id=metered_item.id,
        quantity=100,
        timestamp=when,
        idempotency_key="abc",
    )
    fake.record_metered_usage(
        subscription_item_id=metered_item.id,
        quantity=100,
        timestamp=when,
        idempotency_key="abc",
    )
    assert len(fake.metered_usage_records) == 1


def test_cancel_at_period_end_keeps_status_active() -> None:
    fake = FakeBillingClient()
    cust = fake.create_customer(email="a@a", metadata={})
    sub = fake.create_subscription(
        customer_id=cust.id,
        pro_price_id="price_pro",
        pro_seats=1,
        enterprise_metered_price_id=None,
        metadata={},
    )
    updated = fake.cancel_subscription(subscription_id=sub.id, at_period_end=True)
    assert updated.cancel_at_period_end is True
    assert updated.status == "active"


def test_cancel_immediate_flips_status_to_canceled() -> None:
    fake = FakeBillingClient()
    cust = fake.create_customer(email="a@a", metadata={})
    sub = fake.create_subscription(
        customer_id=cust.id,
        pro_price_id="price_pro",
        pro_seats=1,
        enterprise_metered_price_id=None,
        metadata={},
    )
    updated = fake.cancel_subscription(subscription_id=sub.id, at_period_end=False)
    assert updated.status == "canceled"


def test_pause_then_resume_round_trip() -> None:
    fake = FakeBillingClient()
    cust = fake.create_customer(email="a@a", metadata={})
    sub = fake.create_subscription(
        customer_id=cust.id,
        pro_price_id="price_pro",
        pro_seats=1,
        enterprise_metered_price_id=None,
        metadata={},
    )
    paused = fake.pause_subscription(subscription_id=sub.id)
    assert paused.status == "paused"
    assert paused.pause_collection is True
    resumed = fake.resume_subscription(subscription_id=sub.id)
    assert resumed.status == "active"
    assert resumed.pause_collection is False


def test_webhook_signature_verifies_well_formed_payload() -> None:
    fake = FakeBillingClient(webhook_secret="whsec_test_known")
    payload = json.dumps(
        {
            "id": "evt_1",
            "type": "invoice.payment_succeeded",
            "data": {"object": {"id": "in_1", "subscription": "sub_1", "amount_paid": 5000}},
        }
    ).encode()
    sig = fake.sign_payload(payload)
    event = fake.construct_webhook_event(payload=payload, signature=sig)
    assert event.id == "evt_1"
    assert event.kind == "invoice.payment_succeeded"
    assert event.data["amount_paid"] == 5000


def test_webhook_signature_rejects_wrong_secret() -> None:
    fake_a = FakeBillingClient(webhook_secret="whsec_a")
    fake_b = FakeBillingClient(webhook_secret="whsec_b")
    payload = b'{"id": "evt_1", "type": "x"}'
    sig = fake_a.sign_payload(payload)
    with pytest.raises(WebhookSignatureError):
        fake_b.construct_webhook_event(payload=payload, signature=sig)


def test_webhook_signature_rejects_malformed_header() -> None:
    fake = FakeBillingClient()
    with pytest.raises(WebhookSignatureError):
        fake.construct_webhook_event(payload=b"{}", signature="not_a_signature")


def test_update_subscription_seats_changes_quantity() -> None:
    fake = FakeBillingClient()
    cust = fake.create_customer(email="a@a", metadata={})
    sub = fake.create_subscription(
        customer_id=cust.id,
        pro_price_id="price_pro",
        pro_seats=2,
        enterprise_metered_price_id=None,
        metadata={},
    )
    pro_item = next(i for i in sub.items if not i.metered)
    updated = fake.update_subscription_seats(
        subscription_id=sub.id,
        pro_item_id=pro_item.id,
        seats=7,
    )
    updated_pro = next(i for i in updated.items if not i.metered)
    assert updated_pro.quantity == 7


def test_add_and_remove_subscription_item_round_trip() -> None:
    fake = FakeBillingClient()
    cust = fake.create_customer(email="a@a", metadata={})
    sub = fake.create_subscription(
        customer_id=cust.id,
        pro_price_id="price_pro",
        pro_seats=1,
        enterprise_metered_price_id=None,
        metadata={},
    )
    with_item = fake.add_subscription_item(
        subscription_id=sub.id,
        price_id="price_metered",
        metered=True,
    )
    metered = next(i for i in with_item.items if i.metered)
    without_item = fake.remove_subscription_item(
        subscription_id=sub.id,
        subscription_item_id=metered.id,
    )
    assert all(not i.metered for i in without_item.items)


def test_billing_portal_session_requires_customer() -> None:
    fake = FakeBillingClient()
    with pytest.raises(BillingClientError):
        fake.create_billing_portal_session(
            customer_id="cus_missing",
            return_url="http://example.test/account/billing",
        )


def test_billing_portal_session_returns_url() -> None:
    fake = FakeBillingClient()
    cust = fake.create_customer(email="a@a", metadata={})
    url = fake.create_billing_portal_session(
        customer_id=cust.id,
        return_url="http://example.test/account/billing",
    )
    assert url.startswith("https://billing.stripe.test/session/")


def test_record_metered_usage_rejects_negative_quantity() -> None:
    fake = FakeBillingClient()
    cust = fake.create_customer(email="a@a", metadata={})
    sub = fake.create_subscription(
        customer_id=cust.id,
        pro_price_id="price_pro",
        pro_seats=1,
        enterprise_metered_price_id="price_meter",
        metadata={},
    )
    metered_item = next(i for i in sub.items if i.metered)
    with pytest.raises(BillingClientError):
        fake.record_metered_usage(
            subscription_item_id=metered_item.id,
            quantity=-1,
            timestamp=datetime.now(UTC),
            idempotency_key="x",
        )
