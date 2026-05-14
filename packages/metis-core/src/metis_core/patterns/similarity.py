"""Weighted Jaccard similarity over StructuralFeatures.

See `pattern-store.md §5.3`. The weights sum to 1.0. Empty sets on both
sides are treated as a Jaccard score of 1 (the canonical 0/0 convention);
empty on exactly one side contributes 0.

`workspace_hash` is NOT included: within a workspace it would always match,
and across workspaces queries are forbidden in v1.
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


def weighted_jaccard(a: StructuralFeatures, b: StructuralFeatures) -> float:
    """Per spec §5.3. Result in `[0.0, 1.0]`."""
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
