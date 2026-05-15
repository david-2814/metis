"""JSON round-trip tests for Message and its metadata."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import msgspec
from metis_core.canonical.content import TextBlock, ToolUseBlock
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


def _now() -> datetime:
    return datetime.now(UTC)


def test_minimal_user_message_roundtrip():
    msg = Message(
        id=new_message_id(),
        session_id=new_session_id(),
        role=Role.USER,
        content=[TextBlock(text="hi")],
        created_at=_now(),
    )
    encoded = msgspec.json.encode(msg)
    decoded = msgspec.json.decode(encoded, type=Message)
    assert decoded == msg


def test_assistant_message_with_full_metadata_roundtrip():
    msg = Message(
        id=new_message_id(),
        session_id=new_session_id(),
        role=Role.ASSISTANT,
        content=[
            TextBlock(text="Reading the file."),
            ToolUseBlock(id=new_tool_use_id(), name="read_file", input={"path": "x.md"}),
        ],
        created_at=_now(),
        metadata=MessageMetadata(
            model="anthropic:claude-sonnet-4-6",
            provider="anthropic",
            routing=RoutingDecisionRecord(
                mode=RoutingMode.RULE,
                chosen_model="anthropic:claude-sonnet-4-6",
                reason="rule 'balanced default'",
                rule_name="balanced default",
            ),
            usage=Usage(
                input_tokens=42,
                output_tokens=17,
                cached_input_tokens=0,
                cache_creation_input_tokens=0,
                cost_usd=Decimal("0.000891"),
                pricing_version="2026-05-08",
                latency_ms=820,
            ),
        ),
    )
    encoded = msgspec.json.encode(msg)
    decoded = msgspec.json.decode(encoded, type=Message)
    assert decoded == msg
    assert decoded.metadata.usage is not None
    assert decoded.metadata.usage.cost_usd == Decimal("0.000891")


def test_decimal_serializes_to_string():
    """Cost as Decimal must round-trip without float drift."""
    cost = Decimal("0.123456789")
    usage = Usage(
        input_tokens=1,
        output_tokens=1,
        cost_usd=cost,
        pricing_version="v1",
        latency_ms=10,
    )
    encoded = msgspec.json.encode(usage)
    # msgspec serializes Decimal as a JSON string by default.
    assert b'"0.123456789"' in encoded
    decoded = msgspec.json.decode(encoded, type=Usage)
    assert decoded.cost_usd == cost


def test_partial_message_can_omit_metadata():
    msg = Message(
        id=new_message_id(),
        session_id=new_session_id(),
        role=Role.ASSISTANT,
        content=[],
        created_at=_now(),
        metadata=MessageMetadata(status=MessageStatus.PARTIAL),
    )
    encoded = msgspec.json.encode(msg)
    decoded = msgspec.json.decode(encoded, type=Message)
    assert decoded.metadata.status == MessageStatus.PARTIAL


def test_provider_raw_opaque_roundtrip():
    """provider_raw is an opaque dict that the canonical layer never inspects."""
    md = MessageMetadata(
        model="anthropic:claude-sonnet-4-6",
        provider="anthropic",
        provider_raw={"stop_reason_raw": "end_turn", "thinking_sig": "abc"},
    )
    encoded = msgspec.json.encode(md)
    decoded = msgspec.json.decode(encoded, type=MessageMetadata)
    assert decoded.provider_raw == {"stop_reason_raw": "end_turn", "thinking_sig": "abc"}


def test_user_team_metadata_roundtrip():
    """multi-user.md §3 / §4.4: user_id and team_id round-trip on metadata."""
    md = MessageMetadata(
        model="anthropic:claude-sonnet-4-6",
        provider="anthropic",
        user_id="usr_01HZALICE",
        team_id="team_01HZENG",
    )
    encoded = msgspec.json.encode(md)
    decoded = msgspec.json.decode(encoded, type=MessageMetadata)
    assert decoded.user_id == "usr_01HZALICE"
    assert decoded.team_id == "team_01HZENG"


def test_legacy_metadata_decodes_with_user_team_none():
    """Pre-multi-user persisted metadata omits user_id / team_id; decode cleanly."""
    legacy_wire = {
        "model": "anthropic:claude-sonnet-4-6",
        "provider": "anthropic",
        "status": "complete",
    }
    decoded = msgspec.convert(legacy_wire, MessageMetadata)
    assert decoded.user_id is None
    assert decoded.team_id is None
