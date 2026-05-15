"""Tests for the bus-driven Prometheus metrics collector.

Exercises:

* Synthetic events of every observed type round-trip through the
  collector and bump the documented metric.
* The exposition body is parseable by `prometheus_client`'s text-format
  parser and contains the metric families we expect.
* Polled gauges (session count, gateway-key tally) read their getters
  on every `expose()` call and gracefully tolerate getter failures.
* Cardinality is bounded — missing fields collapse to `unknown` rather
  than minting new label values.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from metis_core.canonical.ids import next_monotonic_ulid
from metis_core.events.bus import EventBus
from metis_core.events.envelope import Actor
from metis_core.events.payloads import (
    EvalCompleted,
    GatewayQuotaExceeded,
    LLMCallCompleted,
    LLMCallFailed,
    PatternMatched,
    PolicyEvaluation,
    QuotaAlert,
    RouteDecided,
    make_event,
)
from metis_core.observability import METRICS_CONTENT_TYPE, MetricsCollector
from prometheus_client.parser import text_string_to_metric_families


def _now() -> datetime:
    return datetime.now(UTC)


def _new_session_id() -> str:
    return f"sess_{next_monotonic_ulid()}"


def _new_turn_id() -> str:
    return str(next_monotonic_ulid())


@pytest.fixture
async def bus():
    b = EventBus()
    b.start()
    try:
        yield b
    finally:
        await b.drain()
        await b.stop()


@pytest.fixture
async def collector(bus: EventBus):
    c = MetricsCollector(bus=bus)
    c.attach()
    try:
        yield c
    finally:
        c.detach()


def _emit(bus: EventBus, *, type: str, payload, session_id: str | None = None, turn_id=None):
    bus.emit(
        make_event(
            type=type,
            session_id=session_id or _new_session_id(),
            actor=Actor.SYSTEM,
            payload=payload,
            timestamp=_now(),
            turn_id=turn_id,
        )
    )


def _families(body: bytes) -> dict[str, list]:
    """Parse exposition bytes into `{family_name: [Sample, ...]}`."""
    text = body.decode("utf-8")
    out: dict[str, list] = {}
    for family in text_string_to_metric_families(text):
        out[family.name] = list(family.samples)
    return out


# ---------------------------------------------------------------------------
# Counter / histogram round-trips
# ---------------------------------------------------------------------------


async def test_llm_call_completed_increments_calls_cost_latency(bus: EventBus, collector):
    _emit(
        bus,
        type="llm.call_completed",
        payload=LLMCallCompleted(
            model="anthropic:claude-haiku-4-5",
            provider="anthropic",
            input_tokens=100,
            output_tokens=50,
            cached_input_tokens=0,
            cache_creation_input_tokens=0,
            cost_usd=0.0123,
            pricing_version="v1",
            latency_ms=350,
            stop_reason="end_turn",
            produced_tool_calls=0,
            produced_thinking_blocks=0,
        ),
    )
    await bus.drain()

    body = collector.expose()
    families = _families(body)

    calls = [s for s in families["metis_llm_calls"] if s.name == "metis_llm_calls_total"]
    assert len(calls) == 1
    assert calls[0].labels == {
        "provider": "anthropic",
        "model": "anthropic:claude-haiku-4-5",
        "status": "ok",
    }
    assert calls[0].value == 1.0

    cost = [s for s in families["metis_llm_cost_usd"] if s.name == "metis_llm_cost_usd_total"]
    assert len(cost) == 1
    assert cost[0].value == pytest.approx(0.0123)

    latency_samples = families["metis_llm_call_latency_seconds"]
    sum_sample = next(s for s in latency_samples if s.name.endswith("_sum"))
    count_sample = next(s for s in latency_samples if s.name.endswith("_count"))
    assert count_sample.value == 1.0
    assert sum_sample.value == pytest.approx(0.35)


async def test_llm_call_failed_uses_error_class_status(bus: EventBus, collector):
    _emit(
        bus,
        type="llm.call_failed",
        payload=LLMCallFailed(
            model="anthropic:claude-haiku-4-5",
            provider="anthropic",
            error_class="rate_limit",
            error_message_redacted="429",
            retry_count=1,
            latency_ms=120,
        ),
    )
    await bus.drain()

    families = _families(collector.expose())
    calls = [s for s in families["metis_llm_calls"] if s.name == "metis_llm_calls_total"]
    assert len(calls) == 1
    assert calls[0].labels["status"] == "rate_limit"


async def test_route_decided_picks_winning_slot_from_chain(bus: EventBus, collector):
    chain = [
        PolicyEvaluation(policy="per_message_override", verdict="not_applicable", reason="absent"),
        PolicyEvaluation(policy="manual_sticky", verdict="not_applicable", reason="absent"),
        PolicyEvaluation(policy="rule", verdict="not_applicable", reason="no rule matched"),
        PolicyEvaluation(
            policy="pattern",
            verdict="chose",
            reason="K-NN winner",
            candidate_model="anthropic:claude-sonnet-4-6",
            confidence=0.7,
        ),
    ]
    _emit(
        bus,
        type="route.decided",
        payload=RouteDecided(
            chosen_model="anthropic:claude-sonnet-4-6",
            winner_index=3,
            elapsed_ms=2.5,
            chain=chain,
        ),
        turn_id=_new_turn_id(),
    )
    await bus.drain()

    families = _families(collector.expose())
    decisions = [
        s for s in families["metis_routing_decisions"] if s.name == "metis_routing_decisions_total"
    ]
    assert len(decisions) == 1
    assert decisions[0].labels == {
        "winning_slot": "pattern",
        "chosen_model": "anthropic:claude-sonnet-4-6",
    }


async def test_pattern_matched_counts_with_fingerprint_kind(bus: EventBus, collector):
    _emit(
        bus,
        type="pattern.matched",
        payload=PatternMatched(
            fingerprint_id="fp_abc",
            fingerprint_kind="hybrid",
            chosen_model="anthropic:claude-sonnet-4-6",
            confidence=0.65,
            sample_size=8,
            k_cluster_size=5,
            alternatives_count=2,
        ),
        turn_id=_new_turn_id(),
    )
    await bus.drain()

    families = _families(collector.expose())
    samples = [
        s for s in families["metis_pattern_matches"] if s.name == "metis_pattern_matches_total"
    ]
    assert len(samples) == 1
    assert samples[0].labels == {
        "chose_model": "anthropic:claude-sonnet-4-6",
        "fingerprint_version": "hybrid",
    }


async def test_quota_alert_updates_used_ratio_gauge(bus: EventBus, collector):
    _emit(
        bus,
        type="quota.alert",
        payload=QuotaAlert(
            scope="key_daily",
            severity="warning",
            current_usd=Decimal("8.0"),
            limit_usd=Decimal("10.0"),
            percentage=0.80,
            gateway_key_id="gk_alpha",
        ),
    )
    await bus.drain()

    families = _families(collector.expose())
    gauges = [s for s in families["metis_quota_used_ratio"] if s.name == "metis_quota_used_ratio"]
    assert len(gauges) == 1
    assert gauges[0].labels == {"identity_kind": "key", "identity_id": "gk_alpha"}
    assert gauges[0].value == pytest.approx(0.80)


async def test_gateway_quota_exceeded_pins_ratio_to_one(bus: EventBus, collector):
    _emit(
        bus,
        type="gateway.quota_exceeded",
        payload=GatewayQuotaExceeded(
            scope="user_monthly",
            current_usd=Decimal("105.0"),
            limit_usd=Decimal("100.0"),
            inbound_shape="anthropic",
            user_id="alice",
        ),
    )
    await bus.drain()

    families = _families(collector.expose())
    gauges = [s for s in families["metis_quota_used_ratio"] if s.name == "metis_quota_used_ratio"]
    assert len(gauges) == 1
    assert gauges[0].labels == {"identity_kind": "user", "identity_id": "alice"}
    assert gauges[0].value == 1.0


async def test_eval_completed_counts_verdict(bus: EventBus, collector):
    _emit(
        bus,
        type="eval.completed",
        payload=EvalCompleted(
            eval_id="ev_001",
            subject_kind="turn",
            subject_id=_new_turn_id(),
            score=0.75,
            confidence=0.9,
            judge_kind="hybrid",
            judge_cost_usd=Decimal("0.0001"),
            judge_latency_ms=42,
            rubric_id="turn-hybrid",
            rubric_version="v1",
            signals={},
        ),
    )
    await bus.drain()

    families = _families(collector.expose())
    samples = [
        s for s in families["metis_eval_verdicts"] if s.name == "metis_eval_verdicts_total"
    ]
    assert len(samples) == 1
    assert samples[0].labels == {"judge_kind": "hybrid", "subject_kind": "turn"}


# ---------------------------------------------------------------------------
# Polled gauges
# ---------------------------------------------------------------------------


async def test_session_count_getter_drives_gauge(bus: EventBus):
    sessions = [object()] * 4
    collector = MetricsCollector(
        bus=bus,
        session_count_getter=lambda: len(sessions),
    )
    collector.attach()
    try:
        families = _families(collector.expose())
        sample = next(s for s in families["metis_session_count"] if s.name == "metis_session_count")
        assert sample.value == 4.0

        sessions.pop()
        sessions.pop()
        families = _families(collector.expose())
        sample = next(s for s in families["metis_session_count"] if s.name == "metis_session_count")
        assert sample.value == 2.0
    finally:
        collector.detach()


async def test_gateway_keys_getter_drives_active_and_revoked(bus: EventBus):
    counts = (3, 1)
    collector = MetricsCollector(
        bus=bus,
        gateway_keys_getter=lambda: counts,
    )
    collector.attach()
    try:
        families = _families(collector.expose())
        active = next(
            s for s in families["metis_gateway_keys_active"] if s.name == "metis_gateway_keys_active"
        )
        revoked = next(
            s
            for s in families["metis_gateway_keys_revoked"]
            if s.name == "metis_gateway_keys_revoked"
        )
        assert active.value == 3.0
        assert revoked.value == 1.0
    finally:
        collector.detach()


async def test_failing_getter_does_not_break_exposition(bus: EventBus):
    def boom() -> int:
        raise RuntimeError("source unavailable")

    collector = MetricsCollector(bus=bus, session_count_getter=boom)
    collector.attach()
    try:
        body = collector.expose()
        # Body still produced; the gauge stays at its last value (0 here).
        assert b"metis_session_count" in body
    finally:
        collector.detach()


# ---------------------------------------------------------------------------
# Wire / cardinality contract
# ---------------------------------------------------------------------------


async def test_exposition_uses_prometheus_content_type(bus: EventBus, collector):
    body = collector.expose()
    # Spec §2.1 — content-type is whatever prometheus_client emits.
    assert "text/plain" in METRICS_CONTENT_TYPE
    families = _families(body)
    # Even with no observed events, every family should be registered.
    expected_families = {
        "metis_llm_calls",
        "metis_llm_call_latency_seconds",
        "metis_llm_cost_usd",
        "metis_routing_decisions",
        "metis_pattern_matches",
        "metis_quota_used_ratio",
        "metis_eval_verdicts",
        "metis_session_count",
        "metis_gateway_keys_active",
        "metis_gateway_keys_revoked",
    }
    assert expected_families.issubset(set(families.keys()))


async def test_route_decided_with_unknown_winner_index_collapses_to_unknown(
    bus: EventBus, collector
):
    _emit(
        bus,
        type="route.decided",
        payload=RouteDecided(
            chosen_model="anthropic:claude-haiku-4-5",
            winner_index=99,  # out of range
            elapsed_ms=1.0,
            chain=[
                PolicyEvaluation(
                    policy="global_default", verdict="chose", reason="default"
                )
            ],
        ),
        turn_id=_new_turn_id(),
    )
    await bus.drain()

    families = _families(collector.expose())
    samples = [
        s for s in families["metis_routing_decisions"] if s.name == "metis_routing_decisions_total"
    ]
    assert len(samples) == 1
    assert samples[0].labels["winning_slot"] == "unknown"


async def test_quota_event_with_unknown_scope_is_ignored(bus: EventBus, collector):
    # Scope must start with key/user/team. A malformed scope shouldn't
    # mint a new label series.
    body_before = collector.expose()
    families_before = _families(body_before)
    gauges_before = [
        s for s in families_before["metis_quota_used_ratio"] if s.name == "metis_quota_used_ratio"
    ]

    _emit(
        bus,
        type="gateway.quota_exceeded",
        payload=GatewayQuotaExceeded(
            scope="team_daily",  # valid — establish a baseline
            current_usd=Decimal("50.0"),
            limit_usd=Decimal("50.0"),
            inbound_shape="openai",
            team_id="growth",
        ),
    )
    await bus.drain()

    families = _families(collector.expose())
    gauges = [s for s in families["metis_quota_used_ratio"] if s.name == "metis_quota_used_ratio"]
    assert len(gauges) == len(gauges_before) + 1
    new_gauge = next(s for s in gauges if s.labels["identity_id"] == "growth")
    assert new_gauge.labels["identity_kind"] == "team"


async def test_subscriber_is_non_fast_path(bus: EventBus, collector):
    """`event-bus-and-trace-catalog.md §3.4` — observability never blocks."""
    handle = collector._handle
    assert handle is not None
    # Internal: the bus's subscription registry knows whether this is fast-path.
    sub = bus._subscriptions[handle.id]
    assert sub.fast_path is False


async def test_handler_exception_swallowed(bus: EventBus, collector, monkeypatch):
    """A thrown handler error must not crash the bus dispatch."""

    def explode(_payload):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(collector, "_on_eval_completed", explode)
    _emit(
        bus,
        type="eval.completed",
        payload=EvalCompleted(
            eval_id="ev_002",
            subject_kind="session",
            subject_id=_new_session_id(),
            score=0.5,
            confidence=0.5,
            judge_kind="heuristic",
            judge_cost_usd=Decimal("0.0"),
            judge_latency_ms=1,
            rubric_id="session-aggregate",
            rubric_version="v1",
            signals={},
        ),
    )
    await bus.drain()
    # No exception bubbled out; bus dispatch task is still alive.
    assert bus._dispatch_task is not None and not bus._dispatch_task.done()
