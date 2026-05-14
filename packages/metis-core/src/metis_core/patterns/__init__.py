"""Per-workspace, bounded pattern store (Phase 2.5).

Implements `docs/specs/pattern-store.md` v1. Records mechanical fingerprints
of turns + outcomes (cost, latency, success score) and surfaces aggregated
recommendations to the routing engine's slot 4 (`PATTERN_RECOMMENDATION`).

The store is SQLite-backed (WAL + synchronous=NORMAL), per-workspace at
`<workspace>/.metis/patterns.db`, bounded by row count and age. Outcomes are
Welford-accumulated; raw per-session rows are not retained.
"""

from metis_core.patterns.aggregation import aggregate_recommendation
from metis_core.patterns.fingerprint import (
    Fingerprint,
    FingerprintInputs,
    FingerprintKind,
    StructuralFeatures,
    compute_fingerprint,
    derive_fingerprint_inputs,
    structural_signature,
)
from metis_core.patterns.retention import PatternCaps
from metis_core.patterns.similarity import weighted_jaccard
from metis_core.patterns.store import (
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
    "Fingerprint",
    "FingerprintInputs",
    "FingerprintKind",
    "ModelOption",
    "NeighborMatch",
    "PatternCaps",
    "PatternEventSubscriber",
    "PatternRecommendation",
    "PatternStore",
    "RecordResult",
    "StoreSize",
    "StructuralFeatures",
    "UpdateScoreResult",
    "aggregate_recommendation",
    "compute_fingerprint",
    "derive_fingerprint_inputs",
    "structural_signature",
    "weighted_jaccard",
]
