"""HTTP-level tests for the gateway app, end-to-end through routing + a
scripted adapter."""

from __future__ import annotations

import httpx
import pytest
from metis_gateway.app import build_app


@pytest.fixture
async def client(runtime):
    app = build_app(runtime)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


async def test_health_does_not_require_auth(client) -> None:
    r = await client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


async def test_chat_completions_rejects_missing_auth(client) -> None:
    r = await client.post(
        "/v1/chat/completions",
        json={"model": "haiku", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 401
    body = r.json()
    assert body["error"]["code"] == "invalid_api_key"
    assert body["error"]["type"] == "invalid_request_error"


async def test_chat_completions_rejects_wrong_bearer(client) -> None:
    r = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer gw_not_a_real_key"},
        json={"model": "haiku", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 401


async def test_chat_completions_end_to_end_happy_path(
    client, bearer_token, scripted_adapter, runtime
) -> None:
    scripted_adapter.push_response(text="hi from gateway")
    r = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {bearer_token}"},
        json={
            "model": "haiku",
            "messages": [{"role": "user", "content": "say hi"}],
            "max_completion_tokens": 100,
            "temperature": 0.5,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["object"] == "chat.completion"
    assert body["model"] == "haiku"
    assert body["choices"][0]["message"]["content"] == "hi from gateway"
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["usage"]["prompt_tokens"] == 10
    assert body["usage"]["completion_tokens"] == 5
    # The scripted adapter received exactly one request via the routing/harness path.
    assert len(scripted_adapter.requests) == 1
    request = scripted_adapter.requests[0]
    # Alias resolved to canonical id via the registry.
    assert request.model == "anthropic:claude-haiku-4-5"
    assert request.workspace_path  # key.workspace_path threaded through
    assert request.temperature == 0.5
    assert request.max_output_tokens == 100


async def test_chat_completions_falls_back_to_global_default(
    client, bearer_token, scripted_adapter
) -> None:
    """When the inbound `model` isn't an alias, routing should use the global
    default and still return a response. The dashboard tracks 'requested: X
    -> routed: Y' from the trace; the client sees `model: <requested>` echoed."""
    scripted_adapter.push_response(text="default response")
    r = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {bearer_token}"},
        json={
            "model": "some-fictional-model",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert r.status_code == 200
    body = r.json()
    # Client sees the requested string back, unchanged.
    assert body["model"] == "some-fictional-model"
    # But the underlying adapter call used the global default.
    assert scripted_adapter.requests[0].model == "anthropic:claude-sonnet-4-6"


async def test_streaming_request_returns_event_stream(
    client, bearer_token, scripted_adapter
) -> None:
    scripted_adapter.push_stream_response(text_deltas=["hi"])
    r = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {bearer_token}"},
        json={
            "model": "haiku",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")


async def test_invalid_json_body_returns_400(client, bearer_token) -> None:
    r = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {bearer_token}", "Content-Type": "application/json"},
        content=b"{not json",
    )
    assert r.status_code == 400
    assert r.json()["error"]["type"] == "invalid_request_error"


async def test_trace_events_emitted_with_gateway_key_id(
    client, bearer_token, scripted_adapter, runtime
) -> None:
    scripted_adapter.push_response()
    r = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {bearer_token}"},
        json={"model": "haiku", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200
    await runtime.bus.drain()

    import json
    import sqlite3

    conn = sqlite3.connect(runtime.db_file)
    try:
        rows = conn.execute("SELECT type, payload_json FROM events ORDER BY id").fetchall()
        types = [row[0] for row in rows]
        assert "route.decided" in types
        assert "llm.call_started" in types
        assert "llm.call_completed" in types
        assert "turn.completed" in types

        payloads_by_type = {row[0]: json.loads(row[1]) for row in rows}
        completed = payloads_by_type["llm.call_completed"]
        assert completed["gateway_key_id"] == "gk_test_001"
        assert completed["inbound_shape"] == "openai"
        assert completed["model"] == "anthropic:claude-haiku-4-5"
        assert completed["provider"] == "anthropic"
        assert completed["cost_usd"] >= 0

        turn = payloads_by_type["turn.completed"]
        assert turn["gateway_key_id"] == "gk_test_001"
        assert turn["llm_call_count"] == 1
    finally:
        conn.close()


async def test_allowed_models_filter_rejects_off_list_model(
    client, scripted_adapter, runtime, tmp_path
) -> None:
    """A key with an `allowed_models` list refuses calls that route off-list."""
    from metis_gateway.auth import GatewayKey, Keystore, hash_bearer_token

    token = "gw_restricted"
    runtime.keystore = Keystore(
        [
            GatewayKey(
                key_id="gk_restricted",
                secret_hash=hash_bearer_token(token),
                name="restricted",
                workspace_path=str(tmp_path),
                allowed_models=("anthropic:claude-opus-4-7",),
            )
        ]
    )
    scripted_adapter.push_response()
    r = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={"model": "haiku", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 403
    body = r.json()
    assert "allowed_models" in body["error"]["message"]


async def test_trace_events_stamp_user_id_and_team_id_for_tagged_key(
    client, scripted_adapter, runtime, tmp_path
) -> None:
    """multi-user.md §4.4 — a request authenticated with a (user, team)-tagged
    key produces `llm.call_completed` and `turn.completed` events that carry
    both stable identity ids alongside the existing `gateway_key_id` stamp."""
    from metis_gateway.auth import GatewayKey, Keystore, hash_bearer_token

    token = "gw_tagged_token"
    runtime.keystore = Keystore(
        [
            GatewayKey(
                key_id="gk_alice",
                secret_hash=hash_bearer_token(token),
                name="alice-claude-code",
                workspace_path=str(tmp_path),
                user_id="alice",
                team_id="eng",
            )
        ]
    )
    scripted_adapter.push_response()
    r = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={"model": "haiku", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200
    await runtime.bus.drain()

    import json
    import sqlite3

    conn = sqlite3.connect(runtime.db_file)
    try:
        rows = conn.execute("SELECT type, payload_json FROM events ORDER BY id").fetchall()
        payloads_by_type = {row[0]: json.loads(row[1]) for row in rows}

        completed = payloads_by_type["llm.call_completed"]
        assert completed["gateway_key_id"] == "gk_alice"
        assert completed["user_id"] == "alice"
        assert completed["team_id"] == "eng"

        turn = payloads_by_type["turn.completed"]
        assert turn["gateway_key_id"] == "gk_alice"
        assert turn["user_id"] == "alice"
        assert turn["team_id"] == "eng"
    finally:
        conn.close()


async def test_trace_events_stamp_null_identity_for_untagged_v1_key(
    client, bearer_token, scripted_adapter, runtime
) -> None:
    """Back-compat: a key issued without `--user` / `--team` (the `keystore`
    fixture's default) still authenticates and stamps both id fields as
    `null` on the analytics-relevant events."""
    scripted_adapter.push_response()
    r = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {bearer_token}"},
        json={"model": "haiku", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200
    await runtime.bus.drain()

    import json
    import sqlite3

    conn = sqlite3.connect(runtime.db_file)
    try:
        rows = conn.execute("SELECT type, payload_json FROM events ORDER BY id").fetchall()
        payloads_by_type = {row[0]: json.loads(row[1]) for row in rows}

        completed = payloads_by_type["llm.call_completed"]
        assert completed["gateway_key_id"] == "gk_test_001"
        assert completed["user_id"] is None
        assert completed["team_id"] is None

        turn = payloads_by_type["turn.completed"]
        assert turn["gateway_key_id"] == "gk_test_001"
        assert turn["user_id"] is None
        assert turn["team_id"] is None
    finally:
        conn.close()


async def test_hard_breaker_returns_429_with_documented_body(
    client, scripted_adapter, runtime, tmp_path
) -> None:
    """multi-user.md §5 / gateway.md §6.4 — once a key's daily cap is
    exceeded, subsequent requests must short-circuit before routing with
    HTTP 429 and the documented error body."""
    from decimal import Decimal

    from metis_gateway.auth import GatewayKey, Keystore, hash_bearer_token

    token = "gw_capped_token"
    runtime.keystore = Keystore(
        [
            GatewayKey(
                key_id="gk_capped",
                secret_hash=hash_bearer_token(token),
                name="capped",
                workspace_path=str(tmp_path),
                # Cap so low that one normal call exhausts it ($0.00001 /
                # 1k input tokens at the test pricing isn't quite zero).
                daily_cap_usd=Decimal("0.00001"),
            )
        ]
    )

    # First call goes through (spend = 0 < $0.00001).
    scripted_adapter.push_response(text="ok", input_tokens=10_000, output_tokens=2_000)
    r1 = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={"model": "haiku", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r1.status_code == 200
    await runtime.bus.drain()

    # Second call: cap is now exceeded → 429 with body shape from §6.4.
    r2 = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={"model": "haiku", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r2.status_code == 429
    body = r2.json()
    assert body["error"]["code"] == "quota_exceeded"
    assert body["error"]["identity"] == "key"
    assert body["error"]["scope"] == "key_daily"
    assert "limit_usd" in body["error"]
    assert "current_usd" in body["error"]
    # Adapter wasn't called on the rejected request — only the first call seeded
    # the scripted adapter, never a second.
    assert len(scripted_adapter.requests) == 1

    await runtime.bus.drain()
    import json as _json
    import sqlite3

    conn = sqlite3.connect(runtime.db_file)
    try:
        rows = conn.execute(
            "SELECT payload_json FROM events WHERE type = 'gateway.quota_exceeded'"
        ).fetchall()
        payloads = [_json.loads(r[0]) for r in rows]
    finally:
        conn.close()
    assert len(payloads) == 1
    assert payloads[0]["scope"] == "key_daily"
    assert payloads[0]["gateway_key_id"] == "gk_capped"
    assert payloads[0]["inbound_shape"] == "openai"


async def test_back_compat_uncapped_key_passes_through(
    client, bearer_token, scripted_adapter, runtime
) -> None:
    """A key with no caps continues to authenticate and route exactly as
    before — no quota check fires, no 429, no extra events."""
    scripted_adapter.push_response(text="ok")
    r = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {bearer_token}"},
        json={"model": "haiku", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200
    await runtime.bus.drain()

    import sqlite3

    conn = sqlite3.connect(runtime.db_file)
    try:
        types = [row[0] for row in conn.execute("SELECT type FROM events ORDER BY id").fetchall()]
    finally:
        conn.close()
    assert "gateway.quota_exceeded" not in types
    assert "quota.alert" not in types


async def test_anthropic_endpoint_stamps_identity_too(
    client, scripted_adapter, runtime, tmp_path
) -> None:
    """The `/v1/messages` (Anthropic-shape) handler resolves the same
    `Identity` projection and stamps the same fields."""
    from metis_gateway.auth import GatewayKey, Keystore, hash_bearer_token

    token = "gw_anthropic_tagged"
    runtime.keystore = Keystore(
        [
            GatewayKey(
                key_id="gk_anthropic_user",
                secret_hash=hash_bearer_token(token),
                name="anthropic-tagged",
                workspace_path=str(tmp_path),
                user_id="bob",
                team_id="ops",
            )
        ]
    )
    scripted_adapter.push_response()
    r = await client.post(
        "/v1/messages",
        headers={"x-api-key": token, "anthropic-version": "2023-06-01"},
        json={
            "model": "haiku",
            "max_tokens": 100,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert r.status_code == 200
    await runtime.bus.drain()

    import json
    import sqlite3

    conn = sqlite3.connect(runtime.db_file)
    try:
        rows = conn.execute("SELECT type, payload_json FROM events ORDER BY id").fetchall()
        payloads_by_type = {row[0]: json.loads(row[1]) for row in rows}
        completed = payloads_by_type["llm.call_completed"]
        assert completed["inbound_shape"] == "anthropic"
        assert completed["user_id"] == "bob"
        assert completed["team_id"] == "ops"
    finally:
        conn.close()
