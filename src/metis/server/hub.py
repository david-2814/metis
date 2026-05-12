"""Per-session streaming event hub.

Streaming events (`text.delta`, `tool.use_*`, `message.*`) are a transient
layer separate from the bus catalog (streaming-protocol.md §5.1). They
flow from the session manager's `on_streaming_event` callback into this
hub, which fans them out to all WebSocket connections subscribed to the
session. Bus events take the normal Subscription path; the WS handler
listens on both.

The hub holds no state beyond the active subscriber list; each turn's
events are published live and not buffered.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from metis.adapters.streaming import (
    MessageComplete,
    MessageStart,
    StreamingEvent,
    TextDelta,
    ThinkingDelta,
    ToolUseEnd,
    ToolUseInputDelta,
    ToolUseStart,
)
from metis.canonical.content import ContentBlock

logger = logging.getLogger(__name__)


# A subscriber callable receives the streaming-event wire frame (already
# converted to the envelope shape) and enqueues it onto its outbound queue.
StreamingSubscriber = Callable[[dict], None]


class StreamingHub:
    """Per-session fan-out for streaming-only events."""

    def __init__(self) -> None:
        self._subscribers: dict[str, list[StreamingSubscriber]] = {}

    def subscribe(
        self, session_id: str, enqueue: StreamingSubscriber
    ) -> Callable[[], None]:
        """Register an enqueue callback for `session_id`; returns an
        idempotent unsubscribe function."""
        self._subscribers.setdefault(session_id, []).append(enqueue)

        def unsubscribe() -> None:
            subs = self._subscribers.get(session_id)
            if subs is None:
                return
            try:
                subs.remove(enqueue)
            except ValueError:
                return
            if not subs:
                self._subscribers.pop(session_id, None)

        return unsubscribe

    def publish(self, session_id: str, event: StreamingEvent) -> None:
        """Convert `event` to its wire frame and fan out to all current
        subscribers for `session_id`. No-op if there are none."""
        subs = self._subscribers.get(session_id)
        if not subs:
            return
        frame = _frame_for_event(session_id, event)
        for enq in list(subs):
            try:
                enq(frame)
            except Exception:
                logger.exception("streaming hub subscriber failed")


# ---------------------------------------------------------------------------
# Wire-format encoders (streaming-protocol.md §5.3)
# ---------------------------------------------------------------------------


def _frame_for_event(session_id: str, event: StreamingEvent) -> dict:
    """Build a `{type: "event", event: {...}}` frame for a streaming event.

    The inner envelope mirrors the bus Event envelope (type, session_id,
    payload, actor) but without an id/timestamp/sensitivity since streaming
    events are not persisted. Clients differentiate streaming vs catalog
    events by `event.type` only.
    """
    event_type, payload = _type_and_payload(event)
    return {
        "type": "event",
        "event": {
            "type": event_type,
            "session_id": session_id,
            "turn_id": None,
            "parent_event_id": None,
            "actor": "agent",
            "payload": payload,
        },
    }


def _type_and_payload(event: StreamingEvent) -> tuple[str, dict]:
    if isinstance(event, MessageStart):
        return "message.start", {
            "message_id": event.message_id,
            "role": "assistant",
            "model": event.model,
        }
    if isinstance(event, TextDelta):
        return "text.delta", {
            "message_id": event.message_id,
            "content_block_index": event.content_block_index,
            "text": event.text,
        }
    if isinstance(event, ThinkingDelta):
        return "thinking.delta", {
            "message_id": event.message_id,
            "content_block_index": event.content_block_index,
            "text": event.text,
            "signature": event.signature,
        }
    if isinstance(event, ToolUseStart):
        return "tool.use_start", {
            "message_id": event.message_id,
            "content_block_index": event.content_block_index,
            "tool_use_id": event.tool_use_id,
            "tool_name": event.tool_name,
        }
    if isinstance(event, ToolUseInputDelta):
        return "tool.use_input_delta", {
            "message_id": event.message_id,
            "content_block_index": event.content_block_index,
            "tool_use_id": event.tool_use_id,
            "partial_json": event.partial_json,
        }
    if isinstance(event, ToolUseEnd):
        return "tool.use_end", {
            "message_id": event.message_id,
            "content_block_index": event.content_block_index,
            "tool_use_id": event.tool_use_id,
            "final_input": event.final_input,
        }
    if isinstance(event, MessageComplete):
        return "message.complete", {
            "message_id": event.message_id,
            "stop_reason": event.stop_reason.value,
            "final_content": [_content_block_to_dict(b) for b in event.final_content],
            "usage": {
                "input_tokens": event.usage.input_tokens,
                "output_tokens": event.usage.output_tokens,
                "cached_input_tokens": event.usage.cached_input_tokens,
                "cache_creation_input_tokens": event.usage.cache_creation_input_tokens,
            },
            "latency_ms": event.latency_ms,
        }
    raise TypeError(f"unknown streaming event type: {type(event).__name__}")


def _content_block_to_dict(block: ContentBlock) -> Any:
    import msgspec

    return msgspec.to_builtins(block)
