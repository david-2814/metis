"""Tests for the audit log reader / exporter.

See `docs/specs/audit-log.md §14`.
"""

from __future__ import annotations

import csv
import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from metis.core.analytics.windows import TimeWindow
from metis.core.audit import (
    AUDIT_EVENT_TYPES,
    AuditLog,
    export_audit_events,
    is_audit_event,
)
from metis.core.events.envelope import Actor
from metis.core.events.payloads import (
    PAYLOAD_REGISTRY,
    GatewayKeyIssued,
    GatewayKeyRevoked,
    GatewayKeyRotated,
    GatewayQuotaExceeded,
    LLMCallCompleted,
    MemoryEviction,
    PatternEvicted,
    QuotaAlert,
    RoutingPolicyInvalid,
    SessionCreated,
    ToolConfirmationResolved,
    make_event,
)
from metis.core.trace.store import TraceStore

# ---- Fixtures ----------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "trace.db"


def _now_offset(seconds: int) -> datetime:
    return datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC) + timedelta(seconds=seconds)


def _window(start_seconds: int = -3600, end_seconds: int = 3600) -> TimeWindow:
    """Default window: ±1 hour around our fixed test timestamp."""
    return TimeWindow(start=_now_offset(start_seconds), end=_now_offset(end_seconds))


def _build_one_of_each_audit_event() -> list:
    """One payload + envelope per type in AUDIT_EVENT_TYPES."""
    return [
        (
            "gateway.key_issued",
            Actor.SYSTEM,
            GatewayKeyIssued(
                gateway_key_id="gk_01J",
                name="alice-prod",
                workspace_path="/srv/alice",
                issued_at=_now_offset(10),
                user_id="usr_01J",
                team_id="team_01J",
                allowed_models=["anthropic:claude-haiku-4-5"],
                daily_cap_usd=Decimal("5.00"),
                monthly_cap_usd=None,
            ),
        ),
        (
            "gateway.key_revoked",
            Actor.SYSTEM,
            GatewayKeyRevoked(
                gateway_key_id="gk_01J",
                revoked_at=_now_offset(20),
                reason="admin_revoke",
            ),
        ),
        (
            "gateway.key_rotated",
            Actor.SYSTEM,
            GatewayKeyRotated(
                old_gateway_key_id="gk_01J",
                new_gateway_key_id="gk_02J",
                grace_period_until=_now_offset(86400),
                workspace_path="/srv/alice",
                user_id="usr_01J",
                team_id="team_01J",
            ),
        ),
        (
            "gateway.quota_exceeded",
            Actor.SYSTEM,
            GatewayQuotaExceeded(
                scope="team_daily",
                current_usd=Decimal("50.00"),
                limit_usd=Decimal("50.00"),
                inbound_shape="openai",
                gateway_key_id="gk_01J",
                user_id="usr_01J",
                team_id="team_01J",
            ),
        ),
        (
            "quota.alert",
            Actor.SYSTEM,
            QuotaAlert(
                scope="user_daily",
                severity="warning",
                current_usd=Decimal("8.00"),
                limit_usd=Decimal("10.00"),
                percentage=0.8,
                gateway_key_id="gk_01J",
                user_id="usr_01J",
                team_id=None,
            ),
        ),
        (
            "routing.policy_invalid",
            Actor.SYSTEM,
            RoutingPolicyInvalid(
                policy_path="/srv/alice/.metis/routing.yaml",
                errors=["unknown predicate `foo`"],
                using_last_known_good=True,
            ),
        ),
        (
            "memory.eviction",
            Actor.SYSTEM,
            MemoryEviction(
                file="MEMORY.md",
                trigger="size_cap_exceeded",
                entries_evicted=3,
                size_before_bytes=2200,
                size_after_bytes=1800,
            ),
        ),
        (
            "pattern.evicted",
            Actor.SYSTEM,
            PatternEvicted(
                trigger="hard_cap_evict",
                fingerprints_before=10000,
                fingerprints_after=9000,
                outcomes_before=20000,
                outcomes_after=18000,
                entries_evicted=2000,
                oldest_evicted_age_days=180.0,
            ),
        ),
        (
            "tool.confirmation_resolved",
            Actor.USER,
            ToolConfirmationResolved(
                tool_use_id="tu_01J",
                confirmation_request_id="cr_01J",
                decision="allow",
                scope="session",
                responding_client_attach_token=None,
            ),
        ),
    ]


def _seed_audit_events(path: Path, *, session_id: str = "sess_audit") -> int:
    """Seed the trace DB with one of each audit event type. Returns count."""
    store = TraceStore(path)
    try:
        n = 0
        for offset, (type_, actor, payload) in enumerate(_build_one_of_each_audit_event()):
            store.write(
                make_event(
                    type=type_,
                    session_id=session_id,
                    actor=actor,
                    payload=payload,
                    timestamp=_now_offset(offset),
                )
            )
            n += 1
        return n
    finally:
        store.close()


# ---- is_audit_event / catalog cross-check ------------------------------


def test_is_audit_event_membership():
    """Every type in AUDIT_EVENT_TYPES is recognized; every other registered
    type is not."""
    for t in AUDIT_EVENT_TYPES:
        assert is_audit_event(t), f"{t} should be audit-relevant"
    for t in PAYLOAD_REGISTRY:
        if t not in AUDIT_EVENT_TYPES:
            assert not is_audit_event(t), f"{t} should not be audit-relevant"


def test_audit_event_types_subset_of_registry():
    """AUDIT_EVENT_TYPES must be a subset of PAYLOAD_REGISTRY — defends
    against typos in the audit set producing silent no-op queries."""
    missing = AUDIT_EVENT_TYPES - set(PAYLOAD_REGISTRY)
    assert missing == set(), f"audit types not in PAYLOAD_REGISTRY: {missing}"


# ---- Query semantics ---------------------------------------------------


def test_query_returns_only_audit_events_in_window(db_path: Path):
    """Audit events in the window are returned; operational events are
    filtered out by type; out-of-window events are filtered by timestamp."""
    store = TraceStore(db_path)
    try:
        # Audit event in window.
        store.write(
            make_event(
                type="gateway.key_issued",
                session_id="sess_x",
                actor=Actor.SYSTEM,
                payload=GatewayKeyIssued(
                    gateway_key_id="gk_in",
                    name="in",
                    workspace_path="/w",
                    issued_at=_now_offset(0),
                ),
                timestamp=_now_offset(0),
            )
        )
        # Operational event in window (should NOT appear).
        store.write(
            make_event(
                type="llm.call_completed",
                session_id="sess_x",
                actor=Actor.AGENT,
                payload=LLMCallCompleted(
                    model="anthropic:claude-haiku-4-5",
                    provider="anthropic",
                    input_tokens=10,
                    output_tokens=10,
                    cached_input_tokens=0,
                    cache_creation_input_tokens=0,
                    cost_usd=0.001,
                    pricing_version="v1",
                    latency_ms=100,
                    stop_reason="end_turn",
                    produced_tool_calls=0,
                    produced_thinking_blocks=0,
                ),
                timestamp=_now_offset(0),
            )
        )
        # Audit event OUT of window (should NOT appear).
        store.write(
            make_event(
                type="gateway.key_revoked",
                session_id="sess_x",
                actor=Actor.SYSTEM,
                payload=GatewayKeyRevoked(
                    gateway_key_id="gk_out",
                    revoked_at=_now_offset(99999),
                    reason="admin_revoke",
                ),
                timestamp=_now_offset(99999),  # outside the ±1h window
            )
        )
    finally:
        store.close()

    trace = TraceStore(db_path)
    try:
        audit = AuditLog(trace)
        events = list(audit.query(window=_window()))
    finally:
        trace.close()

    assert len(events) == 1
    assert events[0].type == "gateway.key_issued"
    assert events[0].payload["gateway_key_id"] == "gk_in"


def test_query_with_explicit_event_types_intersects_with_audit_set(db_path: Path):
    """Passing a non-audit type silently drops it (spec §8.1, lax filter)."""
    _seed_audit_events(db_path)
    trace = TraceStore(db_path)
    try:
        audit = AuditLog(trace)
        # Mix one audit type with one operational type.
        events = list(
            audit.query(
                window=_window(),
                event_types={"gateway.key_issued", "llm.call_completed"},
            )
        )
    finally:
        trace.close()

    assert len(events) == 1
    assert events[0].type == "gateway.key_issued"


def test_query_with_only_non_audit_types_returns_empty(db_path: Path):
    _seed_audit_events(db_path)
    trace = TraceStore(db_path)
    try:
        audit = AuditLog(trace)
        events = list(audit.query(window=_window(), event_types={"llm.call_completed"}))
    finally:
        trace.close()

    assert events == []


def test_query_orders_by_event_id_ascending(db_path: Path):
    """ULID-sortable means timestamp-ascending per audit-log.md §7.3."""
    _seed_audit_events(db_path)
    trace = TraceStore(db_path)
    try:
        audit = AuditLog(trace)
        events = list(audit.query(window=_window()))
    finally:
        trace.close()

    ids = [e.id for e in events]
    assert ids == sorted(ids), "audit events must be ordered by id ascending"


# ---- JSONL round-trip / determinism ------------------------------------


def test_jsonl_export_round_trip(db_path: Path, tmp_path: Path):
    """Emit one of each audit type, export, re-parse, verify shape."""
    seeded = _seed_audit_events(db_path)

    dest = tmp_path / "out.jsonl"
    result = export_audit_events(db_path, dest, window=_window(), format="jsonl")

    assert result.event_count == seeded
    assert result.format == "jsonl"
    assert dest.exists()
    assert result.byte_count == dest.stat().st_size

    lines = dest.read_text(encoding="utf-8").splitlines()
    assert len(lines) == seeded
    parsed = [json.loads(line) for line in lines]

    # Every seeded type appears (defends against silent drop).
    types_in_file = {row["type"] for row in parsed}
    seeded_types = {t for (t, _, _) in _build_one_of_each_audit_event()}
    assert types_in_file == seeded_types
    # Every type in the file is in the audit subset.
    assert types_in_file <= set(AUDIT_EVENT_TYPES)

    # Each row has the canonical envelope keys in canonical order.
    expected_keys = (
        "id",
        "timestamp",
        "session_id",
        "turn_id",
        "parent_event_id",
        "type",
        "actor",
        "sensitivity",
        "payload",
    )
    for row in parsed:
        assert tuple(row.keys()) == expected_keys


def test_jsonl_export_is_deterministic(db_path: Path, tmp_path: Path):
    """Same input → byte-identical output."""
    _seed_audit_events(db_path)

    out_a = tmp_path / "a.jsonl"
    out_b = tmp_path / "b.jsonl"
    export_audit_events(db_path, out_a, window=_window(), format="jsonl")
    export_audit_events(db_path, out_b, window=_window(), format="jsonl")

    digest_a = hashlib.sha256(out_a.read_bytes()).hexdigest()
    digest_b = hashlib.sha256(out_b.read_bytes()).hexdigest()
    assert digest_a == digest_b


def test_jsonl_decimal_fields_serialize_as_strings(db_path: Path, tmp_path: Path):
    """Decimal fields (gateway.key_issued.daily_cap_usd, etc.) serialize as
    JSON strings per the canonical-message-format §6.4 convention."""
    _seed_audit_events(db_path)

    dest = tmp_path / "out.jsonl"
    export_audit_events(db_path, dest, window=_window(), format="jsonl")

    lines = dest.read_text(encoding="utf-8").splitlines()
    parsed = {row["type"]: row for row in (json.loads(line) for line in lines)}
    payload = parsed["gateway.key_issued"]["payload"]
    assert payload["daily_cap_usd"] == "5.00"

    quota = parsed["gateway.quota_exceeded"]["payload"]
    assert quota["current_usd"] == "50.00"
    assert quota["limit_usd"] == "50.00"


# ---- CSV round-trip / determinism --------------------------------------


def test_csv_export_round_trip(db_path: Path, tmp_path: Path):
    seeded = _seed_audit_events(db_path)

    dest = tmp_path / "out.csv"
    result = export_audit_events(db_path, dest, window=_window(), format="csv")

    assert result.event_count == seeded
    assert result.format == "csv"

    with dest.open("r", encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))

    header = rows[0]
    assert header == [
        "id",
        "timestamp",
        "session_id",
        "turn_id",
        "parent_event_id",
        "type",
        "actor",
        "sensitivity",
        "payload_json",
    ]
    body = rows[1:]
    assert len(body) == seeded

    # Each `payload_json` column re-parses to a dict.
    for row in body:
        payload = json.loads(row[-1])
        assert isinstance(payload, dict)


def test_csv_export_is_deterministic(db_path: Path, tmp_path: Path):
    _seed_audit_events(db_path)

    out_a = tmp_path / "a.csv"
    out_b = tmp_path / "b.csv"
    export_audit_events(db_path, out_a, window=_window(), format="csv")
    export_audit_events(db_path, out_b, window=_window(), format="csv")

    assert (
        hashlib.sha256(out_a.read_bytes()).hexdigest()
        == hashlib.sha256(out_b.read_bytes()).hexdigest()
    )


# ---- Edge cases --------------------------------------------------------


def test_empty_window_jsonl_is_zero_bytes(db_path: Path, tmp_path: Path):
    """Empty window → zero-byte JSONL file, event_count=0, no exception."""
    _seed_audit_events(db_path)

    empty_window = TimeWindow(
        start=_now_offset(86400 * 30),
        end=_now_offset(86400 * 31),
    )
    dest = tmp_path / "empty.jsonl"
    result = export_audit_events(db_path, dest, window=empty_window, format="jsonl")

    assert result.event_count == 0
    assert result.oldest_event_id is None
    assert result.newest_event_id is None
    assert dest.exists()
    assert dest.read_bytes() == b""


def test_empty_window_csv_has_header_only(db_path: Path, tmp_path: Path):
    """Empty window CSV produces only the header row."""
    _seed_audit_events(db_path)

    empty_window = TimeWindow(
        start=_now_offset(86400 * 30),
        end=_now_offset(86400 * 31),
    )
    dest = tmp_path / "empty.csv"
    result = export_audit_events(db_path, dest, window=empty_window, format="csv")

    assert result.event_count == 0
    content = dest.read_text(encoding="utf-8")
    assert content == (
        "id,timestamp,session_id,turn_id,parent_event_id,type,actor,sensitivity,payload_json\n"
    )


def test_export_refuses_to_overwrite_existing_file(db_path: Path, tmp_path: Path):
    """Determinism (audit-log.md §7.3) means accidental clobbering can
    invalidate a checksummed export. Refuse by default."""
    _seed_audit_events(db_path)
    dest = tmp_path / "out.jsonl"
    dest.write_text("pre-existing", encoding="utf-8")

    trace = TraceStore(db_path)
    try:
        audit = AuditLog(trace)
        with pytest.raises(FileExistsError):
            audit.export(dest, window=_window(), format="jsonl")
    finally:
        trace.close()

    assert dest.read_text(encoding="utf-8") == "pre-existing"


def test_export_creates_parent_directories(db_path: Path, tmp_path: Path):
    """Convenience for SIEM cron pipelines that target dated subdirs."""
    _seed_audit_events(db_path)

    dest = tmp_path / "exports" / "2026" / "05" / "audit.jsonl"
    result = export_audit_events(db_path, dest, window=_window(), format="jsonl")

    assert dest.exists()
    assert result.event_count > 0


def test_unsupported_format_raises(db_path: Path, tmp_path: Path):
    _seed_audit_events(db_path)
    trace = TraceStore(db_path)
    try:
        audit = AuditLog(trace)
        with pytest.raises(ValueError, match="unsupported audit export format"):
            audit.export(tmp_path / "x.parquet", window=_window(), format="parquet")  # type: ignore[arg-type]
    finally:
        trace.close()


# ---- Append-only invariant (audit-log.md §6, §10) ----------------------
#
# Coordinated with the retention sweep landing in 12a-2. Until 12a-2 lands,
# this test simulates the sweep behavior — any DELETE the sweep issues MUST
# filter `type NOT IN AUDIT_EVENT_TYPES`. We assert that a sweep written
# correctly preserves audit rows and removes operational rows.


def test_simulated_retention_sweep_preserves_audit_events(db_path: Path):
    """Forward-compatible test for 12a-2. Seeds both audit + operational
    events, runs the kind of DELETE the retention sweep will issue, asserts
    audit events survive."""
    # Seed audit events.
    seeded = _seed_audit_events(db_path)

    # Seed operational events at the same window.
    store = TraceStore(db_path)
    try:
        for i in range(5):
            store.write(
                make_event(
                    type="llm.call_completed",
                    session_id="sess_op",
                    actor=Actor.AGENT,
                    payload=LLMCallCompleted(
                        model="anthropic:claude-haiku-4-5",
                        provider="anthropic",
                        input_tokens=10,
                        output_tokens=10,
                        cached_input_tokens=0,
                        cache_creation_input_tokens=0,
                        cost_usd=0.001,
                        pricing_version="v1",
                        latency_ms=100,
                        stop_reason="end_turn",
                        produced_tool_calls=0,
                        produced_thinking_blocks=0,
                    ),
                    timestamp=_now_offset(i + 100),
                )
            )
        # Seed an operational `session.created` event too — proves a non-audit
        # event with `pseudonymous` floor still gets swept (sensitivity is
        # orthogonal to audit-relevance, per audit-log.md §3).
        store.write(
            make_event(
                type="session.created",
                session_id="sess_op",
                actor=Actor.SYSTEM,
                payload=SessionCreated(
                    workspace_path="/w",
                    workspace_hash="h",
                    initial_active_model=None,
                    routing_policy_version="v",
                ),
                timestamp=_now_offset(200),
            )
        )
    finally:
        store.close()

    # The shape of the sweep DELETE that 12a-2 will issue.
    placeholders = ",".join("?" * len(AUDIT_EVENT_TYPES))
    sweep_sql = f"DELETE FROM events WHERE type NOT IN ({placeholders})"

    sweep_store = TraceStore(db_path)
    try:
        sweep_store._conn.execute(sweep_sql, sorted(AUDIT_EVENT_TYPES))

        # Audit rows survive.
        audit = AuditLog(sweep_store)
        audit_events = list(audit.query(window=_window()))
        assert len(audit_events) == seeded

        # Operational rows are gone.
        op_count = sweep_store._conn.execute(
            "SELECT COUNT(*) FROM events WHERE type IN (?, ?)",
            ("llm.call_completed", "session.created"),
        ).fetchone()[0]
        assert op_count == 0
    finally:
        sweep_store.close()


# ---- Module-level convenience function ---------------------------------


def test_export_audit_events_module_level_opens_and_closes_trace(db_path: Path, tmp_path: Path):
    """Just exercises the convenience wrapper that doesn't require the
    caller to manage the TraceStore lifecycle. Used by the CLI."""
    _seed_audit_events(db_path)
    dest = tmp_path / "out.jsonl"
    result = export_audit_events(db_path, dest, window=_window(), format="jsonl")
    assert result.event_count > 0
