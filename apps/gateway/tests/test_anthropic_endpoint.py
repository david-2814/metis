"""Tests for the Anthropic-shape inbound endpoint (`POST /v1/messages`).

Covers:
- Translator round-trip for the Anthropic-native blocks the canonical IR
  models 1:1 (thinking, redacted_thinking, tool_use, tool_result, image
  base64/url, system blocks with cache_control split into stable/volatile).
- HTTP-level happy path, sync + SSE streaming, error envelopes, file_ref
  rejection, and client-disconnect cancellation.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx
import msgspec
import pytest
from metis_core.canonical.content import (
    ImageBlock,
    RedactedThinkingBlock,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from metis_core.canonical.ids import new_message_id
from metis_core.canonical.messages import Message, Role
from metis_gateway.app import build_app
from metis_gateway.endpoints.anthropic import (
    InboundTranslationError,
    parse_anthropic_request,
)

# ---------------------------------------------------------------------------
# Translator: parse + system splitting
# ---------------------------------------------------------------------------


def test_parse_minimal_user_message() -> None:
    body = {
        "model": "haiku",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "hello"}],
    }
    parsed = parse_anthropic_request(body)
    assert parsed.model == "haiku"
    assert parsed.max_output_tokens == 100
    assert parsed.stream is False
    assert len(parsed.messages) == 1
    msg = parsed.messages[0]
    assert msg.role == Role.USER
    assert isinstance(msg.content[0], TextBlock)
    assert msg.content[0].text == "hello"
    assert parsed.system_prompt is None
    assert parsed.system_prompt_volatile is None


def test_parse_system_string() -> None:
    body = {
        "model": "haiku",
        "max_tokens": 50,
        "system": "be terse",
        "messages": [{"role": "user", "content": "hi"}],
    }
    parsed = parse_anthropic_request(body)
    assert parsed.system_prompt == "be terse"
    assert parsed.system_prompt_volatile is None


def test_parse_system_blocks_with_cache_control_splits_stable_volatile() -> None:
    body = {
        "model": "haiku",
        "max_tokens": 50,
        "system": [
            {"type": "text", "text": "stable prefix", "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": "volatile suffix"},
        ],
        "messages": [{"role": "user", "content": "hi"}],
    }
    parsed = parse_anthropic_request(body)
    assert parsed.system_prompt == "stable prefix"
    assert parsed.system_prompt_volatile == "volatile suffix"


def test_parse_system_blocks_without_cache_control_all_stable() -> None:
    body = {
        "model": "haiku",
        "max_tokens": 50,
        "system": [
            {"type": "text", "text": "part one"},
            {"type": "text", "text": "part two"},
        ],
        "messages": [{"role": "user", "content": "hi"}],
    }
    parsed = parse_anthropic_request(body)
    assert parsed.system_prompt == "part one\n\npart two"
    assert parsed.system_prompt_volatile is None


def test_parse_assistant_with_thinking_and_tool_use_round_trips() -> None:
    body = {
        "model": "haiku",
        "max_tokens": 100,
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "let me check", "signature": "sig123"},
                    {"type": "text", "text": "I will read the file."},
                    {
                        "type": "tool_use",
                        "id": "toolu_abc",
                        "name": "read_file",
                        "input": {"path": "README.md"},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_abc",
                        "content": "# Hello",
                    }
                ],
            },
        ],
    }
    parsed = parse_anthropic_request(body)
    # assistant + tool messages (the tool_result becomes its own Role.TOOL msg)
    assert len(parsed.messages) == 2
    assistant = parsed.messages[0]
    assert assistant.role == Role.ASSISTANT
    assert isinstance(assistant.content[0], ThinkingBlock)
    assert assistant.content[0].text == "let me check"
    assert assistant.content[0].signature == "sig123"
    assert isinstance(assistant.content[1], TextBlock)
    assert isinstance(assistant.content[2], ToolUseBlock)
    assert assistant.content[2].id == "toolu_abc"
    assert assistant.content[2].input == {"path": "README.md"}
    tool_msg = parsed.messages[1]
    assert tool_msg.role == Role.TOOL
    assert isinstance(tool_msg.content[0], ToolResultBlock)
    assert tool_msg.content[0].tool_use_id == "toolu_abc"


def test_parse_redacted_thinking_round_trips() -> None:
    body = {
        "model": "haiku",
        "max_tokens": 50,
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {"type": "redacted_thinking", "data": "opaqueblob"},
                    {"type": "text", "text": "hi"},
                ],
            }
        ],
    }
    parsed = parse_anthropic_request(body)
    blocks = parsed.messages[0].content
    assert isinstance(blocks[0], RedactedThinkingBlock)
    assert blocks[0].data == "opaqueblob"


def test_parse_user_image_base64_and_url() -> None:
    body = {
        "model": "haiku",
        "max_tokens": 50,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": "QUFB",
                        },
                    },
                    {
                        "type": "image",
                        "source": {"type": "url", "url": "https://example.com/x.png"},
                    },
                ],
            }
        ],
    }
    parsed = parse_anthropic_request(body)
    blocks = parsed.messages[0].content
    assert isinstance(blocks[0], ImageBlock)
    assert blocks[0].source.kind == "base64"
    assert blocks[0].source.data == "QUFB"
    assert blocks[1].source.kind == "url"
    assert blocks[1].source.data == "https://example.com/x.png"


def test_parse_user_image_file_ref_is_rejected() -> None:
    body = {
        "model": "haiku",
        "max_tokens": 50,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "file_ref", "path": "ignored"},
                    }
                ],
            }
        ],
    }
    with pytest.raises(InboundTranslationError, match="file_ref"):
        parse_anthropic_request(body)


def test_parse_rejects_missing_max_tokens() -> None:
    with pytest.raises(InboundTranslationError, match="max_tokens"):
        parse_anthropic_request({"model": "haiku", "messages": [{"role": "user", "content": "hi"}]})


def test_parse_rejects_missing_messages() -> None:
    with pytest.raises(InboundTranslationError, match="messages"):
        parse_anthropic_request({"model": "haiku", "max_tokens": 10})


def test_parse_rejects_unknown_role() -> None:
    with pytest.raises(InboundTranslationError, match="role"):
        parse_anthropic_request(
            {
                "model": "haiku",
                "max_tokens": 10,
                "messages": [{"role": "system", "content": "no"}],
            }
        )


def test_parse_tools_uses_input_schema_field() -> None:
    body = {
        "model": "haiku",
        "max_tokens": 10,
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [
            {
                "name": "read_file",
                "description": "Read.",
                "input_schema": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            }
        ],
    }
    parsed = parse_anthropic_request(body)
    assert len(parsed.tools) == 1
    assert parsed.tools[0].name == "read_file"
    assert parsed.tools[0].input_schema["required"] == ["path"]


# ---------------------------------------------------------------------------
# HTTP: sync path
# ---------------------------------------------------------------------------


@pytest.fixture
async def client(runtime):
    app = build_app(runtime)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


async def test_messages_rejects_missing_auth(client) -> None:
    r = await client.post(
        "/v1/messages",
        json={
            "model": "haiku",
            "max_tokens": 50,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert r.status_code == 401
    body = r.json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "authentication_error"


async def test_messages_accepts_x_api_key_header(client, bearer_token, scripted_adapter) -> None:
    scripted_adapter.push_response(text="hi back")
    r = await client.post(
        "/v1/messages",
        headers={"x-api-key": bearer_token},
        json={
            "model": "haiku",
            "max_tokens": 50,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["type"] == "message"
    assert body["role"] == "assistant"
    assert body["model"] == "haiku"  # echoes requested alias
    assert body["content"][0]["type"] == "text"
    assert body["content"][0]["text"] == "hi back"
    assert body["stop_reason"] == "end_turn"


async def test_messages_accepts_authorization_bearer(
    client, bearer_token, scripted_adapter
) -> None:
    scripted_adapter.push_response()
    r = await client.post(
        "/v1/messages",
        headers={"Authorization": f"Bearer {bearer_token}"},
        json={
            "model": "haiku",
            "max_tokens": 50,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert r.status_code == 200, r.text


async def test_messages_lossless_round_trip_thinking_tool_use_cache_control(
    client, bearer_token, scripted_adapter
) -> None:
    """Inbound message with thinking + cache_control + tool_use survives parse
    into canonical, and the canonical response with thinking + tool_use renders
    back to Anthropic shape unchanged.
    """
    scripted_adapter.push_blocks_response(
        blocks=[
            ThinkingBlock(text="reasoning step", signature="sig-xyz"),
            TextBlock(text="Calling tool now."),
            ToolUseBlock(id="toolu_out", name="search", input={"q": "metis"}),
        ],
    )
    r = await client.post(
        "/v1/messages",
        headers={"x-api-key": bearer_token},
        json={
            "model": "haiku",
            "max_tokens": 200,
            "system": [
                {
                    "type": "text",
                    "text": "stable system",
                    "cache_control": {"type": "ephemeral"},
                },
                {"type": "text", "text": "volatile system"},
            ],
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "thinking",
                            "thinking": "I should search",
                            "signature": "sig-in",
                        },
                        {
                            "type": "tool_use",
                            "id": "toolu_in",
                            "name": "search",
                            "input": {"q": "in"},
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_in",
                            "content": "result text",
                        },
                        {"type": "text", "text": "now do another search"},
                    ],
                },
            ],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # Outbound preserves thinking + tool_use blocks losslessly.
    types = [b["type"] for b in body["content"]]
    assert types == ["thinking", "text", "tool_use"]
    assert body["content"][0]["thinking"] == "reasoning step"
    assert body["content"][0]["signature"] == "sig-xyz"
    assert body["content"][2]["id"] == "toolu_out"
    assert body["content"][2]["name"] == "search"
    assert body["content"][2]["input"] == {"q": "metis"}
    assert body["stop_reason"] == "end_turn"

    # Inbound preserved: system split + thinking + tool_use + tool_result
    # land on the canonical request the adapter received.
    request = scripted_adapter.requests[0]
    assert request.system_prompt == "stable system"
    assert request.system_prompt_volatile == "volatile system"
    # canonical message ordering:
    #   assistant(thinking + tool_use), tool(tool_result), user(text)
    assert len(request.messages) == 3
    assistant_msg = request.messages[0]
    assert assistant_msg.role == Role.ASSISTANT
    assert isinstance(assistant_msg.content[0], ThinkingBlock)
    assert assistant_msg.content[0].text == "I should search"
    assert assistant_msg.content[0].signature == "sig-in"
    assert isinstance(assistant_msg.content[1], ToolUseBlock)
    assert assistant_msg.content[1].id == "toolu_in"
    tool_msg = request.messages[1]
    assert tool_msg.role == Role.TOOL
    assert isinstance(tool_msg.content[0], ToolResultBlock)
    assert tool_msg.content[0].tool_use_id == "toolu_in"
    user_msg = request.messages[2]
    assert user_msg.role == Role.USER
    assert isinstance(user_msg.content[0], TextBlock)


async def test_messages_rejects_file_ref_image_with_400(client, bearer_token) -> None:
    r = await client.post(
        "/v1/messages",
        headers={"x-api-key": bearer_token},
        json={
            "model": "haiku",
            "max_tokens": 50,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {"type": "file_ref", "path": "ignored.png"},
                        }
                    ],
                }
            ],
        },
    )
    assert r.status_code == 400
    body = r.json()
    assert body["error"]["type"] == "invalid_request_error"
    assert "file_ref" in body["error"]["message"]


async def test_messages_invalid_json_returns_400(client, bearer_token) -> None:
    r = await client.post(
        "/v1/messages",
        headers={"x-api-key": bearer_token, "Content-Type": "application/json"},
        content=b"{not json",
    )
    assert r.status_code == 400
    body = r.json()
    assert body["error"]["type"] == "invalid_request_error"


# ---------------------------------------------------------------------------
# HTTP: streaming
# ---------------------------------------------------------------------------


def _parse_sse(stream_bytes: bytes) -> list[tuple[str, dict]]:
    """Parse a raw SSE byte stream into (event_name, json_data) pairs."""
    out: list[tuple[str, dict]] = []
    for frame in stream_bytes.split(b"\n\n"):
        if not frame.strip():
            continue
        event_name: str | None = None
        data_payload: dict | None = None
        for line in frame.split(b"\n"):
            if line.startswith(b"event:"):
                event_name = line[len(b"event:") :].strip().decode("utf-8")
            elif line.startswith(b"data:"):
                data_payload = msgspec.json.decode(line[len(b"data:") :].strip())
        if event_name is not None and data_payload is not None:
            out.append((event_name, data_payload))
    return out


async def test_messages_streaming_emits_anthropic_event_sequence(
    client, bearer_token, scripted_adapter
) -> None:
    """SSE stream must emit message_start, then content_block_start/delta/stop
    for each block, then message_delta + message_stop. The translator fills in
    the implicit `content_block_start` for text deltas the canonical event
    layer doesn't carry one for."""
    scripted_adapter.push_stream_response(
        text_deltas=["hi ", "there"],
        tool_calls=[
            {
                "id": "toolu_stream",
                "name": "search",
                "arg_chunks": ['{"q":', ' "x"}'],
                "final_input": {"q": "x"},
            }
        ],
        input_tokens=15,
        output_tokens=8,
    )
    async with client.stream(
        "POST",
        "/v1/messages",
        headers={"x-api-key": bearer_token},
        json={
            "model": "haiku",
            "max_tokens": 50,
            "stream": True,
            "messages": [{"role": "user", "content": "go"}],
        },
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        raw = b""
        async for chunk in response.aiter_bytes():
            raw += chunk

    events = _parse_sse(raw)
    names = [name for name, _ in events]
    # First and last frames anchor the protocol.
    assert names[0] == "message_start"
    assert names[-1] == "message_stop"
    # The text block: start, two deltas, stop.
    text_start = names.index("content_block_start")
    assert events[text_start][1]["index"] == 0
    assert events[text_start][1]["content_block"]["type"] == "text"
    # text_delta frames present in order.
    deltas = [evt for name, evt in events if name == "content_block_delta" and evt["index"] == 0]
    assert [d["delta"]["text"] for d in deltas] == ["hi ", "there"]
    # Tool-use block opened with synthetic start carrying tool name/id.
    tool_starts = [
        evt for name, evt in events if name == "content_block_start" and evt["index"] == 1
    ]
    assert tool_starts
    assert tool_starts[0]["content_block"]["type"] == "tool_use"
    assert tool_starts[0]["content_block"]["id"] == "toolu_stream"
    assert tool_starts[0]["content_block"]["name"] == "search"
    # Two input_json_delta frames followed by a stop for the tool block.
    tool_deltas = [
        evt for name, evt in events if name == "content_block_delta" and evt["index"] == 1
    ]
    assert [d["delta"]["partial_json"] for d in tool_deltas] == ['{"q":', ' "x"}']
    tool_stops = [evt for name, evt in events if name == "content_block_stop" and evt["index"] == 1]
    assert tool_stops, "tool_use block must emit content_block_stop"
    # message_delta carries stop_reason and usage.
    msg_deltas = [evt for name, evt in events if name == "message_delta"]
    assert msg_deltas
    assert msg_deltas[0]["delta"]["stop_reason"] == "end_turn"
    assert msg_deltas[0]["usage"]["input_tokens"] == 15
    assert msg_deltas[0]["usage"]["output_tokens"] == 8


async def test_messages_streaming_cancel_mid_stream(
    runtime, scripted_adapter, bearer_token
) -> None:
    """A client closing the connection mid-stream must cancel the in-flight
    adapter call. The bus eventually sees a turn.completed for the partial run.
    """
    from metis_gateway.harness import (
        ClientDisconnected,
        GatewayHarness,
        make_disconnect_probe,
    )

    pause = scripted_adapter.push_stream_pause()
    scripted_adapter.push_stream_response(text_deltas=["too late"])

    harness = GatewayHarness(
        bus=runtime.bus,
        registry=runtime.registry,
        routing=runtime.routing,
        pricing=runtime.pricing,
        global_default_model=runtime.global_default_model,
        inbound_shape="anthropic",
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

    async def consume():
        stream_iter = harness.stream(
            messages=[user_msg],
            tools=[],
            system_prompt=None,
            max_output_tokens=64,
            temperature=None,
            stop_sequences=[],
            output_schema=None,
            requested_model="haiku",
            gateway_key_id="gk_test_001",
            workspace_path="/tmp",
            allowed_models=None,
            is_disconnected=make_disconnect_probe(is_disconnected),
        )
        async for _event in stream_iter:
            pass

    consumer = asyncio.create_task(consume())
    # Let the harness reach the paused stream.
    await asyncio.sleep(0.2)
    assert not consumer.done(), "consumer should still be waiting on the paused stream"
    disconnected.set()
    with pytest.raises(ClientDisconnected):
        await consumer
    assert scripted_adapter.cancel_calls, "harness should have called adapter.cancel"
    pause.event.set()
    await runtime.bus.drain()
