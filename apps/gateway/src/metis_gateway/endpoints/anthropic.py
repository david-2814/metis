"""Anthropic-shape inbound endpoint: `POST /v1/messages`.

Per `gateway.md §3.1` and `§4.2`, this is the inbound surface for clients
that already speak Anthropic Messages API (Claude Code, Anthropic SDK,
Cursor configured Anthropic-side). Translation to the canonical IR is
nearly identity — the canonical IR was designed as a superset of
Anthropic's content-block model (`canonical-message-format.md §4.2`).

Lossless round-trip is the bar (`gateway.md §2.1.7`):

- `text`, `tool_use`, `tool_result`, `image (base64 | url)`, `thinking`,
  `redacted_thinking` round-trip 1:1 via the canonical types.
- `system` blocks with `cache_control` are split into the stable /
  volatile two-segment `CanonicalRequest.system_prompt` pair so the
  Anthropic adapter places the cache breakpoint where the client asked.
- Anthropic uses canonical tool ids verbatim as wire ids
  (`packages/metis-core/src/metis_core/adapters/anthropic.py`), so
  `toolu_*` ids the client sends survive a round-trip without needing
  a separate `ToolIdMap` projection.

Limitations (drop-with-log per `canonical-message-format.md §7.3`):

- `cache_control` markers attached to *message* content blocks (text /
  tool_use / tool_result) are dropped — the canonical IR carries
  `cache_control` only on the system prefix and on the last tool def
  (placed by the adapter). Per `gateway.md §4.4` this is acceptable: the
  marker is a hint, not a contract; dropping it inverts the cost lever
  for the request but does not corrupt the response.
- `document` blocks and the `citations` field on text blocks are dropped:
  the canonical IR has no DocumentBlock and no Citation type today
  (`canonical-message-format.md §4.2` lists the closed set).
- `ImageBlock(kind="file_ref")` is rejected with 400 — the gateway is
  workspace-agnostic by design (`gateway.md §2.2.3`); resolving a
  workspace-relative path here would mean leaking another tenant's
  filesystem through a shared HTTP surface.

Streaming follows Anthropic's named-event SSE format (`event: <name>\\n
data: {...}\\n\\n`) and is built from the canonical streaming events
produced by `adapter.stream()` — see `_AnthropicSSERenderer` below for
the canonical-event → wire-event mapping.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import msgspec
from metis_core.adapters.protocol import CanonicalResponse, StopReason
from metis_core.adapters.streaming import (
    MessageComplete,
    MessageStart,
    StreamingEvent,
    TextDelta,
    ThinkingDelta,
    ToolUseEnd,
    ToolUseInputDelta,
    ToolUseStart,
)
from metis_core.canonical.content import (
    ImageBlock,
    ImageSource,
    RedactedThinkingBlock,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from metis_core.canonical.ids import new_message_id
from metis_core.canonical.messages import Message, Role
from metis_core.canonical.tools import SideEffects, ToolDefinition

logger = logging.getLogger(__name__)


class InboundTranslationError(ValueError):
    """The inbound Anthropic-shape request body could not be translated.

    Maps to `400 invalid_request_error` on the wire (gateway.md §8).
    """


@dataclass(frozen=True)
class AnthropicInboundRequest:
    """Canonical projection of an Anthropic `messages.create` payload."""

    model: str
    messages: list[Message]
    system_prompt: str | None
    system_prompt_volatile: str | None
    tools: list[ToolDefinition]
    max_output_tokens: int
    temperature: float | None
    stop_sequences: list[str]
    stream: bool


# ---------------------------------------------------------------------------
# Inbound: Anthropic JSON -> canonical
# ---------------------------------------------------------------------------


def parse_anthropic_request(body: dict) -> AnthropicInboundRequest:
    """Translate an Anthropic Messages API request body into canonical pieces.

    Raises `InboundTranslationError` for client-side shape problems so the
    HTTP handler can return a 400.
    """
    if not isinstance(body, dict):
        raise InboundTranslationError("request body must be a JSON object")

    raw_model = body.get("model")
    if not isinstance(raw_model, str) or not raw_model:
        raise InboundTranslationError("'model' is required and must be a non-empty string")

    raw_messages = body.get("messages")
    if not isinstance(raw_messages, list) or not raw_messages:
        raise InboundTranslationError("'messages' is required and must be a non-empty list")

    raw_max = body.get("max_tokens")
    if not isinstance(raw_max, (int, float)) or raw_max <= 0:
        raise InboundTranslationError("'max_tokens' is required and must be a positive integer")
    max_output_tokens = int(raw_max)

    system_stable, system_volatile = _parse_system_field(body.get("system"))

    canonical_messages: list[Message] = []
    pending_tool_results: list[ToolResultBlock] = []

    def _flush_tool_results() -> None:
        if not pending_tool_results:
            return
        # Anthropic conventionally bundles tool_results inside a single
        # user-role message; canonical treats each ToolResultBlock as its own
        # Role.TOOL message so the routing/tracing layer doesn't need to know
        # the per-provider packing (canonical-message-format.md §3.2).
        for block in pending_tool_results:
            canonical_messages.append(
                Message(
                    id=new_message_id(),
                    session_id="",
                    role=Role.TOOL,
                    content=[block],
                    created_at=datetime.now(UTC),
                )
            )
        pending_tool_results.clear()

    for index, msg in enumerate(raw_messages):
        if not isinstance(msg, dict):
            raise InboundTranslationError(f"messages[{index}] must be an object")
        role = msg.get("role")
        content = msg.get("content")
        if role == "user":
            tool_results, user_blocks = _split_user_content(content, index)
            pending_tool_results.extend(tool_results)
            if user_blocks:
                _flush_tool_results()
                canonical_messages.append(
                    Message(
                        id=new_message_id(),
                        session_id="",
                        role=Role.USER,
                        content=user_blocks,
                        created_at=datetime.now(UTC),
                    )
                )
        elif role == "assistant":
            _flush_tool_results()
            assistant_blocks = _parse_assistant_content(content, index)
            canonical_messages.append(
                Message(
                    id=new_message_id(),
                    session_id="",
                    role=Role.ASSISTANT,
                    content=assistant_blocks,
                    created_at=datetime.now(UTC),
                )
            )
        else:
            raise InboundTranslationError(f"messages[{index}].role must be 'user' or 'assistant'")
    _flush_tool_results()

    tools = _parse_tools(body.get("tools"))
    stop_sequences = _parse_stop_sequences(body.get("stop_sequences"))
    raw_temp = body.get("temperature")
    temperature = float(raw_temp) if isinstance(raw_temp, (int, float)) else None
    stream = bool(body.get("stream", False))

    return AnthropicInboundRequest(
        model=raw_model,
        messages=canonical_messages,
        system_prompt=system_stable,
        system_prompt_volatile=system_volatile,
        tools=tools,
        max_output_tokens=max_output_tokens,
        temperature=temperature,
        stop_sequences=stop_sequences,
        stream=stream,
    )


def _parse_system_field(raw: Any) -> tuple[str | None, str | None]:
    """Split Anthropic `system` into stable / volatile segments.

    Rules:
    - `system: null` -> (None, None).
    - `system: "..."` -> (text, None) — single stable segment.
    - `system: [TextBlock, ...]` -> walk blocks in order; everything up to and
      including the LAST block carrying `cache_control` is the stable segment,
      everything after is volatile. If no `cache_control` is present, all
      blocks collapse into the stable segment (the adapter will still attach
      a cache breakpoint, per `context-assembler.md §3`).
    """
    if raw is None:
        return None, None
    if isinstance(raw, str):
        return (raw or None), None
    if not isinstance(raw, list):
        raise InboundTranslationError("'system' must be a string or list of blocks")
    blocks: list[tuple[str, bool]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise InboundTranslationError(f"system[{index}] must be an object")
        if item.get("type") != "text":
            raise InboundTranslationError(
                f"system[{index}].type must be 'text' (got {item.get('type')!r})"
            )
        text = item.get("text", "")
        if not isinstance(text, str):
            raise InboundTranslationError(f"system[{index}].text must be a string")
        has_cache = "cache_control" in item
        blocks.append((text, has_cache))
    last_cache_idx = -1
    for i, (_text, has_cache) in enumerate(blocks):
        if has_cache:
            last_cache_idx = i
    if last_cache_idx < 0:
        stable_text = "\n\n".join(t for t, _ in blocks if t) or None
        return stable_text, None
    stable_parts = [t for t, _ in blocks[: last_cache_idx + 1] if t]
    volatile_parts = [t for t, _ in blocks[last_cache_idx + 1 :] if t]
    stable_text = "\n\n".join(stable_parts) or None
    volatile_text = "\n\n".join(volatile_parts) or None
    return stable_text, volatile_text


def _split_user_content(content: Any, msg_index: int) -> tuple[list[ToolResultBlock], list]:
    """Parse an Anthropic user-role content list, separating tool_result
    blocks (which become canonical Role.TOOL messages) from the rest of the
    user content (text + images, which become a Role.USER message).
    """
    tool_results: list[ToolResultBlock] = []
    user_blocks: list = []
    if content is None:
        return tool_results, user_blocks
    if isinstance(content, str):
        if content:
            user_blocks.append(TextBlock(text=content))
        return tool_results, user_blocks
    if not isinstance(content, list):
        raise InboundTranslationError(
            f"messages[{msg_index}].content must be a string or list of blocks"
        )
    for part_index, part in enumerate(content):
        if not isinstance(part, dict):
            raise InboundTranslationError(
                f"messages[{msg_index}].content[{part_index}] must be an object"
            )
        ptype = part.get("type")
        if ptype == "text":
            text = part.get("text", "")
            if isinstance(text, str) and text:
                user_blocks.append(TextBlock(text=text))
        elif ptype == "image":
            user_blocks.append(_parse_image_block(part, msg_index, part_index))
        elif ptype == "tool_result":
            tool_results.append(_parse_tool_result(part, msg_index, part_index))
        elif ptype == "document":
            logger.warning(
                "dropping unsupported 'document' block at messages[%d].content[%d] "
                "(canonical IR has no DocumentBlock today)",
                msg_index,
                part_index,
            )
        else:
            raise InboundTranslationError(
                f"messages[{msg_index}].content[{part_index}].type {ptype!r} "
                "is not supported on a user message"
            )
    return tool_results, user_blocks


def _parse_assistant_content(content: Any, msg_index: int) -> list:
    """Parse an Anthropic assistant-role content list into canonical blocks."""
    blocks: list = []
    if content is None:
        return [TextBlock(text="")]
    if isinstance(content, str):
        if content:
            blocks.append(TextBlock(text=content))
        else:
            blocks.append(TextBlock(text=""))
        return blocks
    if not isinstance(content, list):
        raise InboundTranslationError(
            f"messages[{msg_index}].content must be a string or list of blocks"
        )
    for part_index, part in enumerate(content):
        if not isinstance(part, dict):
            raise InboundTranslationError(
                f"messages[{msg_index}].content[{part_index}] must be an object"
            )
        ptype = part.get("type")
        if ptype == "text":
            text = part.get("text", "")
            if isinstance(text, str) and text:
                blocks.append(TextBlock(text=text))
        elif ptype == "thinking":
            thinking_text = part.get("thinking", "")
            signature = part.get("signature")
            if not isinstance(thinking_text, str):
                raise InboundTranslationError(
                    f"messages[{msg_index}].content[{part_index}].thinking must be a string"
                )
            blocks.append(
                ThinkingBlock(
                    text=thinking_text,
                    signature=signature if isinstance(signature, str) else None,
                )
            )
        elif ptype == "redacted_thinking":
            data = part.get("data", "")
            if not isinstance(data, str):
                raise InboundTranslationError(
                    f"messages[{msg_index}].content[{part_index}].data must be a string"
                )
            blocks.append(RedactedThinkingBlock(data=data))
        elif ptype == "tool_use":
            tool_id = part.get("id")
            name = part.get("name")
            tool_input = part.get("input", {})
            if not isinstance(tool_id, str) or not tool_id:
                raise InboundTranslationError(
                    f"messages[{msg_index}].content[{part_index}].id is required"
                )
            if not isinstance(name, str) or not name:
                raise InboundTranslationError(
                    f"messages[{msg_index}].content[{part_index}].name is required"
                )
            if not isinstance(tool_input, dict):
                raise InboundTranslationError(
                    f"messages[{msg_index}].content[{part_index}].input must be an object"
                )
            blocks.append(ToolUseBlock(id=tool_id, name=name, input=tool_input))
        else:
            raise InboundTranslationError(
                f"messages[{msg_index}].content[{part_index}].type {ptype!r} "
                "is not supported on an assistant message"
            )
    if not blocks:
        blocks.append(TextBlock(text=""))
    return blocks


def _parse_image_block(part: dict, msg_index: int, part_index: int) -> ImageBlock:
    source = part.get("source")
    if not isinstance(source, dict):
        raise InboundTranslationError(
            f"messages[{msg_index}].content[{part_index}].source must be an object"
        )
    source_type = source.get("type")
    if source_type == "base64":
        media_type = source.get("media_type", "image/png")
        data = source.get("data")
        if not isinstance(data, str) or not data:
            raise InboundTranslationError(
                f"messages[{msg_index}].content[{part_index}].source.data is required for base64"
            )
        return ImageBlock(
            source=ImageSource(kind="base64", data=data),
            media_type=str(media_type),
        )
    if source_type == "url":
        url = source.get("url")
        if not isinstance(url, str) or not url:
            raise InboundTranslationError(
                f"messages[{msg_index}].content[{part_index}].source.url is required"
            )
        return ImageBlock(
            source=ImageSource(kind="url", data=url),
            media_type=str(source.get("media_type", "image/png")),
        )
    if source_type == "file_ref":
        # Per gateway.md §2.2.3 the gateway is workspace-agnostic; resolving
        # a workspace path here would either fail (no workspace context) or
        # cross-leak a tenant filesystem. Reject so the client gets a clear
        # 400 instead of a silent drop.
        raise InboundTranslationError(
            f"messages[{msg_index}].content[{part_index}].source.type 'file_ref' "
            "is not supported by the gateway — inline as base64 or use a URL"
        )
    raise InboundTranslationError(
        f"messages[{msg_index}].content[{part_index}].source.type {source_type!r} is not supported"
    )


def _parse_tool_result(part: dict, msg_index: int, part_index: int) -> ToolResultBlock:
    tool_use_id = part.get("tool_use_id")
    if not isinstance(tool_use_id, str) or not tool_use_id:
        raise InboundTranslationError(
            f"messages[{msg_index}].content[{part_index}].tool_use_id is required"
        )
    raw_content = part.get("content", "")
    inner: list = []
    if isinstance(raw_content, str):
        inner.append(TextBlock(text=raw_content))
    elif isinstance(raw_content, list):
        for sub_index, sub in enumerate(raw_content):
            if not isinstance(sub, dict):
                raise InboundTranslationError(
                    f"messages[{msg_index}].content[{part_index}].content[{sub_index}] "
                    "must be an object"
                )
            stype = sub.get("type")
            if stype == "text":
                text = sub.get("text", "")
                if isinstance(text, str):
                    inner.append(TextBlock(text=text))
            elif stype == "image":
                inner.append(_parse_image_block(sub, msg_index, part_index))
            else:
                raise InboundTranslationError(
                    f"messages[{msg_index}].content[{part_index}].content[{sub_index}] "
                    f"type {stype!r} is not supported inside a tool_result"
                )
    else:
        raise InboundTranslationError(
            f"messages[{msg_index}].content[{part_index}].content must be a string or list"
        )
    if not inner:
        inner.append(TextBlock(text=""))
    is_error = bool(part.get("is_error", False))
    return ToolResultBlock(tool_use_id=tool_use_id, content=inner, is_error=is_error)


def _parse_tools(raw: Any) -> list[ToolDefinition]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise InboundTranslationError("'tools' must be a list")
    out: list[ToolDefinition] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise InboundTranslationError(f"tools[{index}] must be an object")
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise InboundTranslationError(f"tools[{index}].name is required")
        description = entry.get("description", "")
        if not isinstance(description, str):
            raise InboundTranslationError(f"tools[{index}].description must be a string")
        input_schema = entry.get("input_schema", {"type": "object", "properties": {}})
        if not isinstance(input_schema, dict):
            raise InboundTranslationError(f"tools[{index}].input_schema must be an object")
        out.append(
            ToolDefinition(
                name=name,
                description=description,
                input_schema=input_schema,
                side_effects=SideEffects.NONE,
                requires_workspace=False,
            )
        )
    return out


def _parse_stop_sequences(raw: Any) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise InboundTranslationError("'stop_sequences' must be a list of strings")
    return [str(s) for s in raw if isinstance(s, str)]


# ---------------------------------------------------------------------------
# Outbound: canonical -> Anthropic JSON (sync response)
# ---------------------------------------------------------------------------


def render_anthropic_response(
    response: CanonicalResponse,
    *,
    requested_model: str,
) -> dict:
    """Translate a non-streaming `CanonicalResponse` into an Anthropic
    Messages-shape response body.

    `requested_model` is echoed back to the client unchanged regardless of
    how routing resolved the actual model (`gateway.md §5.3`); the routed
    model is recorded in trace events for the dashboard.
    """
    content_blocks: list[dict] = []
    for block in response.content:
        rendered = _render_block_for_response(block)
        if rendered is not None:
            content_blocks.append(rendered)

    return {
        "id": f"msg_{new_message_id()}",
        "type": "message",
        "role": "assistant",
        "model": requested_model,
        "content": content_blocks,
        "stop_reason": _stop_reason_to_wire(response.stop_reason),
        "stop_sequence": None,
        "usage": {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
            "cache_read_input_tokens": response.usage.cached_input_tokens,
            "cache_creation_input_tokens": response.usage.cache_creation_input_tokens,
        },
    }


def _render_block_for_response(block) -> dict | None:
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, ToolUseBlock):
        return {
            "type": "tool_use",
            "id": block.id,
            "name": block.name,
            "input": block.input,
        }
    if isinstance(block, ThinkingBlock):
        out: dict = {"type": "thinking", "thinking": block.text}
        if block.signature:
            out["signature"] = block.signature
        return out
    if isinstance(block, RedactedThinkingBlock):
        return {"type": "redacted_thinking", "data": block.data}
    if isinstance(block, ImageBlock):
        if block.source.kind == "base64":
            return {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": block.media_type,
                    "data": block.source.data,
                },
            }
        if block.source.kind == "url":
            return {
                "type": "image",
                "source": {"type": "url", "url": block.source.data},
            }
    logger.warning("dropping unsupported response block %s", type(block).__name__)
    return None


def _stop_reason_to_wire(reason: StopReason) -> str:
    if reason == StopReason.TOOL_USE:
        return "tool_use"
    if reason == StopReason.MAX_TOKENS:
        return "max_tokens"
    if reason == StopReason.STOP_SEQUENCE:
        return "stop_sequence"
    return "end_turn"


# ---------------------------------------------------------------------------
# Outbound streaming: canonical events -> Anthropic SSE wire format
# ---------------------------------------------------------------------------


class _AnthropicSSERenderer:
    """Translates canonical `StreamingEvent`s into Anthropic-shape SSE frames.

    Anthropic's wire format (https://docs.anthropic.com/en/api/messages-streaming):

        event: message_start
        data: {"type":"message_start","message":{...}}

        event: content_block_start
        data: {"type":"content_block_start","index":N,"content_block":{...}}

        event: content_block_delta
        data: {"type":"content_block_delta","index":N,"delta":{...}}

        event: content_block_stop
        data: {"type":"content_block_stop","index":N}

        event: message_delta
        data: {"type":"message_delta","delta":{"stop_reason":"..."},"usage":{...}}

        event: message_stop
        data: {"type":"message_stop"}

    The canonical event stream (`adapters/streaming.py`) only emits
    `content_block_start` synthetically for tool_use blocks; for text and
    thinking blocks the first delta is the implicit start. This renderer
    fills those starts in so the wire format is uniform.
    """

    def __init__(self, *, requested_model: str) -> None:
        self.requested_model = requested_model
        self._active_index: int | None = None

    def render(self, event: StreamingEvent) -> list[bytes]:
        if isinstance(event, MessageStart):
            return [
                _sse(
                    "message_start",
                    {
                        "type": "message_start",
                        "message": {
                            "id": f"msg_{event.message_id}",
                            "type": "message",
                            "role": "assistant",
                            "model": self.requested_model,
                            "content": [],
                            "stop_reason": None,
                            "stop_sequence": None,
                            "usage": {"input_tokens": 0, "output_tokens": 0},
                        },
                    },
                )
            ]
        if isinstance(event, TextDelta):
            frames = self._maybe_open_block(event.content_block_index, "text")
            frames.append(
                _sse(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": event.content_block_index,
                        "delta": {"type": "text_delta", "text": event.text},
                    },
                )
            )
            return frames
        if isinstance(event, ThinkingDelta):
            frames = self._maybe_open_block(event.content_block_index, "thinking")
            frames.append(
                _sse(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": event.content_block_index,
                        "delta": {"type": "thinking_delta", "thinking": event.text},
                    },
                )
            )
            if event.signature:
                frames.append(
                    _sse(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": event.content_block_index,
                            "delta": {"type": "signature_delta", "signature": event.signature},
                        },
                    )
                )
            return frames
        if isinstance(event, ToolUseStart):
            frames = self._maybe_close_block(event.content_block_index)
            self._active_index = event.content_block_index
            frames.append(
                _sse(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": event.content_block_index,
                        "content_block": {
                            "type": "tool_use",
                            "id": event.tool_use_id,
                            "name": event.tool_name,
                            "input": {},
                        },
                    },
                )
            )
            return frames
        if isinstance(event, ToolUseInputDelta):
            return [
                _sse(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": event.content_block_index,
                        "delta": {
                            "type": "input_json_delta",
                            "partial_json": event.partial_json,
                        },
                    },
                )
            ]
        if isinstance(event, ToolUseEnd):
            frames = []
            if self._active_index == event.content_block_index:
                frames.append(
                    _sse(
                        "content_block_stop",
                        {
                            "type": "content_block_stop",
                            "index": event.content_block_index,
                        },
                    )
                )
                self._active_index = None
            return frames
        if isinstance(event, MessageComplete):
            frames = self._maybe_close_block(None)
            frames.append(
                _sse(
                    "message_delta",
                    {
                        "type": "message_delta",
                        "delta": {
                            "stop_reason": _stop_reason_to_wire(event.stop_reason),
                            "stop_sequence": None,
                        },
                        "usage": {
                            "input_tokens": event.usage.input_tokens,
                            "output_tokens": event.usage.output_tokens,
                            "cache_read_input_tokens": event.usage.cached_input_tokens,
                            "cache_creation_input_tokens": (
                                event.usage.cache_creation_input_tokens
                            ),
                        },
                    },
                )
            )
            frames.append(_sse("message_stop", {"type": "message_stop"}))
            return frames
        return []

    def _maybe_open_block(self, index: int, kind: str) -> list[bytes]:
        """Emit `content_block_stop` for any open block of a different index,
        then `content_block_start` for this new index if it hasn't been opened
        already (canonical TextDelta / ThinkingDelta carry no explicit start).
        """
        frames: list[bytes] = []
        if self._active_index is not None and self._active_index != index:
            frames.append(
                _sse(
                    "content_block_stop",
                    {"type": "content_block_stop", "index": self._active_index},
                )
            )
            self._active_index = None
        if self._active_index != index:
            content_block: dict
            if kind == "thinking":
                content_block = {"type": "thinking", "thinking": ""}
            else:
                content_block = {"type": "text", "text": ""}
            frames.append(
                _sse(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": index,
                        "content_block": content_block,
                    },
                )
            )
            self._active_index = index
        return frames

    def _maybe_close_block(self, _index: int | None) -> list[bytes]:
        """Emit a `content_block_stop` if any block is currently open."""
        if self._active_index is None:
            return []
        frame = _sse(
            "content_block_stop",
            {"type": "content_block_stop", "index": self._active_index},
        )
        self._active_index = None
        return [frame]


def _sse(event_name: str, payload: dict) -> bytes:
    """Encode a single SSE frame in Anthropic's named-event format."""
    return f"event: {event_name}\n".encode() + b"data: " + msgspec.json.encode(payload) + b"\n\n"


async def render_sse_stream(
    canonical_events: AsyncIterator[StreamingEvent],
    *,
    requested_model: str,
) -> AsyncIterator[bytes]:
    """Async-iterate canonical streaming events and yield encoded SSE bytes.

    This is the entry point the HTTP handler hands to Starlette's
    `StreamingResponse`. It owns no resources of its own; cleanup runs in the
    underlying `canonical_events` generator's `finally`.
    """
    renderer = _AnthropicSSERenderer(requested_model=requested_model)
    async for event in canonical_events:
        for frame in renderer.render(event):
            yield frame


# ---------------------------------------------------------------------------
# Error envelope
# ---------------------------------------------------------------------------


def anthropic_error_envelope(*, message: str, error_type: str = "invalid_request_error") -> dict:
    """Anthropic's standard error body shape (per `gateway.md §8`)."""
    return {"type": "error", "error": {"type": error_type, "message": message}}


__all__ = [
    "AnthropicInboundRequest",
    "InboundTranslationError",
    "anthropic_error_envelope",
    "parse_anthropic_request",
    "render_anthropic_response",
    "render_sse_stream",
]
