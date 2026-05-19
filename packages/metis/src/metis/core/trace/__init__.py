"""Trace store — durable record of bus events.

See event-bus-and-trace-catalog.md §7.
"""

from metis.core.trace.backup import (
    BackupError,
    BackupResult,
    RestoreResult,
    backup,
    restore,
)
from metis.core.trace.retention import PurgeResult
from metis.core.trace.store import TRACE_SCHEMA_VERSION, TraceStore

__all__ = [
    "TRACE_SCHEMA_VERSION",
    "BackupError",
    "BackupResult",
    "PurgeResult",
    "RestoreResult",
    "TraceStore",
    "backup",
    "restore",
]
