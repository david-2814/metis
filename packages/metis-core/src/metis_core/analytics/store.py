"""AnalyticsStore: read-only SQL projections over the events / sessions / messages tables.

Implementation of the endpoints defined in `docs/specs/analytics-api.md`. Opens
its own read-only SQLite connection (separate from TraceStore / SqliteSessionStore)
since the queries are projections, not mutations.

Decimal convention (analytics-api.md §5.1):
- Aggregate costs in `Decimal` end-to-end.
- Quantize to 6 decimal places at the response boundary, then emit as JSON number.
- Stamped values pass through unchanged.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path

from metis_core.adapters.protocol import TokenUsage
from metis_core.analytics.errors import (
    InvalidGroupByError,
    InvalidOrderError,
    TurnNotFoundError,
    UnknownBaselineModelError,
)
from metis_core.analytics.windows import TimeWindow
from metis_core.pricing import PriceTable

# Allowed enums per spec.
_COST_GROUP_BY_ALLOWED: tuple[str, ...] = (
    "model",
    "provider",
    "session",
    "day",
    "hour",
    "none",
)
_SESSIONS_ORDER_ALLOWED: tuple[str, ...] = ("cost", "recency")
_SESSIONS_ORDER_COLUMN: dict[str, str] = {
    "cost": "s.cost_so_far_usd",
    "recency": "s.updated_at",
}

# Closed enum from routing-engine.md §4.1 — the seven policy slots.
_POLICY_SLOTS: tuple[str, ...] = (
    "per_message_override",
    "manual_sticky",
    "rule",
    "pattern",
    "delegate_request",
    "workspace_default",
    "global_default",
)


_DEC_QUANT = Decimal("0.000001")


def _dec_to_json(value: Decimal) -> float:
    """Quantize to 6 decimal places and convert to JSON number (float).

    The string round-trip avoids IEEE 754 surprises at the boundary; the
    result is within 1e-15 of the true Decimal at our value range, well
    under the SPA's cent-precision render.
    """
    return float(format(value.quantize(_DEC_QUANT, rounding=ROUND_HALF_EVEN), "f"))


class AnalyticsStore:
    """Read-only access to aggregated metrics derived from the trace + session DB.

    **Concurrency note.** Holds a single SQLite connection. At single-user
    loopback scale, the Starlette handlers run on the asyncio loop and call
    these methods synchronously, so the GIL serializes access — no observable
    concurrent use. For a multi-client deployment (post-§3 fork in STRATEGY.md),
    swap to a per-request connection or wrap calls in `asyncio.to_thread`.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._conn = sqlite3.connect(
            self._db_path,
            isolation_level=None,
            check_same_thread=False,
        )
        # WAL was set by the writers; we just need to ensure readers see it.
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.row_factory = sqlite3.Row

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> AnalyticsStore:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # ---- /analytics/cost ---------------------------------------------------

    def cost(self, window: TimeWindow, group_by: str) -> dict | list[dict]:
        """Aggregate cost / tokens / latency over the window.

        Aggregation happens **in Python with `Decimal`** per spec §5.1 — the
        SQL query selects raw rows plus a group-key expression; this method
        sums in `Decimal` and emits quantized JSON numbers at the boundary.
        SQL aggregation with `SUM()` would drift via float at the 12th decimal,
        below display precision but outside the spec contract.
        """
        if group_by not in _COST_GROUP_BY_ALLOWED:
            raise InvalidGroupByError(group_by, _COST_GROUP_BY_ALLOWED)

        # Whitelist-mapped SQL fragments. Never interpolate raw request.
        key_select, key_names, time_series = _cost_key_shape(group_by)
        prefix = f"{key_select}, " if key_select else ""
        sql = (
            f"SELECT {prefix}"
            "  json_extract(payload_json, '$.cost_usd') AS cost_usd, "
            "  json_extract(payload_json, '$.input_tokens') AS input_tokens, "
            "  json_extract(payload_json, '$.output_tokens') AS output_tokens, "
            "  json_extract(payload_json, '$.cached_input_tokens') AS cached_input_tokens, "
            "  json_extract(payload_json, '$.cache_creation_input_tokens') AS cache_creation_input_tokens, "
            "  json_extract(payload_json, '$.latency_ms') AS latency_ms "
            "FROM events "
            "WHERE type = 'llm.call_completed' "
            "  AND timestamp_us >= ? AND timestamp_us < ?"
        )
        cursor = self._conn.execute(sql, (window.start_us, window.end_us))

        # Aggregate by composite key in Python. For `group_by=none`, every row
        # collapses into a single bucket keyed by `()`.
        aggregates: dict[tuple, dict] = {}
        for row in cursor:
            key = tuple(row[n] for n in key_names) if key_names else ()
            agg = aggregates.get(key)
            if agg is None:
                agg = {n: row[n] for n in key_names}
                agg.update(_init_aggregate())
                aggregates[key] = agg
            agg["cost_usd"] += _coerce_decimal(row["cost_usd"])
            agg["input_tokens"] += int(row["input_tokens"] or 0)
            agg["output_tokens"] += int(row["output_tokens"] or 0)
            agg["cached_input_tokens"] += int(row["cached_input_tokens"] or 0)
            agg["cache_creation_input_tokens"] += int(row["cache_creation_input_tokens"] or 0)
            if row["latency_ms"] is not None:
                agg["_latency_sum"] += int(row["latency_ms"])
                agg["_latency_count"] += 1
            agg["call_count"] += 1

        results = [_finalize_cost_aggregate(agg) for agg in aggregates.values()]
        # Sort: time-series ascending by bucket; everything else descending by
        # cost. Time-series order is load-bearing for the SPA's chart rendering.
        if time_series:
            results.sort(key=lambda r: r[key_names[0]])
        else:
            results.sort(key=lambda r: r["cost_usd"], reverse=True)

        if group_by == "none":
            return results[0] if results else _empty_cost_row()
        return results

    # ---- /analytics/cache_effectiveness -----------------------------------

    def cache_effectiveness(self, window: TimeWindow) -> list[dict]:
        sql = (
            "SELECT json_extract(payload_json, '$.model') AS model, "
            "  COALESCE(SUM(json_extract(payload_json, '$.input_tokens')), 0) AS uncached, "
            "  COALESCE(SUM(json_extract(payload_json, '$.cached_input_tokens')), 0) AS cached, "
            "  COALESCE(SUM(json_extract(payload_json, '$.cache_creation_input_tokens')), 0) AS write, "
            "  COUNT(*) AS call_count "
            "FROM events "
            "WHERE type = 'llm.call_completed' "
            "  AND timestamp_us >= ? AND timestamp_us < ? "
            "GROUP BY model"
        )
        out: list[dict] = []
        for row in self._conn.execute(sql, (window.start_us, window.end_us)):
            uncached = int(row["uncached"])
            cached = int(row["cached"])
            write = int(row["write"])
            total = uncached + cached + write
            out.append(
                {
                    "model": row["model"],
                    "uncached_input_tokens": uncached,
                    "cached_input_tokens": cached,
                    "cache_creation_tokens": write,
                    "hit_rate": (cached / total) if total > 0 else None,
                    "cache_write_share": (write / total) if total > 0 else None,
                    "call_count": int(row["call_count"]),
                }
            )
        return out

    # ---- /analytics/routing -----------------------------------------------

    def routing(self, window: TimeWindow) -> dict:
        sql = (
            "SELECT payload_json FROM events "
            "WHERE type = 'route.decided' "
            "  AND timestamp_us >= ? AND timestamp_us < ?"
        )
        wins_by_policy: Counter[str] = Counter()
        wins_by_model: Counter[str] = Counter()
        rejections: Counter[tuple[str, str]] = Counter()
        hard_failures = 0
        for row in self._conn.execute(sql, (window.start_us, window.end_us)):
            payload = json.loads(row["payload_json"])
            chain = payload.get("chain", [])
            winner = payload.get("winner_index", -1)
            if 0 <= winner < len(chain):
                wins_by_policy[chain[winner]["policy"]] += 1
                if payload.get("chosen_model"):
                    wins_by_model[payload["chosen_model"]] += 1
            else:
                hard_failures += 1
            # Rejections counted from every event (including hard failures).
            for entry in chain:
                if entry.get("verdict") == "rejected":
                    rejections[(entry["policy"], entry.get("validation_failure") or "")] += 1
        return {
            "wins_by_policy": [
                {"policy": p, "count": wins_by_policy.get(p, 0)} for p in _POLICY_SLOTS
            ],
            "hard_failures": hard_failures,
            "rejections": [
                {"policy": p, "validation_failure": f, "count": c}
                for (p, f), c in sorted(rejections.items(), key=lambda kv: -kv[1])
            ],
            "wins_by_model": [
                {"chosen_model": m, "count": c}
                for m, c in sorted(wins_by_model.items(), key=lambda kv: -kv[1])
            ],
        }

    # ---- /analytics/reliability -------------------------------------------

    def reliability(self, window: TimeWindow) -> dict:
        err_sql = (
            "SELECT json_extract(payload_json, '$.model') AS model, "
            "  json_extract(payload_json, '$.provider') AS provider, "
            "  json_extract(payload_json, '$.error_class') AS error_class, "
            "  COUNT(*) AS count "
            "FROM events "
            "WHERE type = 'llm.call_failed' "
            "  AND timestamp_us >= ? AND timestamp_us < ? "
            "GROUP BY model, provider, error_class"
        )
        errors = [
            {
                "model": row["model"],
                "provider": row["provider"],
                "error_class": row["error_class"],
                "count": int(row["count"]),
            }
            for row in self._conn.execute(err_sql, (window.start_us, window.end_us))
        ]

        lat_sql = (
            "SELECT json_extract(payload_json, '$.model') AS model, "
            "  json_extract(payload_json, '$.latency_ms') AS latency_ms "
            "FROM events "
            "WHERE type = 'llm.call_completed' "
            "  AND timestamp_us >= ? AND timestamp_us < ? "
            "ORDER BY model, latency_ms"
        )
        per_model: dict[str, list[int]] = defaultdict(list)
        for row in self._conn.execute(lat_sql, (window.start_us, window.end_us)):
            if row["latency_ms"] is None:
                continue
            per_model[row["model"]].append(int(row["latency_ms"]))
        latency = [
            {
                "model": model,
                "p50": _percentile(values, 0.50),
                "p95": _percentile(values, 0.95),
                "sample_size": len(values),
            }
            for model, values in sorted(per_model.items())
        ]

        return {"errors_by_class": errors, "latency_ms_by_model": latency}

    # ---- /analytics/sessions ----------------------------------------------

    def sessions(self, *, limit: int = 25, order: str = "recency") -> list[dict]:
        if order not in _SESSIONS_ORDER_ALLOWED:
            raise InvalidOrderError(order, _SESSIONS_ORDER_ALLOWED)
        order_col = _SESSIONS_ORDER_COLUMN[order]
        sql = (
            "SELECT s.id, s.workspace_path, s.active_model, "
            "  s.cost_so_far_usd, s.turn_count, "
            "  s.created_at, s.updated_at "
            "FROM sessions s "
            f"ORDER BY {order_col} DESC "
            "LIMIT ?"
        )
        return [
            {
                "id": row["id"],
                "workspace_path": row["workspace_path"],
                "active_model": row["active_model"],
                "cost_usd": float(row["cost_so_far_usd"]),
                "turn_count": int(row["turn_count"]),
                "created_at": _us_to_iso(int(row["created_at"])),
                "updated_at": _us_to_iso(int(row["updated_at"])),
            }
            for row in self._conn.execute(sql, (limit,))
        ]

    # ---- /analytics/turns/{turn_id} ---------------------------------------

    def turn(self, turn_id: str, *, now: datetime | None = None) -> dict:
        ev_sql = (
            "SELECT id, timestamp_us, session_id, type, actor, payload_json, "
            "  parent_event_id "
            "FROM events WHERE turn_id = ? ORDER BY id"
        )
        ev_rows = self._conn.execute(ev_sql, (turn_id,)).fetchall()
        if not ev_rows:
            raise TurnNotFoundError(turn_id)

        session_id = ev_rows[0]["session_id"]
        # Bounds: turn.started timestamp on the low end, turn.completed/cancelled
        # on the high end. If neither terminator is present, the turn is in-flight
        # and the upper bound becomes now().
        start_us: int | None = None
        end_us: int | None = None
        in_flight = True
        for r in ev_rows:
            if r["type"] == "turn.started":
                start_us = int(r["timestamp_us"])
            if r["type"] in ("turn.completed", "turn.cancelled"):
                end_us = int(r["timestamp_us"])
                in_flight = False
        if start_us is None:
            # Defensive: a turn missing turn.started shouldn't happen but we still
            # render whatever events we have.
            start_us = int(ev_rows[0]["timestamp_us"])
        if end_us is None:
            end_us = _to_micros(now or datetime.now(UTC))

        msg_sql = (
            "SELECT id, role, content_json, metadata_json, created_at "
            "FROM messages "
            "WHERE session_id = ? AND created_at BETWEEN ? AND ? "
            "ORDER BY created_at, id"
        )
        msg_rows = self._conn.execute(msg_sql, (session_id, start_us, end_us)).fetchall()

        events = [
            {
                "id": r["id"],
                "timestamp": _us_to_iso(int(r["timestamp_us"])),
                "type": r["type"],
                "actor": r["actor"],
                "payload": json.loads(r["payload_json"]),
                "parent_event_id": r["parent_event_id"],
            }
            for r in ev_rows
        ]
        messages = [
            {
                "id": r["id"],
                "role": r["role"],
                "content": json.loads(r["content_json"]),
                "metadata": json.loads(r["metadata_json"]),
                "created_at": _us_to_iso(int(r["created_at"])),
            }
            for r in msg_rows
        ]
        return {
            "turn_id": turn_id,
            "session_id": session_id,
            "in_flight": in_flight,
            "events": events,
            "messages": messages,
        }

    # ---- /analytics/savings -----------------------------------------------

    def savings(
        self,
        window: TimeWindow,
        *,
        baseline: str,
        price_table: PriceTable,
    ) -> dict:
        if baseline not in price_table:
            raise UnknownBaselineModelError(baseline)

        sql = (
            "SELECT json_extract(payload_json, '$.model') AS model, "
            "  json_extract(payload_json, '$.input_tokens') AS input_tokens, "
            "  json_extract(payload_json, '$.output_tokens') AS output_tokens, "
            "  json_extract(payload_json, '$.cached_input_tokens') AS cached_input_tokens, "
            "  json_extract(payload_json, '$.cache_creation_input_tokens') AS cache_creation_input_tokens, "
            "  json_extract(payload_json, '$.cost_usd') AS cost_usd "
            "FROM events "
            "WHERE type = 'llm.call_completed' "
            "  AND timestamp_us >= ? AND timestamp_us < ?"
        )
        actual_repriced = Decimal("0")
        baseline_repriced = Decimal("0")
        actual_stamped = Decimal("0")
        rows_total = 0
        rows_missing = 0
        for row in self._conn.execute(sql, (window.start_us, window.end_us)):
            rows_total += 1
            usage = TokenUsage(
                input_tokens=int(row["input_tokens"] or 0),
                output_tokens=int(row["output_tokens"] or 0),
                cached_input_tokens=int(row["cached_input_tokens"] or 0),
                cache_creation_input_tokens=int(row["cache_creation_input_tokens"] or 0),
            )
            # Stamped is unconditional — passed through whether or not the
            # model is currently priced.
            actual_stamped += _coerce_decimal(row["cost_usd"])
            # Baseline always uses the (validated) baseline model's rates.
            baseline_repriced += price_table.compute_cost(baseline, usage)
            # Re-priced actual requires the row's model to be in the current
            # table; otherwise it counts as missing.
            row_model = row["model"]
            if row_model in price_table:
                actual_repriced += price_table.compute_cost(row_model, usage)
            else:
                rows_missing += 1

        savings = baseline_repriced - actual_repriced
        savings_pct = float(savings / baseline_repriced) if baseline_repriced > 0 else 0.0
        return {
            "baseline_model": baseline,
            "actual_repriced_usd": _dec_to_json(actual_repriced),
            "baseline_repriced_usd": _dec_to_json(baseline_repriced),
            "savings_usd": _dec_to_json(savings),
            "savings_pct": savings_pct,
            "actual_stamped_usd": _dec_to_json(actual_stamped),
            "rows_total": rows_total,
            "rows_missing_from_price_table": rows_missing,
        }


# ---------------------------------------------------------------------------
# Module-private helpers
# ---------------------------------------------------------------------------


def _cost_key_shape(group_by: str) -> tuple[str, tuple[str, ...], bool]:
    """Return (key_select_sql, key_result_names, is_time_series).

    `key_select_sql` is interpolated into the SELECT clause. `key_result_names`
    is the tuple of result-set column names that identify each row's group.
    `is_time_series` flags day/hour so the result list sorts ascending by bucket
    instead of descending by cost.
    """
    if group_by == "model":
        return (
            "json_extract(payload_json, '$.model') AS model, "
            "json_extract(payload_json, '$.provider') AS provider",
            ("model", "provider"),
            False,
        )
    if group_by == "provider":
        return (
            "json_extract(payload_json, '$.provider') AS provider",
            ("provider",),
            False,
        )
    if group_by == "session":
        return ("session_id", ("session_id",), False)
    if group_by == "day":
        return (
            "date(timestamp_us/1000000, 'unixepoch') AS bucket",
            ("bucket",),
            True,
        )
    if group_by == "hour":
        return (
            "strftime('%Y-%m-%dT%H', timestamp_us/1000000, 'unixepoch') AS bucket",
            ("bucket",),
            True,
        )
    # group_by=none: no key columns; all rows fold into one bucket.
    return ("", (), False)


def _init_aggregate() -> dict:
    """Initial accumulator: Decimal cost, int tokens, latency sum + count."""
    return {
        "cost_usd": Decimal("0"),
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "_latency_sum": 0,
        "_latency_count": 0,
        "call_count": 0,
    }


def _finalize_cost_aggregate(agg: dict) -> dict:
    """Convert internal Decimal/sum state to the response dict shape."""
    out = {
        k: v
        for k, v in agg.items()
        if k not in ("_latency_sum", "_latency_count", "cost_usd")
    }
    out["cost_usd"] = _dec_to_json(agg["cost_usd"])
    out["avg_latency_ms"] = (
        agg["_latency_sum"] / agg["_latency_count"] if agg["_latency_count"] > 0 else None
    )
    return out


def _empty_cost_row() -> dict:
    return {
        "cost_usd": 0.0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "avg_latency_ms": None,
        "call_count": 0,
    }


def _coerce_decimal(raw) -> Decimal:
    """SQLite json_extract returns floats / ints / strings; coerce to Decimal.

    Stamped `cost_usd` is written via `json.dumps(default=str)` in the trace
    store, so Decimals land as strings in the JSON column. SUM() over those
    JSON values yields floats from SQLite — we accept either.
    """
    if raw is None:
        return Decimal("0")
    if isinstance(raw, Decimal):
        return raw
    return Decimal(str(raw))


def _percentile(sorted_vals: list[int], p: float) -> int | None:
    """Nearest-rank percentile. `sorted_vals` must already be ascending."""
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * p
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    if f == c:
        return int(sorted_vals[f])
    interp = sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)
    return round(interp)


def _us_to_iso(us: int) -> str:
    return datetime.fromtimestamp(us / 1_000_000, tz=UTC).isoformat()


def _to_micros(dt: datetime) -> int:
    epoch = datetime(1970, 1, 1, tzinfo=dt.tzinfo)
    delta = dt - epoch
    return delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds
