"""Audit log: filtered projection of the trace store.

See `docs/specs/audit-log.md`. Audit events are a subset of trace events
flagged as security/compliance-relevant; the audit log surfaces them as a
deterministic export for SIEM ingest. No parallel write path — derives the
audit tier from the same SQLite trace DB.
"""

from __future__ import annotations

from metis_core.audit.log import AuditExportResult, AuditLog, export_audit_events
from metis_core.events.payloads import AUDIT_EVENT_TYPES, is_audit_event

__all__ = [
    "AUDIT_EVENT_TYPES",
    "AuditExportResult",
    "AuditLog",
    "export_audit_events",
    "is_audit_event",
]
