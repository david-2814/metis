"""Prometheus metrics surface (`docs/specs/observability.md`).

A `MetricsCollector` subscribes to bus events on a non-fast-path
subscription and projects them onto Prometheus counters / gauges /
histograms. Both the gateway and server apps mount a `/metrics`
endpoint that calls `collector.expose()`.

Each collector owns its own `prometheus_client.CollectorRegistry` so
two collectors in the same process (e.g. tests, or a side-by-side
gateway+server in one venv) don't clobber each other's series.
"""

from metis.core.observability.metrics import (
    METRICS_CONTENT_TYPE,
    MetricsCollector,
)

__all__ = [
    "METRICS_CONTENT_TYPE",
    "MetricsCollector",
]
