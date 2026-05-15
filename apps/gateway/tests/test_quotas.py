"""Tests for `metis_gateway.quotas` — QuotaTracker, QuotaStatus, and the
gateway-side `enforce_quotas` helper that drives soft alerts and hard
breakers per multi-user.md §5 / gateway.md §6.4."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import msgspec
import pytest
from metis_core.canonical.ids import next_monotonic_ulid
from metis_core.events.bus import EventBus
from metis_core.events.envelope import Actor
from metis_core.events.payloads import LLMCallCompleted, make_event
from metis_core.trace.store import TraceStore
from metis_gateway.auth import GatewayKey, Identity
from metis_gateway.quotas import (
    QuotaTracker,
    RequestQuotaCache,
    applicable_quotas,
    enforce_quotas,
)


def _emit_completed_event(
    *,
    bus: EventBus,
    cost_usd: float,
    gateway_key_id: str | None = None,
    user_id: str | None = None,
    team_id: str | None = None,
    timestamp: datetime | None = None,
) -> None:
    payload = LLMCallCompleted(
        model="anthropic:claude-haiku-4-5",
        provider="anthropic",
        input_tokens=10,
        output_tokens=5,
        cached_input_tokens=0,
        cache_creation_input_tokens=0,
        cost_usd=cost_usd,
        pricing_version="test",
        latency_ms=42,
        stop_reason="end_turn",
        produced_tool_calls=0,
        produced_thinking_blocks=0,
        gateway_key_id=gateway_key_id,
        inbound_shape="openai",
        user_id=user_id,
        team_id=team_id,
    )
    event = make_event(
        type="llm.call_completed",
        session_id=f"sess_{next_monotonic_ulid()}",
        actor=Actor.AGENT,
        payload=payload,
        timestamp=timestamp or datetime.now(UTC),
    )
    bus.emit(event)


@pytest.fixture
async def trace_db(tmp_path: Path) -> tuple[Path, EventBus]:
    """Build a fresh TraceStore + EventBus, seeded by callers as needed."""
    db_file = tmp_path / "quota.db"
    bus = EventBus()
    bus.start()
    trace = TraceStore(db_file)
    trace.attach_to(bus)
    try:
        yield db_file, bus
    finally:
        await bus.drain()
        await bus.stop()
        trace.close()


# ---------------------------------------------------------------------------
# QuotaStatus / QuotaTracker
# ---------------------------------------------------------------------------


async def test_quota_status_reports_used_cap_and_percentage(trace_db) -> None:
    db_file, bus = trace_db
    _emit_completed_event(bus=bus, cost_usd=0.10, gateway_key_id="gk_alpha")
    _emit_completed_event(bus=bus, cost_usd=0.20, gateway_key_id="gk_alpha")
    _emit_completed_event(bus=bus, cost_usd=0.99, gateway_key_id="gk_other")
    await bus.drain()

    tracker = QuotaTracker(db_file)
    try:
        status = tracker.status(
            identity_kind="key",
            identity_value="gk_alpha",
            window="daily",
            cap_usd=Decimal("1.00"),
        )
        assert status.used_usd == Decimal("0.30")
        assert status.cap_usd == Decimal("1.00")
        assert status.percentage == pytest.approx(0.30)
        assert status.is_exceeded is False
        assert status.remaining_usd() == Decimal("0.70")
    finally:
        tracker.close()


async def test_quota_status_returns_none_percentage_when_no_cap(trace_db) -> None:
    db_file, bus = trace_db
    _emit_completed_event(bus=bus, cost_usd=2.00, gateway_key_id="gk_uncapped")
    await bus.drain()

    tracker = QuotaTracker(db_file)
    try:
        status = tracker.status(
            identity_kind="key",
            identity_value="gk_uncapped",
            window="daily",
            cap_usd=None,
        )
        assert status.cap_usd is None
        assert status.percentage is None
        assert status.is_exceeded is False
        assert status.remaining_usd() is None
        # Spend is still surfaced even when uncapped — useful for dashboards.
        assert status.used_usd == Decimal("2.00")
    finally:
        tracker.close()


async def test_quota_status_filters_by_user_and_team_dimensions(trace_db) -> None:
    db_file, bus = trace_db
    _emit_completed_event(bus=bus, cost_usd=0.40, user_id="alice", team_id="eng")
    _emit_completed_event(bus=bus, cost_usd=0.60, user_id="alice", team_id="eng")
    _emit_completed_event(bus=bus, cost_usd=0.30, user_id="bob", team_id="eng")
    _emit_completed_event(bus=bus, cost_usd=0.10, user_id="alice", team_id="ops")
    await bus.drain()

    tracker = QuotaTracker(db_file)
    try:
        user_status = tracker.status(
            identity_kind="user",
            identity_value="alice",
            window="monthly",
            cap_usd=Decimal("2.00"),
        )
        # alice spend across both teams.
        assert user_status.used_usd == Decimal("1.10")
        team_status = tracker.status(
            identity_kind="team",
            identity_value="eng",
            window="monthly",
            cap_usd=Decimal("2.00"),
        )
        # eng spend across both users.
        assert team_status.used_usd == Decimal("1.30")
    finally:
        tracker.close()


async def test_quota_status_clamps_remaining_to_zero_when_overshot(trace_db) -> None:
    db_file, bus = trace_db
    _emit_completed_event(bus=bus, cost_usd=1.50, gateway_key_id="gk_overshot")
    await bus.drain()

    tracker = QuotaTracker(db_file)
    try:
        status = tracker.status(
            identity_kind="key",
            identity_value="gk_overshot",
            window="daily",
            cap_usd=Decimal("1.00"),
        )
        assert status.is_exceeded is True
        # remaining_usd never returns negative — routing predicate compares
        # against thresholds that should treat "over cap" as zero headroom.
        assert status.remaining_usd() == Decimal("0")
    finally:
        tracker.close()


async def test_request_quota_cache_memoizes(trace_db) -> None:
    """The cache must hand back the same status for repeated lookups so the
    harness doesn't re-issue SQL once per cap check."""
    db_file, bus = trace_db
    _emit_completed_event(bus=bus, cost_usd=0.25, gateway_key_id="gk_cached")
    await bus.drain()

    tracker = QuotaTracker(db_file)
    cache = RequestQuotaCache(tracker)
    try:
        first = cache.status(
            identity_kind="key",
            identity_value="gk_cached",
            window="daily",
            cap_usd=Decimal("1.00"),
        )
        second = cache.status(
            identity_kind="key",
            identity_value="gk_cached",
            window="daily",
            cap_usd=Decimal("1.00"),
        )
        assert first is second
    finally:
        tracker.close()


# ---------------------------------------------------------------------------
# applicable_quotas / enforce_quotas
# ---------------------------------------------------------------------------


def test_applicable_quotas_skips_unset_caps(tmp_path: Path) -> None:
    key_no_cap = GatewayKey(
        key_id="gk_no_cap",
        secret_hash="x" * 64,
        name="n",
        workspace_path=str(tmp_path),
    )
    assert applicable_quotas(key_no_cap) == []

    key_daily_only = GatewayKey(
        key_id="gk_daily",
        secret_hash="x" * 64,
        name="n",
        workspace_path=str(tmp_path),
        daily_cap_usd=Decimal("1.0"),
    )
    quotas = applicable_quotas(key_daily_only)
    assert quotas == [("key", "gk_daily", "daily", Decimal("1.0"))]


async def test_enforce_quotas_returns_none_under_threshold(trace_db, tmp_path) -> None:
    db_file, bus = trace_db
    _emit_completed_event(bus=bus, cost_usd=0.10, gateway_key_id="gk_safe")
    await bus.drain()

    key = GatewayKey(
        key_id="gk_safe",
        secret_hash="x" * 64,
        name="safe",
        workspace_path=str(tmp_path),
        daily_cap_usd=Decimal("1.00"),
    )
    identity = Identity(
        gateway_key_id=key.key_id,
        workspace_path=key.workspace_path,
    )
    tracker = QuotaTracker(db_file)
    try:
        cache = RequestQuotaCache(tracker)
        verdict = enforce_quotas(
            bus=bus, cache=cache, key=key, identity=identity, inbound_shape="openai"
        )
        assert verdict is None
        await bus.drain()
        # No alert emitted at 10% spend.
        assert _quota_alert_count(db_file) == 0
    finally:
        tracker.close()


async def test_enforce_quotas_emits_warning_alert_at_80_percent(trace_db, tmp_path) -> None:
    db_file, bus = trace_db
    _emit_completed_event(bus=bus, cost_usd=0.85, gateway_key_id="gk_warn")
    await bus.drain()

    key = GatewayKey(
        key_id="gk_warn",
        secret_hash="x" * 64,
        name="warn",
        workspace_path=str(tmp_path),
        daily_cap_usd=Decimal("1.00"),
    )
    identity = Identity(gateway_key_id=key.key_id, workspace_path=key.workspace_path)
    tracker = QuotaTracker(db_file)
    try:
        cache = RequestQuotaCache(tracker)
        verdict = enforce_quotas(
            bus=bus, cache=cache, key=key, identity=identity, inbound_shape="openai"
        )
        assert verdict is None  # Soft alert only.
        await bus.drain()
        alerts = _read_quota_alerts(db_file)
        assert len(alerts) == 1
        assert alerts[0]["severity"] == "warning"
        assert alerts[0]["scope"] == "key_daily"
        assert alerts[0]["gateway_key_id"] == "gk_warn"
        assert alerts[0]["percentage"] == pytest.approx(0.85)
    finally:
        tracker.close()


async def test_enforce_quotas_emits_critical_alert_at_95_percent(trace_db, tmp_path) -> None:
    db_file, bus = trace_db
    _emit_completed_event(bus=bus, cost_usd=0.96, gateway_key_id="gk_crit")
    await bus.drain()

    key = GatewayKey(
        key_id="gk_crit",
        secret_hash="x" * 64,
        name="crit",
        workspace_path=str(tmp_path),
        daily_cap_usd=Decimal("1.00"),
    )
    identity = Identity(gateway_key_id=key.key_id, workspace_path=key.workspace_path)
    tracker = QuotaTracker(db_file)
    try:
        cache = RequestQuotaCache(tracker)
        verdict = enforce_quotas(
            bus=bus, cache=cache, key=key, identity=identity, inbound_shape="openai"
        )
        assert verdict is None
        await bus.drain()
        alerts = _read_quota_alerts(db_file)
        assert len(alerts) == 1
        assert alerts[0]["severity"] == "critical"
    finally:
        tracker.close()


async def test_enforce_quotas_returns_quota_exceeded_at_hard_cap(trace_db, tmp_path) -> None:
    db_file, bus = trace_db
    _emit_completed_event(bus=bus, cost_usd=1.50, gateway_key_id="gk_blocked", team_id="eng")
    await bus.drain()

    key = GatewayKey(
        key_id="gk_blocked",
        secret_hash="x" * 64,
        name="blocked",
        workspace_path=str(tmp_path),
        daily_cap_usd=Decimal("1.00"),
        team_id="eng",
    )
    identity = Identity(
        gateway_key_id=key.key_id,
        workspace_path=key.workspace_path,
        team_id="eng",
    )
    tracker = QuotaTracker(db_file)
    try:
        cache = RequestQuotaCache(tracker)
        verdict = enforce_quotas(
            bus=bus, cache=cache, key=key, identity=identity, inbound_shape="openai"
        )
        assert verdict is not None
        assert verdict.scope == "key_daily"
        assert verdict.identity_kind == "key"
        assert verdict.current_usd == Decimal("1.50")
        assert verdict.limit_usd == Decimal("1.00")
        await bus.drain()
        # Hard cap fires `gateway.quota_exceeded`, NOT `quota.alert`.
        assert _quota_alert_count(db_file) == 0
        rejections = _read_quota_exceeded(db_file)
        assert len(rejections) == 1
        assert rejections[0]["scope"] == "key_daily"
        # Decimal-as-string is normalized by msgspec (trailing zeros trimmed),
        # so compare via Decimal to avoid representation drift.
        assert Decimal(rejections[0]["current_usd"]) == Decimal("1.50")
        assert Decimal(rejections[0]["limit_usd"]) == Decimal("1.00")
        assert rejections[0]["inbound_shape"] == "openai"
        assert rejections[0]["gateway_key_id"] == "gk_blocked"
        assert rejections[0]["team_id"] == "eng"
    finally:
        tracker.close()


async def test_enforce_quotas_no_caps_is_noop(trace_db, tmp_path) -> None:
    db_file, bus = trace_db
    key = GatewayKey(
        key_id="gk_uncapped",
        secret_hash="x" * 64,
        name="uncapped",
        workspace_path=str(tmp_path),
    )
    identity = Identity(gateway_key_id=key.key_id, workspace_path=key.workspace_path)
    tracker = QuotaTracker(db_file)
    try:
        cache = RequestQuotaCache(tracker)
        verdict = enforce_quotas(
            bus=bus, cache=cache, key=key, identity=identity, inbound_shape="openai"
        )
        assert verdict is None
        await bus.drain()
        assert _quota_alert_count(db_file) == 0
        assert _read_quota_exceeded(db_file) == []
    finally:
        tracker.close()


async def test_enforce_quotas_alert_fires_once_per_request(trace_db, tmp_path) -> None:
    """Idempotency: a single enforce_quotas call emits at most one alert per
    scope, not one per cap-check inside the function."""
    db_file, bus = trace_db
    # Both daily (85%) AND monthly (90%) above warning threshold.
    _emit_completed_event(bus=bus, cost_usd=0.85, gateway_key_id="gk_dual")
    await bus.drain()

    key = GatewayKey(
        key_id="gk_dual",
        secret_hash="x" * 64,
        name="dual",
        workspace_path=str(tmp_path),
        daily_cap_usd=Decimal("1.00"),
        monthly_cap_usd=Decimal("0.90"),
    )
    identity = Identity(gateway_key_id=key.key_id, workspace_path=key.workspace_path)
    tracker = QuotaTracker(db_file)
    try:
        cache = RequestQuotaCache(tracker)
        verdict = enforce_quotas(
            bus=bus, cache=cache, key=key, identity=identity, inbound_shape="openai"
        )
        assert verdict is None
        await bus.drain()
        alerts = _read_quota_alerts(db_file)
        # Two alerts because they're distinct scopes (key_daily + key_monthly);
        # the idempotency contract is "one per scope per request".
        assert len(alerts) == 2
        scopes = {a["scope"] for a in alerts}
        assert scopes == {"key_daily", "key_monthly"}
    finally:
        tracker.close()


# ---------------------------------------------------------------------------
# Trace-store helpers
# ---------------------------------------------------------------------------


def _read_quota_alerts(db_file: Path) -> list[dict]:
    return _read_events_of_type(db_file, "quota.alert")


def _read_quota_exceeded(db_file: Path) -> list[dict]:
    return _read_events_of_type(db_file, "gateway.quota_exceeded")


def _read_events_of_type(db_file: Path, event_type: str) -> list[dict]:
    conn = sqlite3.connect(db_file)
    try:
        rows = conn.execute(
            "SELECT payload_json FROM events WHERE type = ? ORDER BY id",
            (event_type,),
        ).fetchall()
    finally:
        conn.close()
    return [json.loads(r[0]) for r in rows]


def _quota_alert_count(db_file: Path) -> int:
    return len(_read_quota_alerts(db_file))


# Keep msgspec referenced so the import isn't flagged as unused if the test
# file ever loses its make_event() helper — make_event needs msgspec at
# import time even when it's used implicitly through the typed payload above.
_ = msgspec
