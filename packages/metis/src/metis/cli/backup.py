"""`metis backup` / `metis restore` command handlers.

Thin shim over `metis.core.trace.backup`. Prints a deterministic
human-readable summary on success; emits a one-line diagnostic to stderr on
failure and returns a non-zero exit code.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

from metis.core.trace.backup import (
    BackupError,
    BackupResult,
    RestoreResult,
)
from metis.core.trace.backup import (
    backup as _backup,
)
from metis.core.trace.backup import (
    restore as _restore,
)


def _default_db_path() -> Path:
    return Path.home() / ".metis" / "metis.db"


def _iso(ts: datetime | None) -> str:
    if ts is None:
        return "—"
    return ts.isoformat()


def _print_backup_result(result: BackupResult) -> None:
    # Deterministic block — paths, byte count, schema version, event count,
    # oldest/newest. No random ids.
    print("backup complete")
    print(f"  source:         {result.source_path}")
    print(f"  destination:    {result.dest_path}")
    print(f"  bytes:          {result.byte_count}")
    print(f"  schema version: {result.schema_version}")
    print(f"  events:         {result.event_count}")
    print(f"  oldest event:   {_iso(result.oldest_event_timestamp)}")
    print(f"  newest event:   {_iso(result.newest_event_timestamp)}")


def _print_restore_result(result: RestoreResult) -> None:
    print("restore complete")
    print(f"  source:         {result.source_path}")
    print(f"  destination:    {result.dest_path}")
    print(f"  bytes:          {result.byte_count}")
    print(f"  schema version: {result.schema_version}")
    print(f"  events:         {result.event_count}")


def run_backup_command(*, dest: str, db_path: str | None) -> int:
    source = Path(db_path).expanduser() if db_path else _default_db_path()
    target = Path(dest).expanduser()
    try:
        result = _backup(source, target)
    except BackupError as exc:
        print(f"backup failed: {exc}", file=sys.stderr)
        return 1
    _print_backup_result(result)
    return 0


def run_restore_command(*, source: str, db_path: str | None, force: bool) -> int:
    src = Path(source).expanduser()
    target = Path(db_path).expanduser() if db_path else _default_db_path()
    try:
        result = _restore(src, target, allow_overwrite=force)
    except BackupError as exc:
        print(f"restore failed: {exc}", file=sys.stderr)
        return 1
    _print_restore_result(result)
    return 0
