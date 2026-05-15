"""v2 PatternStore tests: embedding cache, blended K-NN, schema bump.

Covers pattern-store.md §16.10 tests 6-15.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from metis_core.patterns.embeddings import DeterministicEmbeddingProvider
from metis_core.patterns.fingerprint import (
    FingerprintInputs,
    attach_embedding_for_recording,
    compute_fingerprint,
)
from metis_core.patterns.store import PatternStore


def _inputs(text: str = "refactor module", **overrides) -> FingerprintInputs:
    base = dict(
        user_message_text=text,
        workspace_path="/tmp/ws",
        estimated_input_tokens=2_000,
        has_images=False,
        has_tool_calls_in_history=False,
        file_extensions=(".py",),
        file_path_buckets=("src",),
        tool_names=("read_file",),
        side_effect_classes=("read",),
    )
    base.update(overrides)
    return FingerprintInputs(**base)


@pytest.fixture
def v1_store(tmp_path: Path) -> PatternStore:
    s = PatternStore(tmp_path, fingerprint_version="v1")
    yield s
    s.close()


@pytest.fixture
def v2_store(tmp_path: Path) -> PatternStore:
    s = PatternStore(tmp_path, fingerprint_version="v2", embedding_alpha=0.6)
    yield s
    s.close()


def test_schema_version_is_2_on_fresh_db(tmp_path: Path) -> None:
    s = PatternStore(tmp_path)
    try:
        row = s._conn.execute("SELECT value FROM store_meta WHERE key='schema_version'").fetchone()
        assert row[0] == "2"
    finally:
        s.close()


def test_embedding_cache_table_is_created(tmp_path: Path) -> None:
    s = PatternStore(tmp_path)
    try:
        row = s._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='embedding_cache'"
        ).fetchone()
        assert row is not None
    finally:
        s.close()


def test_v1_store_init_accepts_v2_unset(tmp_path: Path) -> None:
    """A v1 store opens with no embedder; cache lookups still work and
    return None (empty cache)."""
    s = PatternStore(tmp_path)
    try:
        assert s.fingerprint_version == "v1"
        assert s.lookup_embedding("anything", "openai:text-embedding-3-small") is None
    finally:
        s.close()


def test_lookup_embedding_returns_none_on_miss(v2_store: PatternStore) -> None:
    assert v2_store.lookup_embedding("never seen", "p") is None


def test_lookup_after_store_returns_vector(v2_store: PatternStore) -> None:
    vec = (0.1, 0.2, 0.3)
    v2_store.store_embedding("hello world", "p", vec)
    out = v2_store.lookup_embedding("hello world", "p")
    assert out is not None
    assert len(out) == 3
    # float32 round-trip; allow tolerance.
    assert out[0] == pytest.approx(0.1, abs=1e-6)
    assert out[1] == pytest.approx(0.2, abs=1e-6)
    assert out[2] == pytest.approx(0.3, abs=1e-6)


def test_cache_lookup_bumps_last_used_and_use_count(
    tmp_path: Path,
) -> None:
    base = datetime(2026, 5, 14, tzinfo=UTC)
    current = [base]
    s = PatternStore(tmp_path, fingerprint_version="v2", now=lambda: current[0])
    try:
        s.store_embedding("hi", "p", (1.0, 0.0))
        current[0] = base + timedelta(hours=1)
        s.lookup_embedding("hi", "p")
        current[0] = base + timedelta(hours=2)
        s.lookup_embedding("hi", "p")
        row = s._conn.execute(
            "SELECT use_count FROM embedding_cache WHERE provider_id='p'"
        ).fetchone()
        assert row[0] == 3  # one INSERT + two lookups
    finally:
        s.close()


def test_cache_provider_id_separates_keys(v2_store: PatternStore) -> None:
    v2_store.store_embedding("hello", "p1", (1.0, 0.0))
    v2_store.store_embedding("hello", "p2", (0.0, 1.0))
    a = v2_store.lookup_embedding("hello", "p1")
    b = v2_store.lookup_embedding("hello", "p2")
    assert a != b
    assert a[0] == pytest.approx(1.0, abs=1e-6)
    assert b[1] == pytest.approx(1.0, abs=1e-6)


def test_cache_ttl_evicts_old_rows(tmp_path: Path) -> None:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    current = [base]
    s = PatternStore(
        tmp_path,
        fingerprint_version="v2",
        embedding_cache_max_age_days=30,
        now=lambda: current[0],
    )
    try:
        s.store_embedding("old", "p", (1.0, 0.0))
        current[0] = base + timedelta(days=200)
        # Next write triggers trim; the old row should be gone.
        s.store_embedding("new", "p", (0.0, 1.0))
        assert s.lookup_embedding("old", "p") is None
        assert s.lookup_embedding("new", "p") is not None
    finally:
        s.close()


def test_cache_size_cap_lru_evicts_least_recently_used(tmp_path: Path) -> None:
    base = datetime(2026, 5, 14, tzinfo=UTC)
    current = [base]
    s = PatternStore(
        tmp_path,
        fingerprint_version="v2",
        embedding_cache_max_rows=2,
        now=lambda: current[0],
    )
    try:
        s.store_embedding("a", "p", (1.0, 0.0))
        current[0] = base + timedelta(hours=1)
        s.store_embedding("b", "p", (0.0, 1.0))
        # Touch 'a' so 'b' becomes the LRU row.
        current[0] = base + timedelta(hours=2)
        s.lookup_embedding("a", "p")
        current[0] = base + timedelta(hours=3)
        # Third insert overflows cap; 'b' should evict (oldest last_used_at).
        s.store_embedding("c", "p", (0.5, 0.5))
        assert s.lookup_embedding("a", "p") is not None
        assert s.lookup_embedding("b", "p") is None
        assert s.lookup_embedding("c", "p") is not None
    finally:
        s.close()


def test_cache_clear_drops_all(v2_store: PatternStore) -> None:
    v2_store.store_embedding("a", "p", (1.0, 0.0))
    v2_store.store_embedding("b", "p", (0.0, 1.0))
    removed = v2_store.cache_clear()
    assert removed == 2
    assert v2_store.cache_size().rows == 0


def test_cache_size_reports_disk_footprint(v2_store: PatternStore) -> None:
    v2_store.store_embedding("a", "p", (1.0, 0.0, 0.5, 0.25))
    size = v2_store.cache_size()
    assert size.rows == 1
    assert size.total_bytes == 4 * 4  # 4 floats x 4 bytes (float32)
    assert size.oldest_row_age_days is not None
    assert size.oldest_row_age_days >= 0.0


def test_v1_db_loads_under_v2_mode_with_no_embeddings(tmp_path: Path) -> None:
    """Pre-flag-flip outcomes recorded under v1 reopen cleanly under v2;
    K-NN falls back to structural-only on the v1 rows (§16.5.3)."""
    s = PatternStore(tmp_path, fingerprint_version="v1")
    try:
        fp = compute_fingerprint(_inputs())
        s.record(fp, "anthropic:haiku", 0.9, Decimal("0.01"), 1000.0, "v1")
    finally:
        s.close()
    s2 = PatternStore(tmp_path, fingerprint_version="v2")
    try:
        # Query with a v2 fingerprint that carries an embedding.
        inputs2 = _inputs(text="refactor module")
        inputs2_with_emb = FingerprintInputs(
            **{
                **{k: getattr(inputs2, k) for k in inputs2.__dataclass_fields__},
                "embedding": (1.0, 0.0),
                "embedding_provider": "p",
            }
        )
        fp2 = compute_fingerprint(inputs2_with_emb)
        neighbors = s2.find_k_nearest(fp2, k=5)
        # The pre-v2 row still appears; similarity is the structural-only
        # fallback per §16.5.3 (neighbor has embedding_blob=NULL).
        assert len(neighbors) == 1
        assert neighbors[0].primary_model == "anthropic:haiku"
    finally:
        s2.close()


async def test_v2_recording_path_writes_hybrid_fingerprint(
    v2_store: PatternStore,
) -> None:
    prov = DeterministicEmbeddingProvider(dim=8)
    inputs = await attach_embedding_for_recording(
        _inputs(text="refactor auth"),
        store=v2_store,
        embedder=prov,
    )
    fp = compute_fingerprint(inputs)
    assert fp.kind.value == "hybrid"
    assert fp.embedding is not None
    assert fp.embedding_provider == prov.provider_id
    assert fp.embedding_dim == 8
    # Cache hit on the second pass: no new embed call required.
    cached = v2_store.lookup_embedding("refactor auth", prov.provider_id)
    assert cached is not None
    # float32 round-trip rounds the doubles the provider produced.
    for a, b in zip(cached, fp.embedding, strict=True):
        assert a == pytest.approx(b, abs=1e-6)


async def test_v2_recording_path_is_cache_first(v2_store: PatternStore) -> None:
    """A pre-populated cache short-circuits the embedder call."""
    calls = [0]

    class _CountingProvider:
        provider_id = "count:1"
        dim = 2
        max_input_tokens = 100

        async def embed(self, text: str) -> tuple[float, ...]:
            calls[0] += 1
            return (1.0, 0.0)

        async def aclose(self) -> None:
            return

    prov = _CountingProvider()
    await attach_embedding_for_recording(_inputs(text="hi"), store=v2_store, embedder=prov)
    await attach_embedding_for_recording(_inputs(text="hi"), store=v2_store, embedder=prov)
    assert calls[0] == 1


def test_blended_knn_inverts_when_cosine_lifts_neighbor(tmp_path: Path) -> None:
    """v2 K-NN: a neighbor with a higher embedding-cosine ranks above one
    whose structural overlap is similar but whose embedding diverges.

    The two stored neighbors differ only in intent_tags (so they
    structural-dedupe under different rows) and embedding. The query's
    embedding aligns with the `near` neighbor; the cosine term in the
    blend lifts that neighbor above the `far` one.
    """
    s = PatternStore(tmp_path, fingerprint_version="v2", embedding_alpha=0.6)
    try:
        near_inputs = _inputs(text="refactor the auth module")
        near_inputs = FingerprintInputs(
            **{
                **{k: getattr(near_inputs, k) for k in near_inputs.__dataclass_fields__},
                "embedding": (1.0, 0.0),
                "embedding_provider": "p",
            }
        )
        far_inputs = _inputs(text="debug the failing test")
        far_inputs = FingerprintInputs(
            **{
                **{k: getattr(far_inputs, k) for k in far_inputs.__dataclass_fields__},
                "embedding": (0.0, 1.0),
                "embedding_provider": "p",
            }
        )
        fp_near = compute_fingerprint(near_inputs)
        fp_far = compute_fingerprint(far_inputs)
        s.record(fp_near, "m_near", 0.9, Decimal("0.01"), 1000.0, "v1")
        s.record(fp_far, "m_far", 0.9, Decimal("0.01"), 1000.0, "v1")

        query_inputs = _inputs(text="refactor the auth module")
        query_inputs = FingerprintInputs(
            **{
                **{k: getattr(query_inputs, k) for k in query_inputs.__dataclass_fields__},
                "embedding": (1.0, 0.0),
                "embedding_provider": "p",
            }
        )
        query_fp = compute_fingerprint(query_inputs)
        neighbors = s.find_k_nearest(query_fp, k=2)
        assert neighbors[0].primary_model == "m_near"
        assert neighbors[0].similarity > neighbors[1].similarity
    finally:
        s.close()


def test_v1_knn_unchanged_with_v2_mode_no_query_embedding(tmp_path: Path) -> None:
    """When the query carries no embedding, K-NN falls back to v1
    structural Jaccard even if the store is configured for v2."""
    s = PatternStore(tmp_path, fingerprint_version="v2")
    try:
        near = compute_fingerprint(_inputs(text="refactor"))
        far = compute_fingerprint(
            _inputs(
                text="debug",
                file_extensions=(".sql",),
                tool_names=("shell",),
                side_effect_classes=("execute",),
            )
        )
        s.record(near, "m_near", 0.9, Decimal("0.01"), 1000.0, "v1")
        s.record(far, "m_far", 0.9, Decimal("0.01"), 1000.0, "v1")

        query = compute_fingerprint(_inputs(text="refactor"))
        neighbors = s.find_k_nearest(query, k=2)
        assert neighbors[0].primary_model == "m_near"
        assert neighbors[0].similarity > neighbors[1].similarity
    finally:
        s.close()


def test_v2_construction_rejects_invalid_version() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp, pytest.raises(ValueError):
        PatternStore(tmp, fingerprint_version="v3")


def test_v2_construction_rejects_invalid_alpha() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp, pytest.raises(ValueError):
        PatternStore(tmp, fingerprint_version="v2", embedding_alpha=1.5)


def test_pattern_config_v2_requires_embedding_provider() -> None:
    from metis_core.routing.policy import PatternConfig

    with pytest.raises(ValueError, match="embedding_provider"):
        PatternConfig(fingerprint_version="v2", embedding_provider=None)


def test_pattern_config_v2_rejects_invalid_alpha() -> None:
    from metis_core.routing.policy import PatternConfig

    with pytest.raises(ValueError, match="embedding_alpha"):
        PatternConfig(embedding_alpha=2.0)
