"""Tests for the price table."""

from __future__ import annotations

from decimal import Decimal

import pytest
from metis_core.adapters.protocol import TokenUsage
from metis_core.pricing.table import (
    DEFAULT_PRICE_TABLE,
    ModelPricing,
    PriceTable,
    UnknownPricingModelError,
)


def test_default_table_has_claude_models():
    for model_id in (
        "anthropic:claude-opus-4-7",
        "anthropic:claude-sonnet-4-6",
        "anthropic:claude-haiku-4-5",
    ):
        assert model_id in DEFAULT_PRICE_TABLE


def test_compute_cost_basic_input_output():
    """1M input + 1M output @ sonnet rates = $3 + $15 = $18."""
    usage = TokenUsage(input_tokens=1_000_000, output_tokens=1_000_000)
    cost = DEFAULT_PRICE_TABLE.compute_cost("anthropic:claude-sonnet-4-6", usage)
    assert cost == Decimal("18.00")


def test_compute_cost_with_cached_tokens():
    usage = TokenUsage(
        input_tokens=1_000_000,
        output_tokens=0,
        cached_input_tokens=1_000_000,
        cache_creation_input_tokens=1_000_000,
    )
    # Sonnet: 3 + 0.30 + 3.75 = 7.05
    cost = DEFAULT_PRICE_TABLE.compute_cost("anthropic:claude-sonnet-4-6", usage)
    assert cost == Decimal("7.05")


def test_compute_cost_zero_usage():
    usage = TokenUsage(input_tokens=0, output_tokens=0)
    cost = DEFAULT_PRICE_TABLE.compute_cost("anthropic:claude-sonnet-4-6", usage)
    assert cost == Decimal("0")


def test_compute_cost_small_usage_no_float_drift():
    """100 input + 50 output @ haiku rates should be exact."""
    usage = TokenUsage(input_tokens=100, output_tokens=50)
    cost = DEFAULT_PRICE_TABLE.compute_cost("anthropic:claude-haiku-4-5", usage)
    # 100 * 1.00 / 1M + 50 * 5.00 / 1M = 0.0001 + 0.00025 = 0.00035
    assert cost == Decimal("0.00035")


def test_unknown_model_raises():
    table = PriceTable(version="t", models={})
    with pytest.raises(UnknownPricingModelError):
        table.compute_cost("nope:model", TokenUsage(0, 0))


def test_version_is_recorded():
    table = PriceTable(
        version="2026-01-01",
        models={"x:y": ModelPricing(Decimal("1"), Decimal("2"))},
    )
    assert table.version == "2026-01-01"
