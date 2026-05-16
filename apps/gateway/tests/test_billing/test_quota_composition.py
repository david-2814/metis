"""Wave 15 — Free-tier $5/mo cap composes with the existing per-key quotas.

The tier cap aggregates spend across every key under the account so a
buyer can't slide past the cap by issuing more keys. These tests
seed the trace DB with `llm.call_completed` events at varying spend,
then drive a request through the gateway and assert the verdict.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from metis_core.events.envelope import Actor
from metis_core.events.payloads import LLMCallCompleted, make_event
from metis_gateway.billing import BillingStore
from metis_gateway.billing.store import CustomerRecord


def _seed_cost_event(runtime, *, gateway_key_id: str, cost_usd: float) -> None:
    """Emit an `llm.call_completed` event with the given cost stamped on it."""
    payload = LLMCallCompleted(
        model="anthropic:claude-haiku-4-5",
        provider="anthropic",
        input_tokens=10,
        output_tokens=10,
        cached_input_tokens=0,
        cache_creation_input_tokens=0,
        cost_usd=cost_usd,
        pricing_version="test",
        latency_ms=100,
        stop_reason="end_turn",
        produced_tool_calls=0,
        produced_thinking_blocks=0,
        gateway_key_id=gateway_key_id,
        inbound_shape="anthropic",
    )
    runtime.bus.emit(
        make_event(
            type="llm.call_completed",
            session_id="gw_test_session",
            actor=Actor.SYSTEM,
            payload=payload,
            timestamp=datetime.now(UTC),
        )
    )


async def _drain(runtime) -> None:
    await runtime.bus.drain()


@pytest.mark.asyncio
async def test_free_tier_under_cap_allows_request(
    billing_client_http,
    signed_up_account,
    runtime,
    scripted_adapter,
):
    """Free-tier account with $2 of spend (under $5 cap) succeeds."""
    _seed_cost_event(runtime, gateway_key_id=signed_up_account["key_id"], cost_usd=2.0)
    await _drain(runtime)
    scripted_adapter.push_response("ok")
    resp = await billing_client_http.post(
        "/v1/messages",
        headers={"x-api-key": signed_up_account["key_token"]},
        json={
            "model": "anthropic:claude-haiku-4-5",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 10,
        },
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_free_tier_at_cap_blocks_with_429(
    billing_client_http,
    signed_up_account,
    runtime,
):
    """Free-tier account whose aggregate spend hits the $5 cap is blocked."""
    _seed_cost_event(runtime, gateway_key_id=signed_up_account["key_id"], cost_usd=5.0)
    await _drain(runtime)
    resp = await billing_client_http.post(
        "/v1/messages",
        headers={"x-api-key": signed_up_account["key_token"]},
        json={
            "model": "anthropic:claude-haiku-4-5",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 10,
        },
    )
    assert resp.status_code == 429
    body = resp.json()
    assert body["error"]["code"] == "quota_exceeded"
    assert Decimal(body["error"]["limit_usd"]) == Decimal("5.00")


@pytest.mark.asyncio
async def test_free_tier_aggregates_across_keys(
    billing_client_http,
    signed_up_account,
    runtime,
    signup_config,
):
    """Two keys under one account can't slide past the cap by splitting spend."""
    from apps.gateway.tests.test_billing.conftest import _merge_keystore_from_disk

    # Issue a second key under the same account.
    second = await billing_client_http.post(
        "/account/keys",
        headers={"Authorization": f"Bearer {signed_up_account['session_token']}"},
        json={"name": "second"},
    )
    assert second.status_code == 201
    second_key_id = second.json()["key_id"]
    second_token = second.json()["token"]
    _merge_keystore_from_disk(runtime, signup_config.resolved_keystore_path())

    # Spread $4 across two keys = $8 total — over the $5 cap.
    _seed_cost_event(runtime, gateway_key_id=signed_up_account["key_id"], cost_usd=4.0)
    _seed_cost_event(runtime, gateway_key_id=second_key_id, cost_usd=4.0)
    await _drain(runtime)

    resp = await billing_client_http.post(
        "/v1/messages",
        headers={"x-api-key": second_token},
        json={
            "model": "anthropic:claude-haiku-4-5",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 10,
        },
    )
    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "quota_exceeded"


@pytest.mark.asyncio
async def test_pro_tier_unlimited_at_tier_layer(
    billing_client_http,
    signed_up_account,
    runtime,
    billing_config,
    scripted_adapter,
):
    """Pro-tier accounts skip the tier cap entirely."""
    # Upgrade the account to Pro.
    store = BillingStore(billing_config.resolved_store_path())
    try:
        store.upsert_customer(
            CustomerRecord(
                account_id=signed_up_account["account_id"],
                stripe_customer_id="cus_test_pro",
                tier="pro",
                email_sha256="x" * 64,
                created_at=datetime.now(UTC),
            )
        )
    finally:
        store.close()

    # $10 spend, well over the $5 free cap, but on Pro should pass.
    _seed_cost_event(runtime, gateway_key_id=signed_up_account["key_id"], cost_usd=10.0)
    await _drain(runtime)
    scripted_adapter.push_response("ok")

    resp = await billing_client_http.post(
        "/v1/messages",
        headers={"x-api-key": signed_up_account["key_token"]},
        json={
            "model": "anthropic:claude-haiku-4-5",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 10,
        },
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_tier_cap_emits_quota_exceeded_event(
    billing_client_http,
    signed_up_account,
    runtime,
):
    _seed_cost_event(runtime, gateway_key_id=signed_up_account["key_id"], cost_usd=5.5)
    await _drain(runtime)
    await billing_client_http.post(
        "/v1/messages",
        headers={"x-api-key": signed_up_account["key_token"]},
        json={
            "model": "anthropic:claude-haiku-4-5",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 10,
        },
    )
    await _drain(runtime)
    assert runtime.trace.count_by_type("gateway.quota_exceeded") >= 1
