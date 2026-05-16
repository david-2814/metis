"""AGGREGATE_ONLY mode accumulator.

See `docs/specs/redaction.md §2`. Rolls up an event stream into a
single dict of count / sum / min / max statistics without retaining any
per-row payload. Output is safe to share for vendor reporting and ROI
presentations.

No k-anonymity / DP-noise additions in v1 (redaction.md §10 item 3).
The aggregate is deterministic and safe-by-construction for the
buyer's own data; cross-tenant pooling is out of scope.
"""

from __future__ import annotations

from collections import Counter
from decimal import Decimal

from metis_core.events.envelope import Event


class AggregateAccumulator:
    """Running aggregate over an event stream.

    Tracks total event count, count by event type, distinct sessions /
    turns / users / gateway keys, plus cost / token / latency stats
    pulled from `llm.call_completed` payloads only (the one event whose
    PSEUDONYMOUS payload carries those fields). Deterministic — calling
    `absorb(e)` in any order yields the same finalized dict.
    """

    def __init__(self) -> None:
        self._event_count = 0
        self._events_by_type: Counter[str] = Counter()
        self._sessions: set[str] = set()
        self._turns: set[str] = set()
        self._users: set[str] = set()
        self._gateway_keys: set[str] = set()
        self._llm_call_count = 0
        self._cost_sum_usd = Decimal("0")
        self._cost_min_usd: Decimal | None = None
        self._cost_max_usd: Decimal | None = None
        self._input_token_sum = 0
        self._output_token_sum = 0
        self._latency_ms_sum = 0
        self._latency_ms_min: int | None = None
        self._latency_ms_max: int | None = None

    def absorb(self, event: Event) -> None:
        self._event_count += 1
        self._events_by_type[event.type] += 1
        self._sessions.add(event.session_id)
        if event.turn_id is not None:
            self._turns.add(event.turn_id)
        payload = event.payload
        user_id = payload.get("user_id")
        if isinstance(user_id, str) and user_id:
            self._users.add(user_id)
        gateway_key_id = payload.get("gateway_key_id")
        if isinstance(gateway_key_id, str) and gateway_key_id:
            self._gateway_keys.add(gateway_key_id)
        if event.type == "llm.call_completed":
            self._llm_call_count += 1
            cost = payload.get("cost_usd")
            if cost is not None:
                cost_dec = Decimal(str(cost))
                self._cost_sum_usd += cost_dec
                if self._cost_min_usd is None or cost_dec < self._cost_min_usd:
                    self._cost_min_usd = cost_dec
                if self._cost_max_usd is None or cost_dec > self._cost_max_usd:
                    self._cost_max_usd = cost_dec
            input_tokens = payload.get("input_tokens")
            if isinstance(input_tokens, int):
                self._input_token_sum += input_tokens
            output_tokens = payload.get("output_tokens")
            if isinstance(output_tokens, int):
                self._output_token_sum += output_tokens
            latency = payload.get("latency_ms")
            if isinstance(latency, int):
                self._latency_ms_sum += latency
                if self._latency_ms_min is None or latency < self._latency_ms_min:
                    self._latency_ms_min = latency
                if self._latency_ms_max is None or latency > self._latency_ms_max:
                    self._latency_ms_max = latency

    def finalize(self) -> dict:
        """Return a JSON-serializable rollup. Decimal is stringified."""
        return {
            "event_count": self._event_count,
            "events_by_type": dict(sorted(self._events_by_type.items())),
            "distinct_sessions": len(self._sessions),
            "distinct_turns": len(self._turns),
            "distinct_users": len(self._users),
            "distinct_gateway_keys": len(self._gateway_keys),
            "llm_call_count": self._llm_call_count,
            "cost_usd_sum": str(self._cost_sum_usd),
            "cost_usd_min": (str(self._cost_min_usd) if self._cost_min_usd is not None else None),
            "cost_usd_max": (str(self._cost_max_usd) if self._cost_max_usd is not None else None),
            "input_tokens_sum": self._input_token_sum,
            "output_tokens_sum": self._output_token_sum,
            "latency_ms_sum": self._latency_ms_sum,
            "latency_ms_min": self._latency_ms_min,
            "latency_ms_max": self._latency_ms_max,
        }
