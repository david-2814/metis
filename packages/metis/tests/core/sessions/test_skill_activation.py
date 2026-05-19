"""SessionManager + context-assembler.md v3 §5.2 skill activation.

Tests cover:

- §5.2.2 pre-activation: `skill.loaded(load_reason="always")` events fire
  at session init for each skill body inlined into the stable prefix as
  v2 §5.1 padding. The discovery index gains a `[preloaded]` annotation.
  Calling `skill_load` for a pre-activated skill returns a pointer (not
  the body) with `already_preloaded=True` and emits no new event.
- §5.2.3/§5.2.4 explicit-activation budget: `MAX_EXPLICIT_ACTIVATIONS_PER_SESSION`
  count cap and `HARD_CAP_CUMULATIVE_ACTIVATION_TOKENS` token cap both
  fire as `ToolExecutionError` → `tool.failed`. Re-loading an
  already-explicitly-activated skill is a no-op (returns pointer; no
  event, no budget increment).
- §5.2.5 (deferred eviction): activated bodies stay in message history
  for session lifetime — no mid-session eviction in v3.
- Byte-stability of the stable prefix turn-to-turn: the cache_control
  marker stays valid as long as the bytes are identical.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from metis.core.adapters.protocol import StopReason
from metis.core.canonical.content import TextBlock, ToolUseBlock
from metis.core.events.bus import EventBus, EventFilter, Subscription
from metis.core.events.envelope import Event
from metis.core.pricing import DEFAULT_PRICE_TABLE
from metis.core.routing import ModelRegistry, RoutingEngine
from metis.core.sessions import InMemorySessionStore, SessionManager
from metis.core.skills import (
    HARD_CAP_CUMULATIVE_ACTIVATION_TOKENS,
    MAX_EXPLICIT_ACTIVATIONS_PER_SESSION,
    SkillActivationRegistry,
    SkillBudgetExceededError,
    register_skill_tools,
)
from metis.core.skills.activation import WARN_CUMULATIVE_ACTIVATION_TOKENS
from metis.core.skills.store import load_skills
from metis.core.tools.dispatcher import ToolDispatcher

from tests_shared.scripted_adapter import (
    _ScriptedAnthropicAdapter,
    _ScriptedResponse,
)

# ---- Fixtures ----------------------------------------------------------


@pytest.fixture
async def bus() -> EventBus:
    b = EventBus()
    b.start()
    return b


@pytest.fixture
async def event_log(bus: EventBus) -> list[Event]:
    events: list[Event] = []

    async def handler(e: Event) -> None:
        events.append(e)

    bus.subscribe(Subscription(filter=EventFilter(), handler=handler, name="log", fast_path=True))
    return events


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return tmp_path


def _make_skill_dir(root: Path, name: str, desc: str, body: str) -> None:
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\n---\n{body}", encoding="utf-8"
    )


@pytest.fixture
def small_skill_dir(tmp_path: Path) -> Path:
    """Three small skills — bodies short enough that v2 §5.1 padding
    will inline ALL of them as pre-activation."""
    root = tmp_path / "skills"
    _make_skill_dir(root, "alpha", "first skill", "alpha body content.\n" * 5)
    _make_skill_dir(root, "beta", "second skill", "beta body content.\n" * 5)
    _make_skill_dir(root, "gamma", "third skill", "gamma body content.\n" * 5)
    return root


def _build_manager(
    bus: EventBus,
    adapter: _ScriptedAnthropicAdapter,
    *,
    skill_store_factory=None,
):
    registry = ModelRegistry()
    registry.register(
        model_id="anthropic:claude-sonnet-4-6",
        adapter=adapter,
        aliases=["sonnet"],
    )
    routing = RoutingEngine(registry=registry, bus=bus)
    dispatcher = ToolDispatcher(bus)
    register_skill_tools(dispatcher)
    return SessionManager(
        registry=registry,
        routing=routing,
        dispatcher=dispatcher,
        bus=bus,
        store=InMemorySessionStore(),
        pricing=DEFAULT_PRICE_TABLE,
        skill_store_factory=skill_store_factory,
    )


# ---- SkillActivationRegistry unit tests --------------------------------


def test_registry_starts_empty():
    r = SkillActivationRegistry()
    assert r.count == 0
    assert r.cumulative_tokens == 0
    assert r.activated_names == []
    assert r.preloaded_names == frozenset()
    assert not r.is_preloaded("anything")
    assert not r.is_activated("anything")


def test_registry_mark_preloaded_is_idempotent():
    r = SkillActivationRegistry()
    r.mark_preloaded("alpha")
    r.mark_preloaded("alpha")
    r.mark_preloaded("beta")
    assert r.preloaded_names == frozenset({"alpha", "beta"})


def test_registry_record_activation_tracks_order_and_tokens():
    r = SkillActivationRegistry()
    r.record_activation("beta", 100)
    r.record_activation("alpha", 200)
    assert r.count == 2
    assert r.cumulative_tokens == 300
    assert r.activated_names == ["beta", "alpha"]  # insertion order


def test_registry_count_cap_raises():
    r = SkillActivationRegistry()
    for i in range(MAX_EXPLICIT_ACTIVATIONS_PER_SESSION):
        name = f"s{i}"
        r.check_can_activate(name, 100)
        r.record_activation(name, 100)
    with pytest.raises(SkillBudgetExceededError) as exc_info:
        r.check_can_activate("one-too-many", 100)
    assert "activation budget exhausted" in str(exc_info.value)


def test_registry_token_cap_raises():
    r = SkillActivationRegistry()
    # First load just under the cap.
    r.check_can_activate("a", HARD_CAP_CUMULATIVE_ACTIVATION_TOKENS - 100)
    r.record_activation("a", HARD_CAP_CUMULATIVE_ACTIVATION_TOKENS - 100)
    # The next load tips us over the cap, regardless of count.
    with pytest.raises(SkillBudgetExceededError) as exc_info:
        r.check_can_activate("b", 200)
    assert "activation token cap exhausted" in str(exc_info.value)


def test_registry_warn_threshold_logs_once(caplog):
    r = SkillActivationRegistry()
    with caplog.at_level("WARNING", logger="metis.core.skills.activation"):
        r.record_activation("a", WARN_CUMULATIVE_ACTIVATION_TOKENS // 2)
        r.record_activation("b", WARN_CUMULATIVE_ACTIVATION_TOKENS)
        r.record_activation("c", 100)
    # Threshold crossed by the second activation; the third doesn't
    # re-log.
    warnings = [rec for rec in caplog.records if "warn threshold" in rec.message]
    assert len(warnings) == 1


# ---- Pre-activation (§5.2.2) ------------------------------------------


async def test_pre_activation_events_fire_at_session_init(
    bus, workspace, small_skill_dir, event_log
):
    """Per v3 §5.2.2: one `skill.loaded(load_reason="always")` event
    per skill body inlined as v2 §5.1 padding, emitted at session init
    (no turn_id, no triggered_by_tool_use_id)."""
    adapter = _ScriptedAnthropicAdapter(
        [_ScriptedResponse(content=[TextBlock(text="ok")], stop_reason=StopReason.END_TURN)]
    )
    manager = _build_manager(
        bus,
        adapter,
        skill_store_factory=lambda ws: load_skills(global_dir=small_skill_dir, workspace_dir=None),
    )
    session = manager.create_session(workspace_path=str(workspace))
    await bus.drain()
    await bus.stop()
    loaded_events = [e for e in event_log if e.type == "skill.loaded"]
    # All three small bodies fit in the padding budget; each emits one
    # event.
    skill_ids = sorted(e.payload["skill_id"] for e in loaded_events)
    assert skill_ids == ["alpha", "beta", "gamma"]
    for event in loaded_events:
        assert event.payload["load_reason"] == "always"
        assert event.payload["triggered_by_tool_use_id"] is None
        assert event.session_id == session.id
        # Pre-activation stands outside any turn (§5.2.6 ordering).
        assert event.turn_id is None


async def test_pre_activation_populates_registry(bus, workspace, small_skill_dir):
    """The per-session registry mirrors the events: each inlined skill
    is `is_preloaded(name) == True`."""
    adapter = _ScriptedAnthropicAdapter(
        [_ScriptedResponse(content=[TextBlock(text="ok")], stop_reason=StopReason.END_TURN)]
    )
    manager = _build_manager(
        bus,
        adapter,
        skill_store_factory=lambda ws: load_skills(global_dir=small_skill_dir, workspace_dir=None),
    )
    session = manager.create_session(workspace_path=str(workspace))
    await bus.drain()
    await bus.stop()
    registry = manager.skill_activations_for(session.id)
    assert registry is not None
    assert registry.preloaded_names == frozenset({"alpha", "beta", "gamma"})
    # No explicit activations yet.
    assert registry.count == 0


async def test_pre_activation_index_annotation(bus, workspace, small_skill_dir):
    """The rendered discovery index annotates preloaded skills with
    `[preloaded]` per v3 §5.2.2."""
    adapter = _ScriptedAnthropicAdapter(
        [_ScriptedResponse(content=[TextBlock(text="ok")], stop_reason=StopReason.END_TURN)]
    )
    manager = _build_manager(
        bus,
        adapter,
        skill_store_factory=lambda ws: load_skills(global_dir=small_skill_dir, workspace_dir=None),
    )
    session = manager.create_session(workspace_path=str(workspace))
    prefix = manager.stable_system_prompt_for(session.id)
    assert "- alpha [preloaded]: first skill" in prefix
    assert "- beta [preloaded]: second skill" in prefix
    assert "- gamma [preloaded]: third skill" in prefix
    await bus.drain()
    await bus.stop()


async def test_no_pre_activation_when_skills_disabled(bus, workspace, event_log):
    """Sessions without a skill store emit no pre-activation events and
    their registry is empty."""
    adapter = _ScriptedAnthropicAdapter(
        [_ScriptedResponse(content=[TextBlock(text="ok")], stop_reason=StopReason.END_TURN)]
    )
    manager = _build_manager(bus, adapter, skill_store_factory=None)
    session = manager.create_session(workspace_path=str(workspace))
    await bus.drain()
    await bus.stop()
    assert not [e for e in event_log if e.type == "skill.loaded"]
    registry = manager.skill_activations_for(session.id)
    assert registry is not None  # always created
    assert registry.preloaded_names == frozenset()


# ---- skill_load for preloaded (§5.2.2) ---------------------------------


async def test_skill_load_returns_pointer_for_preloaded(bus, workspace, small_skill_dir, event_log):
    """`skill_load` on a pre-activated skill returns a pointer (not the
    body) with `already_preloaded=True` metadata. No new `skill.loaded`
    event fires."""
    adapter = _ScriptedAnthropicAdapter(
        [
            # Turn 1: agent calls skill_load("alpha"), then turn ends.
            _ScriptedResponse(
                content=[ToolUseBlock(id="tu_1", name="skill_load", input={"name": "alpha"})],
                stop_reason=StopReason.TOOL_USE,
            ),
            _ScriptedResponse(content=[TextBlock(text="done")], stop_reason=StopReason.END_TURN),
        ]
    )
    manager = _build_manager(
        bus,
        adapter,
        skill_store_factory=lambda ws: load_skills(global_dir=small_skill_dir, workspace_dir=None),
    )
    session = manager.create_session(workspace_path=str(workspace))
    # Snapshot pre-activation events fired at create_session.
    await bus.drain()
    pre_init_count = len([e for e in event_log if e.type == "skill.loaded"])
    assert pre_init_count == 3
    await manager.submit_turn(session.id, "load alpha")
    await bus.drain()
    await bus.stop()
    # No NEW skill.loaded event for the pre-activated skill.
    assert len([e for e in event_log if e.type == "skill.loaded"]) == pre_init_count
    # The skill_load dispatch surfaced as a successful tool.completed
    # (the dispatcher emits tool.completed keyed by tool_use_id).
    tool_completed = next(
        e
        for e in event_log
        if e.type == "tool.completed" and e.payload.get("tool_use_id") == "tu_1"
    )
    assert tool_completed.payload["success"] is True
    # The body lives in message history; find the tool result message.
    messages = manager._store.get_messages(session.id)
    tool_results = [
        b for m in messages for b in m.content if getattr(b, "tool_use_id", None) == "tu_1"
    ]
    assert tool_results
    pointer_text = "".join(getattr(b, "text", "") for tr in tool_results for b in tr.content)
    assert "already loaded in the system prompt" in pointer_text
    # The body itself is NOT in the tool_result (only the pointer).
    assert "alpha body content" not in pointer_text
    # The registry's explicit-activation count stays at 0 (preloaded
    # skills don't count against the explicit budget).
    registry = manager.skill_activations_for(session.id)
    assert registry.count == 0


# ---- skill_load explicit activation + budget (§5.2.3/§5.2.4) -----------


def _ws_skill_factory(skill_root: Path):
    return lambda ws: load_skills(global_dir=skill_root, workspace_dir=None)


def _make_huge_skill_dir(tmp_path: Path) -> Path:
    """Skills sized so only the first (name-ascending) gets inlined as
    v2 §5.1 padding — large enough that the truncated segment fills
    the entire padding budget with no room left for a second skill.
    The rest can only enter context via explicit `skill_load`.

    Each body is ~7875 heuristic tokens (~31.5K chars). The padding
    headroom is bounded by MAX_CACHEABLE_PREFIX_TOKENS ≈ 5500
    heuristic tokens (~22K chars), so a-big gets truncated to fit
    and no further skills are inlined (remaining < 200 chars).
    Three cumulative explicit activations sum to ~23.6K tokens,
    comfortably under the 30K-token hard cap, so the 4th explicit
    activation hits the count cap (`MAX_EXPLICIT_ACTIVATIONS_PER_SESSION`)
    before the token cap. We ship 5 skills so 4 candidates are
    non-preloaded — enough to exercise the count cap +1."""
    root = tmp_path / "skills"
    for name in ["a-big", "b-big", "c-big", "d-big", "e-big"]:
        body = f"# {name} body\n" + ("Substantive content. " * 1500)
        _make_skill_dir(root, name, f"description for {name}", body)
    return root


async def test_explicit_activation_records_in_registry(bus, workspace, tmp_path, event_log):
    """A successful `skill_load(name)` increments the registry's explicit
    count + cumulative tokens."""
    skill_root = _make_huge_skill_dir(tmp_path)
    adapter = _ScriptedAnthropicAdapter(
        [
            _ScriptedResponse(
                content=[ToolUseBlock(id="tu_1", name="skill_load", input={"name": "b-big"})],
                stop_reason=StopReason.TOOL_USE,
            ),
            _ScriptedResponse(content=[TextBlock(text="done")], stop_reason=StopReason.END_TURN),
        ]
    )
    manager = _build_manager(bus, adapter, skill_store_factory=_ws_skill_factory(skill_root))
    session = manager.create_session(workspace_path=str(workspace))
    # Sanity: b-big is NOT preloaded (only a-big fits in padding).
    registry = manager.skill_activations_for(session.id)
    assert "b-big" not in registry.preloaded_names
    await manager.submit_turn(session.id, "load b-big")
    await bus.drain()
    await bus.stop()
    assert registry.count == 1
    assert registry.is_activated("b-big")
    assert registry.cumulative_tokens > 0
    # Exactly one new skill.loaded event with on_demand reason.
    on_demand = [
        e
        for e in event_log
        if e.type == "skill.loaded" and e.payload.get("load_reason") == "on_demand"
    ]
    assert len(on_demand) == 1
    assert on_demand[0].payload["skill_id"] == "b-big"


async def test_reloading_same_skill_is_noop(bus, workspace, tmp_path, event_log):
    """Calling `skill_load` twice for the same skill returns a pointer on
    the second call, doesn't increment the budget, and doesn't emit a
    new event (v3 §5.2.7 q4)."""
    skill_root = _make_huge_skill_dir(tmp_path)
    adapter = _ScriptedAnthropicAdapter(
        [
            _ScriptedResponse(
                content=[ToolUseBlock(id="tu_1", name="skill_load", input={"name": "b-big"})],
                stop_reason=StopReason.TOOL_USE,
            ),
            _ScriptedResponse(
                content=[ToolUseBlock(id="tu_2", name="skill_load", input={"name": "b-big"})],
                stop_reason=StopReason.TOOL_USE,
            ),
            _ScriptedResponse(content=[TextBlock(text="done")], stop_reason=StopReason.END_TURN),
        ]
    )
    manager = _build_manager(bus, adapter, skill_store_factory=_ws_skill_factory(skill_root))
    session = manager.create_session(workspace_path=str(workspace))
    await manager.submit_turn(session.id, "load b-big twice")
    await bus.drain()
    await bus.stop()
    registry = manager.skill_activations_for(session.id)
    assert registry.count == 1  # second call didn't increment
    # Exactly one on_demand event fired across both calls.
    on_demand = [
        e
        for e in event_log
        if e.type == "skill.loaded" and e.payload.get("load_reason") == "on_demand"
    ]
    assert len(on_demand) == 1
    # The second tool_result is the already-loaded pointer.
    messages = manager._store.get_messages(session.id)
    second_results = [
        b for m in messages for b in m.content if getattr(b, "tool_use_id", None) == "tu_2"
    ]
    assert second_results
    pointer_text = "".join(getattr(b, "text", "") for tr in second_results for b in tr.content)
    assert "already loaded in the conversation history" in pointer_text


async def test_count_cap_raises_tool_failed(bus, workspace, tmp_path, event_log):
    """After `MAX_EXPLICIT_ACTIVATIONS_PER_SESSION` skills are loaded,
    the next `skill_load` for a new skill fails with `tool.failed`."""
    skill_root = _make_huge_skill_dir(tmp_path)
    # The padding pass inlines the first big skill (a-big) as
    # pre-activation; the remaining 4 are not preloaded so the count
    # cap path is the one we exercise.
    candidate_names = ["b-big", "c-big", "d-big", "e-big"]
    assert len(candidate_names) == MAX_EXPLICIT_ACTIVATIONS_PER_SESSION + 1
    adapter = _ScriptedAnthropicAdapter(
        [
            _ScriptedResponse(
                content=[
                    ToolUseBlock(
                        id=f"tu_{i}",
                        name="skill_load",
                        input={"name": name},
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            )
            for i, name in enumerate(candidate_names)
        ]
        + [_ScriptedResponse(content=[TextBlock(text="done")], stop_reason=StopReason.END_TURN)]
    )
    manager = _build_manager(bus, adapter, skill_store_factory=_ws_skill_factory(skill_root))
    session = manager.create_session(workspace_path=str(workspace))
    registry = manager.skill_activations_for(session.id)
    preloaded = registry.preloaded_names
    assert not any(n in preloaded for n in candidate_names), (
        f"none of {candidate_names} should be preloaded; got preloaded={preloaded}"
    )
    await manager.submit_turn(session.id, "load 4 big skills")
    await bus.drain()
    await bus.stop()
    assert registry.count == MAX_EXPLICIT_ACTIVATIONS_PER_SESSION
    # Exactly one tool.failed surfaces — the over-cap skill_load. The
    # event's error_message is redacted to the error class for
    # `ToolExecutionError` (is_user_visible=False per
    # tool-dispatcher.md §6.1), so we assert on error_class only.
    failed = [
        e
        for e in event_log
        if e.type == "tool.failed" and e.payload.get("error_class") == "execution_error"
    ]
    assert len(failed) == 1


# ---- Cross-turn persistence + byte-stability ---------------------------


async def test_activated_body_persists_across_turns(bus, workspace, tmp_path):
    """An explicitly-loaded skill body stays in message history for
    every subsequent turn — the model loop re-reads history each
    LLM call, so the body is available without re-activation."""
    skill_root = _make_huge_skill_dir(tmp_path)
    adapter = _ScriptedAnthropicAdapter(
        [
            # Turn 1: load skill, get body in tool_result.
            _ScriptedResponse(
                content=[ToolUseBlock(id="tu_1", name="skill_load", input={"name": "b-big"})],
                stop_reason=StopReason.TOOL_USE,
            ),
            _ScriptedResponse(
                content=[TextBlock(text="done loading")], stop_reason=StopReason.END_TURN
            ),
            # Turn 2: simple response. The body from turn 1 is still in
            # the messages list sent to the adapter (history is replayed
            # whole per session-manager spec).
            _ScriptedResponse(content=[TextBlock(text="ack")], stop_reason=StopReason.END_TURN),
        ]
    )
    manager = _build_manager(bus, adapter, skill_store_factory=_ws_skill_factory(skill_root))
    session = manager.create_session(workspace_path=str(workspace))
    await manager.submit_turn(session.id, "load b-big")
    await manager.submit_turn(session.id, "tell me something")
    await bus.drain()
    await bus.stop()
    # Turn 2's request must include the tool_result with the b-big body.
    turn2_request = adapter.requests[-1]
    found = False
    for msg in turn2_request.messages:
        for block in msg.content:
            text = getattr(block, "text", "") or "".join(
                getattr(c, "text", "") for c in getattr(block, "content", []) or []
            )
            if "b-big" in text and "Substantive content." in text:
                found = True
    assert found, "explicitly-activated skill body must survive into turn 2"


async def test_stable_prefix_byte_stable_across_turns(bus, workspace, small_skill_dir):
    """The cached stable prefix must be byte-identical across LLM calls
    within a session, or the provider's cache_control marker won't fire
    a hit. The pre-activation pipeline cannot churn the prefix."""
    adapter = _ScriptedAnthropicAdapter(
        [
            _ScriptedResponse(
                content=[TextBlock(text="ok turn 1")], stop_reason=StopReason.END_TURN
            ),
            _ScriptedResponse(
                content=[TextBlock(text="ok turn 2")], stop_reason=StopReason.END_TURN
            ),
            _ScriptedResponse(
                content=[TextBlock(text="ok turn 3")], stop_reason=StopReason.END_TURN
            ),
        ]
    )
    manager = _build_manager(
        bus,
        adapter,
        skill_store_factory=lambda ws: load_skills(global_dir=small_skill_dir, workspace_dir=None),
    )
    session = manager.create_session(workspace_path=str(workspace))
    await manager.submit_turn(session.id, "t1")
    await manager.submit_turn(session.id, "t2")
    await manager.submit_turn(session.id, "t3")
    await bus.drain()
    await bus.stop()
    prompts = [r.system_prompt for r in adapter.requests]
    assert len(prompts) == 3
    assert prompts[0] == prompts[1] == prompts[2]
    # And the volatile half (USER.md / MEMORY.md) stays empty when no
    # memory store is configured — bytes flow into the stable side
    # only.
    assert all(r.system_prompt_volatile is None for r in adapter.requests)
