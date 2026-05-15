"""Per-workspace, bounded pattern store (Phase 2.5).

Implements `docs/specs/pattern-store.md` v1. Records mechanical fingerprints
of turns + outcomes (cost, latency, success score) and surfaces aggregated
recommendations to the routing engine's slot 4 (`PATTERN_RECOMMENDATION`).

The store is SQLite-backed (WAL + synchronous=NORMAL), per-workspace at
`<workspace>/.metis/patterns.db`, bounded by row count and age. Outcomes are
Welford-accumulated; raw per-session rows are not retained.

v2 hybrid fingerprint (pattern-store.md §16) is opt-in via
`PatternConfig.fingerprint_version="v2"` — blends a cosine score over a
per-workspace embedding into the K-NN similarity. The bounded embedding
cache lives in the same `patterns.db`. v1 workspaces are unaffected.
"""

from metis_core.patterns.aggregation import aggregate_recommendation
from metis_core.patterns.embeddings import (
    CohereEmbeddingProvider,
    DeterministicEmbeddingProvider,
    EmbeddingProvider,
    LocalEmbeddingProvider,
    OpenAIEmbeddingProvider,
    resolve_embedding_provider,
)
from metis_core.patterns.fingerprint import (
    Fingerprint,
    FingerprintInputs,
    FingerprintKind,
    StructuralFeatures,
    attach_embedding_for_recording,
    compute_fingerprint,
    derive_fingerprint_inputs,
    structural_signature,
    text_sha256,
)
from metis_core.patterns.retention import PatternCaps
from metis_core.patterns.similarity import (
    blended_similarity,
    cosine_similarity,
    weighted_jaccard,
)
from metis_core.patterns.store import (
    EmbeddingCacheSize,
    ModelOption,
    NeighborMatch,
    PatternRecommendation,
    PatternStore,
    RecordResult,
    StoreSize,
    UpdateScoreResult,
)
from metis_core.patterns.subscriber import (
    PatternEventSubscriber,
)

__all__ = [
    "CohereEmbeddingProvider",
    "DeterministicEmbeddingProvider",
    "EmbeddingCacheSize",
    "EmbeddingProvider",
    "Fingerprint",
    "FingerprintInputs",
    "FingerprintKind",
    "LocalEmbeddingProvider",
    "ModelOption",
    "NeighborMatch",
    "OpenAIEmbeddingProvider",
    "PatternCaps",
    "PatternEventSubscriber",
    "PatternRecommendation",
    "PatternStore",
    "RecordResult",
    "StoreSize",
    "StructuralFeatures",
    "UpdateScoreResult",
    "aggregate_recommendation",
    "attach_embedding_for_recording",
    "blended_similarity",
    "compute_fingerprint",
    "cosine_similarity",
    "derive_fingerprint_inputs",
    "resolve_embedding_provider",
    "structural_signature",
    "text_sha256",
    "weighted_jaccard",
]
