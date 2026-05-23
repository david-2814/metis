"""PriceTable: per-model rates and cost computation.

Rates are stored as `usd_per_million_tokens` (matches provider docs) and
Decimal arithmetic preserves accuracy for cent-level costs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal

from metis.core.adapters.protocol import TokenUsage

logger = logging.getLogger(__name__)


class UnknownPricingModelError(KeyError):
    """Raised when a model isn't in the price table."""

    def __init__(self, model_id: str) -> None:
        super().__init__(model_id)
        self.model_id = model_id


@dataclass(frozen=True)
class ModelPricing:
    """USD per million tokens for a single model.

    `batch_rates` is the optional batch-discounted rate variant per
    provider-adapter-contract.md §4.6.4. When set, `PriceTable.compute_cost`
    bills batch-submitted calls (those carrying `TokenUsage.pricing_mode
    == "batch"`) off this row instead of the sync row. Anthropic and
    OpenAI both document a flat 50% discount on input + output tokens for
    batch submission; populate `batch_rates` with half the sync rates.

    When `pricing_mode == "batch"` but `batch_rates is None`, the
    PriceTable logs a WARN once per model and falls back to sync rates
    (correctness preserved, savings lost). See §4.6.4 acceptance bar.
    """

    input_per_mtok: Decimal
    output_per_mtok: Decimal
    cached_read_per_mtok: Decimal = Decimal("0")
    cache_creation_per_mtok: Decimal = Decimal("0")
    batch_rates: ModelPricing | None = field(default=None)


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
        """Compute cost in USD for a token usage record (canonical-format §6.3).

        When `usage.pricing_mode == "batch"` and the model's `ModelPricing`
        row declares `batch_rates`, the batch rates are applied. When
        `pricing_mode == "batch"` but the row has no `batch_rates`, the
        sync rates are used and a WARN is logged once per model (per
        provider-adapter-contract.md §4.6.4 acceptance bar).
        """
        rates = self.pricing_for(model_id)
        rates = self._effective_rates(model_id, rates, usage.pricing_mode)
        cost = (
            (Decimal(usage.input_tokens) * rates.input_per_mtok)
            + (Decimal(usage.output_tokens) * rates.output_per_mtok)
            + (Decimal(usage.cached_input_tokens) * rates.cached_read_per_mtok)
            + (Decimal(usage.cache_creation_input_tokens) * rates.cache_creation_per_mtok)
        ) / _MILLION
        return cost

    def _effective_rates(
        self, model_id: str, rates: ModelPricing, pricing_mode: str | None
    ) -> ModelPricing:
        if pricing_mode != "batch":
            return rates
        if rates.batch_rates is not None:
            return rates.batch_rates
        self._warn_missing_batch_rates(model_id)
        return rates

    def _warn_missing_batch_rates(self, model_id: str) -> None:
        # Cache the warning per (table_version, model) so a long-running
        # process logs once per model rather than per call.
        cache = getattr(self, "_warned_missing_batch_rates", None)
        if cache is None:
            cache: set[str] = set()
            object.__setattr__(self, "_warned_missing_batch_rates", cache)
        if model_id in cache:
            return
        cache.add(model_id)
        logger.warning(
            "pricing: model %r requested batch rates but ModelPricing.batch_rates is None; "
            "falling back to sync rates (savings lost, correctness preserved). "
            "See provider-adapter-contract.md §4.6.4.",
            model_id,
        )

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
        # Anthropic. `batch_rates` is the Anthropic Batches API flat 50%
        # discount on every line item (input + output + cache reads +
        # cache creation), per provider-adapter-contract.md §4.6.4.
        "anthropic:claude-opus-4-7": ModelPricing(
            input_per_mtok=Decimal("15.00"),
            output_per_mtok=Decimal("75.00"),
            cached_read_per_mtok=Decimal("1.50"),
            cache_creation_per_mtok=Decimal("18.75"),
            batch_rates=ModelPricing(
                input_per_mtok=Decimal("7.50"),
                output_per_mtok=Decimal("37.50"),
                cached_read_per_mtok=Decimal("0.75"),
                cache_creation_per_mtok=Decimal("9.375"),
            ),
        ),
        "anthropic:claude-sonnet-4-6": ModelPricing(
            input_per_mtok=Decimal("3.00"),
            output_per_mtok=Decimal("15.00"),
            cached_read_per_mtok=Decimal("0.30"),
            cache_creation_per_mtok=Decimal("3.75"),
            batch_rates=ModelPricing(
                input_per_mtok=Decimal("1.50"),
                output_per_mtok=Decimal("7.50"),
                cached_read_per_mtok=Decimal("0.15"),
                cache_creation_per_mtok=Decimal("1.875"),
            ),
        ),
        "anthropic:claude-haiku-4-5": ModelPricing(
            input_per_mtok=Decimal("1.00"),
            output_per_mtok=Decimal("5.00"),
            cached_read_per_mtok=Decimal("0.10"),
            cache_creation_per_mtok=Decimal("1.25"),
            batch_rates=ModelPricing(
                input_per_mtok=Decimal("0.50"),
                output_per_mtok=Decimal("2.50"),
                cached_read_per_mtok=Decimal("0.05"),
                cache_creation_per_mtok=Decimal("0.625"),
            ),
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
