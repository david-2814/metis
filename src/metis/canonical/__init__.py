"""Canonical message format types.

See docs/specs/canonical-message-format.md for the full specification.
"""

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
from metis.canonical.ids import new_message_id, new_session_id, new_tool_use_id
from metis.canonical.messages import (
    Message,
    MessageMetadata,
    MessageStatus,
    Role,
    RoutingDecisionRecord,
    RoutingMode,
    Usage,
)
from metis.canonical.tools import (
    SideEffects,
    ToolDefinition,
    ToolSchemaError,
    validate_tool_input_schema,
)
from metis.canonical.validation import validate_message

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
    "new_message_id",
    "new_session_id",
    "new_tool_use_id",
    "validate_message",
    "validate_tool_input_schema",
]
