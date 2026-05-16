"""Trace-DB retention helpers per trace-retention.md.

`PurgeResult` is the typed return shape for `TraceStore.purge_older_than`.
The audit-event flag is owned by Wave 12a-1 — this module re-exports
`is_audit_event` / `AUDIT_EVENT_TYPES` from `metis_core.events.payloads`
so the sweep code reads a single source of truth. `trace.swept` is
included in `AUDIT_EVENT_TYPES` so the sweep cannot delete its own
history (trace-retention.md §6.3).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from metis_core.events.payloads import AUDIT_EVENT_TYPES, is_audit_event

__all__ = [
    "AUDIT_EVENT_TYPES",
    "PurgeResult",
    "is_audit_event",
]


@dataclass(frozen=True)
class PurgeResult:
    """Outcome of a `TraceStore.purge_older_than` call.

    `rows_deleted` is 0 in dry-run mode even when `rows_eligible > 0`.
    `oldest_kept_timestamp` is None when the DB is empty after the
    sweep (or empty going in).
    """

    cutoff_timestamp: datetime
    rows_eligible: int
    rows_audit_exempt: int
    rows_deleted: int
    oldest_kept_timestamp: datetime | None
    dry_run: bool
    swept_at: datetime
