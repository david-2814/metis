"""Predicate evaluation against a TurnContext.

See routing-engine.md §5.3. Each predicate type maps to a small pure
function. Compound predicates (any_of/all_of/not) short-circuit.

Predicates that need infra not yet built (skills index, file-extensions
tracker, daily cost accumulator) evaluate to False — they're accepted at
load time so users can write forward-compatible policy, but no rule using
them will ever match in v1.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from metis_core.routing.context import TurnContext
from metis_core.routing.policy import (
    AllOf,
    AnyOf,
    CostTodayExceedsUsd,
    EstimatedInputTokensGt,
    EstimatedInputTokensLt,
    FileExtensionsInContext,
    HasImages,
    HasToolCallsInHistory,
    MessageContainsAny,
    MessageMatches,
    Not,
    Predicate,
    SkillsMatchingMessageIncludes,
    TeamBudgetRemainingLt,
    TimeOfDayBetween,
    WorkspacePathMatches,
)


def evaluate(predicate: Predicate, ctx: TurnContext) -> bool:
    """Return True if `ctx` satisfies `predicate`."""
    if isinstance(predicate, MessageMatches):
        return bool(predicate.pattern.search(ctx.user_message_text or ""))
    if isinstance(predicate, MessageContainsAny):
        text = (ctx.user_message_text or "").lower()
        return any(s.lower() in text for s in predicate.substrings)
    if isinstance(predicate, EstimatedInputTokensGt):
        return ctx.estimated_input_tokens > predicate.threshold
    if isinstance(predicate, EstimatedInputTokensLt):
        return ctx.estimated_input_tokens < predicate.threshold
    if isinstance(predicate, HasImages):
        return ctx.has_images == predicate.expected
    if isinstance(predicate, HasToolCallsInHistory):
        return ctx.has_tool_calls_in_history == predicate.expected
    if isinstance(predicate, WorkspacePathMatches):
        return bool(predicate.pattern.search(ctx.workspace_path or ""))
    if isinstance(predicate, TimeOfDayBetween):
        now = _local_time_in_minutes(ctx)
        return _in_window(now, predicate.start_minutes, predicate.end_minutes)
    if isinstance(predicate, SkillsMatchingMessageIncludes):
        # Skills aren't loaded in v1. No rule using this predicate will fire.
        return False
    if isinstance(predicate, FileExtensionsInContext):
        # File-extensions tracker isn't wired in v1.
        return False
    if isinstance(predicate, CostTodayExceedsUsd):
        # Daily-cost accumulator isn't wired in v1.
        return False
    if isinstance(predicate, TeamBudgetRemainingLt):
        # Set by the gateway harness (multi-user.md §6.1). The agent path
        # leaves it None and the predicate is False — there is no team
        # binding to cap.
        if ctx.team_budget_remaining_usd is None:
            return False
        return ctx.team_budget_remaining_usd < Decimal(str(predicate.threshold_usd))
    if isinstance(predicate, AnyOf):
        return any(evaluate(p, ctx) for p in predicate.predicates)
    if isinstance(predicate, AllOf):
        return all(evaluate(p, ctx) for p in predicate.predicates)
    if isinstance(predicate, Not):
        return not evaluate(predicate.predicate, ctx)
    raise TypeError(f"unknown predicate type: {type(predicate).__name__}")


def _local_time_in_minutes(ctx: TurnContext) -> int:
    """Current wall-clock time in the user's local timezone, expressed as
    minutes since 00:00. Honors `ctx.now_override` for deterministic tests."""
    if ctx.now_override is not None:
        # Override is interpreted as local time directly.
        return ctx.now_override.hour * 60 + ctx.now_override.minute
    tz = ZoneInfo(ctx.timezone) if ctx.timezone else None
    now = datetime.now(tz)
    return now.hour * 60 + now.minute


def _in_window(now_minutes: int, start: int, end: int) -> bool:
    """Closed-open window check that wraps midnight when end < start."""
    if start <= end:
        return start <= now_minutes < end
    # Wraps midnight: e.g. [22:00, 06:00] = [22:00, 24:00) union [00:00, 06:00)
    return now_minutes >= start or now_minutes < end
