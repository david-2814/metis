"""Slot-4 integration: RoutingEngine ↔ PatternStore.

Verifies the routing engine consults the pattern store when configured,
emits `pattern.matched` when slot 4 wins, and falls through with
`not_applicable` when no resolver/builder is wired or the store is empty.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest

_routing_dir = Path(__file__).parent.parent / "routing"
if str(_routing_dir) not in sys.path:
    sys.path.insert(0, str(_routing_dir))

from _helpers import StubAdapter  # noqa: E402
from metis_core.canonical.capabilities import AdapterCapabilities  # noqa: E402
from metis_core.events.bus import EventBus, EventFilter, Subscription  # noqa: E402
from metis_core.events.envelope import Event  # noqa: E402
from metis_core.patterns.fingerprint import FingerprintInputs, compute_fingerprint  # noqa: E402
from metis_core.patterns.store import PatternStore  # noqa: E402
from metis_core.routing.context import TurnContext  # noqa: E402
from metis_core.routing.engine import RoutingEngine  # noqa: E402
from metis_core.routing.registry import ModelRegistry  # noqa: E402


def _caps(**overrides) -> AdapterCapabilities:
    base = dict(
        supports_thinking=False,
        supports_images=True,
        supports_tools=True,
        supports_system_prompt=True,
        supports_structured_output=False,
        supports_streaming=True,
        supports_streaming_tool_calls=True,
        supports_parallel_tool_calls=True,
        supports_prompt_caching=False,
        supports_system_messages_in_list=False,
        max_context_tokens=200_000,
        max_output_tokens=8192,
        accepted_image_media_types=["image/png", "image/jpeg"],
    )
    base.update(overrides)
    return AdapterCapabilities(**base)


@pytest.fixture
def registry() -> ModelRegistry:
    caps_map = {
        "anthropic:haiku": _caps(),
        "anthropic:sonnet": _caps(),
    }
    adapter = StubAdapter(name="anthropic", caps_map=caps_map)
    reg = ModelRegistry()
    reg.register(model_id="anthropic:haiku", adapter=adapter, aliases=["haiku"])
    reg.register(model_id="anthropic:sonnet", adapter=adapter, aliases=["sonnet"])
    return reg


@pytest.fixture
async def bus() -> EventBus:
    b = EventBus()
    b.start()
    yield b
    await b.stop()


@pytest.fixture
async def event_log(bus: EventBus) -> list[Event]:
    log: list[Event] = []

    async def handler(e: Event) -> None:
        log.append(e)

    bus.subscribe(Subscription(filter=EventFilter(), handler=handler, name="log", fast_path=True))
    return log


def _inputs_for_ctx(ctx: TurnContext) -> FingerprintInputs:
    return FingerprintInputs(
        user_message_text=ctx.user_message_text,
        workspace_path=ctx.workspace_path,
        estimated_input_tokens=ctx.estimated_input_tokens,
        has_images=ctx.has_images,
        has_tool_calls_in_history=ctx.has_tool_calls_in_history,
        file_extensions=(".py",),
        file_path_buckets=("src",),
        tool_names=("read_file",),
        side_effect_classes=("read",),
    )


def _ctx(workspace: str, **overrides) -> TurnContext:
    base = dict(
        session_id="sess_1",
        turn_id="turn_1",
        estimated_input_tokens=1_000,
        has_images=False,
        has_tool_definitions=False,
        has_system_prompt=False,
        has_tool_calls_in_history=False,
        per_message_override=None,
        session_active_model=None,
        workspace_default_model="anthropic:sonnet",
        global_default_model="anthropic:sonnet",
        user_message_text="please refactor this module",
        workspace_path=workspace,
    )
    base.update(overrides)
    return TurnContext(**base)


async def test_pattern_not_applicable_without_resolver(registry, bus, event_log, tmp_path) -> None:
    engine = RoutingEngine(registry=registry, bus=bus)
    decision = engine.decide(_ctx(workspace=str(tmp_path)))
    # Falls through to workspace_default.
    assert decision.chosen_model == "anthropic:sonnet"
    pattern_slot = next(p for p in decision.chain if p.policy == "pattern")
    assert pattern_slot.verdict == "not_applicable"


async def test_pattern_slot_returns_recommendation(registry, bus, event_log, tmp_path) -> None:
    store = PatternStore(tmp_path)
    try:
        # Build prior pattern history for haiku at this fingerprint shape.
        ctx_seed = _ctx(workspace=str(tmp_path))
        inputs = _inputs_for_ctx(ctx_seed)
        fp = compute_fingerprint(inputs)
        for _ in range(5):
            store.record(fp, "anthropic:haiku", 0.9, Decimal("0.005"), 800.0, "v1")
        for _ in range(5):
            store.record(fp, "anthropic:sonnet", 0.3, Decimal("0.020"), 1500.0, "v1")

        engine = RoutingEngine(
            registry=registry,
            bus=bus,
            pattern_store_resolver=lambda ws: store if ws == str(tmp_path) else None,
            fingerprint_inputs_builder=_inputs_for_ctx,
        )
        decision = engine.decide(_ctx(workspace=str(tmp_path)))
        assert decision.chosen_model == "anthropic:haiku"
        pattern_slot = next(p for p in decision.chain if p.policy == "pattern")
        assert pattern_slot.verdict == "chose"
        assert pattern_slot.confidence is not None
        await bus.drain()
        matched = [e for e in event_log if e.type == "pattern.matched"]
        assert len(matched) == 1
        assert matched[0].payload["chosen_model"] == "anthropic:haiku"
    finally:
        store.close()


async def test_pattern_slot_defers_when_no_neighbors(registry, bus, event_log, tmp_path) -> None:
    store = PatternStore(tmp_path)
    try:
        engine = RoutingEngine(
            registry=registry,
            bus=bus,
            pattern_store_resolver=lambda ws: store,
            fingerprint_inputs_builder=_inputs_for_ctx,
        )
        decision = engine.decide(_ctx(workspace=str(tmp_path)))
        assert decision.chosen_model == "anthropic:sonnet"
        pattern_slot = next(p for p in decision.chain if p.policy == "pattern")
        assert pattern_slot.verdict == "not_applicable"
        # No `pattern.matched` event when slot 4 didn't win.
        await bus.drain()
        assert not any(e.type == "pattern.matched" for e in event_log)
    finally:
        store.close()
