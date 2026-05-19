"""K-NN selectivity once `user_message_text` is plumbed through to the
fingerprint inputs.

Before this change, the routing-side fingerprint builder left
`user_message_text=""` so every turn collapsed to the same structural
signature: all `intent_tags` empty, every Jaccard pairwise similarity
~1.0. After the change, two turns with substantively different user
messages (e.g. "refactor the auth module" vs "debug the SQL query") get
distinct intent_tags and therefore distinct K-NN cluster matches.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from metis.core.patterns.fingerprint import (
    FingerprintInputs,
    build_structural_features,
    compute_fingerprint,
    structural_signature,
)
from metis.core.patterns.store import PatternStore


def _inputs(workspace_path: str, *, user_message_text: str) -> FingerprintInputs:
    """Two turns differ ONLY in user_message_text so any selectivity gain
    is attributable to the new plumbing, not to other structural fields."""
    return FingerprintInputs(
        user_message_text=user_message_text,
        workspace_path=workspace_path,
        estimated_input_tokens=1000,
        has_images=False,
        has_tool_calls_in_history=False,
    )


def test_signature_diverges_when_user_message_intent_differs(tmp_path: Path) -> None:
    """Sanity floor: the structural signature must actually change when
    user_message_text moves between two intent buckets. If it doesn't, the
    K-NN store can't distinguish the turns no matter how we score them.
    """
    refactor = build_structural_features(
        _inputs(str(tmp_path), user_message_text="refactor this function")
    )
    debug = build_structural_features(_inputs(str(tmp_path), user_message_text="debug the issue"))
    assert refactor.intent_tags == ("refactor",)
    assert debug.intent_tags == ("debug",)
    assert structural_signature(refactor) != structural_signature(debug)


def test_knn_returns_matching_cluster_for_distinct_user_messages(tmp_path: Path) -> None:
    """Record two outcomes — one for a refactor turn, one for a debug turn —
    then query the store with a third refactor-flavored fingerprint. The
    refactor cluster's similarity (Jaccard-weighted) should exceed the
    debug cluster's because the probe shares its `intent_tags=("refactor",)`
    with the recorded refactor row but not with the debug row.

    Previously, with `user_message_text=""` everywhere, intent_tags were
    always empty and both clusters scored Jaccard-identically, so this
    test would fail (both similarities ~1.0).
    """
    store = PatternStore(tmp_path)
    try:
        refactor_fp = compute_fingerprint(
            _inputs(str(tmp_path), user_message_text="refactor this function")
        )
        debug_fp = compute_fingerprint(_inputs(str(tmp_path), user_message_text="debug the issue"))
        store.record(
            fingerprint=refactor_fp,
            primary_model="model_refactor",
            success_score=0.9,
            cost_usd=Decimal("0.01"),
            latency_ms=1000.0,
            pricing_version="v0",
        )
        store.record(
            fingerprint=debug_fp,
            primary_model="model_debug",
            success_score=0.9,
            cost_usd=Decimal("0.01"),
            latency_ms=1000.0,
            pricing_version="v0",
        )

        # Probe with a third refactor-flavored fingerprint. K=10 picks up
        # both clusters; the refactor neighbor must dominate on similarity
        # since its intent_tags overlap with the probe's.
        probe = compute_fingerprint(
            _inputs(str(tmp_path), user_message_text="refactor this helper")
        )
        neighbors = store.find_k_nearest(probe, k=10)
        assert len(neighbors) == 2
        by_model = {n.primary_model: n.similarity for n in neighbors}
        assert by_model["model_refactor"] > by_model["model_debug"], (
            f"refactor cluster should beat debug cluster on similarity, got {by_model!r}"
        )
    finally:
        store.close()


def test_knn_collapses_to_single_cluster_when_user_message_text_empty(
    tmp_path: Path,
) -> None:
    """Regression guard: this is the WRONG behavior the workaround in
    runtime.py was perpetuating — `user_message_text=""` collapsed every
    turn to a single empty-intent cluster. We assert it here so any future
    drift back to that shape is visible as a test diff.
    """
    a = build_structural_features(_inputs(str(tmp_path), user_message_text=""))
    b = build_structural_features(_inputs(str(tmp_path), user_message_text=""))
    assert a.intent_tags == ()
    assert structural_signature(a) == structural_signature(b)
