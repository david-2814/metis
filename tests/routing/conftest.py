"""Shared fixtures: a stub adapter and a populated model registry."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from metis.canonical.capabilities import AdapterCapabilities
from metis.routing.registry import ModelRegistry


@dataclass
class StubAdapter:
    """Minimal stand-in for ProviderAdapter for routing tests."""

    name: str = "anthropic"
    caps_map: dict[str, AdapterCapabilities] | None = None

    def capabilities_for(self, model: str) -> AdapterCapabilities:
        return (self.caps_map or {})[model]

    async def complete(self, request):  # pragma: no cover — not exercised here
        raise NotImplementedError

    async def cancel(self, request_id: str) -> bool:  # pragma: no cover
        return False

    async def close(self) -> None:  # pragma: no cover
        return

    def estimate_input_tokens(self, *args, **kwargs) -> int:  # pragma: no cover
        return 0

    def stream(self, request):  # pragma: no cover
        raise NotImplementedError


def _caps(**overrides) -> AdapterCapabilities:
    base = dict(
        supports_thinking=False,
        supports_images=True,
        supports_tools=True,
        supports_system_prompt=True,
        supports_structured_output=False,
        supports_streaming=True,
        supports_streaming_tool_calls=True,
        supports_parallel_tool_calls=True,
        supports_prompt_caching=False,
        supports_system_messages_in_list=False,
        max_context_tokens=200_000,
        max_output_tokens=8192,
        accepted_image_media_types=["image/png", "image/jpeg"],
    )
    base.update(overrides)
    return AdapterCapabilities(**base)


@pytest.fixture
def caps_factory():
    return _caps


@pytest.fixture
def registry() -> ModelRegistry:
    """Registry with three Anthropic models and one OpenAI text-only model."""
    anth_caps = {
        "anthropic:claude-opus-4-7": _caps(),
        "anthropic:claude-sonnet-4-6": _caps(),
        "anthropic:claude-haiku-4-5": _caps(max_context_tokens=200_000),
    }
    openai_caps = {
        "openai:gpt-text-only": _caps(supports_images=False, supports_tools=False),
    }
    anthropic_adapter = StubAdapter(name="anthropic", caps_map=anth_caps)
    openai_adapter = StubAdapter(name="openai", caps_map=openai_caps)

    reg = ModelRegistry()
    reg.register(
        model_id="anthropic:claude-opus-4-7",
        adapter=anthropic_adapter,
        aliases=["opus", "deep"],
    )
    reg.register(
        model_id="anthropic:claude-sonnet-4-6",
        adapter=anthropic_adapter,
        aliases=["sonnet", "balanced"],
    )
    reg.register(
        model_id="anthropic:claude-haiku-4-5",
        adapter=anthropic_adapter,
        aliases=["haiku", "fast"],
    )
    reg.register(
        model_id="openai:gpt-text-only",
        adapter=openai_adapter,
        aliases=["gpt-text"],
    )
    return reg
