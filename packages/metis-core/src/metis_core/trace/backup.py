"""Backup and restore for the trace DB.

Buyer-runnable recipe behind `metis backup` / `metis restore` and the helm
chart's PVC snapshot story. The contract:

- **Backup** uses SQLite's `VACUUM INTO` — atomic, WAL-safe, single-file
  output. The running trace store can keep writing during the call.
- **Restore** schema-checks the backup (`PRAGMA user_version` must match the
  running code's `TRACE_SCHEMA_VERSION`) and refuses to clobber an existing
  destination unless `allow_overwrite=True`. WAL files alongside the source
  backup are a clean-backup-invariant violation and are rejected too.

See `docs/specs/event-bus-and-trace-catalog.md` §7.5 for the spec note and
`docs/gateway-deployment.md` "Backup & restore" for the operator recipe.
"""

from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from metis_core.trace.store import TRACE_SCHEMA_VERSION


class BackupError(Exception):
    """Raised when backup or restore preconditions aren't met."""


@dataclass(frozen=True)
class BackupResult:
    """Metadata captured at backup time. Returned to callers and printed by the
    CLI so the operator has a paper trail without re-opening the DB."""

    source_path: Path
    dest_path: Path
    byte_count: int
    schema_version: int
    event_count: int
    oldest_event_timestamp: datetime | None
    newest_event_timestamp: datetime | None


@dataclass(frozen=True)
class RestoreResult:
    source_path: Path
    dest_path: Path
    byte_count: int
    schema_version: int
    event_count: int


def _open_readonly(path: Path) -> sqlite3.Connection:
    # `mode=ro` requires URI form. `immutable=0` (the default) is fine — the
    # only writer should be us during VACUUM INTO; backup callers explicitly
    # don't need to close the source connection first.
    uri = f"file:{path}?mode=ro"
    return sqlite3.connect(uri, uri=True, isolation_level=None)


def _wal_companions(path: Path) -> list[Path]:
    """Return any SQLite WAL/SHM files sitting alongside `path`."""
    return [
        sibling
        for sibling in (
            path.with_name(path.name + "-wal"),
            path.with_name(path.name + "-shm"),
        )
        if sibling.exists()
    ]


def _query_event_stats(conn: sqlite3.Connection) -> tuple[int, datetime | None, datetime | None]:
    """Return (count, oldest_ts, newest_ts) from an opened trace DB."""
    row = conn.execute(
        "SELECT COUNT(*), MIN(timestamp_us), MAX(timestamp_us) FROM events"
    ).fetchone()
    count = int(row[0])
    if count == 0:
        return count, None, None
    oldest = datetime.fromtimestamp(int(row[1]) / 1_000_000, tz=UTC)
    newest = datetime.fromtimestamp(int(row[2]) / 1_000_000, tz=UTC)
    return count, oldest, newest


def backup(source_db: Path, dest: Path) -> BackupResult:
    """Snapshot `source_db` to `dest` via `VACUUM INTO`.

    Atomic and WAL-safe — SQLite handles in-flight writes correctly. The
    source DB does not need to be closed. The destination must not already
    exist; SQLite's `VACUUM INTO` refuses to overwrite, and we don't paper
    over that because it's the right default for a backup tool.
    """
    source_db = Path(source_db)
    dest = Path(dest)

    if not source_db.exists():
        raise BackupError(f"source trace DB does not exist: {source_db}")
    if dest.exists():
        raise BackupError(
            f"backup destination already exists: {dest} "
            "(remove it or choose a different path; this tool will not overwrite a backup)"
        )
    dest.parent.mkdir(parents=True, exist_ok=True)

    # Open in read-only mode and run VACUUM INTO. This is the SQLite-blessed
    # path for a hot backup: it does not require closing the source, copies
    # only live pages, and produces a single defragmented file (no -wal /
    # -shm companions). The destination is locked for the duration.
    conn = _open_readonly(source_db)
    try:
        # `VACUUM INTO` does not accept a parameter placeholder for the path,
        # so the literal must be embedded. The path is a controlled Path
        # value (we resolve and stringify); quote it to defend against
        # single-quote characters in the path.
        dest_literal = str(dest).replace("'", "''")
        conn.execute(f"VACUUM INTO '{dest_literal}'")
        conn.commit()
    finally:
        conn.close()

    # Re-open the backup to capture metadata. The backup is itself a fully
    # valid SQLite DB, so we just open and query.
    return _summarize_backup(source_db=source_db, dest=dest)


def _summarize_backup(*, source_db: Path, dest: Path) -> BackupResult:
    byte_count = dest.stat().st_size
    conn = _open_readonly(dest)
    try:
        schema_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        count, oldest, newest = _query_event_stats(conn)
    finally:
        conn.close()
    return BackupResult(
        source_path=source_db,
        dest_path=dest,
        byte_count=byte_count,
        schema_version=schema_version,
        event_count=count,
        oldest_event_timestamp=oldest,
        newest_event_timestamp=newest,
    )


def restore(
    source: Path,
    dest_db: Path,
    *,
    allow_overwrite: bool = False,
) -> RestoreResult:
    """Restore the backup at `source` to `dest_db`.

    Schema-version check: the backup's `PRAGMA user_version` must match
    `TRACE_SCHEMA_VERSION`. A mismatch surfaces a clear error pointing the
    operator at the migration path (currently: "downgrade to the binary that
    wrote this version, run a forward-migration script, then re-restore").

    WAL companion check: `<source>-wal` or `<source>-shm` next to the source
    means the backup is mid-write or was hand-edited. Refuse — the caller
    should close the DB cleanly first.

    `allow_overwrite=False` (the default) refuses to clobber an existing
    `dest_db`. Set `True` for the documented "restore over a corrupted DB"
    flow.
    """
    source = Path(source)
    dest_db = Path(dest_db)

    if not source.exists():
        raise BackupError(f"backup source does not exist: {source}")
    stray = _wal_companions(source)
    if stray:
        joined = ", ".join(str(p) for p in stray)
        raise BackupError(
            f"backup source has WAL companion files alongside it ({joined}); "
            "the backup is not in a clean state. "
            "Close the writing process and re-take the backup with `metis backup`."
        )
    if dest_db.exists() and not allow_overwrite:
        raise BackupError(
            f"destination DB already exists: {dest_db} "
            "(pass --force to overwrite; this tool refuses by default)"
        )

    # Schema-version check happens against the source backup before we
    # touch the destination. Open read-only and read `PRAGMA user_version`
    # first; only if the version matches do we trust that the events table
    # has the expected shape and query it for the row count.
    src_conn = _open_readonly(source)
    try:
        backup_schema_version = int(src_conn.execute("PRAGMA user_version").fetchone()[0])
        if backup_schema_version != TRACE_SCHEMA_VERSION:
            raise BackupError(
                f"schema-version mismatch: backup is v{backup_schema_version}, "
                f"running code expects v{TRACE_SCHEMA_VERSION}. "
                "Restore with a matching binary, or run a forward-migration script "
                "before retrying (no in-tree migrations exist yet — v1 is the only version)."
            )
        count, _, _ = _query_event_stats(src_conn)
    finally:
        src_conn.close()

    dest_db.parent.mkdir(parents=True, exist_ok=True)
    # Remove the existing destination (and any stale WAL companions) before
    # copying. `allow_overwrite` gates the unlink; we've already enforced
    # the precondition above.
    if dest_db.exists():
        dest_db.unlink()
    for companion in _wal_companions(dest_db):
        companion.unlink()

    # File-level copy is correct here: source is a clean single-file DB
    # (we verified no WAL companions). `shutil.copy2` preserves mtime so
    # the operator can see when the backup was taken.
    shutil.copy2(source, dest_db)

    return RestoreResult(
        source_path=source,
        dest_path=dest_db,
        byte_count=dest_db.stat().st_size,
        schema_version=backup_schema_version,
        event_count=count,
    )
