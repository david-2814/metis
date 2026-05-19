"""Routing engine.

See docs/specs/routing-engine.md.

Surface: per-message override, manual sticky, configured rules, workspace
default, global default. PATTERN_RECOMMENDATION and DELEGATE_REQUEST slots
exist in the chain but always return NOT_APPLICABLE — filled in in later phases.
"""

from metis.core.routing.availability import (
    AvailabilityState,
    ProviderAvailability,
)
from metis.core.routing.context import RoutingDecision, TurnContext
from metis.core.routing.engine import RoutingEngine, RoutingError
from metis.core.routing.overrides import OverrideParseResult, parse_per_message_override
from metis.core.routing.policy import (
    EMPTY_POLICY,
    PatternConfig,
    RoutingPolicy,
    Rule,
    TierMap,
    WorkspaceScope,
)
from metis.core.routing.policy_loader import (
    PolicyValidationError,
    load_policy_file,
    parse_policy,
    parse_policy_text,
)
from metis.core.routing.profiles import (
    STANDARD_TASK_PROFILES,
    standard_profile_for,
)
from metis.core.routing.registry import (
    ModelEntry,
    ModelRegistry,
    UnknownModelError,
)

__all__ = [
    "EMPTY_POLICY",
    "STANDARD_TASK_PROFILES",
    "AvailabilityState",
    "ModelEntry",
    "ModelRegistry",
    "OverrideParseResult",
    "PatternConfig",
    "PolicyValidationError",
    "ProviderAvailability",
    "RoutingDecision",
    "RoutingEngine",
    "RoutingError",
    "RoutingPolicy",
    "Rule",
    "TierMap",
    "TurnContext",
    "UnknownModelError",
    "WorkspaceScope",
    "load_policy_file",
    "parse_per_message_override",
    "parse_policy",
    "parse_policy_text",
    "standard_profile_for",
]
