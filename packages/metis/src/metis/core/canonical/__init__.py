"""Canonical message format types.

See docs/specs/canonical-message-format.md for the full specification.
"""

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
