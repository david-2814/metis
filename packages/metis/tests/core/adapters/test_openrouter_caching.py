"""Tests for OpenRouter prompt-caching support.

Covers provider-adapter-contract.md §4.5: per-model caching-capability
detection from the `/api/v1/models` pricing block, `cache_control` breakpoint
injection for explicit-breakpoint upstreams (Anthropic) vs. none for
implicit-cache upstreams (OpenAI / DeepSeek), cache-write pricing parse,
cache-token usage readback, and the `provider` routing object.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from metis.core.adapters.openai import _OpenAIStreamAccumulator, _usage_to_canonical
from metis.core.adapters.openrouter import (
    EXPLICIT_BREAKPOINT_FAMILIES,
    OpenRouterAdapter,
    _parse_capabilities,
    _parse_pricing,
)
from metis.core.adapters.protocol import CanonicalRequest
from metis.core.adapters.tool_id_map import ToolIdMap
from metis.core.canonical.content import TextBlock
from metis.core.canonical.messages import Message, MessageMetadata, Role

# ---- Fakes -------------------------------------------------------------


def _fake_text_response(text: str = "ok", usage=None):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content=text, tool_calls=None),
            )
        ],
        usage=usage
        or SimpleNamespace(
            prompt_tokens=10,
            completion_tokens=5,
            prompt_tokens_details=SimpleNamespace(cached_tokens=0),
        ),
    )


class _FakeCompletions:
    """Non-streaming fake: records the kwargs of each `create` call."""

    def __init__(self, responses=None) -> None:
        self.responses = list(responses or [])
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0) if self.responses else _fake_text_response()


async def _empty_stream():
    """An async generator that yields nothing — a degenerate SSE stream."""
    return
    yield  # pragma: no cover - makes this an async generator


class _FakeStreamCompletions:
    """Streaming fake: `create` records kwargs and returns an empty stream."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return _empty_stream()


class _FakeChat:
    def __init__(self, completions) -> None:
        self.completions = completions


class _FakeClient:
    def __init__(self, completions) -> None:
        self.chat = _FakeChat(completions)

    async def close(self):
        return


def _catalog_payload() -> dict:
    """A three-model catalog: an Anthropic explicit-breakpoint upstream, an
    OpenAI implicit-cache upstream (cache-read priced but not on the family
    allowlist), and a DeepSeek model with no cache pricing at all."""
    return {
        "data": [
            {
                "id": "anthropic/claude-haiku-4.5",
                "context_length": 200_000,
                "architecture": {"input_modalities": ["text", "image"]},
                "supported_parameters": ["tools", "response_format"],
                "top_provider": {"context_length": 200_000, "max_completion_tokens": 8192},
                "pricing": {
                    "prompt": "0.000001",
                    "completion": "0.000005",
                    "input_cache_read": "0.0000001",
                    "input_cache_write": "0.00000125",
                },
            },
            {
                "id": "openai/gpt-4o-mini",
                "context_length": 128_000,
                "architecture": {"input_modalities": ["text"]},
                "supported_parameters": ["tools"],
                "top_provider": {"context_length": 128_000, "max_completion_tokens": 16384},
                "pricing": {
                    "prompt": "0.00000015",
                    "completion": "0.0000006",
                    "input_cache_read": "0.000000075",
                },
            },
            {
                "id": "deepseek/deepseek-chat",
                "context_length": 64_000,
                "architecture": {"input_modalities": ["text"]},
                "supported_parameters": ["tools"],
                "top_provider": {"context_length": 64_000, "max_completion_tokens": 8192},
                "pricing": {"prompt": "0.0000003", "completion": "0.0000011"},
            },
        ]
    }


async def _adapter_with_catalog(client) -> OpenRouterAdapter:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_catalog_payload())

    adapter = OpenRouterAdapter(api_key="k", client=client)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await adapter.fetch_catalog(http_client=http)
    return adapter


def _request(
    model: str,
    *,
    system_prompt: str | None = None,
    system_prompt_volatile: str | None = None,
) -> CanonicalRequest:
    return CanonicalRequest(
        request_id="req_cache",
        messages=[
            Message(
                id="01HZ",
                session_id="s",
                role=Role.USER,
                content=[TextBlock(text="hi")],
                created_at=datetime.datetime.now(datetime.UTC),
                metadata=MessageMetadata(),
            )
        ],
        tools=[],
        system_prompt=system_prompt,
        model=model,
        max_output_tokens=128,
        system_prompt_volatile=system_prompt_volatile,
        tool_id_map=ToolIdMap(),
    )


# ---- Capability detection (§4.5.2) -------------------------------------


def test_parse_capabilities_detects_caching_from_cache_read_pricing():
    entry = {
        "id": "anthropic/claude-haiku-4.5",
        "architecture": {"input_modalities": ["text"]},
        "supported_parameters": ["tools"],
        "pricing": {
            "prompt": "0.000001",
            "completion": "0.000005",
            "input_cache_read": "0.0000001",
        },
    }
    assert _parse_capabilities(entry).supports_prompt_caching is True


def test_parse_capabilities_no_caching_without_cache_read_pricing():
    entry = {
        "id": "deepseek/deepseek-chat",
        "architecture": {"input_modalities": ["text"]},
        "supported_parameters": ["tools"],
        "pricing": {"prompt": "0.0000003", "completion": "0.0000011"},
    }
    assert _parse_capabilities(entry).supports_prompt_caching is False


def test_parse_capabilities_no_caching_when_pricing_block_absent():
    entry = {"id": "tiny/foo", "architecture": {"input_modalities": ["text"]}}
    assert _parse_capabilities(entry).supports_prompt_caching is False


async def test_fetch_catalog_sets_prompt_caching_capability_per_model():
    adapter = await _adapter_with_catalog(_FakeClient(_FakeCompletions()))
    assert adapter.capabilities_for("openrouter:anthropic/claude-haiku-4.5").supports_prompt_caching
    assert adapter.capabilities_for("openrouter:openai/gpt-4o-mini").supports_prompt_caching
    assert not adapter.capabilities_for("openrouter:deepseek/deepseek-chat").supports_prompt_caching


# ---- Pricing parse (§4.5.2 rule 2) -------------------------------------


def test_parse_pricing_reads_cache_write_rate():
    entry = {
        "pricing": {
            "prompt": "0.000003",
            "completion": "0.000015",
            "input_cache_read": "0.0000003",
            "input_cache_write": "0.00000375",  # $3.75 / Mtok
        }
    }
    priced = _parse_pricing(entry)
    assert priced is not None
    assert priced.cached_read_per_mtok == Decimal("0.3")
    assert priced.cache_creation_per_mtok == Decimal("3.75")


def test_parse_pricing_cache_write_absent_defaults_to_zero():
    entry = {"pricing": {"prompt": "0.000003", "completion": "0.000015"}}
    priced = _parse_pricing(entry)
    assert priced is not None
    assert priced.cache_creation_per_mtok == Decimal("0")


# ---- Breakpoint injection (§4.5.3) -------------------------------------


async def test_breakpoint_injected_for_anthropic_upstream():
    fake = _FakeCompletions()
    adapter = await _adapter_with_catalog(_FakeClient(fake))
    await adapter.complete(
        _request("openrouter:anthropic/claude-haiku-4.5", system_prompt="STABLE SYSTEM")
    )
    system_msg = fake.calls[0]["messages"][0]
    assert system_msg["role"] == "system"
    content = system_msg["content"]
    assert isinstance(content, list)
    assert content[0]["text"] == "STABLE SYSTEM"
    assert content[0]["cache_control"] == {"type": "ephemeral"}


async def test_no_breakpoint_for_openai_upstream():
    """OpenAI via OpenRouter caches implicitly — it is cache-read priced
    (`supports_prompt_caching=True`) but not on the breakpoint allowlist, so
    it must NOT get a `cache_control` marker."""
    fake = _FakeCompletions()
    adapter = await _adapter_with_catalog(_FakeClient(fake))
    await adapter.complete(_request("openrouter:openai/gpt-4o-mini", system_prompt="STABLE SYSTEM"))
    system_msg = fake.calls[0]["messages"][0]
    assert system_msg["content"] == "STABLE SYSTEM"  # plain string, no parts
    assert "cache_control" not in repr(fake.calls[0]["messages"])


async def test_no_breakpoint_for_deepseek_upstream():
    fake = _FakeCompletions()
    adapter = await _adapter_with_catalog(_FakeClient(fake))
    await adapter.complete(
        _request("openrouter:deepseek/deepseek-chat", system_prompt="STABLE SYSTEM")
    )
    assert fake.calls[0]["messages"][0]["content"] == "STABLE SYSTEM"


async def test_breakpoint_splits_stable_and_volatile_segments():
    """The breakpoint sits on the stable part only — per-turn volatile
    mutations must not churn the cached prefix (§4.5.3)."""
    fake = _FakeCompletions()
    adapter = await _adapter_with_catalog(_FakeClient(fake))
    await adapter.complete(
        _request(
            "openrouter:anthropic/claude-haiku-4.5",
            system_prompt="STABLE",
            system_prompt_volatile="VOLATILE",
        )
    )
    content = fake.calls[0]["messages"][0]["content"]
    assert content == [
        {"type": "text", "text": "STABLE", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "VOLATILE"},
    ]


async def test_no_breakpoint_without_catalog():
    """`_wants_cache_breakpoint` gates on the catalog-derived capability —
    without a catalog fetch the adapter cannot confirm `input_cache_read`,
    so it conservatively emits no breakpoints."""
    fake = _FakeCompletions()
    adapter = OpenRouterAdapter(api_key="k", client=_FakeClient(fake))
    await adapter.complete(
        _request("openrouter:anthropic/claude-haiku-4.5", system_prompt="STABLE SYSTEM")
    )
    assert fake.calls[0]["messages"][0]["content"] == "STABLE SYSTEM"


async def test_breakpoint_injected_on_streaming_path():
    fake = _FakeStreamCompletions()
    adapter = await _adapter_with_catalog(_FakeClient(fake))
    req = _request("openrouter:anthropic/claude-haiku-4.5", system_prompt="STABLE SYSTEM")
    async for _ in adapter.stream(req):
        pass
    content = fake.calls[0]["messages"][0]["content"]
    assert content[0]["cache_control"] == {"type": "ephemeral"}


# ---- Provider routing object (§4.5.5) ----------------------------------


async def test_provider_routing_object_on_complete():
    fake = _FakeCompletions()
    adapter = await _adapter_with_catalog(_FakeClient(fake))
    await adapter.complete(_request("openrouter:anthropic/claude-haiku-4.5"))
    assert fake.calls[0]["extra_body"] == {"provider": {"allow_fallbacks": True}}


async def test_provider_routing_object_on_stream():
    fake = _FakeStreamCompletions()
    adapter = await _adapter_with_catalog(_FakeClient(fake))
    async for _ in adapter.stream(_request("openrouter:anthropic/claude-haiku-4.5")):
        pass
    assert fake.calls[0]["extra_body"] == {"provider": {"allow_fallbacks": True}}


# ---- Cache-token usage readback (§4.5.4) -------------------------------


def test_usage_readback_maps_cache_tokens():
    # Warm hit: cached_tokens > 0, no write.
    warm = SimpleNamespace(
        prompt_tokens=8000,
        completion_tokens=42,
        prompt_tokens_details=SimpleNamespace(cached_tokens=7800, cache_write_tokens=0),
    )
    out = _usage_to_canonical(warm)
    assert out.cached_input_tokens == 7800
    assert out.cache_creation_input_tokens == 0
    # prompt_tokens (8000) is the total; input_tokens is the uncached remainder.
    assert out.input_tokens == 200

    # Cold call that establishes the cache: cache_write_tokens > 0.
    cold = SimpleNamespace(
        prompt_tokens=8000,
        completion_tokens=42,
        prompt_tokens_details=SimpleNamespace(cached_tokens=0, cache_write_tokens=7800),
    )
    out_cold = _usage_to_canonical(cold)
    assert out_cold.cached_input_tokens == 0
    assert out_cold.cache_creation_input_tokens == 7800
    # The written span is also excluded from input_tokens.
    assert out_cold.input_tokens == 200


async def test_complete_surfaces_cache_creation_tokens():
    usage = SimpleNamespace(
        prompt_tokens=5000,
        completion_tokens=20,
        prompt_tokens_details=SimpleNamespace(cached_tokens=0, cache_write_tokens=4800),
    )
    fake = _FakeCompletions(responses=[_fake_text_response(usage=usage)])
    adapter = await _adapter_with_catalog(_FakeClient(fake))
    response = await adapter.complete(_request("openrouter:anthropic/claude-haiku-4.5"))
    assert response.usage.cache_creation_input_tokens == 4800


def test_stream_accumulator_reads_cache_write_tokens():
    acc = _OpenAIStreamAccumulator(message_id="m", tool_map=ToolIdMap())
    usage_chunk = SimpleNamespace(
        choices=[],
        usage=SimpleNamespace(
            prompt_tokens=200,
            completion_tokens=10,
            prompt_tokens_details=SimpleNamespace(cached_tokens=50, cache_write_tokens=150),
        ),
    )
    acc.consume(usage_chunk)
    usage = acc.usage()
    assert usage.cached_input_tokens == 50
    assert usage.cache_creation_input_tokens == 150
    # prompt_tokens 200 = 50 cached + 150 written + 0 uncached.
    assert usage.input_tokens == 0


# ---- Family allowlist --------------------------------------------------


def test_explicit_breakpoint_families_are_prefixes():
    assert "anthropic/" in EXPLICIT_BREAKPOINT_FAMILIES
    assert all(fam.endswith("/") for fam in EXPLICIT_BREAKPOINT_FAMILIES)


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("openrouter:anthropic/claude-haiku-4.5", True),
        ("openrouter:google/gemini-2.5-flash", False),  # not in catalog → no capability
        ("openrouter:openai/gpt-4o-mini", False),
        ("openrouter:deepseek/deepseek-chat", False),
    ],
)
async def test_wants_cache_breakpoint_gates_on_family_and_capability(model, expected):
    adapter = await _adapter_with_catalog(_FakeClient(_FakeCompletions()))
    assert adapter._wants_cache_breakpoint(model) is expected
