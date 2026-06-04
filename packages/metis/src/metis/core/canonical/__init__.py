"""Canonical message format types.

See docs/specs/canonical-message-format.md for the full specification.
"""

# Pre-prime `metis.core.adapters` so its `__init__.py` finishes (and
# `adapters.protocol` finishes its own `from canonical.batch import ...`)
# before `canonical.batch` runs its top-level `from adapters.errors
# import ErrorClass`. Without this nudge the cycle is order-dependent:
# when any caller imports `canonical.*` before `adapters.*`, Python
# starts `canonical.batch` first, which then tries to load
# `adapters.protocol` which loops back into the in-flight `canonical.batch`
# and `ImportError`s on `BatchError`. Wave 18a-1 introduced this
# dependency; this pre-import is the minimally-invasive break.
from metis.core import adapters as _adapters_preload  # noqa: F401
from metis.core.canonical.batch import BatchError, BatchHandle, BatchStatus
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
    decode_content_blocks_tolerant,
)
from metis.core.canonical.ids import new_message_id, new_session_id, new_tool_use_id
from metis.core.canonical.messages import (
    Message,
    MessageMetadata,
    MessageStatus,
    Role,
    RoutingDecisionRecord,
    RoutingMode,
    Usage,
)
from metis.core.canonical.tools import (
    SideEffects,
    ToolDefinition,
    ToolSchemaError,
    validate_tool_input_schema,
)
from metis.core.canonical.validation import validate_message

__all__ = [
    "AdapterCapabilities",
    "BatchError",
    "BatchHandle",
    "BatchStatus",
    "ContentBlock",
    "ImageBlock",
    "ImageSource",
    "Message",
    "MessageMetadata",
    "MessageStatus",
    "RedactedThinkingBlock",
    "Role",
    "RoutingDecisionRecord",
    "RoutingMode",
    "SideEffects",
    "TextBlock",
    "ThinkingBlock",
    "ToolDefinition",
    "ToolResultBlock",
    "ToolSchemaError",
    "ToolUseBlock",
    "Usage",
    "decode_content_blocks_tolerant",
    "new_message_id",
    "new_session_id",
    "new_tool_use_id",
    "validate_message",
    "validate_tool_input_schema",
]
