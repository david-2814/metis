"""OpenAI ↔ canonical translator round-trip tests."""

from __future__ import annotations

import pytest
from metis_core.adapters.protocol import CanonicalResponse, StopReason, TokenUsage
from metis_core.adapters.tool_id_map import ToolIdMap
from metis_core.canonical.content import (
    ImageBlock,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from metis_core.canonical.messages import Role
from metis_gateway.translators import (
    InboundTranslationError,
    parse_openai_request,
    render_openai_response,
)


def test_parse_minimal_user_message() -> None:
    body = {
        "model": "haiku",
        "messages": [{"role": "user", "content": "hello"}],
    }
    parsed = parse_openai_request(body, tool_map=ToolIdMap())
    assert parsed.model == "haiku"
    assert len(parsed.messages) == 1
    assert parsed.messages[0].role == Role.USER
    assert isinstance(parsed.messages[0].content[0], TextBlock)
    assert parsed.messages[0].content[0].text == "hello"
    assert parsed.system_prompt is None


def test_parse_system_messages_hoisted_and_concatenated() -> None:
    body = {
        "model": "haiku",
        "messages": [
            {"role": "system", "content": "be terse"},
            {"role": "system", "content": "also be kind"},
            {"role": "user", "content": "hi"},
        ],
    }
    parsed = parse_openai_request(body, tool_map=ToolIdMap())
    assert parsed.system_prompt == "be terse\n\nalso be kind"
    assert len(parsed.messages) == 1
    assert parsed.messages[0].role == Role.USER


def test_parse_multimodal_user_content() -> None:
    body = {
        "model": "haiku",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "describe this"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,AAAA"},
                    },
                ],
            }
        ],
    }
    parsed = parse_openai_request(body, tool_map=ToolIdMap())
    blocks = parsed.messages[0].content
    assert isinstance(blocks[0], TextBlock)
    assert isinstance(blocks[1], ImageBlock)
    assert blocks[1].source.kind == "base64"
    assert blocks[1].media_type == "image/png"


def test_parse_assistant_with_tool_calls_uses_tool_id_map() -> None:
    tool_map = ToolIdMap()
    body = {
        "model": "haiku",
        "messages": [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_abc",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": '{"path": "README.md"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_abc",
                "content": "file contents",
            },
            {"role": "user", "content": "summarize"},
        ],
    }
    parsed = parse_openai_request(body, tool_map=tool_map)
    assert len(parsed.messages) == 3
    assistant = parsed.messages[0]
    tool_msg = parsed.messages[1]
    user = parsed.messages[2]
    assert assistant.role == Role.ASSISTANT
    assert tool_msg.role == Role.TOOL
    assert user.role == Role.USER
    use = assistant.content[0]
    res = tool_msg.content[0]
    assert isinstance(use, ToolUseBlock)
    assert isinstance(res, ToolResultBlock)
    assert use.id == res.tool_use_id
    assert tool_map.to_provider(use.id) == "call_abc"


def test_parse_tools_translates_function_definitions() -> None:
    body = {
        "model": "haiku",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Read a file.",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                    },
                },
            }
        ],
    }
    parsed = parse_openai_request(body, tool_map=ToolIdMap())
    assert len(parsed.tools) == 1
    tool = parsed.tools[0]
    assert tool.name == "read_file"
    assert tool.input_schema["properties"]["path"]["type"] == "string"


def test_parse_rejects_missing_model() -> None:
    with pytest.raises(InboundTranslationError, match="'model' is required"):
        parse_openai_request(
            {"messages": [{"role": "user", "content": "hi"}]}, tool_map=ToolIdMap()
        )


def test_parse_rejects_missing_messages() -> None:
    with pytest.raises(InboundTranslationError, match="'messages' is required"):
        parse_openai_request({"model": "haiku"}, tool_map=ToolIdMap())


def test_parse_rejects_unknown_role() -> None:
    with pytest.raises(InboundTranslationError, match="role must be"):
        parse_openai_request(
            {"model": "haiku", "messages": [{"role": "wat", "content": "x"}]},
            tool_map=ToolIdMap(),
        )


def test_parse_rejects_malformed_tool_arguments_json() -> None:
    body = {
        "model": "haiku",
        "messages": [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_x",
                        "type": "function",
                        "function": {"name": "f", "arguments": "{not json"},
                    }
                ],
            }
        ],
    }
    with pytest.raises(InboundTranslationError, match="not valid JSON"):
        parse_openai_request(body, tool_map=ToolIdMap())


def test_render_response_basic_text() -> None:
    response = CanonicalResponse(
        request_id="r1",
        model="anthropic:claude-haiku-4-5",
        provider="anthropic",
        content=[TextBlock(text="hello back")],
        stop_reason=StopReason.END_TURN,
        usage=TokenUsage(input_tokens=10, output_tokens=2, cached_input_tokens=1),
        latency_ms=99,
    )
    body = render_openai_response(response, requested_model="haiku", tool_map=ToolIdMap())
    assert body["model"] == "haiku"
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["choices"][0]["message"]["content"] == "hello back"
    assert body["usage"]["prompt_tokens"] == 10
    assert body["usage"]["completion_tokens"] == 2
    assert body["usage"]["prompt_tokens_details"]["cached_tokens"] == 1


def test_render_response_with_tool_use_maps_provider_ids() -> None:
    tool_map = ToolIdMap()
    tool_map.remember("tu_xyz", "call_xyz")
    response = CanonicalResponse(
        request_id="r1",
        model="anthropic:claude-haiku-4-5",
        provider="anthropic",
        content=[ToolUseBlock(id="tu_xyz", name="read_file", input={"path": "x"})],
        stop_reason=StopReason.TOOL_USE,
        usage=TokenUsage(input_tokens=5, output_tokens=10),
        latency_ms=1,
    )
    body = render_openai_response(response, requested_model="haiku", tool_map=tool_map)
    assert body["choices"][0]["finish_reason"] == "tool_calls"
    tool_calls = body["choices"][0]["message"]["tool_calls"]
    assert tool_calls[0]["id"] == "call_xyz"
    assert tool_calls[0]["function"]["name"] == "read_file"
    assert tool_calls[0]["function"]["arguments"] == '{"path": "x"}'
