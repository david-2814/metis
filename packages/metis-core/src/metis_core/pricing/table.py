"""PriceTable: per-model rates and cost computation.

Rates are stored as `usd_per_million_tokens` (matches provider docs) and
Decimal arithmetic preserves accuracy for cent-level costs.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from metis_core.adapters.protocol import TokenUsage


class UnknownPricingModelError(KeyError):
    """Raised when a model isn't in the price table."""

    def __init__(self, model_id: str) -> None:
        super().__init__(model_id)
        self.model_id = model_id


@dataclass(frozen=True)
class ModelPricing:
    """USD per million tokens for a single model."""

    input_per_mtok: Decimal
    output_per_mtok: Decimal
    cached_read_per_mtok: Decimal = Decimal("0")
    cache_creation_per_mtok: Decimal = Decimal("0")


_MILLION = Decimal("1000000")


class PriceTable:
    """Maps canonical model id → per-million-token rates.

    Versioned via `pricing_version`. Each computed cost stamps this version
    on the message metadata so retroactive reprice (Phase 2+) can walk traces.
    """

    def __init__(self, *, version: str, models: dict[str, ModelPricing]) -> None:
        self._version = version
        self._models = dict(models)

    @property
    def version(self) -> str:
        return self._version

    def pricing_for(self, model_id: str) -> ModelPricing:
        try:
            return self._models[model_id]
        except KeyError:
            raise UnknownPricingModelError(model_id) from None

    def compute_cost(self, model_id: str, usage: TokenUsage) -> Decimal:
        """Compute cost in USD for a token usage record (canonical-format §6.3)."""
        rates = self.pricing_for(model_id)
        cost = (
            (Decimal(usage.input_tokens) * rates.input_per_mtok)
            + (Decimal(usage.output_tokens) * rates.output_per_mtok)
            + (Decimal(usage.cached_input_tokens) * rates.cached_read_per_mtok)
            + (Decimal(usage.cache_creation_input_tokens) * rates.cache_creation_per_mtok)
        ) / _MILLION
        return cost

    def __contains__(self, model_id: object) -> bool:
        return model_id in self._models

    def with_overlay(
        self, *, overlay_version: str, overlay_models: dict[str, ModelPricing]
    ) -> PriceTable:
        """Return a new PriceTable with `overlay_models` merged in.

        Used by adapters with dynamic pricing (e.g. OpenRouter fetches rates
        at startup). The composed `version` string lets retroactive reprice
        know which source/version was active when a cost was stamped.
        """
        merged = {**self._models, **overlay_models}
        return PriceTable(version=f"{self._version}+{overlay_version}", models=merged)


# Default rates for the Claude 4.x line and the GPT-5 line. Update when
# pricing changes — the `version` string lets us walk historical traces and
# re-price as needed.
DEFAULT_PRICE_TABLE = PriceTable(
    version="2026-05-08",
    models={
        # Anthropic
        "anthropic:claude-opus-4-7": ModelPricing(
            input_per_mtok=Decimal("15.00"),
            output_per_mtok=Decimal("75.00"),
            cached_read_per_mtok=Decimal("1.50"),
            cache_creation_per_mtok=Decimal("18.75"),
        ),
        "anthropic:claude-sonnet-4-6": ModelPricing(
            input_per_mtok=Decimal("3.00"),
            output_per_mtok=Decimal("15.00"),
            cached_read_per_mtok=Decimal("0.30"),
            cache_creation_per_mtok=Decimal("3.75"),
        ),
        "anthropic:claude-haiku-4-5": ModelPricing(
            input_per_mtok=Decimal("1.00"),
            output_per_mtok=Decimal("5.00"),
            cached_read_per_mtok=Decimal("0.10"),
            cache_creation_per_mtok=Decimal("1.25"),
        ),
        # OpenAI — GPT-5 family. cache_creation_input_tokens is always 0 for
        # OpenAI (their cache is provider-managed, not separately reported).
        "openai:gpt-5": ModelPricing(
            input_per_mtok=Decimal("2.50"),
            output_per_mtok=Decimal("10.00"),
            cached_read_per_mtok=Decimal("0.25"),
        ),
        "openai:gpt-5-mini": ModelPricing(
            input_per_mtok=Decimal("0.50"),
            output_per_mtok=Decimal("2.00"),
            cached_read_per_mtok=Decimal("0.05"),
        ),
    },
)
