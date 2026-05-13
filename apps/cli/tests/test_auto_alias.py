"""Auto-aliasing for bulk-registered models (OpenRouter catalog)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from metis_cli.runtime import _auto_alias_candidates, _pick_auto_alias
from metis_core.canonical.capabilities import AdapterCapabilities
from metis_core.routing import ModelRegistry


def _caps() -> AdapterCapabilities:
    return AdapterCapabilities(
        supports_thinking=False,
        supports_images=False,
        supports_tools=True,
        supports_system_prompt=True,
        supports_structured_output=False,
        supports_streaming=True,
        supports_streaming_tool_calls=True,
        supports_parallel_tool_calls=True,
        supports_prompt_caching=False,
        supports_system_messages_in_list=False,
        max_context_tokens=100_000,
        max_output_tokens=4096,
    )


@pytest.fixture
def adapter():
    a = MagicMock()
    a.capabilities_for = lambda m: _caps()
    a.name = "stub"
    return a


# ---- Candidate generation ------------------------------------------------


def test_provider_slash_name_yields_two_candidates():
    """`openrouter:upstage/solar-pro-3` → shortened name first, provider-prefixed fallback."""
    assert _auto_alias_candidates("openrouter:upstage/solar-pro-3") == [
        "solar-pro-3",
        "upstage-solar-pro-3",
    ]


def test_no_slash_yields_single_candidate():
    """`anthropic:claude-haiku-4-5` → just the tail; no provider-prefixed form."""
    assert _auto_alias_candidates("anthropic:claude-haiku-4-5") == ["claude-haiku-4-5"]


def test_no_colon_yields_empty():
    """A model id without `:` is malformed; no auto-alias is generated."""
    assert _auto_alias_candidates("local-only-id") == []


def test_nested_slashes_flatten_to_hyphens():
    """Forward slashes in the tail are normalized to hyphens so the alias is
    a single shell token."""
    cands = _auto_alias_candidates("openrouter:google/gemini-pro/exp-0801")
    assert cands == [
        "gemini-pro-exp-0801",
        "google-gemini-pro-exp-0801",
    ]


# ---- Collision handling --------------------------------------------------


def test_picks_first_candidate_when_unused(adapter):
    reg = ModelRegistry()
    aliases = _pick_auto_alias("openrouter:upstage/solar-pro-3", reg)
    assert aliases == ["solar-pro-3"]


def test_falls_back_when_two_providers_share_tail(adapter):
    """If two OpenRouter providers ship a model with the same tail name, the
    second registration falls back to the provider-prefixed alias."""
    reg = ModelRegistry()
    # First registration: takes the short alias.
    aliases_a = _pick_auto_alias("openrouter:foo/llama-3", reg)
    reg.register(model_id="openrouter:foo/llama-3", adapter=adapter, aliases=aliases_a)
    assert aliases_a == ["llama-3"]
    # Second registration: short alias is taken, prefixed form picked.
    aliases_b = _pick_auto_alias("openrouter:bar/llama-3", reg)
    assert aliases_b == ["bar-llama-3"]


def test_falls_back_when_short_alias_collides_with_existing_model_id(adapter):
    """If a candidate string exactly equals an already-registered model id,
    it's treated as a collision and the fallback is chosen."""
    reg = ModelRegistry()
    # Register a model whose canonical id IS the short candidate string.
    reg.register(
        model_id="solar-pro-3",  # no colon, intentionally weird but possible
        adapter=adapter,
        aliases=[],
    )
    aliases = _pick_auto_alias("openrouter:upstage/solar-pro-3", reg)
    assert aliases == ["upstage-solar-pro-3"]


def test_falls_back_when_short_alias_collides_with_existing_alias(adapter):
    reg = ModelRegistry()
    reg.register(
        model_id="anthropic:claude-haiku-4-5",
        adapter=adapter,
        aliases=["solar-pro-3"],  # contrived but exercises the collision check
    )
    aliases = _pick_auto_alias("openrouter:upstage/solar-pro-3", reg)
    assert aliases == ["upstage-solar-pro-3"]


def test_empty_when_all_candidates_taken(adapter):
    reg = ModelRegistry()
    reg.register(
        model_id="anthropic:foo",
        adapter=adapter,
        aliases=["claude-sonnet-4-6"],
    )
    reg.register(
        model_id="anthropic:bar",
        adapter=adapter,
        aliases=["anthropic-claude-sonnet-4-6"],
    )
    aliases = _pick_auto_alias("openrouter:anthropic/claude-sonnet-4-6", reg)
    assert aliases == []


def test_real_world_openrouter_models(adapter):
    """Spot-check the user's reported model ids."""
    reg = ModelRegistry()
    assert _pick_auto_alias("openrouter:upstage/solar-pro-3", reg) == ["solar-pro-3"]
    reg.register(
        model_id="openrouter:upstage/solar-pro-3",
        adapter=adapter,
        aliases=["solar-pro-3"],
    )
    # A second model that doesn't collide:
    assert _pick_auto_alias("openrouter:writer/palmyra-x5", reg) == ["palmyra-x5"]


# ---- End-to-end registration via _pick_auto_alias -----------------------


def test_registry_round_trip(adapter):
    """The alias picked by `_pick_auto_alias` resolves back to the registered
    model id."""
    reg = ModelRegistry()
    aliases = _pick_auto_alias("openrouter:upstage/solar-pro-3", reg)
    reg.register(
        model_id="openrouter:upstage/solar-pro-3",
        adapter=adapter,
        aliases=aliases,
    )
    assert reg.resolve_alias("solar-pro-3") == "openrouter:upstage/solar-pro-3"
