"""SSE streaming tests for the OpenAI-shape chat-completions endpoint.

Covers:
- Happy path: text deltas flow through as `chat.completion.chunk` frames,
  terminated by a `finish_reason: stop` chunk and `data: [DONE]`.
- `stream_options.include_usage`: usage block is emitted only when requested.
- Tool-call streaming: each `ToolUseStart` becomes a tool_calls delta with an
  id + name, each `ToolUseInputDelta` becomes an arguments-only delta with the
  matching `tool_calls[].index`, and the final chunk's `finish_reason` is
  `tool_calls`.
- Cancellation: a client disconnect mid-stream aborts the in-flight adapter
  call (the harness invokes `adapter.cancel(...)`).
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from metis.core.adapters.protocol import StopReason
from metis.gateway.app import build_app
from metis.gateway.auth import Identity


@pytest.fixture
async def client(runtime):
    app = build_app(runtime)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


def _parse_sse(body: str) -> list:
    """Split an SSE body into its `data:` JSON payloads (or `[DONE]`)."""
    out: list = []
    for chunk in body.split("\n\n"):
        chunk = chunk.strip()
        if not chunk.startswith("data: "):
            continue
        payload = chunk[len("data: ") :]
        if payload == "[DONE]":
            out.append("[DONE]")
        else:
            out.append(json.loads(payload))
    return out


async def test_stream_happy_path_three_text_deltas(client, bearer_token, scripted_adapter) -> None:
    scripted_adapter.push_stream_response(
        text_deltas=["he", "llo", " world"],
        input_tokens=12,
        output_tokens=4,
    )
    r = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {bearer_token}"},
        json={
            "model": "haiku",
            "messages": [{"role": "user", "content": "say hi"}],
            "stream": True,
        },
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    frames = _parse_sse(r.text)
    # Role delta, three content deltas, terminating chunk, [DONE]. No usage
    # chunk because stream_options.include_usage was not set.
    assert frames[-1] == "[DONE]"
    chunks = [f for f in frames if f != "[DONE]"]
    deltas = [c["choices"][0]["delta"] for c in chunks if c["choices"]]
    contents = [d.get("content") for d in deltas if "content" in d]
    assert contents == ["he", "llo", " world"]
    # Each chunk echoes the requested (alias) model verbatim.
    assert all(c["model"] == "haiku" for c in chunks)
    # Final chunk carries finish_reason.
    final = chunks[-1]
    assert final["choices"][0]["finish_reason"] == "stop"
    assert final["choices"][0]["delta"] == {}
    # No usage object on any frame because include_usage wasn't requested.
    assert all("usage" not in c for c in chunks)


async def test_stream_emits_usage_only_when_include_usage_is_set(
    client, bearer_token, scripted_adapter
) -> None:
    scripted_adapter.push_stream_response(
        text_deltas=["ok"],
        input_tokens=7,
        output_tokens=2,
    )
    r = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {bearer_token}"},
        json={
            "model": "haiku",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
            "stream_options": {"include_usage": True},
        },
    )
    assert r.status_code == 200
    frames = _parse_sse(r.text)
    chunks = [f for f in frames if f != "[DONE]"]
    usage_chunks = [c for c in chunks if c.get("usage") is not None]
    assert len(usage_chunks) == 1
    usage = usage_chunks[0]["usage"]
    assert usage["prompt_tokens"] == 7
    assert usage["completion_tokens"] == 2
    assert usage["total_tokens"] == 9
    # The usage frame carries an empty `choices` list per OpenAI's wire shape.
    assert usage_chunks[0]["choices"] == []
    # And the [DONE] frame still comes last.
    assert frames[-1] == "[DONE]"


async def test_stream_tool_calls_emit_arguments_deltas(
    client, bearer_token, scripted_adapter
) -> None:
    scripted_adapter.push_stream_response(
        text_deltas=["thinking..."],
        tool_calls=[
            {
                "id": "tu_abc",
                "name": "read_file",
                "arg_chunks": ['{"path":', ' "README.md"}'],
                "final_input": {"path": "README.md"},
            }
        ],
        stop_reason=StopReason.TOOL_USE,
        input_tokens=20,
        output_tokens=8,
    )
    r = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {bearer_token}"},
        json={
            "model": "haiku",
            "messages": [{"role": "user", "content": "read it"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "description": "read a file",
                        "parameters": {
                            "type": "object",
                            "properties": {"path": {"type": "string"}},
                        },
                    },
                }
            ],
            "stream": True,
        },
    )
    assert r.status_code == 200
    frames = _parse_sse(r.text)
    chunks = [f for f in frames if f != "[DONE]"]
    tool_deltas = [
        c["choices"][0]["delta"]
        for c in chunks
        if c["choices"] and "tool_calls" in c["choices"][0]["delta"]
    ]
    # First tool_calls delta: id + name + empty arguments.
    head = tool_deltas[0]["tool_calls"][0]
    assert head["index"] == 0
    assert head["id"].startswith("call_")
    assert head["type"] == "function"
    assert head["function"]["name"] == "read_file"
    assert head["function"]["arguments"] == ""
    # Subsequent tool_calls deltas: arguments fragments matched by index.
    arg_chunks = [d["tool_calls"][0]["function"]["arguments"] for d in tool_deltas[1:]]
    assert arg_chunks == ['{"path":', ' "README.md"}']
    assert all(d["tool_calls"][0]["index"] == 0 for d in tool_deltas[1:])
    # Final chunk carries tool-use finish_reason.
    assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"


async def test_stream_cancel_on_client_disconnect(runtime, scripted_adapter) -> None:
    """Simulate a client disconnect mid-stream and assert the harness aborts
    the in-flight adapter call.

    We drive the harness directly instead of through the HTTP layer so the
    disconnect signal is controllable. The HTTP integration is exercised by
    the existing test_cancellation.py for the sync path; this test extends
    that coverage to the streaming path.
    """
    from datetime import UTC, datetime

    from metis.core.canonical.content import TextBlock
    from metis.core.canonical.ids import new_message_id
    from metis.core.canonical.messages import Message, Role
    from metis.gateway.harness import (
        ClientDisconnected,
        GatewayHarness,
        make_disconnect_probe,
    )

    pause = scripted_adapter.push_stream_pause()
    scripted_adapter.push_stream_response(text_deltas=["too", "late"])

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

    async def drive() -> list:
        events: list = []
        async for event in harness.stream(
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
        ):
            events.append(event)
        return events

    task = asyncio.create_task(drive())
    # Let the harness reach the paused adapter stream.
    await asyncio.sleep(0.2)
    assert not task.done(), "harness should still be waiting on the paused stream"
    # Simulate the client hanging up.
    disconnected.set()

    with pytest.raises(ClientDisconnected):
        await task

    assert scripted_adapter.cancel_calls, "harness should have invoked adapter.cancel"
    pause.event.set()
    await runtime.bus.drain()
