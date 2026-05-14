"""JSON round-trip tests for content blocks.

Verifies msgspec's tagged-union encoding works as expected: the `type`
discriminator is the wire format, and decoding picks the right concrete class.
"""

from __future__ import annotations

import logging

import msgspec
import pytest
from metis_core.canonical.content import (
    ContentBlock,
    ImageBlock,
    ImageSource,
    RedactedThinkingBlock,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    decode_content_blocks_tolerant,
)

# Use the tagged union for decoding so msgspec dispatches on `type`.
_decoder = msgspec.json.Decoder(ContentBlock)
_encoder = msgspec.json.Encoder()


def _roundtrip(block: ContentBlock) -> ContentBlock:
    return _decoder.decode(_encoder.encode(block))


def test_text_block_roundtrip():
    block = TextBlock(text="hello")
    out = _roundtrip(block)
    assert out == block
    assert isinstance(out, TextBlock)


def test_text_block_wire_shape():
    block = TextBlock(text="hi")
    payload = msgspec.json.decode(_encoder.encode(block))
    assert payload == {"type": "text", "text": "hi"}


def test_tool_use_block_roundtrip():
    block = ToolUseBlock(id="tu_01HZ0001", name="read_file", input={"path": "README.md"})
    out = _roundtrip(block)
    assert out == block
    assert isinstance(out, ToolUseBlock)


def test_tool_result_block_with_text_roundtrip():
    block = ToolResultBlock(
        tool_use_id="tu_01HZ0001",
        content=[TextBlock(text="file content here")],
    )
    out = _roundtrip(block)
    assert out == block
    assert isinstance(out, ToolResultBlock)
    assert isinstance(out.content[0], TextBlock)


def test_tool_result_block_with_image_roundtrip():
    block = ToolResultBlock(
        tool_use_id="tu_01HZ0002",
        content=[
            TextBlock(text="see image"),
            ImageBlock(
                source=ImageSource(kind="base64", data="ZmFrZQ=="),
                media_type="image/png",
            ),
        ],
        is_error=False,
    )
    out = _roundtrip(block)
    assert out == block


def test_image_block_roundtrip():
    block = ImageBlock(
        source=ImageSource(kind="url", data="https://example.invalid/x.png"),
        media_type="image/png",
    )
    out = _roundtrip(block)
    assert out == block
    assert isinstance(out, ImageBlock)


def test_thinking_block_roundtrip():
    block = ThinkingBlock(text="reasoning...", signature="sig_abc")
    out = _roundtrip(block)
    assert out == block
    assert isinstance(out, ThinkingBlock)


def test_thinking_block_without_signature():
    block = ThinkingBlock(text="reasoning...")
    out = _roundtrip(block)
    assert out == block
    assert out.signature is None


def test_redacted_thinking_block_roundtrip():
    block = RedactedThinkingBlock(data="opaque-blob")
    out = _roundtrip(block)
    assert out == block
    assert isinstance(out, RedactedThinkingBlock)


def test_tag_field_is_type():
    """The wire format uses `type` as the discriminator, matching the spec."""
    payload = msgspec.json.decode(_encoder.encode(ToolUseBlock(id="tu_x", name="t", input={})))
    assert payload["type"] == "tool_use"


# ---- §10.3 tolerance: unknown content block types -----------------------


def test_strict_decode_rejects_unknown_content_type():
    """Sanity: the strict tagged-union decoder raises on unknown `type`.

    This is the failure mode that decode_content_blocks_tolerant exists to
    paper over (canonical-message-format.md §10.3).
    """
    raw = msgspec.json.encode([{"type": "future_block", "data": "x"}])
    with pytest.raises(msgspec.ValidationError):
        msgspec.json.decode(raw, type=list[ContentBlock])


def test_tolerant_decode_skips_unknown_content_block(caplog: pytest.LogCaptureFixture):
    """canonical-message-format.md §10.3: unknown content-block types are
    skipped with a warning, not raised."""
    payload = [
        {"type": "text", "text": "hello"},
        {"type": "future_block", "experimental_field": 42},
        {"type": "tool_use", "id": "tu_x", "name": "t", "input": {}},
    ]
    with caplog.at_level(logging.WARNING, logger="metis_core.canonical.content"):
        blocks = decode_content_blocks_tolerant(msgspec.json.encode(payload))
    assert len(blocks) == 2
    assert isinstance(blocks[0], TextBlock)
    assert isinstance(blocks[1], ToolUseBlock)
    assert any("future_block" in r.message for r in caplog.records)


def test_tolerant_decode_accepts_list_of_dicts_directly():
    blocks = decode_content_blocks_tolerant(
        [
            {"type": "text", "text": "a"},
            {"type": "unknown", "x": 1},
            {"type": "text", "text": "b"},
        ]
    )
    assert [b.text for b in blocks if isinstance(b, TextBlock)] == ["a", "b"]


def test_tolerant_decode_still_rejects_malformed_known_block():
    """Tolerance is for unknown `type`, not for malformed payloads of known
    types — those should still raise so bugs aren't silently swallowed."""
    payload = [{"type": "text"}]  # missing required `text` field
    with pytest.raises(msgspec.ValidationError):
        decode_content_blocks_tolerant(msgspec.json.encode(payload))


# ---- ImageSource.kind constraint ---------------------------------------


def test_image_source_kind_rejects_unknown_value():
    """canonical-message-format §4.2: ImageSource.kind ∈ {base64, url, file_ref}."""
    payload = msgspec.json.encode(
        ImageBlock(
            source=ImageSource(kind="base64", data="ZmFrZQ=="),
            media_type="image/png",
        )
    )
    # Sanity: known kinds decode.
    msgspec.json.decode(payload, type=ContentBlock)

    # Garbage kinds raise instead of silently decoding.
    garbage = msgspec.json.encode(
        {
            "type": "image",
            "source": {"kind": "ftp", "data": "x"},
            "media_type": "image/png",
        }
    )
    with pytest.raises(msgspec.ValidationError):
        msgspec.json.decode(garbage, type=ContentBlock)
