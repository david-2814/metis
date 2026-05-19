"""Top-level Message type and its metadata.

See canonical-message-format.md §4.1, §4.3.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

import msgspec

from metis.core.canonical.content import ContentBlock

SCHEMA_VERSION = 1


class Role(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


class MessageStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"  # streaming in progress
    CANCELLED = "cancelled"  # user cancelled mid-generation
    ERROR = "error"  # generation failed


class RoutingMode(StrEnum):
    """Coarse summary of why this turn picked this model.

    The routing-engine chain enum is finer-grained; this projection is what
    persists on each assistant message. Mapping per canonical-message-format
    §4.3:

      Chain policy            -> RoutingMode
      PER_MESSAGE_OVERRIDE    -> OVERRIDE
      MANUAL_STICKY           -> MANUAL
      RULE                    -> RULE
      PATTERN                 -> PATTERN
      DELEGATE_REQUEST        -> DELEGATE
      WORKSPACE_DEFAULT       -> DEFAULT
      GLOBAL_DEFAULT          -> DEFAULT
    """

    OVERRIDE = "override"
    MANUAL = "manual"
    RULE = "rule"
    PATTERN = "pattern"
    DELEGATE = "delegate"
    DEFAULT = "default"


class RoutingDecisionRecord(msgspec.Struct, frozen=True):
    """Compact summary attached to an assistant message.

    Full chain trace lives in the corresponding route.decided event.
    """

    mode: RoutingMode
    chosen_model: str
    reason: str
    rule_name: str | None = None
    confidence: float | None = None
    alternatives_considered: list[str] = msgspec.field(default_factory=list)


class Usage(msgspec.Struct, frozen=True):
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal
    pricing_version: str
    latency_ms: int
    cached_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


class MessageMetadata(msgspec.Struct, frozen=True, eq=False):
    # Provenance
    model: str | None = None
    provider: str | None = None

    # Routing decision context (for ASSISTANT messages)
    routing: RoutingDecisionRecord | None = None

    # Resource accounting
    usage: Usage | None = None

    # Tool linkage (for TOOL messages)
    parent_tool_use_id: str | None = None

    # Status
    status: MessageStatus = MessageStatus.COMPLETE

    # Provider-specific opaque payload (round-trip aid; see §6.5).
    # Excluded from __eq__ / __hash__ per canonical-message-format.md §6.5.
    provider_raw: dict | None = None

    # Multi-user identity dimensions (multi-user.md §3, §4.4). Stable
    # principal ids resolved from the gateway key; `None` for agent-loop
    # traffic and pre-multi-user gateway keys.
    user_id: str | None = None
    team_id: str | None = None

    def _identity(self) -> tuple:
        return (
            self.model,
            self.provider,
            self.routing,
            self.usage,
            self.parent_tool_use_id,
            self.status,
            self.user_id,
            self.team_id,
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, MessageMetadata):
            return NotImplemented
        return self._identity() == other._identity()

    def __hash__(self) -> int:
        return hash(self._identity())


class Message(msgspec.Struct, frozen=True):
    id: str  # ULID, monotonic per session
    session_id: str
    role: Role
    content: list[ContentBlock]
    created_at: datetime  # microsecond precision UTC
    metadata: MessageMetadata = msgspec.field(default_factory=MessageMetadata)
    schema_version: int = SCHEMA_VERSION
