"""Tests for canonical ↔ Anthropic wire-format translation.

These tests don't make HTTP calls — they exercise the pure translation
functions in `metis.core.adapters.anthropic`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from metis.core.adapters.anthropic import (
    _anthropic_blocks_to_canonical,
    _canonical_messages_to_anthropic,
    _stop_reason,
    _wire_model_name,
    _with_history_cache_breakpoint,
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
    assert _wire_model_name("anthropic:claude-sonnet-4-6") == "claude-sonnet-4-6"
    assert _wire_model_name("claude-sonnet-4-6") == "claude-sonnet-4-6"


# ---- System prompt hoisting --------------------------------------------


def test_system_hoisted_from_top_level_param():
    tm = ToolIdMap()
    wire, system = _canonical_messages_to_anthropic(
        [_msg(Role.USER, [TextBlock(text="hello")])],
        system_prompt="be helpful",
        tool_map=tm,
    )
    assert system == "be helpful"
    assert len(wire) == 1
    assert wire[0]["role"] == "user"


def test_system_messages_in_list_are_concatenated_into_top_level():
    tm = ToolIdMap()
    wire, system = _canonical_messages_to_anthropic(
        [
            _msg(Role.SYSTEM, [TextBlock(text="rule 1")]),
            _msg(Role.SYSTEM, [TextBlock(text="rule 2")]),
            _msg(Role.USER, [TextBlock(text="hi")]),
        ],
        system_prompt=None,
        tool_map=tm,
    )
    assert system == "rule 1\n\nrule 2"
    assert [m["role"] for m in wire] == ["user"]


def test_external_system_prompt_combined_with_in_list_system():
    tm = ToolIdMap()
    _, system = _canonical_messages_to_anthropic(
        [
            _msg(Role.SYSTEM, [TextBlock(text="extra")]),
            _msg(Role.USER, [TextBlock(text="hi")]),
        ],
        system_prompt="base",
        tool_map=tm,
    )
    assert system == "base\n\nextra"


# ---- USER messages ------------------------------------------------------


def test_user_text_message_translation():
    tm = ToolIdMap()
    wire, _ = _canonical_messages_to_anthropic(
        [_msg(Role.USER, [TextBlock(text="hi")])],
        system_prompt=None,
        tool_map=tm,
    )
    assert wire == [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]


def test_user_image_message_base64():
    tm = ToolIdMap()
    wire, _ = _canonical_messages_to_anthropic(
        [
            _msg(
                Role.USER,
                [
                    TextBlock(text="look"),
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
    image_block = wire[0]["content"][1]
    assert image_block["type"] == "image"
    assert image_block["source"]["type"] == "base64"
    assert image_block["source"]["media_type"] == "image/png"
    assert image_block["source"]["data"] == "ZmFrZQ=="


# ---- ASSISTANT messages -------------------------------------------------


def test_assistant_with_text_and_tool_use():
    tm = ToolIdMap()
    wire, _ = _canonical_messages_to_anthropic(
        [
            _msg(
                Role.ASSISTANT,
                [
                    TextBlock(text="I'll read it."),
                    ToolUseBlock(id="tu_01HZ", name="read_file", input={"path": "x.md"}),
                ],
            )
        ],
        system_prompt=None,
        tool_map=tm,
    )
    assert wire[0]["role"] == "assistant"
    blocks = wire[0]["content"]
    assert blocks[0]["type"] == "text"
    assert blocks[1]["type"] == "tool_use"
    assert blocks[1]["id"] == "tu_01HZ"  # canonical id used as wire id
    assert blocks[1]["name"] == "read_file"
    # Identity mapping recorded.
    assert tm.to_provider("tu_01HZ") == "tu_01HZ"


def test_assistant_with_thinking_block():
    tm = ToolIdMap()
    wire, _ = _canonical_messages_to_anthropic(
        [
            _msg(
                Role.ASSISTANT,
                [
                    ThinkingBlock(text="reasoning...", signature="sig_xyz"),
                    TextBlock(text="answer"),
                ],
            )
        ],
        system_prompt=None,
        tool_map=tm,
    )
    blocks = wire[0]["content"]
    assert blocks[0]["type"] == "thinking"
    assert blocks[0]["thinking"] == "reasoning..."
    assert blocks[0]["signature"] == "sig_xyz"


# ---- TOOL messages → user with tool_result -----------------------------


def test_tool_message_becomes_user_with_tool_result_block():
    tm = ToolIdMap()
    wire, _ = _canonical_messages_to_anthropic(
        [
            _msg(
                Role.TOOL,
                [
                    ToolResultBlock(
                        tool_use_id="tu_xyz",
                        content=[TextBlock(text="contents")],
                    )
                ],
            )
        ],
        system_prompt=None,
        tool_map=tm,
    )
    assert wire == [
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tu_xyz",
                    "content": [{"type": "text", "text": "contents"}],
                    "is_error": False,
                }
            ],
        }
    ]


def test_consecutive_tool_messages_merge_into_one_user_message():
    """Two consecutive TOOL messages must become ONE Anthropic user message
    with multiple tool_result blocks (per §4.2 of provider-adapter spec)."""
    tm = ToolIdMap()
    wire, _ = _canonical_messages_to_anthropic(
        [
            _msg(Role.TOOL, [ToolResultBlock(tool_use_id="tu_a", content=[TextBlock(text="a")])]),
            _msg(Role.TOOL, [ToolResultBlock(tool_use_id="tu_b", content=[TextBlock(text="b")])]),
        ],
        system_prompt=None,
        tool_map=tm,
    )
    assert len(wire) == 1
    assert wire[0]["role"] == "user"
    assert len(wire[0]["content"]) == 2
    assert wire[0]["content"][0]["tool_use_id"] == "tu_a"
    assert wire[0]["content"][1]["tool_use_id"] == "tu_b"


def test_tool_message_then_user_message_does_not_merge():
    """A USER message after TOOL must be its own user message."""
    tm = ToolIdMap()
    wire, _ = _canonical_messages_to_anthropic(
        [
            _msg(Role.TOOL, [ToolResultBlock(tool_use_id="tu_a", content=[TextBlock(text="r")])]),
            _msg(Role.USER, [TextBlock(text="next")]),
        ],
        system_prompt=None,
        tool_map=tm,
    )
    assert len(wire) == 2
    assert wire[0]["role"] == "user"
    assert wire[0]["content"][0]["type"] == "tool_result"
    assert wire[1]["role"] == "user"
    assert wire[1]["content"][0]["type"] == "text"


def test_tool_result_with_is_error():
    tm = ToolIdMap()
    wire, _ = _canonical_messages_to_anthropic(
        [
            _msg(
                Role.TOOL,
                [
                    ToolResultBlock(
                        tool_use_id="tu_x",
                        content=[TextBlock(text="failed")],
                        is_error=True,
                    )
                ],
            )
        ],
        system_prompt=None,
        tool_map=tm,
    )
    assert wire[0]["content"][0]["is_error"] is True


# ---- Response parsing --------------------------------------------------


def test_parse_text_block_response():
    tm = ToolIdMap()
    raw = SimpleNamespace(type="text", text="hello")
    blocks = _anthropic_blocks_to_canonical([raw], tm)
    assert blocks == [TextBlock(text="hello")]


def test_parse_tool_use_response_records_mapping():
    tm = ToolIdMap()
    raw = SimpleNamespace(
        type="tool_use",
        id="toolu_provider_id",
        name="read_file",
        input={"path": "x"},
    )
    blocks = _anthropic_blocks_to_canonical([raw], tm)
    assert isinstance(blocks[0], ToolUseBlock)
    assert blocks[0].id == "toolu_provider_id"
    assert tm.to_provider("toolu_provider_id") == "toolu_provider_id"


def test_parse_thinking_block_response():
    tm = ToolIdMap()
    raw = SimpleNamespace(type="thinking", thinking="reasoning", signature="sig")
    blocks = _anthropic_blocks_to_canonical([raw], tm)
    assert blocks == [ThinkingBlock(text="reasoning", signature="sig")]


def test_parse_unknown_block_type_is_dropped():
    tm = ToolIdMap()
    raw_known = SimpleNamespace(type="text", text="hi")
    raw_unknown = SimpleNamespace(type="future_block_type", weird="data")
    blocks = _anthropic_blocks_to_canonical([raw_known, raw_unknown], tm)
    assert len(blocks) == 1


# ---- Stop reason mapping -----------------------------------------------


def test_stop_reason_mapping():
    assert _stop_reason("end_turn") == StopReason.END_TURN
    assert _stop_reason("max_tokens") == StopReason.MAX_TOKENS
    assert _stop_reason("stop_sequence") == StopReason.STOP_SEQUENCE
    assert _stop_reason("tool_use") == StopReason.TOOL_USE
    # Unknown / None defaults to END_TURN (forward compatibility).
    assert _stop_reason(None) == StopReason.END_TURN
    assert _stop_reason("future_reason") == StopReason.END_TURN


# ---- Rolling history cache breakpoint (context-assembler.md §3) ---------


def test_history_breakpoint_marks_last_block_of_last_message():
    tm = ToolIdMap()
    wire, _ = _canonical_messages_to_anthropic(
        [_msg(Role.USER, [TextBlock(text="hello")])],
        system_prompt=None,
        tool_map=tm,
    )
    marked = _with_history_cache_breakpoint(wire)
    assert marked[-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}


def test_history_breakpoint_only_on_last_message():
    """Earlier messages must NOT carry the breakpoint — exactly one rolling
    marker per request, on the last message."""
    tm = ToolIdMap()
    wire, _ = _canonical_messages_to_anthropic(
        [
            _msg(Role.USER, [TextBlock(text="first")]),
            _msg(Role.ASSISTANT, [TextBlock(text="reply")]),
            _msg(Role.USER, [TextBlock(text="second")]),
        ],
        system_prompt=None,
        tool_map=tm,
    )
    marked = _with_history_cache_breakpoint(wire)
    assert len(marked) == 3
    assert "cache_control" not in marked[0]["content"][-1]
    assert "cache_control" not in marked[1]["content"][-1]
    assert marked[-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}


def test_history_breakpoint_lands_on_tool_result_block():
    """When the last message is a merged tool-result user message, the
    breakpoint lands on its final tool_result block."""
    tm = ToolIdMap()
    wire, _ = _canonical_messages_to_anthropic(
        [
            _msg(Role.ASSISTANT, [ToolUseBlock(id="tu_a", name="read_file", input={})]),
            _msg(Role.TOOL, [ToolResultBlock(tool_use_id="tu_a", content=[TextBlock(text="r")])]),
        ],
        system_prompt=None,
        tool_map=tm,
    )
    marked = _with_history_cache_breakpoint(wire)
    last_block = marked[-1]["content"][-1]
    assert last_block["type"] == "tool_result"
    assert last_block["cache_control"] == {"type": "ephemeral"}


def test_history_breakpoint_empty_messages_is_noop():
    assert _with_history_cache_breakpoint([]) == []


def test_history_breakpoint_does_not_mutate_input():
    tm = ToolIdMap()
    wire, _ = _canonical_messages_to_anthropic(
        [_msg(Role.USER, [TextBlock(text="hello")])],
        system_prompt=None,
        tool_map=tm,
    )
    _with_history_cache_breakpoint(wire)
    assert "cache_control" not in wire[-1]["content"][-1]
