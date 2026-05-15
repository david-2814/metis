"""PatternStore SQLite-backed tests.

Covers the core recording/upsert/Welford behavior, K-NN matching, recommend
filters, soft/hard caps, age-based eviction, workspace isolation, score
update via update_score, and the missing-file failure mode.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from metis_core.patterns.fingerprint import (
    FingerprintInputs,
    build_structural_features,
    compute_fingerprint,
    structural_signature,
)
from metis_core.patterns.retention import PatternCaps
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
def store(tmp_path: Path) -> PatternStore:
    s = PatternStore(tmp_path)
    yield s
    s.close()


def test_missing_db_returns_empty_recommendation(tmp_path: Path) -> None:
    # Even though we open the store (which creates the dir), no rows means
    # an empty recommendation.
    store = PatternStore(tmp_path)
    try:
        fp = compute_fingerprint(_inputs())
        rec = store.recommend(fp, cost_weight=0.3, min_confidence=0.3, min_sample_size=1)
        assert rec.chosen_model is None
        assert rec.alternatives == ()
    finally:
        store.close()


def test_first_record_creates_fingerprint_and_outcome(store: PatternStore) -> None:
    fp = compute_fingerprint(_inputs())
    result = store.record(
        fingerprint=fp,
        primary_model="anthropic:haiku",
        success_score=0.8,
        cost_usd=Decimal("0.0100"),
        latency_ms=1200.0,
        pricing_version="v1",
    )
    assert result.was_new_fingerprint is True
    assert result.sample_size_before == 0
    assert result.sample_size_after == 1
    assert result.rows_auto_evicted == 0


def test_second_record_same_fingerprint_increments_sample(store: PatternStore) -> None:
    inputs = _inputs()
    fp1 = compute_fingerprint(inputs)
    fp2 = compute_fingerprint(inputs)
    # Even though the IDs differ, the structural signature is identical so
    # the second record() upserts into the same outcome row.
    assert structural_signature(fp1.structural) == structural_signature(fp2.structural)

    store.record(fp1, "anthropic:haiku", 0.8, Decimal("0.0100"), 1000.0, "v1")
    result = store.record(fp2, "anthropic:haiku", 0.6, Decimal("0.0200"), 2000.0, "v1")
    assert result.was_new_fingerprint is False
    assert result.sample_size_before == 1
    assert result.sample_size_after == 2

    size = store.size()
    assert size.fingerprints == 1
    assert size.outcomes == 1


def test_welford_score_mean_is_exact_streaming(store: PatternStore) -> None:
    fp = compute_fingerprint(_inputs())
    scores = [0.1, 0.5, 0.9, 0.3, 0.7]
    for s in scores:
        store.record(fp, "m", s, Decimal("0.01"), 1000.0, "v1")
    # After the 5 scores, the Welford mean should be ~ sum/5.
    neighbors = store.find_k_nearest(fp, k=10)
    assert len(neighbors) == 1
    assert neighbors[0].success_score_mean == pytest.approx(sum(scores) / len(scores))


def test_second_record_different_model_creates_second_outcome(
    store: PatternStore,
) -> None:
    fp = compute_fingerprint(_inputs())
    store.record(fp, "anthropic:haiku", 0.8, Decimal("0.01"), 1000.0, "v1")
    store.record(fp, "anthropic:sonnet", 0.9, Decimal("0.03"), 2000.0, "v1")
    size = store.size()
    assert size.fingerprints == 1
    assert size.outcomes == 2


def test_knn_ranks_closest_neighbor_first(store: PatternStore) -> None:
    near = _inputs(
        text="refactor module",
        file_extensions=(".py",),
        tool_names=("read_file",),
    )
    far = _inputs(
        text="debug an error",
        file_extensions=(".sql",),
        tool_names=("shell",),
        side_effect_classes=("execute",),
    )
    fp_near = compute_fingerprint(near)
    fp_far = compute_fingerprint(far)
    store.record(fp_near, "m_near", 0.8, Decimal("0.01"), 1000.0, "v1")
    store.record(fp_far, "m_far", 0.8, Decimal("0.01"), 1000.0, "v1")

    query_fp = compute_fingerprint(near)
    neighbors = store.find_k_nearest(query_fp, k=10)
    assert neighbors[0].primary_model == "m_near"
    assert neighbors[0].similarity > neighbors[1].similarity


def test_recommend_returns_chosen_when_above_thresholds(store: PatternStore) -> None:
    fp = compute_fingerprint(_inputs())
    for _ in range(5):
        store.record(fp, "anthropic:haiku", 0.9, Decimal("0.01"), 1000.0, "v1")
    for _ in range(5):
        store.record(fp, "anthropic:sonnet", 0.3, Decimal("0.05"), 2000.0, "v1")

    rec = store.recommend(
        fp,
        cost_weight=0.0,
        min_confidence=0.1,
        min_sample_size=3,
    )
    assert rec.chosen_model == "anthropic:haiku"
    assert rec.confidence > 0.0
    assert rec.sample_size == 5
    assert any(alt.model == "anthropic:sonnet" for alt in rec.alternatives)


def test_recommend_returns_none_under_confidence(store: PatternStore) -> None:
    fp = compute_fingerprint(_inputs())
    for _ in range(5):
        store.record(fp, "m_a", 0.6, Decimal("0.01"), 1000.0, "v1")
    for _ in range(5):
        store.record(fp, "m_b", 0.6, Decimal("0.01"), 1000.0, "v1")
    rec = store.recommend(fp, cost_weight=0.0, min_confidence=0.5, min_sample_size=1)
    assert rec.chosen_model is None
    # Alternatives still populated so caller can fall to next-best.
    assert len(rec.alternatives) == 2


def test_recommend_returns_none_under_sample_size(store: PatternStore) -> None:
    fp = compute_fingerprint(_inputs())
    store.record(fp, "m_a", 0.9, Decimal("0.01"), 1000.0, "v1")
    rec = store.recommend(fp, cost_weight=0.0, min_confidence=0.0, min_sample_size=5)
    assert rec.chosen_model is None


def test_recommend_a3_rev2_unblock_min_confidence_default_05_fires_slot4(
    store: PatternStore,
) -> None:
    """§A3-rev2 finding (benchmarks/RESULTS.md): on `write-a-doc-from-notes`
    Pass C turn 2, the K-NN aggregated `sonnet=0.900` ahead of `haiku=0.842`
    — the first cluster-level inversion in any A3 series. The cluster
    confidence is `(0.900 - 0.842) / 0.900 ≈ 0.064`, which fell below the
    legacy `min_confidence=0.3` gate, so slot 4 emitted `not_applicable`
    on all 18 Pass C turns and slot 7 (`global_default`) won every time.

    The Wave 9 unblock (this test) lowers `PatternConfig.min_confidence`
    from `0.3` → `0.05` so the gate scales down with the prior
    `cost_weight 0.3 → 0.1` migration. The two knobs are coupled:
    confidence is a ratio, and under `cost_weight=0.1` the same
    tied-quality clusters produce ~0.10 confidence vs ~0.35 under the
    legacy `cost_weight=0.3` regime.

    Test setup: a single fingerprint with 5 haiku samples scoring 0.842
    and 5 sonnet samples scoring 0.900, equal costs so the
    cost-efficiency term is zero for both. With `cost_weight=0.0` (chosen
    here for transparent algebra; equal-cost neighbors at any
    `cost_weight` yield the same confidence ratio) the per-model scores
    are exactly the §A3-rev2 published values.

    Assert: at `min_confidence=0.3` slot 4 gates off (legacy behavior);
    at `min_confidence=0.05` (the new default) slot 4 picks sonnet.
    """
    fp = compute_fingerprint(_inputs())
    for _ in range(5):
        store.record(fp, "anthropic:haiku", 0.842, Decimal("0.01"), 1000.0, "v1")
    for _ in range(5):
        store.record(fp, "anthropic:sonnet", 0.900, Decimal("0.01"), 1000.0, "v1")

    # Sanity-check the cluster numerics match the §A3-rev2 published values.
    rec_legacy = store.recommend(fp, cost_weight=0.0, min_confidence=0.3, min_sample_size=5)
    sonnet_alt = next(a for a in rec_legacy.alternatives if a.model == "anthropic:sonnet")
    haiku_alt = next(a for a in rec_legacy.alternatives if a.model == "anthropic:haiku")
    assert sonnet_alt.score == pytest.approx(0.900)
    assert haiku_alt.score == pytest.approx(0.842)
    assert rec_legacy.confidence == pytest.approx((0.900 - 0.842) / 0.900)

    # Headline: legacy `min_confidence=0.3` gates slot 4 off on this cluster.
    assert rec_legacy.chosen_model is None
    # Alternatives still surface so the routing engine can render them.
    assert len(rec_legacy.alternatives) == 2

    # Headline: new default `min_confidence=0.05` lets slot 4 fire and
    # invert the ranking — sonnet wins, which is the cluster-level signal
    # the §A3-rev2 unblock chain (workload-tag + cost_weight=0.1 +
    # grounding-check) produced for the first time.
    rec_new = store.recommend(fp, cost_weight=0.0, min_confidence=0.05, min_sample_size=5)
    assert rec_new.chosen_model == "anthropic:sonnet"
    assert rec_new.confidence == pytest.approx((0.900 - 0.842) / 0.900)
    assert rec_new.sample_size == 5


def test_soft_cap_signal_without_eviction() -> None:
    # Build a store with tight caps so we cross soft cap quickly.
    caps = PatternCaps(soft_cap_rows=2, hard_cap_rows=10, max_age_days=180)
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        store = PatternStore(tmp, caps=caps)
        try:
            # Three distinct fingerprints → three outcome rows → over soft cap.
            for i in range(3):
                fp = compute_fingerprint(_inputs(text=f"refactor item {i}"))
                result = store.record(
                    fp,
                    f"m_{i}",
                    0.5,
                    Decimal("0.01"),
                    1000.0,
                    "v1",
                )
            # The third write should report over_soft_cap=True without
            # evicting anything (rows are all fresh).
            assert result.over_soft_cap is True
            assert result.rows_auto_evicted == 0
            assert store.size().outcomes == 3
        finally:
            store.close()


def test_hard_cap_auto_evicts_oldest() -> None:
    """Synthesize a store that overflows the hard cap.

    We control `now` so we can manufacture old rows that the hard-cap evictor
    selects via the LRU + sample-size tie-break.
    """
    caps = PatternCaps(soft_cap_rows=2, hard_cap_rows=3, max_age_days=180)
    import tempfile

    times = iter([datetime(2025, 1, 1, tzinfo=UTC) + timedelta(days=i) for i in range(100)])
    current = [next(times)]

    def fake_now() -> datetime:
        return current[0]

    with tempfile.TemporaryDirectory() as tmp:
        store = PatternStore(tmp, caps=caps, now=fake_now)
        try:
            for i in range(4):
                current[0] = datetime(2025, 1, 1, tzinfo=UTC) + timedelta(days=i)
                fp = compute_fingerprint(_inputs(text=f"refactor item {i}"))
                result = store.record(
                    fp,
                    f"m_{i}",
                    0.5,
                    Decimal("0.01"),
                    1000.0,
                    "v1",
                )
            # 4 rows > hard cap (3) → auto-evict on the last write.
            assert result.rows_auto_evicted > 0
            assert store.size().outcomes <= caps.hard_cap_rows
        finally:
            store.close()


def test_age_based_continuous_trim_evicts_old_rows() -> None:
    caps = PatternCaps(soft_cap_rows=2, hard_cap_rows=10, max_age_days=30)
    import tempfile

    base = datetime(2025, 1, 1, tzinfo=UTC)
    current = [base]

    def fake_now() -> datetime:
        return current[0]

    with tempfile.TemporaryDirectory() as tmp:
        store = PatternStore(tmp, caps=caps, now=fake_now)
        try:
            # Write three rows in the distant past.
            for i in range(3):
                fp = compute_fingerprint(_inputs(text=f"item {i}"))
                store.record(fp, f"m_{i}", 0.5, Decimal("0.01"), 1000.0, "v1")
            # Jump forward 180 days; soft cap is 2; one more write triggers
            # the opportunistic age trim of all 3 ancient rows.
            current[0] = base + timedelta(days=200)
            fp_new = compute_fingerprint(_inputs(text="brand new"))
            store.record(fp_new, "m_new", 0.5, Decimal("0.01"), 1000.0, "v1")
            # The three ancient rows should have been trimmed.
            assert store.size().outcomes == 1
        finally:
            store.close()


def test_eviction_is_atomic(store: PatternStore) -> None:
    """An eviction either removes all condemned rows or none of them."""
    for i in range(5):
        fp = compute_fingerprint(_inputs(text=f"item {i}"))
        store.record(fp, f"m_{i}", 0.5, Decimal("0.01"), 1000.0, "v1")
    removed = store.evict(max_rows=2)
    assert removed == 3
    assert store.size().outcomes == 2


def test_clear_empties_store(store: PatternStore) -> None:
    for i in range(3):
        fp = compute_fingerprint(_inputs(text=f"item {i}"))
        store.record(fp, f"m_{i}", 0.5, Decimal("0.01"), 1000.0, "v1")
    removed = store.clear()
    assert removed == 3
    assert store.size().outcomes == 0
    assert store.size().fingerprints == 0


def test_workspace_isolation(tmp_path: Path) -> None:
    ws_a = tmp_path / "a"
    ws_b = tmp_path / "b"
    ws_a.mkdir()
    ws_b.mkdir()
    store_a = PatternStore(ws_a)
    store_b = PatternStore(ws_b)
    try:
        fp = compute_fingerprint(_inputs())
        store_a.record(fp, "m_a", 0.8, Decimal("0.01"), 1000.0, "v1")
        # Store B should not see store A's row.
        assert store_b.size().outcomes == 0
        rec_b = store_b.recommend(fp, cost_weight=0.0, min_confidence=0.0, min_sample_size=1)
        assert rec_b.chosen_model is None
    finally:
        store_a.close()
        store_b.close()


def test_score_only_record_with_none_does_not_bump_score_count(
    store: PatternStore,
) -> None:
    fp = compute_fingerprint(_inputs())
    store.record(fp, "m", None, Decimal("0.01"), 1000.0, "v1")
    neighbors = store.find_k_nearest(fp, k=10)
    assert neighbors[0].sample_size == 1
    assert neighbors[0].success_score_count == 0
    assert neighbors[0].success_score_mean == 0.0


def test_update_score_applies_late_verdict(store: PatternStore) -> None:
    fp = compute_fingerprint(_inputs())
    result = store.record(fp, "m", None, Decimal("0.01"), 1000.0, "v1")
    update = store.update_score(
        turn_id="turn_1",
        fingerprint_id=result.fingerprint_id,
        primary_model="m",
        score=0.8,
        confidence=0.7,
        eval_id="eval_1",
    )
    assert update.applied is True
    assert update.success_score_mean_after == pytest.approx(0.8)
    assert update.success_score_count_after == 1


def test_update_score_idempotent_on_same_eval_id(store: PatternStore) -> None:
    fp = compute_fingerprint(_inputs())
    result = store.record(fp, "m", None, Decimal("0.01"), 1000.0, "v1")
    store.update_score(
        turn_id="turn_1",
        fingerprint_id=result.fingerprint_id,
        primary_model="m",
        score=0.8,
        confidence=0.7,
        eval_id="eval_1",
    )
    second = store.update_score(
        turn_id="turn_1",
        fingerprint_id=result.fingerprint_id,
        primary_model="m",
        score=0.8,
        confidence=0.7,
        eval_id="eval_1",
    )
    assert second.applied is False  # de-duplicated


def test_update_score_unknown_turn_logs_and_returns(store: PatternStore) -> None:
    result = store.update_score(
        turn_id="missing",
        fingerprint_id="missing",
        primary_model="m",
        score=0.5,
        confidence=0.5,
        eval_id="eval_x",
    )
    assert result.applied is False
    assert result.success_score_count_after == 0


def test_pricing_version_preserved_across_records(store: PatternStore) -> None:
    fp = compute_fingerprint(_inputs())
    store.record(fp, "m", None, Decimal("0.01"), 1000.0, "v1")
    store.record(fp, "m", None, Decimal("0.01"), 1000.0, "v2")
    # The 'last' wins.
    row = store._lookup_outcome(
        store._lookup_fingerprint_by_sig(
            structural_signature(build_structural_features(_inputs())), None
        ),
        "m",
    )
    assert row is not None
    assert row["pricing_version_last"] == "v2"
