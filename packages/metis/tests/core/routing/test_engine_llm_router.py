"""Engine-side tests for slot 5 (LLM_ROUTER).

Covers what the engine does given a pre-computed `ctx.llm_router_result`
(set by the SessionManager before calling `engine.decide()`). The actual
meta-call is exercised in `test_llm_router_module.py`.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest
from metis.core.events.bus import EventBus, EventFilter, Subscription
from metis.core.events.envelope import Event
from metis.core.routing.context import TurnContext
from metis.core.routing.engine import RoutingEngine
from metis.core.routing.llm_router import LLMRouterResult
from metis.core.routing.policy import EMPTY_POLICY, LLMRouterConfig


@pytest.fixture
async def bus() -> EventBus:
    bus = EventBus()
    bus.start()
    return bus


@pytest.fixture
async def event_log(bus: EventBus) -> list[Event]:
    log: list[Event] = []

    async def handler(e: Event) -> None:
        log.append(e)

    bus.subscribe(Subscription(filter=EventFilter(), handler=handler, name="log", fast_path=True))
    return log


def _ctx(**overrides) -> TurnContext:
    defaults = dict(
        session_id="sess_1",
        turn_id="01HZ_t1",
        estimated_input_tokens=100,
        has_images=False,
        has_tool_definitions=False,
        has_system_prompt=False,
        user_message_text="hello",
    )
    defaults.update(overrides)
    return TurnContext(**defaults)


def _engine_with_llm_router(*, registry, bus, enabled: bool) -> RoutingEngine:
    policy = replace(EMPTY_POLICY, llm_router=LLMRouterConfig(enabled=enabled))
    return RoutingEngine(registry=registry, bus=bus, policy=policy)


# ---- Slot wiring -------------------------------------------------------


async def test_llm_router_slot_disabled_by_default(registry, bus, event_log):
    """Empty policy → slot 5 reports `llm_router disabled` and chain proceeds."""
    engine = RoutingEngine(registry=registry, bus=bus, policy=EMPTY_POLICY)
    ctx = _ctx(global_default_model="anthropic:claude-haiku-4-5")
    decision = engine.decide(ctx)
    await bus.drain()
    await bus.stop()
    slot5 = decision.chain[4]
    assert slot5.policy == "llm_router"
    assert slot5.verdict == "not_applicable"
    assert slot5.reason == "llm_router disabled"
    # No meta-call happened → cost / token fields are None.
    assert slot5.meta_cost_usd is None
    assert slot5.meta_tokens_input is None
    assert slot5.meta_tokens_output is None


async def test_llm_router_enabled_but_no_precomputed(registry, bus, event_log):
    """Defensive: policy enabled but caller didn't pre-compute → fall through."""
    engine = _engine_with_llm_router(registry=registry, bus=bus, enabled=True)
    ctx = _ctx(global_default_model="anthropic:claude-haiku-4-5")
    decision = engine.decide(ctx)
    await bus.drain()
    await bus.stop()
    slot5 = decision.chain[4]
    assert slot5.policy == "llm_router"
    assert slot5.verdict == "not_applicable"
    assert slot5.reason == "llm_router not pre-computed"


async def test_llm_router_defers_in_worker_reentry(registry, bus, event_log):
    """Worker re-entry: slot 5 defers per §4.6.4 even when enabled."""
    engine = _engine_with_llm_router(registry=registry, bus=bus, enabled=True)
    ctx = _ctx(
        worker_tier_model="anthropic:claude-haiku-4-5",
        global_default_model="anthropic:claude-sonnet-4-6",
        # Even with a precomputed result, worker re-entry overrides:
        llm_router_result=LLMRouterResult(
            chosen_model="anthropic:claude-opus-4-7", failure_reason=None
        ),
    )
    decision = engine.decide(ctx)
    await bus.drain()
    await bus.stop()
    slot5 = decision.chain[4]
    assert slot5.policy == "llm_router"
    assert slot5.verdict == "not_applicable"
    assert slot5.reason == "delegate_request_in_flight"
    # Slot 6 (delegate_request) wins.
    assert decision.chosen_model == "anthropic:claude-haiku-4-5"
    assert decision.winner_index == 5  # delegate_request slot


async def test_llm_router_chose_picks_model(registry, bus, event_log):
    """Pre-computed `chosen_model` becomes the slot 5 candidate."""
    engine = _engine_with_llm_router(registry=registry, bus=bus, enabled=True)
    ctx = _ctx(
        global_default_model="anthropic:claude-sonnet-4-6",
        llm_router_result=LLMRouterResult(
            chosen_model="anthropic:claude-haiku-4-5",
            failure_reason=None,
            meta_cost_usd=Decimal("0.00021"),
            meta_tokens_input=1840,
            meta_tokens_output=72,
            reason_text="commit-style task fits fast tier",
        ),
    )
    decision = engine.decide(ctx)
    await bus.drain()
    await bus.stop()
    assert decision.chosen_model == "anthropic:claude-haiku-4-5"
    assert decision.winner_index == 4  # slot 5 (index 4)
    slot5 = decision.chain[4]
    assert slot5.verdict == "chose"
    assert slot5.candidate_model == "anthropic:claude-haiku-4-5"
    assert "commit-style task fits fast tier" in slot5.reason
    assert slot5.meta_cost_usd == pytest.approx(0.00021)
    assert slot5.meta_tokens_input == 1840
    assert slot5.meta_tokens_output == 72


async def test_llm_router_failure_reason_propagates(registry, bus, event_log):
    """Pre-computed failure → not_applicable with the failure reason recorded."""
    engine = _engine_with_llm_router(registry=registry, bus=bus, enabled=True)
    ctx = _ctx(
        global_default_model="anthropic:claude-haiku-4-5",
        llm_router_result=LLMRouterResult(
            chosen_model=None,
            failure_reason="timeout",
            meta_cost_usd=Decimal("0"),
            meta_tokens_input=0,
            meta_tokens_output=0,
        ),
    )
    decision = engine.decide(ctx)
    await bus.drain()
    await bus.stop()
    slot5 = decision.chain[4]
    assert slot5.verdict == "not_applicable"
    assert slot5.reason == "timeout"
    # Slot 7 (workspace_default disabled, so global_default) wins.
    assert decision.chosen_model == "anthropic:claude-haiku-4-5"


async def test_llm_router_chose_rejected_on_validation_failure(registry, bus, event_log):
    """Router picks a vision-required-but-text-only model → slot rejected."""
    engine = _engine_with_llm_router(registry=registry, bus=bus, enabled=True)
    ctx = _ctx(
        has_images=True,
        global_default_model="anthropic:claude-haiku-4-5",
        llm_router_result=LLMRouterResult(
            chosen_model="openai:gpt-text-only",  # supports_images=False
            failure_reason=None,
            meta_cost_usd=Decimal("0.0001"),
            meta_tokens_input=500,
            meta_tokens_output=20,
        ),
    )
    decision = engine.decide(ctx)
    await bus.drain()
    await bus.stop()
    slot5 = decision.chain[4]
    assert slot5.verdict == "rejected"
    assert slot5.candidate_model == "openai:gpt-text-only"
    assert slot5.validation_failure == "no_vision_support"
    # Meta-cost is still attributed even on a rejected pick.
    assert slot5.meta_cost_usd == pytest.approx(0.0001)


async def test_llm_router_workspace_scope_overrides_global(registry, bus, event_log):
    """Workspace `llm_router:` block overrides global per §5.6."""
    from metis.core.routing.policy import RoutingPolicy, WorkspaceScope

    workspace_path = "/special"
    policy = RoutingPolicy(
        schema_version=1,
        global_default="anthropic:claude-sonnet-4-6",
        tiers=None,
        pattern=EMPTY_POLICY.pattern,
        rules=(),
        workspaces=(
            WorkspaceScope(
                workspace_path=workspace_path,
                llm_router=LLMRouterConfig(enabled=True, model="anthropic:claude-haiku-4-5"),
            ),
        ),
        # Global block: disabled
        llm_router=LLMRouterConfig(enabled=False),
    )
    engine = RoutingEngine(registry=registry, bus=bus, policy=policy)
    ctx = _ctx(workspace_path=workspace_path)
    decision = engine.decide(ctx)
    slot5 = decision.chain[4]
    # Workspace block enabled → slot looks for ctx.llm_router_result, finds None,
    # reports "not pre-computed" (the workspace override IS being honored).
    assert slot5.reason == "llm_router not pre-computed"

    # Now with workspace_path that doesn't match → global block (disabled) applies.
    ctx_other = _ctx(workspace_path="/other")
    decision_other = engine.decide(ctx_other)
    assert decision_other.chain[4].reason == "llm_router disabled"
    await bus.drain()
    await bus.stop()
