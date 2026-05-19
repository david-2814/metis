"""HTTP-level tests for the gateway's `GET /metrics` endpoint.

End-to-end: hit the route via httpx ASGITransport, parse the body
back through prometheus_client, and assert the bounded metric
families show up. Synthetic events emitted on the bus drive the
counter samples.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from metis.core.events.envelope import Actor
from metis.core.events.payloads import (
    GatewayKeyRevoked,
    LLMCallCompleted,
    QuotaAlert,
    make_event,
)
from metis.gateway.app import build_app
from metis.gateway.auth import GatewayKey, Keystore
from prometheus_client.parser import text_string_to_metric_families


@pytest.fixture
async def client(runtime):
    app = build_app(runtime)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


def _families(text: str) -> dict[str, list]:
    return {f.name: list(f.samples) for f in text_string_to_metric_families(text)}


async def test_metrics_endpoint_returns_prometheus_text(client) -> None:
    r = await client.get("/metrics")
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]
    families = _families(r.text)
    # Bounded family set per observability.md §3.
    assert "metis_llm_calls" in families
    assert "metis_routing_decisions" in families
    assert "metis_gateway_keys_active" in families


async def test_metrics_endpoint_does_not_require_auth(client) -> None:
    """`/metrics` matches `/healthz` — loopback bind is the security model."""
    r = await client.get("/metrics")
    assert r.status_code == 200


async def test_llm_completed_event_propagates_to_metrics(client, runtime) -> None:
    runtime.bus.emit(
        make_event(
            type="llm.call_completed",
            session_id="sess_metrics_001",
            actor=Actor.AGENT,
            timestamp=datetime.now(UTC),
            payload=LLMCallCompleted(
                model="anthropic:claude-haiku-4-5",
                provider="anthropic",
                input_tokens=200,
                output_tokens=80,
                cached_input_tokens=0,
                cache_creation_input_tokens=0,
                cost_usd=0.05,
                pricing_version="v1",
                latency_ms=420,
                stop_reason="end_turn",
                produced_tool_calls=0,
                produced_thinking_blocks=0,
            ),
        )
    )
    await runtime.bus.drain()

    r = await client.get("/metrics")
    assert r.status_code == 200
    families = _families(r.text)
    calls = [s for s in families["metis_llm_calls"] if s.name == "metis_llm_calls_total"]
    assert any(s.labels["model"] == "anthropic:claude-haiku-4-5" for s in calls)


async def test_gateway_keys_gauges_reflect_keystore(runtime) -> None:
    """Add a revoked key to the keystore; the gauge picks it up on scrape."""
    revoked_at = datetime(2026, 5, 15, tzinfo=UTC)
    revoked = GatewayKey(
        key_id="gk_revoked_for_metrics",
        secret_hash=hashlib.sha256(b"unused").hexdigest(),
        name="metric-test",
        workspace_path=runtime.keystore.keys()[0].workspace_path,
        status="revoked",
        revoked_at=revoked_at,
    )
    new_store = Keystore([*runtime.keystore.keys(), revoked])
    runtime.keystore = new_store

    app = build_app(runtime)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        r = await c.get("/metrics")
    families = _families(r.text)
    active = next(
        s for s in families["metis_gateway_keys_active"] if s.name == "metis_gateway_keys_active"
    )
    revoked_sample = next(
        s for s in families["metis_gateway_keys_revoked"] if s.name == "metis_gateway_keys_revoked"
    )
    assert active.value == 1.0
    assert revoked_sample.value == 1.0


async def test_quota_alert_event_drives_used_ratio_gauge(client, runtime) -> None:
    runtime.bus.emit(
        make_event(
            type="quota.alert",
            session_id="sess_metrics_quota",
            actor=Actor.SYSTEM,
            timestamp=datetime.now(UTC),
            payload=QuotaAlert(
                scope="key_monthly",
                severity="critical",
                current_usd=Decimal("95.0"),
                limit_usd=Decimal("100.0"),
                percentage=0.95,
                gateway_key_id="gk_test_001",
            ),
        )
    )
    await runtime.bus.drain()

    r = await client.get("/metrics")
    families = _families(r.text)
    gauges = [s for s in families["metis_quota_used_ratio"] if s.name == "metis_quota_used_ratio"]
    matching = [s for s in gauges if s.labels.get("identity_id") == "gk_test_001"]
    assert len(matching) == 1
    assert matching[0].value == pytest.approx(0.95)


async def test_audit_event_does_not_perturb_metrics(client, runtime) -> None:
    """Events outside the observed set leave the metrics untouched."""
    runtime.bus.emit(
        make_event(
            type="gateway.key_revoked",
            session_id="sess_unrelated",
            actor=Actor.SYSTEM,
            timestamp=datetime.now(UTC),
            payload=GatewayKeyRevoked(
                gateway_key_id="gk_audit",
                revoked_at=datetime.now(UTC),
                reason="admin_revoke",
            ),
        )
    )
    await runtime.bus.drain()

    r = await client.get("/metrics")
    families = _families(r.text)
    # Sanity: the families exist; none of them grew an "audit" series.
    for fam_name, samples in families.items():
        for s in samples:
            assert "gk_audit" not in s.labels.values() or fam_name != "metis_llm_calls"


async def test_missing_auth_bumps_gateway_auth_failures_metric(client, runtime) -> None:
    """A request with no Authorization header emits `gateway.auth_failed`
    with `reason=missing_token`, which the metrics collector projects onto
    `metis_gateway_auth_failures_total{reason="missing_token"}`.
    """
    r = await client.post(
        "/v1/chat/completions",
        json={"model": "haiku", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 401
    await runtime.bus.drain()

    families = _families((await client.get("/metrics")).text)
    samples = [
        s
        for s in families["metis_gateway_auth_failures"]
        if s.name == "metis_gateway_auth_failures_total"
    ]
    matching = [s for s in samples if s.labels.get("reason") == "missing_token"]
    assert matching, "expected a missing_token row in the auth-failures counter"
    assert matching[0].value >= 1.0


async def test_invalid_bearer_bumps_gateway_auth_failures_metric(client, runtime) -> None:
    """A wrong bearer token produces `reason=invalid_token` (the token
    was offered but didn't match any active key).
    """
    r = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer gw_not_a_real_key"},
        json={"model": "haiku", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 401
    await runtime.bus.drain()

    families = _families((await client.get("/metrics")).text)
    samples = [
        s
        for s in families["metis_gateway_auth_failures"]
        if s.name == "metis_gateway_auth_failures_total"
    ]
    matching = [s for s in samples if s.labels.get("reason") == "invalid_token"]
    assert matching, "expected an invalid_token row in the auth-failures counter"
    assert matching[0].value >= 1.0


async def test_revoked_key_bumps_gateway_auth_failures_metric(
    revoked_client, revoked_bearer_token, revoked_runtime
) -> None:
    """A revoked-key request produces `reason=key_revoked` and includes
    the matched `gateway_key_id` on the event payload (so the audit
    trail can correlate to the previously-issued key).
    """
    r = await revoked_client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {revoked_bearer_token}"},
        json={"model": "haiku", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 401
    await revoked_runtime.bus.drain()

    r2 = await revoked_client.get("/metrics")
    families = _families(r2.text)
    samples = [
        s
        for s in families["metis_gateway_auth_failures"]
        if s.name == "metis_gateway_auth_failures_total"
    ]
    matching = [s for s in samples if s.labels.get("reason") == "key_revoked"]
    assert matching, "expected a key_revoked row in the auth-failures counter"
    assert matching[0].value >= 1.0
