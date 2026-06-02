"""CompactionCache — content-hash-keyed SQLite store for compaction summaries.

Schema-only substrate per `docs/specs/session-compaction.md §5.2`. The cache is
a per-workspace SQLite database keyed by a content-hash `cache_key` that the
caller computes (§5.1). LRU eviction on `last_read_at_ms`; default cap 1000
rows.

Concurrency mirrors `pattern-store.md §17`: a single `threading.RLock` wraps
every public method as defense-in-depth on the shared `sqlite3.Connection`.
Under the documented single-asyncio-task architecture the lock is uncontended.

No caller wires this in Wave 18a-4 — Wave 19 §19a-2 wires `SessionManager`.
The store exists but is unused. Likewise, no event emission lives here; the
cache is a passive store. Agent C (Wave 18a-5) registered the `session.compaction_*`
event payloads in `events/payloads.py`; the emitter is wired by Wave 19.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MAX_ROWS = 1000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS compaction_cache (
    cache_key TEXT PRIMARY KEY,
    summary_text TEXT NOT NULL,
    summarization_model TEXT NOT NULL,
    summarization_prompt_version TEXT NOT NULL,
    span_message_count INTEGER NOT NULL,
    span_token_count_in INTEGER NOT NULL,
    span_token_count_out INTEGER NOT NULL,
    created_at_ms INTEGER NOT NULL,
    last_read_at_ms INTEGER NOT NULL,
    use_count INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_compaction_cache_last_read
    ON compaction_cache(last_read_at_ms);
"""


@dataclass(frozen=True)
class CompactionRow:
    """A read of a compaction-cache row.

    Returned by `CompactionCache.read()` on a hit. Mirrors the schema
    columns in `session-compaction.md §5.2`.
    """

    cache_key: str
    summary_text: str
    summarization_model: str
    summarization_prompt_version: str
    span_message_count: int
    span_token_count_in: int
    span_token_count_out: int
    created_at_ms: int
    last_read_at_ms: int
    use_count: int


class CompactionCache:
    """Per-workspace SQLite store of compaction summaries.

    `path` is the file path (the caller decides the location; Wave 19 will
    pass `<workspace>/.metis/compaction-cache.sqlite`). Parent directory is
    created on demand.

    `max_rows` is the LRU cap; `evict_lru()` trims when the row count
    exceeds it. There is no age-based eviction in v1.
    """

    def __init__(self, path: Path, *, max_rows: int = DEFAULT_MAX_ROWS) -> None:
        if max_rows <= 0:
            raise ValueError(f"max_rows must be > 0 (got {max_rows})")
        self._path = Path(path)
        self._max_rows = int(max_rows)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # `check_same_thread=False` is required because we share one
        # connection across the asyncio task and any test/worker thread; the
        # RLock below guards cursor-result interleaving (pattern-store.md §17).
        self._conn = sqlite3.connect(str(self._path), isolation_level=None, check_same_thread=False)
        self._lock = threading.RLock()
        self._configure()
        self._conn.executescript(_SCHEMA)

    @property
    def path(self) -> Path:
        return self._path

    @property
    def max_rows(self) -> int:
        return self._max_rows

    def _configure(self) -> None:
        # Mirror trace-store / pattern-store: WAL + NORMAL.
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = NORMAL")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> CompactionCache:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # ---- Public API ------------------------------------------------------

    def read(self, cache_key: str) -> CompactionRow | None:
        """Return the cached row for `cache_key`, or None on miss.

        On hit, updates `last_read_at_ms` to `now()` and increments
        `use_count` atomically before returning. Per `session-compaction.md
        §5.3`, this is the LRU-touch behavior the cap relies on.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT cache_key, summary_text, summarization_model, "
                "summarization_prompt_version, span_message_count, "
                "span_token_count_in, span_token_count_out, created_at_ms, "
                "last_read_at_ms, use_count "
                "FROM compaction_cache WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
            if row is None:
                return None
            now_ms = _now_ms()
            self._conn.execute(
                "UPDATE compaction_cache SET last_read_at_ms = ?, "
                "use_count = use_count + 1 WHERE cache_key = ?",
                (now_ms, cache_key),
            )
            # Return the post-update view so the caller sees fresh
            # `last_read_at_ms` and incremented `use_count`.
            return CompactionRow(
                cache_key=row[0],
                summary_text=row[1],
                summarization_model=row[2],
                summarization_prompt_version=row[3],
                span_message_count=row[4],
                span_token_count_in=row[5],
                span_token_count_out=row[6],
                created_at_ms=row[7],
                last_read_at_ms=now_ms,
                use_count=row[9] + 1,
            )

    def write(
        self,
        cache_key: str,
        summary_text: str,
        *,
        summarization_model: str,
        summarization_prompt_version: str,
        span_message_count: int,
        span_token_count_in: int,
        span_token_count_out: int,
    ) -> None:
        """Insert or replace the row for `cache_key`.

        On insert, `created_at_ms` and `last_read_at_ms` are set to `now()`
        and `use_count` starts at 1. On conflict (idempotent re-write of an
        already-cached span) the existing `created_at_ms` is preserved while
        `last_read_at_ms` advances and `use_count` increments, mirroring the
        `read()` semantics.
        """
        with self._lock:
            now_ms = _now_ms()
            self._conn.execute(
                "INSERT INTO compaction_cache "
                "(cache_key, summary_text, summarization_model, "
                "summarization_prompt_version, span_message_count, "
                "span_token_count_in, span_token_count_out, created_at_ms, "
                "last_read_at_ms, use_count) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1) "
                "ON CONFLICT(cache_key) DO UPDATE SET "
                "summary_text = excluded.summary_text, "
                "summarization_model = excluded.summarization_model, "
                "summarization_prompt_version = excluded.summarization_prompt_version, "
                "span_message_count = excluded.span_message_count, "
                "span_token_count_in = excluded.span_token_count_in, "
                "span_token_count_out = excluded.span_token_count_out, "
                "last_read_at_ms = excluded.last_read_at_ms, "
                "use_count = compaction_cache.use_count + 1",
                (
                    cache_key,
                    summary_text,
                    summarization_model,
                    summarization_prompt_version,
                    int(span_message_count),
                    int(span_token_count_in),
                    int(span_token_count_out),
                    now_ms,
                    now_ms,
                ),
            )

    def evict_lru(self) -> int:
        """Trim rows so at most `max_rows` remain; return the number evicted.

        Oldest `last_read_at_ms` evicts first. Ties on `last_read_at_ms` fall
        back to `cache_key` as a deterministic tiebreaker. A no-op when the
        current row count is at or below `max_rows`.
        """
        with self._lock:
            total = self._conn.execute("SELECT COUNT(*) FROM compaction_cache").fetchone()[0]
            overflow = total - self._max_rows
            if overflow <= 0:
                return 0
            self._conn.execute(
                "DELETE FROM compaction_cache WHERE cache_key IN ("
                "  SELECT cache_key FROM compaction_cache "
                "  ORDER BY last_read_at_ms ASC, cache_key ASC "
                "  LIMIT ?"
                ")",
                (overflow,),
            )
            return overflow

    def row_count(self) -> int:
        """Return the current number of cached rows. Used by tests."""
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM compaction_cache").fetchone()[0])


# ---- helpers ---------------------------------------------------------


def _now_ms() -> int:
    """Current wall-clock time in milliseconds since the epoch.

    Local helper rather than the project-wide monotonic-id generator: the
    LRU index sorts on real elapsed time, not per-process monotonic ULID.
    """
    return int(time.time() * 1000)
