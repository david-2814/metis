"""HTTP-level tests for the /analytics/* routes.

The core-side `test_store.py` covers the SQL logic exhaustively. These tests
just verify the wiring: routes registered, query params parsed, errors mapped,
and the standard envelope wraps the response.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from metis_server.app import build_app

# Mirror the schemas from trace/store.py and sessions/sqlite_store.py — the
# runtime fixture uses InMemorySessionStore so we have to create the tables.
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


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 5, 12, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
async def seeded_client(runtime, now):
    db_path: Path = runtime.db_file
    # Ensure the trace store has flushed its schema (which it does on init).
    # Then create the sessions/messages tables which InMemorySessionStore
    # doesn't bother creating.
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.executescript(_SCHEMA)
    # Insert a couple of llm.call_completed events.
    for i in range(3):
        conn.execute(
            "INSERT INTO events "
            "(id, timestamp_us, session_id, turn_id, parent_event_id, type, "
            " actor, sensitivity, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"01HZ{i:020d}",
                _to_micros(now),
                "sess_seed",
                "turn_a",
                None,
                "llm.call_completed",
                "agent",
                "pseudonymous",
                json.dumps(
                    {
                        "model": "anthropic:claude-sonnet-4-6",
                        "provider": "anthropic",
                        "input_tokens": 100,
                        "output_tokens": 20,
                        "cached_input_tokens": 0,
                        "cache_creation_input_tokens": 0,
                        "cost_usd": "0.05",
                        "pricing_version": "test-1",
                        "latency_ms": 1000,
                        "stop_reason": "end_turn",
                        "produced_tool_calls": 0,
                        "produced_thinking_blocks": 0,
                    }
                ),
            ),
        )
    conn.close()
    # build_app must be called AFTER the schema exists; AnalyticsStore opens
    # its connection in __init__ and doesn't reload the schema.
    app = build_app(runtime)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


async def test_cost_endpoint_default_group_by_model(seeded_client, now):
    r = await seeded_client.get(
        "/analytics/cost",
        params={
            "from": (now.replace(hour=0)).isoformat(),
            "to": now.replace(hour=23).isoformat(),
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "window" in body
    assert "current_pricing_version" in body
    assert body["window"]["start"].startswith("2026-05-12")
    assert isinstance(body["data"], list)
    row = body["data"][0]
    assert row["model"] == "anthropic:claude-sonnet-4-6"
    assert row["call_count"] == 3
    assert row["cost_usd"] == pytest.approx(0.15)


async def test_cost_invalid_group_by_returns_400(seeded_client):
    r = await seeded_client.get("/analytics/cost", params={"group_by": "DROP TABLE"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_group_by"


async def test_cost_invalid_time_window_returns_400(seeded_client):
    r = await seeded_client.get(
        "/analytics/cost",
        params={"from": "2026-05-12T01:00:00Z", "to": "2026-05-12T00:00:00Z"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_time_window"


async def test_cache_effectiveness_endpoint(seeded_client):
    r = await seeded_client.get("/analytics/cache_effectiveness")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body["data"], list)


async def test_routing_endpoint_envelope(seeded_client):
    r = await seeded_client.get("/analytics/routing")
    assert r.status_code == 200
    body = r.json()
    assert "wins_by_policy" in body["data"]
    assert len(body["data"]["wins_by_policy"]) == 7  # all seven slots


async def test_reliability_endpoint(seeded_client):
    r = await seeded_client.get("/analytics/reliability")
    assert r.status_code == 200
    body = r.json()
    assert "errors_by_class" in body["data"]
    assert "latency_ms_by_model" in body["data"]


async def test_sessions_endpoint_returns_null_window(seeded_client):
    r = await seeded_client.get("/analytics/sessions")
    assert r.status_code == 200
    body = r.json()
    assert body["window"] == {"start": None, "end": None}


async def test_sessions_invalid_order_returns_400(seeded_client):
    r = await seeded_client.get("/analytics/sessions", params={"order": "; DELETE"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_order"


async def test_sessions_invalid_limit_returns_invalid_limit(seeded_client):
    r = await seeded_client.get("/analytics/sessions", params={"limit": "abc"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_limit"


async def test_sessions_zero_limit_rejected(seeded_client):
    r = await seeded_client.get("/analytics/sessions", params={"limit": "0"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_limit"


# ---- /analytics/quality (evaluator.md §9.2) -------------------------------


@pytest.fixture
async def quality_seeded_client(runtime, now):
    """Like `seeded_client` but seeds `eval.completed` + `route.decided` rows.

    The /analytics/quality endpoint reads these two event types; we insert
    them directly to avoid spinning up the full evaluator + bus stack.
    """
    db_path: Path = runtime.db_file
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.executescript(_SCHEMA)
    # One route.decided per turn (routing-engine §4.1 invariant).
    for i, model in enumerate(["anthropic:claude-haiku-4-5", "anthropic:claude-sonnet-4-6"]):
        turn_id = f"turn_q_{i}"
        conn.execute(
            "INSERT INTO events "
            "(id, timestamp_us, session_id, turn_id, parent_event_id, type, "
            " actor, sensitivity, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"01HZQ{i:020d}",
                _to_micros(now),
                "sess_q",
                turn_id,
                None,
                "route.decided",
                "system",
                "pseudonymous",
                json.dumps(
                    {
                        "chosen_model": model,
                        "winner_index": 0,
                        "elapsed_ms": 1.0,
                        "chain": [{"policy": "workspace_default", "verdict": "selected"}],
                    }
                ),
            ),
        )
        # One eval.completed per turn.
        conn.execute(
            "INSERT INTO events "
            "(id, timestamp_us, session_id, turn_id, parent_event_id, type, "
            " actor, sensitivity, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"01HZV{i:020d}",
                _to_micros(now),
                "sess_q",
                turn_id,
                None,
                "eval.completed",
                "system",
                "pseudonymous",
                json.dumps(
                    {
                        "eval_id": f"eval_{i}",
                        "subject_kind": "turn",
                        "subject_id": turn_id,
                        "score": 0.9 if "haiku" in model else 0.5,
                        "confidence": 0.8,
                        "judge_kind": "heuristic",
                        "judge_model": None,
                        "judge_cost_usd": "0",
                        "judge_latency_ms": 2,
                        "rubric_id": "turn-heuristic-v1",
                        "rubric_version": "1.0.0",
                        "signals": {"flags": ["stop_reason_clean"]},
                        "parent_eval_id": None,
                        "judge_pricing_version": None,
                    }
                ),
            ),
        )
    conn.close()
    app = build_app(runtime)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


async def test_quality_endpoint_round_trip(quality_seeded_client, now):
    r = await quality_seeded_client.get(
        "/analytics/quality",
        params={
            "from": now.replace(hour=0).isoformat(),
            "to": now.replace(hour=23).isoformat(),
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "window" in body
    assert "current_pricing_version" in body
    assert isinstance(body["data"], list)
    # Two distinct models judged → two rows.
    by_model = {row["chosen_model"]: row for row in body["data"]}
    assert set(by_model) == {
        "anthropic:claude-haiku-4-5",
        "anthropic:claude-sonnet-4-6",
    }
    assert by_model["anthropic:claude-haiku-4-5"]["mean_score"] == 0.9


async def test_quality_invalid_group_by_returns_400(quality_seeded_client):
    r = await quality_seeded_client.get("/analytics/quality", params={"group_by": "DROP TABLE"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_group_by"


async def test_quality_invalid_min_confidence_returns_400(quality_seeded_client):
    r = await quality_seeded_client.get("/analytics/quality", params={"min_confidence": "1.5"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_group_by"


async def test_quality_group_by_judge_kind(quality_seeded_client):
    r = await quality_seeded_client.get("/analytics/quality", params={"group_by": "judge_kind"})
    assert r.status_code == 200
    body = r.json()
    assert any(row["judge_kind"] == "heuristic" for row in body["data"])


async def test_turn_drill_down_404_unknown(seeded_client):
    r = await seeded_client.get("/analytics/turns/does_not_exist")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "turn_not_found"


async def test_turn_drill_down_happy_path(seeded_client):
    # The seeded fixture inserted three llm.call_completed events under turn_a.
    r = await seeded_client.get("/analytics/turns/turn_a")
    assert r.status_code == 200
    body = r.json()
    assert body["data"]["turn_id"] == "turn_a"
    assert body["data"]["session_id"] == "sess_seed"
    assert len(body["data"]["events"]) == 3
    # No turn.started / turn.completed events were seeded; in_flight is true.
    assert body["data"]["in_flight"] is True


async def test_savings_unknown_baseline_returns_400(seeded_client):
    r = await seeded_client.get("/analytics/savings", params={"baseline": "does-not-exist"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "unknown_baseline_model"


async def test_savings_endpoint_envelope(seeded_client):
    r = await seeded_client.get(
        "/analytics/savings",
        params={"baseline": "anthropic:claude-sonnet-4-6"},
    )
    assert r.status_code == 200
    body = r.json()
    data = body["data"]
    assert data["baseline_model"] == "anthropic:claude-sonnet-4-6"
    assert "savings_usd" in data
    assert "rows_total" in data


# ---- Dashboard SPA static serving ----------------------------------------


async def test_root_redirects_to_dashboard(seeded_client):
    r = await seeded_client.get("/", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert r.headers["location"] == "/dashboard/"


async def test_dashboard_index_html_served(seeded_client):
    r = await seeded_client.get("/dashboard/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    body = r.text
    assert "Metis" in body
    assert "app.js" in body


async def test_dashboard_app_js_served(seeded_client):
    r = await seeded_client.get("/dashboard/app.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]
    assert "renderCostView" in r.text


async def test_dashboard_style_css_served(seeded_client):
    r = await seeded_client.get("/dashboard/style.css")
    assert r.status_code == 200
    assert "text/css" in r.headers["content-type"]


async def test_dashboard_assets_set_no_cache(seeded_client):
    """SPA assets must revalidate on every load — otherwise edits look invisible."""
    for path in ("/dashboard/", "/dashboard/app.js", "/dashboard/style.css"):
        r = await seeded_client.get(path)
        assert r.status_code == 200
        assert r.headers.get("cache-control") == "no-cache", path


# ---- /analytics/cost?group_by=gateway_key + /analytics/by_key -------------


@pytest.fixture
async def gateway_seeded_client(runtime, now):
    """Seeds llm.call_completed rows with `gateway_key_id` / `inbound_shape`."""
    db_path: Path = runtime.db_file
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.executescript(_SCHEMA)

    def _insert(seq: int, *, gateway_key_id: str | None, inbound_shape: str | None, cost: str):
        payload: dict = {
            "model": "anthropic:claude-sonnet-4-6",
            "provider": "anthropic",
            "input_tokens": 100,
            "output_tokens": 20,
            "cached_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cost_usd": cost,
            "pricing_version": "test-1",
            "latency_ms": 500,
            "stop_reason": "end_turn",
            "produced_tool_calls": 0,
            "produced_thinking_blocks": 0,
        }
        if gateway_key_id is not None:
            payload["gateway_key_id"] = gateway_key_id
        if inbound_shape is not None:
            payload["inbound_shape"] = inbound_shape
        conn.execute(
            "INSERT INTO events "
            "(id, timestamp_us, session_id, turn_id, parent_event_id, type, "
            " actor, sensitivity, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"01HZK{seq:020d}",
                _to_micros(now),
                "sess_gw",
                f"turn_gw_{seq}",
                None,
                "llm.call_completed",
                "agent",
                "pseudonymous",
                json.dumps(payload),
            ),
        )

    _insert(1, gateway_key_id="gk_alpha", inbound_shape="openai", cost="0.10")
    _insert(2, gateway_key_id="gk_alpha", inbound_shape="anthropic", cost="0.05")
    _insert(3, gateway_key_id="gk_beta", inbound_shape="openai", cost="0.02")
    _insert(4, gateway_key_id=None, inbound_shape=None, cost="0.03")
    conn.close()

    app = build_app(runtime)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


async def test_cost_group_by_gateway_key_returns_rows(gateway_seeded_client):
    r = await gateway_seeded_client.get("/analytics/cost", params={"group_by": "gateway_key"})
    assert r.status_code == 200, r.text
    body = r.json()
    by_id = {row["gateway_key_id"]: row for row in body["data"]}
    assert by_id["gk_alpha"]["cost_usd"] == pytest.approx(0.15)
    assert by_id["gk_alpha"]["call_count"] == 2
    assert by_id["gk_beta"]["call_count"] == 1
    # Agent-loop traffic surfaces under null.
    assert None in by_id
    assert by_id[None]["cost_usd"] == pytest.approx(0.03)


async def test_by_key_endpoint_round_trip(gateway_seeded_client):
    r = await gateway_seeded_client.get("/analytics/by_key")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "window" in body
    assert "current_pricing_version" in body
    by_id = {row["gateway_key_id"]: row for row in body["data"]}
    alpha = by_id["gk_alpha"]
    assert alpha["cost_usd"] == pytest.approx(0.15)
    assert alpha["call_count"] == 2
    shapes = {s["inbound_shape"]: s for s in alpha["by_inbound_shape"]}
    assert shapes["openai"]["cost_usd"] == pytest.approx(0.10)
    assert shapes["anthropic"]["cost_usd"] == pytest.approx(0.05)
    # gk_alpha is first (cost DESC).
    assert body["data"][0]["gateway_key_id"] == "gk_alpha"


async def test_by_key_endpoint_filter_returns_one_key(gateway_seeded_client):
    r = await gateway_seeded_client.get(
        "/analytics/by_key",
        params={"gateway_key": "gk_alpha"},
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["data"]) == 1
    assert body["data"][0]["gateway_key_id"] == "gk_alpha"


async def test_by_key_endpoint_sql_injection_guard(gateway_seeded_client):
    r = await gateway_seeded_client.get(
        "/analytics/by_key",
        params={"gateway_key": "DROP TABLE"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_gateway_key"


async def test_by_key_endpoint_sql_injection_guard_special_chars(gateway_seeded_client):
    """A semicolon-laced value is also rejected at the HTTP boundary."""
    r = await gateway_seeded_client.get(
        "/analytics/by_key",
        params={"gateway_key": "gk_alpha'; DROP TABLE events; --"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_gateway_key"
