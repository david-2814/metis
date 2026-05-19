"""Inbound/outbound translation between OpenAI Chat Completions shape and
the canonical IR.

Mirrors the egress translator in `metis.core.adapters.openai` (which goes
canonical → OpenAI wire) but in reverse for the inbound side, and again
canonical → OpenAI for the outbound JSON body.

This module is sync; the gateway never blocks while translating. The
ToolIdMap that round-trips ids across inbound/outbound is owned by the
caller (per-request) per `gateway.md §4.3`.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from metis.core.adapters.protocol import CanonicalResponse, StopReason
from metis.core.adapters.streaming import (
    MessageComplete,
    MessageStart,
    StreamingEvent,
    TextDelta,
    ToolUseInputDelta,
    ToolUseStart,
)
from metis.core.adapters.tool_id_map import ToolIdMap
from metis.core.canonical.content import (
    ImageBlock,
    ImageSource,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from metis.core.canonical.ids import new_message_id, new_tool_use_id
from metis.core.canonical.messages import Message, Role
from metis.core.canonical.tools import SideEffects, ToolDefinition


class InboundTranslationError(ValueError):
    """The inbound OpenAI-shape request body could not be translated."""


@dataclass(frozen=True)
class OpenAIInboundRequest:
    """Canonical projection of an `openai.ChatCompletionCreateParams` payload."""

    model: str
    messages: list[Message]
    system_prompt: str | None
    tools: list[ToolDefinition]
    max_output_tokens: int
    temperature: float | None
    stop_sequences: list[str]
    output_schema: dict | None
    stream: bool
    include_usage: bool


def parse_openai_request(body: dict, *, tool_map: ToolIdMap) -> OpenAIInboundRequest:
    """Translate an OpenAI Chat Completions request body into canonical pieces.

    Raises `InboundTranslationError` for client-side shape problems so the
    handler can return a 400.
    """
    if not isinstance(body, dict):
        raise InboundTranslationError("request body must be a JSON object")

    raw_model = body.get("model")
    if not isinstance(raw_model, str) or not raw_model:
        raise InboundTranslationError("'model' is required and must be a non-empty string")

    raw_messages = body.get("messages")
    if not isinstance(raw_messages, list) or not raw_messages:
        raise InboundTranslationError("'messages' is required and must be a non-empty list")

    system_parts: list[str] = []
    canonical_messages: list[Message] = []
    pending_tool_results: list[ToolResultBlock] = []

    def _flush_tool_results() -> None:
        if not pending_tool_results:
            return
        for block in pending_tool_results:
            canonical_messages.append(_tool_message_for_result(block))
        pending_tool_results.clear()

    for index, msg in enumerate(raw_messages):
        if not isinstance(msg, dict):
            raise InboundTranslationError(f"messages[{index}] must be an object")
        role = msg.get("role")
        if role == "system":
            _flush_tool_results()
            system_parts.append(_extract_system_text(msg, index))
        elif role == "user":
            _flush_tool_results()
            canonical_messages.append(_user_message(msg, index))
        elif role == "assistant":
            _flush_tool_results()
            canonical_messages.append(_assistant_message(msg, index, tool_map))
        elif role == "tool":
            pending_tool_results.append(_tool_result_block(msg, index, tool_map))
        else:
            raise InboundTranslationError(
                f"messages[{index}].role must be one of system/user/assistant/tool"
            )
    _flush_tool_results()

    tools = _parse_tools(body.get("tools"))

    raw_max = body.get("max_completion_tokens", body.get("max_tokens"))
    max_output_tokens = int(raw_max) if isinstance(raw_max, (int, float)) and raw_max else 1024

    raw_temp = body.get("temperature")
    temperature = float(raw_temp) if isinstance(raw_temp, (int, float)) else None

    raw_stop = body.get("stop")
    if raw_stop is None:
        stop_sequences: list[str] = []
    elif isinstance(raw_stop, str):
        stop_sequences = [raw_stop]
    elif isinstance(raw_stop, list):
        stop_sequences = [str(s) for s in raw_stop if isinstance(s, str)]
    else:
        raise InboundTranslationError("'stop' must be a string or list of strings")

    output_schema = _parse_response_format(body.get("response_format"))
    stream = bool(body.get("stream", False))
    include_usage = _parse_stream_options(body.get("stream_options"))

    system_prompt = "\n\n".join(p for p in system_parts if p) or None

    return OpenAIInboundRequest(
        model=raw_model,
        messages=canonical_messages,
        system_prompt=system_prompt,
        tools=tools,
        max_output_tokens=max_output_tokens,
        temperature=temperature,
        stop_sequences=stop_sequences,
        output_schema=output_schema,
        stream=stream,
        include_usage=include_usage,
    )


def render_openai_response(
    response: CanonicalResponse,
    *,
    requested_model: str,
    tool_map: ToolIdMap,
) -> dict:
    """Translate a CanonicalResponse into an OpenAI Chat Completions body.

    `requested_model` is what the client sent; OpenAI clients expect the same
    string back regardless of how routing translated it. The actual routed
    model is recorded in trace events for the dashboard.
    """
    message: dict[str, Any] = {"role": "assistant"}
    text_pieces: list[str] = []
    tool_calls: list[dict] = []
    for block in response.content:
        if isinstance(block, TextBlock):
            text_pieces.append(block.text)
        elif isinstance(block, ToolUseBlock):
            provider_id = tool_map.to_provider(block.id)
            if provider_id is None:
                provider_id = f"call_{block.id.removeprefix('tu_')}"
                tool_map.remember(block.id, provider_id)
            tool_calls.append(
                {
                    "id": provider_id,
                    "type": "function",
                    "function": {
                        "name": block.name,
                        "arguments": json.dumps(block.input),
                    },
                }
            )
        elif isinstance(block, ThinkingBlock):
            continue
    if text_pieces:
        message["content"] = "\n".join(text_pieces)
    else:
        message["content"] = None
    if tool_calls:
        message["tool_calls"] = tool_calls

    return {
        "id": f"chatcmpl-{new_message_id()}",
        "object": "chat.completion",
        "created": 0,
        "model": requested_model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": _stop_reason_to_finish(response.stop_reason),
            }
        ],
        "usage": {
            "prompt_tokens": response.usage.input_tokens,
            "completion_tokens": response.usage.output_tokens,
            "total_tokens": response.usage.input_tokens + response.usage.output_tokens,
            "prompt_tokens_details": {
                "cached_tokens": response.usage.cached_input_tokens,
            },
        },
    }


# ---------------------------------------------------------------------------
# Inbound helpers
# ---------------------------------------------------------------------------


def _extract_system_text(msg: dict, index: int) -> str:
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        pieces: list[str] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                text = part.get("text", "")
                if isinstance(text, str):
                    pieces.append(text)
        return "\n".join(pieces)
    raise InboundTranslationError(f"messages[{index}].content must be string or list")


def _user_message(msg: dict, index: int) -> Message:
    content = msg.get("content")
    blocks: list = []
    if isinstance(content, str):
        if content:
            blocks.append(TextBlock(text=content))
    elif isinstance(content, list):
        for part_index, part in enumerate(content):
            if not isinstance(part, dict):
                raise InboundTranslationError(
                    f"messages[{index}].content[{part_index}] must be an object"
                )
            ptype = part.get("type")
            if ptype == "text":
                text = part.get("text", "")
                if isinstance(text, str) and text:
                    blocks.append(TextBlock(text=text))
            elif ptype == "image_url":
                blocks.append(_image_url_to_block(part, index, part_index))
            else:
                raise InboundTranslationError(
                    f"messages[{index}].content[{part_index}].type {ptype!r} is not supported"
                )
    else:
        raise InboundTranslationError(f"messages[{index}].content must be string or list")
    if not blocks:
        blocks.append(TextBlock(text=""))
    return Message(
        id=new_message_id(),
        session_id="",
        role=Role.USER,
        content=blocks,
        created_at=datetime.now(UTC),
    )


def _image_url_to_block(part: dict, msg_index: int, part_index: int) -> ImageBlock:
    image_url = part.get("image_url")
    if not isinstance(image_url, dict):
        raise InboundTranslationError(
            f"messages[{msg_index}].content[{part_index}].image_url must be an object"
        )
    url = image_url.get("url")
    if not isinstance(url, str) or not url:
        raise InboundTranslationError(
            f"messages[{msg_index}].content[{part_index}].image_url.url is required"
        )
    if url.startswith("data:"):
        prefix, _, b64 = url.partition(";base64,")
        if not b64:
            raise InboundTranslationError(
                f"messages[{msg_index}].content[{part_index}] data URL must be base64"
            )
        media_type = prefix.removeprefix("data:") or "image/png"
        return ImageBlock(
            source=ImageSource(kind="base64", data=b64),
            media_type=media_type,
        )
    return ImageBlock(
        source=ImageSource(kind="url", data=url),
        media_type="image/png",
    )


def _assistant_message(msg: dict, index: int, tool_map: ToolIdMap) -> Message:
    blocks: list = []
    content = msg.get("content")
    if isinstance(content, str) and content:
        blocks.append(TextBlock(text=content))
    elif isinstance(content, list):
        for part_index, part in enumerate(content):
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                text = part.get("text", "")
                if isinstance(text, str) and text:
                    blocks.append(TextBlock(text=text))
            else:
                raise InboundTranslationError(
                    f"messages[{index}].content[{part_index}] has unsupported type"
                )

    tool_calls = msg.get("tool_calls")
    if isinstance(tool_calls, list):
        for call_index, call in enumerate(tool_calls):
            if not isinstance(call, dict):
                raise InboundTranslationError(
                    f"messages[{index}].tool_calls[{call_index}] must be an object"
                )
            call_id = call.get("id")
            if not isinstance(call_id, str) or not call_id:
                raise InboundTranslationError(
                    f"messages[{index}].tool_calls[{call_index}].id is required"
                )
            fn = call.get("function")
            if not isinstance(fn, dict):
                raise InboundTranslationError(
                    f"messages[{index}].tool_calls[{call_index}].function is required"
                )
            name = fn.get("name")
            if not isinstance(name, str) or not name:
                raise InboundTranslationError(
                    f"messages[{index}].tool_calls[{call_index}].function.name is required"
                )
            raw_args = fn.get("arguments", "")
            parsed: dict
            if isinstance(raw_args, str):
                if raw_args.strip():
                    try:
                        parsed = json.loads(raw_args)
                    except json.JSONDecodeError as exc:
                        raise InboundTranslationError(
                            f"messages[{index}].tool_calls[{call_index}]."
                            f"function.arguments is not valid JSON: {exc}"
                        ) from exc
                else:
                    parsed = {}
            elif isinstance(raw_args, dict):
                parsed = raw_args
            else:
                raise InboundTranslationError(
                    f"messages[{index}].tool_calls[{call_index}]."
                    "function.arguments must be string or object"
                )
            canonical_id = tool_map.to_canonical(call_id)
            if canonical_id is None:
                canonical_id = new_tool_use_id()
                tool_map.remember(canonical_id, call_id)
            blocks.append(ToolUseBlock(id=canonical_id, name=name, input=parsed))

    if not blocks:
        blocks.append(TextBlock(text=""))
    return Message(
        id=new_message_id(),
        session_id="",
        role=Role.ASSISTANT,
        content=blocks,
        created_at=datetime.now(UTC),
    )


def _tool_result_block(msg: dict, index: int, tool_map: ToolIdMap) -> ToolResultBlock:
    tool_call_id = msg.get("tool_call_id")
    if not isinstance(tool_call_id, str) or not tool_call_id:
        raise InboundTranslationError(f"messages[{index}].tool_call_id is required for role=tool")
    canonical_id = tool_map.to_canonical(tool_call_id)
    if canonical_id is None:
        canonical_id = new_tool_use_id()
        tool_map.remember(canonical_id, tool_call_id)
    content = msg.get("content", "")
    inner: list = []
    if isinstance(content, str):
        inner.append(TextBlock(text=content))
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                text = part.get("text", "")
                if isinstance(text, str):
                    inner.append(TextBlock(text=text))
    else:
        raise InboundTranslationError(
            f"messages[{index}].content must be string or list for role=tool"
        )
    if not inner:
        inner.append(TextBlock(text=""))
    return ToolResultBlock(tool_use_id=canonical_id, content=inner)


def _tool_message_for_result(block: ToolResultBlock) -> Message:
    return Message(
        id=new_message_id(),
        session_id="",
        role=Role.TOOL,
        content=[block],
        created_at=datetime.now(UTC),
    )


def _parse_tools(raw: Iterable | None) -> list[ToolDefinition]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise InboundTranslationError("'tools' must be a list")
    out: list[ToolDefinition] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise InboundTranslationError(f"tools[{index}] must be an object")
        if entry.get("type") != "function":
            raise InboundTranslationError(f"tools[{index}].type must be 'function'")
        fn = entry.get("function")
        if not isinstance(fn, dict):
            raise InboundTranslationError(f"tools[{index}].function is required")
        name = fn.get("name")
        if not isinstance(name, str) or not name:
            raise InboundTranslationError(f"tools[{index}].function.name is required")
        description = fn.get("description", "")
        if not isinstance(description, str):
            raise InboundTranslationError(f"tools[{index}].function.description must be a string")
        parameters = fn.get("parameters", {"type": "object", "properties": {}})
        if not isinstance(parameters, dict):
            raise InboundTranslationError(f"tools[{index}].function.parameters must be an object")
        out.append(
            ToolDefinition(
                name=name,
                description=description,
                input_schema=parameters,
                side_effects=SideEffects.NONE,
                requires_workspace=False,
            )
        )
    return out


def _parse_stream_options(raw: Any) -> bool:
    if raw is None:
        return False
    if not isinstance(raw, dict):
        raise InboundTranslationError("'stream_options' must be an object")
    return bool(raw.get("include_usage", False))


def _parse_response_format(raw: Any) -> dict | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise InboundTranslationError("'response_format' must be an object")
    if raw.get("type") == "json_schema":
        schema_wrapper = raw.get("json_schema")
        if not isinstance(schema_wrapper, dict):
            raise InboundTranslationError("'response_format.json_schema' must be an object")
        schema = schema_wrapper.get("schema")
        if not isinstance(schema, dict):
            raise InboundTranslationError("'response_format.json_schema.schema' must be an object")
        return schema
    return None


def _stop_reason_to_finish(reason: StopReason) -> str:
    if reason == StopReason.TOOL_USE:
        return "tool_calls"
    if reason == StopReason.MAX_TOKENS:
        return "length"
    if reason == StopReason.STOP_SEQUENCE:
        return "stop"
    return "stop"


# ---------------------------------------------------------------------------
# SSE streaming output (OpenAI Chat Completions chunked shape)
# ---------------------------------------------------------------------------


async def render_openai_sse_stream(
    events: AsyncIterator[StreamingEvent],
    *,
    requested_model: str,
    tool_map: ToolIdMap,
    include_usage: bool,
) -> AsyncIterator[bytes]:
    """Translate canonical StreamingEvents into OpenAI SSE byte chunks.

    Per `streaming-protocol.md §5.3` (canonical events) and OpenAI's Chat
    Completions streaming wire format. Emits one `chat.completion.chunk` per
    delta, a final chunk with `finish_reason`, an optional usage-only chunk
    when `stream_options.include_usage` was requested, and a terminating
    `data: [DONE]\\n\\n` frame.

    Tool calls: each new `ToolUseStart` is assigned the next OpenAI
    `tool_calls[].index` (0, 1, 2, ...). The canonical `tool_use_id` is
    translated to a provider `call_*` id via `tool_map`, minting and
    remembering a synthetic id on first sight.
    """
    chunk_id = f"chatcmpl-{new_message_id()}"
    # Maps content_block_index → tool_calls[].index for OpenAI's wire shape.
    tool_index_by_block: dict[int, int] = {}
    next_tool_index = 0
    sent_role_delta = False
    final_stop_reason = StopReason.END_TURN
    final_usage = None

    async for event in events:
        if isinstance(event, MessageStart):
            sent_role_delta = True
            yield _sse_data(
                _chunk_frame(
                    chunk_id=chunk_id,
                    model=requested_model,
                    delta={"role": "assistant"},
                )
            )
        elif isinstance(event, TextDelta):
            if not sent_role_delta:
                sent_role_delta = True
                yield _sse_data(
                    _chunk_frame(
                        chunk_id=chunk_id,
                        model=requested_model,
                        delta={"role": "assistant"},
                    )
                )
            yield _sse_data(
                _chunk_frame(
                    chunk_id=chunk_id,
                    model=requested_model,
                    delta={"content": event.text},
                )
            )
        elif isinstance(event, ToolUseStart):
            tool_index = next_tool_index
            next_tool_index += 1
            tool_index_by_block[event.content_block_index] = tool_index
            provider_id = tool_map.to_provider(event.tool_use_id)
            if provider_id is None:
                provider_id = f"call_{event.tool_use_id.removeprefix('tu_')}"
                tool_map.remember(event.tool_use_id, provider_id)
            yield _sse_data(
                _chunk_frame(
                    chunk_id=chunk_id,
                    model=requested_model,
                    delta={
                        "tool_calls": [
                            {
                                "index": tool_index,
                                "id": provider_id,
                                "type": "function",
                                "function": {
                                    "name": event.tool_name,
                                    "arguments": "",
                                },
                            }
                        ]
                    },
                )
            )
        elif isinstance(event, ToolUseInputDelta):
            tool_index = tool_index_by_block.get(event.content_block_index)
            if tool_index is None:
                # Defensive: should have seen a ToolUseStart first.
                tool_index = next_tool_index
                next_tool_index += 1
                tool_index_by_block[event.content_block_index] = tool_index
            yield _sse_data(
                _chunk_frame(
                    chunk_id=chunk_id,
                    model=requested_model,
                    delta={
                        "tool_calls": [
                            {
                                "index": tool_index,
                                "function": {"arguments": event.partial_json},
                            }
                        ]
                    },
                )
            )
        elif isinstance(event, MessageComplete):
            final_stop_reason = event.stop_reason
            final_usage = event.usage
        # ThinkingDelta and ToolUseEnd intentionally not surfaced in OpenAI
        # SSE — OpenAI clients don't model them.

    yield _sse_data(
        _chunk_frame(
            chunk_id=chunk_id,
            model=requested_model,
            delta={},
            finish_reason=_stop_reason_to_finish(final_stop_reason),
        )
    )

    if include_usage and final_usage is not None:
        usage_chunk = {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": 0,
            "model": requested_model,
            "choices": [],
            "usage": {
                "prompt_tokens": final_usage.input_tokens,
                "completion_tokens": final_usage.output_tokens,
                "total_tokens": final_usage.input_tokens + final_usage.output_tokens,
                "prompt_tokens_details": {
                    "cached_tokens": final_usage.cached_input_tokens,
                },
            },
        }
        yield _sse_data(usage_chunk)

    yield b"data: [DONE]\n\n"


def _chunk_frame(
    *,
    chunk_id: str,
    model: str,
    delta: dict,
    finish_reason: str | None = None,
) -> dict:
    return {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": 0,
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }
        ],
    }


def _sse_data(payload: dict) -> bytes:
    return b"data: " + json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n\n"
