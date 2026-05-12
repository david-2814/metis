"""HTTP endpoint tests via Starlette ASGI transport (no real port binding)."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from metis.server.app import build_app


@pytest.fixture
async def client(runtime):
    app = build_app(runtime)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as c:
        yield c


# ---- Meta ---------------------------------------------------------------


async def test_health(client):
    r = await client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["active_sessions"] == 0
    assert body["active_turns"] == 0


async def test_server_version(client):
    r = await client.get("/server/version")
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == "0.1.0"
    assert "canonical_message" in body["schema_versions"]


# ---- Sessions -----------------------------------------------------------


async def test_create_session_happy_path(client, workspace):
    r = await client.post(
        "/sessions",
        json={"workspace_path": str(workspace)},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["id"].startswith("sess_")
    assert body["workspace_path"] == str(workspace)


async def test_create_session_missing_workspace(client):
    r = await client.post("/sessions", json={})
    assert r.status_code == 400
    body = r.json()
    assert body["error"]["code"] == "invalid_content"


async def test_create_session_workspace_not_dir(client):
    r = await client.post(
        "/sessions",
        json={"workspace_path": "/nonexistent/dir/here/please"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "workspace_not_found"


async def test_create_session_unknown_model(client, workspace):
    r = await client.post(
        "/sessions",
        json={"workspace_path": str(workspace), "initial_active_model": "wildebeest"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "model_not_configured"


async def test_list_sessions(client, workspace):
    await client.post("/sessions", json={"workspace_path": str(workspace)})
    await client.post("/sessions", json={"workspace_path": str(workspace)})
    r = await client.get("/sessions")
    assert r.status_code == 200
    body = r.json()
    assert len(body["sessions"]) == 2


async def test_get_session_returns_attach_token(client, workspace):
    r = await client.post("/sessions", json={"workspace_path": str(workspace)})
    sid = r.json()["id"]
    r = await client.get(f"/sessions/{sid}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == sid
    assert body["attach_token"].startswith("atk_")
    assert body["ws_url"].startswith("ws://")
    assert f"/sessions/{sid}/stream?attach=" in body["ws_url"]


async def test_get_session_not_found(client):
    r = await client.get("/sessions/sess_nope")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "session_not_found"


async def test_patch_session_active_model(client, workspace):
    r = await client.post("/sessions", json={"workspace_path": str(workspace)})
    sid = r.json()["id"]
    r = await client.patch(f"/sessions/{sid}", json={"active_model": "haiku"})
    assert r.status_code == 200
    assert r.json()["active_model"] == "anthropic:claude-haiku-4-5"


async def test_patch_session_clear_sticky(client, workspace):
    r = await client.post(
        "/sessions",
        json={"workspace_path": str(workspace), "initial_active_model": "sonnet"},
    )
    sid = r.json()["id"]
    r = await client.patch(f"/sessions/{sid}", json={"active_model": None})
    assert r.status_code == 200
    assert r.json()["active_model"] is None


async def test_patch_session_unknown_model(client, workspace):
    r = await client.post("/sessions", json={"workspace_path": str(workspace)})
    sid = r.json()["id"]
    r = await client.patch(f"/sessions/{sid}", json={"active_model": "bogus"})
    assert r.status_code == 400


async def test_delete_session(client, workspace):
    r = await client.post("/sessions", json={"workspace_path": str(workspace)})
    sid = r.json()["id"]
    r = await client.delete(f"/sessions/{sid}")
    assert r.status_code == 200
    assert r.json()["id"] == sid


# ---- Turns --------------------------------------------------------------


async def test_submit_turn_returns_202(client, workspace, runtime):
    r = await client.post("/sessions", json={"workspace_path": str(workspace)})
    sid = r.json()["id"]
    r = await client.post(
        f"/sessions/{sid}/turns",
        json={"content": [{"type": "text", "text": "hello"}]},
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["session_id"] == sid
    assert "turn_id" in body
    # Wait for the background turn to finish so the test fixture teardown is clean.
    for _ in range(50):
        msgs = runtime.session_store.get_messages(sid)
        if any(m.role.value == "assistant" for m in msgs):
            break
        await asyncio.sleep(0.02)
    msgs = runtime.session_store.get_messages(sid)
    assert any(m.role.value == "assistant" for m in msgs)


async def test_submit_turn_invalid_content(client, workspace):
    r = await client.post("/sessions", json={"workspace_path": str(workspace)})
    sid = r.json()["id"]
    r = await client.post(f"/sessions/{sid}/turns", json={"content": []})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_content"


async def test_submit_turn_unknown_session(client):
    r = await client.post(
        "/sessions/sess_unknown/turns",
        json={"content": [{"type": "text", "text": "hi"}]},
    )
    assert r.status_code == 404


async def test_cancel_unknown_turn(client, workspace):
    r = await client.post("/sessions", json={"workspace_path": str(workspace)})
    sid = r.json()["id"]
    r = await client.post(f"/sessions/{sid}/turns/01HZ_nope/cancel", json={})
    assert r.status_code == 404


# ---- Messages -----------------------------------------------------------


async def test_list_messages_after_turn(client, workspace, runtime):
    r = await client.post("/sessions", json={"workspace_path": str(workspace)})
    sid = r.json()["id"]
    await client.post(
        f"/sessions/{sid}/turns",
        json={"content": [{"type": "text", "text": "hello"}]},
    )
    # Wait for the assistant reply to land.
    for _ in range(50):
        msgs = runtime.session_store.get_messages(sid)
        if any(m.role.value == "assistant" for m in msgs):
            break
        await asyncio.sleep(0.02)
    r = await client.get(f"/sessions/{sid}/messages")
    assert r.status_code == 200
    body = r.json()
    assert len(body["messages"]) >= 2
    roles = [m["role"] for m in body["messages"]]
    assert "user" in roles
    assert "assistant" in roles


# ---- Models -------------------------------------------------------------


async def test_list_models(client):
    r = await client.get("/models")
    assert r.status_code == 200
    body = r.json()
    ids = [m["id"] for m in body["models"]]
    assert "anthropic:claude-sonnet-4-6" in ids
    assert "anthropic:claude-haiku-4-5" in ids


# ---- Attach token flow --------------------------------------------------


async def test_attach_token_minted_per_get(client, workspace):
    r = await client.post("/sessions", json={"workspace_path": str(workspace)})
    sid = r.json()["id"]
    a = (await client.get(f"/sessions/{sid}")).json()["attach_token"]
    b = (await client.get(f"/sessions/{sid}")).json()["attach_token"]
    assert a != b
