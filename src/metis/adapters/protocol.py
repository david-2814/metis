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

from metis.adapters.tool_id_map import ToolIdMap
from metis.canonical.capabilities import AdapterCapabilities
from metis.canonical.content import ContentBlock
from metis.canonical.messages import Message
from metis.canonical.tools import ToolDefinition


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


@dataclass
class StreamEvent:
    """Placeholder for streaming events; defined fully in streaming-protocol §5.

    Layer 3 only implements complete(); stream() returns NotImplementedError.
    """

    type: str
    payload: dict


class ProviderAdapter(Protocol):
    """Implemented by every provider adapter."""

    name: str

    async def complete(self, request: CanonicalRequest) -> CanonicalResponse: ...

    def stream(self, request: CanonicalRequest) -> AsyncIterator[StreamEvent]: ...

    def estimate_input_tokens(
        self,
        messages: list[Message],
        tools: list[ToolDefinition],
        system_prompt: str | None,
    ) -> int: ...

    async def cancel(self, request_id: str) -> bool: ...

    async def close(self) -> None: ...

    def capabilities_for(self, model: str) -> AdapterCapabilities: ...
