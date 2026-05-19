"""Content blocks — the tagged union that makes up a Message's content list.

See canonical-message-format.md §4.2.
"""

from __future__ import annotations

import logging
from typing import Literal

import msgspec

logger = logging.getLogger(__name__)


class TextBlock(msgspec.Struct, tag="text", tag_field="type", frozen=True):
    text: str


class ImageSource(msgspec.Struct, frozen=True):
    kind: Literal["base64", "url", "file_ref"]
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


_KNOWN_CONTENT_TAGS: frozenset[str] = frozenset(
    {"text", "image", "tool_use", "tool_result", "thinking", "redacted_thinking"}
)


def decode_content_blocks_tolerant(data: bytes | str | list[dict]) -> list[ContentBlock]:
    """Decode a list of content blocks, skipping unknown `type` discriminators.

    Per canonical-message-format.md §10.3, code reading messages MUST tolerate
    unknown content-block types — skip with warning rather than crash. The strict
    tagged-union decoder raises on unknown `type`, which is correct for outbound
    encoding but unsafe for inbound reads during schema drift / partial deploys.
    """
    if isinstance(data, (bytes, str)):
        raw = msgspec.json.decode(data)
    else:
        raw = data
    if not isinstance(raw, list):
        raise msgspec.ValidationError(f"expected list of content blocks, got {type(raw).__name__}")
    blocks: list[ContentBlock] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise msgspec.ValidationError(
                f"content block at index {index}: expected object, got {type(item).__name__}"
            )
        tag = item.get("type")
        if tag not in _KNOWN_CONTENT_TAGS:
            logger.warning(
                "skipping unknown content block type %r at index %d (canonical-message-format §10.3)",
                tag,
                index,
            )
            continue
        blocks.append(msgspec.convert(item, type=ContentBlock))
    return blocks
