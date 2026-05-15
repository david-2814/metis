"""Bus subscriber tests for `PatternEventSubscriber`.

Records outcomes when `route.decided` + `turn.completed` pair up; patches
score on `eval.completed`; emits `pattern.recorded` and `pattern.evicted`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from metis_core.events.bus import EventBus, EventFilter, Subscription
from metis_core.events.envelope import Actor, Event
from metis_core.events.payloads import (
    EvalCompleted,
    RouteDecided,
    ToolCompleted,
    TurnCompleted,
    make_event,
)
from metis_core.patterns.fingerprint import FingerprintInputs
from metis_core.patterns.store import PatternStore
from metis_core.patterns.subscriber import PatternEventSubscriber


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


def _fingerprint_inputs(workspace_path: str) -> FingerprintInputs:
    return FingerprintInputs(
        user_message_text="refactor",
        workspace_path=workspace_path,
        estimated_input_tokens=1000,
        has_images=False,
        has_tool_calls_in_history=False,
        file_extensions=(".py",),
        file_path_buckets=("src",),
        tool_names=("read_file",),
        side_effect_classes=("read",),
    )


def _emit_route(bus: EventBus, *, session_id: str, turn_id: str, model: str) -> Event:
    event = make_event(
        type="route.decided",
        session_id=session_id,
        turn_id=turn_id,
        actor=Actor.SYSTEM,
        payload=RouteDecided(
            chosen_model=model,
            winner_index=0,
            elapsed_ms=1.0,
            chain=[],
        ),
        timestamp=datetime.now(UTC),
    )
    bus.emit(event)
    return event


def _emit_turn_completed(bus: EventBus, *, session_id: str, turn_id: str, cost: float) -> Event:
    event = make_event(
        type="turn.completed",
        session_id=session_id,
        turn_id=turn_id,
        actor=Actor.SYSTEM,
        payload=TurnCompleted(
            stop_reason="end_turn",
            llm_call_count=1,
            tool_call_count=0,
            total_input_tokens=100,
            total_output_tokens=50,
            total_cost_usd=cost,
            wall_time_seconds=1.0,
        ),
        timestamp=datetime.now(UTC),
    )
    bus.emit(event)
    return event


async def test_subscriber_records_outcome_on_turn_completed(bus, event_log, tmp_path: Path) -> None:
    store = PatternStore(tmp_path)
    try:
        subscriber = PatternEventSubscriber(
            store_factory=lambda _ws: store,
            workspace_resolver=lambda _sid: str(tmp_path),
            bus=bus,
        )
        subscriber.attach()
        session_id = "sess_x"
        turn_id = "turn_x"
        subscriber.set_fingerprint_inputs(turn_id, _fingerprint_inputs(str(tmp_path)))
        _emit_route(bus, session_id=session_id, turn_id=turn_id, model="m_a")
        _emit_turn_completed(bus, session_id=session_id, turn_id=turn_id, cost=0.01)
        await bus.drain()
        # The subscriber emits pattern.recorded inside the turn.completed
        # handler; a second drain ensures the follow-on event has been
        # dispatched to the log subscriber.
        await bus.drain()
        assert store.size().outcomes == 1
        recorded = [e for e in event_log if e.type == "pattern.recorded"]
        assert len(recorded) == 1
        payload = recorded[0].payload
        assert payload["primary_model"] == "m_a"
        assert payload["was_new_fingerprint"] is True
    finally:
        store.close()


async def test_eval_completed_patches_score(bus, event_log, tmp_path: Path) -> None:
    store = PatternStore(tmp_path)
    try:
        subscriber = PatternEventSubscriber(
            store_factory=lambda _ws: store,
            workspace_resolver=lambda _sid: str(tmp_path),
            bus=bus,
        )
        subscriber.attach()
        session_id = "sess_y"
        turn_id = "turn_y"
        subscriber.set_fingerprint_inputs(turn_id, _fingerprint_inputs(str(tmp_path)))
        _emit_route(bus, session_id=session_id, turn_id=turn_id, model="m_a")
        _emit_turn_completed(bus, session_id=session_id, turn_id=turn_id, cost=0.01)
        await bus.drain()
        # The subscriber emits pattern.recorded inside the turn.completed
        # handler; a second drain ensures the follow-on event has been
        # dispatched to the log subscriber.
        await bus.drain()
        # Now an eval.completed lands for the same turn.
        eval_evt = make_event(
            type="eval.completed",
            session_id=session_id,
            turn_id=turn_id,
            actor=Actor.SYSTEM,
            payload=EvalCompleted(
                eval_id="eval_1",
                subject_kind="turn",
                subject_id=turn_id,
                score=0.9,
                confidence=0.8,
                judge_kind="heuristic",
                judge_cost_usd=Decimal("0"),
                judge_latency_ms=10,
                rubric_id="default",
                rubric_version="v1",
                signals={},
            ),
            timestamp=datetime.now(UTC),
        )
        bus.emit(eval_evt)
        await bus.drain()
        # The outcome's success_score_mean should reflect the score.
        from metis_core.patterns.fingerprint import (
            build_structural_features,
            structural_signature,
        )

        sig = structural_signature(build_structural_features(_fingerprint_inputs(str(tmp_path))))
        fp_id = store._lookup_fingerprint_by_sig(sig, None)
        row = store._lookup_outcome(fp_id, "m_a")
        assert row is not None
        assert row["success_score_mean"] == pytest.approx(0.9)
        assert row["success_score_count"] == 1
    finally:
        store.close()


async def test_fingerprint_inputs_hook_records_distinct_signatures_per_turn(
    bus, event_log, tmp_path: Path
) -> None:
    """Producer-side plumbing: the SessionManager's `fingerprint_inputs_hook`
    forwards each turn's `TurnContext.user_message_text` to the pattern
    subscriber via `set_fingerprint_inputs`. Two turns with substantively
    different user messages (refactor vs debug) must record distinct
    structural fingerprints, not collapse into a single empty-intent
    cluster as the pre-hook codepath did."""
    from metis_core.adapters.protocol import StopReason
    from metis_core.canonical.content import TextBlock
    from metis_core.patterns.fingerprint import (
        build_structural_features,
        structural_signature,
    )
    from metis_core.pricing import DEFAULT_PRICE_TABLE
    from metis_core.routing import ModelRegistry, RoutingEngine
    from metis_core.sessions import InMemorySessionStore, SessionManager
    from metis_core.tools.dispatcher import ToolDispatcher

    from tests_shared.scripted_adapter import (
        _ScriptedAnthropicAdapter,
        _ScriptedResponse,
    )

    adapter = _ScriptedAnthropicAdapter(
        [
            _ScriptedResponse(
                content=[TextBlock(text="ok")],
                stop_reason=StopReason.END_TURN,
            ),
            _ScriptedResponse(
                content=[TextBlock(text="ok")],
                stop_reason=StopReason.END_TURN,
            ),
        ]
    )
    registry = ModelRegistry()
    registry.register(model_id="anthropic:claude-sonnet-4-6", adapter=adapter, aliases=["sonnet"])
    routing = RoutingEngine(registry=registry, bus=bus)
    dispatcher = ToolDispatcher(bus)

    store = PatternStore(tmp_path)
    try:
        subscriber = PatternEventSubscriber(
            store_factory=lambda _ws: store,
            workspace_resolver=lambda _sid: str(tmp_path),
            bus=bus,
        )
        subscriber.attach()

        def _builder(ctx) -> FingerprintInputs:
            return FingerprintInputs(
                user_message_text=ctx.user_message_text,
                workspace_path=ctx.workspace_path,
                estimated_input_tokens=ctx.estimated_input_tokens,
                has_images=ctx.has_images,
                has_tool_calls_in_history=ctx.has_tool_calls_in_history,
            )

        def _hook(turn_id: str, ctx) -> None:
            subscriber.set_fingerprint_inputs(turn_id, _builder(ctx))

        manager = SessionManager(
            registry=registry,
            routing=routing,
            dispatcher=dispatcher,
            bus=bus,
            store=InMemorySessionStore(),
            pricing=DEFAULT_PRICE_TABLE,
            fingerprint_inputs_hook=_hook,
        )
        session = manager.create_session(workspace_path=str(tmp_path))
        await manager.submit_turn(session.id, "refactor this function")
        await bus.drain()
        await bus.drain()

        session_2 = manager.create_session(workspace_path=str(tmp_path))
        await manager.submit_turn(session_2.id, "debug the failing test")
        await bus.drain()
        await bus.drain()

        recorded = [e for e in event_log if e.type == "pattern.recorded"]
        assert len(recorded) == 2
        fp_ids = {e.payload["fingerprint_id"] for e in recorded}
        assert len(fp_ids) == 2, (
            f"two substantively different prompts should produce two fingerprints; got {fp_ids!r}"
        )

        refactor_sig = structural_signature(
            build_structural_features(
                FingerprintInputs(
                    user_message_text="refactor this function",
                    workspace_path=str(tmp_path),
                    estimated_input_tokens=0,
                    has_images=False,
                    has_tool_calls_in_history=False,
                )
            )
        )
        debug_sig = structural_signature(
            build_structural_features(
                FingerprintInputs(
                    user_message_text="debug the failing test",
                    workspace_path=str(tmp_path),
                    estimated_input_tokens=0,
                    has_images=False,
                    has_tool_calls_in_history=False,
                )
            )
        )
        assert refactor_sig != debug_sig
    finally:
        store.close()


async def test_submit_turn_workload_id_flows_into_pattern_recorded(
    bus, event_log, tmp_path: Path
) -> None:
    """End-to-end: a `workload_id` passed to `SessionManager.submit_turn`
    flows through `TurnContext`, the `fingerprint_inputs_hook`, and the
    pattern subscriber so the recorded fingerprint reflects it. Two turns
    that differ only by workload_id produce two distinct fingerprint ids,
    not a single deduped row."""
    from metis_core.adapters.protocol import StopReason
    from metis_core.canonical.content import TextBlock
    from metis_core.patterns.fingerprint import (
        build_structural_features,
        structural_signature,
    )
    from metis_core.pricing import DEFAULT_PRICE_TABLE
    from metis_core.routing import ModelRegistry, RoutingEngine
    from metis_core.sessions import InMemorySessionStore, SessionManager
    from metis_core.tools.dispatcher import ToolDispatcher

    from tests_shared.scripted_adapter import (
        _ScriptedAnthropicAdapter,
        _ScriptedResponse,
    )

    adapter = _ScriptedAnthropicAdapter(
        [
            _ScriptedResponse(
                content=[TextBlock(text="ok")],
                stop_reason=StopReason.END_TURN,
            ),
            _ScriptedResponse(
                content=[TextBlock(text="ok")],
                stop_reason=StopReason.END_TURN,
            ),
        ]
    )
    registry = ModelRegistry()
    registry.register(model_id="anthropic:claude-sonnet-4-6", adapter=adapter, aliases=["sonnet"])
    routing = RoutingEngine(registry=registry, bus=bus)
    dispatcher = ToolDispatcher(bus)

    store = PatternStore(tmp_path)
    try:
        subscriber = PatternEventSubscriber(
            store_factory=lambda _ws: store,
            workspace_resolver=lambda _sid: str(tmp_path),
            bus=bus,
        )
        subscriber.attach()

        def _builder(ctx) -> FingerprintInputs:
            return FingerprintInputs(
                user_message_text=ctx.user_message_text,
                workspace_path=ctx.workspace_path,
                estimated_input_tokens=ctx.estimated_input_tokens,
                has_images=ctx.has_images,
                has_tool_calls_in_history=ctx.has_tool_calls_in_history,
                workload_id=getattr(ctx, "workload_id", None),
            )

        def _hook(turn_id: str, ctx) -> None:
            subscriber.set_fingerprint_inputs(turn_id, _builder(ctx))

        manager = SessionManager(
            registry=registry,
            routing=routing,
            dispatcher=dispatcher,
            bus=bus,
            store=InMemorySessionStore(),
            pricing=DEFAULT_PRICE_TABLE,
            fingerprint_inputs_hook=_hook,
        )

        # Two sessions with the *same* user prompt but different workload_ids.
        # Without workload_id plumbing they'd produce the same structural
        # signature and dedup into one row.
        session_a = manager.create_session(workspace_path=str(tmp_path))
        await manager.submit_turn(session_a.id, "same prompt", workload_id="foo")
        await bus.drain()
        await bus.drain()

        session_b = manager.create_session(workspace_path=str(tmp_path))
        await manager.submit_turn(session_b.id, "same prompt", workload_id="bar")
        await bus.drain()
        await bus.drain()

        recorded = [e for e in event_log if e.type == "pattern.recorded"]
        assert len(recorded) == 2
        fp_ids = {e.payload["fingerprint_id"] for e in recorded}
        assert len(fp_ids) == 2, f"two workload_ids should produce two fingerprints; got {fp_ids!r}"

        foo_sig = structural_signature(
            build_structural_features(
                FingerprintInputs(
                    user_message_text="same prompt",
                    workspace_path=str(tmp_path),
                    estimated_input_tokens=0,
                    has_images=False,
                    has_tool_calls_in_history=False,
                    workload_id="foo",
                )
            )
        )
        bar_sig = structural_signature(
            build_structural_features(
                FingerprintInputs(
                    user_message_text="same prompt",
                    workspace_path=str(tmp_path),
                    estimated_input_tokens=0,
                    has_images=False,
                    has_tool_calls_in_history=False,
                    workload_id="bar",
                )
            )
        )
        assert foo_sig != bar_sig
    finally:
        store.close()


async def test_one_turn_with_multiple_tool_calls_lands_eval_score_after_bus_stop(
    bus, tmp_path: Path
) -> None:
    """Regression for the §A3-rev3 outcome-update bug on multi-tool 1-turn
    workloads (benchmarks/RESULTS.md "A3-rev3 caveats and observations").

    `architectural-explanation-without-hallucination` (1 turn, 3-20 tool
    calls) reproducibly landed `success_score_count = 0` on its outcome
    rows even though the `eval.completed kind=turn` event was durable in
    the trace DB. `intentionally-failing-task` (1 turn, 0 tool calls)
    accumulated correctly. The differentiator: multi-tool 1-turn turns
    have several `tool.completed` events firing through the evaluator's
    bus subscription concurrently with the turn-level cascade, and the
    pre-fix `shutdown_runtime` order — detach subscribers *then* drain —
    left some `eval.completed` events dispatched to no-subscribers when
    the caller hadn't drained first. The fix in
    `apps/cli/src/metis_cli/runtime.py:shutdown_runtime` drains before
    detaching; this test pins the invariant at the bus level.

    Asserts: after `bus.drain() → unregister → detach → bus.stop()`,
    the outcome row's `success_score_count >= 1`. A future regression
    that re-orders detach-before-drain, or that leaves a cascade level
    out of `bus.drain()`'s loop, will produce 0.
    """
    from metis_core.eval import register_evaluator
    from metis_core.patterns.fingerprint import (
        build_structural_features,
        structural_signature,
    )
    from metis_core.trace.store import TraceStore

    trace_db = tmp_path / "trace.db"
    trace = TraceStore(trace_db)
    trace_handle = trace.attach_to(bus, name="trace-store")
    store = PatternStore(tmp_path)
    try:
        subscriber = PatternEventSubscriber(
            store_factory=lambda _ws: store,
            workspace_resolver=lambda _sid: str(tmp_path),
            bus=bus,
        )
        subscriber.attach()
        evaluator, _ = register_evaluator(bus, trace)

        session_id = "sess_multi_tool"
        turn_id = "turn_multi_tool"
        subscriber.set_fingerprint_inputs(turn_id, _fingerprint_inputs(str(tmp_path)))
        _emit_route(bus, session_id=session_id, turn_id=turn_id, model="m_a")
        for tool_use_id in (
            "toolu_aaa",
            "toolu_bbb",
            "toolu_ccc",
            "toolu_ddd",
            "toolu_eee",
            "toolu_fff",
        ):
            bus.emit(
                make_event(
                    type="tool.completed",
                    session_id=session_id,
                    turn_id=turn_id,
                    actor=Actor.TOOL,
                    payload=ToolCompleted(
                        tool_use_id=tool_use_id,
                        success=True,
                        output_size_bytes=128,
                        latency_ms=5,
                    ),
                    timestamp=datetime.now(UTC),
                )
            )
        _emit_turn_completed(bus, session_id=session_id, turn_id=turn_id, cost=0.04)

        # Mirror the fixed `shutdown_runtime` ordering: drain *first* so any
        # in-flight evaluator task (whose `eval.completed` cascades into the
        # pattern subscriber) finishes while subscribers are still attached.
        # The fixture takes care of `bus.stop()` during teardown; reading the
        # outcome row here verifies the score landed before stop would run.
        await bus.drain()
        evaluator.unregister()
        subscriber.detach()
        bus.unsubscribe(trace_handle)

        sig = structural_signature(build_structural_features(_fingerprint_inputs(str(tmp_path))))
        fp_id = store._lookup_fingerprint_by_sig(sig, None)
        assert fp_id is not None
        row = store._lookup_outcome(fp_id, "m_a")
        assert row is not None
        assert row["success_score_count"] >= 1, (
            f"multi-tool 1-turn cascade lost the eval score "
            f"(success_score_count={row['success_score_count']}); the "
            f"§A3-rev3 architectural-explanation-without-hallucination caveat "
            f"is back. Check shutdown ordering and bus.drain() cascade."
        )
    finally:
        store.close()
        trace.close()


async def test_drain_processes_eval_completed_cascade_before_returning(
    bus, event_log, tmp_path: Path
) -> None:
    """Regression for the §A3-rev3 outcome-update bug.

    With both the evaluator and the pattern subscriber attached, a single
    `turn.completed` event sets off a cascade: pattern subscriber records
    an outcome row (and emits `pattern.recorded`), evaluator emits
    `eval.started` + `eval.completed`, and pattern subscriber's
    `_on_eval_completed` handler applies the score via `update_score`.

    The cascade lives in handler tasks emitting new events. `bus.drain()`
    has to await *every level* of that cascade — not just the first wave
    of in-flight tasks — or callers that detach subscribers immediately
    after drain (as `shutdown_runtime` does) drop the score before it
    lands on the outcome row. Symptom in the wild:
    `success_score_count = 0` on a `pattern.recorded` row whose matching
    `eval.completed` is durable in the trace DB
    (see [`benchmarks/RESULTS.md §A3-rev3 caveats`](../../benchmarks/RESULTS.md)).
    """
    from metis_core.eval import register_evaluator
    from metis_core.patterns.fingerprint import (
        build_structural_features,
        structural_signature,
    )
    from metis_core.trace.store import TraceStore

    trace_db = tmp_path / "trace.db"
    trace = TraceStore(trace_db)
    trace_handle = trace.attach_to(bus, name="trace-store")
    store = PatternStore(tmp_path)
    try:
        subscriber = PatternEventSubscriber(
            store_factory=lambda _ws: store,
            workspace_resolver=lambda _sid: str(tmp_path),
            bus=bus,
        )
        subscriber.attach()
        evaluator, _ = register_evaluator(bus, trace)

        session_id = "sess_cascade"
        turn_id = "turn_cascade"
        subscriber.set_fingerprint_inputs(turn_id, _fingerprint_inputs(str(tmp_path)))
        _emit_route(bus, session_id=session_id, turn_id=turn_id, model="m_a")
        _emit_turn_completed(bus, session_id=session_id, turn_id=turn_id, cost=0.01)

        # One drain call must walk the full cascade: pattern.recorded +
        # eval.started + eval.completed + the eval.completed handler that
        # writes update_score. After this returns we detach immediately
        # (mirroring `shutdown_runtime` in apps/cli/.../runtime.py).
        await bus.drain()
        evaluator.unregister()
        subscriber.detach()

        # The outcome row must carry the eval score. If drain returned
        # before the cascading eval.completed reached `_on_eval_completed`,
        # `success_score_count` stays at 0.
        sig = structural_signature(build_structural_features(_fingerprint_inputs(str(tmp_path))))
        fp_id = store._lookup_fingerprint_by_sig(sig, None)
        row = store._lookup_outcome(fp_id, "m_a")
        assert row is not None
        assert row["success_score_count"] >= 1, (
            f"eval.completed score never reached the outcome row "
            f"(success_score_count={row['success_score_count']}); "
            f"drain() likely returned before the cascade finished."
        )
    finally:
        bus.unsubscribe(trace_handle)
        store.close()
        trace.close()
