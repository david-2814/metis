"""Similarity over fingerprints — v1 weighted Jaccard, v2 hybrid blend.

See `pattern-store.md §5.3` (v1) and §16.5 (v2 blend). The base structural
weights sum to 1.0. Empty sets on both sides are treated as a Jaccard score
of 1 (the canonical 0/0 convention); empty on exactly one side contributes 0.

`workspace_hash` is NOT included: within a workspace it would always match,
and across workspaces queries are forbidden in v1.

`workload_id` is a near-keyed partition (see `pattern-store.md §5.1`). When
both sides carry the same workload tag the structural score is shifted to
~1.0; when they differ the score is collapsed toward 0.0. When either side
is None the workload_id contributes nothing and the score reduces to the v1
weighted-Jaccard, so non-benchmark callers (who never set workload_id) see
identical behavior.

The v2 blend (§16.5) layers cosine over the user-message embedding on top of
the v1 structural-Jaccard score:

    similarity = alpha * cosine + (1 - alpha) * weighted_jaccard

with `alpha = PatternConfig.embedding_alpha`, default 0.6. The workload-id
near-keyed partition still wraps the blended score so same-workload
neighbors land near 1.0 even when the cosine is noisy. Mixed-version stores
fall back to pure structural Jaccard when either side lacks an embedding -
the migration is forward-only and lossless (§16.5.3).
"""

from __future__ import annotations

from collections.abc import Sequence

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


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity over two vectors. Range: `[-1.0, 1.0]`.

    Vectors are expected to be L2-normalized by the embedding provider
    (§16.2), in which case cosine reduces to a dot product. The
    implementation does not assume normalization — it computes the full
    dot/norm form so the unit tests can verify the math against
    unnormalized fixtures.
    """
    if len(a) != len(b):
        raise ValueError(f"cosine_similarity: dim mismatch ({len(a)} vs {len(b)})")
    if not a:
        raise ValueError("cosine_similarity: empty vectors")
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b, strict=True):
        fx = float(x)
        fy = float(y)
        dot += fx * fy
        norm_a += fx * fx
        norm_b += fy * fy
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / ((norm_a**0.5) * (norm_b**0.5))


def blended_similarity(
    a: StructuralFeatures,
    b: StructuralFeatures,
    *,
    a_embedding: Sequence[float] | None,
    b_embedding: Sequence[float] | None,
    alpha: float,
) -> float:
    """v2 hybrid similarity per spec §16.5.

    Computes `alpha * cosine + (1 - alpha) * weighted_jaccard` when both
    sides carry an embedding of equal dimension. Falls back to pure
    `weighted_jaccard` per §16.5.3 when either side lacks an embedding or
    the dims disagree (provider-mismatch).

    `alpha` outside `[0.0, 1.0]` is rejected - `PatternConfig` validates
    this at construction, the redundant check here keeps the function
    safe when callers bypass `PatternConfig`.
    """
    if not (0.0 <= alpha <= 1.0):
        raise ValueError(f"blended_similarity: alpha must be in [0.0, 1.0] (got {alpha})")
    structural = weighted_jaccard(a, b)
    if a_embedding is None or b_embedding is None or len(a_embedding) != len(b_embedding):
        return structural
    cosine = cosine_similarity(a_embedding, b_embedding)
    blended = alpha * cosine + (1.0 - alpha) * _structural_jaccard(a, b)
    if a.workload_id is None or b.workload_id is None:
        return blended
    cluster = 1.0 if a.workload_id == b.workload_id else 0.0
    return _WORKLOAD_BLEND_WEIGHT * cluster + (1.0 - _WORKLOAD_BLEND_WEIGHT) * blended
