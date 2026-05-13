"""AdapterCapabilities — the substitutability contract.

See canonical-message-format.md §7.2 and provider-adapter-contract.md §3.4.

Declarations MUST be honest. If a model technically supports a feature but the
adapter implementation doesn't expose it, declare False.
"""

from __future__ import annotations

import msgspec


class AdapterCapabilities(msgspec.Struct, frozen=True):
    # Content type support
    supports_thinking: bool
    supports_images: bool
    supports_tools: bool
    supports_system_prompt: bool
    supports_structured_output: bool

    # Streaming
    supports_streaming: bool
    supports_streaming_tool_calls: bool
    supports_parallel_tool_calls: bool

    # Caching
    supports_prompt_caching: bool

    # System prompt placement quirk
    supports_system_messages_in_list: bool  # False = adapter hoists system out

    # Limits
    max_context_tokens: int
    max_output_tokens: int

    # Image format support (only meaningful if supports_images)
    accepted_image_media_types: list[str] = msgspec.field(default_factory=list)
