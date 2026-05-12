"""Content blocks — the tagged union that makes up a Message's content list.

See canonical-message-format.md §4.2.
"""

from __future__ import annotations

import msgspec


class TextBlock(msgspec.Struct, tag="text", tag_field="type", frozen=True):
    text: str


class ImageSource(msgspec.Struct, frozen=True):
    kind: str  # "base64" | "url" | "file_ref"
    data: str


class ImageBlock(msgspec.Struct, tag="image", tag_field="type", frozen=True):
    source: ImageSource
    media_type: str


class ToolUseBlock(msgspec.Struct, tag="tool_use", tag_field="type", frozen=True):
    id: str  # canonical, format: tu_<ulid>
    name: str  # canonical tool name
    input: dict  # JSON-Schema-validated against tool definition


class ToolResultBlock(msgspec.Struct, tag="tool_result", tag_field="type", frozen=True):
    tool_use_id: str  # FK to ToolUseBlock.id
    content: list[ContentBlock]  # usually [TextBlock]; may include ImageBlock
    is_error: bool = False


class ThinkingBlock(msgspec.Struct, tag="thinking", tag_field="type", frozen=True):
    text: str
    signature: str | None = None  # opaque provider token (Anthropic verifiability)


class RedactedThinkingBlock(msgspec.Struct, tag="redacted_thinking", tag_field="type", frozen=True):
    data: str  # opaque provider-encoded blob


ContentBlock = (
    TextBlock | ImageBlock | ToolUseBlock | ToolResultBlock | ThinkingBlock | RedactedThinkingBlock
)
