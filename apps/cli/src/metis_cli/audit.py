"""`metis audit export` command handler.

Thin shim over `metis_core.audit`. Prints a deterministic human-readable
summary on success; emits a one-line diagnostic to stderr on failure.

See `docs/specs/audit-log.md §9`.
"""

from __future__ import annotations

import sys
from pathlib import Path

from metis_core.analytics.errors import InvalidTimeWindowError
from metis_core.analytics.windows import resolve_window
from metis_core.audit import AUDIT_EVENT_TYPES, AuditExportResult, export_audit_events
from metis_core.events.payloads import PAYLOAD_REGISTRY
from metis_core.redaction import EventRedactor, RedactionMode


def _default_db_path() -> Path:
    return Path.home() / ".metis" / "metis.db"


def _print_export_result(result: AuditExportResult, *, redact_mode: RedactionMode) -> None:
    """Deterministic block — no random ids, no current-time stamps."""
    print("audit export complete")
    print(f"  destination:    {result.dest_path}")
    print(f"  format:         {result.format}")
    print(f"  redact mode:    {redact_mode.value}")
    print(f"  events:         {result.event_count}")
    print(f"  window start:   {result.window_start.isoformat()}")
    print(f"  window end:     {result.window_end.isoformat()}")
    print(f"  oldest event:   {result.oldest_event_id or '—'}")
    print(f"  newest event:   {result.newest_event_id or '—'}")
    print(f"  bytes:          {result.byte_count}")


def run_audit_export_command(
    *,
    dest: str,
    db_path: str | None,
    format: str,
    since: str | None,
    until: str | None,
    event_types: list[str] | None,
    redact: str = "passthrough",
) -> int:
    source = Path(db_path).expanduser() if db_path else _default_db_path()
    target = Path(dest).expanduser()

    if format not in ("jsonl", "csv"):
        print(f"audit export failed: unsupported format {format!r}", file=sys.stderr)
        return 2

    try:
        mode = RedactionMode(redact)
    except ValueError:
        print(f"audit export failed: unknown redact mode {redact!r}", file=sys.stderr)
        return 2

    if not source.exists():
        print(f"audit export failed: trace DB not found: {source}", file=sys.stderr)
        return 1

    try:
        window = resolve_window(since, until)
    except InvalidTimeWindowError as exc:
        print(f"audit export failed: {exc}", file=sys.stderr)
        return 2

    if event_types:
        unknown = [t for t in event_types if t not in PAYLOAD_REGISTRY]
        if unknown:
            print(
                f"audit export failed: unknown event type(s): {', '.join(sorted(unknown))}",
                file=sys.stderr,
            )
            return 2
        non_audit = [t for t in event_types if t not in AUDIT_EVENT_TYPES]
        if non_audit:
            # Spec §8.1: silently drop non-audit types when filtering, but the
            # CLI warns so the operator notices the typo / scope mistake.
            print(
                f"warning: ignoring non-audit event type(s): {', '.join(sorted(non_audit))}",
                file=sys.stderr,
            )

    redactor = EventRedactor(mode) if mode != RedactionMode.PASSTHROUGH else None

    try:
        result = export_audit_events(
            source,
            target,
            window=window,
            format=format,  # type: ignore[arg-type]
            event_types=event_types if event_types else None,
            redactor=redactor,
        )
    except FileExistsError as exc:
        print(f"audit export failed: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"audit export failed: {exc}", file=sys.stderr)
        return 1

    _print_export_result(result, redact_mode=mode)
    return 0
