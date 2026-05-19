"""Tests for AdapterCapabilities."""

from __future__ import annotations

import msgspec
from metis.core.canonical.capabilities import AdapterCapabilities


def _full_caps() -> AdapterCapabilities:
    return AdapterCapabilities(
        supports_thinking=True,
        supports_images=True,
        supports_tools=True,
        supports_system_prompt=True,
        supports_structured_output=False,
        supports_streaming=True,
        supports_streaming_tool_calls=True,
        supports_parallel_tool_calls=True,
        supports_prompt_caching=True,
        supports_system_messages_in_list=False,
        max_context_tokens=200_000,
        max_output_tokens=8192,
        accepted_image_media_types=["image/png", "image/jpeg", "image/gif", "image/webp"],
    )


def test_capabilities_construct_and_roundtrip():
    caps = _full_caps()
    encoded = msgspec.json.encode(caps)
    decoded = msgspec.json.decode(encoded, type=AdapterCapabilities)
    assert decoded == caps


def test_capabilities_image_media_types_defaults_empty():
    caps = AdapterCapabilities(
        supports_thinking=False,
        supports_images=False,
        supports_tools=True,
        supports_system_prompt=True,
        supports_structured_output=False,
        supports_streaming=True,
        supports_streaming_tool_calls=True,
        supports_parallel_tool_calls=False,
        supports_prompt_caching=False,
        supports_system_messages_in_list=True,
        max_context_tokens=128_000,
        max_output_tokens=4096,
    )
    assert caps.accepted_image_media_types == []
