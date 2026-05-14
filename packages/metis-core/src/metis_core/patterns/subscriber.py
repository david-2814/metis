"""Bus subscriber that records pattern outcomes from `turn.completed`.

Per `pattern-store.md §15.3`: outcomes are recorded asynchronously off the
fast event path. The subscriber consumes `turn.completed` (cost + latency)
and `route.decided` (chosen model + chain) for the same turn to build the
fingerprint + outcome row, then writes via `PatternStore.record()`.

When `eval.completed` events flow (Task 4b-2), a second handler patches the
outcome via `PatternStore.update_score()`.

The subscriber is workspace-scoped: it owns one PatternStore per workspace
path and dispatches by `session_id → workspace_path` provided by the
caller. The session manager wires this when constructing the runtime.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

from metis_core.events.bus import EventBus, EventFilter, Subscription, SubscriptionHandle
from metis_core.events.envelope import Actor, Event
from metis_core.events.payloads import (
    PatternEvicted,
    PatternRecorded,
    make_event,
)
from metis_core.patterns.fingerprint import (
    Fingerprint,
    FingerprintInputs,
    compute_fingerprint,
)
from metis_core.patterns.store import PatternStore, RecordResult

logger = logging.getLogger(__name__)


WorkspaceResolver = Callable[[str], str | None]
"""Maps `session_id` to an absolute workspace path. Returning None disables
the subscriber for that session (e.g., test sessions without a workspace)."""


FingerprintBuilder = Callable[[Event, Event], FingerprintInputs]
"""Builds fingerprint inputs from (turn_completed, route_decided). The
session manager controls this so it can fold session state (tool history,
file extensions) into the fingerprint."""


def default_fingerprint_builder(
    turn_completed: Event, route_decided: Event, *, workspace_path: str
) -> FingerprintInputs:
    """Best-effort fingerprint inputs from event payloads alone.

    Production callers should supply a richer builder that observes session
    state; this default is suitable for tests and for sessions without an
    enriched state hook.
    """
    payload = turn_completed.payload
    return FingerprintInputs(
        user_message_text="",  # turn.started carries the hash; raw text not on bus
        workspace_path=workspace_path,
        estimated_input_tokens=int(payload.get("total_input_tokens", 0)),
        has_images=False,
        has_tool_calls_in_history=bool(payload.get("tool_call_count", 0)),
        tool_names=(),
        side_effect_classes=(),
    )


class PatternEventSubscriber:
    """Records pattern outcomes from `turn.completed` + `route.decided`.

    The subscriber holds short-lived per-(session, turn) state to correlate
    `route.decided` with the matching `turn.completed`. Both events carry
    `turn_id`, so the join key is stable.
    """

    def __init__(
        self,
        *,
        store_factory: Callable[[str], PatternStore],
        workspace_resolver: WorkspaceResolver,
        bus: EventBus,
        fingerprint_builder: Callable[..., FingerprintInputs] | None = None,
    ) -> None:
        self._store_factory = store_factory
        self._workspace_resolver = workspace_resolver
        self._bus = bus
        self._fingerprint_builder = fingerprint_builder
        self._stores: dict[str, PatternStore] = {}
        # turn_id -> (route_decided event, fingerprint_inputs_override)
        self._pending_routes: dict[str, Event] = {}
        # turn_id -> override FingerprintInputs (set by the session manager
        # before turn.completed fires)
        self._fingerprint_overrides: dict[str, FingerprintInputs] = {}
        # turn_id -> (fingerprint_id, primary_model) so update_score can find
        # the outcome row when an eval.completed event arrives.
        self._turn_outcomes: dict[str, tuple[str, str]] = {}
        self._handles: list[SubscriptionHandle] = []

    # ---- Lifecycle -----------------------------------------------------

    def attach(self) -> list[SubscriptionHandle]:
        """Register subscriptions on the bus. Non-fast-path on every event."""
        self._handles.append(
            self._bus.subscribe(
                Subscription(
                    filter=EventFilter(event_types=frozenset({"route.decided"})),
                    handler=self._on_route_decided,
                    name="pattern-record-route-decided",
                    fast_path=False,
                )
            )
        )
        self._handles.append(
            self._bus.subscribe(
                Subscription(
                    filter=EventFilter(event_types=frozenset({"turn.completed"})),
                    handler=self._on_turn_completed,
                    name="pattern-record-turn-completed",
                    fast_path=False,
                )
            )
        )
        self._handles.append(
            self._bus.subscribe(
                Subscription(
                    filter=EventFilter(event_types=frozenset({"eval.completed"})),
                    handler=self._on_eval_completed,
                    name="pattern-record-eval-completed",
                    fast_path=False,
                )
            )
        )
        return list(self._handles)

    def detach(self) -> None:
        """Unsubscribe from the bus. Does not close stores; the owner that
        provided the `store_factory` is responsible for closing them when
        appropriate."""
        for handle in self._handles:
            self._bus.unsubscribe(handle)
        self._handles.clear()

    # ---- Public API for the session manager ---------------------------

    def set_fingerprint_inputs(self, turn_id: str, inputs: FingerprintInputs) -> None:
        """Supply fingerprint inputs collected by the session manager.

        Called before `turn.completed` fires. When set, this overrides the
        default fingerprint builder for that turn.
        """
        self._fingerprint_overrides[turn_id] = inputs

    def get_outcome_for_turn(self, turn_id: str) -> tuple[str, str] | None:
        """Return `(fingerprint_id, primary_model)` recorded for a turn."""
        return self._turn_outcomes.get(turn_id)

    # ---- Internals: handlers ------------------------------------------

    async def _on_route_decided(self, event: Event) -> None:
        if event.turn_id:
            self._pending_routes[event.turn_id] = event

    async def _on_turn_completed(self, event: Event) -> None:
        if event.turn_id is None:
            return
        route_event = self._pending_routes.pop(event.turn_id, None)
        inputs_override = self._fingerprint_overrides.pop(event.turn_id, None)
        if route_event is None:
            logger.debug(
                "pattern subscriber: no route.decided for turn %s; skipping record",
                event.turn_id,
            )
            return
        primary_model = route_event.payload.get("chosen_model") or ""
        if not primary_model:
            return
        workspace_path = self._workspace_resolver(event.session_id)
        if not workspace_path:
            return

        try:
            store = self._get_or_create_store(workspace_path)
        except Exception:
            logger.exception("pattern subscriber: failed to open store")
            return

        inputs = inputs_override or self._build_inputs(
            event, route_event, workspace_path=workspace_path
        )
        fingerprint = compute_fingerprint(inputs)
        payload = event.payload
        cost_usd = Decimal(str(payload.get("total_cost_usd", "0")))
        latency_ms = float(payload.get("wall_time_seconds", 0.0)) * 1000.0
        pricing_version = payload.get("pricing_version_last", "v0")

        await self._record_outcome(
            store=store,
            event=event,
            fingerprint=fingerprint,
            primary_model=primary_model,
            success_score=None,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
            pricing_version=pricing_version,
        )

    async def _on_eval_completed(self, event: Event) -> None:
        payload = event.payload
        if payload.get("subject_kind") != "turn":
            return
        turn_id = payload.get("subject_id")
        if not turn_id:
            return
        outcome_key = self._turn_outcomes.get(turn_id)
        if outcome_key is None:
            logger.debug(
                "pattern subscriber: eval.completed for unknown turn %s; skipping",
                turn_id,
            )
            return
        workspace_path = self._workspace_resolver(event.session_id)
        if not workspace_path:
            return
        store = self._stores.get(workspace_path)
        if store is None:
            return
        fingerprint_id, primary_model = outcome_key
        score = float(payload.get("score", 0.0))
        confidence = float(payload.get("confidence", 0.0))
        eval_id = payload.get("eval_id", "")
        pricing_version = payload.get("judge_pricing_version")
        try:
            store.update_score(
                turn_id=turn_id,
                fingerprint_id=fingerprint_id,
                primary_model=primary_model,
                score=score,
                confidence=confidence,
                eval_id=eval_id,
                pricing_version=pricing_version,
            )
        except Exception:
            logger.exception("pattern subscriber: update_score failed")

    # ---- Internals: orchestration -------------------------------------

    def _build_inputs(
        self,
        turn_event: Event,
        route_event: Event,
        *,
        workspace_path: str,
    ) -> FingerprintInputs:
        if self._fingerprint_builder is not None:
            return self._fingerprint_builder(turn_event, route_event)
        return default_fingerprint_builder(turn_event, route_event, workspace_path=workspace_path)

    def _get_or_create_store(self, workspace_path: str) -> PatternStore:
        existing = self._stores.get(workspace_path)
        if existing is not None:
            return existing
        store = self._store_factory(workspace_path)
        self._stores[workspace_path] = store
        return store

    async def _record_outcome(
        self,
        *,
        store: PatternStore,
        event: Event,
        fingerprint: Fingerprint,
        primary_model: str,
        success_score: float | None,
        cost_usd: Decimal,
        latency_ms: float,
        pricing_version: str,
    ) -> None:
        try:
            result: RecordResult = store.record(
                fingerprint=fingerprint,
                primary_model=primary_model,
                success_score=success_score,
                cost_usd=cost_usd,
                latency_ms=latency_ms,
                pricing_version=pricing_version,
            )
        except Exception:
            logger.exception("pattern subscriber: record() failed")
            return
        if event.turn_id is not None:
            self._turn_outcomes[event.turn_id] = (result.fingerprint_id, primary_model)
        self._emit_pattern_recorded(
            event=event,
            fingerprint=fingerprint,
            result=result,
            success_score=success_score,
            cost_usd=cost_usd,
            pricing_version=pricing_version,
        )
        if result.over_soft_cap or result.rows_auto_evicted > 0:
            self._emit_pattern_evicted(store=store, event=event, result=result)

    def _emit_pattern_recorded(
        self,
        *,
        event: Event,
        fingerprint: Fingerprint,
        result: RecordResult,
        success_score: float | None,
        cost_usd: Decimal,
        pricing_version: str,
    ) -> None:
        ts = datetime.now(UTC)
        self._bus.emit(
            make_event(
                type="pattern.recorded",
                session_id=event.session_id,
                turn_id=event.turn_id,
                actor=Actor.SYSTEM,
                payload=PatternRecorded(
                    fingerprint_id=result.fingerprint_id,
                    fingerprint_kind=fingerprint.kind.value,
                    primary_model=result.primary_model,
                    sample_size_before=result.sample_size_before,
                    sample_size_after=result.sample_size_after,
                    was_new_fingerprint=result.was_new_fingerprint,
                    success_score=success_score,
                    cost_usd_at_record=cost_usd,
                    pricing_version=pricing_version,
                    over_soft_cap=result.over_soft_cap,
                ),
                timestamp=ts,
                parent_event_id=event.id,
            )
        )

    def _emit_pattern_evicted(
        self,
        *,
        store: PatternStore,
        event: Event,
        result: RecordResult,
    ) -> None:
        ts = datetime.now(UTC)
        size = store.size()
        trigger = "hard_cap_evict" if result.rows_auto_evicted > 0 else "soft_cap_signal"
        self._bus.emit(
            make_event(
                type="pattern.evicted",
                session_id=event.session_id,
                turn_id=event.turn_id,
                actor=Actor.SYSTEM,
                payload=PatternEvicted(
                    trigger=trigger,
                    fingerprints_before=size.fingerprints,
                    fingerprints_after=size.fingerprints,
                    outcomes_before=size.outcomes + result.rows_auto_evicted,
                    outcomes_after=size.outcomes,
                    entries_evicted=result.rows_auto_evicted,
                    oldest_evicted_age_days=size.oldest_outcome_age_days,
                ),
                timestamp=ts,
                parent_event_id=event.id,
            )
        )
