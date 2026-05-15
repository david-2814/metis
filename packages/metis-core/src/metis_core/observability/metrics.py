"""Bus-driven Prometheus metrics collector (`docs/specs/observability.md`).

Each `MetricsCollector` subscribes to the catalog events listed in
`_OBSERVED_EVENT_TYPES` on a non-fast-path subscription and projects
them onto a private `prometheus_client.CollectorRegistry`. Counters
are monotonic; cost / latency / quota gauges hold the most recently
observed value.

Two design constraints:

* **Best-effort.** The collector lives off the agent loop's critical
  path (non-fast-path subscriber per `event-bus-and-trace-catalog.md`
  §3.4). A handler exception is logged and swallowed — observability
  never blocks a turn.
* **Per-instance registry.** The default `prometheus_client.REGISTRY`
  is process-global; two collectors registering against it raise on
  duplicate-metric. We pin a private `CollectorRegistry` so tests, the
  gateway, and the server can each own one without leaking series.

The `/metrics` HTTP exposition lives in the apps; this module
generates the bytes via `expose()` and reports the canonical
content-type via `METRICS_CONTENT_TYPE`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from decimal import Decimal

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from prometheus_client.exposition import CONTENT_TYPE_LATEST

from metis_core.events.bus import EventBus, EventFilter, Subscription, SubscriptionHandle
from metis_core.events.envelope import Event

logger = logging.getLogger(__name__)


METRICS_CONTENT_TYPE = CONTENT_TYPE_LATEST


# Events the collector subscribes to. Kept as a frozenset so adding a new
# event in payloads.py without updating this list is a no-op (rather than
# silently widening the metric surface).
_OBSERVED_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "llm.call_completed",
        "llm.call_failed",
        "route.decided",
        "pattern.matched",
        "quota.alert",
        "gateway.quota_exceeded",
        "eval.completed",
    }
)


# Histogram buckets covering typical LLM latencies — short tool-cycle
# completions through long thinking-block calls. Matches the order-of-
# magnitude shape Prometheus expects for `_seconds` histograms.
_LATENCY_BUCKETS_SECONDS: tuple[float, ...] = (
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
    120.0,
)


class MetricsCollector:
    """Owns the metric definitions, the bus subscription, and the exposition path.

    Construct with `bus=EventBus`. Optionally pass:

    * `session_count_getter`: returns the current open-session count for
      `metis_session_count`. Polled on every scrape — the server runtime
      reads `runtime.session_store.list_sessions()`. Gateway runtimes
      omit this (they're per-request stateless).
    * `gateway_keys_getter`: returns `(active, revoked)` counts for
      `metis_gateway_keys_active` / `_revoked`. Polled on every scrape.
      Server omits this; the gateway reads `runtime.keystore.keys()` and
      checks `is_active(now=…)` so grace-period-expired keys count as
      revoked even before the next admin sweep persists them.

    Call `attach()` to register on the bus and `detach()` for tear-down.
    Call `expose()` on each `/metrics` request to refresh the polled
    gauges and return the exposition bytes.
    """

    def __init__(
        self,
        *,
        bus: EventBus,
        registry: CollectorRegistry | None = None,
        session_count_getter: Callable[[], int] | None = None,
        gateway_keys_getter: Callable[[], tuple[int, int]] | None = None,
    ) -> None:
        self._bus = bus
        self._registry = registry if registry is not None else CollectorRegistry()
        self._session_count_getter = session_count_getter
        self._gateway_keys_getter = gateway_keys_getter
        self._handle: SubscriptionHandle | None = None

        self._llm_calls_total = Counter(
            "metis_llm_calls_total",
            "LLM API calls observed on the bus, by provider/model and outcome.",
            labelnames=("provider", "model", "status"),
            registry=self._registry,
        )
        self._llm_call_latency_seconds = Histogram(
            "metis_llm_call_latency_seconds",
            "LLM call wall-time, observed from llm.call_completed / llm.call_failed.",
            labelnames=("provider", "model"),
            buckets=_LATENCY_BUCKETS_SECONDS,
            registry=self._registry,
        )
        self._llm_cost_usd_total = Counter(
            "metis_llm_cost_usd_total",
            "Cumulative LLM cost in USD, observed from llm.call_completed.",
            labelnames=("provider", "model"),
            registry=self._registry,
        )
        self._routing_decisions_total = Counter(
            "metis_routing_decisions_total",
            "Routing decisions observed on the bus, by winning slot and chosen model.",
            labelnames=("winning_slot", "chosen_model"),
            registry=self._registry,
        )
        self._pattern_matches_total = Counter(
            "metis_pattern_matches_total",
            "Pattern-store routing wins observed on the bus.",
            labelnames=("chose_model", "fingerprint_version"),
            registry=self._registry,
        )
        self._quota_used_ratio = Gauge(
            "metis_quota_used_ratio",
            "Most recent quota usage ratio (used/limit) per identity dimension.",
            labelnames=("identity_kind", "identity_id"),
            registry=self._registry,
        )
        self._eval_verdicts_total = Counter(
            "metis_eval_verdicts_total",
            "Evaluator verdicts observed on the bus (eval.completed only).",
            labelnames=("judge_kind", "subject_kind"),
            registry=self._registry,
        )
        self._session_count = Gauge(
            "metis_session_count",
            "Current open agent sessions (server runtime only). Polled at scrape time.",
            registry=self._registry,
        )
        self._gateway_keys_active = Gauge(
            "metis_gateway_keys_active",
            "Active gateway keys in the keystore. Polled at scrape time.",
            registry=self._registry,
        )
        self._gateway_keys_revoked = Gauge(
            "metis_gateway_keys_revoked",
            "Revoked or grace-expired gateway keys in the keystore. Polled at scrape time.",
            registry=self._registry,
        )

    # ---- Lifecycle -----------------------------------------------------

    def attach(self, *, name: str = "observability-metrics") -> SubscriptionHandle:
        """Subscribe to the observed events on a non-fast-path subscription."""
        if self._handle is not None:
            return self._handle
        self._handle = self._bus.subscribe(
            Subscription(
                filter=EventFilter(event_types=_OBSERVED_EVENT_TYPES),
                handler=self._on_event,
                name=name,
                fast_path=False,
            )
        )
        return self._handle

    def detach(self) -> None:
        if self._handle is not None:
            self._bus.unsubscribe(self._handle)
            self._handle = None

    @property
    def registry(self) -> CollectorRegistry:
        return self._registry

    # ---- Exposition ----------------------------------------------------

    def expose(self) -> bytes:
        """Refresh polled gauges and return Prometheus exposition bytes."""
        self._refresh_polled_gauges()
        return generate_latest(self._registry)

    def _refresh_polled_gauges(self) -> None:
        if self._session_count_getter is not None:
            try:
                self._session_count.set(self._session_count_getter())
            except Exception:
                logger.warning("session_count_getter failed; gauge stale", exc_info=True)
        if self._gateway_keys_getter is not None:
            try:
                active, revoked = self._gateway_keys_getter()
                self._gateway_keys_active.set(active)
                self._gateway_keys_revoked.set(revoked)
            except Exception:
                logger.warning("gateway_keys_getter failed; gauges stale", exc_info=True)

    # ---- Bus handler ---------------------------------------------------

    async def _on_event(self, event: Event) -> None:
        try:
            self._dispatch(event)
        except Exception:
            # Observability MUST NOT take down a turn. Log and continue;
            # the bus is non-fast-path here so a swallow is the contract.
            logger.warning("metrics collector handler error on %s", event.type, exc_info=True)

    def _dispatch(self, event: Event) -> None:
        payload = event.payload
        if event.type == "llm.call_completed":
            self._on_llm_completed(payload)
        elif event.type == "llm.call_failed":
            self._on_llm_failed(payload)
        elif event.type == "route.decided":
            self._on_route_decided(payload)
        elif event.type == "pattern.matched":
            self._on_pattern_matched(payload)
        elif event.type in ("quota.alert", "gateway.quota_exceeded"):
            self._on_quota_event(event.type, payload)
        elif event.type == "eval.completed":
            self._on_eval_completed(payload)

    def _on_llm_completed(self, payload: dict) -> None:
        provider = _label(payload, "provider")
        model = _label(payload, "model")
        self._llm_calls_total.labels(provider=provider, model=model, status="ok").inc()
        latency_ms = payload.get("latency_ms")
        if isinstance(latency_ms, (int, float)):
            self._llm_call_latency_seconds.labels(provider=provider, model=model).observe(
                float(latency_ms) / 1000.0
            )
        cost = payload.get("cost_usd")
        cost_float = _coerce_cost(cost)
        if cost_float is not None and cost_float > 0:
            self._llm_cost_usd_total.labels(provider=provider, model=model).inc(cost_float)

    def _on_llm_failed(self, payload: dict) -> None:
        provider = _label(payload, "provider")
        model = _label(payload, "model")
        status = _label(payload, "error_class", default="error")
        self._llm_calls_total.labels(provider=provider, model=model, status=status).inc()
        latency_ms = payload.get("latency_ms")
        if isinstance(latency_ms, (int, float)):
            self._llm_call_latency_seconds.labels(provider=provider, model=model).observe(
                float(latency_ms) / 1000.0
            )

    def _on_route_decided(self, payload: dict) -> None:
        chain = payload.get("chain") or []
        winner_index = payload.get("winner_index")
        winning_slot = "unknown"
        if isinstance(winner_index, int) and 0 <= winner_index < len(chain):
            entry = chain[winner_index]
            if isinstance(entry, dict):
                winning_slot = str(entry.get("policy") or "unknown")
        chosen_model = _label(payload, "chosen_model")
        self._routing_decisions_total.labels(
            winning_slot=winning_slot,
            chosen_model=chosen_model,
        ).inc()

    def _on_pattern_matched(self, payload: dict) -> None:
        chose_model = _label(payload, "chosen_model")
        fingerprint_version = _label(payload, "fingerprint_kind", default="structural")
        self._pattern_matches_total.labels(
            chose_model=chose_model,
            fingerprint_version=fingerprint_version,
        ).inc()

    def _on_quota_event(self, event_type: str, payload: dict) -> None:
        # `scope` is "{identity_kind}_{window}" per multi-user.md §5.1; the
        # leading token is the identity dimension this metric is sliced on.
        scope = payload.get("scope") or ""
        identity_kind, _, _ = str(scope).partition("_")
        if identity_kind not in ("key", "user", "team"):
            return
        identity_id = _identity_id_for(identity_kind, payload)
        if identity_id is None:
            return
        if event_type == "gateway.quota_exceeded":
            ratio = 1.0
        else:
            pct = payload.get("percentage")
            ratio = float(pct) if isinstance(pct, (int, float)) else 1.0
        self._quota_used_ratio.labels(identity_kind=identity_kind, identity_id=identity_id).set(
            ratio
        )

    def _on_eval_completed(self, payload: dict) -> None:
        judge_kind = _label(payload, "judge_kind")
        subject_kind = _label(payload, "subject_kind")
        self._eval_verdicts_total.labels(judge_kind=judge_kind, subject_kind=subject_kind).inc()


def _label(payload: dict, key: str, *, default: str = "unknown") -> str:
    """Coerce a payload field into a stable label string.

    Missing / None / empty-string all collapse to a single `default`
    bucket so we don't proliferate cardinality on malformed events.
    """
    raw = payload.get(key)
    if raw is None or raw == "":
        return default
    return str(raw)


def _coerce_cost(raw: object) -> float | None:
    """Coerce `cost_usd` (which serializes as float OR Decimal-string) to float."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, Decimal):
        return float(raw)
    try:
        return float(str(raw))
    except (TypeError, ValueError):
        return None


def _identity_id_for(identity_kind: str, payload: dict) -> str | None:
    if identity_kind == "key":
        return _nullable_str(payload.get("gateway_key_id"))
    if identity_kind == "user":
        return _nullable_str(payload.get("user_id"))
    if identity_kind == "team":
        return _nullable_str(payload.get("team_id"))
    return None


def _nullable_str(raw: object) -> str | None:
    if raw is None or raw == "":
        return None
    return str(raw)
