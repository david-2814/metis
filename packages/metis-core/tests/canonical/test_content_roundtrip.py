"""JSON round-trip tests for content blocks.

Verifies msgspec's tagged-union encoding works as expected: the `type`
discriminator is the wire format, and decoding picks the right concrete class.
"""

from __future__ import annotations

import msgspec
from metis_core.canonical.content import (
    ContentBlock,
    ImageBlock,
    ImageSource,
    RedactedThinkingBlock,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
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
