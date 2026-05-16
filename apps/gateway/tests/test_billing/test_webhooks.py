"""Stripe webhook tests for the Wave-15 billing module.

Drives `POST /webhooks/stripe` against the `FakeBillingClient`'s
signature scheme. Tests the four event kinds Wave 15 handles end-to-end
plus the signature-verification + idempotency contracts.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

import pytest


def _payloads_of_type(runtime, event_type: str) -> list[dict]:
    cur: sqlite3.Cursor = runtime.trace._conn.execute(
        "SELECT payload_json FROM events WHERE type = ? ORDER BY id",
        (event_type,),
    )
    return [json.loads(row[0]) for row in cur.fetchall()]


def make_stripe_event(
    *,
    kind: str,
    event_id: str,
    object_data: dict,
    livemode: bool = False,
) -> bytes:
    """Build the raw JSON Stripe would POST for a given event."""
    body = {
        "id": event_id,
        "type": kind,
        "livemode": livemode,
        "data": {"object": object_data},
        "created": int(datetime.now(UTC).timestamp()),
    }
    return json.dumps(body).encode("utf-8")


async def _subscribe(billing_client_http, session_token: str) -> dict:
    resp = await billing_client_http.post(
        "/account/billing/subscribe",
        headers={"Authorization": f"Bearer {session_token}"},
        json={"seats": 2},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_webhook_missing_signature_is_rejected(billing_client_http):
    resp = await billing_client_http.post("/webhooks/stripe", content=b"{}")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "missing_signature"


@pytest.mark.asyncio
async def test_webhook_invalid_signature_is_rejected(billing_client_http):
    resp = await billing_client_http.post(
        "/webhooks/stripe",
        content=b'{"id": "evt_1", "type": "invoice.payment_succeeded"}',
        headers={"stripe-signature": "t=123,v1=deadbeef"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "signature_invalid"


@pytest.mark.asyncio
async def test_webhook_valid_signature_with_valid_event_accepted(
    billing_client_http,
    signed_up_account,
    fake_billing_client,
):
    sub_body = await _subscribe(billing_client_http, signed_up_account["session_token"])
    payload = make_stripe_event(
        kind="customer.subscription.updated",
        event_id="evt_test_001",
        object_data={"id": sub_body["subscription_id"], "status": "active"},
    )
    sig = fake_billing_client.sign_payload(payload)
    resp = await billing_client_http.post(
        "/webhooks/stripe",
        content=payload,
        headers={"stripe-signature": sig},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert body["event_id"] == "evt_test_001"


@pytest.mark.asyncio
async def test_webhook_replay_is_idempotent(
    billing_client_http,
    signed_up_account,
    fake_billing_client,
):
    sub_body = await _subscribe(billing_client_http, signed_up_account["session_token"])
    payload = make_stripe_event(
        kind="customer.subscription.updated",
        event_id="evt_replay_001",
        object_data={"id": sub_body["subscription_id"], "status": "active"},
    )
    sig = fake_billing_client.sign_payload(payload)
    first = await billing_client_http.post(
        "/webhooks/stripe", content=payload, headers={"stripe-signature": sig}
    )
    assert first.status_code == 200
    assert first.json()["status"] == "ok"
    second = await billing_client_http.post(
        "/webhooks/stripe", content=payload, headers={"stripe-signature": sig}
    )
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"


@pytest.mark.asyncio
async def test_webhook_subscription_deleted_drops_tier_to_free(
    billing_client_http,
    signed_up_account,
    fake_billing_client,
    runtime,
):
    sub_body = await _subscribe(billing_client_http, signed_up_account["session_token"])
    payload = make_stripe_event(
        kind="customer.subscription.deleted",
        event_id="evt_delete_001",
        object_data={"id": sub_body["subscription_id"]},
    )
    sig = fake_billing_client.sign_payload(payload)
    resp = await billing_client_http.post(
        "/webhooks/stripe", content=payload, headers={"stripe-signature": sig}
    )
    assert resp.status_code == 200
    # Tier drops back to free.
    status = await billing_client_http.get(
        "/account/billing",
        headers={"Authorization": f"Bearer {signed_up_account['session_token']}"},
    )
    assert status.json()["tier"] == "free"
    await runtime.bus.drain()
    assert runtime.trace.count_by_type("billing.subscription_canceled") >= 1


@pytest.mark.asyncio
async def test_webhook_invoice_payment_succeeded_emits_audit_event(
    billing_client_http,
    signed_up_account,
    fake_billing_client,
    runtime,
):
    sub_body = await _subscribe(billing_client_http, signed_up_account["session_token"])
    payload = make_stripe_event(
        kind="invoice.payment_succeeded",
        event_id="evt_invoice_001",
        object_data={
            "id": "in_test_001",
            "subscription": sub_body["subscription_id"],
            "amount_paid": 5000,
        },
    )
    sig = fake_billing_client.sign_payload(payload)
    resp = await billing_client_http.post(
        "/webhooks/stripe", content=payload, headers={"stripe-signature": sig}
    )
    assert resp.status_code == 200
    await runtime.bus.drain()
    paid = _payloads_of_type(runtime, "billing.invoice_paid")
    assert len(paid) == 1
    assert paid[0]["amount_paid_cents"] == 5000
    assert paid[0]["stripe_invoice_id"] == "in_test_001"


@pytest.mark.asyncio
async def test_webhook_invoice_payment_failed_emits_audit_event(
    billing_client_http,
    signed_up_account,
    fake_billing_client,
    runtime,
):
    sub_body = await _subscribe(billing_client_http, signed_up_account["session_token"])
    payload = make_stripe_event(
        kind="invoice.payment_failed",
        event_id="evt_invoice_fail_001",
        object_data={
            "id": "in_test_002",
            "subscription": sub_body["subscription_id"],
            "amount_due": 5000,
            "attempt_count": 2,
        },
    )
    sig = fake_billing_client.sign_payload(payload)
    resp = await billing_client_http.post(
        "/webhooks/stripe", content=payload, headers={"stripe-signature": sig}
    )
    assert resp.status_code == 200
    await runtime.bus.drain()
    failed = _payloads_of_type(runtime, "billing.invoice_payment_failed")
    assert len(failed) == 1
    assert failed[0]["amount_due_cents"] == 5000
    assert failed[0]["attempt_count"] == 2


@pytest.mark.asyncio
async def test_webhook_unhandled_kind_is_logged_and_accepted(
    billing_client_http,
    fake_billing_client,
):
    """Stripe sends many event types we don't care about; we 200 to stop retries."""
    payload = make_stripe_event(
        kind="customer.created",
        event_id="evt_other_001",
        object_data={"id": "cus_other"},
    )
    sig = fake_billing_client.sign_payload(payload)
    resp = await billing_client_http.post(
        "/webhooks/stripe", content=payload, headers={"stripe-signature": sig}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"


@pytest.mark.asyncio
async def test_webhook_unknown_subscription_is_no_op(
    billing_client_http,
    fake_billing_client,
):
    """A webhook for a subscription we don't have a record of is silently dropped."""
    payload = make_stripe_event(
        kind="customer.subscription.updated",
        event_id="evt_unknown_001",
        object_data={"id": "sub_unknown_999", "status": "active"},
    )
    sig = fake_billing_client.sign_payload(payload)
    resp = await billing_client_http.post(
        "/webhooks/stripe", content=payload, headers={"stripe-signature": sig}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_webhook_processed_events_persist_across_app_lifecycle(
    billing_client_http,
    signed_up_account,
    fake_billing_client,
    billing_config,
):
    """A duplicate event sent across a hypothetical restart still de-dupes
    (the processed-events table is on disk)."""
    sub_body = await _subscribe(billing_client_http, signed_up_account["session_token"])
    payload = make_stripe_event(
        kind="invoice.payment_succeeded",
        event_id="evt_persist_001",
        object_data={
            "id": "in_persist",
            "subscription": sub_body["subscription_id"],
            "amount_paid": 1000,
        },
    )
    sig = fake_billing_client.sign_payload(payload)
    first = await billing_client_http.post(
        "/webhooks/stripe", content=payload, headers={"stripe-signature": sig}
    )
    assert first.json()["status"] == "ok"

    # Open a fresh BillingStore connection — simulating a restart — and
    # verify the event id is still marked processed.
    from metis_gateway.billing import BillingStore

    fresh = BillingStore(billing_config.resolved_store_path())
    try:
        assert fresh.has_processed("evt_persist_001") is True
    finally:
        fresh.close()


@pytest.mark.asyncio
async def test_webhook_subscription_status_change_emits_updated_event(
    billing_client_http,
    signed_up_account,
    fake_billing_client,
    runtime,
):
    sub_body = await _subscribe(billing_client_http, signed_up_account["session_token"])
    # Stripe's webhook fires after they flip status; our handler refreshes
    # via get_subscription which still returns 'active' — we are testing
    # that an event is emitted, not the specific status transition.
    payload = make_stripe_event(
        kind="customer.subscription.updated",
        event_id="evt_status_001",
        object_data={"id": sub_body["subscription_id"], "status": "past_due"},
    )
    sig = fake_billing_client.sign_payload(payload)
    resp = await billing_client_http.post(
        "/webhooks/stripe", content=payload, headers={"stripe-signature": sig}
    )
    assert resp.status_code == 200
    await runtime.bus.drain()
    updated = _payloads_of_type(runtime, "billing.subscription_updated")
    # The fixture's create_subscription path also emits subscription_created
    # but not subscription_updated; this webhook is the first source of
    # subscription_updated.
    assert len(updated) == 1
