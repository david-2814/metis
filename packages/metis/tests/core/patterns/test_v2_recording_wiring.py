"""Wave 11: v2 recording path lands HYBRID fingerprints, not STRUCTURAL.

Regressions for the §A3-rev4 Q1 partial-wiring gap. The Wave-10
implementation populated the embedding cache AFTER `store.record()`, so the
recorded fingerprint row stayed STRUCTURAL and routing-time K-NN fell back
to v1 weighted-Jaccard via the mixed-version detection path. The fix moves
embedding computation to turn-start (the SessionManager's
`fingerprint_inputs_hook`), so by the time `turn.completed` fires the
inputs already carry the embedding and `compute_fingerprint` produces a
HYBRID row.

Tests cover three invariants:

  1. Async fingerprint_inputs_hook is awaited by the SessionManager (the
     manager-side support that makes the runtime hook viable).
  2. Recording end-to-end under v2 lands `schema_version=2` rows with
     `embedding_blob` populated (HYBRID), not STRUCTURAL.
  3. The per-turn eval cascade (turn.completed → eval.completed →
     update_score) still succeeds: `success_score_count >= 1`. The Wave-10
     cascade fix (drain before unregister; bus.stop drain ordering) is
     unaffected.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from metis.core.adapters.protocol import StopReason
from metis.core.canonical.content import TextBlock
from metis.core.events.bus import EventBus
from metis.core.events.envelope import Actor, Event
from metis.core.events.payloads import (
    RouteDecided,
    TurnCompleted,
    make_event,
)
from metis.core.patterns.embeddings import DeterministicEmbeddingProvider
from metis.core.patterns.fingerprint import (
    FingerprintInputs,
    attach_embedding_for_recording,
    build_structural_features,
    structural_signature,
)
from metis.core.patterns.store import PatternStore
from metis.core.patterns.subscriber import PatternEventSubscriber
from metis.core.pricing import DEFAULT_PRICE_TABLE
from metis.core.routing import ModelRegistry, RoutingEngine
from metis.core.sessions import InMemorySessionStore, SessionManager
from metis.core.tools.dispatcher import ToolDispatcher

from tests_shared.scripted_adapter import (
    _ScriptedAnthropicAdapter,
    _ScriptedResponse,
)


@pytest.fixture
async def bus() -> EventBus:
    b = EventBus()
    b.start()
    yield b
    await b.stop()


def _inputs_for_ctx(ctx) -> FingerprintInputs:
    return FingerprintInputs(
        user_message_text=ctx.user_message_text,
        workspace_path=ctx.workspace_path,
        estimated_input_tokens=ctx.estimated_input_tokens,
        has_images=ctx.has_images,
        has_tool_calls_in_history=ctx.has_tool_calls_in_history,
        workload_id=getattr(ctx, "workload_id", None),
    )


async def test_async_fingerprint_inputs_hook_is_awaited_by_session_manager(
    bus, tmp_path: Path
) -> None:
    """SessionManager must `await` a hook that returns an awaitable.

    Pre-Wave-11 the hook was sync-only — the v2 wiring needed to attach an
    embedding asynchronously at turn start, so the hook signature was
    extended to accept `Callable[..., Awaitable[None] | None]`. This test
    pins the manager's `inspect.isawaitable` branch: an async hook that
    sleeps for one event-loop tick must complete before turn.started fires.
    """
    import asyncio

    adapter = _ScriptedAnthropicAdapter(
        [
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

    completed_before_turn_started = [False]
    turn_started_seen = [False]

    async def _hook(turn_id: str, ctx) -> None:
        # Yield to the event loop several times to make the await visible.
        for _ in range(5):
            await asyncio.sleep(0)
        if not turn_started_seen[0]:
            completed_before_turn_started[0] = True

    async def _track_turn_started(e: Event) -> None:
        if e.type == "turn.started":
            turn_started_seen[0] = True

    from metis.core.events.bus import EventFilter, Subscription

    bus.subscribe(
        Subscription(
            filter=EventFilter(event_types=frozenset({"turn.started"})),
            handler=_track_turn_started,
            name="track-turn-started",
            fast_path=True,
        )
    )

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
    await manager.submit_turn(session.id, "test message")
    await bus.drain()

    assert completed_before_turn_started[0], (
        "async hook must complete before turn.started fires; "
        "SessionManager did not await the coroutine"
    )


async def test_v2_recording_writes_hybrid_fingerprint_row(bus, tmp_path: Path) -> None:
    """End-to-end: a v2-config session records a HYBRID fingerprint row on
    disk. The `fingerprints` table row has `kind='hybrid'`, non-NULL
    `embedding_blob`, the correct `embedding_provider`, and the right
    `embedding_dim`. Pre-Wave-11 this row was always STRUCTURAL because the
    embedding was attached AFTER `store.record()`.
    """
    adapter = _ScriptedAnthropicAdapter(
        [
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

    store = PatternStore(tmp_path, fingerprint_version="v2", embedding_alpha=0.6)
    embedder = DeterministicEmbeddingProvider(dim=16)
    try:
        subscriber = PatternEventSubscriber(
            store_factory=lambda _ws: store,
            workspace_resolver=lambda _sid: str(tmp_path),
            bus=bus,
        )
        subscriber.attach()

        async def _hook(turn_id: str, ctx) -> None:
            inputs = _inputs_for_ctx(ctx)
            if inputs.user_message_text:
                inputs = await attach_embedding_for_recording(
                    inputs, store=store, embedder=embedder
                )
            subscriber.set_fingerprint_inputs(turn_id, inputs)

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
        await manager.submit_turn(session.id, "refactor the auth module")
        await bus.drain()
        await bus.drain()

        # Inspect SQL directly to verify the row carries the embedding.
        row = store._conn.execute(
            """
            SELECT kind, embedding_blob, embedding_provider, embedding_dim
            FROM fingerprints
            """
        ).fetchone()
        assert row is not None, "no fingerprint row recorded"
        kind, embedding_blob, embedding_provider, embedding_dim = row
        assert kind == "hybrid", (
            f"expected HYBRID fingerprint kind (Wave 11 fix); got {kind!r}. "
            "This is the §A3-rev4 Q1 regression — embedding is being attached "
            "AFTER store.record() and the row stays STRUCTURAL."
        )
        assert embedding_blob is not None, "embedding_blob must be populated"
        assert embedding_provider == "deterministic:sha256:64", embedding_provider
        assert embedding_dim == 16

        # schema_version is "2" (set on every v2 store init).
        meta_row = store._conn.execute(
            "SELECT value FROM store_meta WHERE key='schema_version'"
        ).fetchone()
        assert meta_row[0] == "2"
    finally:
        subscriber.detach()
        store.close()


async def test_v2_recording_preserves_eval_cascade_score_update(bus, tmp_path: Path) -> None:
    """Regression for the §A3-rev4 caveat the Wave-11 fix must NOT regress.

    The §A3-rev3 outcome-update bug fix relied on:
      - `_turn_outcomes[turn_id]` being set before any await in the
        pattern subscriber's `_on_turn_completed`,
      - `shutdown_runtime` draining the bus before detaching subscribers.

    The Wave-11 v2 fix moves embedding compute earlier (turn start, not
    post-record), so the recording-time path becomes purely synchronous
    and `_turn_outcomes` is set without any intermediate await. This test
    pins the invariant: after a v2 turn + a synthetic eval.completed event,
    the outcome row's `success_score_count >= 1`.
    """
    from metis.core.eval import register_evaluator
    from metis.core.trace.store import TraceStore

    trace_db = tmp_path / "trace.db"
    trace = TraceStore(trace_db)
    trace_handle = trace.attach_to(bus, name="trace-store")

    store = PatternStore(tmp_path, fingerprint_version="v2", embedding_alpha=0.6)
    embedder = DeterministicEmbeddingProvider(dim=16)
    try:
        subscriber = PatternEventSubscriber(
            store_factory=lambda _ws: store,
            workspace_resolver=lambda _sid: str(tmp_path),
            bus=bus,
        )
        subscriber.attach()
        evaluator, _ = register_evaluator(bus, trace)

        session_id = "sess_v2_cascade"
        turn_id = "turn_v2_cascade"

        # Mimic the runtime hook: precompute the embedding, then call
        # set_fingerprint_inputs with the embedded inputs.
        inputs = FingerprintInputs(
            user_message_text="refactor the auth flow",
            workspace_path=str(tmp_path),
            estimated_input_tokens=1_000,
            has_images=False,
            has_tool_calls_in_history=False,
            file_extensions=(".py",),
            file_path_buckets=("src",),
            tool_names=("read_file",),
            side_effect_classes=("read",),
        )
        inputs = await attach_embedding_for_recording(inputs, store=store, embedder=embedder)
        subscriber.set_fingerprint_inputs(turn_id, inputs)

        bus.emit(
            make_event(
                type="route.decided",
                session_id=session_id,
                turn_id=turn_id,
                actor=Actor.SYSTEM,
                payload=RouteDecided(
                    chosen_model="m_a",
                    winner_index=0,
                    elapsed_ms=1.0,
                    chain=[],
                ),
                timestamp=datetime.now(UTC),
            )
        )
        bus.emit(
            make_event(
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
                    total_cost_usd=0.01,
                    wall_time_seconds=1.0,
                ),
                timestamp=datetime.now(UTC),
            )
        )

        # One drain walks the full cascade: pattern.recorded + the
        # evaluator's eval.completed + the subscriber's _on_eval_completed
        # update_score.
        await bus.drain()
        evaluator.unregister()
        subscriber.detach()

        # Verify: the recorded row is HYBRID and the cascade landed.
        sig = structural_signature(build_structural_features(inputs))
        fp_id = store._lookup_fingerprint_by_sig(sig, "deterministic:sha256:64")
        assert fp_id is not None, (
            "no HYBRID fingerprint row found under the embedder's provider_id; "
            "Wave-11 fix did not produce a hybrid row"
        )
        # Cross-check the row's kind via SQL.
        fp_row = store._conn.execute(
            "SELECT kind FROM fingerprints WHERE id = ?", (fp_id,)
        ).fetchone()
        assert fp_row[0] == "hybrid"

        row = store._lookup_outcome(fp_id, "m_a")
        assert row is not None
        assert row["success_score_count"] >= 1, (
            f"eval cascade did not land on the v2 outcome row "
            f"(success_score_count={row['success_score_count']}); "
            f"Wave-11 fix regressed the §A3-rev3 cascade invariant."
        )
    finally:
        bus.unsubscribe(trace_handle)
        store.close()
        trace.close()


async def test_v2_recording_without_hook_falls_back_to_structural(bus, tmp_path: Path) -> None:
    """Defensive: if the hook isn't wired (or it fails), the recording path
    falls back to STRUCTURAL — it does NOT block the turn and does NOT
    leak an exception. This is the documented degradation mode in
    runtime.py's hook implementation.
    """
    store = PatternStore(tmp_path, fingerprint_version="v2", embedding_alpha=0.6)
    try:
        subscriber = PatternEventSubscriber(
            store_factory=lambda _ws: store,
            workspace_resolver=lambda _sid: str(tmp_path),
            bus=bus,
        )
        subscriber.attach()

        # Inputs without embedding — simulating a hook that didn't precompute
        # (e.g., embedder failed, or the workspace didn't opt into v2).
        inputs = FingerprintInputs(
            user_message_text="some message",
            workspace_path=str(tmp_path),
            estimated_input_tokens=100,
            has_images=False,
            has_tool_calls_in_history=False,
        )
        subscriber.set_fingerprint_inputs("turn_no_emb", inputs)

        bus.emit(
            make_event(
                type="route.decided",
                session_id="s",
                turn_id="turn_no_emb",
                actor=Actor.SYSTEM,
                payload=RouteDecided(
                    chosen_model="m",
                    winner_index=0,
                    elapsed_ms=1.0,
                    chain=[],
                ),
                timestamp=datetime.now(UTC),
            )
        )
        bus.emit(
            make_event(
                type="turn.completed",
                session_id="s",
                turn_id="turn_no_emb",
                actor=Actor.SYSTEM,
                payload=TurnCompleted(
                    stop_reason="end_turn",
                    llm_call_count=1,
                    tool_call_count=0,
                    total_input_tokens=10,
                    total_output_tokens=10,
                    total_cost_usd=0.001,
                    wall_time_seconds=0.5,
                ),
                timestamp=datetime.now(UTC),
            )
        )
        await bus.drain()
        await bus.drain()

        row = store._conn.execute("SELECT kind, embedding_blob FROM fingerprints").fetchone()
        assert row is not None
        assert row[0] == "structural"
        assert row[1] is None
    finally:
        subscriber.detach()
        store.close()
