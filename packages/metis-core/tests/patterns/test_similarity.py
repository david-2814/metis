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
