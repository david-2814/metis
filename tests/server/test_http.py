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


async def test_list_models_includes_pricing(client):
    r = await client.get("/models")
    body = r.json()
    sonnet = next(m for m in body["models"] if m["id"] == "anthropic:claude-sonnet-4-6")
    assert sonnet["pricing"] is not None
    assert sonnet["pricing"]["input_per_mtok"] == "3.00"
    assert sonnet["pricing"]["output_per_mtok"] == "15.00"
    assert sonnet["pricing"]["currency"] == "USD"
    assert sonnet["pricing"]["pricing_version"]


async def test_list_models_includes_task_profile(client):
    """Every model carries the curated `task_profile` list (possibly empty)."""
    r = await client.get("/models")
    body = r.json()
    for m in body["models"]:
        assert "task_profile" in m
        assert isinstance(m["task_profile"], list)


async def test_list_models_primary_only(client):
    """`?primary_only=true` collapses OpenRouter version siblings.

    With only Anthropic + OpenAI configured in the test fixture, primary_only
    is a no-op (each native id is its own family), so the response should
    contain the same set of ids as the unfiltered list.
    """
    full = (await client.get("/models")).json()
    primary = (await client.get("/models?primary_only=true")).json()
    full_ids = {m["id"] for m in full["models"]}
    primary_ids = {m["id"] for m in primary["models"]}
    # Native ids are their own family — primary_only doesn't remove any.
    assert primary_ids == full_ids


async def test_list_models_pattern_filter(client):
    """`?pattern=...` returns only models whose id contains the substring."""
    # The server fixture registers sonnet and haiku — search for the substring
    # we know is present.
    r = await client.get("/models?pattern=sonnet")
    body = r.json()
    assert body["models"], "pattern filter returned empty for a known substring"
    for m in body["models"]:
        assert "sonnet" in m["id"].lower()


async def test_list_models_pattern_filter_no_match(client):
    """An unmatched pattern returns an empty list, not an error."""
    r = await client.get("/models?pattern=zzz-does-not-exist")
    assert r.status_code == 200
    assert r.json()["models"] == []


# ---- Attach token flow --------------------------------------------------


async def test_attach_token_minted_per_get(client, workspace):
    r = await client.post("/sessions", json={"workspace_path": str(workspace)})
    sid = r.json()["id"]
    a = (await client.get(f"/sessions/{sid}")).json()["attach_token"]
    b = (await client.get(f"/sessions/{sid}")).json()["attach_token"]
    assert a != b


# ---- Body-level edge cases ---------------------------------------------


async def test_post_session_empty_body_returns_400(client):
    """A POST with no body still parses as `{}` and trips the missing-field
    check — surfaces as invalid_content rather than a 500."""
    r = await client.post(
        "/sessions",
        content=b"",
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_content"


async def test_post_session_malformed_json_returns_400(client):
    r = await client.post(
        "/sessions",
        content=b"{not valid json",
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 400
    body = r.json()
    assert body["error"]["code"] == "invalid_content"
    assert "invalid JSON" in body["error"]["message"]


async def test_patch_session_missing_active_model_returns_400(client, workspace):
    r = await client.post("/sessions", json={"workspace_path": str(workspace)})
    sid = r.json()["id"]
    r = await client.patch(f"/sessions/{sid}", json={})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_content"


# ---- Confirmation endpoint ---------------------------------------------


async def test_confirmation_unknown_request_id_returns_404(client, workspace):
    r = await client.post("/sessions", json={"workspace_path": str(workspace)})
    sid = r.json()["id"]
    r = await client.post(
        f"/sessions/{sid}/turns/01HZ_t/confirmations/conf_nope",
        json={"decision": "allow"},
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "confirmation_not_found"


async def test_confirmation_invalid_decision_returns_400(client, workspace):
    r = await client.post("/sessions", json={"workspace_path": str(workspace)})
    sid = r.json()["id"]
    r = await client.post(
        f"/sessions/{sid}/turns/01HZ_t/confirmations/conf_x",
        json={"decision": "maybe"},
    )
    assert r.status_code == 400
    assert "must be 'allow' or 'deny'" in r.json()["error"]["message"]


async def test_confirmation_invalid_scope_returns_400(client, workspace):
    r = await client.post("/sessions", json={"workspace_path": str(workspace)})
    sid = r.json()["id"]
    r = await client.post(
        f"/sessions/{sid}/turns/01HZ_t/confirmations/conf_x",
        json={"decision": "allow", "scope": "forever"},
    )
    assert r.status_code == 400
    assert "scope" in r.json()["error"]["message"]


async def test_confirmation_resolved_happy_path(client, workspace, runtime):
    """Manually register a pending confirmation on the handler, then post a
    resolve. The endpoint should return applied: true and the handler should
    no longer report it as pending."""
    import asyncio

    from metis.canonical.tools import SideEffects
    from metis.server.app import build_app
    from metis.tools.confirmation import ConfirmationRequest

    # Reuse the same runtime; the app fixture's `build_app` already swapped
    # the dispatcher's handler. Recover that handler via app state.
    app = build_app(runtime)
    handler = app.state.app_state.confirmation_handler

    # Kick off a request() in the background so the handler has a pending entry.
    req = ConfirmationRequest(
        tool_use_id="tu_http",
        tool_name="writer",
        side_effects=SideEffects.WRITE,
        input_summary="(test)",
    )
    pending_task = asyncio.create_task(handler.request(req))
    # Wait for registration.
    for _ in range(50):
        if handler.is_pending("conf_tu_http"):
            break
        await asyncio.sleep(0.01)
    assert handler.is_pending("conf_tu_http")

    # Resolve via the REST endpoint.
    import httpx

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as c:
        sid = (await c.post("/sessions", json={"workspace_path": str(workspace)})).json()[
            "id"
        ]
        r = await c.post(
            f"/sessions/{sid}/turns/01HZ_t/confirmations/conf_tu_http",
            json={"decision": "allow", "scope": "once"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["applied"] is True
        assert body["decision"] == "allow"

    # The pending task should now resolve.
    decision = await pending_task
    assert decision.value == "allow"


async def test_confirmation_double_resolve_returns_404_or_409(client, workspace, runtime):
    """After resolve, the handler drops the pending entry, so a second
    POST returns 404 (the unknown path) in v1. (server-api.md allows
    confirmation_already_resolved as an alternative; this asserts whichever
    we picked.)"""
    import asyncio

    from metis.canonical.tools import SideEffects
    from metis.server.app import build_app
    from metis.tools.confirmation import ConfirmationRequest

    app = build_app(runtime)
    handler = app.state.app_state.confirmation_handler

    req = ConfirmationRequest(
        tool_use_id="tu_dbl",
        tool_name="writer",
        side_effects=SideEffects.WRITE,
        input_summary="(test)",
    )
    task = asyncio.create_task(handler.request(req))
    for _ in range(50):
        if handler.is_pending("conf_tu_dbl"):
            break
        await asyncio.sleep(0.01)

    import httpx

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as c:
        sid = (await c.post("/sessions", json={"workspace_path": str(workspace)})).json()[
            "id"
        ]
        first = await c.post(
            f"/sessions/{sid}/turns/01HZ_t/confirmations/conf_tu_dbl",
            json={"decision": "allow"},
        )
        assert first.status_code == 200
        # Allow the background task to finish + the handler to drop pending.
        await task
        await asyncio.sleep(0.01)
        second = await c.post(
            f"/sessions/{sid}/turns/01HZ_t/confirmations/conf_tu_dbl",
            json={"decision": "deny"},
        )
        assert second.status_code in (404, 409)
        assert second.json()["error"]["code"] in (
            "confirmation_not_found",
            "confirmation_already_resolved",
        )


async def test_confirmation_endpoint_unknown_session_returns_404(client):
    r = await client.post(
        "/sessions/sess_unknown/turns/01HZ/confirmations/conf_x",
        json={"decision": "allow"},
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "session_not_found"
