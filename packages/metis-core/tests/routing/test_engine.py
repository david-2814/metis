"""Tests for the RoutingEngine policy chain and event emission."""

from __future__ import annotations

import pytest
from metis_core.adapters.errors import ErrorClass
from metis_core.events.bus import EventBus, EventFilter, Subscription
from metis_core.events.envelope import Event
from metis_core.routing.context import TurnContext
from metis_core.routing.engine import RoutingEngine, RoutingError


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


@pytest.fixture
def engine(bus, registry) -> RoutingEngine:
    return RoutingEngine(registry=registry, bus=bus)


def _ctx(**overrides) -> TurnContext:
    defaults = dict(
        session_id="sess_1",
        turn_id="01HZ_t1",
        estimated_input_tokens=100,
        has_images=False,
        has_tool_definitions=False,
        has_system_prompt=False,
    )
    defaults.update(overrides)
    return TurnContext(**defaults)


# ---- Chain ordering ----------------------------------------------------


async def test_per_message_override_wins_over_sticky(engine, bus, event_log):
    ctx = _ctx(
        per_message_override="anthropic:claude-haiku-4-5",
        session_active_model="anthropic:claude-sonnet-4-6",
        workspace_default_model="anthropic:claude-opus-4-7",
        global_default_model="anthropic:claude-sonnet-4-6",
    )
    decision = engine.decide(ctx)
    await bus.drain()
    await bus.stop()
    assert decision.chosen_model == "anthropic:claude-haiku-4-5"
    assert decision.winner_index == 0
    assert decision.chain[0].verdict == "chose"


async def test_manual_sticky_wins_when_no_override(engine, bus, event_log):
    ctx = _ctx(
        session_active_model="anthropic:claude-opus-4-7",
        workspace_default_model="anthropic:claude-sonnet-4-6",
    )
    decision = engine.decide(ctx)
    await bus.drain()
    await bus.stop()
    assert decision.chosen_model == "anthropic:claude-opus-4-7"
    assert decision.winner_index == 1
    assert decision.chain[0].verdict == "not_applicable"
    assert decision.chain[1].verdict == "chose"


async def test_workspace_default_wins_over_global(engine, bus, event_log):
    ctx = _ctx(
        workspace_default_model="anthropic:claude-sonnet-4-6",
        global_default_model="anthropic:claude-haiku-4-5",
    )
    decision = engine.decide(ctx)
    await bus.drain()
    await bus.stop()
    assert decision.chosen_model == "anthropic:claude-sonnet-4-6"
    assert decision.winner_index == 5


async def test_global_default_is_last_resort(engine, bus, event_log):
    ctx = _ctx(global_default_model="anthropic:claude-haiku-4-5")
    decision = engine.decide(ctx)
    await bus.drain()
    await bus.stop()
    assert decision.chosen_model == "anthropic:claude-haiku-4-5"
    assert decision.winner_index == 6


async def test_phase1_stub_policies_always_not_applicable(engine, bus, event_log):
    ctx = _ctx(global_default_model="anthropic:claude-sonnet-4-6")
    decision = engine.decide(ctx)
    await bus.drain()
    await bus.stop()
    stub_policies = {"rule", "pattern", "delegate_request"}
    for entry in decision.chain:
        if entry.policy in stub_policies:
            assert entry.verdict == "not_applicable"


# ---- Validation gates --------------------------------------------------


async def test_vision_required_skips_text_only_model(engine, bus, event_log):
    ctx = _ctx(
        has_images=True,
        session_active_model="openai:gpt-text-only",  # supports_images=False
        global_default_model="anthropic:claude-sonnet-4-6",
    )
    decision = engine.decide(ctx)
    await bus.drain()
    await bus.stop()
    assert decision.chosen_model == "anthropic:claude-sonnet-4-6"
    # Sticky candidate was rejected for vision.
    sticky_eval = decision.chain[1]
    assert sticky_eval.verdict == "rejected"
    assert sticky_eval.validation_failure == "no_vision_support"


async def test_oversized_input_skips_model(engine, bus, event_log):
    ctx = _ctx(
        estimated_input_tokens=10_000_000,
        session_active_model="anthropic:claude-sonnet-4-6",
        global_default_model="anthropic:claude-haiku-4-5",
    )
    # Both have 200k context so both fail. Hard failure.
    with pytest.raises(RoutingError):
        engine.decide(ctx)
    await bus.drain()
    await bus.stop()


async def test_tool_required_skips_non_tool_model(engine, bus, event_log):
    ctx = _ctx(
        has_tool_definitions=True,
        session_active_model="openai:gpt-text-only",  # supports_tools=False
        global_default_model="anthropic:claude-sonnet-4-6",
    )
    decision = engine.decide(ctx)
    await bus.drain()
    await bus.stop()
    assert decision.chosen_model == "anthropic:claude-sonnet-4-6"
    assert decision.chain[1].validation_failure == "no_tool_support"


async def test_unconfigured_model_is_rejected(engine, bus, event_log):
    ctx = _ctx(
        session_active_model="anthropic:not-real",
        global_default_model="anthropic:claude-sonnet-4-6",
    )
    decision = engine.decide(ctx)
    await bus.drain()
    await bus.stop()
    assert decision.chosen_model == "anthropic:claude-sonnet-4-6"
    assert decision.chain[1].validation_failure == "not_configured"


async def test_unavailable_provider_skipped(engine, bus, event_log, registry):
    engine.availability.mark_failure("anthropic", "anthropic:claude-sonnet-4-6", ErrorClass.AUTH)
    ctx = _ctx(global_default_model="anthropic:claude-sonnet-4-6")
    with pytest.raises(RoutingError):
        engine.decide(ctx)
    await bus.drain()
    await bus.stop()


async def test_unavailable_provider_falls_through_to_another(engine, bus, event_log):
    engine.availability.mark_failure("anthropic", "anthropic:claude-sonnet-4-6", ErrorClass.AUTH)
    ctx = _ctx(
        session_active_model="anthropic:claude-sonnet-4-6",
        global_default_model="openai:gpt-text-only",  # different provider
    )
    decision = engine.decide(ctx)
    await bus.drain()
    await bus.stop()
    assert decision.chosen_model == "openai:gpt-text-only"
    assert decision.chain[1].validation_failure == "provider_unavailable"


# ---- Hard failure ------------------------------------------------------


async def test_hard_failure_when_chain_exhausted(engine, bus, event_log):
    """No candidate, no defaults → routing fails."""
    ctx = _ctx()  # nothing populated
    with pytest.raises(RoutingError) as exc:
        engine.decide(ctx)
    await bus.drain()
    await bus.stop()
    # Even on hard failure, route.decided is emitted.
    assert any(e.type == "route.decided" for e in event_log)
    # Chain is full (7 entries), winner_index is -1 in the payload.
    decided = next(e for e in event_log if e.type == "route.decided")
    assert decided.payload["winner_index"] == -1
    assert decided.payload["chosen_model"] == ""
    assert len(exc.value.chain) == 7


# ---- Event emission ----------------------------------------------------


async def test_exactly_one_route_decided_per_decision(engine, bus, event_log):
    ctx = _ctx(global_default_model="anthropic:claude-sonnet-4-6")
    engine.decide(ctx)
    await bus.drain()
    await bus.stop()
    assert sum(1 for e in event_log if e.type == "route.decided") == 1


async def test_route_decided_chain_payload_shape(engine, bus, event_log):
    """When fallthrough reaches global_default, all 7 policies appear in
    chain order (per routing-engine.md §10.1.17 — chain length equals the
    number of policies that actually ran)."""
    ctx = _ctx(
        global_default_model="anthropic:claude-sonnet-4-6",
        parent_event_id="parent_evt_1",
    )
    engine.decide(ctx)
    await bus.drain()
    await bus.stop()
    decided = next(e for e in event_log if e.type == "route.decided")
    assert decided.parent_event_id == "parent_evt_1"
    chain = decided.payload["chain"]
    policies = [entry["policy"] for entry in chain]
    assert policies == [
        "per_message_override",
        "manual_sticky",
        "rule",
        "pattern",
        "delegate_request",
        "workspace_default",
        "global_default",
    ]
    assert chain[-1]["verdict"] == "chose"
    assert decided.payload["chosen_model"] == "anthropic:claude-sonnet-4-6"
    assert decided.payload["winner_index"] == 6
    assert decided.payload["elapsed_ms"] >= 0


async def test_chain_truncates_after_winner(engine, bus, event_log):
    """When an earlier policy wins, lower-priority policies aren't evaluated."""
    ctx = _ctx(per_message_override="anthropic:claude-haiku-4-5")
    decision = engine.decide(ctx)
    await bus.drain()
    await bus.stop()
    assert decision.winner_index == 0
    assert len(decision.chain) == 1
    assert decision.chain[0].verdict == "chose"


async def test_rejected_entries_carry_validation_failure(engine, bus, event_log):
    ctx = _ctx(
        has_images=True,
        session_active_model="openai:gpt-text-only",
        global_default_model="anthropic:claude-sonnet-4-6",
    )
    engine.decide(ctx)
    await bus.drain()
    await bus.stop()
    decided = next(e for e in event_log if e.type == "route.decided")
    sticky_entry = decided.payload["chain"][1]
    assert sticky_entry["verdict"] == "rejected"
    assert sticky_entry["validation_failure"] == "no_vision_support"
    assert sticky_entry["candidate_model"] == "openai:gpt-text-only"
