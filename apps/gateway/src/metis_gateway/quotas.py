"""Per-key / per-user / per-team spend quotas (multi-user.md §5).

The tracker is a thin SQL projection over the trace store: given an
identity dimension (`key` / `user` / `team`) and a window (`daily` /
`monthly`), it sums `llm.call_completed.cost_usd` and returns a
`QuotaStatus` carrying the raw spend, the configured cap (if any), and
the percentage. The quota tracker is the read side; the gateway
harness is the write side that decides whether to alert (soft) or
short-circuit (hard).

Concurrency posture matches `AnalyticsStore` — single SQLite connection
per tracker; the gateway runs one tracker per `GatewayRuntime`. Read
cost is one query per (identity, window) pair at request entry; the
in-request cache below memoizes them so the harness can ask the same
question multiple times (daily + monthly + soft + hard) without
re-issuing SQL.

Spend is computed in `Decimal` end-to-end to match
`canonical-message-format.md §6.4` and the `analytics/store.py`
convention. The trace store writes `cost_usd` as a float JSON number,
so coercion via `Decimal(str(value))` follows the same path as the
analytics surface (`store.py::_coerce_decimal`).
"""

from __future__ import annotations

import logging
import sqlite3
from calendar import monthrange
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal

from metis_core.canonical.ids import new_message_id
from metis_core.events.bus import EventBus
from metis_core.events.envelope import Actor
from metis_core.events.payloads import GatewayQuotaExceeded, QuotaAlert, make_event

from metis_gateway.auth import GatewayKey, Identity

logger = logging.getLogger(__name__)

# multi-user.md §5 / gateway.md §6.4 — soft alert thresholds. Hard breaker at 1.0.
SOFT_ALERT_WARNING_THRESHOLD = 0.80
SOFT_ALERT_CRITICAL_THRESHOLD = 0.95


QuotaScope = Literal[
    "key_daily", "key_monthly", "user_daily", "user_monthly", "team_daily", "team_monthly"
]
"""multi-user.md §5.1 — the cap scopes the tracker can produce.

Names mirror the wire `gateway.quota_exceeded.scope` literal so the
event payload, the QuotaStatus, and the routing-rule reasoning all
read the same string."""

IdentityKind = Literal["key", "user", "team"]
"""Which identity dimension the cap applies to.

`key` matches `gateway_key_id` on the trace event; `user` matches
`user_id`; `team` matches `team_id`. Each maps to a different
`json_extract` projection in the SQL below."""

WindowKind = Literal["daily", "monthly"]


@dataclass(frozen=True)
class QuotaStatus:
    """A point-in-time spend / cap snapshot for one identity dimension.

    `used_usd` is the running total over the window; `cap_usd` is the
    configured limit (or `None` when no cap is set, in which case
    `percentage` is also `None`). `percentage` is `used / cap`,
    clamped to a non-negative float; consumers compare against the
    soft (0.80 / 0.95) and hard (1.0) thresholds.
    """

    scope: QuotaScope
    identity_kind: IdentityKind
    identity_value: str
    used_usd: Decimal
    cap_usd: Decimal | None
    percentage: float | None

    @property
    def is_exceeded(self) -> bool:
        return self.percentage is not None and self.percentage >= 1.0

    def remaining_usd(self) -> Decimal | None:
        """Headroom in USD; `None` when no cap is set.

        Negative values are clamped to zero — a request that overshot the
        cap mid-stream landed on the wrong side of the breaker, but the
        routing predicate should still see `0` (no headroom), not a
        negative number that an unrelated rule might compare against.
        """
        if self.cap_usd is None:
            return None
        diff = self.cap_usd - self.used_usd
        return diff if diff > 0 else Decimal("0")


class QuotaTracker:
    """Read-only spend aggregator over the trace store's `events` table.

    Opens its own SQLite connection (separate from `TraceStore`'s writer)
    so the gateway can poll spend without coordinating with the bus
    drain. WAL mode lets the writer keep emitting while the tracker
    reads.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._conn = sqlite3.connect(
            self._db_path,
            isolation_level=None,
            check_same_thread=False,
        )
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.row_factory = sqlite3.Row

    def close(self) -> None:
        self._conn.close()

    def status(
        self,
        *,
        identity_kind: IdentityKind,
        identity_value: str,
        window: WindowKind,
        cap_usd: Decimal | None,
        now: datetime | None = None,
    ) -> QuotaStatus:
        """Return the current `QuotaStatus` for one identity / window.

        `now` is the reference instant for the daily / monthly window;
        defaulting to wall-clock UTC. Tests pass an override for
        determinism. The window matches the convention from
        `multi-user.md §11.6`: daily resets at UTC midnight, monthly at
        UTC first-of-month.
        """
        scope = _scope_for(identity_kind, window)
        used = self._sum_spend(
            identity_kind=identity_kind,
            identity_value=identity_value,
            window=window,
            now=now or datetime.now(UTC),
        )
        percentage: float | None
        if cap_usd is None or cap_usd <= 0:
            percentage = None
        else:
            percentage = float(used / cap_usd)
        return QuotaStatus(
            scope=scope,
            identity_kind=identity_kind,
            identity_value=identity_value,
            used_usd=used,
            cap_usd=cap_usd,
            percentage=percentage,
        )

    def _sum_spend(
        self,
        *,
        identity_kind: IdentityKind,
        identity_value: str,
        window: WindowKind,
        now: datetime,
    ) -> Decimal:
        start_us, end_us = _window_bounds_us(window, now)
        column = _identity_column(identity_kind)
        sql = (
            "SELECT json_extract(payload_json, '$.cost_usd') AS cost_usd "
            "FROM events "
            "WHERE type = 'llm.call_completed' "
            "  AND timestamp_us >= ? AND timestamp_us < ? "
            f"  AND {column} = ?"
        )
        total = Decimal("0")
        for row in self._conn.execute(sql, (start_us, end_us, identity_value)):
            raw = row["cost_usd"]
            if raw is None:
                continue
            try:
                total += Decimal(str(raw))
            except Exception:
                logger.warning("non-numeric cost_usd in trace; skipping", exc_info=True)
        return total


@dataclass
class RequestQuotaCache:
    """Per-request memoization of quota lookups.

    Holds the snapshot of every `(identity_kind, identity_value, window)`
    the harness asks about so the same question doesn't re-issue SQL.
    The harness builds one cache per inbound HTTP request and discards
    it afterwards — quota status is read at request entry, never
    mid-stream.

    The cache is a transient projection; do not persist or share it
    across requests.
    """

    tracker: QuotaTracker
    now: datetime
    _cache: dict[tuple[IdentityKind, str, WindowKind, Decimal | None], QuotaStatus]

    def __init__(self, tracker: QuotaTracker, now: datetime | None = None) -> None:
        self.tracker = tracker
        self.now = now or datetime.now(UTC)
        self._cache = {}

    def status(
        self,
        *,
        identity_kind: IdentityKind,
        identity_value: str,
        window: WindowKind,
        cap_usd: Decimal | None,
    ) -> QuotaStatus:
        cache_key = (identity_kind, identity_value, window, cap_usd)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        status = self.tracker.status(
            identity_kind=identity_kind,
            identity_value=identity_value,
            window=window,
            cap_usd=cap_usd,
            now=self.now,
        )
        self._cache[cache_key] = status
        return status


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _identity_column(identity_kind: IdentityKind) -> str:
    if identity_kind == "key":
        return "json_extract(payload_json, '$.gateway_key_id')"
    if identity_kind == "user":
        return "json_extract(payload_json, '$.user_id')"
    if identity_kind == "team":
        return "json_extract(payload_json, '$.team_id')"
    raise ValueError(f"unknown identity_kind: {identity_kind!r}")


def _scope_for(identity_kind: IdentityKind, window: WindowKind) -> QuotaScope:
    return f"{identity_kind}_{window}"  # type: ignore[return-value]


def _window_bounds_us(window: WindowKind, now: datetime) -> tuple[int, int]:
    """UTC bounds for the daily / monthly window enclosing `now`.

    Returns `(start_us, end_us)` matching the trace store's
    `events.timestamp_us` column type. The window is half-open
    `[start, end)` per the analytics-api convention.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    else:
        now = now.astimezone(UTC)
    if window == "daily":
        start = datetime(now.year, now.month, now.day, tzinfo=UTC)
        end = start + timedelta(days=1)
    elif window == "monthly":
        start = datetime(now.year, now.month, 1, tzinfo=UTC)
        days_in_month = monthrange(now.year, now.month)[1]
        end = start + timedelta(days=days_in_month)
    else:
        raise ValueError(f"unknown window: {window!r}")
    return _to_micros(start), _to_micros(end)


def _to_micros(dt: datetime) -> int:
    epoch = datetime(1970, 1, 1, tzinfo=dt.tzinfo)
    delta = dt - epoch
    return delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds


# ---------------------------------------------------------------------------
# Enforcement (the gateway-facing surface)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QuotaExceeded:
    """The result of `enforce_quotas` when a hard cap blocks the request.

    `identity_kind` is what the 429 body's `identity` field should
    say ("key" / "user" / "team"). The HTTP layer translates the rest
    to gateway.md §6.4's body shape and returns 429.
    """

    scope: QuotaScope
    identity_kind: IdentityKind
    current_usd: Decimal
    limit_usd: Decimal


def applicable_quotas(key: GatewayKey) -> list[tuple[IdentityKind, str, WindowKind, Decimal]]:
    """Enumerate every (identity, window, cap) tuple this key is subject to.

    v1 lands key-level caps only — `multi-user.md §5.1` describes
    user-level and team-level caps but those need `users.json` /
    `teams.json` to land first. The shape is identity-agnostic so the
    user/team caps slot in here when they ship.
    """
    out: list[tuple[IdentityKind, str, WindowKind, Decimal]] = []
    if key.daily_cap_usd is not None:
        out.append(("key", key.key_id, "daily", key.daily_cap_usd))
    if key.monthly_cap_usd is not None:
        out.append(("key", key.key_id, "monthly", key.monthly_cap_usd))
    return out


def enforce_quotas(
    *,
    bus: EventBus,
    cache: RequestQuotaCache,
    key: GatewayKey,
    identity: Identity,
    inbound_shape: Literal["openai", "anthropic"],
) -> QuotaExceeded | None:
    """Compute every applicable quota status and apply soft / hard policy.

    Returns `None` when the request can proceed; returns a
    `QuotaExceeded` when a hard cap blocks it (the HTTP layer turns
    that into a 429). Soft alerts emit `quota.alert` events as a
    side effect — at most one event per (request, scope), capped at
    the highest severity tier crossed.

    Hard caps short-circuit on first match — once one cap fires we
    don't bother computing the others, since the request is rejected.
    """
    quotas = applicable_quotas(key)
    if not quotas:
        return None

    statuses: list[QuotaStatus] = []
    for identity_kind, identity_value, window, cap_usd in quotas:
        status = cache.status(
            identity_kind=identity_kind,
            identity_value=identity_value,
            window=window,
            cap_usd=cap_usd,
        )
        statuses.append(status)
        if status.is_exceeded:
            _emit_quota_exceeded(
                bus=bus,
                status=status,
                identity=identity,
                inbound_shape=inbound_shape,
            )
            return QuotaExceeded(
                scope=status.scope,
                identity_kind=status.identity_kind,
                current_usd=status.used_usd,
                limit_usd=status.cap_usd or Decimal("0"),
            )

    # No hard cap exceeded — fire soft alerts for every status that
    # crossed the warning/critical threshold but stayed under 1.0.
    for status in statuses:
        severity = _severity_for(status)
        if severity is None:
            continue
        _emit_quota_alert(bus=bus, status=status, severity=severity, identity=identity)
    return None


def _severity_for(status: QuotaStatus) -> Literal["warning", "critical"] | None:
    if status.percentage is None:
        return None
    if status.percentage >= 1.0:
        # Reached or passed the hard breaker — separate event type, not a
        # soft alert. The hard-cap path already handled emission.
        return None
    if status.percentage >= SOFT_ALERT_CRITICAL_THRESHOLD:
        return "critical"
    if status.percentage >= SOFT_ALERT_WARNING_THRESHOLD:
        return "warning"
    return None


def _emit_quota_alert(
    *,
    bus: EventBus,
    status: QuotaStatus,
    severity: Literal["warning", "critical"],
    identity: Identity,
) -> None:
    cap = status.cap_usd or Decimal("0")
    payload = QuotaAlert(
        scope=status.scope,
        severity=severity,
        current_usd=status.used_usd,
        limit_usd=cap,
        percentage=status.percentage or 0.0,
        gateway_key_id=identity.gateway_key_id,
        user_id=identity.user_id,
        team_id=identity.team_id,
    )
    try:
        bus.emit(
            make_event(
                type="quota.alert",
                session_id=_synthetic_session_id(),
                actor=Actor.SYSTEM,
                payload=payload,
                timestamp=datetime.now(UTC),
            )
        )
    except Exception:
        logger.warning("failed to emit quota.alert", exc_info=True)


def _emit_quota_exceeded(
    *,
    bus: EventBus,
    status: QuotaStatus,
    identity: Identity,
    inbound_shape: Literal["openai", "anthropic"],
) -> None:
    cap = status.cap_usd or Decimal("0")
    payload = GatewayQuotaExceeded(
        scope=status.scope,
        current_usd=status.used_usd,
        limit_usd=cap,
        inbound_shape=inbound_shape,
        gateway_key_id=identity.gateway_key_id,
        user_id=identity.user_id,
        team_id=identity.team_id,
    )
    try:
        bus.emit(
            make_event(
                type="gateway.quota_exceeded",
                session_id=_synthetic_session_id(),
                actor=Actor.SYSTEM,
                payload=payload,
                timestamp=datetime.now(UTC),
            )
        )
    except Exception:
        logger.warning("failed to emit gateway.quota_exceeded", exc_info=True)


def _synthetic_session_id() -> str:
    """Mint a short-lived session id for pre-routing audit events.

    The gateway is per-request stateless (gateway.md §2) — there is no
    real session to attach quota events to. The id matches the
    `gw_<ulid>` shape `harness.py` uses for its own synthetic ids so
    trace queries can recognize the prefix as gateway-scoped.
    """
    return f"gw_{new_message_id()}"
