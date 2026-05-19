"""Tests for OpenAIAdapter.stream() — SDK delta chunks → canonical events."""

from __future__ import annotations

import datetime
from types import SimpleNamespace

from metis.core.adapters.openai import OpenAIAdapter
from metis.core.adapters.protocol import CanonicalRequest, StopReason
from metis.core.adapters.streaming import (
    MessageComplete,
    ToolUseEnd,
    ToolUseInputDelta,
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


class _FakeCompletions:
    def __init__(self, *, stream_chunks=None):
        self.stream_chunks = list(stream_chunks or [])
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if not kwargs.get("stream"):
            raise AssertionError("expected stream=True")
        return _AsyncIter(self.stream_chunks)


class _FakeChat:
    def __init__(self, completions):
        self.completions = completions


class _FakeClient:
    def __init__(self, completions):
        self.chat = _FakeChat(completions)

    async def close(self):
        return


def _user_request() -> CanonicalRequest:
    return CanonicalRequest(
        request_id="req_oai_s1",
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
        model="openai:gpt-5",
        max_output_tokens=128,
        tool_id_map=ToolIdMap(),
    )


def _role_chunk():
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(role="assistant", content=None, tool_calls=None),
                finish_reason=None,
            )
        ],
        usage=None,
    )


def _text_chunk(text):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content=text, tool_calls=None),
                finish_reason=None,
            )
        ],
        usage=None,
    )


def _tool_call_start(index, call_id, name):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(
                    content=None,
                    tool_calls=[
                        SimpleNamespace(
                            index=index,
                            id=call_id,
                            type="function",
                            function=SimpleNamespace(name=name, arguments=""),
                        )
                    ],
                ),
                finish_reason=None,
            )
        ],
        usage=None,
    )


def _tool_call_args(index, args):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(
                    content=None,
                    tool_calls=[
                        SimpleNamespace(
                            index=index,
                            id=None,
                            type=None,
                            function=SimpleNamespace(name=None, arguments=args),
                        )
                    ],
                ),
                finish_reason=None,
            )
        ],
        usage=None,
    )


def _finish_chunk(finish_reason):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content=None, tool_calls=None),
                finish_reason=finish_reason,
            )
        ],
        usage=None,
    )


def _usage_chunk(prompt=20, completion=5, cached=0):
    return SimpleNamespace(
        choices=[],
        usage=SimpleNamespace(
            prompt_tokens=prompt,
            completion_tokens=completion,
            prompt_tokens_details=SimpleNamespace(cached_tokens=cached),
        ),
    )


# ---- Tests ------------------------------------------------------------


async def test_stream_text_only():
    chunks = [
        _role_chunk(),
        _text_chunk("Hello "),
        _text_chunk("world"),
        _finish_chunk("stop"),
        _usage_chunk(prompt=100, completion=2),
    ]
    adapter = OpenAIAdapter(client=_FakeClient(_FakeCompletions(stream_chunks=chunks)))

    collected = []
    async for ev in adapter.stream(_user_request()):
        collected.append(ev)

    types = [type(ev).__name__ for ev in collected]
    assert types == ["MessageStart", "TextDelta", "TextDelta", "MessageComplete"]
    assert collected[1].text == "Hello "
    assert collected[2].text == "world"
    final = collected[-1]
    assert final.stop_reason == StopReason.END_TURN
    assert final.final_content[0].text == "Hello world"
    assert final.usage.input_tokens == 100
    assert final.usage.output_tokens == 2


async def test_stream_single_tool_call():
    chunks = [
        _role_chunk(),
        _tool_call_start(index=0, call_id="call_xyz", name="read_file"),
        _tool_call_args(index=0, args='{"path"'),
        _tool_call_args(index=0, args=': "x.md"}'),
        _finish_chunk("tool_calls"),
        _usage_chunk(),
    ]
    req = _user_request()
    adapter = OpenAIAdapter(client=_FakeClient(_FakeCompletions(stream_chunks=chunks)))

    collected = []
    async for ev in adapter.stream(req):
        collected.append(ev)

    # One ToolUseStart, two ToolUseInputDelta, one ToolUseEnd.
    tool_starts = [e for e in collected if isinstance(e, ToolUseStart)]
    tool_input_deltas = [e for e in collected if isinstance(e, ToolUseInputDelta)]
    tool_ends = [e for e in collected if isinstance(e, ToolUseEnd)]
    assert len(tool_starts) == 1
    assert len(tool_input_deltas) == 2
    assert len(tool_ends) == 1
    assert tool_starts[0].tool_name == "read_file"
    # Canonical id is generated; provider id mapped.
    canonical_id = tool_starts[0].tool_use_id
    assert canonical_id.startswith("tu_")
    assert req.tool_id_map.to_canonical("call_xyz") == canonical_id
    assert tool_ends[0].final_input == {"path": "x.md"}

    final = collected[-1]
    assert isinstance(final, MessageComplete)
    assert final.stop_reason == StopReason.TOOL_USE
    assert isinstance(final.final_content[0], ToolUseBlock)
    assert final.final_content[0].input == {"path": "x.md"}


async def test_stream_parallel_tool_calls():
    """Two tool_calls at different indices stream interleaved."""
    chunks = [
        _role_chunk(),
        _tool_call_start(index=0, call_id="call_a", name="read_file"),
        _tool_call_start(index=1, call_id="call_b", name="list_dir"),
        _tool_call_args(index=0, args='{"path": "a.md"}'),
        _tool_call_args(index=1, args='{"path": "."}'),
        _finish_chunk("tool_calls"),
        _usage_chunk(),
    ]
    adapter = OpenAIAdapter(client=_FakeClient(_FakeCompletions(stream_chunks=chunks)))
    collected = []
    async for ev in adapter.stream(_user_request()):
        collected.append(ev)

    tool_starts = [e for e in collected if isinstance(e, ToolUseStart)]
    assert len(tool_starts) == 2
    names = sorted([t.tool_name for t in tool_starts])
    assert names == ["list_dir", "read_file"]

    final = collected[-1]
    assert len(final.final_content) == 2
    tool_blocks = [b for b in final.final_content if isinstance(b, ToolUseBlock)]
    assert len(tool_blocks) == 2


async def test_stream_text_then_tool_call():
    """Sometimes the model emits text before a tool call. Both end up in final_content."""
    chunks = [
        _role_chunk(),
        _text_chunk("I'll check that."),
        _tool_call_start(index=0, call_id="call_x", name="read_file"),
        _tool_call_args(index=0, args='{"path": "x"}'),
        _finish_chunk("tool_calls"),
        _usage_chunk(),
    ]
    adapter = OpenAIAdapter(client=_FakeClient(_FakeCompletions(stream_chunks=chunks)))
    collected = []
    async for ev in adapter.stream(_user_request()):
        collected.append(ev)

    final = collected[-1]
    # Text comes first, tool second.
    assert isinstance(final.final_content[0], TextBlock)
    assert final.final_content[0].text == "I'll check that."
    assert isinstance(final.final_content[1], ToolUseBlock)


async def test_stream_invalid_json_arguments_falls_back_to_empty():
    chunks = [
        _role_chunk(),
        _tool_call_start(index=0, call_id="call_bad", name="t"),
        _tool_call_args(index=0, args="not json"),
        _finish_chunk("tool_calls"),
        _usage_chunk(),
    ]
    adapter = OpenAIAdapter(client=_FakeClient(_FakeCompletions(stream_chunks=chunks)))
    collected = []
    async for ev in adapter.stream(_user_request()):
        collected.append(ev)
    final = collected[-1]
    assert isinstance(final.final_content[0], ToolUseBlock)
    assert final.final_content[0].input == {}


async def test_stream_options_include_usage_set():
    """stream_options.include_usage=True is required for usage in the stream."""
    chunks = [_role_chunk(), _text_chunk("x"), _finish_chunk("stop"), _usage_chunk()]
    fake = _FakeCompletions(stream_chunks=chunks)
    adapter = OpenAIAdapter(client=_FakeClient(fake))
    async for _ in adapter.stream(_user_request()):
        pass
    assert fake.calls[0].get("stream_options") == {"include_usage": True}
