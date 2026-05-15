"""Parsed routing policy types.

See routing-engine.md §5. The yaml on disk is parsed into these immutable
dataclasses; the engine then evaluates the chain against them per turn.

Splitting this from `policy_loader.py` keeps the types importable without
pulling in yaml or the file system — tests can construct policies directly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class TierMap:
    """Tier → canonical model id mapping (routing-engine §5.1).

    All three slots required if present at all (validated at load time per
    §5.2 "must define all three slots ... or be omitted entirely").
    """

    fast: str
    balanced: str
    deep: str


@dataclass(frozen=True)
class PatternConfig:
    """Pattern store knobs (routing-engine §5.5).

    `min_eval_confidence` is the consumer-side confidence gate from
    `pattern-store.md §15.4`: verdicts with `confidence < min_eval_confidence`
    are recorded but excluded from K-cluster success aggregation. Default
    `0.5` matches `evaluator.md §4.3`.
    """

    cost_weight: float = 0.3
    min_confidence: float = 0.3
    min_sample_size: int = 5
    min_eval_confidence: float = 0.5


# ---- Predicates ------------------------------------------------------------
#
# We model predicates as a tagged union. A predicate is a callable in spirit
# but represented as data so the policy can be inspected, serialized, and
# tested without invoking it.


@dataclass(frozen=True)
class MessageMatches:
    """`message_matches`: regex against the new USER message."""

    pattern: re.Pattern[str]


@dataclass(frozen=True)
class MessageContainsAny:
    """`message_contains_any`: case-insensitive substring match (any of)."""

    substrings: tuple[str, ...]


@dataclass(frozen=True)
class EstimatedInputTokensGt:
    threshold: int


@dataclass(frozen=True)
class EstimatedInputTokensLt:
    threshold: int


@dataclass(frozen=True)
class HasImages:
    expected: bool = True


@dataclass(frozen=True)
class HasToolCallsInHistory:
    expected: bool = True


@dataclass(frozen=True)
class WorkspacePathMatches:
    """Regex against the session's absolute workspace path."""

    pattern: re.Pattern[str]


@dataclass(frozen=True)
class TimeOfDayBetween:
    """Local wall-clock window, e.g. (22:00, 06:00). Wraps midnight if end < start."""

    start_minutes: int  # minutes since 00:00 local
    end_minutes: int


# Predicates that need infra we haven't built yet. The engine treats them as
# always-false (i.e., a rule mentioning them will never match), but they're
# accepted at validation time so users can write forward-compatible policy.


@dataclass(frozen=True)
class SkillsMatchingMessageIncludes:
    """Phase 2.5: skill description index. v1 evaluates to False."""

    skill_names: tuple[str, ...]


@dataclass(frozen=True)
class FileExtensionsInContext:
    """File extensions touched by tools this session. v1 evaluates to False."""

    extensions: tuple[str, ...]  # case-insensitive, leading dot included


@dataclass(frozen=True)
class CostTodayExceedsUsd:
    """Daily cost circuit breaker (§5.4). v1 evaluates to False — daily
    accumulator isn't wired yet."""

    threshold_usd: float


# Compound predicates.


@dataclass(frozen=True)
class AnyOf:
    predicates: tuple[Predicate, ...]


@dataclass(frozen=True)
class AllOf:
    predicates: tuple[Predicate, ...]


@dataclass(frozen=True)
class Not:
    predicate: Predicate


Predicate = (
    MessageMatches
    | MessageContainsAny
    | EstimatedInputTokensGt
    | EstimatedInputTokensLt
    | HasImages
    | HasToolCallsInHistory
    | WorkspacePathMatches
    | TimeOfDayBetween
    | SkillsMatchingMessageIncludes
    | FileExtensionsInContext
    | CostTodayExceedsUsd
    | AnyOf
    | AllOf
    | Not
)


# ---- Rules + scopes --------------------------------------------------------


@dataclass(frozen=True)
class Rule:
    """One configured routing rule (routing-engine §5.1).

    `when` is the parsed predicate (a single top-level key, or AllOf if
    multiple top-level keys were present in yaml). `use` is the canonical
    model id. `name` is unique within its scope; synthetic names get the
    form `rule_<index>`.
    """

    name: str
    when: Predicate
    use: str
    scope: Literal["global", "workspace"] = "global"


@dataclass(frozen=True)
class WorkspaceScope:
    """Per-workspace overrides (routing-engine §5.1 `workspaces.{path}`).

    `default`, `tiers`, `pattern` fully replace global config when present
    (no merge — v1 simplification per §5.2). `rules` are evaluated *before*
    global rules (workspace rules win on tie).
    """

    workspace_path: str  # absolute, expanded
    default: str | None = None
    tiers: TierMap | None = None
    pattern: PatternConfig | None = None
    rules: tuple[Rule, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class RoutingPolicy:
    """The full parsed and validated routing.yaml."""

    schema_version: int
    global_default: str | None
    tiers: TierMap | None
    pattern: PatternConfig
    rules: tuple[Rule, ...]
    workspaces: tuple[WorkspaceScope, ...]
    source_path: str | None = None  # for /rules check display; None for in-memory
    # Opaque per-load identifier surfaced by `GET /sessions/{id}` so the SPA
    # / clients can label "rules vN" and notice when the active policy
    # changes. Computed from the raw yaml content at parse time; `None` for
    # `EMPTY_POLICY` and other in-memory fixtures that don't carry a source.
    version: str | None = None

    def workspace_for(self, workspace_path: str) -> WorkspaceScope | None:
        """Best-match workspace scope for a given absolute workspace path.

        Exact match wins; otherwise None. (Substring/prefix matching is a
        deliberate non-feature in v1 — explicit paths only.)
        """
        for ws in self.workspaces:
            if ws.workspace_path == workspace_path:
                return ws
        return None


# An empty policy that matches the engine's pre-rules behavior. Useful as
# a default when no routing.yaml is present.
EMPTY_POLICY = RoutingPolicy(
    schema_version=1,
    global_default=None,
    tiers=None,
    pattern=PatternConfig(),
    rules=(),
    workspaces=(),
)
