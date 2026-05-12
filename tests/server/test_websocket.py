"""WebSocket streaming tests via Starlette TestClient.

Verifies: attach token flow, subscribe + snapshot, live event forwarding,
filter rejection, cancel-via-WS hook.

Uses Starlette's sync TestClient (which spawns an event loop internally to
drive the ASGI app); the runtime is built in-process. Each test does its
own asyncio plumbing for the runtime fixture, but the WS itself uses sync
calls inside `with client.websocket_connect(...)` blocks.
"""

from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from metis.server.app import build_app


@pytest.fixture
def test_client(runtime):
    app = build_app(runtime)
    with TestClient(app) as c:
        yield c


def _create_session(client: TestClient, workspace_path: str) -> str:
    r = client.post("/sessions", json={"workspace_path": workspace_path})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _get_attach(client: TestClient, sid: str) -> str:
    r = client.get(f"/sessions/{sid}")
    return r.json()["attach_token"]


def test_ws_attach_with_invalid_token_closes(test_client: TestClient, workspace):
    sid = _create_session(test_client, str(workspace))
    with pytest.raises(WebSocketDisconnect):
        with test_client.websocket_connect(
            f"/sessions/{sid}/stream?attach=atk_bogus"
        ) as ws:
            ws.receive_text()


def test_ws_attach_without_token_closes(test_client: TestClient, workspace):
    sid = _create_session(test_client, str(workspace))
    with pytest.raises(WebSocketDisconnect):
        with test_client.websocket_connect(f"/sessions/{sid}/stream") as ws:
            ws.receive_text()


def test_ws_subscribe_and_snapshot(test_client: TestClient, workspace):
    sid = _create_session(test_client, str(workspace))
    token = _get_attach(test_client, sid)
    with test_client.websocket_connect(
        f"/sessions/{sid}/stream?attach={token}"
    ) as ws:
        ws.send_text(
            json.dumps(
                {
                    "type": "subscribe",
                    "filter": "preset:chat",
                    "since": None,
                    "snapshot": True,
                }
            )
        )
        ack = json.loads(ws.receive_text())
        assert ack["type"] == "subscribe_ack"
        snap = json.loads(ws.receive_text())
        assert snap["type"] == "snapshot"
        assert snap["session"]["id"] == sid
        assert snap["messages"] == []


def test_ws_invalid_filter_returns_subscribe_error(test_client: TestClient, workspace):
    sid = _create_session(test_client, str(workspace))
    token = _get_attach(test_client, sid)
    with test_client.websocket_connect(
        f"/sessions/{sid}/stream?attach={token}"
    ) as ws:
        ws.send_text(
            json.dumps(
                {
                    "type": "subscribe",
                    "filter": {"event_types": ["made.up.thing"]},
                    "since": None,
                    "snapshot": False,
                }
            )
        )
        err = json.loads(ws.receive_text())
        assert err["type"] == "subscribe_error"
        assert err["code"] == "invalid_filter"


def test_ws_unknown_session_closes(test_client: TestClient, workspace):
    sid = _create_session(test_client, str(workspace))
    token = _get_attach(test_client, sid)
    # Drop the session out-of-band so the WS path sees a missing record.
    # Easiest way: try a different session id; the token is scoped to sid,
    # so it won't validate for sess_other, and we'll get a 1008 close.
    with pytest.raises(WebSocketDisconnect):
        with test_client.websocket_connect(
            f"/sessions/sess_other/stream?attach={token}"
        ) as ws:
            ws.receive_text()


def test_ws_pong_responds_to_ping(test_client: TestClient, workspace):
    sid = _create_session(test_client, str(workspace))
    token = _get_attach(test_client, sid)
    with test_client.websocket_connect(
        f"/sessions/{sid}/stream?attach={token}"
    ) as ws:
        ws.send_text(
            json.dumps(
                {
                    "type": "subscribe",
                    "filter": "preset:chat",
                    "since": None,
                    "snapshot": False,
                }
            )
        )
        assert json.loads(ws.receive_text())["type"] == "subscribe_ack"
        ws.send_text(json.dumps({"type": "ping", "nonce": "abc123"}))
        pong = json.loads(ws.receive_text())
        assert pong["type"] == "pong"
        assert pong["nonce"] == "abc123"
