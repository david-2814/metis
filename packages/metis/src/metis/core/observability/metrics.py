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
from collections import OrderedDict
from collections.abc import Callable
from decimal import Decimal

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from prometheus_client.exposition import CONTENT_TYPE_LATEST

from metis.core.events.bus import EventBus, EventFilter, Subscription, SubscriptionHandle
from metis.core.events.envelope import Event

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
        # Wave 14a — production-grade observability extensions:
        # tool-call latency + failure attribution requires correlating
        # tool.completed / tool.failed (no tool_name) back to tool.called
        # (carries tool_name). The collector keeps a small bounded LRU.
        "tool.called",
        "tool.completed",
        "tool.failed",
        # Gateway auth-failure rate alert input.
        "gateway.auth_failed",
    }
)


# Histogram buckets covering typical LLM latencies — short tool-cycle
# completions through long thinking-block calls. Matches the order-of-
# magnitude shape Prometheus expects for `_seconds` histograms.
# Range spans 50ms (tiny cached completions) through 120s (long
# multi-block thinking calls) — covers the observability.md §3 0.1-30s
# target with headroom on both ends.
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


# Routing slot evaluation is the cheapest hot path in the system — sync
# Python over 7 slots + at most one K-NN SQLite read. Typical wall-time is
# sub-millisecond; the tail catches the K-NN cluster-tightening regime
# where pattern-store lookup dominates. Buckets span 100µs through 500ms.
_ROUTING_LATENCY_BUCKETS_SECONDS: tuple[float, ...] = (
    0.0001,
    0.0005,
    0.001,
    0.0025,
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.5,
)


# Tool calls range from instant in-process file reads to bash invocations
# that legitimately take tens of seconds (build / test loops). Buckets
# share the LLM shape on the long end but add 5ms / 10ms on the short
# end so file/memory tools don't all pile into the 50ms bucket.
_TOOL_LATENCY_BUCKETS_SECONDS: tuple[float, ...] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
)


# Cap on the in-memory `tool_use_id → tool_name` correlation table.
# Tools execute serially within a turn but a runaway turn (or a tool that
# never completes) could leak entries; the LRU is the safety net. 1000 is
# >> the largest fan-out we expect from a single turn's tool cycle.
_TOOL_NAME_CACHE_MAX = 1000


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
    * `pattern_cache_getter`: returns a list of
      `(workspace_id, hits, misses)` tuples — one per open
      `PatternStore`. Drives `metis_pattern_embedding_cache_hit_ratio`
      (gauge) and `_hits_total` / `_misses_total` (gauge derived from
      the per-store counters; see `pattern-store.md §16.7.2`). v1
      runtimes that don't run the pattern subscriber omit this.

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
        trace_wal_bytes_getter: Callable[[], int] | None = None,
        pattern_cache_getter: Callable[[], list[tuple[str, int, int]]] | None = None,
    ) -> None:
        self._bus = bus
        self._registry = registry if registry is not None else CollectorRegistry()
        self._session_count_getter = session_count_getter
        self._gateway_keys_getter = gateway_keys_getter
        self._trace_wal_bytes_getter = trace_wal_bytes_getter
        self._pattern_cache_getter = pattern_cache_getter
        self._handle: SubscriptionHandle | None = None
        # Wave 14a — tool.completed / tool.failed don't carry `tool_name`
        # in their payloads (event-bus-and-trace-catalog §6.4). We
        # correlate via `tool_use_id` from tool.called using a bounded
        # LRU. Entries land on `tool.called`, drain on
        # tool.completed/tool.failed; the cap is the leak backstop.
        self._tool_names: OrderedDict[str, str] = OrderedDict()

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
        # Wave 13: trace-DB WAL file size, polled at scrape time. Operators
        # alert on this exceeding 2-3x the auto-checkpoint threshold —
        # sustained WAL growth means a long-running reader is holding the
        # checkpoint barrier (`SQLITE_BUSY` on writers under TRUNCATE
        # checkpoints, or analytic queries pinning the WAL via
        # `read_uncommitted=0`). See docs/operations/trace-performance.md
        # §WAL.
        self._trace_wal_bytes = Gauge(
            "metis_trace_wal_bytes",
            "Trace-DB WAL file size in bytes. 0 means freshly checkpointed or no WAL.",
            registry=self._registry,
        )
        # v2 embedding-cache observability per pattern-store.md §16.7.2.
        # Hit ratio is `hits / (hits + misses)` per workspace; the spec target
        # is ≥80% within 100 turns of a workload, so a sustained ratio below
        # ~0.5 is a signal the cache is undersized for the traffic mix or
        # that the cache is being thrashed by eviction (cap=10k by default).
        # Hits/misses are exposed alongside the ratio so prometheus can rate()
        # them independently for trend detection.
        self._pattern_cache_hit_ratio = Gauge(
            "metis_pattern_embedding_cache_hit_ratio",
            "v2 embedding-cache hit ratio per workspace (hits/(hits+misses)).",
            labelnames=("workspace_id",),
            registry=self._registry,
        )
        self._pattern_cache_hits = Gauge(
            "metis_pattern_embedding_cache_hits_total",
            "Cumulative v2 embedding-cache hits per workspace (process-local).",
            labelnames=("workspace_id",),
            registry=self._registry,
        )
        self._pattern_cache_misses = Gauge(
            "metis_pattern_embedding_cache_misses_total",
            "Cumulative v2 embedding-cache misses per workspace (process-local).",
            labelnames=("workspace_id",),
            registry=self._registry,
        )
        # Wave 14a — production-grade observability extensions per
        # observability.md §3.2. Three additions:
        #
        # 1. Latency histograms for the routing decision and tool-call hot
        #    paths (LLM latency was already shipped in Wave 11).
        # 2. Dedicated error counters with the failure-class label split out
        #    — distinct from the `metis_llm_calls_total{status}` rollup so
        #    rate() queries don't pay the cardinality of mixing success +
        #    error series in one histogram.
        # 3. Gateway auth-failure counter for credential-stuffing / leaked-
        #    key burn detection.
        #
        # Counters and histograms have process-startup cost only; gauges are
        # polled. Cardinality is bounded by the same closed-enum discipline
        # spelled out in observability.md §3.1.
        self._routing_decision_latency_seconds = Histogram(
            "metis_routing_decision_latency_seconds",
            "Routing-engine wall-time per turn (route.decided.elapsed_ms).",
            buckets=_ROUTING_LATENCY_BUCKETS_SECONDS,
            registry=self._registry,
        )
        self._tool_call_latency_seconds = Histogram(
            "metis_tool_call_latency_seconds",
            "Tool dispatcher wall-time, observed from tool.completed and tool.failed.",
            labelnames=("tool_name",),
            buckets=_TOOL_LATENCY_BUCKETS_SECONDS,
            registry=self._registry,
        )
        self._llm_call_errors_total = Counter(
            "metis_llm_call_errors_total",
            "LLM call failures observed on llm.call_failed, by error class.",
            labelnames=("provider", "model", "error_class"),
            registry=self._registry,
        )
        self._tool_failures_total = Counter(
            "metis_tool_failures_total",
            "Tool failures observed on tool.failed, by tool and error class.",
            labelnames=("tool_name", "error_class"),
            registry=self._registry,
        )
        self._gateway_auth_failures_total = Counter(
            "metis_gateway_auth_failures_total",
            "Gateway auth rejections, by reason (missing/invalid token, revoked key).",
            labelnames=("reason",),
            registry=self._registry,
        )
        # Per-key cost counter. Drives the `MetisGatewayKeyCostSpike` alert
        # (prometheus-rules.yaml). Cardinality follows the same discipline
        # as `metis_quota_used_ratio` — the identity dimension is operator-
        # bounded (typically <100 keys per deployment). Calls without a
        # `gateway_key_id` (agent-loop traffic / pre-multi-user gateway
        # keys) collapse under the `null` bucket so dashboards stay one query.
        self._gateway_key_cost_usd_total = Counter(
            "metis_gateway_key_cost_usd_total",
            "Cumulative LLM cost in USD attributed per gateway key.",
            labelnames=("gateway_key_id",),
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
        if self._trace_wal_bytes_getter is not None:
            try:
                self._trace_wal_bytes.set(self._trace_wal_bytes_getter())
            except Exception:
                logger.warning("trace_wal_bytes_getter failed; gauge stale", exc_info=True)
        if self._pattern_cache_getter is not None:
            try:
                entries = self._pattern_cache_getter()
            except Exception:
                logger.warning("pattern_cache_getter failed; gauges stale", exc_info=True)
            else:
                for workspace_id, hits, misses in entries:
                    total = hits + misses
                    ratio = (hits / total) if total else 0.0
                    self._pattern_cache_hit_ratio.labels(workspace_id=workspace_id).set(ratio)
                    self._pattern_cache_hits.labels(workspace_id=workspace_id).set(hits)
                    self._pattern_cache_misses.labels(workspace_id=workspace_id).set(misses)

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
        elif event.type == "tool.called":
            self._on_tool_called(payload)
        elif event.type == "tool.completed":
            self._on_tool_completed(payload)
        elif event.type == "tool.failed":
            self._on_tool_failed(payload)
        elif event.type == "gateway.auth_failed":
            self._on_gateway_auth_failed(payload)

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
            # Per-key cost attribution. `gateway_key_id` is None for the
            # in-process agent loop and pre-multi-user keys; the `null`
            # bucket keeps it queryable in one shot vs. dropping the row.
            gateway_key_id = _nullable_label(payload, "gateway_key_id")
            self._gateway_key_cost_usd_total.labels(gateway_key_id=gateway_key_id).inc(cost_float)

    def _on_llm_failed(self, payload: dict) -> None:
        provider = _label(payload, "provider")
        model = _label(payload, "model")
        error_class = _label(payload, "error_class", default="error")
        # `metis_llm_calls_total{status=error_class}` keeps the success +
        # error roll-up parity with completed calls; the dedicated
        # `metis_llm_call_errors_total` counter below carries the same
        # data on its own series so alerting rules can rate() errors
        # without summing across status labels.
        self._llm_calls_total.labels(provider=provider, model=model, status=error_class).inc()
        self._llm_call_errors_total.labels(
            provider=provider, model=model, error_class=error_class
        ).inc()
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
        elapsed_ms = payload.get("elapsed_ms")
        if isinstance(elapsed_ms, (int, float)):
            self._routing_decision_latency_seconds.observe(float(elapsed_ms) / 1000.0)

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

    def _on_tool_called(self, payload: dict) -> None:
        """Record the tool_use_id → tool_name mapping for later correlation.

        `tool.completed` and `tool.failed` don't carry `tool_name` directly
        (event-bus-and-trace-catalog §6.4); this LRU lets the latency
        histogram and failure counter both label by tool. The OrderedDict
        is bounded by `_TOOL_NAME_CACHE_MAX` so a runaway turn that never
        completes its tool calls can't leak memory.
        """
        tool_use_id = payload.get("tool_use_id")
        tool_name = payload.get("tool_name")
        if not isinstance(tool_use_id, str) or not isinstance(tool_name, str):
            return
        self._tool_names[tool_use_id] = tool_name
        self._tool_names.move_to_end(tool_use_id)
        while len(self._tool_names) > _TOOL_NAME_CACHE_MAX:
            self._tool_names.popitem(last=False)

    def _on_tool_completed(self, payload: dict) -> None:
        tool_use_id = payload.get("tool_use_id")
        tool_name = self._pop_tool_name(tool_use_id)
        latency_ms = payload.get("latency_ms")
        if isinstance(latency_ms, (int, float)):
            self._tool_call_latency_seconds.labels(tool_name=tool_name).observe(
                float(latency_ms) / 1000.0
            )

    def _on_tool_failed(self, payload: dict) -> None:
        tool_use_id = payload.get("tool_use_id")
        tool_name = self._pop_tool_name(tool_use_id)
        error_class = _label(payload, "error_class", default="error")
        self._tool_failures_total.labels(tool_name=tool_name, error_class=error_class).inc()
        latency_ms = payload.get("latency_ms")
        if isinstance(latency_ms, (int, float)):
            self._tool_call_latency_seconds.labels(tool_name=tool_name).observe(
                float(latency_ms) / 1000.0
            )

    def _pop_tool_name(self, tool_use_id: object) -> str:
        """Resolve and drain a tool_use_id from the correlation cache.

        Missing entries (the `tool.called` event was lost / out of order /
        never seen — e.g. tests that emit only completed/failed) collapse
        to `"unknown"` rather than mint a new label series.
        """
        if not isinstance(tool_use_id, str):
            return "unknown"
        name = self._tool_names.pop(tool_use_id, None)
        return name if name is not None else "unknown"

    def _on_gateway_auth_failed(self, payload: dict) -> None:
        reason = _label(payload, "reason")
        self._gateway_auth_failures_total.labels(reason=reason).inc()


def _label(payload: dict, key: str, *, default: str = "unknown") -> str:
    """Coerce a payload field into a stable label string.

    Missing / None / empty-string all collapse to a single `default`
    bucket so we don't proliferate cardinality on malformed events.
    """
    raw = payload.get(key)
    if raw is None or raw == "":
        return default
    return str(raw)


def _nullable_label(payload: dict, key: str) -> str:
    """Coerce a nullable identity field into a label string.

    Distinct from `_label()` because the null bucket has a documented
    meaning ("agent-loop traffic / pre-multi-user gateway key") and the
    word `null` reads clearer than `unknown` for these dimensions.
    Mirrors the analytics-api null-row convention (multi-user.md §3.4).
    """
    raw = payload.get(key)
    if raw is None or raw == "":
        return "null"
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
