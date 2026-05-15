"""Weighted Jaccard similarity tests."""

from __future__ import annotations

import pytest
from metis_core.patterns.fingerprint import StructuralFeatures
from metis_core.patterns.similarity import _WEIGHTS, weighted_jaccard


def _feat(**overrides) -> StructuralFeatures:
    base = dict(
        file_extensions=(),
        file_path_buckets=(),
        tool_names=(),
        side_effect_classes=(),
        has_images=False,
        has_tool_calls_in_history=False,
        estimated_input_tokens_bucket=0,
        intent_tags=(),
        workspace_hash="ws",
    )
    base.update(overrides)
    return StructuralFeatures(**base)


def test_weights_sum_to_one() -> None:
    assert pytest.approx(sum(_WEIGHTS.values()), abs=1e-9) == 1.0


def test_identical_features_score_one() -> None:
    feat = _feat(
        file_extensions=(".py",),
        intent_tags=("refactor",),
    )
    assert weighted_jaccard(feat, feat) == pytest.approx(1.0)


def test_disjoint_intent_tags_lose_weight() -> None:
    a = _feat(intent_tags=("refactor",))
    b = _feat(intent_tags=("debug",))
    # Everything else identical; intent_tags Jaccard = 0
    score = weighted_jaccard(a, b)
    assert score == pytest.approx(1.0 - _WEIGHTS["intent_tags"])


def test_empty_sets_match() -> None:
    a = _feat()
    b = _feat()
    # All empty-empty → Jaccard 1.0 across set-valued dims; bool/bucket match.
    assert weighted_jaccard(a, b) == pytest.approx(1.0)


def test_empty_on_one_side_contributes_zero() -> None:
    a = _feat(intent_tags=("refactor",))
    b = _feat(intent_tags=())
    score = weighted_jaccard(a, b)
    # intent_tags Jaccard = 0 (one side empty, the other not).
    assert score == pytest.approx(1.0 - _WEIGHTS["intent_tags"])


def test_close_match_outranks_distant() -> None:
    base = _feat(
        file_extensions=(".py",),
        tool_names=("read_file",),
        intent_tags=("refactor",),
    )
    close = _feat(
        file_extensions=(".py",),
        tool_names=("read_file", "edit_file"),
        intent_tags=("refactor",),
    )
    far = _feat(
        file_extensions=(".sql",),
        tool_names=("shell",),
        intent_tags=("debug",),
    )
    assert weighted_jaccard(base, close) > weighted_jaccard(base, far)


def test_workload_id_none_on_both_sides_is_back_compat() -> None:
    """v1 back-compat: when neither side sets workload_id the result is
    exactly the v1 structural weighted-Jaccard (no blend, no shift)."""
    a = _feat(intent_tags=("refactor",), workload_id=None)
    b = _feat(intent_tags=("debug",), workload_id=None)
    # Same expectation as test_disjoint_intent_tags_lose_weight.
    assert weighted_jaccard(a, b) == pytest.approx(1.0 - _WEIGHTS["intent_tags"])


def test_workload_id_matching_clusters_above_threshold() -> None:
    """Two structurally-unlike turns from the same workload should cluster
    above the K-NN gate (~0.7). intent_tags / file_extensions / tool_names
    are all disjoint here — without the workload-id blend the v1 score
    would be far below threshold."""
    a = _feat(
        intent_tags=("refactor",),
        file_extensions=(".py",),
        tool_names=("read_file",),
        workload_id="regex-with-edge-cases",
    )
    b = _feat(
        intent_tags=("debug",),
        file_extensions=(".sql",),
        tool_names=("shell",),
        workload_id="regex-with-edge-cases",
    )
    score = weighted_jaccard(a, b)
    assert score >= 0.85, score


def test_workload_id_mismatching_collapses_below_threshold() -> None:
    """Two structurally-similar turns from different workloads should fall
    below the K-NN gate. Without the workload-id blend the v1 score would
    be ~1.0 since the structural features are identical."""
    a = _feat(
        intent_tags=("refactor",),
        file_extensions=(".py",),
        tool_names=("read_file",),
        workload_id="fix-a-bug-small",
    )
    b = _feat(
        intent_tags=("refactor",),
        file_extensions=(".py",),
        tool_names=("read_file",),
        workload_id="multi-turn-refactor",
    )
    score = weighted_jaccard(a, b)
    # Cluster signal = 0 (workload mismatch), so result ≈ (1 - 0.85) = 0.15
    # (the structural score is 1.0 here since all features are identical).
    assert score == pytest.approx(0.15, abs=1e-9), score


def test_workload_id_one_sided_falls_back_to_v1() -> None:
    """If only one side sets workload_id the blend is skipped and the
    structural score governs — matches v1 semantics for agent-loop turns
    mixed against benchmark turns in the same workspace."""
    a = _feat(intent_tags=("refactor",), workload_id="fix-a-bug-small")
    b = _feat(intent_tags=("refactor",), workload_id=None)
    # Pure structural: features otherwise identical → 1.0.
    assert weighted_jaccard(a, b) == pytest.approx(1.0)


def test_workload_id_matching_outranks_mismatching() -> None:
    """Two same-workload neighbors must rank above an identical-structure
    different-workload neighbor — the cluster signal dominates structural
    overlap."""
    base = _feat(
        intent_tags=("refactor",),
        file_extensions=(".py",),
        workload_id="A",
    )
    same_workload_unlike = _feat(
        intent_tags=("debug",),
        file_extensions=(".sql",),
        workload_id="A",
    )
    other_workload_identical_shape = _feat(
        intent_tags=("refactor",),
        file_extensions=(".py",),
        workload_id="B",
    )
    assert weighted_jaccard(base, same_workload_unlike) > weighted_jaccard(
        base, other_workload_identical_shape
    )
