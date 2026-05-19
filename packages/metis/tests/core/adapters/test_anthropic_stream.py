"""Tests for AnthropicAdapter.stream() — SDK SSE chunks → canonical events."""

from __future__ import annotations

import datetime
from types import SimpleNamespace

from metis.core.adapters.anthropic import AnthropicAdapter
from metis.core.adapters.protocol import CanonicalRequest, StopReason
from metis.core.adapters.streaming import (
    MessageComplete,
    MessageStart,
    TextDelta,
    ThinkingDelta,
    ToolUseEnd,
    ToolUseStart,
)
from metis.core.adapters.tool_id_map import ToolIdMap
from metis.core.canonical.content import TextBlock, ToolUseBlock
from metis.core.canonical.messages import Message, MessageMetadata, Role

# ---- Helpers ----------------------------------------------------------


class _AsyncIter:
    def __init__(self, items):
        self._items = list(items)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._items:
            raise StopAsyncIteration
        return self._items.pop(0)


class _FakeMessages:
    def __init__(self, *, stream_events=None):
        self.stream_events = list(stream_events or [])
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("stream"):
            return _AsyncIter(self.stream_events)
        raise AssertionError("expected stream=True")


class _FakeClient:
    def __init__(self, messages=None):
        self.messages = messages or _FakeMessages()

    async def close(self):
        return


def _user_request() -> CanonicalRequest:
    return CanonicalRequest(
        request_id="req_s1",
        messages=[
            Message(
                id="01HZ",
                session_id="s",
                role=Role.USER,
                content=[TextBlock(text="hi")],
                created_at=datetime.datetime.now(datetime.UTC),
                metadata=MessageMetadata(),
            )
        ],
        tools=[],
        system_prompt=None,
        model="anthropic:claude-sonnet-4-6",
        max_output_tokens=128,
        tool_id_map=ToolIdMap(),
    )


# Stream-event factories matching the anthropic SDK shape.


def _evt_message_start(input_tokens=10, cached=0, cache_creation=0):
    return SimpleNamespace(
        type="message_start",
        message=SimpleNamespace(
            usage=SimpleNamespace(
                input_tokens=input_tokens,
                cache_read_input_tokens=cached,
                cache_creation_input_tokens=cache_creation,
            )
        ),
    )


def _evt_text_block_start():
    return SimpleNamespace(
        type="content_block_start",
        content_block=SimpleNamespace(type="text", text=""),
    )


def _evt_text_delta(text):
    return SimpleNamespace(
        type="content_block_delta",
        delta=SimpleNamespace(type="text_delta", text=text),
    )


def _evt_thinking_block_start():
    return SimpleNamespace(
        type="content_block_start",
        content_block=SimpleNamespace(type="thinking", thinking=""),
    )


def _evt_thinking_delta(text):
    return SimpleNamespace(
        type="content_block_delta",
        delta=SimpleNamespace(type="thinking_delta", thinking=text),
    )


def _evt_signature_delta(sig):
    return SimpleNamespace(
        type="content_block_delta",
        delta=SimpleNamespace(type="signature_delta", signature=sig),
    )


def _evt_tool_use_block_start(tool_use_id, name):
    return SimpleNamespace(
        type="content_block_start",
        content_block=SimpleNamespace(type="tool_use", id=tool_use_id, name=name),
    )


def _evt_input_json_delta(partial):
    return SimpleNamespace(
        type="content_block_delta",
        delta=SimpleNamespace(type="input_json_delta", partial_json=partial),
    )


def _evt_content_block_stop():
    return SimpleNamespace(type="content_block_stop")


def _evt_message_delta(stop_reason="end_turn", output_tokens=5):
    return SimpleNamespace(
        type="message_delta",
        delta=SimpleNamespace(stop_reason=stop_reason),
        usage=SimpleNamespace(output_tokens=output_tokens),
    )


def _evt_message_stop():
    return SimpleNamespace(type="message_stop")


# ---- Tests ------------------------------------------------------------


async def test_stream_text_only():
    events = [
        _evt_message_start(input_tokens=12),
        _evt_text_block_start(),
        _evt_text_delta("Hello"),
        _evt_text_delta(" world"),
        _evt_content_block_stop(),
        _evt_message_delta(stop_reason="end_turn", output_tokens=3),
        _evt_message_stop(),
    ]
    adapter = AnthropicAdapter(client=_FakeClient(_FakeMessages(stream_events=events)))

    collected = []
    async for ev in adapter.stream(_user_request()):
        collected.append(ev)

    # Expected event order
    types = [type(ev).__name__ for ev in collected]
    assert types == [
        "MessageStart",
        "TextDelta",
        "TextDelta",
        "MessageComplete",
    ]

    # MessageStart carries the canonical message id and model.
    assert collected[0].model == "anthropic:claude-sonnet-4-6"

    # TextDeltas have correct content and indices.
    assert collected[1].text == "Hello"
    assert collected[2].text == " world"
    assert collected[1].content_block_index == 0
    assert collected[2].content_block_index == 0

    # MessageComplete carries finalized state.
    final = collected[-1]
    assert isinstance(final, MessageComplete)
    assert final.stop_reason == StopReason.END_TURN
    assert len(final.final_content) == 1
    assert isinstance(final.final_content[0], TextBlock)
    assert final.final_content[0].text == "Hello world"
    assert final.usage.input_tokens == 12
    assert final.usage.output_tokens == 3


async def test_stream_tool_use():
    events = [
        _evt_message_start(),
        _evt_tool_use_block_start(tool_use_id="toolu_abc", name="read_file"),
        _evt_input_json_delta('{"path"'),
        _evt_input_json_delta(': "x.md"}'),
        _evt_content_block_stop(),
        _evt_message_delta(stop_reason="tool_use"),
        _evt_message_stop(),
    ]
    req = _user_request()
    adapter = AnthropicAdapter(client=_FakeClient(_FakeMessages(stream_events=events)))

    collected = []
    async for ev in adapter.stream(req):
        collected.append(ev)

    types = [type(ev).__name__ for ev in collected]
    assert types == [
        "MessageStart",
        "ToolUseStart",
        "ToolUseInputDelta",
        "ToolUseInputDelta",
        "ToolUseEnd",
        "MessageComplete",
    ]
    tool_start = next(e for e in collected if isinstance(e, ToolUseStart))
    assert tool_start.tool_name == "read_file"
    # Anthropic uses identity mapping for tool ids; canonical id == provider id.
    assert tool_start.tool_use_id == "toolu_abc"
    # The map records the mapping.
    assert req.tool_id_map.to_provider("toolu_abc") == "toolu_abc"

    tool_end = next(e for e in collected if isinstance(e, ToolUseEnd))
    assert tool_end.final_input == {"path": "x.md"}

    final = collected[-1]
    assert final.stop_reason == StopReason.TOOL_USE
    assert isinstance(final.final_content[0], ToolUseBlock)
    assert final.final_content[0].input == {"path": "x.md"}


async def test_stream_mixed_text_and_tool_use():
    """Text first, then a tool_use — two content blocks at indices 0 and 1."""
    events = [
        _evt_message_start(),
        _evt_text_block_start(),
        _evt_text_delta("I'll check"),
        _evt_content_block_stop(),
        _evt_tool_use_block_start(tool_use_id="toolu_x", name="read_file"),
        _evt_input_json_delta('{"path": "a"}'),
        _evt_content_block_stop(),
        _evt_message_delta(stop_reason="tool_use"),
        _evt_message_stop(),
    ]
    adapter = AnthropicAdapter(client=_FakeClient(_FakeMessages(stream_events=events)))
    collected = []
    async for ev in adapter.stream(_user_request()):
        collected.append(ev)

    # Text block at index 0, tool block at index 1.
    text_delta = next(e for e in collected if isinstance(e, TextDelta))
    assert text_delta.content_block_index == 0
    tool_start = next(e for e in collected if isinstance(e, ToolUseStart))
    assert tool_start.content_block_index == 1

    final = collected[-1]
    assert len(final.final_content) == 2
    assert isinstance(final.final_content[0], TextBlock)
    assert isinstance(final.final_content[1], ToolUseBlock)


async def test_stream_thinking_block():
    events = [
        _evt_message_start(),
        _evt_thinking_block_start(),
        _evt_thinking_delta("reasoning about"),
        _evt_thinking_delta(" the problem..."),
        _evt_signature_delta("sig_xyz"),
        _evt_content_block_stop(),
        _evt_text_block_start(),
        _evt_text_delta("answer"),
        _evt_content_block_stop(),
        _evt_message_delta(stop_reason="end_turn"),
        _evt_message_stop(),
    ]
    adapter = AnthropicAdapter(client=_FakeClient(_FakeMessages(stream_events=events)))
    collected = []
    async for ev in adapter.stream(_user_request()):
        collected.append(ev)

    # Two thinking deltas streamed.
    thinking_events = [e for e in collected if isinstance(e, ThinkingDelta)]
    assert len(thinking_events) == 2

    final = collected[-1]
    # ThinkingBlock first, TextBlock second.
    assert final.final_content[0].text == "reasoning about the problem..."
    assert final.final_content[0].signature == "sig_xyz"


async def test_stream_handles_invalid_json_in_tool_input():
    """Adapter falls back to {} on un-parseable input_json, with the
    MessageComplete still carrying a usable ToolUseBlock."""
    events = [
        _evt_message_start(),
        _evt_tool_use_block_start(tool_use_id="toolu_bad", name="t"),
        _evt_input_json_delta("not json"),
        _evt_content_block_stop(),
        _evt_message_delta(stop_reason="tool_use"),
        _evt_message_stop(),
    ]
    adapter = AnthropicAdapter(client=_FakeClient(_FakeMessages(stream_events=events)))
    collected = []
    async for ev in adapter.stream(_user_request()):
        collected.append(ev)
    final = collected[-1]
    assert isinstance(final.final_content[0], ToolUseBlock)
    assert final.final_content[0].input == {}


async def test_stream_passes_stream_true_to_sdk():
    events = [
        _evt_message_start(),
        _evt_message_delta(),
        _evt_message_stop(),
    ]
    fake = _FakeMessages(stream_events=events)
    adapter = AnthropicAdapter(client=_FakeClient(fake))
    async for _ in adapter.stream(_user_request()):
        pass
    assert fake.calls[0]["stream"] is True


async def test_stream_yields_message_start_before_any_chunk():
    """MessageStart is yielded eagerly, before the SDK call resolves —
    consumers can render an "assistant is thinking..." placeholder immediately."""
    events = [
        _evt_message_start(),
        _evt_text_block_start(),
        _evt_text_delta("hi"),
        _evt_content_block_stop(),
        _evt_message_delta(),
        _evt_message_stop(),
    ]
    adapter = AnthropicAdapter(client=_FakeClient(_FakeMessages(stream_events=events)))
    iterator = adapter.stream(_user_request())
    first = await iterator.__anext__()
    assert isinstance(first, MessageStart)
    # Drain the rest.
    async for _ in iterator:
        pass
