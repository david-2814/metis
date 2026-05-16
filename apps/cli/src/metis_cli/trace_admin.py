"""`metis trace prune` command handler.

Thin shim over `TraceStore.purge_older_than`. The library defaults to
dry-run for programmatic safety; the CLI inverts that default so a
`CronJob` invocation doesn't need to pass an extra flag every iteration
(trace-retention.md §3.3). Operators preview with `--dry-run` first.

The bus is constructed locally so `trace.swept` emission rides through
the trace store's own subscriber chain — i.e. the same event the sweep
emits is persisted by the same `TraceStore` that just performed the
sweep, so the audit-trail invariant is preserved by construction.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from metis_core.events.bus import EventBus
from metis_core.trace.retention import PurgeResult
from metis_core.trace.store import TraceStore


def _default_db_path() -> Path:
    return Path.home() / ".metis" / "metis.db"


def _iso(ts: datetime | None) -> str:
    if ts is None:
        return "—"
    return ts.isoformat()


def _print_result(
    *,
    db_path: Path,
    days: int,
    result: PurgeResult,
) -> None:
    flag = "true" if result.dry_run else "false"
    print(f"trace prune complete (dry_run={flag})")
    print(f"  db_path:               {db_path}")
    print(f"  cutoff:                {_iso(result.cutoff_timestamp)} ({days} days)")
    print(f"  rows_eligible:         {result.rows_eligible}")
    print(f"  rows_audit_exempt:     {result.rows_audit_exempt}")
    print(f"  rows_deleted:          {result.rows_deleted}")
    print(f"  oldest_kept_timestamp: {_iso(result.oldest_kept_timestamp)}")


async def _run(
    *,
    db_path: Path,
    days: int,
    dry_run: bool,
) -> int:
    if days <= 0:
        print(f"trace prune failed: --days must be positive, got {days}", file=sys.stderr)
        return 2
    if not db_path.exists():
        print(f"trace prune failed: db_path does not exist: {db_path}", file=sys.stderr)
        return 1

    cutoff = datetime.now(UTC) - timedelta(days=days)

    bus = EventBus()
    bus.start()
    store = TraceStore(db_path)
    handle = store.attach_to(bus)
    try:
        result = store.purge_older_than(cutoff, bus=bus, dry_run=dry_run)
        # Drain the bus so the trace.swept emission (if any) is persisted
        # before we close the store.
        await bus.drain()
    finally:
        bus.unsubscribe(handle)
        await bus.stop()
        store.close()

    _print_result(db_path=db_path, days=days, result=result)
    return 0


def run_trace_prune_command(
    *,
    db_path: str | None,
    days: int,
    dry_run: bool,
) -> int:
    target = Path(db_path).expanduser() if db_path else _default_db_path()
    try:
        return asyncio.run(_run(db_path=target, days=days, dry_run=dry_run))
    except OSError as exc:
        print(f"trace prune failed: {exc}", file=sys.stderr)
        return 1


def run_trace_vacuum_command(*, db_path: str | None) -> int:
    """Rebuild the trace DB in place (`SQLite VACUUM`).

    Standalone — does NOT spin up a bus; VACUUM is a maintenance op,
    not a domain action, and it would be misleading to emit a
    `trace.swept`-class event for it. Operators run this from the
    monthly CronJob (docs/operations/trace-performance.md §4) when the
    gateway pod is paused or against a backup file.
    """
    target = Path(db_path).expanduser() if db_path else _default_db_path()
    if not target.exists():
        print(f"trace vacuum failed: db_path does not exist: {target}", file=sys.stderr)
        return 1
    try:
        store = TraceStore(target)
    except OSError as exc:
        print(f"trace vacuum failed: {exc}", file=sys.stderr)
        return 1
    try:
        delta = store.vacuum()
    except OSError as exc:
        print(f"trace vacuum failed: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()
    print("trace vacuum complete")
    print(f"  db_path:           {target}")
    print(f"  bytes_reclaimed:   {delta:,}")
    return 0
