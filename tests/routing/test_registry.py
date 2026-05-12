"""Tests for ModelRegistry."""

from __future__ import annotations

import pytest

from metis.routing.registry import (
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
    from tests.routing.conftest import StubAdapter

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
    from tests.routing.conftest import StubAdapter

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
