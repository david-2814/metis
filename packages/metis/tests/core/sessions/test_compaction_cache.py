"""Tests for `metis.core.sessions.compaction_cache.CompactionCache`.

Pins the schema-only behavior from `session-compaction.md §5.2 / §5.3 / §5.4`:

- Schema columns and indexes match the spec exactly.
- `write()` then `read()` round-trip; second `read()` advances
  `last_read_at_ms` and increments `use_count`.
- `evict_lru()` keeps at most `max_rows`; oldest `last_read_at_ms` evicts
  first.
- Concurrency: 100 threads x 10 writes, no `sqlite3.InterfaceError`.

No caller wiring is exercised — `SessionManager` does not touch this store
in Wave 18a-4. The Wave-19 §19a-2 patch will add SessionManager-side tests.
"""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from metis.core.sessions.compaction_cache import (
    DEFAULT_MAX_ROWS,
    CompactionCache,
    CompactionRow,
)


@pytest.fixture
def cache_path(tmp_path: Path) -> Path:
    return tmp_path / "compaction-cache.sqlite"


def _write_row(cache: CompactionCache, key: str, *, summary: str = "S") -> None:
    cache.write(
        key,
        summary,
        summarization_model="anthropic:claude-haiku-4-5",
        summarization_prompt_version="v1",
        span_message_count=4,
        span_token_count_in=1234,
        span_token_count_out=200,
    )


# ---- Schema ----------------------------------------------------------


def test_schema_matches_spec(cache_path: Path) -> None:
    """`session-compaction.md §5.2` pins the exact columns + PK + index."""
    CompactionCache(cache_path).close()
    conn = sqlite3.connect(cache_path)
    try:
        cols = {
            (row[1], row[2], bool(row[3]), bool(row[5]))
            for row in conn.execute("PRAGMA table_info(compaction_cache)").fetchall()
        }
        # (name, type, NOT NULL, is_PK)
        assert cols == {
            (
                "cache_key",
                "TEXT",
                False,
                True,
            ),  # PRIMARY KEY implies NOT NULL but PRAGMA reports NOT NULL=0
            ("summary_text", "TEXT", True, False),
            ("summarization_model", "TEXT", True, False),
            ("summarization_prompt_version", "TEXT", True, False),
            ("span_message_count", "INTEGER", True, False),
            ("span_token_count_in", "INTEGER", True, False),
            ("span_token_count_out", "INTEGER", True, False),
            ("created_at_ms", "INTEGER", True, False),
            ("last_read_at_ms", "INTEGER", True, False),
            ("use_count", "INTEGER", True, False),
        }
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(compaction_cache)").fetchall()}
        assert "idx_compaction_cache_last_read" in indexes
    finally:
        conn.close()


def test_creates_parent_directory(tmp_path: Path) -> None:
    """`<workspace>/.metis/` may not yet exist when the cache is opened."""
    nested = tmp_path / "deep" / "nested" / "compaction-cache.sqlite"
    cache = CompactionCache(nested)
    try:
        assert nested.parent.is_dir()
        assert nested.is_file()
    finally:
        cache.close()


# ---- Round-trip ------------------------------------------------------


def test_read_miss_returns_none(cache_path: Path) -> None:
    cache = CompactionCache(cache_path)
    try:
        assert cache.read("missing") is None
    finally:
        cache.close()


def test_write_then_read_round_trip(cache_path: Path) -> None:
    cache = CompactionCache(cache_path)
    try:
        _write_row(cache, "k1", summary="hello world")
        row = cache.read("k1")
        assert isinstance(row, CompactionRow)
        assert row.cache_key == "k1"
        assert row.summary_text == "hello world"
        assert row.summarization_model == "anthropic:claude-haiku-4-5"
        assert row.summarization_prompt_version == "v1"
        assert row.span_message_count == 4
        assert row.span_token_count_in == 1234
        assert row.span_token_count_out == 200
        # First read after a fresh write touches last_read_at_ms and bumps
        # use_count from 1 → 2.
        assert row.use_count == 2
        assert row.created_at_ms <= row.last_read_at_ms
    finally:
        cache.close()


def test_second_read_advances_last_read_and_increments_use_count(
    cache_path: Path,
) -> None:
    cache = CompactionCache(cache_path)
    try:
        _write_row(cache, "k1")
        first = cache.read("k1")
        assert first is not None
        # Sleep enough that `time.time() * 1000` advances at least one tick on
        # every platform. 5ms is well above the resolution floor.
        import time

        time.sleep(0.005)
        second = cache.read("k1")
        assert second is not None
        assert second.last_read_at_ms >= first.last_read_at_ms
        assert second.use_count == first.use_count + 1
        # Persisted state matches the returned view.
        conn = sqlite3.connect(cache_path)
        try:
            row = conn.execute(
                "SELECT last_read_at_ms, use_count FROM compaction_cache WHERE cache_key = ?",
                ("k1",),
            ).fetchone()
        finally:
            conn.close()
        assert row == (second.last_read_at_ms, second.use_count)
    finally:
        cache.close()


def test_write_idempotent_replaces_payload(cache_path: Path) -> None:
    """A re-write of the same key updates the payload and bumps use_count
    without disturbing `created_at_ms`."""
    cache = CompactionCache(cache_path)
    try:
        cache.write(
            "k1",
            "original",
            summarization_model="anthropic:claude-haiku-4-5",
            summarization_prompt_version="v1",
            span_message_count=3,
            span_token_count_in=100,
            span_token_count_out=50,
        )
        conn = sqlite3.connect(cache_path)
        try:
            original_created = conn.execute(
                "SELECT created_at_ms FROM compaction_cache WHERE cache_key = ?",
                ("k1",),
            ).fetchone()[0]
        finally:
            conn.close()

        cache.write(
            "k1",
            "rewritten",
            summarization_model="openai:gpt-4o-mini",
            summarization_prompt_version="v2",
            span_message_count=5,
            span_token_count_in=2000,
            span_token_count_out=300,
        )
        row = cache.read("k1")
        assert row is not None
        assert row.summary_text == "rewritten"
        assert row.summarization_model == "openai:gpt-4o-mini"
        assert row.summarization_prompt_version == "v2"
        assert row.span_message_count == 5
        assert row.span_token_count_in == 2000
        assert row.span_token_count_out == 300
        # write+read incremented use_count to at least 3 (start=1 → conflict
        # rewrite → +1 from second write → +1 from read).
        assert row.use_count >= 3
        assert row.created_at_ms == original_created
    finally:
        cache.close()


# ---- LRU eviction ----------------------------------------------------


def test_evict_lru_no_op_under_cap(cache_path: Path) -> None:
    cache = CompactionCache(cache_path, max_rows=10)
    try:
        for i in range(5):
            _write_row(cache, f"k{i}")
        assert cache.evict_lru() == 0
        assert cache.row_count() == 5
    finally:
        cache.close()


def test_evict_lru_drops_oldest_last_read_first(cache_path: Path) -> None:
    """Insert N+overflow rows, then touch a subset so they're the most
    recent. `evict_lru` should remove rows that were never re-read."""
    import time

    cache = CompactionCache(cache_path, max_rows=5)
    try:
        # Write 8 rows with a small sleep so each has a strictly later
        # `last_read_at_ms` than the one before it. After insertion the
        # oldest are k0..k2.
        for i in range(8):
            _write_row(cache, f"k{i}")
            time.sleep(0.002)
        assert cache.row_count() == 8

        # Touch k0 → its last_read_at_ms is now the most recent. Three of
        # k1..k4 are now the oldest by last_read_at_ms.
        touched = cache.read("k0")
        assert touched is not None

        evicted = cache.evict_lru()
        assert evicted == 3
        assert cache.row_count() == 5
        # The three oldest-not-touched rows are gone.
        assert cache.read("k1") is None
        assert cache.read("k2") is None
        assert cache.read("k3") is None
        # The touched row survives.
        kept = cache.read("k0")
        assert kept is not None
        # The most-recently-written rows also survive.
        assert cache.read("k7") is not None
    finally:
        cache.close()


# ---- Concurrency ----------------------------------------------------


def test_concurrent_writes_no_interface_error(cache_path: Path) -> None:
    """100 threads, 10 writes each. Every write lands, zero
    `sqlite3.InterfaceError`. Mirrors `pattern-store.md §17.3`'s
    `test_concurrent_record_lands_all_writes` fixture.

    Without the RLock this reproduces `InterfaceError: bad parameter or
    other API misuse` at ~36% rate (see pattern-store.md §17.3)."""
    cache = CompactionCache(cache_path, max_rows=10_000)
    try:
        n_threads = 100
        per_thread = 10
        errors: list[BaseException] = []

        def worker(t: int) -> None:
            try:
                for i in range(per_thread):
                    key = f"t{t:03d}-i{i:02d}"
                    cache.write(
                        key,
                        f"summary {t}-{i}",
                        summarization_model="anthropic:claude-haiku-4-5",
                        summarization_prompt_version="v1",
                        span_message_count=3,
                        span_token_count_in=100 + i,
                        span_token_count_out=50,
                    )
            except BaseException as exc:
                errors.append(exc)

        with ThreadPoolExecutor(max_workers=n_threads) as ex:
            futures = [ex.submit(worker, t) for t in range(n_threads)]
            for f in futures:
                f.result()

        assert errors == [], f"unexpected errors: {errors[:3]}"
        assert cache.row_count() == n_threads * per_thread
    finally:
        cache.close()


# ---- Misc ------------------------------------------------------------


def test_default_max_rows_and_validation(cache_path: Path) -> None:
    """`session-compaction.md §5.2` pins the default cap at 1000; non-positive
    `max_rows` is rejected so the LRU cap can never be skipped."""
    assert DEFAULT_MAX_ROWS == 1000
    with pytest.raises(ValueError):
        CompactionCache(cache_path, max_rows=0)
    with pytest.raises(ValueError):
        CompactionCache(cache_path, max_rows=-5)
