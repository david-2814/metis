"""AnalyticsStore tests covering the spec's required test plan (§8.1)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from metis.core.analytics import (
    AnalyticsStore,
    InvalidGroupByError,
    InvalidOrderError,
    TimeWindow,
    TurnNotFoundError,
    UnknownBaselineModelError,
)
from metis.core.analytics.store import _percentile
from metis.core.pricing import DEFAULT_PRICE_TABLE, ModelPricing, PriceTable


@pytest.fixture
def window(now):
    return TimeWindow(start=now - timedelta(days=1), end=now + timedelta(days=1))


# ---- /analytics/cost ------------------------------------------------------


def test_cost_empty_window_returns_empty_data(seeded_db, window):
    db_path, _seeder = seeded_db
    with AnalyticsStore(db_path) as store:
        assert store.cost(window, group_by="model") == []


def test_cost_group_by_model_aggregates(seeded_db, now, window):
    db_path, seeder = seeded_db
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="anthropic:claude-sonnet-4-6",
        provider="anthropic",
        cost_usd="0.10",
        input_tokens=100,
        output_tokens=20,
        latency_ms=1000,
    )
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="anthropic:claude-sonnet-4-6",
        provider="anthropic",
        cost_usd="0.20",
        input_tokens=200,
        output_tokens=40,
        latency_ms=2000,
    )
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="anthropic:claude-haiku-4-5",
        provider="anthropic",
        cost_usd="0.01",
        input_tokens=50,
        output_tokens=10,
        latency_ms=500,
    )
    with AnalyticsStore(db_path) as store:
        data = store.cost(window, group_by="model")
    by_model = {row["model"]: row for row in data}
    sonnet = by_model["anthropic:claude-sonnet-4-6"]
    assert sonnet["cost_usd"] == pytest.approx(0.30)
    assert sonnet["input_tokens"] == 300
    assert sonnet["output_tokens"] == 60
    assert sonnet["call_count"] == 2
    assert sonnet["avg_latency_ms"] == pytest.approx(1500.0)
    haiku = by_model["anthropic:claude-haiku-4-5"]
    assert haiku["call_count"] == 1
    # Order: cost_usd DESC.
    assert data[0]["model"] == "anthropic:claude-sonnet-4-6"


def test_cost_group_by_provider(seeded_db, now, window):
    db_path, seeder = seeded_db
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="anthropic:claude-sonnet-4-6",
        provider="anthropic",
        cost_usd="0.10",
    )
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="openai:gpt-5",
        provider="openai",
        cost_usd="0.05",
    )
    with AnalyticsStore(db_path) as store:
        data = store.cost(window, group_by="provider")
    assert {row["provider"] for row in data} == {"anthropic", "openai"}
    assert "model" not in data[0]


def test_cost_group_by_session_uses_envelope_column(seeded_db, now, window):
    db_path, seeder = seeded_db
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="x:y",
        provider="x",
        cost_usd="0.10",
        session_id="sess_one",
    )
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="x:y",
        provider="x",
        cost_usd="0.05",
        session_id="sess_two",
    )
    with AnalyticsStore(db_path) as store:
        data = store.cost(window, group_by="session")
    sessions = {row["session_id"]: row for row in data}
    assert set(sessions) == {"sess_one", "sess_two"}
    assert sessions["sess_one"]["cost_usd"] == pytest.approx(0.10)


def test_cost_group_by_day_ordered_ascending(seeded_db, now, window):
    db_path, seeder = seeded_db
    seeder.insert_llm_call_completed(
        timestamp=now - timedelta(hours=12),
        model="x:y",
        provider="x",
        cost_usd="0.20",  # earlier
    )
    seeder.insert_llm_call_completed(
        timestamp=now + timedelta(hours=2),
        model="x:y",
        provider="x",
        cost_usd="0.10",  # later
    )
    with AnalyticsStore(db_path) as store:
        data = store.cost(window, group_by="day")
    assert len(data) >= 1
    buckets = [row["bucket"] for row in data]
    assert buckets == sorted(buckets)  # ASC


def test_cost_group_by_hour_uses_hour_bucket(seeded_db, now, window):
    db_path, seeder = seeded_db
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="x:y",
        provider="x",
        cost_usd="0.10",
    )
    with AnalyticsStore(db_path) as store:
        data = store.cost(window, group_by="hour")
    assert "T" in data[0]["bucket"]  # date+hour, e.g. 2026-05-12T12


def test_cost_group_by_none_returns_object(seeded_db, now, window):
    db_path, seeder = seeded_db
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="a:1",
        provider="a",
        cost_usd="0.10",
        input_tokens=100,
    )
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="b:1",
        provider="b",
        cost_usd="0.20",
        input_tokens=200,
    )
    with AnalyticsStore(db_path) as store:
        data = store.cost(window, group_by="none")
    assert isinstance(data, dict)
    assert data["cost_usd"] == pytest.approx(0.30)
    assert data["input_tokens"] == 300
    assert data["call_count"] == 2


def test_cost_group_by_none_empty_returns_zeroed_object(seeded_db, window):
    db_path, _ = seeded_db
    with AnalyticsStore(db_path) as store:
        data = store.cost(window, group_by="none")
    assert isinstance(data, dict)
    assert data["cost_usd"] == 0.0
    assert data["call_count"] == 0


def test_cost_group_by_gateway_key(seeded_db, now, window):
    db_path, seeder = seeded_db
    # Two gateway-stamped calls (key A), one (key B), one agent-loop call (null).
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="anthropic:claude-sonnet-4-6",
        provider="anthropic",
        cost_usd="0.10",
        input_tokens=100,
        output_tokens=20,
        latency_ms=1000,
        gateway_key_id="gk_alpha",
        inbound_shape="openai",
    )
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="anthropic:claude-sonnet-4-6",
        provider="anthropic",
        cost_usd="0.05",
        input_tokens=50,
        output_tokens=10,
        latency_ms=1000,
        gateway_key_id="gk_alpha",
        inbound_shape="anthropic",
    )
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="anthropic:claude-haiku-4-5",
        provider="anthropic",
        cost_usd="0.01",
        input_tokens=20,
        output_tokens=5,
        latency_ms=200,
        gateway_key_id="gk_beta",
        inbound_shape="openai",
    )
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="anthropic:claude-haiku-4-5",
        provider="anthropic",
        cost_usd="0.02",
        input_tokens=30,
        output_tokens=5,
        latency_ms=400,
    )
    with AnalyticsStore(db_path) as store:
        data = store.cost(window, group_by="gateway_key")
    by_key = {row["gateway_key_id"]: row for row in data}
    assert by_key["gk_alpha"]["cost_usd"] == pytest.approx(0.15)
    assert by_key["gk_alpha"]["call_count"] == 2
    assert by_key["gk_beta"]["cost_usd"] == pytest.approx(0.01)
    # Agent-loop traffic (no gateway_key_id stamp) keyed under None.
    assert None in by_key
    assert by_key[None]["cost_usd"] == pytest.approx(0.02)
    # Result ordered by cost DESC.
    assert data[0]["gateway_key_id"] == "gk_alpha"


def test_cost_invalid_group_by_raises(seeded_db, window):
    db_path, _ = seeded_db
    with AnalyticsStore(db_path) as store:
        with pytest.raises(InvalidGroupByError):
            store.cost(window, group_by="DROP TABLE")


def test_cost_only_counts_in_window(seeded_db, now):
    db_path, seeder = seeded_db
    # Insert one row outside window, one inside.
    inside = now
    outside = now - timedelta(days=30)
    seeder.insert_llm_call_completed(
        timestamp=outside,
        model="x:y",
        provider="x",
        cost_usd="9.99",
    )
    seeder.insert_llm_call_completed(
        timestamp=inside,
        model="x:y",
        provider="x",
        cost_usd="0.10",
    )
    window = TimeWindow(start=now - timedelta(hours=1), end=now + timedelta(hours=1))
    with AnalyticsStore(db_path) as store:
        data = store.cost(window, group_by="model")
    assert len(data) == 1
    assert data[0]["cost_usd"] == pytest.approx(0.10)


# ---- /analytics/cost delegation rollups (delegation.md §8.2) -------------


def test_cost_group_by_parent_session_rolls_workers_under_planner(seeded_db, now, window):
    db_path, seeder = seeded_db
    # Planner spend on session sess_planner.
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="anthropic:claude-sonnet-4-6",
        provider="anthropic",
        cost_usd="0.10",
        session_id="sess_planner",
    )
    # Worker spend on a child session, parent points at sess_planner.
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="anthropic:claude-haiku-4-5",
        provider="anthropic",
        cost_usd="0.02",
        session_id="sess_worker_a",
        parent_session_id="sess_planner",
    )
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="anthropic:claude-haiku-4-5",
        provider="anthropic",
        cost_usd="0.01",
        session_id="sess_worker_b",
        parent_session_id="sess_planner",
    )
    # Unrelated top-level session.
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="anthropic:claude-sonnet-4-6",
        provider="anthropic",
        cost_usd="0.05",
        session_id="sess_other",
    )
    with AnalyticsStore(db_path) as store:
        data = store.cost(window, group_by="parent_session")
    rolled = {row["parent_session_id"]: row for row in data}
    assert rolled["sess_planner"]["cost_usd"] == pytest.approx(0.13)
    assert rolled["sess_planner"]["call_count"] == 3
    assert rolled["sess_other"]["cost_usd"] == pytest.approx(0.05)


def test_cost_group_by_is_worker_partitions_planner_vs_worker(seeded_db, now, window):
    db_path, seeder = seeded_db
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="anthropic:claude-sonnet-4-6",
        provider="anthropic",
        cost_usd="0.10",
        session_id="sess_planner",
    )
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="anthropic:claude-haiku-4-5",
        provider="anthropic",
        cost_usd="0.02",
        session_id="sess_worker",
        parent_session_id="sess_planner",
    )
    with AnalyticsStore(db_path) as store:
        data = store.cost(window, group_by="is_worker")
    by_label = {row["is_worker"]: row for row in data}
    assert by_label["planner"]["cost_usd"] == pytest.approx(0.10)
    assert by_label["worker"]["cost_usd"] == pytest.approx(0.02)


def test_cost_include_workers_false_excludes_worker_rows(seeded_db, now, window):
    db_path, seeder = seeded_db
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="anthropic:claude-sonnet-4-6",
        provider="anthropic",
        cost_usd="0.10",
        session_id="sess_planner",
    )
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="anthropic:claude-haiku-4-5",
        provider="anthropic",
        cost_usd="0.02",
        session_id="sess_worker",
        parent_session_id="sess_planner",
    )
    with AnalyticsStore(db_path) as store:
        all_rows = store.cost(window, group_by="model")
        planner_only = store.cost(window, group_by="model", include_workers=False)
    assert sum(r["cost_usd"] for r in all_rows) == pytest.approx(0.12)
    assert sum(r["cost_usd"] for r in planner_only) == pytest.approx(0.10)


# ---- /analytics/cache_effectiveness --------------------------------------


def test_cache_hit_rate_includes_cache_writes(seeded_db, now, window):
    db_path, seeder = seeded_db
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="anthropic:claude-sonnet-4-6",
        provider="anthropic",
        cost_usd="0.10",
        input_tokens=1000,
        cached_input_tokens=400,
        cache_creation_input_tokens=600,
    )
    with AnalyticsStore(db_path) as store:
        data = store.cache_effectiveness(window)
    row = data[0]
    # total = 1000 + 400 + 600 = 2000
    assert row["hit_rate"] == pytest.approx(400 / 2000)  # 0.20
    assert row["cache_write_share"] == pytest.approx(600 / 2000)  # 0.30


def test_cache_zero_tokens_returns_null_ratios(seeded_db, now, window):
    db_path, seeder = seeded_db
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="x:y",
        provider="x",
        cost_usd="0.10",
        input_tokens=0,
        cached_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    with AnalyticsStore(db_path) as store:
        data = store.cache_effectiveness(window)
    assert data[0]["hit_rate"] is None
    assert data[0]["cache_write_share"] is None


# ---- /analytics/routing ---------------------------------------------------


def _chain_entry(policy, verdict="not_applicable", **extras):
    return {
        "policy": policy,
        "verdict": verdict,
        "candidate_model": extras.get("candidate_model"),
        "reason": extras.get("reason", ""),
        "rule_name": extras.get("rule_name"),
        "validation_failure": extras.get("validation_failure"),
    }


def test_routing_wins_by_policy_emits_all_seven_slots(seeded_db, now, window):
    db_path, seeder = seeded_db
    # winner_index = 2 → policy 'rule'
    chain = [
        _chain_entry("per_message_override"),
        _chain_entry("manual_sticky"),
        _chain_entry("rule", verdict="chose", candidate_model="anthropic:claude-sonnet-4-6"),
        _chain_entry("pattern"),
        _chain_entry("delegate_request"),
        _chain_entry("workspace_default"),
        _chain_entry("global_default"),
    ]
    seeder.insert_route_decided(
        timestamp=now,
        chosen_model="anthropic:claude-sonnet-4-6",
        winner_index=2,
        chain=chain,
    )
    with AnalyticsStore(db_path) as store:
        data = store.routing(window)
    by_policy = {row["policy"]: row["count"] for row in data["wins_by_policy"]}
    # All seven slots present, with 'rule' counted once.
    assert set(by_policy) == {
        "per_message_override",
        "manual_sticky",
        "rule",
        "pattern",
        "delegate_request",
        "workspace_default",
        "global_default",
    }
    assert by_policy["rule"] == 1
    assert by_policy["global_default"] == 0


def test_routing_hard_failure_bucketed(seeded_db, now, window):
    db_path, seeder = seeded_db
    chain = [
        _chain_entry(
            "per_message_override",
            verdict="rejected",
            candidate_model="x:y",
            validation_failure="not_configured",
        ),
        _chain_entry(
            "manual_sticky",
            verdict="rejected",
            candidate_model="x:y",
            validation_failure="provider_unavailable",
        ),
        _chain_entry(
            "global_default",
            verdict="rejected",
            candidate_model="x:y",
            validation_failure="provider_unavailable",
        ),
    ]
    seeder.insert_route_decided(
        timestamp=now,
        chosen_model="",
        winner_index=-1,
        chain=chain,
    )
    with AnalyticsStore(db_path) as store:
        data = store.routing(window)
    assert data["hard_failures"] == 1
    # No policy got a win.
    assert all(row["count"] == 0 for row in data["wins_by_policy"])
    # Rejections still flow into the rejections breakdown.
    failures = {(row["policy"], row["validation_failure"]) for row in data["rejections"]}
    assert ("per_message_override", "not_configured") in failures
    assert ("manual_sticky", "provider_unavailable") in failures
    assert ("global_default", "provider_unavailable") in failures


def test_routing_rejections_aggregated(seeded_db, now, window):
    db_path, seeder = seeded_db
    chain = [
        _chain_entry(
            "manual_sticky",
            verdict="rejected",
            candidate_model="anthropic:claude-opus-4-7",
            validation_failure="exceeds_context_window",
        ),
        _chain_entry(
            "global_default",
            verdict="chose",
            candidate_model="anthropic:claude-sonnet-4-6",
        ),
    ]
    seeder.insert_route_decided(
        timestamp=now,
        chosen_model="anthropic:claude-sonnet-4-6",
        winner_index=1,
        chain=chain,
    )
    with AnalyticsStore(db_path) as store:
        data = store.routing(window)
    rej = data["rejections"]
    assert len(rej) == 1
    assert rej[0]["policy"] == "manual_sticky"
    assert rej[0]["validation_failure"] == "exceeds_context_window"
    assert rej[0]["count"] == 1


# ---- /analytics/reliability -----------------------------------------------


def test_reliability_errors_grouped(seeded_db, now, window):
    db_path, seeder = seeded_db
    seeder.insert_llm_call_failed(
        timestamp=now,
        model="anthropic:claude-opus-4-7",
        provider="anthropic",
        error_class="rate_limit",
    )
    seeder.insert_llm_call_failed(
        timestamp=now,
        model="anthropic:claude-opus-4-7",
        provider="anthropic",
        error_class="rate_limit",
    )
    seeder.insert_llm_call_failed(
        timestamp=now,
        model="openai:gpt-5",
        provider="openai",
        error_class="server_error",
    )
    with AnalyticsStore(db_path) as store:
        data = store.reliability(window)
    rl = next(row for row in data["errors_by_class"] if row["model"] == "anthropic:claude-opus-4-7")
    assert rl["error_class"] == "rate_limit"
    assert rl["count"] == 2


def test_reliability_percentiles_nearest_rank(seeded_db, now, window):
    db_path, seeder = seeded_db
    for ms in (100, 200, 300, 400, 500, 600, 700, 800, 900, 1000):
        seeder.insert_llm_call_completed(
            timestamp=now,
            model="x:y",
            provider="x",
            cost_usd="0.01",
            latency_ms=ms,
        )
    with AnalyticsStore(db_path) as store:
        data = store.reliability(window)
    row = next(r for r in data["latency_ms_by_model"] if r["model"] == "x:y")
    assert row["sample_size"] == 10
    # Spec test 7: p50 ~= 500, p95 ~= 950 (nearest-rank interpolation).
    assert 450 <= row["p50"] <= 550
    assert 900 <= row["p95"] <= 1000


def test_percentile_helper():
    assert _percentile([], 0.5) is None
    assert _percentile([42], 0.5) == 42
    # Even split: linear interpolation.
    assert _percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 0.5) in (5, 6)


# ---- /analytics/sessions --------------------------------------------------


def test_sessions_order_cost(seeded_db, now):
    db_path, seeder = seeded_db
    seeder.insert_session(
        session_id="cheap",
        cost_so_far_usd=0.10,
        created_at=now,
        updated_at=now - timedelta(hours=1),
    )
    seeder.insert_session(
        session_id="expensive",
        cost_so_far_usd=5.00,
        created_at=now,
        updated_at=now - timedelta(days=1),
    )
    with AnalyticsStore(db_path) as store:
        data = store.sessions(order="cost", limit=10)
    assert [s["id"] for s in data] == ["expensive", "cheap"]
    assert data[0]["cost_usd"] == pytest.approx(5.00)


def test_sessions_order_recency(seeded_db, now):
    db_path, seeder = seeded_db
    seeder.insert_session(
        session_id="old",
        cost_so_far_usd=10.0,
        created_at=now - timedelta(days=10),
        updated_at=now - timedelta(days=10),
    )
    seeder.insert_session(
        session_id="new",
        cost_so_far_usd=0.01,
        created_at=now,
        updated_at=now,
    )
    with AnalyticsStore(db_path) as store:
        data = store.sessions(order="recency", limit=10)
    assert [s["id"] for s in data] == ["new", "old"]


def test_sessions_response_renames_cost(seeded_db, now):
    db_path, seeder = seeded_db
    seeder.insert_session(session_id="x", cost_so_far_usd=0.42, created_at=now)
    with AnalyticsStore(db_path) as store:
        data = store.sessions()
    assert "cost_usd" in data[0]
    assert "cost_so_far_usd" not in data[0]


def test_sessions_invalid_order_rejected(seeded_db):
    db_path, _ = seeded_db
    with AnalyticsStore(db_path) as store:
        with pytest.raises(InvalidOrderError):
            store.sessions(order="; DELETE")


# ---- /analytics/turns/{turn_id} -------------------------------------------


def test_turn_drill_down_round_trip(seeded_db, now):
    db_path, seeder = seeded_db
    seeder.insert_session(session_id="s", created_at=now)
    seeder.insert_event(
        event_type="turn.started",
        timestamp=now,
        session_id="s",
        turn_id="t1",
        payload={
            "user_message_hash": "abc",
            "estimated_input_tokens": 10,
            "has_images": False,
            "has_tool_calls_in_history": False,
        },
    )
    seeder.insert_message(
        message_id="m1",
        session_id="s",
        role="user",
        content=[{"type": "text", "text": "hi"}],
        metadata={},
        created_at=now,
    )
    seeder.insert_event(
        event_type="turn.completed",
        timestamp=now + timedelta(seconds=10),
        session_id="s",
        turn_id="t1",
        payload={
            "stop_reason": "end_turn",
            "llm_call_count": 1,
            "tool_call_count": 0,
            "total_input_tokens": 10,
            "total_output_tokens": 5,
            "total_cost_usd": 0.01,
            "wall_time_seconds": 0.5,
        },
    )
    seeder.insert_message(
        message_id="m2",
        session_id="s",
        role="assistant",
        content=[{"type": "text", "text": "ok"}],
        metadata={"model": "x:y"},
        created_at=now + timedelta(seconds=5),
    )
    with AnalyticsStore(db_path) as store:
        data = store.turn("t1")
    assert data["turn_id"] == "t1"
    assert data["session_id"] == "s"
    assert data["in_flight"] is False
    types = [e["type"] for e in data["events"]]
    assert types == ["turn.started", "turn.completed"]
    assert len(data["messages"]) == 2


def test_turn_drill_down_in_flight(seeded_db, now):
    db_path, seeder = seeded_db
    seeder.insert_session(session_id="s", created_at=now)
    seeder.insert_event(
        event_type="turn.started",
        timestamp=now,
        session_id="s",
        turn_id="t_live",
        payload={
            "user_message_hash": "x",
            "estimated_input_tokens": 1,
            "has_images": False,
            "has_tool_calls_in_history": False,
        },
    )
    seeder.insert_message(
        message_id="m1",
        session_id="s",
        role="user",
        content=[{"type": "text", "text": "hi"}],
        metadata={},
        created_at=now,
    )
    with AnalyticsStore(db_path) as store:
        data = store.turn("t_live", now=now + timedelta(minutes=1))
    assert data["in_flight"] is True
    # User message still appears (within now() bound).
    assert len(data["messages"]) == 1


def test_turn_not_found_raises(seeded_db):
    db_path, _ = seeded_db
    with AnalyticsStore(db_path) as store:
        with pytest.raises(TurnNotFoundError):
            store.turn("does_not_exist")


# ---- /analytics/savings ---------------------------------------------------


def _test_price_table() -> PriceTable:
    return PriceTable(
        version="test-1",
        models={
            "fast": ModelPricing(input_per_mtok=Decimal("1"), output_per_mtok=Decimal("5")),
            "balanced": ModelPricing(input_per_mtok=Decimal("3"), output_per_mtok=Decimal("15")),
            "deep": ModelPricing(input_per_mtok=Decimal("15"), output_per_mtok=Decimal("75")),
        },
    )


def test_savings_unknown_baseline_rejected(seeded_db, window):
    db_path, _ = seeded_db
    pt = _test_price_table()
    with AnalyticsStore(db_path) as store:
        with pytest.raises(UnknownBaselineModelError):
            store.savings(window, baseline="does-not-exist", price_table=pt)


def test_savings_counterfactual_math(seeded_db, now, window):
    db_path, seeder = seeded_db
    pt = _test_price_table()
    # One row on fast, one on balanced. Baseline = deep.
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="fast",
        provider="x",
        cost_usd="0.001",
        input_tokens=1_000_000,
        output_tokens=200_000,
    )
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="balanced",
        provider="x",
        cost_usd="0.003",
        input_tokens=1_000_000,
        output_tokens=200_000,
    )
    with AnalyticsStore(db_path) as store:
        data = store.savings(window, baseline="deep", price_table=pt)
    # baseline: 2 rows * (1*15 + 0.2*75) = 2 * 30 = 60
    assert data["baseline_repriced_usd"] == pytest.approx(60.0)
    # actual_repriced: row1 (1*1+0.2*5)=2 + row2 (1*3+0.2*15)=6 = 8
    assert data["actual_repriced_usd"] == pytest.approx(8.0)
    assert data["savings_usd"] == pytest.approx(52.0)
    assert data["savings_pct"] == pytest.approx(52.0 / 60.0)


def test_savings_missing_model_excluded_from_actual(seeded_db, now, window):
    db_path, seeder = seeded_db
    pt = _test_price_table()
    # Row uses a model not in the current table.
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="legacy-model",
        provider="x",
        cost_usd="0.025",
        input_tokens=1_000_000,
        output_tokens=200_000,
    )
    with AnalyticsStore(db_path) as store:
        data = store.savings(window, baseline="deep", price_table=pt)
    # Stamped reflects what we paid.
    assert data["actual_stamped_usd"] == pytest.approx(0.025)
    # Re-priced excludes the missing-model row.
    assert data["actual_repriced_usd"] == pytest.approx(0.0)
    # Baseline includes the row.
    assert data["baseline_repriced_usd"] == pytest.approx(30.0)
    assert data["rows_missing_from_price_table"] == 1
    assert data["rows_total"] == 1


def test_savings_actual_stamped_unconditional(seeded_db, now, window):
    db_path, seeder = seeded_db
    pt = _test_price_table()
    # One known, one missing — stamped should include both.
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="fast",
        provider="x",
        cost_usd="0.10",
        input_tokens=10,
        output_tokens=2,
    )
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="legacy",
        provider="x",
        cost_usd="0.50",
        input_tokens=10,
        output_tokens=2,
    )
    with AnalyticsStore(db_path) as store:
        data = store.savings(window, baseline="deep", price_table=pt)
    assert data["actual_stamped_usd"] == pytest.approx(0.60)


def test_savings_negative_when_actual_exceeds_baseline(seeded_db, now, window):
    db_path, seeder = seeded_db
    pt = _test_price_table()
    # Actual on deep, baseline = fast. Actual > baseline → negative savings.
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="deep",
        provider="x",
        cost_usd="0.030",
        input_tokens=1_000_000,
        output_tokens=200_000,
    )
    with AnalyticsStore(db_path) as store:
        data = store.savings(window, baseline="fast", price_table=pt)
    # baseline = 1*1 + 0.2*5 = 2.0
    # actual_repriced = 1*15 + 0.2*75 = 30.0
    assert data["savings_usd"] == pytest.approx(-28.0)
    assert data["savings_pct"] == pytest.approx(-28.0 / 2.0)


def test_cost_decimal_precision_through_aggregate(seeded_db, now, window):
    """`/cost` aggregates in Decimal per spec §5.1 (not via SQL SUM).

    Seeds rows with stamped costs that float SUM would not handle exactly:
    `0.1 + 0.2 + 0.3` in floats is 0.5999999999999999, not 0.6. With Decimal
    aggregation, the result is exact at 6-decimal-place quantization.
    """
    db_path, seeder = seeded_db
    for cost in ("0.1", "0.2", "0.3"):
        seeder.insert_llm_call_completed(
            timestamp=now,
            model="x:y",
            provider="x",
            cost_usd=cost,
            input_tokens=10,
        )
    with AnalyticsStore(db_path) as store:
        data = store.cost(window, group_by="model")
    # If we aggregated in floats, this would assert 0.6 with epsilon drift.
    # Decimal aggregation quantized to 6 places gives exact 0.6.
    assert data[0]["cost_usd"] == pytest.approx(0.6, abs=1e-12)


def test_savings_decimal_precision_through_aggregate(seeded_db, now, window):
    """Spec test 19: summing many odd-decimal rows shouldn't drift."""
    db_path, seeder = seeded_db
    pt = _test_price_table()
    # 100 rows, each stamped at a long Decimal value that sums exactly to 1.00
    # if you sum Decimals (and floats are near-equal too at this scale).
    per_row = Decimal("0.01")
    for _ in range(100):
        seeder.insert_llm_call_completed(
            timestamp=now,
            model="fast",
            provider="x",
            cost_usd=str(per_row),
            input_tokens=1000,
            output_tokens=200,
        )
    with AnalyticsStore(db_path) as store:
        data = store.savings(window, baseline="deep", price_table=pt)
    # Stamped sum = 100 * 0.01 = 1.00 exactly.
    assert abs(data["actual_stamped_usd"] - 1.0) < 1e-9


def test_savings_cost_serialization_at_most_six_decimal_places(seeded_db, now, window):
    db_path, seeder = seeded_db
    pt = _test_price_table()
    # Force a re-priced result with more than 6 raw decimal places.
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="fast",
        provider="x",
        cost_usd="0.123",
        input_tokens=1_234_567,
        output_tokens=123_456,
    )
    with AnalyticsStore(db_path) as store:
        data = store.savings(window, baseline="deep", price_table=pt)
    # JSON number → at most 6 decimal places after quantization.
    assert isinstance(data["actual_repriced_usd"], float)
    text = format(data["actual_repriced_usd"], ".10f").rstrip("0")
    # Count places after the decimal point.
    decimals = text.split(".")[1] if "." in text else ""
    assert len(decimals) <= 6


def test_savings_stamped_vs_repriced_separation(seeded_db, now, window):
    """Stamped value written under version A; current table prices differently."""
    db_path, seeder = seeded_db
    # Use the actual DEFAULT_PRICE_TABLE so the model is known.
    pt = DEFAULT_PRICE_TABLE
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="anthropic:claude-haiku-4-5",
        provider="anthropic",
        cost_usd="999.99",  # stamped under a hypothetical old table — way off.
        input_tokens=1000,
        output_tokens=200,
    )
    with AnalyticsStore(db_path) as store:
        data = store.savings(
            window,
            baseline="anthropic:claude-sonnet-4-6",
            price_table=pt,
        )
    # Stamped preserves the historic value, even though it doesn't match current rates.
    assert data["actual_stamped_usd"] == pytest.approx(999.99)
    # Re-priced reflects the current table.
    assert data["actual_repriced_usd"] != pytest.approx(999.99)


# ---- /analytics/by_key ----------------------------------------------------


def test_by_key_rollup_per_gateway_key(seeded_db, now, window):
    db_path, seeder = seeded_db
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="anthropic:claude-sonnet-4-6",
        provider="anthropic",
        cost_usd="0.10",
        input_tokens=100,
        output_tokens=20,
        latency_ms=1000,
        gateway_key_id="gk_alpha",
        inbound_shape="openai",
    )
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="anthropic:claude-sonnet-4-6",
        provider="anthropic",
        cost_usd="0.05",
        input_tokens=50,
        output_tokens=10,
        latency_ms=1000,
        gateway_key_id="gk_alpha",
        inbound_shape="anthropic",
    )
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="anthropic:claude-haiku-4-5",
        provider="anthropic",
        cost_usd="0.02",
        input_tokens=30,
        output_tokens=5,
        latency_ms=200,
        gateway_key_id="gk_beta",
        inbound_shape="openai",
    )
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="anthropic:claude-haiku-4-5",
        provider="anthropic",
        cost_usd="0.03",
        input_tokens=30,
        output_tokens=5,
        latency_ms=400,
    )
    with AnalyticsStore(db_path) as store:
        data = store.by_key(window)
    by_id = {row["gateway_key_id"]: row for row in data}
    assert by_id["gk_alpha"]["cost_usd"] == pytest.approx(0.15)
    assert by_id["gk_alpha"]["call_count"] == 2
    shapes = {s["inbound_shape"]: s for s in by_id["gk_alpha"]["by_inbound_shape"]}
    assert shapes["openai"]["call_count"] == 1
    assert shapes["openai"]["cost_usd"] == pytest.approx(0.10)
    assert shapes["anthropic"]["call_count"] == 1
    # Null gateway_key (agent-loop traffic) rolls up under None.
    assert by_id[None]["call_count"] == 1
    # Sorted by cost DESC.
    assert data[0]["gateway_key_id"] == "gk_alpha"


def test_by_key_filter_exact_match(seeded_db, now, window):
    db_path, seeder = seeded_db
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="anthropic:claude-sonnet-4-6",
        provider="anthropic",
        cost_usd="0.10",
        gateway_key_id="gk_alpha",
        inbound_shape="openai",
    )
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="anthropic:claude-haiku-4-5",
        provider="anthropic",
        cost_usd="0.02",
        gateway_key_id="gk_beta",
        inbound_shape="openai",
    )
    with AnalyticsStore(db_path) as store:
        data = store.by_key(window, gateway_key="gk_alpha")
    assert len(data) == 1
    assert data[0]["gateway_key_id"] == "gk_alpha"


def test_by_key_filter_uses_parameterized_sql(seeded_db, now, window):
    """Even a hostile filter value goes through SQL placeholders, not interpolation."""
    db_path, seeder = seeded_db
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="anthropic:claude-sonnet-4-6",
        provider="anthropic",
        cost_usd="0.10",
        gateway_key_id="gk_alpha",
        inbound_shape="openai",
    )
    with AnalyticsStore(db_path) as store:
        # The store doesn't validate the shape (that's the HTTP layer's job);
        # it must safely pass any string through parameterized SQL.
        data = store.by_key(window, gateway_key="DROP TABLE events")
    assert data == []
    # And the original event row is still present.
    with AnalyticsStore(db_path) as store:
        data = store.by_key(window)
    assert len(data) == 1


# ---- /analytics/cost group_by user/team + filters (multi-user.md §5) -----


def test_cost_group_by_user(seeded_db, now, window):
    db_path, seeder = seeded_db
    # alice has two calls, bob one, plus one un-tagged agent-loop call.
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="x:y",
        provider="x",
        cost_usd="0.10",
        user_id="usr_alice",
        team_id="team_eng",
    )
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="x:y",
        provider="x",
        cost_usd="0.05",
        user_id="usr_alice",
        team_id="team_eng",
    )
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="x:y",
        provider="x",
        cost_usd="0.20",
        user_id="usr_bob",
        team_id="team_eng",
    )
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="x:y",
        provider="x",
        cost_usd="0.02",
    )
    with AnalyticsStore(db_path) as store:
        data = store.cost(window, group_by="user")
    by_user = {row["user_id"]: row for row in data}
    assert by_user["usr_alice"]["cost_usd"] == pytest.approx(0.15)
    assert by_user["usr_alice"]["call_count"] == 2
    assert by_user["usr_bob"]["call_count"] == 1
    assert None in by_user  # agent-loop traffic
    assert by_user[None]["cost_usd"] == pytest.approx(0.02)
    # Order: cost_usd DESC.
    assert data[0]["user_id"] == "usr_bob"


def test_cost_group_by_team(seeded_db, now, window):
    db_path, seeder = seeded_db
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="x:y",
        provider="x",
        cost_usd="0.10",
        user_id="usr_alice",
        team_id="team_eng",
    )
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="x:y",
        provider="x",
        cost_usd="0.50",
        user_id="usr_carol",
        team_id="team_sales",
    )
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="x:y",
        provider="x",
        cost_usd="0.03",
    )
    with AnalyticsStore(db_path) as store:
        data = store.cost(window, group_by="team")
    by_team = {row["team_id"]: row for row in data}
    assert by_team["team_sales"]["cost_usd"] == pytest.approx(0.50)
    assert by_team["team_eng"]["cost_usd"] == pytest.approx(0.10)
    assert None in by_team  # un-tagged traffic
    assert by_team[None]["cost_usd"] == pytest.approx(0.03)
    # Result ordered by cost DESC.
    assert data[0]["team_id"] == "team_sales"


def test_cost_filter_by_user(seeded_db, now, window):
    db_path, seeder = seeded_db
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="x:y",
        provider="x",
        cost_usd="0.10",
        user_id="usr_alice",
    )
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="x:y",
        provider="x",
        cost_usd="0.50",
        user_id="usr_bob",
    )
    with AnalyticsStore(db_path) as store:
        data = store.cost(window, group_by="model", user="usr_alice")
    assert len(data) == 1
    assert data[0]["cost_usd"] == pytest.approx(0.10)


def test_cost_filter_by_team(seeded_db, now, window):
    db_path, seeder = seeded_db
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="x:y",
        provider="x",
        cost_usd="0.10",
        team_id="team_eng",
    )
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="x:y",
        provider="x",
        cost_usd="0.50",
        team_id="team_sales",
    )
    with AnalyticsStore(db_path) as store:
        data = store.cost(window, group_by="model", team="team_eng")
    assert len(data) == 1
    assert data[0]["cost_usd"] == pytest.approx(0.10)


def test_cost_filter_user_and_team_combined(seeded_db, now, window):
    """`?user=alice&team=eng` — AND filter; rows must match both stamps."""
    db_path, seeder = seeded_db
    # Match.
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="x:y",
        provider="x",
        cost_usd="0.10",
        user_id="usr_alice",
        team_id="team_eng",
    )
    # Wrong team.
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="x:y",
        provider="x",
        cost_usd="0.99",
        user_id="usr_alice",
        team_id="team_sales",
    )
    # Wrong user.
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="x:y",
        provider="x",
        cost_usd="0.99",
        user_id="usr_bob",
        team_id="team_eng",
    )
    with AnalyticsStore(db_path) as store:
        data = store.cost(window, group_by="session", user="usr_alice", team="team_eng")
    assert len(data) == 1
    assert data[0]["cost_usd"] == pytest.approx(0.10)


def test_cost_filter_by_user_with_session_group_by(seeded_db, now, window):
    """Spec §5.3 example: `?user=alice&group_by=session` works."""
    db_path, seeder = seeded_db
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="x:y",
        provider="x",
        cost_usd="0.10",
        user_id="usr_alice",
        session_id="sess_a",
    )
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="x:y",
        provider="x",
        cost_usd="0.05",
        user_id="usr_alice",
        session_id="sess_b",
    )
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="x:y",
        provider="x",
        cost_usd="0.99",
        user_id="usr_bob",
        session_id="sess_c",
    )
    with AnalyticsStore(db_path) as store:
        data = store.cost(window, group_by="session", user="usr_alice")
    sessions = {row["session_id"] for row in data}
    assert sessions == {"sess_a", "sess_b"}


def test_cost_filter_uses_parameterized_sql(seeded_db, now, window):
    """Hostile filter values hit a SQL placeholder, never string-interp."""
    db_path, seeder = seeded_db
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="x:y",
        provider="x",
        cost_usd="0.10",
        user_id="usr_alice",
    )
    with AnalyticsStore(db_path) as store:
        # Store doesn't validate shape; that's the HTTP layer's job. Any
        # string is passed through safely via parameterized SQL.
        data = store.cost(window, group_by="model", user="DROP TABLE events")
    assert data == []
    # Original row is still present.
    with AnalyticsStore(db_path) as store:
        data = store.cost(window, group_by="model")
    assert len(data) == 1


# ---- /analytics/by_team --------------------------------------------------


def test_by_team_rollup(seeded_db, now, window):
    """Three users in two teams + one un-tagged call (multi-user.md §12.1.4)."""
    db_path, seeder = seeded_db
    # team_eng: alice 2 calls, bob 1 call.
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="x:y",
        provider="x",
        cost_usd="0.10",
        user_id="usr_alice",
        team_id="team_eng",
    )
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="x:y",
        provider="x",
        cost_usd="0.05",
        user_id="usr_alice",
        team_id="team_eng",
    )
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="x:y",
        provider="x",
        cost_usd="0.20",
        user_id="usr_bob",
        team_id="team_eng",
    )
    # team_sales: carol alone.
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="x:y",
        provider="x",
        cost_usd="1.00",
        user_id="usr_carol",
        team_id="team_sales",
    )
    # Un-tagged agent-loop traffic.
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="x:y",
        provider="x",
        cost_usd="0.02",
    )
    with AnalyticsStore(db_path) as store:
        data = store.by_team(window)
    by_team = {row["team_id"]: row for row in data}
    # team_sales is most expensive → first.
    assert data[0]["team_id"] == "team_sales"
    sales = by_team["team_sales"]
    assert sales["cost_usd"] == pytest.approx(1.00)
    assert sales["call_count"] == 1
    assert sales["user_count"] == 1
    assert sales["by_user"][0]["user_id"] == "usr_carol"
    # team_eng totals match sum-of-users.
    eng = by_team["team_eng"]
    assert eng["cost_usd"] == pytest.approx(0.35)
    assert eng["call_count"] == 3
    assert eng["user_count"] == 2
    by_user = {row["user_id"]: row for row in eng["by_user"]}
    assert by_user["usr_alice"]["cost_usd"] == pytest.approx(0.15)
    assert by_user["usr_alice"]["call_count"] == 2
    assert by_user["usr_bob"]["cost_usd"] == pytest.approx(0.20)
    # bob spent more than alice → bob first in the sub-array.
    assert eng["by_user"][0]["user_id"] == "usr_bob"
    # Un-tagged null bucket present; user_count==0 (null is not an identity).
    assert by_team[None]["cost_usd"] == pytest.approx(0.02)
    assert by_team[None]["user_count"] == 0
    assert by_team[None]["by_user"][0]["user_id"] is None


def test_by_team_filter_exact_match(seeded_db, now, window):
    db_path, seeder = seeded_db
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="x:y",
        provider="x",
        cost_usd="0.10",
        user_id="usr_alice",
        team_id="team_eng",
    )
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="x:y",
        provider="x",
        cost_usd="0.99",
        user_id="usr_bob",
        team_id="team_sales",
    )
    with AnalyticsStore(db_path) as store:
        data = store.by_team(window, team="team_eng")
    assert len(data) == 1
    assert data[0]["team_id"] == "team_eng"


def test_by_team_filter_uses_parameterized_sql(seeded_db, now, window):
    """Hostile filter passes safely through placeholders."""
    db_path, seeder = seeded_db
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="x:y",
        provider="x",
        cost_usd="0.10",
        user_id="usr_alice",
        team_id="team_eng",
    )
    with AnalyticsStore(db_path) as store:
        data = store.by_team(window, team="DROP TABLE events")
    assert data == []
    with AnalyticsStore(db_path) as store:
        data = store.by_team(window)
    assert len(data) == 1


def test_by_team_sum_of_users_equals_team_total(seeded_db, now, window):
    """Property test (multi-user.md §12.2): sum of by_user equals team total."""
    db_path, seeder = seeded_db
    for cost, user in [
        ("0.1234", "usr_a"),
        ("0.5678", "usr_b"),
        ("0.9012", "usr_a"),
        ("0.3456", "usr_c"),
    ]:
        seeder.insert_llm_call_completed(
            timestamp=now,
            model="x:y",
            provider="x",
            cost_usd=cost,
            user_id=user,
            team_id="team_eng",
        )
    with AnalyticsStore(db_path) as store:
        data = store.by_team(window)
    team = data[0]
    user_sum = sum(u["cost_usd"] for u in team["by_user"])
    assert team["cost_usd"] == pytest.approx(user_sum, abs=1e-9)


def test_by_team_null_bucket_present_when_only_untagged(seeded_db, now, window):
    """Pre-v1 keys + agent-loop traffic all fold into team_id=null."""
    db_path, seeder = seeded_db
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="x:y",
        provider="x",
        cost_usd="0.10",
    )
    seeder.insert_llm_call_completed(
        timestamp=now,
        model="x:y",
        provider="x",
        cost_usd="0.05",
        gateway_key_id="gk_legacy",  # pre-v1: no user_id/team_id.
    )
    with AnalyticsStore(db_path) as store:
        data = store.by_team(window)
    assert len(data) == 1
    assert data[0]["team_id"] is None
    assert data[0]["cost_usd"] == pytest.approx(0.15)
    assert data[0]["user_count"] == 0


# ---- Cross-cutting --------------------------------------------------------


def test_invalid_group_by_does_not_reach_sql(seeded_db, window):
    """Spec test 17: SQL injection attempts on whitelisted params are rejected."""
    db_path, _ = seeded_db
    with AnalyticsStore(db_path) as store:
        with pytest.raises(InvalidGroupByError):
            store.cost(window, group_by="DROP TABLE events")


def test_invalid_order_does_not_reach_sql(seeded_db):
    db_path, _ = seeded_db
    with AnalyticsStore(db_path) as store:
        with pytest.raises(InvalidOrderError):
            store.sessions(order="; DELETE FROM sessions")
