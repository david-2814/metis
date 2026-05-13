"""Message-level invariants from canonical-message-format.md §5.

Validators return a list of error strings; empty list means valid. They never
raise. Callers decide whether to log, fail-loud, or surface to the user.
"""

from __future__ import annotations

from metis_core.canonical.content import (
    ImageBlock,
    RedactedThinkingBlock,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from metis_core.canonical.messages import Message, MessageStatus, Role

_USER_ALLOWED = (TextBlock, ImageBlock)
_ASSISTANT_ALLOWED = (TextBlock, ToolUseBlock, ThinkingBlock, RedactedThinkingBlock)
_TOOL_ALLOWED = (ToolResultBlock,)
_SYSTEM_ALLOWED = (TextBlock,)


def validate_message(message: Message) -> list[str]:
    """Validate a Message against the canonical invariants.

    Per §5.1.5: PARTIAL messages skip validation (a streaming message in
    progress may have empty content, malformed tool input, etc).
    """
    if message.metadata.status == MessageStatus.PARTIAL:
        return []

    errors: list[str] = []
    _check_content_shape(message, errors)
    _check_metadata(message, errors)
    return errors


def _check_content_shape(message: Message, errors: list[str]) -> None:
    role = message.role
    content = message.content

    # §5.1.1, §5.1.2: non-empty for non-SYSTEM; SYSTEM may be empty.
    if role != Role.SYSTEM and not content:
        errors.append(f"{role}: content must be non-empty")

    # §5.1.3: role-content compatibility.
    allowed = _allowed_blocks_for(role)
    for i, block in enumerate(content):
        if not isinstance(block, allowed):
            names = [t.__name__ for t in allowed]
            errors.append(
                f"{role}.content[{i}]: {type(block).__name__} not allowed for {role} "
                f"(allowed: {names})"
            )

    # TOOL messages: exactly one block.
    if role == Role.TOOL and len(content) != 1:
        errors.append(f"TOOL message must have exactly one content block, got {len(content)}")


def _allowed_blocks_for(role: Role) -> tuple[type, ...]:
    if role == Role.USER:
        return _USER_ALLOWED
    if role == Role.ASSISTANT:
        return _ASSISTANT_ALLOWED
    if role == Role.TOOL:
        return _TOOL_ALLOWED
    if role == Role.SYSTEM:
        return _SYSTEM_ALLOWED
    return ()


def _check_metadata(message: Message, errors: list[str]) -> None:
    md = message.metadata
    # §5.3: COMPLETE ASSISTANT messages require model/provider/routing/usage.
    if message.role == Role.ASSISTANT and md.status == MessageStatus.COMPLETE:
        if md.model is None:
            errors.append("ASSISTANT.metadata.model: required at status=complete")
        if md.provider is None:
            errors.append("ASSISTANT.metadata.provider: required at status=complete")
        if md.routing is None:
            errors.append("ASSISTANT.metadata.routing: required at status=complete")
        if md.usage is None:
            errors.append("ASSISTANT.metadata.usage: required at status=complete")

    # §5.3.4: TOOL messages always carry parent_tool_use_id.
    if message.role == Role.TOOL and md.parent_tool_use_id is None:
        errors.append("TOOL.metadata.parent_tool_use_id: required")
