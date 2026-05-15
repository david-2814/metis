"""Weighted Jaccard similarity over StructuralFeatures.

See `pattern-store.md §5.3`. The base structural weights sum to 1.0. Empty
sets on both sides are treated as a Jaccard score of 1 (the canonical 0/0
convention); empty on exactly one side contributes 0.

`workspace_hash` is NOT included: within a workspace it would always match,
and across workspaces queries are forbidden in v1.

`workload_id` is a near-keyed partition (see `pattern-store.md §5.1`). When
both sides carry the same workload tag the structural score is shifted to
~1.0; when they differ the score is collapsed toward 0.0. When either side
is None the workload_id contributes nothing and the score reduces to the v1
weighted-Jaccard, so non-benchmark callers (who never set workload_id) see
identical behavior.
"""

from __future__ import annotations

from metis_core.patterns.fingerprint import StructuralFeatures

# Weights per spec §5.3. Sum to 1.0.
_WEIGHTS = {
    "intent_tags": 0.30,
    "file_extensions": 0.20,
    "tool_names": 0.15,
    "file_path_buckets": 0.10,
    "side_effect_classes": 0.10,
    "estimated_input_tokens_bucket": 0.10,
    "has_images": 0.05,
}

# When both fingerprints carry a workload_id, the cluster signal is blended
# with the structural score so same-workload neighbors land near 1.0 and
# different-workload neighbors land near 0.0 even when their structural
# features (tool shape, length bucket, intent tags) happen to overlap. The
# blend weight is dominant (0.85): structural similarity contributes 15% so
# two same-workload turns with no structural overlap still score above the
# 0.7-ish threshold the K-NN gate uses.
_WORKLOAD_BLEND_WEIGHT = 0.85


def _jaccard(a: tuple[str, ...], b: tuple[str, ...]) -> float:
    if not a and not b:
        return 1.0
    set_a = set(a)
    set_b = set(b)
    if not set_a or not set_b:
        return 0.0
    union = set_a | set_b
    if not union:
        return 1.0
    return len(set_a & set_b) / len(union)


def _structural_jaccard(a: StructuralFeatures, b: StructuralFeatures) -> float:
    """Weighted-Jaccard over the v1 structural fields. Result in `[0.0, 1.0]`."""
    score = 0.0
    score += _WEIGHTS["intent_tags"] * _jaccard(a.intent_tags, b.intent_tags)
    score += _WEIGHTS["file_extensions"] * _jaccard(a.file_extensions, b.file_extensions)
    score += _WEIGHTS["tool_names"] * _jaccard(a.tool_names, b.tool_names)
    score += _WEIGHTS["file_path_buckets"] * _jaccard(a.file_path_buckets, b.file_path_buckets)
    score += _WEIGHTS["side_effect_classes"] * _jaccard(
        a.side_effect_classes, b.side_effect_classes
    )
    score += _WEIGHTS["estimated_input_tokens_bucket"] * float(
        a.estimated_input_tokens_bucket == b.estimated_input_tokens_bucket
    )
    score += _WEIGHTS["has_images"] * float(a.has_images == b.has_images)
    return score


def weighted_jaccard(a: StructuralFeatures, b: StructuralFeatures) -> float:
    """Similarity over `StructuralFeatures` per spec §5.3. Result in `[0.0, 1.0]`.

    Workload_id acts as a near-keyed partition: when both sides set it, the
    structural score is blended with a strong cluster signal (1.0 if equal,
    0.0 if different). When either side is None the score is exactly the v1
    weighted-Jaccard so back-compat is preserved.
    """
    structural = _structural_jaccard(a, b)
    if a.workload_id is None or b.workload_id is None:
        return structural
    cluster = 1.0 if a.workload_id == b.workload_id else 0.0
    return _WORKLOAD_BLEND_WEIGHT * cluster + (1.0 - _WORKLOAD_BLEND_WEIGHT) * structural
