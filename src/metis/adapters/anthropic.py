"""Anthropic provider adapter.

Wire-format translation per provider-adapter-contract.md §4.2. The adapter:

- Hoists canonical SYSTEM messages into the `system` request parameter.
- Translates ASSISTANT/USER content blocks directly to Anthropic blocks.
- Merges consecutive canonical TOOL messages into a single user message with
  multiple `tool_result` blocks (Anthropic's wire format).
- Uses canonical `tu_<ulid>` ids as wire ids (Anthropic accepts any string),
  recording the identity mapping in the per-session ToolIdMap.
- Reports raw token counts; cost is computed by the core.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator

import anthropic
import httpx
from anthropic.types import (
    MessageParam,
    TextBlockParam,
    ToolParam,
    ToolResultBlockParam,
    ToolUseBlockParam,
)

from metis.adapters.errors import (
    AdapterError,
    CancelledError,
    NetworkError,
    classify_anthropic_response,
    error_for_class,
)
from metis.adapters.protocol import (
    CanonicalRequest,
    CanonicalResponse,
    StopReason,
    TokenUsage,
)
from metis.adapters.retry import RetryPolicy, with_retry
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
from metis.adapters.tool_id_map import ToolIdMap
from metis.canonical.capabilities import AdapterCapabilities
from metis.canonical.content import (
    ContentBlock,
    ImageBlock,
    ImageSource,
    RedactedThinkingBlock,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from metis.canonical.ids import new_message_id
from metis.canonical.messages import Message, Role
from metis.canonical.tools import ToolDefinition

logger = logging.getLogger(__name__)


# Per-model capability declarations.
_CAPS_CLAUDE_4 = AdapterCapabilities(
    supports_thinking=True,
    supports_images=True,
    supports_tools=True,
    supports_system_prompt=True,
    supports_structured_output=False,
    supports_streaming=True,
    supports_streaming_tool_calls=True,
    supports_parallel_tool_calls=True,
    supports_prompt_caching=True,
    supports_system_messages_in_list=False,  # hoisted to top-level
    max_context_tokens=200_000,
    max_output_tokens=8192,
    accepted_image_media_types=["image/png", "image/jpeg", "image/gif", "image/webp"],
)

_MODEL_CAPS: dict[str, AdapterCapabilities] = {
    "anthropic:claude-opus-4-7": _CAPS_CLAUDE_4,
    "anthropic:claude-sonnet-4-6": _CAPS_CLAUDE_4,
    "anthropic:claude-haiku-4-5": _CAPS_CLAUDE_4,
}


class AnthropicAdapter:
    """Anthropic Messages API adapter."""

    name = "anthropic"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout_seconds: float = 600.0,
        retry_policy: RetryPolicy | None = None,
        client: anthropic.AsyncAnthropic | None = None,
    ) -> None:
        # Disable the SDK's own retries so retry logic lives in one place.
        self._client = client or anthropic.AsyncAnthropic(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout_seconds,
            max_retries=0,
        )
        self._retry_policy = retry_policy or RetryPolicy()
        self._in_flight: dict[str, asyncio.Task] = {}

    # ---- Public API ---------------------------------------------------

    def capabilities_for(self, model: str) -> AdapterCapabilities:
        try:
            return _MODEL_CAPS[model]
        except KeyError:
            raise ValueError(f"unknown anthropic model: {model!r}") from None

    def estimate_input_tokens(
        self,
        messages: list[Message],
        tools: list[ToolDefinition],
        system_prompt: str | None,
    ) -> int:
        """±10% heuristic: ~4 chars per token (provider-adapter §3.1).

        Production setups can swap in a tokenizer-based count. For routing
        decisions at Phase 1 scale, the heuristic is sufficient.
        """
        text_chars = 0
        if system_prompt:
            text_chars += len(system_prompt)
        for m in messages:
            for block in m.content:
                text_chars += _content_block_text_chars(block)
        for tool in tools:
            text_chars += len(tool.description) + len(str(tool.input_schema))
        return max(1, text_chars // 4)

    async def complete(self, request: CanonicalRequest) -> CanonicalResponse:
        """Run a non-streaming Messages API call with bounded retry."""
        task = asyncio.current_task()
        if task is not None:
            self._in_flight[request.request_id] = task
        try:
            return await with_retry(lambda: self._call_once(request), policy=self._retry_policy)
        except asyncio.CancelledError as exc:
            raise CancelledError(
                "request cancelled",
                request_id=request.request_id,
            ) from exc
        finally:
            self._in_flight.pop(request.request_id, None)

    async def cancel(self, request_id: str) -> bool:
        task = self._in_flight.get(request_id)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    async def close(self) -> None:
        await self._client.close()

    # ---- Streaming ----------------------------------------------------

    async def stream(self, request: CanonicalRequest) -> AsyncIterator[StreamingEvent]:
        """Stream a response as canonical streaming events.

        Maps the Anthropic SSE event types (content_block_start, content_block_delta,
        content_block_stop, message_delta, message_stop) to the canonical streaming
        events defined in streaming-protocol.md §5.3.
        """
        task = asyncio.current_task()
        if task is not None:
            self._in_flight[request.request_id] = task
        try:
            async for event in self._stream_once(request):
                yield event
        except asyncio.CancelledError as exc:
            raise CancelledError("request cancelled", request_id=request.request_id) from exc
        finally:
            self._in_flight.pop(request.request_id, None)

    async def _stream_once(self, request: CanonicalRequest) -> AsyncIterator[StreamingEvent]:
        tool_map = request.tool_id_map if request.tool_id_map is not None else ToolIdMap()
        anthropic_messages, system_text = _canonical_messages_to_anthropic(
            request.messages, request.system_prompt, tool_map
        )
        wire_tools = [_tool_to_anthropic(t) for t in request.tools]
        wire_model = _wire_model_name(request.model)

        kwargs: dict = {
            "model": wire_model,
            "max_tokens": request.max_output_tokens,
            "messages": anthropic_messages,
            "stream": True,
        }
        if system_text:
            kwargs["system"] = system_text
        if wire_tools:
            kwargs["tools"] = wire_tools
        if request.stop_sequences:
            kwargs["stop_sequences"] = request.stop_sequences
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature

        message_id = new_message_id()
        accumulator = _AnthropicStreamAccumulator(message_id=message_id, tool_map=tool_map)
        start = time.monotonic()

        yield MessageStart(message_id=message_id, model=request.model)

        try:
            response = await self._client.messages.create(**kwargs)
        except anthropic.APIStatusError as exc:
            raise _translate_status_error(exc, request.request_id) from exc
        except anthropic.APIConnectionError as exc:
            raise NetworkError(
                f"anthropic connection error: {exc}", request_id=request.request_id
            ) from exc
        except anthropic.APITimeoutError as exc:
            raise NetworkError(f"anthropic timeout: {exc}", request_id=request.request_id) from exc
        except httpx.HTTPError as exc:
            raise NetworkError(f"http error: {exc}", request_id=request.request_id) from exc

        async for raw in response:
            for canonical_event in accumulator.consume(raw):
                yield canonical_event

        latency_ms = int((time.monotonic() - start) * 1000)
        yield MessageComplete(
            message_id=message_id,
            stop_reason=_stop_reason(accumulator.stop_reason),
            final_content=accumulator.final_content(),
            usage=accumulator.usage(),
            latency_ms=latency_ms,
        )

    # ---- Single call --------------------------------------------------

    async def _call_once(self, request: CanonicalRequest) -> CanonicalResponse:
        # NOTE: `or ToolIdMap()` would break — ToolIdMap.__len__ makes an
        # empty map falsy, so we'd silently allocate a new map and drop the
        # caller's mutations on the floor.
        tool_map = request.tool_id_map if request.tool_id_map is not None else ToolIdMap()
        anthropic_messages, system_text = _canonical_messages_to_anthropic(
            request.messages, request.system_prompt, tool_map
        )
        wire_tools = [_tool_to_anthropic(t) for t in request.tools]
        wire_model = _wire_model_name(request.model)

        kwargs: dict = {
            "model": wire_model,
            "max_tokens": request.max_output_tokens,
            "messages": anthropic_messages,
        }
        if system_text:
            kwargs["system"] = system_text
        if wire_tools:
            kwargs["tools"] = wire_tools
        if request.stop_sequences:
            kwargs["stop_sequences"] = request.stop_sequences
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature

        start = time.monotonic()
        try:
            response = await self._client.messages.create(**kwargs)
        except anthropic.APIStatusError as exc:
            raise _translate_status_error(exc, request.request_id) from exc
        except anthropic.APIConnectionError as exc:
            raise NetworkError(
                f"anthropic connection error: {exc}",
                request_id=request.request_id,
            ) from exc
        except anthropic.APITimeoutError as exc:
            raise NetworkError(
                f"anthropic timeout: {exc}",
                request_id=request.request_id,
            ) from exc
        except httpx.HTTPError as exc:
            raise NetworkError(
                f"http error: {exc}",
                request_id=request.request_id,
            ) from exc

        latency_ms = int((time.monotonic() - start) * 1000)
        content = _anthropic_blocks_to_canonical(response.content, tool_map)
        usage = TokenUsage(
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cached_input_tokens=getattr(response.usage, "cache_read_input_tokens", 0) or 0,
            cache_creation_input_tokens=getattr(response.usage, "cache_creation_input_tokens", 0)
            or 0,
        )
        return CanonicalResponse(
            request_id=request.request_id,
            model=request.model,
            provider=self.name,
            content=content,
            stop_reason=_stop_reason(response.stop_reason),
            usage=usage,
            latency_ms=latency_ms,
        )


# ---------------------------------------------------------------------------
# Wire translation helpers
# ---------------------------------------------------------------------------


def _wire_model_name(canonical: str) -> str:
    if ":" not in canonical:
        return canonical
    return canonical.split(":", 1)[1]


def _stop_reason(raw: str | None) -> StopReason:
    if raw == "end_turn":
        return StopReason.END_TURN
    if raw == "max_tokens":
        return StopReason.MAX_TOKENS
    if raw == "stop_sequence":
        return StopReason.STOP_SEQUENCE
    if raw == "tool_use":
        return StopReason.TOOL_USE
    return StopReason.END_TURN  # default; new stop reasons fall back


def _tool_to_anthropic(tool: ToolDefinition) -> ToolParam:
    return {
        "name": tool.name,
        "description": tool.description,
        "input_schema": tool.input_schema,
    }


def _content_block_text_chars(block: ContentBlock) -> int:
    if isinstance(block, TextBlock):
        return len(block.text)
    if isinstance(block, ToolUseBlock):
        return len(block.name) + len(str(block.input))
    if isinstance(block, ToolResultBlock):
        return sum(_content_block_text_chars(b) for b in block.content) + 16
    if isinstance(block, ThinkingBlock):
        return len(block.text)
    if isinstance(block, ImageBlock):
        # Rough placeholder; Anthropic charges per image differently.
        return 1024
    return 0


def _canonical_messages_to_anthropic(
    messages: list[Message],
    system_prompt: str | None,
    tool_map: ToolIdMap,
) -> tuple[list[MessageParam], str | None]:
    """Translate the canonical message list to Anthropic wire format.

    - SYSTEM messages are concatenated and hoisted out of the list.
    - Consecutive TOOL messages are merged into a single user message
      carrying multiple tool_result blocks.
    """
    system_parts: list[str] = []
    if system_prompt:
        system_parts.append(system_prompt)

    out: list[MessageParam] = []
    pending_tool_results: list[ToolResultBlockParam] = []

    def flush_tool_results() -> None:
        if pending_tool_results:
            out.append({"role": "user", "content": list(pending_tool_results)})
            pending_tool_results.clear()

    for msg in messages:
        if msg.role == Role.SYSTEM:
            flush_tool_results()
            for block in msg.content:
                if isinstance(block, TextBlock):
                    system_parts.append(block.text)
            continue

        if msg.role == Role.TOOL:
            for block in msg.content:
                if isinstance(block, ToolResultBlock):
                    pending_tool_results.append(_tool_result_to_anthropic(block, tool_map))
            continue

        # Non-system, non-tool message: flush pending tool_results first.
        flush_tool_results()

        wire_content = [
            _block_to_anthropic(block, tool_map, role=msg.role) for block in msg.content
        ]
        wire_content = [b for b in wire_content if b is not None]
        if not wire_content:
            continue
        out.append(
            {"role": "user" if msg.role == Role.USER else "assistant", "content": wire_content}
        )

    flush_tool_results()
    system_text = "\n\n".join(s for s in system_parts if s) or None
    return out, system_text


def _block_to_anthropic(block: ContentBlock, tool_map: ToolIdMap, *, role: Role):
    if isinstance(block, TextBlock):
        return TextBlockParam(type="text", text=block.text)
    if isinstance(block, ImageBlock):
        return _image_to_anthropic(block)
    if isinstance(block, ToolUseBlock):
        # Use canonical id as the wire id (Anthropic accepts any string).
        # Record identity mapping for later round-trips.
        provider_id = tool_map.to_provider(block.id) or block.id
        tool_map.remember(block.id, provider_id)
        return ToolUseBlockParam(
            type="tool_use",
            id=provider_id,
            name=block.name,
            input=block.input,
        )
    if isinstance(block, ThinkingBlock):
        return {
            "type": "thinking",
            "thinking": block.text,
            **({"signature": block.signature} if block.signature else {}),
        }
    if isinstance(block, RedactedThinkingBlock):
        return {"type": "redacted_thinking", "data": block.data}
    logger.warning("dropping unsupported block type %s for role %s", type(block).__name__, role)
    return None


def _image_to_anthropic(block: ImageBlock):
    src = block.source
    if src.kind == "base64":
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": block.media_type,
                "data": src.data,
            },
        }
    if src.kind == "url":
        return {
            "type": "image",
            "source": {"type": "url", "url": src.data},
        }
    if src.kind == "file_ref":
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": block.media_type,
                "data": src.data,
            },
        }
    return None


def _tool_result_to_anthropic(block: ToolResultBlock, tool_map: ToolIdMap) -> ToolResultBlockParam:
    provider_id = tool_map.to_provider(block.tool_use_id) or block.tool_use_id
    tool_map.remember(block.tool_use_id, provider_id)
    parts = []
    for inner in block.content:
        if isinstance(inner, TextBlock):
            parts.append({"type": "text", "text": inner.text})
        elif isinstance(inner, ImageBlock):
            converted = _image_to_anthropic(inner)
            if converted:
                parts.append(converted)
    if not parts:
        parts = [{"type": "text", "text": ""}]
    return ToolResultBlockParam(
        type="tool_result",
        tool_use_id=provider_id,
        content=parts,
        is_error=block.is_error,
    )


# ---------------------------------------------------------------------------
# Streaming accumulator
# ---------------------------------------------------------------------------


class _AnthropicStreamAccumulator:
    """Per-stream state machine that turns Anthropic SSE events into canonical
    streaming events, accumulating final content + usage along the way."""

    def __init__(self, *, message_id: str, tool_map: ToolIdMap) -> None:
        self.message_id = message_id
        self.tool_map = tool_map
        self.stop_reason: str | None = None
        self._content_blocks: list[ContentBlock] = []
        self._current_index = -1
        self._current_type: str | None = None
        self._current_text = ""
        self._current_thinking = ""
        self._current_thinking_signature: str | None = None
        self._current_tool_id: str | None = None
        self._current_tool_name: str | None = None
        self._current_tool_input_json = ""
        self._input_tokens = 0
        self._output_tokens = 0
        self._cached_input_tokens = 0
        self._cache_creation_input_tokens = 0

    def consume(self, raw) -> list[StreamingEvent]:
        """Process one Anthropic SSE event and return zero or more canonical events."""
        etype = getattr(raw, "type", None)
        emitted: list[StreamingEvent] = []

        if etype == "message_start":
            msg = getattr(raw, "message", None)
            if msg is not None:
                usage = getattr(msg, "usage", None)
                if usage is not None:
                    self._input_tokens = getattr(usage, "input_tokens", 0) or 0
                    self._cached_input_tokens = getattr(usage, "cache_read_input_tokens", 0) or 0
                    self._cache_creation_input_tokens = (
                        getattr(usage, "cache_creation_input_tokens", 0) or 0
                    )
            return emitted

        if etype == "content_block_start":
            self._current_index += 1
            block = getattr(raw, "content_block", None)
            btype = getattr(block, "type", None)
            self._current_type = btype
            if btype == "tool_use":
                provider_id = getattr(block, "id", "")
                canonical_id = self.tool_map.to_canonical(provider_id) or provider_id
                self.tool_map.remember(canonical_id, provider_id)
                self._current_tool_id = canonical_id
                self._current_tool_name = getattr(block, "name", "")
                self._current_tool_input_json = ""
                emitted.append(
                    ToolUseStart(
                        message_id=self.message_id,
                        content_block_index=self._current_index,
                        tool_use_id=canonical_id,
                        tool_name=self._current_tool_name,
                    )
                )
            elif btype == "text":
                self._current_text = ""
                # Capture any pre-streamed text on the block itself (rare).
                pre = getattr(block, "text", "")
                if pre:
                    self._current_text = pre
                    emitted.append(
                        TextDelta(
                            message_id=self.message_id,
                            content_block_index=self._current_index,
                            text=pre,
                        )
                    )
            elif btype == "thinking":
                self._current_thinking = ""
                self._current_thinking_signature = None
            return emitted

        if etype == "content_block_delta":
            delta = getattr(raw, "delta", None)
            dtype = getattr(delta, "type", None)
            if dtype == "text_delta":
                text = getattr(delta, "text", "")
                self._current_text += text
                emitted.append(
                    TextDelta(
                        message_id=self.message_id,
                        content_block_index=self._current_index,
                        text=text,
                    )
                )
            elif dtype == "input_json_delta":
                partial = getattr(delta, "partial_json", "")
                self._current_tool_input_json += partial
                emitted.append(
                    ToolUseInputDelta(
                        message_id=self.message_id,
                        content_block_index=self._current_index,
                        tool_use_id=self._current_tool_id or "",
                        partial_json=partial,
                    )
                )
            elif dtype == "thinking_delta":
                thinking_text = getattr(delta, "thinking", "")
                self._current_thinking += thinking_text
                emitted.append(
                    ThinkingDelta(
                        message_id=self.message_id,
                        content_block_index=self._current_index,
                        text=thinking_text,
                    )
                )
            elif dtype == "signature_delta":
                self._current_thinking_signature = getattr(delta, "signature", None)
            return emitted

        if etype == "content_block_stop":
            if self._current_type == "tool_use":
                try:
                    final_input = (
                        json.loads(self._current_tool_input_json)
                        if self._current_tool_input_json
                        else {}
                    )
                except json.JSONDecodeError:
                    final_input = {}
                self._content_blocks.append(
                    ToolUseBlock(
                        id=self._current_tool_id or "",
                        name=self._current_tool_name or "",
                        input=final_input,
                    )
                )
                emitted.append(
                    ToolUseEnd(
                        message_id=self.message_id,
                        content_block_index=self._current_index,
                        tool_use_id=self._current_tool_id or "",
                        final_input=final_input,
                    )
                )
            elif self._current_type == "text":
                self._content_blocks.append(TextBlock(text=self._current_text))
            elif self._current_type == "thinking":
                self._content_blocks.append(
                    ThinkingBlock(
                        text=self._current_thinking,
                        signature=self._current_thinking_signature,
                    )
                )
            self._current_type = None
            return emitted

        if etype == "message_delta":
            delta = getattr(raw, "delta", None)
            if delta is not None:
                sr = getattr(delta, "stop_reason", None)
                if sr is not None:
                    self.stop_reason = sr
            usage = getattr(raw, "usage", None)
            if usage is not None:
                self._output_tokens = getattr(usage, "output_tokens", 0) or 0
            return emitted

        # message_stop and anything else: nothing to emit.
        return emitted

    def final_content(self) -> list[ContentBlock]:
        return list(self._content_blocks)

    def usage(self) -> TokenUsage:
        return TokenUsage(
            input_tokens=self._input_tokens,
            output_tokens=self._output_tokens,
            cached_input_tokens=self._cached_input_tokens,
            cache_creation_input_tokens=self._cache_creation_input_tokens,
        )


# ---- Response parsing ------------------------------------------------------


def _anthropic_blocks_to_canonical(response_blocks, tool_map: ToolIdMap) -> list[ContentBlock]:
    out: list[ContentBlock] = []
    for raw in response_blocks:
        # The SDK returns pydantic models with `type` attribute; treat them
        # uniformly via attribute access.
        btype = getattr(raw, "type", None)
        if btype == "text":
            out.append(TextBlock(text=raw.text))
        elif btype == "tool_use":
            # Canonical id == provider id for Anthropic. Record mapping.
            tool_map.remember(raw.id, raw.id)
            out.append(ToolUseBlock(id=raw.id, name=raw.name, input=dict(raw.input)))
        elif btype == "thinking":
            sig = getattr(raw, "signature", None)
            out.append(ThinkingBlock(text=raw.thinking, signature=sig))
        elif btype == "redacted_thinking":
            out.append(RedactedThinkingBlock(data=raw.data))
        elif btype == "image":
            # Assistant doesn't typically emit images; included for safety.
            src = raw.source
            kind = getattr(src, "type", "base64")
            data = getattr(src, "data", "") if kind == "base64" else getattr(src, "url", "")
            media_type = getattr(src, "media_type", "image/png")
            out.append(
                ImageBlock(
                    source=ImageSource(kind=kind, data=data),
                    media_type=media_type,
                )
            )
        else:
            logger.warning("ignoring unknown anthropic block type %r", btype)
    return out


# ---- Error translation -----------------------------------------------------


def _translate_status_error(exc: anthropic.APIStatusError, request_id: str) -> AdapterError:
    status = exc.status_code
    body: dict | None = None
    try:
        body = exc.response.json() if exc.response is not None else None
    except Exception:
        body = None
    classification = classify_anthropic_response(status, body)
    msg = _provider_message(body) or str(exc)
    retry_after = _retry_after_seconds(exc, body)
    return error_for_class(
        classification,
        f"anthropic {status}: {msg}",
        provider_status=status,
        provider_message=msg,
        request_id=request_id,
        retry_after_seconds=retry_after,
    )


def _provider_message(body: dict | None) -> str:
    if not body or not isinstance(body, dict):
        return ""
    err = body.get("error")
    if isinstance(err, dict):
        return err.get("message", "")
    return ""


def _retry_after_seconds(exc: anthropic.APIStatusError, body: dict | None) -> float | None:
    resp = getattr(exc, "response", None)
    if resp is None:
        return None
    header = resp.headers.get("retry-after") if hasattr(resp, "headers") else None
    if header:
        try:
            return float(header)
        except ValueError:
            return None
    return None


__all__ = ["AnthropicAdapter"]
