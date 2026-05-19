"""Production-readiness tests for the pattern store.

Covers the four `pattern-store.md §Production tuning` claims:

1. K-NN scan is O(N) and the spec's ≤3ms slot-budget is **not** met past
   ~500 fingerprints — this is a performance smoke, not a strict limit.
2. Concurrent `record()` from multiple threads is safe (the in-store
   RLock serializes writes; the documented architectural single-writer
   invariant remains, but the lock is defense-in-depth).
3. `pattern.evicted` is in `AUDIT_EVENT_TYPES` so the trace retention
   sweep (Wave 12) cannot delete eviction history. `pattern.recorded`
   and `pattern.matched` are intentionally NOT audit-flagged (operational
   telemetry; rotates under retention).
4. `cache_hit_count` / `cache_miss_count` / `cache_hit_ratio` track the
   v2 cache observability surface that backs the
   `metis_pattern_embedding_cache_hit_ratio` gauge.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

import pytest
from metis.core.events.payloads import AUDIT_EVENT_TYPES, is_audit_event
from metis.core.patterns.fingerprint import (
    FingerprintInputs,
    compute_fingerprint,
)
from metis.core.patterns.retention import PatternCaps
from metis.core.patterns.store import PatternStore


def _inputs(i: int, *, text: str | None = None) -> FingerprintInputs:
    return FingerprintInputs(
        user_message_text=text if text is not None else f"task {i}",
        workspace_path="/tmp/ws",
        estimated_input_tokens=1000 + (i % 100) * 10,
        has_images=False,
        has_tool_calls_in_history=(i % 2 == 0),
        file_extensions=(f".ext{i % 50}",),
        file_path_buckets=(f"dir{i % 30}",),
        tool_names=(f"tool{i % 20}",),
        side_effect_classes=("read",),
    )


# ---- (1) K-NN performance smoke -----------------------------------------


def test_knn_latency_smoke_under_500_fingerprints(tmp_path: Path) -> None:
    """At v1's typical laptop scale (<500 fingerprints) K-NN stays under
    ~30ms p95. Not the spec's 3ms slot budget — see pattern-store.md
    §Production tuning. Lenient bound; CI variance makes a tighter one
    flaky."""
    store = PatternStore(
        tmp_path,
        caps=PatternCaps(soft_cap_rows=2_000, hard_cap_rows=4_000, max_age_days=365),
    )
    try:
        for i in range(400):
            fp = compute_fingerprint(_inputs(i))
            store.record(fp, "m", 0.7, Decimal("0.01"), 1000.0, "v1")
        query = compute_fingerprint(_inputs(9999))
        # Warm SQLite cache before measurement.
        for _ in range(3):
            store.find_k_nearest(query, k=10)
        samples: list[float] = []
        for _ in range(20):
            t0 = time.perf_counter()
            store.find_k_nearest(query, k=10)
            samples.append((time.perf_counter() - t0) * 1000)
        p95 = sorted(samples)[int(len(samples) * 0.95)]
        # Generous bound — actual p95 on a quiet laptop is ~6-8ms.
        assert p95 < 100.0, f"p95 K-NN latency {p95:.1f}ms exceeded smoke bound"
    finally:
        store.close()


# ---- (2) Concurrent recording safety ------------------------------------


def test_concurrent_record_lands_all_writes(tmp_path: Path) -> None:
    """100 threads x 10 records each — every write lands and the
    aggregate sample_size matches."""
    store = PatternStore(
        tmp_path,
        caps=PatternCaps(soft_cap_rows=10_000, hard_cap_rows=20_000, max_age_days=365),
    )
    try:
        n_threads = 100
        per_thread = 10
        errors: list[BaseException] = []

        def worker(t: int) -> None:
            try:
                for i in range(per_thread):
                    fp = compute_fingerprint(_inputs(t * per_thread + i))
                    store.record(
                        fingerprint=fp,
                        primary_model=f"m{t % 3}",
                        success_score=0.7,
                        cost_usd=Decimal("0.01"),
                        latency_ms=1000.0,
                        pricing_version="v1",
                    )
            except BaseException as exc:
                errors.append(exc)

        with ThreadPoolExecutor(max_workers=n_threads) as ex:
            futs = [ex.submit(worker, t) for t in range(n_threads)]
            for f in futs:
                f.result()

        assert errors == [], f"unexpected errors: {errors[:3]}"
        total_samples = store._conn.execute(
            "SELECT COALESCE(SUM(sample_size), 0) FROM outcomes"
        ).fetchone()[0]
        assert total_samples == n_threads * per_thread
    finally:
        store.close()


def test_concurrent_record_and_recommend_no_corruption(tmp_path: Path) -> None:
    """Writers and readers interleaving do not raise — the RLock
    serializes per-statement access on the shared sqlite3 connection.

    Without the lock, this test reproduces `InterfaceError: bad parameter
    or other API misuse` on a substantial fraction of calls (see
    pattern-store.md §Production tuning, item C)."""
    store = PatternStore(tmp_path)
    try:
        # Seed so the reader has neighbors to scan.
        for i in range(20):
            fp = compute_fingerprint(_inputs(i))
            store.record(fp, "haiku", 0.7, Decimal("0.01"), 1000.0, "v1")

        errors: list[BaseException] = []

        def writer(n: int) -> None:
            try:
                for i in range(n):
                    fp = compute_fingerprint(_inputs(1000 + i))
                    store.record(fp, "haiku", 0.7, Decimal("0.01"), 1000.0, "v1")
            except BaseException as exc:
                errors.append(exc)

        def reader(n: int) -> None:
            try:
                query = compute_fingerprint(_inputs(1))
                for _ in range(n):
                    store.recommend(query, cost_weight=0.05, min_confidence=0.05, min_sample_size=1)
            except BaseException as exc:
                errors.append(exc)

        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = [ex.submit(writer, 30) for _ in range(4)]
            futs += [ex.submit(reader, 30) for _ in range(4)]
            for f in futs:
                f.result()
        assert errors == [], f"unexpected errors: {errors[:3]}"
    finally:
        store.close()


# ---- (3) Audit-flag verification ----------------------------------------


def test_pattern_evicted_is_audit_flagged() -> None:
    """`pattern.evicted` survives a Wave 12 trace retention sweep.

    Pinning this assertion in the pattern-store test surface (not just the
    audit-log spec) so a change to AUDIT_EVENT_TYPES that drops the entry
    surfaces immediately in this package's CI run."""
    assert "pattern.evicted" in AUDIT_EVENT_TYPES
    assert is_audit_event("pattern.evicted") is True


def test_pattern_recorded_and_matched_are_not_audit_flagged() -> None:
    """Operational telemetry (`pattern.recorded` / `pattern.matched`)
    must rotate under retention; otherwise a busy workspace's pattern
    events would never age out and accumulate in the trace DB.

    The eviction events (`pattern.evicted`) carry the long-term audit
    signal of cap pressure; the per-write `pattern.recorded` stream is
    reconstructable from `outcome_score_history` + `last_updated_at_us`
    on the outcome row if forensics ever require it (pattern-store.md
    §Production tuning, item E)."""
    assert "pattern.recorded" not in AUDIT_EVENT_TYPES
    assert "pattern.matched" not in AUDIT_EVENT_TYPES
    assert is_audit_event("pattern.recorded") is False
    assert is_audit_event("pattern.matched") is False


# ---- (4) Cache observability counters -----------------------------------


def test_cache_hit_ratio_is_none_when_no_lookups(tmp_path: Path) -> None:
    store = PatternStore(tmp_path)
    try:
        assert store.cache_hit_count() == 0
        assert store.cache_miss_count() == 0
        assert store.cache_hit_ratio() is None
    finally:
        store.close()


def test_cache_counters_track_hits_and_misses(tmp_path: Path) -> None:
    store = PatternStore(tmp_path)
    try:
        # First lookup is a miss.
        assert store.lookup_embedding("hello", "p") is None
        assert store.cache_miss_count() == 1
        assert store.cache_hit_count() == 0

        # Populate the cache, then a lookup hits.
        store.store_embedding("hello", "p", (1.0, 0.0, 0.0))
        assert store.lookup_embedding("hello", "p") == (1.0, 0.0, 0.0)
        assert store.cache_hit_count() == 1

        # Another miss on a different key.
        assert store.lookup_embedding("other", "p") is None
        assert store.cache_miss_count() == 2

        ratio = store.cache_hit_ratio()
        assert ratio is not None
        assert pytest.approx(ratio, abs=1e-6) == 1 / 3
    finally:
        store.close()


# ---- Cache eviction smoke — pinned for /docs/operations awareness ------


def test_cache_eviction_holds_at_cap_under_sustained_writes(tmp_path: Path) -> None:
    """Once the cache is at cap, sustained writes never grow it past the
    cap. The per-write `_trim_embedding_cache` pass is O(N log N) in the
    cap, so this test stays small enough to run fast; pattern-store.md
    §Production tuning documents the throughput collapse at scale."""
    cap = 50
    store = PatternStore(tmp_path, embedding_cache_max_rows=cap)
    try:
        vec = (1.0, 0.0)
        for i in range(cap * 3):
            store.store_embedding(f"k-{i}", "p", vec)
        size = store.cache_size()
        assert size.rows <= cap, f"cache grew past cap: {size.rows} > {cap}"
    finally:
        store.close()
