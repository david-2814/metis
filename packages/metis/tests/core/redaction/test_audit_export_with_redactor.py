"""Integration tests for `AuditLog.export(redactor=...)`.

Validates that the export pipeline composes with the EventRedactor —
each mode produces the expected file shape (per-row vs aggregate) and
the deterministic byte-output invariant holds.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from metis.core.analytics.windows import TimeWindow
from metis.core.audit import AuditLog
from metis.core.events.envelope import Actor
from metis.core.events.payloads import (
    GatewayKeyIssued,
    GatewayQuotaExceeded,
    make_event,
)
from metis.core.redaction import (
    PSEUDONYM_PREFIX,
    EventRedactor,
    RedactionMode,
)
from metis.core.trace.store import TraceStore


def _seed_audit_events(db: Path) -> None:
    store = TraceStore(db)
    try:
        store.write(
            make_event(
                type="gateway.key_issued",
                session_id="admin-sess",
                actor=Actor.SYSTEM,
                payload=GatewayKeyIssued(
                    gateway_key_id="gk_secret",
                    name="alice",
                    workspace_path="/srv/private",
                    issued_at=datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC),
                    user_id="alice",
                    team_id="team_a",
                ),
                timestamp=datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC),
            )
        )
        store.write(
            make_event(
                type="gateway.quota_exceeded",
                session_id="admin-sess",
                actor=Actor.SYSTEM,
                payload=GatewayQuotaExceeded(
                    gateway_key_id="gk_secret",
                    user_id="alice",
                    team_id="team_a",
                    scope="user_daily",
                    limit_usd="10.0",  # type: ignore[arg-type]
                    current_usd="11.0",  # type: ignore[arg-type]
                    inbound_shape="openai",
                ),
                timestamp=datetime(2026, 5, 15, 13, 0, 0, tzinfo=UTC),
            )
        )
    finally:
        store.close()


def _full_window() -> TimeWindow:
    return TimeWindow(
        start=datetime(2026, 5, 1, tzinfo=UTC),
        end=datetime(2026, 6, 1, tzinfo=UTC),
    )


def test_export_passthrough_matches_no_redactor(tmp_path: Path):
    db = tmp_path / "trace.db"
    _seed_audit_events(db)

    dest_a = tmp_path / "no_redactor.jsonl"
    dest_b = tmp_path / "passthrough.jsonl"
    trace = TraceStore(db)
    try:
        audit = AuditLog(trace)
        audit.export(dest_a, window=_full_window())
        audit.export(
            dest_b,
            window=_full_window(),
            redactor=EventRedactor(RedactionMode.PASSTHROUGH),
        )
    finally:
        trace.close()

    assert dest_a.read_bytes() == dest_b.read_bytes()


def test_export_pseudonymize_hashes_identity_fields(tmp_path: Path):
    db = tmp_path / "trace.db"
    _seed_audit_events(db)
    dest = tmp_path / "pseudonymized.jsonl"

    trace = TraceStore(db)
    try:
        audit = AuditLog(trace)
        audit.export(
            dest,
            window=_full_window(),
            redactor=EventRedactor(RedactionMode.PSEUDONYMIZE),
        )
    finally:
        trace.close()

    lines = dest.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    for line in lines:
        obj = json.loads(line)
        # Envelope: session_id pseudonymized
        assert obj["session_id"].startswith(PSEUDONYM_PREFIX)
        # Payload: user_id / team_id / gateway_key_id pseudonymized
        payload = obj["payload"]
        if "user_id" in payload and payload["user_id"] is not None:
            assert payload["user_id"].startswith(PSEUDONYM_PREFIX)
        if "gateway_key_id" in payload:
            assert payload["gateway_key_id"].startswith(PSEUDONYM_PREFIX)
        # Non-identity fields preserved
        if obj["type"] == "gateway.quota_exceeded":
            assert payload["scope"] == "user_daily"
            assert payload["inbound_shape"] == "openai"


def test_export_aggregate_only_produces_single_object(tmp_path: Path):
    db = tmp_path / "trace.db"
    _seed_audit_events(db)
    dest = tmp_path / "aggregate.json"

    trace = TraceStore(db)
    try:
        audit = AuditLog(trace)
        result = audit.export(
            dest,
            window=_full_window(),
            redactor=EventRedactor(RedactionMode.AGGREGATE_ONLY),
        )
    finally:
        trace.close()

    text = dest.read_text(encoding="utf-8").strip()
    agg = json.loads(text)
    assert agg["event_count"] == 2
    assert agg["events_by_type"] == {
        "gateway.key_issued": 1,
        "gateway.quota_exceeded": 1,
    }
    assert agg["distinct_users"] == 1
    # Counted from raw_events, not the redacted (None-filtered) stream
    assert result.event_count == 2


def test_export_determinism_under_redaction(tmp_path: Path):
    db = tmp_path / "trace.db"
    _seed_audit_events(db)
    dest_a = tmp_path / "a.jsonl"
    dest_b = tmp_path / "b.jsonl"

    trace = TraceStore(db)
    try:
        audit = AuditLog(trace)
        audit.export(
            dest_a,
            window=_full_window(),
            redactor=EventRedactor(RedactionMode.PSEUDONYMIZE),
        )
        audit.export(
            dest_b,
            window=_full_window(),
            redactor=EventRedactor(RedactionMode.PSEUDONYMIZE),
        )
    finally:
        trace.close()

    assert dest_a.read_bytes() == dest_b.read_bytes()


def test_export_refuses_to_overwrite_existing_redacted_file(tmp_path: Path):
    db = tmp_path / "trace.db"
    _seed_audit_events(db)
    dest = tmp_path / "existing.jsonl"
    dest.write_text("not empty", encoding="utf-8")

    trace = TraceStore(db)
    try:
        audit = AuditLog(trace)
        with pytest.raises(FileExistsError):
            audit.export(
                dest,
                window=_full_window(),
                redactor=EventRedactor(RedactionMode.PSEUDONYMIZE),
            )
    finally:
        trace.close()
