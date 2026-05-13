"""routing/predicates.py: evaluate each predicate type against TurnContext."""

from __future__ import annotations

import re
from datetime import datetime

import pytest
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
    SkillsMatchingMessageIncludes,
    TimeOfDayBetween,
    WorkspacePathMatches,
)
from metis_core.routing.predicates import _in_window, evaluate


def _ctx(**overrides) -> TurnContext:
    base = dict(
        session_id="s",
        turn_id="t",
        estimated_input_tokens=100,
    )
    base.update(overrides)
    return TurnContext(**base)


# ---- Leaf predicates ----------------------------------------------------


def test_message_matches_regex():
    ctx = _ctx(user_message_text="please /commit the change")
    assert evaluate(MessageMatches(pattern=re.compile(r"/commit")), ctx) is True
    assert evaluate(MessageMatches(pattern=re.compile(r"^/commit")), ctx) is False


def test_message_matches_empty_message_is_false():
    ctx = _ctx(user_message_text="")
    assert evaluate(MessageMatches(pattern=re.compile(r".+")), ctx) is False


def test_message_contains_any_case_insensitive():
    ctx = _ctx(user_message_text="Please review the SECURITY of this")
    assert evaluate(MessageContainsAny(substrings=("security",)), ctx) is True
    # No substring in list matches
    assert evaluate(MessageContainsAny(substrings=("nothing",)), ctx) is False


def test_estimated_input_tokens_gt():
    ctx = _ctx(estimated_input_tokens=85_000)
    assert evaluate(EstimatedInputTokensGt(threshold=80_000), ctx) is True
    assert evaluate(EstimatedInputTokensGt(threshold=85_000), ctx) is False


def test_estimated_input_tokens_lt():
    ctx = _ctx(estimated_input_tokens=500)
    assert evaluate(EstimatedInputTokensLt(threshold=1_000), ctx) is True
    assert evaluate(EstimatedInputTokensLt(threshold=500), ctx) is False


def test_has_images():
    assert evaluate(HasImages(expected=True), _ctx(has_images=True)) is True
    assert evaluate(HasImages(expected=True), _ctx(has_images=False)) is False
    assert evaluate(HasImages(expected=False), _ctx(has_images=False)) is True


def test_has_tool_calls_in_history():
    assert (
        evaluate(HasToolCallsInHistory(expected=True), _ctx(has_tool_calls_in_history=True)) is True
    )
    assert (
        evaluate(HasToolCallsInHistory(expected=True), _ctx(has_tool_calls_in_history=False))
        is False
    )


def test_workspace_path_matches():
    ctx = _ctx(workspace_path="/Users/me/code/myproject")
    assert evaluate(WorkspacePathMatches(pattern=re.compile(r"^/Users/")), ctx) is True
    assert evaluate(WorkspacePathMatches(pattern=re.compile(r"^/Code/")), ctx) is False


# ---- Stub predicates (return False in v1) -------------------------------


def test_skills_predicate_returns_false_in_v1():
    ctx = _ctx(user_message_text="anything")
    assert evaluate(SkillsMatchingMessageIncludes(skill_names=("anything",)), ctx) is False


def test_file_extensions_returns_false_in_v1():
    ctx = _ctx()
    assert evaluate(FileExtensionsInContext(extensions=(".sql",)), ctx) is False


def test_cost_today_returns_false_in_v1():
    ctx = _ctx()
    assert evaluate(CostTodayExceedsUsd(threshold_usd=5.0), ctx) is False


# ---- time_of_day_between ------------------------------------------------


def test_time_of_day_uses_now_override():
    ctx = _ctx(now_override=datetime(2026, 5, 11, 14, 30))  # 14:30 local
    assert evaluate(TimeOfDayBetween(start_minutes=14 * 60, end_minutes=15 * 60), ctx) is True
    assert evaluate(TimeOfDayBetween(start_minutes=15 * 60, end_minutes=16 * 60), ctx) is False


def test_time_of_day_window_wraps_midnight():
    # 23:30 local is inside the night-hours window [22:00, 06:00].
    ctx = _ctx(now_override=datetime(2026, 5, 11, 23, 30))
    assert evaluate(TimeOfDayBetween(start_minutes=22 * 60, end_minutes=6 * 60), ctx) is True
    # 12:00 noon is NOT in [22:00, 06:00].
    ctx2 = _ctx(now_override=datetime(2026, 5, 11, 12, 0))
    assert evaluate(TimeOfDayBetween(start_minutes=22 * 60, end_minutes=6 * 60), ctx2) is False


@pytest.mark.parametrize(
    "now,start,end,expected",
    [
        (300, 200, 400, True),  # in non-wrap window
        (200, 200, 400, True),  # boundary start (closed)
        (400, 200, 400, False),  # boundary end (open)
        (100, 200, 400, False),  # before window
        (500, 200, 400, False),  # after window
        (30, 22 * 60, 6 * 60, True),  # 00:30 inside wrap window
        (23 * 60, 22 * 60, 6 * 60, True),  # 23:00 inside wrap window
        (12 * 60, 22 * 60, 6 * 60, False),  # noon outside wrap window
    ],
)
def test_in_window(now, start, end, expected):
    assert _in_window(now, start, end) is expected


# ---- Compound predicates ------------------------------------------------


def test_any_of_short_circuits_on_first_true():
    ctx = _ctx(user_message_text="hello", estimated_input_tokens=100)
    pred = AnyOf(
        predicates=(
            MessageMatches(pattern=re.compile("hello")),
            EstimatedInputTokensGt(threshold=100_000),  # would be False
        )
    )
    assert evaluate(pred, ctx) is True


def test_any_of_all_false():
    ctx = _ctx(user_message_text="xyz")
    pred = AnyOf(
        predicates=(
            MessageMatches(pattern=re.compile("hello")),
            MessageMatches(pattern=re.compile("world")),
        )
    )
    assert evaluate(pred, ctx) is False


def test_all_of_requires_every_match():
    ctx = _ctx(user_message_text="hello world", estimated_input_tokens=500)
    pred = AllOf(
        predicates=(
            MessageMatches(pattern=re.compile("hello")),
            EstimatedInputTokensGt(threshold=100),
        )
    )
    assert evaluate(pred, ctx) is True
    # Now one fails:
    pred2 = AllOf(
        predicates=(
            MessageMatches(pattern=re.compile("hello")),
            EstimatedInputTokensGt(threshold=1_000),
        )
    )
    assert evaluate(pred2, ctx) is False


def test_not_inverts():
    ctx = _ctx(has_images=True)
    assert evaluate(Not(predicate=HasImages(expected=True)), ctx) is False
    assert evaluate(Not(predicate=HasImages(expected=False)), ctx) is True


def test_nested_compound():
    """NOT ( ANY_OF [has_images, message_matches('skip')] )"""
    pred = Not(
        predicate=AnyOf(
            predicates=(
                HasImages(expected=True),
                MessageMatches(pattern=re.compile("skip")),
            )
        )
    )
    # Neither child true → outer false → NOT-wrapped → True
    ctx = _ctx(has_images=False, user_message_text="ship it")
    assert evaluate(pred, ctx) is True
    # has_images flips → inner True → NOT → False
    ctx2 = _ctx(has_images=True, user_message_text="ship it")
    assert evaluate(pred, ctx2) is False
