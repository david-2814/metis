"""Client-disconnect cancellation: an in-flight adapter call is aborted."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from metis.core.canonical.content import TextBlock
from metis.core.canonical.ids import new_message_id
from metis.core.canonical.messages import Message, Role
from metis.gateway.auth import Identity
from metis.gateway.harness import (
    ClientDisconnected,
    GatewayHarness,
    make_disconnect_probe,
)


async def test_client_disconnect_aborts_in_flight_adapter_call(runtime, scripted_adapter) -> None:
    pause = scripted_adapter.push_pause()
    scripted_adapter.push_response(text="too late")

    harness = GatewayHarness(
        bus=runtime.bus,
        registry=runtime.registry,
        routing=runtime.routing,
        pricing=runtime.pricing,
        global_default_model=runtime.global_default_model,
    )

    disconnected = asyncio.Event()

    async def is_disconnected() -> bool:
        return disconnected.is_set()

    user_msg = Message(
        id=new_message_id(),
        session_id="",
        role=Role.USER,
        content=[TextBlock(text="hello")],
        created_at=datetime.now(UTC),
    )
    call_task = asyncio.create_task(
        harness.call(
            messages=[user_msg],
            tools=[],
            system_prompt=None,
            max_output_tokens=128,
            temperature=None,
            stop_sequences=[],
            output_schema=None,
            requested_model="haiku",
            identity=Identity(gateway_key_id="gk_test_001", workspace_path="/tmp"),
            allowed_models=None,
            is_disconnected=make_disconnect_probe(is_disconnected),
        )
    )
    # Let the harness reach the paused adapter call.
    await asyncio.sleep(0.2)
    assert not call_task.done(), "harness should still be waiting on the adapter"
    # Simulate the client hanging up.
    disconnected.set()

    with pytest.raises(ClientDisconnected):
        await call_task

    # The harness asked the adapter to cancel, and the paused response never
    # leaked through.
    assert scripted_adapter.cancel_calls, "harness should have invoked adapter.cancel"
    # Release the pause so the asyncio gather inside the scripted adapter
    # cleans up if any path still references it.
    pause.event.set()
    await runtime.bus.drain()
