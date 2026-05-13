"""format_models_lines: nested tree by provider / namespace with pricing."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from metis_cli.models_display import format_models_lines
from metis_core.canonical.capabilities import AdapterCapabilities
from metis_core.pricing.table import ModelPricing, PriceTable
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


def _registry(
    models: list[tuple[str, list[str]]] | list[tuple[str, list[str], list[str]]],
) -> ModelRegistry:
    """Build a registry. Each tuple is ``(id, aliases)`` or
    ``(id, aliases, task_profile)``."""
    adapter = MagicMock()
    adapter.capabilities_for = lambda _m: _caps()
    reg = ModelRegistry()
    for entry in models:
        if len(entry) == 2:
            model_id, aliases = entry
            reg.register(model_id=model_id, adapter=adapter, aliases=aliases)
        else:
            model_id, aliases, task_profile = entry
            reg.register(
                model_id=model_id,
                adapter=adapter,
                aliases=aliases,
                task_profile=task_profile,
            )
    return reg


def _pricing(*entries: tuple[str, str, str]) -> PriceTable:
    """Build a PriceTable from (model_id, in_rate, out_rate)."""
    return PriceTable(
        version="test",
        models={
            mid: ModelPricing(input_per_mtok=Decimal(i), output_per_mtok=Decimal(o))
            for mid, i, o in entries
        },
    )


@pytest.fixture
def empty_pricing() -> PriceTable:
    return PriceTable(version="test", models={})


# ---- Empty input --------------------------------------------------------


def test_empty_model_list(empty_pricing):
    reg = _registry([])
    out = format_models_lines([], registry=reg, pricing=empty_pricing)
    assert out == ["  (no models match)"]


# ---- Single-level (provider-only) nesting -------------------------------


def test_native_models_grouped_under_provider_header():
    """Two anthropic models share one ``anthropic:`` header."""
    reg = _registry(
        [
            ("anthropic:claude-haiku-4-5", []),
            ("anthropic:claude-sonnet-4-6", []),
        ]
    )
    pricing = _pricing(
        ("anthropic:claude-haiku-4-5", "1.00", "5.00"),
        ("anthropic:claude-sonnet-4-6", "3.00", "15.00"),
    )
    lines = format_models_lines(
        ["anthropic:claude-haiku-4-5", "anthropic:claude-sonnet-4-6"],
        registry=reg,
        pricing=pricing,
    )
    assert lines[0] == "anthropic:"
    # Leaves are indented under the header. Names appear without the provider.
    leaf_lines = lines[1:]
    assert any("claude-haiku-4-5" in line for line in leaf_lines)
    assert any("claude-sonnet-4-6" in line for line in leaf_lines)
    # Provider prefix should NOT repeat on the leaf lines.
    assert not any(line.lstrip().startswith("anthropic:") for line in leaf_lines)


def test_multiple_providers_get_separate_headers():
    reg = _registry(
        [
            ("anthropic:claude-haiku-4-5", []),
            ("openai:gpt-5", []),
        ]
    )
    pricing = _pricing(
        ("anthropic:claude-haiku-4-5", "1.00", "5.00"),
        ("openai:gpt-5", "2.50", "10.00"),
    )
    lines = format_models_lines(
        ["anthropic:claude-haiku-4-5", "openai:gpt-5"],
        registry=reg,
        pricing=pricing,
    )
    # Provider headers appear in sorted order.
    assert "anthropic:" in lines
    assert "openai:" in lines
    assert lines.index("anthropic:") < lines.index("openai:")


# ---- Two-level (provider + sub-namespace) nesting -----------------------


def test_openrouter_nested_two_levels():
    """`openrouter:deepseek/...` produces nested headers: openrouter → deepseek."""
    reg = _registry(
        [
            ("openrouter:deepseek/deepseek-chat-v3.1", []),
        ]
    )
    pricing = _pricing(
        ("openrouter:deepseek/deepseek-chat-v3.1", "0.30", "0.90"),
    )
    lines = format_models_lines(
        ["openrouter:deepseek/deepseek-chat-v3.1"],
        registry=reg,
        pricing=pricing,
    )
    assert "openrouter:" in lines
    # The `deepseek:` sub-header is indented under openrouter.
    deepseek_header = next(line for line in lines if line.lstrip() == "deepseek:")
    assert deepseek_header.startswith("  ")  # one level of indent
    # Leaf is indented one more level beyond that and carries pricing.
    leaf = next(line for line in lines if "deepseek-chat-v3.1" in line)
    assert leaf.startswith("    ")  # two levels of indent (plus marker col)
    assert "$0.30 in" in leaf


def test_multiple_openrouter_subnamespaces_each_get_header():
    reg = _registry(
        [
            ("openrouter:anthropic/claude-opus-4.7", []),
            ("openrouter:deepseek/deepseek-chat-v3.1", []),
        ]
    )
    pricing = _pricing()
    lines = format_models_lines(
        [
            "openrouter:anthropic/claude-opus-4.7",
            "openrouter:deepseek/deepseek-chat-v3.1",
        ],
        registry=reg,
        pricing=pricing,
    )
    # One openrouter header, two sub-headers.
    assert lines.count("openrouter:") == 1
    sub_headers = [line.strip() for line in lines if line.lstrip() in ("anthropic:", "deepseek:")]
    assert "anthropic:" in sub_headers
    assert "deepseek:" in sub_headers


# ---- Pricing alignment across depths ------------------------------------


def test_pricing_aligned_across_nesting_depths():
    """The pricing column lines up even when leaves sit at different depths.

    A short leaf at depth 1 should have padding so its pricing starts at the
    same column as a long leaf at depth 2.
    """
    reg = _registry(
        [
            ("anthropic:claude-haiku-4-5", []),
            ("openrouter:deepseek/deepseek-chat-v3.1", []),
        ]
    )
    pricing = _pricing(
        ("anthropic:claude-haiku-4-5", "1.00", "5.00"),
        ("openrouter:deepseek/deepseek-chat-v3.1", "0.30", "0.90"),
    )
    lines = format_models_lines(
        ["anthropic:claude-haiku-4-5", "openrouter:deepseek/deepseek-chat-v3.1"],
        registry=reg,
        pricing=pricing,
    )
    haiku = next(line for line in lines if "claude-haiku-4-5" in line)
    deepseek = next(line for line in lines if "deepseek-chat-v3.1" in line)
    # Both leaves should report '$' at the same column.
    assert haiku.index("$") == deepseek.index("$")


# ---- Sticky marker ------------------------------------------------------


def test_sticky_leaf_marked_with_asterisk():
    reg = _registry(
        [
            ("anthropic:claude-haiku-4-5", []),
            ("anthropic:claude-sonnet-4-6", []),
        ]
    )
    pricing = _pricing()
    lines = format_models_lines(
        ["anthropic:claude-haiku-4-5", "anthropic:claude-sonnet-4-6"],
        registry=reg,
        pricing=pricing,
        sticky_model="anthropic:claude-haiku-4-5",
    )
    haiku = next(line for line in lines if "claude-haiku-4-5" in line)
    sonnet = next(line for line in lines if "claude-sonnet-4-6" in line)
    assert "*" in haiku
    assert "*" not in sonnet
    # Sticky and non-sticky leaves should still align — marker is in a fixed
    # 2-char column, so the leaf name itself starts at the same offset.
    assert haiku.index("claude-haiku-4-5") == sonnet.index("claude-sonnet-4-6")


def test_no_sticky_means_no_asterisk_anywhere():
    reg = _registry([("anthropic:claude-haiku-4-5", [])])
    pricing = _pricing(("anthropic:claude-haiku-4-5", "1.00", "5.00"))
    lines = format_models_lines(
        ["anthropic:claude-haiku-4-5"],
        registry=reg,
        pricing=pricing,
        sticky_model=None,
    )
    assert all("*" not in line for line in lines)


# ---- Pricing presence ---------------------------------------------------


def test_unpriced_model_shows_dash():
    reg = _registry([("openrouter:obscure/model", [])])
    pricing = _pricing()  # no pricing data
    lines = format_models_lines(["openrouter:obscure/model"], registry=reg, pricing=pricing)
    leaf = next(line for line in lines if "model" in line.lower() and ":" not in line.split()[0])
    assert "—" in leaf


def test_priced_model_shows_in_and_out_rates():
    reg = _registry([("anthropic:claude-sonnet-4-6", [])])
    pricing = _pricing(("anthropic:claude-sonnet-4-6", "3.00", "15.00"))
    lines = format_models_lines(["anthropic:claude-sonnet-4-6"], registry=reg, pricing=pricing)
    leaf = next(line for line in lines if "claude-sonnet-4-6" in line)
    assert "$3.00 in" in leaf
    assert "$15.00 out" in leaf
    assert "MTok" in leaf


# ---- No aliases column --------------------------------------------------


def test_aliases_not_displayed_anywhere():
    """The new format drops the alias column entirely."""
    reg = _registry([("anthropic:claude-sonnet-4-6", ["sonnet", "balanced"])])
    pricing = _pricing(("anthropic:claude-sonnet-4-6", "3.00", "15.00"))
    lines = format_models_lines(["anthropic:claude-sonnet-4-6"], registry=reg, pricing=pricing)
    joined = "\n".join(lines)
    assert "sonnet" not in joined.replace("claude-sonnet-4-6", "")
    assert "balanced" not in joined
