"""Anthropic provider adapter.

Wire-format translation per provider-adapter-contract.md §4.2. The adapter:

- Hoists canonical SYSTEM messages into the `system` request parameter.
- Translates ASSISTANT/USER content blocks directly to Anthropic blocks.
- Merges consecutive canonical TOOL messages into a single user message with
  multiple `tool_result` blocks (Anthropic's wire format).
- Uses canonical `tu_<ulid>` ids as wire ids (Anthropic accepts any string),
  recording the identity mapping in the per-session ToolIdMap.
- Resolves `ImageBlock(kind="file_ref")` through `WorkspaceFileAPI` (path
  security is load-bearing — `..` escape and out-of-root symlinks are
  rejected), reads the bytes, base64-encodes them, and emits an
  Anthropic-shape image block with the inferred media type.
- Writes prompt-cache breakpoints per `docs/specs/context-assembler.md`:
  `cache_control: {"type": "ephemeral"}` on the last tool definition and
  on the last *stable* system block. The volatile system block (driven
  by `CanonicalRequest.system_prompt_volatile`) trails the breakpoint
  so per-turn memory mutations don't churn the cached prefix.
- Reports raw token counts; cost is computed by the core.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from collections.abc import AsyncIterator

import anthropic
import httpx
from anthropic.types import (
    MessageParam,
    ToolParam,
    ToolResultBlockParam,
    ToolUseBlockParam,
)

from metis.core.adapters.errors import (
    AdapterError,
    CancelledError,
    ErrorClass,
    InvalidRequestError,
    NetworkError,
    ServerError,
    classify_anthropic_response,
    error_for_class,
)
from metis.core.adapters.protocol import (
    CanonicalRequest,
    CanonicalResponse,
    StopReason,
    TokenUsage,
)
from metis.core.adapters.retry import RetryPolicy, with_retry
from metis.core.adapters.streaming import (
    MessageComplete,
    MessageStart,
    StreamingEvent,
    TextDelta,
    ThinkingDelta,
    ToolUseEnd,
    ToolUseInputDelta,
    ToolUseStart,
)
from metis.core.adapters.tool_id_map import ToolIdMap
from metis.core.canonical.batch import (
    BatchError,
    BatchHandle,
    BatchStatus,
)
from metis.core.canonical.capabilities import AdapterCapabilities
from metis.core.canonical.content import (
    ContentBlock,
    ImageBlock,
    ImageSource,
    RedactedThinkingBlock,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from metis.core.canonical.ids import new_message_id
from metis.core.canonical.messages import Message, Role
from metis.core.canonical.tools import ToolDefinition
from metis.core.tools.workspace import WorkspaceEscapeError, WorkspaceFileAPI

logger = logging.getLogger(__name__)


# Map common image extensions to Anthropic-accepted media types. Anything
# unmapped falls back to image/png (the most permissive default).
_IMAGE_EXTENSION_MEDIA_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


# Per-model capability declarations. `supports_batch_api=True` reflects
# Anthropic's Batches API support across the Claude 4.x line per
# provider-adapter-contract.md §4.6.3 (`POST /v1/messages/batches`).
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
    supports_batch_api=True,
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

    # ---- Asynchronous batch submission (§4.6) -------------------------

    async def submit_batch(self, requests: list[CanonicalRequest]) -> BatchHandle:
        """Submit `requests` to Anthropic's Batches API.

        Wire mapping per provider-adapter-contract.md §4.6.3: each
        canonical request becomes a `{custom_id, params}` entry in the
        batch creation body. `custom_id` defaults to the canonical
        `request_id`; the caller can pre-rewrite `request_id` if it
        needs a stable cross-process key. Duplicate `custom_id`s within a
        single batch are an upstream-rejected condition; the adapter
        does not deduplicate.
        """
        if not requests:
            raise InvalidRequestError(
                "submit_batch: requests is empty",
                request_id="",
            )
        # Detect duplicate custom_ids early — Anthropic rejects them
        # upstream with a 400, but the local error is clearer.
        seen: set[str] = set()
        for req in requests:
            if req.request_id in seen:
                raise InvalidRequestError(
                    f"submit_batch: duplicate request_id {req.request_id!r}; "
                    "custom_ids within a batch must be unique",
                    request_id=req.request_id,
                )
            seen.add(req.request_id)

        # Build the wire bodies. `_assemble_messages_create_kwargs` returns
        # the same kwarg shape `messages.create` consumes; we strip
        # `stream` (batch is not a streaming surface).
        wire_requests: list[dict] = []
        for req in requests:
            kwargs = self._assemble_messages_create_kwargs(req)
            kwargs.pop("stream", None)
            wire_requests.append({"custom_id": req.request_id, "params": kwargs})

        try:
            batch = await self._client.messages.batches.create(
                requests=wire_requests  # type: ignore[arg-type]
            )
        except anthropic.APIStatusError as exc:
            raise _translate_status_error(exc, request_id="") from exc
        except anthropic.APIConnectionError as exc:
            raise NetworkError(f"anthropic connection error: {exc}") from exc
        except anthropic.APITimeoutError as exc:
            raise NetworkError(f"anthropic timeout: {exc}") from exc
        except httpx.HTTPError as exc:
            raise NetworkError(f"http error: {exc}") from exc

        submitted_at_ms = int(time.time() * 1000)
        return BatchHandle(
            provider=self.name,
            batch_id=batch.id,
            submitted_at_ms=submitted_at_ms,
            request_count=len(requests),
            custom_ids=tuple(req.request_id for req in requests),
        )

    async def poll_batch(self, handle: BatchHandle) -> BatchStatus:
        """Return the current upstream status of `handle`.

        Maps Anthropic's `processing_status` (`in_progress`, `canceling`,
        `ended`) plus the `request_counts` breakdown to the canonical
        `BatchStatus` literal. A batch in `ended` state with non-zero
        `expired` counts maps to `"expired"`; otherwise `ended` is
        `"completed"`. A batch with `errored == request_count` maps to
        `"failed"` (an entire-batch upstream abort that produced no
        successful results).
        """
        _ensure_provider_matches(handle, self.name)
        try:
            batch = await self._client.messages.batches.retrieve(handle.batch_id)
        except anthropic.APIStatusError as exc:
            raise _translate_status_error(exc, request_id=handle.batch_id) from exc
        except anthropic.APIConnectionError as exc:
            raise NetworkError(f"anthropic connection error: {exc}") from exc
        except anthropic.APITimeoutError as exc:
            raise NetworkError(f"anthropic timeout: {exc}") from exc
        except httpx.HTTPError as exc:
            raise NetworkError(f"http error: {exc}") from exc

        return _classify_batch_status(batch, handle.request_count)

    async def fetch_batch(
        self,
        handle: BatchHandle,
    ) -> list[CanonicalResponse | BatchError]:
        """Retrieve per-request results for a completed batch.

        Returns a list same-length, same-order as `handle.custom_ids`.
        Per-request failures (a Messages-API error returned for one
        custom_id) surface as `BatchError`; per-request expirations
        (24h elapsed before that request was scheduled) also surface as
        `BatchError(error_class=SERVER_ERROR, retryable=True)`.

        Batch-level failures — `processing_status='ended'` with zero
        successes and non-trivial errored counts — currently surface as
        `BatchError` per row too (Anthropic emits one row per
        `custom_id` even on full-batch failure). A truly empty results
        stream raises `ServerError`.
        """
        _ensure_provider_matches(handle, self.name)

        # Re-fetch status first: if the batch is fully expired *without*
        # any results being emitted (per §4.6.6), every custom_id must
        # surface as a `BatchError`. Anthropic's results endpoint also
        # emits an `expired`-typed row per custom_id in this case, so the
        # natural translation falls out of the per-row classifier — but
        # we still poll first to fail fast on `queued` / `in_progress`
        # and to detect batch-level `failed`.
        try:
            batch = await self._client.messages.batches.retrieve(handle.batch_id)
        except anthropic.APIStatusError as exc:
            raise _translate_status_error(exc, request_id=handle.batch_id) from exc
        except anthropic.APIConnectionError as exc:
            raise NetworkError(f"anthropic connection error: {exc}") from exc
        except anthropic.APITimeoutError as exc:
            raise NetworkError(f"anthropic timeout: {exc}") from exc
        except httpx.HTTPError as exc:
            raise NetworkError(f"http error: {exc}") from exc

        status = _classify_batch_status(batch, handle.request_count)
        if status in ("queued", "in_progress"):
            # Provider-side block-until-completion semantics are
            # acceptable per §4.6.2; the synchronous re-poll path makes
            # the caller's loop predictable.
            raise ServerError(
                f"batch {handle.batch_id} still {status}; poll until completed before fetching",
                request_id=handle.batch_id,
            )

        # Pull the results stream. The SDK returns an async iterator of
        # `MessageBatchIndividualResponse` rows. A fully-expired batch
        # emits one `expired`-typed row per custom_id; we translate them
        # uniformly via `_translate_batch_row`.
        try:
            stream = await self._client.messages.batches.results(handle.batch_id)
        except anthropic.APIStatusError as exc:
            raise _translate_status_error(exc, request_id=handle.batch_id) from exc
        except anthropic.APIConnectionError as exc:
            raise NetworkError(f"anthropic connection error: {exc}") from exc
        except anthropic.APITimeoutError as exc:
            raise NetworkError(f"anthropic timeout: {exc}") from exc
        except httpx.HTTPError as exc:
            raise NetworkError(f"http error: {exc}") from exc

        by_custom_id: dict[str, CanonicalResponse | BatchError] = {}
        async for row in stream:
            translated = _translate_batch_row(row, model_lookup=_request_model_lookup(handle))
            by_custom_id[translated_custom_id(translated)] = translated

        # Preserve input order; missing rows surface as PROVIDER_TRANSIENT
        # (treated as "still queued upstream" — caller can retry the
        # whole batch). This matches §4.6.6's "expired" semantics from
        # the caller's perspective even though Anthropic normally emits
        # a row per custom_id.
        if not by_custom_id:
            raise ServerError(
                f"batch {handle.batch_id} returned no result rows "
                f"(processing_status={getattr(batch, 'processing_status', '?')})",
                request_id=handle.batch_id,
            )

        out: list[CanonicalResponse | BatchError] = []
        for custom_id in handle.custom_ids:
            matched: CanonicalResponse | BatchError | None = by_custom_id.get(custom_id)
            if matched is None:
                out.append(
                    BatchError(
                        custom_id=custom_id,
                        error_class=ErrorClass.SERVER_ERROR,
                        error_message="missing result row from upstream",
                        retryable=True,
                    )
                )
            else:
                out.append(matched)
        return out

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
        workspace_files = _maybe_workspace_files(request.workspace_path)
        anthropic_messages, stable_system_text = _canonical_messages_to_anthropic(
            request.messages, request.system_prompt, tool_map, workspace_files
        )
        anthropic_messages = _with_history_cache_breakpoint(anthropic_messages)
        wire_tools = _tools_to_anthropic_with_cache(request.tools)
        wire_model = _wire_model_name(request.model)

        kwargs: dict = {
            "model": wire_model,
            "max_tokens": request.max_output_tokens,
            "messages": anthropic_messages,
            "stream": True,
        }
        system_blocks = _system_blocks(stable_system_text, request.system_prompt_volatile)
        if system_blocks:
            kwargs["system"] = system_blocks
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

    def _assemble_messages_create_kwargs(self, request: CanonicalRequest) -> dict:
        """Compose the kwargs dict that `messages.create` consumes.

        Shared by `_call_once` and `submit_batch` so the batch path goes
        through the same wire-translation logic — tool-id remapping,
        cache breakpoint placement, system-block hoisting, etc.
        """
        # NOTE: `or ToolIdMap()` would break — ToolIdMap.__len__ makes an
        # empty map falsy, so we'd silently allocate a new map and drop the
        # caller's mutations on the floor.
        tool_map = request.tool_id_map if request.tool_id_map is not None else ToolIdMap()
        workspace_files = _maybe_workspace_files(request.workspace_path)
        anthropic_messages, stable_system_text = _canonical_messages_to_anthropic(
            request.messages, request.system_prompt, tool_map, workspace_files
        )
        anthropic_messages = _with_history_cache_breakpoint(anthropic_messages)
        wire_tools = _tools_to_anthropic_with_cache(request.tools)
        wire_model = _wire_model_name(request.model)

        kwargs: dict = {
            "model": wire_model,
            "max_tokens": request.max_output_tokens,
            "messages": anthropic_messages,
        }
        system_blocks = _system_blocks(stable_system_text, request.system_prompt_volatile)
        if system_blocks:
            kwargs["system"] = system_blocks
        if wire_tools:
            kwargs["tools"] = wire_tools
        if request.stop_sequences:
            kwargs["stop_sequences"] = request.stop_sequences
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature
        return kwargs

    async def _call_once(self, request: CanonicalRequest) -> CanonicalResponse:
        kwargs = self._assemble_messages_create_kwargs(request)
        tool_map = request.tool_id_map if request.tool_id_map is not None else ToolIdMap()

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


def _tools_to_anthropic_with_cache(tools: list[ToolDefinition]) -> list[ToolParam]:
    """Translate canonical tools and place a cache breakpoint on the last
    one. The breakpoint covers the entire `tools` section in Anthropic's
    cache-prefix walk (see `docs/specs/context-assembler.md` §3).

    Cast through `dict` so we can attach `cache_control` without the
    TypedDict TS-style narrowing complaining about extra keys.
    """
    if not tools:
        return []
    out: list[dict] = [_tool_to_anthropic(t) for t in tools]  # type: ignore[misc]
    out[-1] = {**out[-1], "cache_control": {"type": "ephemeral"}}
    return out  # type: ignore[return-value]


def _with_history_cache_breakpoint(messages: list[MessageParam]) -> list[MessageParam]:
    """Place a rolling cache breakpoint on the last content block of the
    last message, extending the cached prefix over the whole transcript.

    The tools and stable-system breakpoints (`_tools_to_anthropic_with_cache`,
    `_system_blocks`) cache only the static prefix; without this third
    breakpoint the conversation transcript falls outside the cached prefix
    and is re-billed at full input rate every turn. With it, turn N+1 reads
    `tools + system + history-through-turn-N` at cache-read rate and pays
    full price only on the new delta.

    The marker rolls forward — it sits on a newer block every request — but
    the prefix it caches is byte-stable with the previous turn, which is
    what Anthropic matches on. See `docs/specs/context-assembler.md` §3.

    Returns a new list; the input is not mutated. A no-op on empty input or
    a last message with no content blocks.
    """
    if not messages:
        return messages
    last: dict = dict(messages[-1])
    content = last.get("content")
    # Messages from `_canonical_messages_to_anthropic` always carry a
    # non-empty list `content`; guard so a malformed message can't crash
    # request assembly.
    if not isinstance(content, list) or not content:
        return messages
    new_content: list = list(content)
    new_content[-1] = {**new_content[-1], "cache_control": {"type": "ephemeral"}}
    last["content"] = new_content
    return [*messages[:-1], last]  # type: ignore[list-item]


def _system_blocks(stable_text: str | None, volatile_text: str | None) -> list[dict] | None:
    """Build the `system` request param as a typed-block list.

    Returns:
        - None when both segments are empty (caller omits the kwarg).
        - One block (stable, with cache_control) when only stable is set.
        - Two blocks (stable with cache_control, then volatile without)
          when both are set. The cache breakpoint sits on the stable
          block so per-turn mutations to the volatile content don't
          churn the cached prefix.

    Empty strings are treated as missing.
    """
    stable = (stable_text or "").strip() if stable_text else ""
    volatile = (volatile_text or "").strip() if volatile_text else ""
    blocks: list[dict] = []
    if stable:
        blocks.append(
            {
                "type": "text",
                "text": stable_text,
                "cache_control": {"type": "ephemeral"},
            }
        )
    if volatile:
        blocks.append({"type": "text", "text": volatile_text})
    return blocks or None


def _maybe_workspace_files(workspace_path: str | None) -> WorkspaceFileAPI | None:
    """Build a WorkspaceFileAPI for `file_ref` image resolution, or None
    if no workspace context is available. Failure here is non-fatal — the
    file_ref blocks just get dropped with a WARN, matching the spec's
    lossy-projection rule (canonical-format §7.3)."""
    if not workspace_path:
        return None
    try:
        return WorkspaceFileAPI(workspace_path)
    except (ValueError, OSError) as exc:
        logger.warning("anthropic adapter: workspace_path %r unusable: %s", workspace_path, exc)
        return None


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
    workspace_files: WorkspaceFileAPI | None = None,
) -> tuple[list[MessageParam], str | None]:
    """Translate the canonical message list to Anthropic wire format.

    - SYSTEM messages are concatenated and hoisted out of the list.
    - Consecutive TOOL messages are merged into a single user message
      carrying multiple tool_result blocks.
    - `workspace_files`, if provided, resolves `ImageBlock(kind="file_ref")`
      payloads. Without it, file_ref images are dropped with a WARN.
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
                    pending_tool_results.append(
                        _tool_result_to_anthropic(block, tool_map, workspace_files)
                    )
            continue

        # Non-system, non-tool message: flush pending tool_results first.
        flush_tool_results()

        wire_content = [
            _block_to_anthropic(block, tool_map, role=msg.role, workspace_files=workspace_files)
            for block in msg.content
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


def _block_to_anthropic(
    block: ContentBlock,
    tool_map: ToolIdMap,
    *,
    role: Role,
    workspace_files: WorkspaceFileAPI | None = None,
):
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, ImageBlock):
        return _image_to_anthropic(block, workspace_files)
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


def _image_to_anthropic(block: ImageBlock, workspace_files: WorkspaceFileAPI | None = None):
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
        return _file_ref_image_to_anthropic(block, workspace_files)
    logger.warning("dropping image with unknown source kind %r", src.kind)
    return None


def _file_ref_image_to_anthropic(
    block: ImageBlock, workspace_files: WorkspaceFileAPI | None
) -> dict | None:
    """Resolve a workspace-relative path through `WorkspaceFileAPI`, base64
    the bytes, and emit the Anthropic-shape base64 image block.

    The path goes through `WorkspaceFileAPI._resolve` (workspace path
    security is load-bearing per AGENTS.md — `..` escape and out-of-root
    symlinks are rejected by construction). Failure modes (no workspace,
    file missing, escape, read error) drop the block with a WARN per
    canonical-format §7.3.
    """
    if workspace_files is None:
        logger.warning(
            "dropping file_ref image %r: adapter has no workspace context", block.source.data
        )
        return None
    try:
        data = workspace_files.read_bytes(block.source.data)
    except WorkspaceEscapeError as exc:
        logger.warning("dropping file_ref image: %s", exc)
        return None
    except (FileNotFoundError, IsADirectoryError, PermissionError, OSError) as exc:
        logger.warning("dropping file_ref image %r: %s", block.source.data, exc)
        return None
    media_type = block.media_type or _media_type_from_path(block.source.data)
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": base64.standard_b64encode(data).decode("ascii"),
        },
    }


def _media_type_from_path(path: str) -> str:
    """Infer media type from the file extension.

    Anthropic accepts image/{png,jpeg,gif,webp}. Unknown extensions fall
    back to image/png — the most permissive default. Callers that care
    about exact typing should set `ImageBlock.media_type` explicitly.
    """
    lower = path.rsplit(".", 1)
    if len(lower) == 2:
        ext = "." + lower[1].lower()
        if ext in _IMAGE_EXTENSION_MEDIA_TYPES:
            return _IMAGE_EXTENSION_MEDIA_TYPES[ext]
    return "image/png"


def _tool_result_to_anthropic(
    block: ToolResultBlock,
    tool_map: ToolIdMap,
    workspace_files: WorkspaceFileAPI | None = None,
) -> ToolResultBlockParam:
    provider_id = tool_map.to_provider(block.tool_use_id) or block.tool_use_id
    tool_map.remember(block.tool_use_id, provider_id)
    parts = []
    for inner in block.content:
        if isinstance(inner, TextBlock):
            parts.append({"type": "text", "text": inner.text})
        elif isinstance(inner, ImageBlock):
            converted = _image_to_anthropic(inner, workspace_files)
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


# ---- Batch helpers ---------------------------------------------------------


def _ensure_provider_matches(handle: BatchHandle, expected: str) -> None:
    """Defense against accidentally calling the wrong adapter on a handle.

    `BatchHandle.provider` is set by `submit_batch`; the caller persists
    the handle and may later route to the wrong adapter if the model
    registry resolves differently. Catch that early.
    """
    if handle.provider != expected:
        raise InvalidRequestError(
            f"BatchHandle for provider {handle.provider!r} cannot be processed by "
            f"adapter {expected!r}",
            request_id=handle.batch_id,
        )


def _classify_batch_status(batch, request_count: int) -> BatchStatus:
    """Map Anthropic's `MessageBatch` to the canonical BatchStatus literal.

    Anthropic exposes:
      - `processing_status: Literal["in_progress", "canceling", "ended"]`
      - `request_counts: {processing, succeeded, errored, canceled, expired}`

    The mapping:
      - `processing_status == "in_progress"` → `"in_progress"`
      - `processing_status == "canceling"` → `"in_progress"` (still running)
      - `processing_status == "ended"`:
          - all `expired` → `"expired"`
          - all `errored` (no succeeded) → `"failed"`
          - otherwise → `"completed"` (mixed results are still completed;
            per-row classification surfaces individual `BatchError`s)
    """
    status = getattr(batch, "processing_status", None)
    if status == "in_progress" or status == "canceling":
        # Submitted but not yet observable at the queued-vs-running level;
        # we use "in_progress" for both since Anthropic doesn't expose a
        # distinct queued state in the v1 API.
        return "in_progress"
    if status != "ended":
        # Unknown future status — best-effort downgrade.
        return "in_progress"

    counts = getattr(batch, "request_counts", None)
    expired = getattr(counts, "expired", 0) if counts else 0
    errored = getattr(counts, "errored", 0) if counts else 0
    succeeded = getattr(counts, "succeeded", 0) if counts else 0

    if request_count > 0 and expired == request_count:
        return "expired"
    if request_count > 0 and succeeded == 0 and errored == request_count:
        return "failed"
    return "completed"


def _request_model_lookup(handle: BatchHandle):
    """Return a callable mapping `custom_id` -> model string.

    The adapter doesn't keep request bodies around after submission, but
    the response carries enough metadata for cost stamping. We default to
    `None` here; the per-row translator falls back to reading
    `message.model` from each result row.
    """
    del handle  # unused — kept for forward-compat with a richer handle.
    return None


def translated_custom_id(result: CanonicalResponse | BatchError) -> str:
    """Return the custom_id (== request_id) for a translated batch row."""
    if isinstance(result, BatchError):
        return result.custom_id
    return result.request_id


def _translate_batch_row(row, *, model_lookup) -> CanonicalResponse | BatchError:
    """Translate one `MessageBatchIndividualResponse` to a canonical row.

    The SDK returns:
        row.custom_id: str
        row.result: succeeded | errored | expired | canceled (tagged union)
    """
    del model_lookup  # reserved for future use
    custom_id = getattr(row, "custom_id", "")
    result = getattr(row, "result", None)
    rtype = getattr(result, "type", None)

    if rtype == "succeeded":
        message = getattr(result, "message", None)
        return _succeeded_row_to_canonical(custom_id, message)
    if rtype == "errored":
        err = getattr(result, "error", None)
        return _errored_row_to_batch_error(custom_id, err)
    if rtype == "expired":
        return BatchError(
            custom_id=custom_id,
            error_class=ErrorClass.SERVER_ERROR,
            error_message="batch entry expired before completion",
            retryable=True,
        )
    if rtype == "canceled":
        return BatchError(
            custom_id=custom_id,
            error_class=ErrorClass.CANCELLED,
            error_message="batch entry cancelled",
            retryable=False,
        )
    # Unknown future row type — surface as transient.
    return BatchError(
        custom_id=custom_id,
        error_class=ErrorClass.OTHER,
        error_message=f"unknown batch result type {rtype!r}",
        retryable=False,
    )


def _succeeded_row_to_canonical(custom_id: str, message) -> CanonicalResponse:
    """Build a `CanonicalResponse` for a successful batch entry.

    The `message` body is the same shape as a sync `Messages.create`
    response, so we reuse `_anthropic_blocks_to_canonical` and the
    `usage` mapping. `pricing_mode='batch'` is stamped on the
    `TokenUsage` so the caller's later `PriceTable.compute_cost`
    selects `ModelPricing.batch_rates`.

    `latency_ms` is set to 0 — batch entries don't have a meaningful
    per-request latency. Callers that care about wall-clock spend can
    derive it from the BatchHandle's submission timestamp.
    """
    tool_map = ToolIdMap()  # fresh; no carry-over within a batch
    content = _anthropic_blocks_to_canonical(getattr(message, "content", []) or [], tool_map)
    usage_obj = getattr(message, "usage", None)
    usage = TokenUsage(
        input_tokens=getattr(usage_obj, "input_tokens", 0) or 0,
        output_tokens=getattr(usage_obj, "output_tokens", 0) or 0,
        cached_input_tokens=(getattr(usage_obj, "cache_read_input_tokens", 0) or 0)
        if usage_obj
        else 0,
        cache_creation_input_tokens=(getattr(usage_obj, "cache_creation_input_tokens", 0) or 0)
        if usage_obj
        else 0,
        pricing_mode="batch",
    )
    raw_model = getattr(message, "model", "") or ""
    canonical_model = _canonical_model_from_wire(raw_model)
    return CanonicalResponse(
        request_id=custom_id,
        model=canonical_model,
        provider="anthropic",
        content=content,
        stop_reason=_stop_reason(getattr(message, "stop_reason", None)),
        usage=usage,
        latency_ms=0,
    )


def _errored_row_to_batch_error(custom_id: str, err) -> BatchError:
    """Map an `errored`-typed row to a BatchError.

    Anthropic's per-row errors carry `{type, error: {type, message}}`. We
    map the inner `error.type` to `ErrorClass` heuristically — the same
    mapping the sync `classify_anthropic_response` uses for response
    bodies. `retryable` is set conservatively: True for rate-limit / 5xx
    / overload, False for invalid_request / auth.
    """
    if err is None:
        return BatchError(
            custom_id=custom_id,
            error_class=ErrorClass.OTHER,
            error_message="upstream returned errored row with no detail",
            retryable=False,
        )
    inner = getattr(err, "error", None)
    msg = getattr(inner, "message", "") or getattr(err, "message", "") or "unknown error"
    err_type = (getattr(inner, "type", "") or getattr(err, "type", "") or "").lower()

    if "rate_limit" in err_type or "overload" in err_type:
        return BatchError(
            custom_id=custom_id,
            error_class=ErrorClass.RATE_LIMIT,
            error_message=msg,
            retryable=True,
        )
    if "authentication" in err_type or "permission" in err_type or "auth" in err_type:
        return BatchError(
            custom_id=custom_id,
            error_class=ErrorClass.AUTH,
            error_message=msg,
            retryable=False,
        )
    if "not_found" in err_type or "invalid" in err_type:
        return BatchError(
            custom_id=custom_id,
            error_class=ErrorClass.INVALID_REQUEST,
            error_message=msg,
            retryable=False,
        )
    if "api_error" in err_type or "server" in err_type or "5" in err_type[:3]:
        return BatchError(
            custom_id=custom_id,
            error_class=ErrorClass.SERVER_ERROR,
            error_message=msg,
            retryable=True,
        )
    return BatchError(
        custom_id=custom_id,
        error_class=ErrorClass.OTHER,
        error_message=msg,
        retryable=False,
    )


def _canonical_model_from_wire(wire_model: str) -> str:
    """Rebuild the canonical `anthropic:<name>` id from a wire model.

    Inverse of `_wire_model_name`. Empty input → empty string (lets the
    caller stamp a fallback).
    """
    if not wire_model:
        return ""
    if ":" in wire_model:
        return wire_model
    return f"anthropic:{wire_model}"


__all__ = ["AnthropicAdapter"]
