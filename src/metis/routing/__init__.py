"""Routing engine.

See docs/specs/routing-engine.md.

Phase 1 surface: per-message override, manual sticky, workspace default,
global default. CONFIGURED_RULES, PATTERN_RECOMMENDATION, and DELEGATE_REQUEST
slots exist in the chain but always return NOT_APPLICABLE — they're filled in
in later phases.
"""

from metis.routing.availability import (
    AvailabilityState,
    ProviderAvailability,
)
from metis.routing.context import RoutingDecision, TurnContext
from metis.routing.engine import RoutingEngine, RoutingError
from metis.routing.overrides import OverrideParseResult, parse_per_message_override
from metis.routing.registry import (
    ModelEntry,
    ModelRegistry,
    UnknownModelError,
)

__all__ = [
    "AvailabilityState",
    "ModelEntry",
    "ModelRegistry",
    "OverrideParseResult",
    "ProviderAvailability",
    "RoutingDecision",
    "RoutingEngine",
    "RoutingError",
    "TurnContext",
    "UnknownModelError",
    "parse_per_message_override",
]
