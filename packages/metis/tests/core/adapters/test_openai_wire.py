"""Tests for canonical ↔ OpenAI wire-format translation.

These exercise the pure translation functions without HTTP calls.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

from metis.core.adapters.openai import (
    _canonical_messages_to_openai,
    _classify_openai_response,
    _openai_message_to_canonical,
    _stop_reason,
    _tool_to_openai,
    _usage_to_canonical,
    _wire_model_name,
)
from metis.core.adapters.protocol import StopReason
from metis.core.adapters.tool_id_map import ToolIdMap
from metis.core.canonical.content import (
    ImageBlock,
    ImageSource,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from metis.core.canonical.messages import Message, MessageMetadata, Role
from metis.core.canonical.tools import SideEffects, ToolDefinition


def _msg(role: Role, content: list, **md) -> Message:
    return Message(
        id="01HZ",
        session_id="s",
        role=role,
        content=content,
        created_at=datetime.now(UTC),
        metadata=MessageMetadata(**md) if md else MessageMetadata(),
    )


# ---- Model id stripping ------------------------------------------------


def test_wire_model_name_strips_prefix():
    assert _wire_model_name("openai:gpt-5") == "gpt-5"
    assert _wire_model_name("gpt-5") == "gpt-5"


# ---- System placement (in-list, NOT hoisted) ---------------------------


def test_external_system_prompt_becomes_first_message():
    tm = ToolIdMap()
    wire = _canonical_messages_to_openai(
        [_msg(Role.USER, [TextBlock(text="hi")])],
        system_prompt="be helpful",
        tool_map=tm,
    )
    assert wire[0] == {"role": "system", "content": "be helpful"}
    assert wire[1]["role"] == "user"


def test_canonical_system_messages_merged_with_external():
    tm = ToolIdMap()
    wire = _canonical_messages_to_openai(
        [
            _msg(Role.SYSTEM, [TextBlock(text="rule 1")]),
            _msg(Role.SYSTEM, [TextBlock(text="rule 2")]),
            _msg(Role.USER, [TextBlock(text="hi")]),
        ],
        system_prompt="base",
        tool_map=tm,
    )
    assert wire[0]["role"] == "system"
    assert wire[0]["content"] == "base\n\nrule 1\n\nrule 2"


def test_no_system_message_when_neither_set():
    tm = ToolIdMap()
    wire = _canonical_messages_to_openai(
        [_msg(Role.USER, [TextBlock(text="hi")])],
        system_prompt=None,
        tool_map=tm,
    )
    assert all(m["role"] != "system" for m in wire)


def test_volatile_system_appended_after_stable():
    """Stable text precedes volatile in the system message — OpenAI's
    automatic prefix-match cache (≥1024 tokens) keys on the byte-stable
    leading portion (context-assembler.md §3 last para)."""
    tm = ToolIdMap()
    wire = _canonical_messages_to_openai(
        [_msg(Role.USER, [TextBlock(text="hi")])],
        system_prompt="stable instructions",
        tool_map=tm,
        system_prompt_volatile="MEMORY: user prefers Rust",
    )
    assert wire[0]["role"] == "system"
    text = wire[0]["content"]
    assert text == "stable instructions\n\nMEMORY: user prefers Rust"
    # Stable prefix is at the start; volatile follows.
    assert text.startswith("stable instructions")
    assert text.endswith("user prefers Rust")


def test_only_volatile_system_still_emits_system_message():
    tm = ToolIdMap()
    wire = _canonical_messages_to_openai(
        [_msg(Role.USER, [TextBlock(text="hi")])],
        system_prompt=None,
        tool_map=tm,
        system_prompt_volatile="memory only",
    )
    assert wire[0] == {"role": "system", "content": "memory only"}


# ---- USER messages -----------------------------------------------------


def test_user_text_only_uses_string_content():
    tm = ToolIdMap()
    wire = _canonical_messages_to_openai(
        [_msg(Role.USER, [TextBlock(text="hello")])],
        system_prompt=None,
        tool_map=tm,
    )
    assert wire == [{"role": "user", "content": "hello"}]


def test_user_multimodal_uses_list_content():
    tm = ToolIdMap()
    wire = _canonical_messages_to_openai(
        [
            _msg(
                Role.USER,
                [
                    TextBlock(text="look at this"),
                    ImageBlock(
                        source=ImageSource(kind="base64", data="ZmFrZQ=="),
                        media_type="image/png",
                    ),
                ],
            )
        ],
        system_prompt=None,
        tool_map=tm,
    )
    msg = wire[0]
    assert msg["role"] == "user"
    assert isinstance(msg["content"], list)
    assert msg["content"][0] == {"type": "text", "text": "look at this"}
    img = msg["content"][1]
    assert img["type"] == "image_url"
    assert img["image_url"]["url"] == "data:image/png;base64,ZmFrZQ=="


def test_user_image_url_pass_through():
    tm = ToolIdMap()
    wire = _canonical_messages_to_openai(
        [
            _msg(
                Role.USER,
                [
                    ImageBlock(
                        source=ImageSource(kind="url", data="https://example.invalid/x.png"),
                        media_type="image/png",
                    )
                ],
            )
        ],
        system_prompt=None,
        tool_map=tm,
    )
    img = wire[0]["content"][0]
    assert img["image_url"]["url"] == "https://example.invalid/x.png"


# ---- ASSISTANT messages + tool calls -----------------------------------


def test_assistant_text_only():
    tm = ToolIdMap()
    wire = _canonical_messages_to_openai(
        [_msg(Role.ASSISTANT, [TextBlock(text="answer")])],
        system_prompt=None,
        tool_map=tm,
    )
    assert wire == [{"role": "assistant", "content": "answer"}]


def test_assistant_tool_use_only():
    tm = ToolIdMap()
    wire = _canonical_messages_to_openai(
        [
            _msg(
                Role.ASSISTANT,
                [ToolUseBlock(id="tu_x", name="read_file", input={"path": "a.md"})],
            )
        ],
        system_prompt=None,
        tool_map=tm,
    )
    msg = wire[0]
    assert msg["role"] == "assistant"
    assert "content" not in msg  # OpenAI accepts assistant message with only tool_calls
    assert len(msg["tool_calls"]) == 1
    call = msg["tool_calls"][0]
    assert call["type"] == "function"
    assert call["function"]["name"] == "read_file"
    # Arguments are JSON-stringified.
    assert json.loads(call["function"]["arguments"]) == {"path": "a.md"}
    # Canonical id was mapped to a generated call_* provider id.
    provider_id = call["id"]
    assert provider_id.startswith("call_")
    assert tm.to_provider("tu_x") == provider_id


def test_assistant_text_and_tool_use():
    tm = ToolIdMap()
    wire = _canonical_messages_to_openai(
        [
            _msg(
                Role.ASSISTANT,
                [
                    TextBlock(text="I'll read it."),
                    ToolUseBlock(id="tu_y", name="read_file", input={"path": "x"}),
                ],
            )
        ],
        system_prompt=None,
        tool_map=tm,
    )
    msg = wire[0]
    assert msg["content"] == "I'll read it."
    assert len(msg["tool_calls"]) == 1


def test_assistant_thinking_block_dropped_with_log(caplog):
    import logging

    tm = ToolIdMap()
    with caplog.at_level(logging.WARNING):
        wire = _canonical_messages_to_openai(
            [
                _msg(
                    Role.ASSISTANT,
                    [
                        ThinkingBlock(text="reasoning..."),
                        TextBlock(text="final"),
                    ],
                )
            ],
            system_prompt=None,
            tool_map=tm,
        )
    assert wire[0]["content"] == "final"
    assert any("dropping block" in rec.message for rec in caplog.records)


def test_assistant_existing_tool_mapping_is_reused():
    tm = ToolIdMap()
    tm.remember("tu_abc", "call_existing_id")
    wire = _canonical_messages_to_openai(
        [
            _msg(
                Role.ASSISTANT,
                [ToolUseBlock(id="tu_abc", name="x", input={})],
            )
        ],
        system_prompt=None,
        tool_map=tm,
    )
    assert wire[0]["tool_calls"][0]["id"] == "call_existing_id"


# ---- TOOL messages → role: tool standalone ----------------------------


def test_tool_message_becomes_role_tool():
    tm = ToolIdMap()
    tm.remember("tu_xyz", "call_xyz_provider")
    wire = _canonical_messages_to_openai(
        [
            _msg(
                Role.TOOL,
                [ToolResultBlock(tool_use_id="tu_xyz", content=[TextBlock(text="result")])],
            )
        ],
        system_prompt=None,
        tool_map=tm,
    )
    assert wire == [
        {
            "role": "tool",
            "tool_call_id": "call_xyz_provider",
            "content": "result",
        }
    ]


def test_consecutive_tool_messages_each_become_own_message():
    """Unlike Anthropic (which merges into one user message), OpenAI keeps
    each tool_call's result as its own role=tool entry."""
    tm = ToolIdMap()
    tm.remember("tu_a", "call_a")
    tm.remember("tu_b", "call_b")
    wire = _canonical_messages_to_openai(
        [
            _msg(Role.TOOL, [ToolResultBlock(tool_use_id="tu_a", content=[TextBlock(text="a")])]),
            _msg(Role.TOOL, [ToolResultBlock(tool_use_id="tu_b", content=[TextBlock(text="b")])]),
        ],
        system_prompt=None,
        tool_map=tm,
    )
    assert len(wire) == 2
    assert wire[0]["role"] == "tool"
    assert wire[0]["tool_call_id"] == "call_a"
    assert wire[1]["tool_call_id"] == "call_b"


def test_tool_result_image_becomes_placeholder():
    """OpenAI's tool message content is a string; image blocks placeholder."""
    tm = ToolIdMap()
    tm.remember("tu_x", "call_x")
    wire = _canonical_messages_to_openai(
        [
            _msg(
                Role.TOOL,
                [
                    ToolResultBlock(
                        tool_use_id="tu_x",
                        content=[
                            TextBlock(text="before"),
                            ImageBlock(
                                source=ImageSource(kind="base64", data="ZmFrZQ=="),
                                media_type="image/png",
                            ),
                            TextBlock(text="after"),
                        ],
                    )
                ],
            )
        ],
        system_prompt=None,
        tool_map=tm,
    )
    assert wire[0]["content"] == "before\n[image]\nafter"


# ---- Tool definition translation ---------------------------------------


def test_tool_definition_to_openai_function_shape():
    tool = ToolDefinition(
        name="read_file",
        description="reads a file",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
        side_effects=SideEffects.READ,
    )
    wire = _tool_to_openai(tool)
    assert wire == {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "reads a file",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
            },
        },
    }


# ---- Response parsing --------------------------------------------------


def test_parse_text_response():
    tm = ToolIdMap()
    msg = SimpleNamespace(content="hello", tool_calls=None)
    blocks = _openai_message_to_canonical(msg, tm)
    assert blocks == [TextBlock(text="hello")]


def test_parse_tool_call_response_generates_canonical_id():
    tm = ToolIdMap()
    msg = SimpleNamespace(
        content=None,
        tool_calls=[
            SimpleNamespace(
                id="call_provider_abc",
                type="function",
                function=SimpleNamespace(
                    name="read_file",
                    arguments='{"path": "x.md"}',
                ),
            )
        ],
    )
    blocks = _openai_message_to_canonical(msg, tm)
    assert len(blocks) == 1
    block = blocks[0]
    assert isinstance(block, ToolUseBlock)
    assert block.name == "read_file"
    assert block.input == {"path": "x.md"}
    # A canonical id was generated and the bidirectional map records it.
    assert block.id.startswith("tu_")
    assert tm.to_canonical("call_provider_abc") == block.id
    assert tm.to_provider(block.id) == "call_provider_abc"


def test_parse_tool_call_response_reuses_existing_mapping():
    tm = ToolIdMap()
    tm.remember("tu_existing", "call_known")
    msg = SimpleNamespace(
        content=None,
        tool_calls=[
            SimpleNamespace(
                id="call_known",
                type="function",
                function=SimpleNamespace(name="t", arguments="{}"),
            )
        ],
    )
    blocks = _openai_message_to_canonical(msg, tm)
    assert blocks[0].id == "tu_existing"


def test_parse_text_and_tool_call_response():
    tm = ToolIdMap()
    msg = SimpleNamespace(
        content="I'll do that",
        tool_calls=[
            SimpleNamespace(
                id="call_x",
                type="function",
                function=SimpleNamespace(name="t", arguments="{}"),
            )
        ],
    )
    blocks = _openai_message_to_canonical(msg, tm)
    assert len(blocks) == 2
    assert isinstance(blocks[0], TextBlock)
    assert isinstance(blocks[1], ToolUseBlock)


def test_parse_invalid_json_arguments_returns_empty_dict(caplog):
    import logging

    tm = ToolIdMap()
    msg = SimpleNamespace(
        content=None,
        tool_calls=[
            SimpleNamespace(
                id="call_bad",
                type="function",
                function=SimpleNamespace(name="t", arguments="not json"),
            )
        ],
    )
    with caplog.at_level(logging.WARNING):
        blocks = _openai_message_to_canonical(msg, tm)
    assert isinstance(blocks[0], ToolUseBlock)
    assert blocks[0].input == {}


# ---- Stop reason -------------------------------------------------------


def test_stop_reason_mapping():
    assert _stop_reason("stop") == StopReason.END_TURN
    assert _stop_reason("length") == StopReason.MAX_TOKENS
    assert _stop_reason("tool_calls") == StopReason.TOOL_USE
    assert _stop_reason("function_call") == StopReason.TOOL_USE
    assert _stop_reason("content_filter") == StopReason.END_TURN
    assert _stop_reason(None) == StopReason.END_TURN


# ---- Usage mapping -----------------------------------------------------


def test_usage_maps_cached_tokens():
    usage = SimpleNamespace(
        prompt_tokens=1000,
        completion_tokens=500,
        prompt_tokens_details=SimpleNamespace(cached_tokens=200),
    )
    out = _usage_to_canonical(usage)
    assert out.input_tokens == 1000
    assert out.output_tokens == 500
    assert out.cached_input_tokens == 200
    assert out.cache_creation_input_tokens == 0


def test_usage_without_details_is_safe():
    usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5)
    out = _usage_to_canonical(usage)
    assert out.input_tokens == 10
    assert out.output_tokens == 5
    assert out.cached_input_tokens == 0


# ---- Error classification ----------------------------------------------


def test_classify_rate_limit_by_code():
    body = {"error": {"code": "rate_limit_exceeded", "message": "slow down"}}
    from metis.core.adapters.errors import ErrorClass

    assert _classify_openai_response(429, body) == ErrorClass.RATE_LIMIT


def test_classify_context_overflow_by_code():
    body = {"error": {"code": "context_length_exceeded", "message": "too long"}}
    from metis.core.adapters.errors import ErrorClass

    assert _classify_openai_response(400, body) == ErrorClass.CONTEXT_OVERFLOW


def test_classify_invalid_api_key():
    body = {"error": {"code": "invalid_api_key", "message": "bad key"}}
    from metis.core.adapters.errors import ErrorClass

    assert _classify_openai_response(401, body) == ErrorClass.AUTH


def test_classify_server_error_type():
    body = {"error": {"type": "server_error", "message": "down"}}
    from metis.core.adapters.errors import ErrorClass

    assert _classify_openai_response(500, body) == ErrorClass.SERVER_ERROR


def test_classify_falls_back_to_http_status():
    from metis.core.adapters.errors import ErrorClass

    assert _classify_openai_response(429, None) == ErrorClass.RATE_LIMIT
    assert _classify_openai_response(503, {"error": {}}) == ErrorClass.SERVER_ERROR
