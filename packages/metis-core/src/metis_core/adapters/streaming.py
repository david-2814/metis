"""Canonical streaming event types.

Per streaming-protocol.md §5.3 these are a SEPARATE transient layer from the
bus catalog (events.payloads). They flow directly from the agent loop /
adapter to the streaming server (or CLI), are not persisted in the trace
store, and are reconstructible from the final canonical Message plus the
`usage` totals on `llm.call_completed`.

The naming follows the spec: `<domain>.<verb>`. We use frozen dataclasses
(not msgspec.Struct) because these events never hit the wire as JSON — they
live entirely in-process between the adapter and the consumer.
"""

from __future__ import annotations

from dataclasses import dataclass

from metis_core.adapters.protocol import StopReason, TokenUsage
from metis_core.canonical.content import ContentBlock


@dataclass(frozen=True)
class MessageStart:
    """`message.start` — a new ASSISTANT message is beginning to stream."""

    message_id: str
    model: str  # canonical "provider:name"


@dataclass(frozen=True)
class TextDelta:
    """`text.delta` — a chunk of text within a TextBlock."""

    message_id: str
    content_block_index: int
    text: str


@dataclass(frozen=True)
class ThinkingDelta:
    """`thinking.delta` — a chunk of thinking text (Anthropic models)."""

    message_id: str
    content_block_index: int
    text: str
    signature: str | None = None  # only populated on the final delta of the block


@dataclass(frozen=True)
class ToolUseStart:
    """`tool.use_start` — assistant has begun emitting a tool call."""

    message_id: str
    content_block_index: int
    tool_use_id: str  # canonical id
    tool_name: str


@dataclass(frozen=True)
class ToolUseInputDelta:
    """`tool.use_input_delta` — partial JSON string for tool input arguments.

    Per spec §5.5, v1 streams raw partial JSON fragments. Clients accumulate
    them but only the final `ToolUseEnd.final_input` is authoritative."""

    message_id: str
    content_block_index: int
    tool_use_id: str
    partial_json: str


@dataclass(frozen=True)
class ToolUseEnd:
    """`tool.use_end` — tool call args fully streamed; `final_input` is the
    parsed authoritative value."""

    message_id: str
    content_block_index: int
    tool_use_id: str
    final_input: dict


@dataclass(frozen=True)
class MessageComplete:
    """`message.complete` — assistant message is final.

    Carries the assembled canonical content + finalized usage. Consumers
    that incrementally accumulated deltas should reconcile against
    `final_content` (per spec §5.3 it's authoritative)."""

    message_id: str
    stop_reason: StopReason
    final_content: list[ContentBlock]
    usage: TokenUsage
    latency_ms: int


# Tagged union of all streaming event types.
StreamingEvent = (
    MessageStart
    | TextDelta
    | ThinkingDelta
    | ToolUseStart
    | ToolUseInputDelta
    | ToolUseEnd
    | MessageComplete
)


__all__ = [
    "MessageComplete",
    "MessageStart",
    "StreamingEvent",
    "TextDelta",
    "ThinkingDelta",
    "ToolUseEnd",
    "ToolUseInputDelta",
    "ToolUseStart",
]
