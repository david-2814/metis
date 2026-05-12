"""RoutingEngine + RoutingPolicy: rule slot fills with matched configured rules.

Verifies:
- Rule match → `rule` slot chooses, winner_index = 2, rule_name surfaced
- No rules matched → `rule` slot reports not_applicable with reason
- Workspace rule beats global rule for same predicate
- Workspace default replaces global default when set
- Policy global_default takes precedence over TurnContext.global_default_model
"""

from __future__ import annotations

import re

import pytest

from metis.events.bus import EventBus, EventFilter, Subscription
from metis.events.envelope import Event
from metis.routing.context import TurnContext
from metis.routing.engine import RoutingEngine
from metis.routing.policy import (
    EMPTY_POLICY,
    MessageMatches,
    RoutingPolicy,
    Rule,
    WorkspaceScope,
)


@pytest.fixture
async def bus() -> EventBus:
    b = EventBus()
    b.start()
    return b


@pytest.fixture
async def event_log(bus: EventBus) -> list[Event]:
    log: list[Event] = []

    async def handler(e: Event) -> None:
        log.append(e)

    bus.subscribe(Subscription(filter=EventFilter(), handler=handler, name="log", fast_path=True))
    return log


def _ctx(**overrides) -> TurnContext:
    base = dict(
        session_id="s",
        turn_id="t",
        estimated_input_tokens=100,
        global_default_model="anthropic:claude-sonnet-4-6",
    )
    base.update(overrides)
    return TurnContext(**base)


def _policy(
    rules=(),
    workspaces=(),
    global_default=None,
):
    return RoutingPolicy(
        schema_version=1,
        global_default=global_default,
        tiers=None,
        pattern=EMPTY_POLICY.pattern,
        rules=tuple(rules),
        workspaces=tuple(workspaces),
    )


# ---- Rule slot ---------------------------------------------------------


async def test_matched_rule_wins(bus, event_log, registry):
    policy = _policy(
        rules=[
            Rule(
                name="fast for commits",
                when=MessageMatches(pattern=re.compile(r"/commit")),
                use="anthropic:claude-haiku-4-5",
            )
        ]
    )
    engine = RoutingEngine(registry=registry, bus=bus, policy=policy)
    ctx = _ctx(user_message_text="please /commit the change")
    decision = engine.decide(ctx)
    await bus.drain()
    await bus.stop()
    assert decision.chosen_model == "anthropic:claude-haiku-4-5"
    assert decision.winner_index == 2  # `rule` slot
    rule_eval = decision.chain[2]
    assert rule_eval.verdict == "chose"
    assert rule_eval.rule_name == "fast for commits"
    assert "matched rule" in rule_eval.reason


async def test_no_rule_match_falls_through_to_default(bus, event_log, registry):
    policy = _policy(
        rules=[
            Rule(
                name="commits",
                when=MessageMatches(pattern=re.compile(r"/commit")),
                use="anthropic:claude-haiku-4-5",
            )
        ]
    )
    engine = RoutingEngine(registry=registry, bus=bus, policy=policy)
    ctx = _ctx(user_message_text="hello there")
    decision = engine.decide(ctx)
    await bus.drain()
    await bus.stop()
    assert decision.chosen_model == "anthropic:claude-sonnet-4-6"  # global default
    assert decision.winner_index == 6  # global_default slot
    rule_eval = decision.chain[2]
    assert rule_eval.verdict == "not_applicable"
    assert "no rule matched" in rule_eval.reason


async def test_first_matching_rule_wins(bus, event_log, registry):
    policy = _policy(
        rules=[
            Rule(
                name="first",
                when=MessageMatches(pattern=re.compile(r"hello")),
                use="anthropic:claude-haiku-4-5",
            ),
            Rule(
                name="second",
                when=MessageMatches(pattern=re.compile(r"hello")),
                use="anthropic:claude-opus-4-7",
            ),
        ]
    )
    engine = RoutingEngine(registry=registry, bus=bus, policy=policy)
    decision = engine.decide(_ctx(user_message_text="hello there"))
    await bus.drain()
    await bus.stop()
    assert decision.chosen_model == "anthropic:claude-haiku-4-5"
    assert decision.chain[2].rule_name == "first"


async def test_no_rules_configured_reports_specific_reason(bus, event_log, registry):
    engine = RoutingEngine(registry=registry, bus=bus, policy=EMPTY_POLICY)
    decision = engine.decide(_ctx(user_message_text="hi"))
    await bus.drain()
    await bus.stop()
    assert decision.chain[2].verdict == "not_applicable"
    assert "no rules configured" in decision.chain[2].reason


async def test_sticky_still_wins_over_rule(bus, event_log, registry):
    """`manual_sticky` is slot 1, `rule` is slot 2 — sticky wins."""
    policy = _policy(
        rules=[
            Rule(
                name="r",
                when=MessageMatches(pattern=re.compile(r"x")),
                use="anthropic:claude-haiku-4-5",
            )
        ]
    )
    engine = RoutingEngine(registry=registry, bus=bus, policy=policy)
    ctx = _ctx(
        user_message_text="x",
        session_active_model="anthropic:claude-opus-4-7",
    )
    decision = engine.decide(ctx)
    await bus.drain()
    await bus.stop()
    assert decision.chosen_model == "anthropic:claude-opus-4-7"
    assert decision.winner_index == 1  # manual_sticky


async def test_rule_picks_unconfigured_model_falls_through(bus, event_log, registry):
    """A rule that selects a model not in the registry must be rejected at
    validation time (in the policy loader); but if it somehow gets through,
    the engine's validation rejects the candidate and the chain continues."""
    policy = _policy(
        rules=[
            Rule(
                name="bogus",
                when=MessageMatches(pattern=re.compile(r".*")),
                use="anthropic:not-real",
            )
        ]
    )
    engine = RoutingEngine(registry=registry, bus=bus, policy=policy)
    decision = engine.decide(_ctx(user_message_text="anything"))
    await bus.drain()
    await bus.stop()
    rule_eval = decision.chain[2]
    assert rule_eval.verdict == "rejected"
    assert rule_eval.validation_failure == "not_configured"
    assert decision.chosen_model == "anthropic:claude-sonnet-4-6"  # global default


# ---- Workspace scope ---------------------------------------------------


async def test_workspace_rule_beats_global_rule(bus, event_log, registry):
    policy = _policy(
        rules=[
            Rule(
                name="global",
                when=MessageMatches(pattern=re.compile(r"hi")),
                use="anthropic:claude-haiku-4-5",
            )
        ],
        workspaces=[
            WorkspaceScope(
                workspace_path="/workspace/a",
                rules=(
                    Rule(
                        name="workspace",
                        when=MessageMatches(pattern=re.compile(r"hi")),
                        use="anthropic:claude-opus-4-7",
                        scope="workspace",
                    ),
                ),
            )
        ],
    )
    engine = RoutingEngine(registry=registry, bus=bus, policy=policy)
    ctx = _ctx(user_message_text="hi", workspace_path="/workspace/a")
    decision = engine.decide(ctx)
    await bus.drain()
    await bus.stop()
    assert decision.chosen_model == "anthropic:claude-opus-4-7"
    assert decision.chain[2].rule_name == "workspace"


async def test_workspace_rules_skip_when_path_doesnt_match(bus, event_log, registry):
    policy = _policy(
        rules=[
            Rule(
                name="global",
                when=MessageMatches(pattern=re.compile(r"hi")),
                use="anthropic:claude-haiku-4-5",
            )
        ],
        workspaces=[
            WorkspaceScope(
                workspace_path="/workspace/a",
                rules=(
                    Rule(
                        name="workspace",
                        when=MessageMatches(pattern=re.compile(r"hi")),
                        use="anthropic:claude-opus-4-7",
                        scope="workspace",
                    ),
                ),
            )
        ],
    )
    engine = RoutingEngine(registry=registry, bus=bus, policy=policy)
    # workspace_path doesn't match any workspace scope.
    ctx = _ctx(user_message_text="hi", workspace_path="/workspace/b")
    decision = engine.decide(ctx)
    await bus.drain()
    await bus.stop()
    assert decision.chosen_model == "anthropic:claude-haiku-4-5"
    assert decision.chain[2].rule_name == "global"


async def test_workspace_default_overrides_global(bus, event_log, registry):
    policy = _policy(
        global_default="anthropic:claude-sonnet-4-6",
        workspaces=[
            WorkspaceScope(
                workspace_path="/special",
                default="anthropic:claude-opus-4-7",
            )
        ],
    )
    engine = RoutingEngine(registry=registry, bus=bus, policy=policy)
    ctx = _ctx(
        user_message_text="anything",
        workspace_path="/special",
        global_default_model=None,  # disable legacy fallback
    )
    decision = engine.decide(ctx)
    await bus.drain()
    await bus.stop()
    # Workspace default takes the `workspace_default` slot (index 5).
    assert decision.chosen_model == "anthropic:claude-opus-4-7"
    assert decision.winner_index == 5


async def test_policy_global_default_overrides_turn_context(bus, event_log, registry):
    policy = _policy(global_default="anthropic:claude-haiku-4-5")
    engine = RoutingEngine(registry=registry, bus=bus, policy=policy)
    ctx = _ctx(
        user_message_text="hi",
        global_default_model="anthropic:claude-opus-4-7",  # should be overridden
    )
    decision = engine.decide(ctx)
    await bus.drain()
    await bus.stop()
    assert decision.chosen_model == "anthropic:claude-haiku-4-5"


# ---- Backward compatibility ---------------------------------------------


async def test_no_policy_behaves_like_before(bus, event_log, registry):
    """Engine constructed without a policy must still route via the chain."""
    engine = RoutingEngine(registry=registry, bus=bus)  # no policy kwarg
    decision = engine.decide(
        _ctx(user_message_text="hi", global_default_model="anthropic:claude-sonnet-4-6")
    )
    await bus.drain()
    await bus.stop()
    assert decision.chosen_model == "anthropic:claude-sonnet-4-6"
    assert decision.chain[2].verdict == "not_applicable"
