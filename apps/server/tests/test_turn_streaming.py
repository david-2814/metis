"""TurnExecutor + StreamingHub integration: streaming events fan out during a turn."""

from __future__ import annotations

import asyncio

import pytest
from metis_server.hub import StreamingHub
from metis_server.turns import TurnExecutor


@pytest.fixture
def hub() -> StreamingHub:
    return StreamingHub()


async def test_turn_publishes_streaming_events_to_hub(runtime, workspace, hub):
    """Submit a turn via TurnExecutor; the hub should receive at least
    message.start, text.delta, and message.complete during execution."""
    executor = TurnExecutor(runtime.manager, hub=hub)
    session = runtime.manager.create_session(workspace_path=str(workspace))

    received: list[dict] = []
    hub.subscribe(session.id, received.append)

    executor.submit(session.id, "hi")

    # Wait for the background turn to finish.
    for _ in range(100):
        if any(f["event"]["type"] == "message.complete" for f in received):
            break
        await asyncio.sleep(0.02)

    types = [f["event"]["type"] for f in received]
    assert "message.start" in types
    assert "text.delta" in types
    assert "message.complete" in types

    # All frames are properly scoped to the session.
    assert all(f["event"]["session_id"] == session.id for f in received)
    # Each text.delta carries the model's text chunk.
    text_chunks = [
        f["event"]["payload"]["text"] for f in received if f["event"]["type"] == "text.delta"
    ]
    assert "".join(text_chunks) == "hi from server"


async def test_no_hub_means_no_publishing(runtime, workspace):
    """TurnExecutor without a hub still runs turns successfully."""
    executor = TurnExecutor(runtime.manager, hub=None)
    session = runtime.manager.create_session(workspace_path=str(workspace))
    executor.submit(session.id, "hi")
    for _ in range(100):
        msgs = runtime.session_store.get_messages(session.id)
        if any(m.role.value == "assistant" for m in msgs):
            break
        await asyncio.sleep(0.02)
    msgs = runtime.session_store.get_messages(session.id)
    assert any(m.role.value == "assistant" for m in msgs)


async def test_only_subscribed_session_gets_events(runtime, workspace, hub):
    """An event for sess_a should not reach sess_b's subscriber."""
    executor = TurnExecutor(runtime.manager, hub=hub)
    session_a = runtime.manager.create_session(workspace_path=str(workspace))
    session_b = runtime.manager.create_session(workspace_path=str(workspace))

    a_frames: list[dict] = []
    b_frames: list[dict] = []
    hub.subscribe(session_a.id, a_frames.append)
    hub.subscribe(session_b.id, b_frames.append)

    executor.submit(session_a.id, "hi")
    for _ in range(100):
        if any(f["event"]["type"] == "message.complete" for f in a_frames):
            break
        await asyncio.sleep(0.02)

    assert len(a_frames) > 0
    assert len(b_frames) == 0
