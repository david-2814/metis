"""HTTP-level tests for GDPR portability + forget endpoints.

`/analytics/user/{user_id}/export` and `/analytics/user/{user_id}/forget`
per analytics-api.md §4.10.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from metis_server.app import build_app

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY,
  timestamp_us INTEGER NOT NULL,
  session_id TEXT NOT NULL,
  turn_id TEXT,
  parent_event_id TEXT,
  type TEXT NOT NULL,
  actor TEXT NOT NULL,
  sensitivity TEXT NOT NULL,
  payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  workspace_path TEXT NOT NULL,
  active_model TEXT,
  routing_policy_json TEXT,
  cost_so_far_usd REAL NOT NULL DEFAULT 0,
  turn_count INTEGER NOT NULL DEFAULT 0,
  schema_version INTEGER NOT NULL,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  role TEXT NOT NULL,
  content_json TEXT NOT NULL,
  metadata_json TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  schema_version INTEGER NOT NULL
);
"""


def _to_micros(dt: datetime) -> int:
    epoch = datetime(1970, 1, 1, tzinfo=dt.tzinfo)
    delta = dt - epoch
    return delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds


def _seed_user_event(conn, *, idx: int, user_id: str, when: datetime) -> None:
    conn.execute(
        "INSERT INTO events "
        "(id, timestamp_us, session_id, turn_id, parent_event_id, type, "
        " actor, sensitivity, payload_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            f"01HZ{idx:020d}",
            _to_micros(when),
            "sess_test",
            "turn_test",
            None,
            "llm.call_completed",
            "agent",
            "pseudonymous",
            json.dumps(
                {
                    "model": "anthropic:claude-sonnet-4-6",
                    "provider": "anthropic",
                    "input_tokens": 100,
                    "output_tokens": 10,
                    "cached_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "cost_usd": "0.05",
                    "pricing_version": "test-1",
                    "latency_ms": 1000,
                    "stop_reason": "end_turn",
                    "produced_tool_calls": 0,
                    "produced_thinking_blocks": 0,
                    "user_id": user_id,
                }
            ),
        ),
    )


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 5, 12, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
async def seeded_client(runtime, now):
    db_path: Path = runtime.db_file
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.executescript(_SCHEMA)
    for i in range(3):
        _seed_user_event(conn, idx=i, user_id="usr_alice", when=now)
    _seed_user_event(conn, idx=10, user_id="usr_bob", when=now)
    # Older event outside the default window for window-filter tests.
    _seed_user_event(conn, idx=20, user_id="usr_alice", when=now - timedelta(days=30))
    conn.close()

    app = build_app(runtime)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


async def test_user_export_returns_only_subject_events(seeded_client):
    r = await seeded_client.get("/analytics/user/usr_alice/export")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/jsonl")
    body = r.content
    lines = [line for line in body.split(b"\n") if line]
    assert len(lines) == 4  # 3 in-window + 1 older
    for line in lines:
        obj = json.loads(line)
        assert obj["payload"]["user_id"] == "usr_alice"


async def test_user_export_window_filter(seeded_client, now):
    r = await seeded_client.get(
        "/analytics/user/usr_alice/export",
        params={
            "from": (now - timedelta(hours=1)).isoformat(),
            "to": (now + timedelta(hours=1)).isoformat(),
        },
    )
    assert r.status_code == 200
    lines = [line for line in r.content.split(b"\n") if line]
    assert len(lines) == 3  # excludes the 30-day-old row


async def test_user_export_unknown_user_empty_body(seeded_client):
    r = await seeded_client.get("/analytics/user/usr_unknown/export")
    assert r.status_code == 200
    assert r.content == b""


async def test_user_export_invalid_user_id_returns_400(seeded_client):
    r = await seeded_client.get("/analytics/user/has%20space/export")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_user_id"


async def test_user_export_content_disposition_attachment(seeded_client):
    r = await seeded_client.get("/analytics/user/usr_alice/export")
    cd = r.headers.get("content-disposition", "")
    assert 'filename="usr_alice.jsonl"' in cd


async def test_user_export_emits_audit_event(seeded_client, runtime):
    r = await seeded_client.get("/analytics/user/usr_alice/export")
    assert r.status_code == 200
    # Drain so the bus emit runs.
    await runtime.bus.drain()
    audit_rows = list(
        sqlite3.connect(str(runtime.db_file)).execute(
            "SELECT payload_json FROM events WHERE type = ?", ("analytics.user_exported",)
        )
    )
    assert len(audit_rows) == 1
    payload = json.loads(audit_rows[0][0])
    assert payload["subject_user_id"] == "usr_alice"
    assert payload["row_count"] == 4


async def test_user_forget_pseudonymizes_then_export_empty(seeded_client, runtime):
    r = await seeded_client.post("/analytics/user/usr_alice/forget")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user_id"] == "usr_alice"
    assert body["pseudonymized_rows"] == 4
    assert body["pseudonym"].startswith("redacted_")

    # Subsequent export is empty — the rows now carry the pseudonym.
    r2 = await seeded_client.get("/analytics/user/usr_alice/export")
    assert r2.status_code == 200
    assert r2.content == b""

    # Bob's rows are untouched.
    r3 = await seeded_client.get("/analytics/user/usr_bob/export")
    assert r3.status_code == 200
    lines = [line for line in r3.content.split(b"\n") if line]
    assert len(lines) == 1


async def test_user_forget_is_idempotent(seeded_client):
    r1 = await seeded_client.post("/analytics/user/usr_alice/forget")
    r2 = await seeded_client.post("/analytics/user/usr_alice/forget")
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["pseudonymized_rows"] == 4
    assert r2.json()["pseudonymized_rows"] == 0


async def test_user_forget_invalid_user_id_returns_400(seeded_client):
    r = await seeded_client.post("/analytics/user/has%20space/forget")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_user_id"


async def test_user_forget_emits_audit_event(seeded_client, runtime):
    await seeded_client.post("/analytics/user/usr_alice/forget")
    await runtime.bus.drain()
    audit_rows = list(
        sqlite3.connect(str(runtime.db_file)).execute(
            "SELECT payload_json FROM events WHERE type = ?", ("analytics.user_forgotten",)
        )
    )
    assert len(audit_rows) == 1
    payload = json.loads(audit_rows[0][0])
    assert payload["subject_user_id"] == "usr_alice"
    assert payload["pseudonymized_rows"] == 4
