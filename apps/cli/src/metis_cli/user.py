"""`metis analytics user-export` and `metis user forget` command handlers.

Local-FS mirrors of the HTTP `/analytics/user/{user_id}/export` and
`/analytics/user/{user_id}/forget` surfaces in apps/server. The CLI path
exists because the buyer's compliance workflow ("download every event for
ex-employee X to honor their portability request, then forget them") is
typically run from an admin shell — not a browser session against the
loopback dashboard.

The export half opens the trace DB directly via `AnalyticsStore`. The
forget half delegates to `metis_core.redaction.forget.forget_user` —
shipped by 12a-3 (redaction.md §5) — which owns the pseudonymization
and audit-event emission. This keeps the policy in one place; the CLI
is a thin presentation shim.
"""

from __future__ import annotations

import sys
from pathlib import Path

from metis_core.analytics import AnalyticsStore, InvalidTimeWindowError, resolve_window
from metis_core.redaction.forget import forget_user as _forget_user


def _default_db_path() -> Path:
    return Path.home() / ".metis" / "metis.db"


def run_user_export_command(
    *,
    user_id: str,
    from_: str | None,
    to: str | None,
    out: str | None,
    db_path: str | None,
) -> int:
    """Stream every event for `user_id` to `out` (or stdout) as JSONL.

    `from_` / `to` are ISO 8601 UTC strings; either or both may be omitted
    (the analytics window helper applies the same defaults the HTTP
    endpoint does — last 7d if both omitted, otherwise as supplied).
    Returns 0 on success, 1 on a malformed window.
    """
    target_db = Path(db_path).expanduser() if db_path else _default_db_path()
    if not target_db.exists():
        print(f"trace DB not found at {target_db}", file=sys.stderr)
        return 1
    try:
        # The CLI export reuses the resolve_window default of "last 7d when
        # both omitted", but for compliance / audit workflows the caller
        # usually wants "all time." Skip window resolution when nothing was
        # passed so the export covers everything stamped for this user.
        if from_ is None and to is None:
            window = None
        else:
            window = resolve_window(from_, to)
    except InvalidTimeWindowError as exc:
        print(f"invalid time window: {exc.message}", file=sys.stderr)
        return 1

    out_path = Path(out).expanduser() if out else None
    row_count = 0
    byte_count = 0
    with AnalyticsStore(target_db) as store:
        if out_path is not None:
            with out_path.open("wb") as fh:
                for chunk in store.user_export(user_id, window=window):
                    fh.write(chunk)
                    byte_count += len(chunk)
                    row_count += 1
        else:
            # Stdout. Write bytes directly via sys.stdout.buffer so we
            # don't double-encode through Python's text wrapper.
            for chunk in store.user_export(user_id, window=window):
                sys.stdout.buffer.write(chunk)
                byte_count += len(chunk)
                row_count += 1
            sys.stdout.buffer.flush()

    # Deterministic single-line summary to stderr (so stdout stays
    # pipe-able to `jq`). Mirrors the audit shape the HTTP endpoint emits.
    print(
        f"user-export complete: user_id={user_id} rows={row_count} bytes={byte_count}",
        file=sys.stderr,
    )
    return 0


def run_user_forget_command(
    *,
    user_id: str,
    confirm: bool,
    db_path: str | None,
) -> int:
    """Pseudonymize every event for `user_id` via 12a-3's `forget_user`.

    `--confirm` is required; without it the command refuses. The
    pseudonymization + audit-event emission lives in
    `metis_core.redaction.forget.forget_user`; this handler is a thin
    presentation shim.

    Returns 0 on success, 2 on missing --confirm, 1 on DB error.
    """
    target_db = Path(db_path).expanduser() if db_path else _default_db_path()
    if not target_db.exists():
        print(f"trace DB not found at {target_db}", file=sys.stderr)
        return 1

    if not confirm:
        # Dry-run: surface the count that would be touched so the operator
        # can validate scope before re-running with --confirm
        # (redaction.md §5).
        dry = _forget_user(target_db, user_id, confirm=False)
        print(
            f"refusing to forget without explicit --confirm flag.\n"
            f"  this would pseudonymize {dry.matched_events} event(s) "
            f"stamped with user_id={user_id} in the trace store.\n"
            f"  re-run with --confirm to proceed.",
            file=sys.stderr,
        )
        return 2

    try:
        result = _forget_user(target_db, user_id, confirm=True)
    except FileNotFoundError as exc:
        print(f"forget failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"forget failed: {exc}", file=sys.stderr)
        return 1

    print("user-forget complete")
    print(f"  user_id:                {result.user_id}")
    print(f"  pseudonym:              {result.pseudonym}")
    print(f"  pseudonymized rows:     {result.pseudonymized_rows}")
    return 0
