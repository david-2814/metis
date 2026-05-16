"""Redaction layer for trace exports + GDPR right-to-be-forgotten.

See `docs/specs/redaction.md`. Composes two contracts:

1. **`Redactor` Protocol** (shipped by 12a-2) — the
   `forget_user(user_id) -> int` contract behind the
   `/analytics/user/{user_id}/forget` HTTP endpoint. The default impl
   (`PseudonymizingRedactor`) does an in-place SQL update of every
   event row carrying the user_id.

2. **`EventRedactor`** (this spec) — the mode-driven per-event redactor
   for trace exports. Composable with any `Iterable[Event]` source
   (TraceStore queries, AnalyticsStore.user_export, future audit-log
   projections). Four modes: `passthrough`, `pseudonymize`,
   `redact_private`, `aggregate_only`.

`pseudonym_for()` is the single source of truth for the
identity-hashing format used by both contracts — a row pseudonymized
by `forget_user` and re-exported under `pseudonymize` produces the
same value byte-for-byte.
"""

from __future__ import annotations

from metis_core.redaction.aggregator import AggregateAccumulator
from metis_core.redaction.default import PseudonymizingRedactor, pseudonym_for
from metis_core.redaction.event_redactor import EventRedactor, pseudonymize_value
from metis_core.redaction.forget import ForgetResult, forget_user
from metis_core.redaction.modes import (
    PRIVATE_TEXT_FIELDS,
    PSEUDONYM_PREFIX,
    REDACTED_SENTINEL,
    PseudonymTag,
    RedactionMode,
)
from metis_core.redaction.protocol import Redactor

__all__ = [
    "PRIVATE_TEXT_FIELDS",
    "PSEUDONYM_PREFIX",
    "REDACTED_SENTINEL",
    "AggregateAccumulator",
    "EventRedactor",
    "ForgetResult",
    "PseudonymTag",
    "PseudonymizingRedactor",
    "RedactionMode",
    "Redactor",
    "forget_user",
    "pseudonym_for",
    "pseudonymize_value",
]
