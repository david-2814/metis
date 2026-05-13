"""Per-model price table.

See canonical-message-format.md §6.4. Costs are computed by the core (not
parroted from the provider) so we can retroactively reprice via the trace
store and handle synthetic providers (Ollama at zero) uniformly.
"""

from metis_core.pricing.table import (
    DEFAULT_PRICE_TABLE,
    ModelPricing,
    PriceTable,
    UnknownPricingModelError,
)

__all__ = [
    "DEFAULT_PRICE_TABLE",
    "ModelPricing",
    "PriceTable",
    "UnknownPricingModelError",
]
