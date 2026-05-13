"""StreamingHub: per-session fan-out + wire frame conversion."""

from __future__ import annotations

from metis_core.adapters.protocol import StopReason, TokenUsage
from metis_core.adapters.streaming import (
    MessageComplete,
    MessageStart,
    TextDelta,
    ThinkingDelta,
    ToolUseEnd,
    ToolUseInputDelta,
    ToolUseStart,
)
from metis_core.canonical.content import TextBlock
from metis_server.hub import StreamingHub


def test_publish_with_no_subscribers_is_noop():
    hub = StreamingHub()
    # Must not raise.
    hub.publish("sess_1", TextDelta(message_id="m1", content_block_index=0, text="hi"))


def test_subscriber_receives_published_event():
    hub = StreamingHub()
    received: list[dict] = []
    hub.subscribe("sess_1", received.append)
    hub.publish("sess_1", TextDelta(message_id="m1", content_block_index=0, text="hello"))
    assert len(received) == 1
    frame = received[0]
    assert frame["type"] == "event"
    assert frame["event"]["type"] == "text.delta"
    assert frame["event"]["session_id"] == "sess_1"
    assert frame["event"]["payload"]["text"] == "hello"


def test_session_scoped_fanout():
    """A subscriber for sess_1 should NOT see events for sess_2."""
    hub = StreamingHub()
    a, b = [], []
    hub.subscribe("sess_a", a.append)
    hub.subscribe("sess_b", b.append)
    hub.publish("sess_a", TextDelta(message_id="m", content_block_index=0, text="x"))
    assert len(a) == 1
    assert len(b) == 0


def test_unsubscribe_stops_delivery():
    hub = StreamingHub()
    received: list[dict] = []
    unsub = hub.subscribe("sess_1", received.append)
    hub.publish("sess_1", TextDelta(message_id="m", content_block_index=0, text="a"))
    unsub()
    hub.publish("sess_1", TextDelta(message_id="m", content_block_index=0, text="b"))
    assert [f["event"]["payload"]["text"] for f in received] == ["a"]


def test_unsubscribe_is_idempotent():
    hub = StreamingHub()
    unsub = hub.subscribe("s", lambda _f: None)
    unsub()
    unsub()  # must not raise


def test_multiple_subscribers_for_same_session():
    hub = StreamingHub()
    a, b = [], []
    hub.subscribe("sess_1", a.append)
    hub.subscribe("sess_1", b.append)
    hub.publish("sess_1", TextDelta(message_id="m", content_block_index=0, text="hi"))
    assert len(a) == 1
    assert len(b) == 1


def test_subscriber_exception_does_not_break_fanout():
    hub = StreamingHub()
    received: list[dict] = []

    def broken(_frame):
        raise RuntimeError("boom")

    hub.subscribe("s", broken)
    hub.subscribe("s", received.append)
    hub.publish("s", TextDelta(message_id="m", content_block_index=0, text="ok"))
    assert len(received) == 1


# ---- Wire-frame conversion -------------------------------------------------


def test_message_start_frame():
    hub = StreamingHub()
    got: list[dict] = []
    hub.subscribe("s", got.append)
    hub.publish("s", MessageStart(message_id="m1", model="anthropic:claude-sonnet-4-6"))
    p = got[0]["event"]
    assert p["type"] == "message.start"
    assert p["payload"] == {
        "message_id": "m1",
        "role": "assistant",
        "model": "anthropic:claude-sonnet-4-6",
    }


def test_text_delta_frame():
    hub = StreamingHub()
    got: list[dict] = []
    hub.subscribe("s", got.append)
    hub.publish("s", TextDelta(message_id="m1", content_block_index=0, text="hi"))
    p = got[0]["event"]
    assert p["type"] == "text.delta"
    assert p["payload"] == {"message_id": "m1", "content_block_index": 0, "text": "hi"}


def test_thinking_delta_frame():
    hub = StreamingHub()
    got: list[dict] = []
    hub.subscribe("s", got.append)
    hub.publish(
        "s",
        ThinkingDelta(message_id="m", content_block_index=0, text="...", signature="sig"),
    )
    payload = got[0]["event"]["payload"]
    assert payload["signature"] == "sig"


def test_tool_use_start_and_end_frames():
    hub = StreamingHub()
    got: list[dict] = []
    hub.subscribe("s", got.append)
    hub.publish(
        "s",
        ToolUseStart(
            message_id="m", content_block_index=1, tool_use_id="tu_1", tool_name="read_file"
        ),
    )
    hub.publish(
        "s",
        ToolUseInputDelta(
            message_id="m",
            content_block_index=1,
            tool_use_id="tu_1",
            partial_json='{"pa',
        ),
    )
    hub.publish(
        "s",
        ToolUseEnd(
            message_id="m",
            content_block_index=1,
            tool_use_id="tu_1",
            final_input={"path": "README.md"},
        ),
    )
    types = [f["event"]["type"] for f in got]
    assert types == ["tool.use_start", "tool.use_input_delta", "tool.use_end"]
    assert got[2]["event"]["payload"]["final_input"] == {"path": "README.md"}


def test_message_complete_frame_includes_content_and_usage():
    hub = StreamingHub()
    got: list[dict] = []
    hub.subscribe("s", got.append)
    hub.publish(
        "s",
        MessageComplete(
            message_id="m1",
            stop_reason=StopReason.END_TURN,
            final_content=[TextBlock(text="hello")],
            usage=TokenUsage(input_tokens=5, output_tokens=3),
            latency_ms=42,
        ),
    )
    payload = got[0]["event"]["payload"]
    assert payload["stop_reason"] == "end_turn"
    assert payload["final_content"] == [{"type": "text", "text": "hello"}]
    assert payload["usage"]["input_tokens"] == 5
    assert payload["latency_ms"] == 42
