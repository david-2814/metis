"""Adapter Protocol + canonical request/response types.

See provider-adapter-contract.md §3.

This module deviates from the spec wording in one place: `CanonicalResponse`
returns `content: list[ContentBlock]` rather than a full `Message`. The
adapter doesn't know the routing decision (decided upstream) or the cost
(computed by the core from a price table per canonical-format §6.4), so it
returns the parts it knows and the caller assembles the final Message.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from metis.core.adapters.tool_id_map import ToolIdMap
from metis.core.canonical.capabilities import AdapterCapabilities
from metis.core.canonical.content import ContentBlock
from metis.core.canonical.messages import Message
from metis.core.canonical.tools import ToolDefinition


class StopReason(StrEnum):
    END_TURN = "end_turn"
    MAX_TOKENS = "max_tokens"
    STOP_SEQUENCE = "stop_sequence"
    TOOL_USE = "tool_use"
    CANCELLED = "cancelled"
    ERROR = "error"


@dataclass(frozen=True)
class TokenUsage:
    """Raw token counts. Cost computation is the core's responsibility per
    provider-adapter-contract.md §7."""

    input_tokens: int
    output_tokens: int
    cached_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass
class CanonicalRequest:
    """A model-agnostic LLM request.

    `tool_id_map` carries the per-session bidirectional id map (§6.2). The
    adapter reads and writes to it; callers (typically the session manager)
    own the lifecycle.

    `system_prompt` carries the *stable* portion of the system prompt
    (base persona, skill discovery index — content that doesn't change
    turn-to-turn within a session). `system_prompt_volatile` carries the
    *volatile* portion (`USER.md`, `MEMORY.md`, anything mutating). The
    split is load-bearing for prompt caching: see
    `docs/specs/context-assembler.md` §2-§3. Adapters concatenate the
    two segments stable-first when the provider doesn't expose
    breakpoints; for Anthropic the cache breakpoint sits between them.

    `workspace_path` is the absolute path of the session's workspace.
    Used by the adapter to resolve `ImageBlock(kind="file_ref")` payloads
    via `WorkspaceFileAPI` (workspace path security is load-bearing —
    don't bypass it). Optional: callers without a workspace context (or
    requests with no `file_ref` images) leave it None.
    """

    request_id: str  # ULID; passed to cancel()
    messages: list[Message]
    tools: list[ToolDefinition]
    system_prompt: str | None
    model: str  # canonical "provider:name"
    max_output_tokens: int
    stop_sequences: list[str] = field(default_factory=list)
    temperature: float | None = None
    output_schema: dict | None = None
    stream: bool = False
    tool_id_map: ToolIdMap | None = None
    system_prompt_volatile: str | None = None
    workspace_path: str | None = None


@dataclass
class CanonicalResponse:
    """The adapter's parsed response.

    The caller assembles a full canonical Message by combining `content`,
    routing context, and cost (from a price table)."""

    request_id: str
    model: str
    provider: str
    content: list[ContentBlock]
    stop_reason: StopReason
    usage: TokenUsage
    latency_ms: int


class ProviderAdapter(Protocol):
    """Implemented by every provider adapter."""

    name: str

    async def complete(self, request: CanonicalRequest) -> CanonicalResponse: ...

    def stream(self, request: CanonicalRequest) -> AsyncIterator[StreamingEvent]:
        """Translate provider chunks into canonical streaming events.

        The iterator yields events in the order defined in streaming-protocol
        §5.3 (MessageStart → deltas → ToolUseEnd → MessageComplete). The
        final MessageComplete carries the authoritative final content + usage.
        """
        ...

    def estimate_input_tokens(
        self,
        messages: list[Message],
        tools: list[ToolDefinition],
        system_prompt: str | None,
    ) -> int: ...

    async def cancel(self, request_id: str) -> bool: ...

    async def close(self) -> None: ...

    def capabilities_for(self, model: str) -> AdapterCapabilities: ...


# Forward-reference: import here to avoid a cycle (streaming imports protocol).
from metis.core.adapters.streaming import StreamingEvent  # noqa: E402
