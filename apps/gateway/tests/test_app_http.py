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
