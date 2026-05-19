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
from metis.core.canonical.ids import next_monotonic_ulid
from metis.core.events.bus import EventBus
from metis.core.events.envelope import Actor
from metis.core.events.payloads import (
    EvalCompleted,
    GatewayAuthFailed,
    GatewayQuotaExceeded,
    LLMCallCompleted,
    LLMCallFailed,
    PatternMatched,
    PolicyEvaluation,
    QuotaAlert,
    RouteDecided,
    ToolCalled,
    ToolCompleted,
    ToolFailed,
    make_event,
)
from metis.core.observability import METRICS_CONTENT_TYPE, MetricsCollector
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
    samples = [s for s in families["metis_eval_verdicts"] if s.name == "metis_eval_verdicts_total"]
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
            s
            for s in families["metis_gateway_keys_active"]
            if s.name == "metis_gateway_keys_active"
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


async def test_pattern_cache_getter_drives_hit_ratio(bus: EventBus):
    """v2 embedding-cache hit ratio is exposed per workspace.

    The getter contract is `() -> list[(workspace_id, hits, misses)]`; the
    collector computes `hits/(hits+misses)` and exposes hits/misses as
    separate gauges so prometheus can rate() them independently.
    """
    entries = [("ws-A", 80, 20), ("ws-B", 0, 0)]
    collector = MetricsCollector(
        bus=bus,
        pattern_cache_getter=lambda: entries,
    )
    collector.attach()
    try:
        families = _families(collector.expose())
        ratios = {
            tuple(sorted(s.labels.items())): s.value
            for s in families["metis_pattern_embedding_cache_hit_ratio"]
        }
        # ws-A: 80 / 100 = 0.8
        assert ratios[(("workspace_id", "ws-A"),)] == pytest.approx(0.8)
        # ws-B: zero lookups -> ratio defaults to 0.0 (not NaN, not absent)
        assert ratios[(("workspace_id", "ws-B"),)] == pytest.approx(0.0)
        hits = {
            tuple(sorted(s.labels.items())): s.value
            for s in families["metis_pattern_embedding_cache_hits_total"]
        }
        assert hits[(("workspace_id", "ws-A"),)] == 80
        misses = {
            tuple(sorted(s.labels.items())): s.value
            for s in families["metis_pattern_embedding_cache_misses_total"]
        }
        assert misses[(("workspace_id", "ws-A"),)] == 20
    finally:
        collector.detach()


async def test_pattern_cache_getter_failure_does_not_break_exposition(bus: EventBus):
    def boom() -> list:
        raise RuntimeError("getter failed")

    collector = MetricsCollector(bus=bus, pattern_cache_getter=boom)
    collector.attach()
    try:
        body = collector.expose()
        # Body still produced; pattern cache gauges stay at zero (no labels).
        assert b"metis_pattern_embedding_cache_hit_ratio" in body
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


async def test_trace_wal_bytes_getter_drives_gauge(bus: EventBus):
    """Wave 13: `trace_wal_bytes_getter` polls the trace-DB WAL file size.

    Operators alert on this gauge sustaining above ~3x the auto-checkpoint
    threshold per docs/operations/trace-performance.md §3.
    """
    wal_size = 12_345
    collector = MetricsCollector(
        bus=bus,
        trace_wal_bytes_getter=lambda: wal_size,
    )
    collector.attach()
    try:
        families = _families(collector.expose())
        gauge = next(
            s for s in families["metis_trace_wal_bytes"] if s.name == "metis_trace_wal_bytes"
        )
        assert gauge.value == 12_345.0

        # Mutating the captured value drives the gauge — confirms it's
        # polled per scrape, not snapshot at construction.
        wal_size = 67_890
        families = _families(collector.expose())
        gauge = next(
            s for s in families["metis_trace_wal_bytes"] if s.name == "metis_trace_wal_bytes"
        )
        assert gauge.value == 67_890.0
    finally:
        collector.detach()


async def test_trace_wal_bytes_getter_failure_does_not_break_exposition(bus: EventBus):
    def boom() -> int:
        raise RuntimeError("WAL stat failed")

    collector = MetricsCollector(bus=bus, trace_wal_bytes_getter=boom)
    collector.attach()
    try:
        body = collector.expose()
        # Body still produced; gauge holds its prior value (zero).
        assert b"metis_trace_wal_bytes" in body
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
            chain=[PolicyEvaluation(policy="global_default", verdict="chose", reason="default")],
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


# ---------------------------------------------------------------------------
# Wave 14a — production-grade observability extensions (observability.md §3.2)
# ---------------------------------------------------------------------------


async def test_route_decided_observes_routing_latency(bus: EventBus, collector):
    """`route.decided.elapsed_ms` drives `metis_routing_decision_latency_seconds`."""
    _emit(
        bus,
        type="route.decided",
        payload=RouteDecided(
            chosen_model="anthropic:claude-haiku-4-5",
            winner_index=0,
            elapsed_ms=2.5,
            chain=[PolicyEvaluation(policy="global_default", verdict="chose", reason="default")],
        ),
        turn_id=_new_turn_id(),
    )
    await bus.drain()

    families = _families(collector.expose())
    samples = families["metis_routing_decision_latency_seconds"]
    sum_sample = next(s for s in samples if s.name.endswith("_sum"))
    count_sample = next(s for s in samples if s.name.endswith("_count"))
    assert count_sample.value == 1.0
    # 2.5 ms → 0.0025 s
    assert sum_sample.value == pytest.approx(0.0025)


async def test_llm_failed_increments_dedicated_error_counter(bus: EventBus, collector):
    """`llm.call_failed` bumps both `metis_llm_calls_total{status}` AND the
    dedicated `metis_llm_call_errors_total{error_class}` so alerting can
    rate() the error series without summing across status labels.
    """
    _emit(
        bus,
        type="llm.call_failed",
        payload=LLMCallFailed(
            model="anthropic:claude-haiku-4-5",
            provider="anthropic",
            error_class="server_error",
            error_message_redacted="500",
            retry_count=2,
            latency_ms=180,
        ),
    )
    await bus.drain()

    families = _families(collector.expose())
    errs = [s for s in families["metis_llm_call_errors"] if s.name == "metis_llm_call_errors_total"]
    assert len(errs) == 1
    assert errs[0].labels == {
        "provider": "anthropic",
        "model": "anthropic:claude-haiku-4-5",
        "error_class": "server_error",
    }
    assert errs[0].value == 1.0

    # The legacy mixed counter still picks the same row up — invariant for
    # back-compat with dashboards built against the Wave-11 surface.
    calls = [s for s in families["metis_llm_calls"] if s.name == "metis_llm_calls_total"]
    assert any(s.labels["status"] == "server_error" for s in calls)


async def test_tool_completed_observes_latency_under_tool_name(bus: EventBus, collector):
    """`tool.called → tool.completed` correlation drives the tool latency
    histogram with the right `tool_name` label, then drains the LRU.
    """
    _emit(
        bus,
        type="tool.called",
        payload=ToolCalled(
            tool_use_id="tu_abc",
            tool_name="read_file",
            input_hash="x",
            input_size_bytes=100,
            side_effects="read",
        ),
    )
    _emit(
        bus,
        type="tool.completed",
        payload=ToolCompleted(
            tool_use_id="tu_abc",
            success=True,
            output_size_bytes=2048,
            latency_ms=15,
        ),
    )
    await bus.drain()

    families = _families(collector.expose())
    samples = families["metis_tool_call_latency_seconds"]
    matching = [s for s in samples if s.labels.get("tool_name") == "read_file"]
    sum_sample = next(s for s in matching if s.name.endswith("_sum"))
    count_sample = next(s for s in matching if s.name.endswith("_count"))
    assert count_sample.value == 1.0
    assert sum_sample.value == pytest.approx(0.015)
    # The mapping was drained off the LRU when completed fired.
    assert "tu_abc" not in collector._tool_names


async def test_tool_failed_increments_failure_counter_with_tool_name(bus: EventBus, collector):
    _emit(
        bus,
        type="tool.called",
        payload=ToolCalled(
            tool_use_id="tu_fail",
            tool_name="run_bash",
            input_hash="y",
            input_size_bytes=50,
            side_effects="execute",
        ),
    )
    _emit(
        bus,
        type="tool.failed",
        payload=ToolFailed(
            tool_use_id="tu_fail",
            error_class="timeout",
            error_message="exceeded 60s",
            latency_ms=60_000,
        ),
    )
    await bus.drain()

    families = _families(collector.expose())
    failures = [s for s in families["metis_tool_failures"] if s.name == "metis_tool_failures_total"]
    assert len(failures) == 1
    assert failures[0].labels == {"tool_name": "run_bash", "error_class": "timeout"}
    assert failures[0].value == 1.0


async def test_tool_completed_without_prior_call_collapses_to_unknown(bus: EventBus, collector):
    """A `tool.completed` we never saw `tool.called` for must not mint a
    new series — it falls into the `unknown` bucket. Real-world cause:
    the collector started mid-turn after the dispatcher already emitted
    the call event.
    """
    _emit(
        bus,
        type="tool.completed",
        payload=ToolCompleted(
            tool_use_id="tu_orphan",
            success=True,
            output_size_bytes=10,
            latency_ms=5,
        ),
    )
    await bus.drain()

    families = _families(collector.expose())
    samples = families["metis_tool_call_latency_seconds"]
    matching = [s for s in samples if s.labels.get("tool_name") == "unknown"]
    count_sample = next(s for s in matching if s.name.endswith("_count"))
    assert count_sample.value == 1.0


async def test_gateway_auth_failed_increments_counter_by_reason(bus: EventBus, collector):
    """`gateway.auth_failed` drives `metis_gateway_auth_failures_total{reason}`.

    Buckets exactly the three documented reasons from the spec — anything
    else collapses to `unknown` via the same `_label()` fallback used by
    the other event handlers.
    """
    for reason in ("missing_token", "invalid_token", "key_revoked"):
        _emit(
            bus,
            type="gateway.auth_failed",
            payload=GatewayAuthFailed(
                reason=reason,
                inbound_shape="openai",
                token_hash_prefix="deadbeef" if reason != "missing_token" else None,
            ),
        )
    await bus.drain()

    families = _families(collector.expose())
    rows = {
        s.labels["reason"]: s.value
        for s in families["metis_gateway_auth_failures"]
        if s.name == "metis_gateway_auth_failures_total"
    }
    assert rows == {
        "missing_token": 1.0,
        "invalid_token": 1.0,
        "key_revoked": 1.0,
    }


async def test_tool_name_cache_is_bounded(bus: EventBus, collector):
    """LRU caps at `_TOOL_NAME_CACHE_MAX` so a never-completed tool
    leak can't grow without bound. The oldest entries are evicted first.
    """
    from metis.core.observability.metrics import _TOOL_NAME_CACHE_MAX

    # Emit cap + 5 tool.called events; the oldest 5 should be evicted.
    for i in range(_TOOL_NAME_CACHE_MAX + 5):
        _emit(
            bus,
            type="tool.called",
            payload=ToolCalled(
                tool_use_id=f"tu_leak_{i}",
                tool_name=f"tool_{i % 3}",
                input_hash="h",
                input_size_bytes=1,
                side_effects="read",
            ),
        )
    await bus.drain()

    assert len(collector._tool_names) == _TOOL_NAME_CACHE_MAX
    assert "tu_leak_0" not in collector._tool_names
    assert "tu_leak_4" not in collector._tool_names
    assert "tu_leak_5" in collector._tool_names
    assert f"tu_leak_{_TOOL_NAME_CACHE_MAX + 4}" in collector._tool_names


async def test_llm_completed_attributes_cost_to_per_key_counter(bus: EventBus, collector):
    """Wave 14a — per-key spend anomaly detection runs against
    `metis_gateway_key_cost_usd_total{gateway_key_id}`. Calls with no key
    (agent-loop) bucket under `null` so the metric is queryable in one
    shot.
    """
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
            cost_usd=0.0500,
            pricing_version="v1",
            latency_ms=200,
            stop_reason="end_turn",
            produced_tool_calls=0,
            produced_thinking_blocks=0,
            gateway_key_id="gk_metric_test",
        ),
    )
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
            cost_usd=0.0200,
            pricing_version="v1",
            latency_ms=200,
            stop_reason="end_turn",
            produced_tool_calls=0,
            produced_thinking_blocks=0,
            # No gateway_key_id — agent-loop traffic
        ),
    )
    await bus.drain()

    families = _families(collector.expose())
    samples = {
        s.labels["gateway_key_id"]: s.value
        for s in families["metis_gateway_key_cost_usd"]
        if s.name == "metis_gateway_key_cost_usd_total"
    }
    assert samples["gk_metric_test"] == pytest.approx(0.05)
    assert samples["null"] == pytest.approx(0.02)


async def test_llm_latency_buckets_cover_required_range(bus: EventBus, collector):
    """Spec §3 requires the LLM latency histogram covers the 0.1-30s range.

    Verify the registered bucket boundaries hit both ends of that span
    (this is the gate on adopting the histogram for the production
    p99 alert rule in `prometheus-rules.yaml`).
    """
    from metis.core.observability.metrics import _LATENCY_BUCKETS_SECONDS

    assert 0.1 in _LATENCY_BUCKETS_SECONDS
    assert 30.0 in _LATENCY_BUCKETS_SECONDS
    # Edges of the alert-rule range have at least one bucket inside them.
    inside = [b for b in _LATENCY_BUCKETS_SECONDS if 0.1 <= b <= 30.0]
    assert len(inside) >= 5
