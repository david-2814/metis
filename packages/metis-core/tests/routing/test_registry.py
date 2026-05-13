"""Tests for ModelRegistry."""

from __future__ import annotations

import pytest
from metis_core.routing.registry import (
    DuplicateAliasError,
    ModelRegistry,
    UnknownModelError,
)


def test_register_and_resolve_canonical(registry: ModelRegistry):
    assert registry.is_configured("anthropic:claude-sonnet-4-6")
    assert registry.resolve_alias("anthropic:claude-sonnet-4-6") == "anthropic:claude-sonnet-4-6"


def test_aliases_resolve_to_canonical(registry: ModelRegistry):
    assert registry.resolve_alias("sonnet") == "anthropic:claude-sonnet-4-6"
    assert registry.resolve_alias("balanced") == "anthropic:claude-sonnet-4-6"
    assert registry.resolve_alias("opus") == "anthropic:claude-opus-4-7"
    assert registry.resolve_alias("haiku") == "anthropic:claude-haiku-4-5"


def test_unknown_alias_returns_none(registry: ModelRegistry):
    assert registry.resolve_alias("nope") is None


def test_get_unknown_model_raises(registry: ModelRegistry):
    with pytest.raises(UnknownModelError):
        registry.get("anthropic:nonexistent")


def test_provider_of_strips_prefix(registry: ModelRegistry):
    assert registry.provider_of("anthropic:claude-sonnet-4-6") == "anthropic"
    assert registry.provider_of("openai:gpt-text-only") == "openai"
    # No prefix: return the whole string.
    assert registry.provider_of("local-model") == "local-model"


def test_duplicate_alias_across_models_rejected(caps_factory):
    from _helpers import StubAdapter

    reg = ModelRegistry()
    adapter = StubAdapter(
        caps_map={
            "anthropic:a": caps_factory(),
            "anthropic:b": caps_factory(),
        }
    )
    reg.register(model_id="anthropic:a", adapter=adapter, aliases=["x"])
    with pytest.raises(DuplicateAliasError):
        reg.register(model_id="anthropic:b", adapter=adapter, aliases=["x"])


def test_re_register_same_model_keeps_aliases(caps_factory):
    from _helpers import StubAdapter

    reg = ModelRegistry()
    adapter = StubAdapter(caps_map={"anthropic:a": caps_factory()})
    reg.register(model_id="anthropic:a", adapter=adapter, aliases=["x"])
    # Same model + same alias should be idempotent (alias maps to same id).
    reg.register(model_id="anthropic:a", adapter=adapter, aliases=["x"])
    assert reg.resolve_alias("x") == "anthropic:a"


def test_unregister_clears_aliases(registry: ModelRegistry):
    registry.unregister("anthropic:claude-sonnet-4-6")
    assert registry.is_configured("anthropic:claude-sonnet-4-6") is False
    assert registry.resolve_alias("sonnet") is None
    # Other models still present.
    assert registry.is_configured("anthropic:claude-opus-4-7")


def test_list_models_sorted(registry: ModelRegistry):
    models = registry.list_models()
    assert models == sorted(models)
    assert "anthropic:claude-opus-4-7" in models


# ---- task_profile ------------------------------------------------------


def test_register_with_task_profile(caps_factory):
    from _helpers import StubAdapter

    reg = ModelRegistry()
    adapter = StubAdapter(caps_map={"anthropic:a": caps_factory()})
    entry = reg.register(
        model_id="anthropic:a",
        adapter=adapter,
        aliases=["x"],
        task_profile=["deep-reasoning", "architecture"],
    )
    assert entry.task_profile == ("deep-reasoning", "architecture")
    assert reg.get("anthropic:a").task_profile == ("deep-reasoning", "architecture")


def test_register_without_task_profile_defaults_empty(caps_factory):
    from _helpers import StubAdapter

    reg = ModelRegistry()
    adapter = StubAdapter(caps_map={"anthropic:a": caps_factory()})
    entry = reg.register(model_id="anthropic:a", adapter=adapter)
    assert entry.task_profile == ()


# ---- find_by_suffix -----------------------------------------------------


def test_find_by_suffix_matches_after_colon_boundary(caps_factory):
    """The provider:rest boundary is a valid suffix anchor."""
    from _helpers import StubAdapter

    reg = ModelRegistry()
    adapter = StubAdapter(caps_map={"openai:gpt-5": caps_factory()})
    reg.register(model_id="openai:gpt-5", adapter=adapter)
    assert reg.find_by_suffix("gpt-5") == ["openai:gpt-5"]


def test_find_by_suffix_matches_after_slash_boundary(caps_factory):
    """OpenRouter namespace `/` is a valid suffix anchor."""
    from _helpers import StubAdapter

    reg = ModelRegistry()
    mid = "openrouter:openai/gpt-oss-20b"
    adapter = StubAdapter(caps_map={mid: caps_factory()})
    reg.register(model_id=mid, adapter=adapter)
    # Two valid suffix forms — the full namespaced name, and the bare leaf.
    assert reg.find_by_suffix("openai/gpt-oss-20b") == [mid]
    assert reg.find_by_suffix("gpt-oss-20b") == [mid]


def test_find_by_suffix_rejects_mid_name_substring(caps_factory):
    """Suffix must start at a boundary char — `t-5` is mid-name, not a tail."""
    from _helpers import StubAdapter

    reg = ModelRegistry()
    adapter = StubAdapter(caps_map={"openai:gpt-5": caps_factory()})
    reg.register(model_id="openai:gpt-5", adapter=adapter)
    assert reg.find_by_suffix("t-5") == []
    assert reg.find_by_suffix("pt-5") == []


def test_find_by_suffix_returns_all_matches_sorted(caps_factory):
    """Ambiguous suffix returns every candidate, sorted."""
    from _helpers import StubAdapter

    reg = ModelRegistry()
    ids = ["openai:gpt-5", "openrouter:openai/gpt-5"]
    adapter = StubAdapter(caps_map={mid: caps_factory() for mid in ids})
    for mid in ids:
        reg.register(model_id=mid, adapter=adapter)
    assert reg.find_by_suffix("gpt-5") == sorted(ids)


def test_find_by_suffix_exact_canonical_id_match(caps_factory):
    """Passing the whole canonical id matches it (boundary check is skipped
    when input length equals the id length)."""
    from _helpers import StubAdapter

    reg = ModelRegistry()
    adapter = StubAdapter(caps_map={"anthropic:claude-sonnet-4-6": caps_factory()})
    reg.register(model_id="anthropic:claude-sonnet-4-6", adapter=adapter)
    assert reg.find_by_suffix("anthropic:claude-sonnet-4-6") == ["anthropic:claude-sonnet-4-6"]


def test_find_by_suffix_empty_input_returns_empty(caps_factory):
    from _helpers import StubAdapter

    reg = ModelRegistry()
    adapter = StubAdapter(caps_map={"openai:gpt-5": caps_factory()})
    reg.register(model_id="openai:gpt-5", adapter=adapter)
    assert reg.find_by_suffix("") == []


def test_find_by_suffix_no_matches(caps_factory):
    from _helpers import StubAdapter

    reg = ModelRegistry()
    adapter = StubAdapter(caps_map={"openai:gpt-5": caps_factory()})
    reg.register(model_id="openai:gpt-5", adapter=adapter)
    assert reg.find_by_suffix("haiku") == []
