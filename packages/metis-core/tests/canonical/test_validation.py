"""Tests for message-level invariants from canonical-message-format.md §5."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from metis_core.canonical.content import (
    ImageBlock,
    ImageSource,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from metis_core.canonical.ids import new_message_id, new_session_id, new_tool_use_id
from metis_core.canonical.messages import (
    Message,
    MessageMetadata,
    MessageStatus,
    Role,
    RoutingDecisionRecord,
    RoutingMode,
    Usage,
)
from metis_core.canonical.validation import validate_message


def _now() -> datetime:
    return datetime.now(UTC)


def _complete_assistant_metadata() -> MessageMetadata:
    return MessageMetadata(
        model="anthropic:claude-sonnet-4-6",
        provider="anthropic",
        routing=RoutingDecisionRecord(
            mode=RoutingMode.DEFAULT,
            chosen_model="anthropic:claude-sonnet-4-6",
            reason="workspace default",
        ),
        usage=Usage(
            input_tokens=10,
            output_tokens=5,
            cost_usd=Decimal("0.0001"),
            pricing_version="v1",
            latency_ms=100,
        ),
    )


def _msg(role: Role, content: list, **kwargs) -> Message:
    return Message(
        id=new_message_id(),
        session_id=new_session_id(),
        role=role,
        content=content,
        created_at=_now(),
        **kwargs,
    )


# ---- Happy paths --------------------------------------------------------


def test_user_text_message_valid():
    msg = _msg(Role.USER, [TextBlock(text="hi")])
    assert validate_message(msg) == []


def test_user_with_image_valid():
    msg = _msg(
        Role.USER,
        [
            TextBlock(text="look"),
            ImageBlock(
                source=ImageSource(kind="base64", data="ZmFrZQ=="),
                media_type="image/png",
            ),
        ],
    )
    assert validate_message(msg) == []


def test_assistant_with_full_metadata_valid():
    msg = _msg(
        Role.ASSISTANT,
        [TextBlock(text="ok")],
        metadata=_complete_assistant_metadata(),
    )
    assert validate_message(msg) == []


def test_tool_message_valid():
    msg = _msg(
        Role.TOOL,
        [ToolResultBlock(tool_use_id=new_tool_use_id(), content=[TextBlock(text="result")])],
        metadata=MessageMetadata(parent_tool_use_id=new_tool_use_id()),
    )
    assert validate_message(msg) == []


def test_system_text_message_valid():
    msg = _msg(Role.SYSTEM, [TextBlock(text="you are helpful")])
    assert validate_message(msg) == []


def test_empty_system_message_valid():
    """§5.1.2: SYSTEM may have empty content."""
    msg = _msg(Role.SYSTEM, [])
    assert validate_message(msg) == []


# ---- Failure cases ------------------------------------------------------


def test_empty_user_message_invalid():
    msg = _msg(Role.USER, [])
    errors = validate_message(msg)
    assert any("non-empty" in e for e in errors)


def test_user_with_tool_use_invalid():
    """USER messages cannot carry tool_use blocks."""
    msg = _msg(Role.USER, [ToolUseBlock(id="tu_x", name="t", input={})])
    errors = validate_message(msg)
    assert any("ToolUseBlock not allowed for user" in e for e in errors)


def test_assistant_with_tool_result_invalid():
    """ASSISTANT messages cannot carry tool_result blocks."""
    msg = _msg(
        Role.ASSISTANT,
        [ToolResultBlock(tool_use_id="tu_x", content=[TextBlock(text="r")])],
        metadata=_complete_assistant_metadata(),
    )
    errors = validate_message(msg)
    assert any("ToolResultBlock not allowed for assistant" in e for e in errors)


def test_tool_message_with_two_blocks_invalid():
    msg = _msg(
        Role.TOOL,
        [
            ToolResultBlock(tool_use_id="tu_a", content=[TextBlock(text="a")]),
            ToolResultBlock(tool_use_id="tu_b", content=[TextBlock(text="b")]),
        ],
        metadata=MessageMetadata(parent_tool_use_id="tu_a"),
    )
    errors = validate_message(msg)
    assert any("exactly one content block" in e for e in errors)


def test_system_with_image_invalid():
    msg = _msg(
        Role.SYSTEM,
        [ImageBlock(source=ImageSource(kind="url", data="x"), media_type="image/png")],
    )
    errors = validate_message(msg)
    assert any("ImageBlock not allowed for system" in e for e in errors)


def test_complete_assistant_missing_metadata_invalid():
    """§5.3: COMPLETE ASSISTANT messages require model/provider/routing/usage."""
    msg = _msg(Role.ASSISTANT, [TextBlock(text="ok")])
    errors = validate_message(msg)
    assert any("metadata.model" in e for e in errors)
    assert any("metadata.provider" in e for e in errors)
    assert any("metadata.routing" in e for e in errors)
    assert any("metadata.usage" in e for e in errors)


def test_tool_message_missing_parent_tool_use_id_invalid():
    msg = _msg(
        Role.TOOL,
        [ToolResultBlock(tool_use_id=new_tool_use_id(), content=[TextBlock(text="r")])],
    )
    errors = validate_message(msg)
    assert any("parent_tool_use_id" in e for e in errors)


# ---- PARTIAL bypass -----------------------------------------------------


def test_partial_message_skips_validation():
    """§5.1.5: PARTIAL messages may violate other invariants."""
    msg = _msg(
        Role.ASSISTANT,
        [],  # would normally fail (empty content + missing metadata)
        metadata=MessageMetadata(status=MessageStatus.PARTIAL),
    )
    assert validate_message(msg) == []


# ---- Mode mapping coverage ---------------------------------------------


@pytest.mark.parametrize(
    "mode",
    [
        RoutingMode.OVERRIDE,
        RoutingMode.MANUAL,
        RoutingMode.RULE,
        RoutingMode.PATTERN,
        RoutingMode.DELEGATE,
        RoutingMode.DEFAULT,
    ],
)
def test_all_routing_modes_accepted(mode):
    md = _complete_assistant_metadata()
    md = MessageMetadata(
        model=md.model,
        provider=md.provider,
        routing=RoutingDecisionRecord(
            mode=mode,
            chosen_model="anthropic:claude-sonnet-4-6",
            reason="test",
        ),
        usage=md.usage,
    )
    msg = _msg(Role.ASSISTANT, [TextBlock(text="ok")], metadata=md)
    assert validate_message(msg) == []
