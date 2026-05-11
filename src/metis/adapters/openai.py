"""OpenAI provider adapter.

Wire-format translation per provider-adapter-contract.md §4.3. Key differences
from the Anthropic adapter:

- System messages live IN the messages list as the first entry (no hoisting).
- Tool calls live on the assistant message as a `tool_calls[]` array, separate
  from `content`. `function.arguments` is a JSON-stringified string.
- Tool results are their own message with `role: "tool"` and `tool_call_id`
  (NOT a user-with-tool_result pattern).
- Provider issues `call_*` ids; we map them to canonical `tu_<ulid>` via the
  per-session `ToolIdMap`. This is where the map is actually load-bearing.
- Images use `{type: "image_url", image_url: {url: ...}}` (data URI for base64).
- ThinkingBlock / RedactedThinkingBlock are dropped on the wire with a WARN
  log — OpenAI's reasoning models use a different mechanism we don't bridge.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

import httpx
import openai

from metis.adapters.errors import (
    AdapterError,
    CancelledError,
    ErrorClass,
    NetworkError,
    classify_http_status,
    error_for_class,
)
from metis.adapters.protocol import (
    CanonicalRequest,
    CanonicalResponse,
    StopReason,
    TokenUsage,
)
from metis.adapters.retry import RetryPolicy, with_retry
from metis.adapters.tool_id_map import ToolIdMap
from metis.canonical.capabilities import AdapterCapabilities
from metis.canonical.content import (
    ContentBlock,
    ImageBlock,
    ImageSource,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from metis.canonical.ids import new_tool_use_id
from metis.canonical.messages import Message, Role
from metis.canonical.tools import ToolDefinition

logger = logging.getLogger(__name__)


# Per-model capability declarations. GPT-5 family.
_CAPS_GPT5 = AdapterCapabilities(
    supports_thinking=False,
    supports_images=True,
    supports_tools=True,
    supports_system_prompt=True,
    supports_structured_output=True,
    supports_streaming=True,
    supports_streaming_tool_calls=True,
    supports_parallel_tool_calls=True,
    supports_prompt_caching=True,
    supports_system_messages_in_list=True,  # NOT hoisted
    max_context_tokens=200_000,
    max_output_tokens=16_384,
    accepted_image_media_types=["image/png", "image/jpeg", "image/gif", "image/webp"],
)

_CAPS_GPT5_MINI = AdapterCapabilities(
    supports_thinking=False,
    supports_images=True,
    supports_tools=True,
    supports_system_prompt=True,
    supports_structured_output=True,
    supports_streaming=True,
    supports_streaming_tool_calls=True,
    supports_parallel_tool_calls=True,
    supports_prompt_caching=True,
    supports_system_messages_in_list=True,
    max_context_tokens=128_000,
    max_output_tokens=16_384,
    accepted_image_media_types=["image/png", "image/jpeg", "image/gif", "image/webp"],
)

_MODEL_CAPS: dict[str, AdapterCapabilities] = {
    "openai:gpt-5": _CAPS_GPT5,
    "openai:gpt-5-mini": _CAPS_GPT5_MINI,
}


class OpenAIAdapter:
    """OpenAI Chat Completions API adapter."""

    name = "openai"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        organization: str | None = None,
        timeout_seconds: float = 600.0,
        retry_policy: RetryPolicy | None = None,
        client: openai.AsyncOpenAI | None = None,
    ) -> None:
        # Disable the SDK's own retries so retry logic lives in one place.
        self._client = client or openai.AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            organization=organization,
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
            raise ValueError(f"unknown openai model: {model!r}") from None

    def estimate_input_tokens(
        self,
        messages: list[Message],
        tools: list[ToolDefinition],
        system_prompt: str | None,
    ) -> int:
        """±10% heuristic: ~4 chars per token (matching Anthropic adapter)."""
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
        task = asyncio.current_task()
        if task is not None:
            self._in_flight[request.request_id] = task
        try:
            return await with_retry(lambda: self._call_once(request), policy=self._retry_policy)
        except asyncio.CancelledError as exc:
            raise CancelledError("request cancelled", request_id=request.request_id) from exc
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

    # ---- Single call --------------------------------------------------

    async def _call_once(self, request: CanonicalRequest) -> CanonicalResponse:
        # NOTE: `or ToolIdMap()` would break — ToolIdMap.__len__ makes an
        # empty map falsy, so we'd silently allocate a new map and drop the
        # caller's mutations on the floor.
        tool_map = request.tool_id_map if request.tool_id_map is not None else ToolIdMap()
        wire_messages = _canonical_messages_to_openai(
            request.messages, request.system_prompt, tool_map
        )
        wire_tools = [_tool_to_openai(t) for t in request.tools]
        wire_model = _wire_model_name(request.model)

        kwargs: dict = {
            "model": wire_model,
            "max_completion_tokens": request.max_output_tokens,
            "messages": wire_messages,
        }
        if wire_tools:
            kwargs["tools"] = wire_tools
        if request.stop_sequences:
            kwargs["stop"] = request.stop_sequences
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature
        if request.output_schema is not None:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "response",
                    "schema": request.output_schema,
                    "strict": True,
                },
            }

        start = time.monotonic()
        try:
            response = await self._client.chat.completions.create(**kwargs)
        except openai.APIStatusError as exc:
            raise _translate_status_error(exc, request.request_id) from exc
        except openai.APIConnectionError as exc:
            raise NetworkError(
                f"openai connection error: {exc}", request_id=request.request_id
            ) from exc
        except openai.APITimeoutError as exc:
            raise NetworkError(f"openai timeout: {exc}", request_id=request.request_id) from exc
        except httpx.HTTPError as exc:
            raise NetworkError(f"http error: {exc}", request_id=request.request_id) from exc

        latency_ms = int((time.monotonic() - start) * 1000)
        choice = response.choices[0]
        content = _openai_message_to_canonical(choice.message, tool_map)
        usage = _usage_to_canonical(response.usage)
        return CanonicalResponse(
            request_id=request.request_id,
            model=request.model,
            provider=self.name,
            content=content,
            stop_reason=_stop_reason(choice.finish_reason),
            usage=usage,
            latency_ms=latency_ms,
        )


# ---------------------------------------------------------------------------
# Wire translation
# ---------------------------------------------------------------------------


def _wire_model_name(canonical: str) -> str:
    if ":" not in canonical:
        return canonical
    return canonical.split(":", 1)[1]


def _stop_reason(raw: str | None) -> StopReason:
    if raw == "stop":
        return StopReason.END_TURN
    if raw == "length":
        return StopReason.MAX_TOKENS
    if raw == "tool_calls" or raw == "function_call":
        return StopReason.TOOL_USE
    # `content_filter` and anything else: treat as end_turn for canonical purposes.
    return StopReason.END_TURN


def _tool_to_openai(tool: ToolDefinition) -> dict:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.input_schema,
        },
    }


def _content_block_text_chars(block: ContentBlock) -> int:
    if isinstance(block, TextBlock):
        return len(block.text)
    if isinstance(block, ToolUseBlock):
        return len(block.name) + len(str(block.input))
    if isinstance(block, ToolResultBlock):
        return sum(_content_block_text_chars(b) for b in block.content) + 16
    if isinstance(block, ImageBlock):
        return 1024
    return 0


def _canonical_messages_to_openai(
    messages: list[Message],
    system_prompt: str | None,
    tool_map: ToolIdMap,
) -> list[dict]:
    """Translate the canonical message list to OpenAI wire format.

    - SYSTEM canonical messages and the optional `system_prompt` are
      concatenated into a single first message with role=system.
    - ASSISTANT messages produce content text + a `tool_calls[]` array.
    - TOOL canonical messages produce role=tool messages with `tool_call_id`.
    """
    system_parts: list[str] = []
    if system_prompt:
        system_parts.append(system_prompt)

    out: list[dict] = []
    for msg in messages:
        if msg.role == Role.SYSTEM:
            for block in msg.content:
                if isinstance(block, TextBlock):
                    system_parts.append(block.text)
            continue
        if msg.role == Role.USER:
            out.append(_user_message(msg))
            continue
        if msg.role == Role.ASSISTANT:
            out.append(_assistant_message(msg, tool_map))
            continue
        if msg.role == Role.TOOL:
            out.extend(_tool_messages(msg, tool_map))
            continue

    system_text = "\n\n".join(s for s in system_parts if s)
    if system_text:
        out.insert(0, {"role": "system", "content": system_text})
    return out


def _user_message(msg: Message) -> dict:
    parts: list[dict] = []
    text_only = True
    for block in msg.content:
        if isinstance(block, TextBlock):
            parts.append({"type": "text", "text": block.text})
        elif isinstance(block, ImageBlock):
            parts.append(_image_to_openai(block))
            text_only = False
    # If the message is plain text, OpenAI accepts content as a string for
    # smaller payloads; multimodal must be a list.
    if text_only and len(parts) == 1:
        return {"role": "user", "content": parts[0]["text"]}
    return {"role": "user", "content": parts}


def _assistant_message(msg: Message, tool_map: ToolIdMap) -> dict:
    text_pieces: list[str] = []
    tool_calls: list[dict] = []
    for block in msg.content:
        if isinstance(block, TextBlock):
            text_pieces.append(block.text)
        elif isinstance(block, ToolUseBlock):
            provider_id = tool_map.to_provider(block.id)
            if provider_id is None:
                # First time seeing this canonical id on the wire — generate
                # an OpenAI-shaped provider id and record the mapping.
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
        else:
            # ThinkingBlock, RedactedThinkingBlock — drop with a log.
            logger.warning(
                "dropping block of type %s on openai wire (not representable)",
                type(block).__name__,
            )
    out: dict = {"role": "assistant"}
    if text_pieces:
        out["content"] = "\n".join(text_pieces)
    if tool_calls:
        out["tool_calls"] = tool_calls
    return out


def _tool_messages(msg: Message, tool_map: ToolIdMap) -> list[dict]:
    """A canonical TOOL message has exactly one ToolResultBlock. OpenAI
    represents tool results as standalone role=tool messages."""
    out: list[dict] = []
    for block in msg.content:
        if not isinstance(block, ToolResultBlock):
            continue
        provider_id = tool_map.to_provider(block.tool_use_id)
        if provider_id is None:
            # Defensive: synthesize a provider id from canonical if mapping
            # somehow wasn't recorded.
            provider_id = f"call_{block.tool_use_id.removeprefix('tu_')}"
            tool_map.remember(block.tool_use_id, provider_id)
        content = _tool_result_content_to_string(block)
        out.append(
            {
                "role": "tool",
                "tool_call_id": provider_id,
                "content": content,
            }
        )
    return out


def _tool_result_content_to_string(block: ToolResultBlock) -> str:
    """OpenAI's tool message content is a plain string. We concatenate
    text blocks and emit `[image]` placeholders for any images.
    """
    parts: list[str] = []
    for inner in block.content:
        if isinstance(inner, TextBlock):
            parts.append(inner.text)
        elif isinstance(inner, ImageBlock):
            parts.append("[image]")
    return "\n".join(parts)


def _image_to_openai(block: ImageBlock) -> dict:
    src = block.source
    if src.kind == "url":
        return {"type": "image_url", "image_url": {"url": src.data}}
    if src.kind == "base64":
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{block.media_type};base64,{src.data}"},
        }
    if src.kind == "file_ref":
        # Same shape as base64 — caller is responsible for providing data.
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{block.media_type};base64,{src.data}"},
        }
    return {"type": "image_url", "image_url": {"url": ""}}


# ---- Response parsing ------------------------------------------------------


def _openai_message_to_canonical(message, tool_map: ToolIdMap) -> list[ContentBlock]:
    """Translate an OpenAI assistant message back into canonical content blocks.

    On first sight of a `call_*` id we generate a canonical `tu_<ulid>` and
    record the bidirectional mapping for future round-trips.
    """
    out: list[ContentBlock] = []
    content = getattr(message, "content", None)
    if isinstance(content, str) and content:
        out.append(TextBlock(text=content))
    elif isinstance(content, list):
        for part in content:
            ptype = getattr(part, "type", None) or (
                part.get("type") if isinstance(part, dict) else None
            )
            if ptype == "text":
                text = getattr(part, "text", None) or (
                    part.get("text") if isinstance(part, dict) else ""
                )
                out.append(TextBlock(text=text))
            elif ptype == "image_url":
                # Assistants almost never emit images; included for safety.
                url = ""
                image_url = getattr(part, "image_url", None) or (
                    part.get("image_url") if isinstance(part, dict) else None
                )
                if image_url:
                    url = getattr(image_url, "url", None) or image_url.get("url", "")
                if url.startswith("data:"):
                    media_type, _, b64 = url.partition(";base64,")
                    media_type = media_type.removeprefix("data:") or "image/png"
                    out.append(
                        ImageBlock(
                            source=ImageSource(kind="base64", data=b64),
                            media_type=media_type,
                        )
                    )
                else:
                    out.append(
                        ImageBlock(
                            source=ImageSource(kind="url", data=url),
                            media_type="image/png",
                        )
                    )

    tool_calls = getattr(message, "tool_calls", None) or []
    for call in tool_calls:
        call_id = getattr(call, "id", None) or call.get("id")
        fn = getattr(call, "function", None) or call.get("function")
        name = getattr(fn, "name", None) or fn.get("name")
        raw_args = getattr(fn, "arguments", None) or fn.get("arguments")
        try:
            parsed = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
        except json.JSONDecodeError:
            logger.warning("openai tool_call %s had non-JSON arguments; using empty dict", call_id)
            parsed = {}

        canonical_id = tool_map.to_canonical(call_id) if call_id else None
        if canonical_id is None:
            canonical_id = new_tool_use_id()
            if call_id is not None:
                tool_map.remember(canonical_id, call_id)
        out.append(ToolUseBlock(id=canonical_id, name=name, input=parsed))
    return out


def _usage_to_canonical(usage) -> TokenUsage:
    """OpenAI usage → canonical TokenUsage.

    cache_creation_input_tokens is always 0 — OpenAI doesn't separately
    report cache creation tokens; their cache is provider-managed."""
    prompt = getattr(usage, "prompt_tokens", 0) or 0
    completion = getattr(usage, "completion_tokens", 0) or 0
    cached = 0
    details = getattr(usage, "prompt_tokens_details", None)
    if details is not None:
        cached = getattr(details, "cached_tokens", 0) or 0
    return TokenUsage(
        input_tokens=prompt,
        output_tokens=completion,
        cached_input_tokens=cached,
        cache_creation_input_tokens=0,
    )


# ---- Error translation -----------------------------------------------------


def _translate_status_error(exc: openai.APIStatusError, request_id: str) -> AdapterError:
    status = exc.status_code
    body: dict | None = None
    try:
        body = exc.response.json() if exc.response is not None else None
    except Exception:
        body = None
    classification = _classify_openai_response(status, body)
    msg = _provider_message(body) or str(exc)
    retry_after = _retry_after_seconds(exc)
    return error_for_class(
        classification,
        f"openai {status}: {msg}",
        provider_status=status,
        provider_message=msg,
        request_id=request_id,
        retry_after_seconds=retry_after,
    )


def _classify_openai_response(status: int, body: dict | None) -> ErrorClass:
    """OpenAI-specific classifier (provider-adapter §6.2).

    Body's `error.code` refines the HTTP status mapping:
    - rate_limit_exceeded → RATE_LIMIT (also 429)
    - context_length_exceeded → CONTEXT_OVERFLOW (often surfaces as 400)
    - invalid_api_key → AUTH (also 401)
    - server_error → SERVER_ERROR (also 5xx)
    """
    default = classify_http_status(status, body)
    if not body or "error" not in body:
        return default
    err = body["error"]
    code = err.get("code", "") if isinstance(err, dict) else ""
    err_type = err.get("type", "") if isinstance(err, dict) else ""

    if code == "rate_limit_exceeded":
        return ErrorClass.RATE_LIMIT
    if code == "context_length_exceeded":
        return ErrorClass.CONTEXT_OVERFLOW
    if code in ("invalid_api_key", "authentication_error"):
        return ErrorClass.AUTH
    if err_type == "server_error":
        return ErrorClass.SERVER_ERROR
    return default


def _provider_message(body: dict | None) -> str:
    if not body or not isinstance(body, dict):
        return ""
    err = body.get("error")
    if isinstance(err, dict):
        return err.get("message", "")
    return ""


def _retry_after_seconds(exc: openai.APIStatusError) -> float | None:
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


__all__ = ["OpenAIAdapter"]
