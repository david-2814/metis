"""v2 cluster-tightening A/B against a *real* embedder (`pattern-store.md §16.10 test 5`).

`test_v2_cluster_tightening.py` lands the §16.10 test-5 gate as a SYNTHETIC
fixture: deterministic one-hot-per-workload vectors that, by construction,
clear the >=0.10-intra / >=0.05-inter gates. That synthetic fixture controls
the cluster geometry exactly and is the load-bearing CI gate.

This file is the deferred companion — the real-embedder confidence check.
It constructs no vectors. It reads the `embedding_blob` column of an actual
§A3-series patterns DB: vectors that `openai:text-embedding-3-small`
genuinely produced over real §A3 user-message turns during the live runs
captured in `benchmarks/RESULTS.md`. The question it answers is the one the
§16.10 deferral left open: *do real embedders preserve the cluster signal
the synthetic fixture asserts?*

Why the patterns DB is the corpus, not a fresh API call
-------------------------------------------------------
The pattern store deliberately does NOT retain raw user-message text
(`pattern-store.md §7.3`) — only the structural fingerprint, the SHA-256 of
the message, and the embedding vector. So the §A3 user messages cannot be
re-embedded from the DB. The stored `embedding_blob` IS the real-embedder
output; consuming it is strictly more faithful than any re-embedding could
be, and it spends no new API budget — the §A3-rev5 / §A3-rev7 runs already
paid for it. (`a3rev2` predates v2 entirely; `a3rev4` is 70/70 STRUCTURAL —
the embedding-recording wiring landed in Wave 11, so rev5/rev7 are the
earliest DBs that carry real HYBRID fingerprints.)

`test_live_openai_embedder_separates_workload_clusters` below is the one
genuinely-fresh API call: it embeds the checked-in benchmark workload
prompts and is `skipif`-gated on `OPENAI_API_KEY` so the default
`uv run pytest` never spends budget — the `@pytest.mark.live` equivalent
the task brief asked for, expressed as a self-contained skip guard so no
shared pytest config has to change.

The headline finding (recorded in `pattern-store.md §16.10`)
------------------------------------------------------------
Real `text-embedding-3-small` vectors STRONGLY preserve the workload
cluster signal — but the literal §16.10 test-5 *intra* gate ("v2 intra
>= 0.10 HIGHER than v1") does NOT transfer to the §A3 benchmark corpus.
On benchmark traffic v1 structural-Jaccard is already saturated (~0.78-0.81
intra) because turns within one benchmark workload repeatedly touch the
same files with the same tools — there is no headroom for v2 to *raise*
intra. The synthetic fixture deliberately models the opposite regime
(sparse-structural, agent-loop): it asserts `v1 intra < 0.7`. On the
dense-structural benchmark corpus the embedding's whole contribution lands
on the *inter* leg (v2 inter 0.17-0.28 lower) and in cluster *separation*
(intra - inter). Measured by separation v2 tightens clusters by +0.11 to
+0.19 and the raw cosine separates same-workload from different-workload
pairs by +0.25 to +0.34 — the v2 design is confirmed end-to-end.

This file therefore asserts the regime-robust gates (raw-cosine separation,
the inter leg, separation improvement) and records the intra-leg
saturation as an explicit finding rather than a failed assertion.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import msgspec
import pytest
from metis.core.patterns.fingerprint import StructuralFeatures
from metis.core.patterns.similarity import (
    blended_similarity,
    cosine_similarity,
    weighted_jaccard,
)
from metis.core.patterns.store import _decode_embedding_blob

# The blend weight the routing engine uses by default (`PatternConfig.
# embedding_alpha`, pattern-store.md §16.13 note 1). Matches the synthetic
# fixture in `test_v2_cluster_tightening.py`.
_ALPHA = 0.6

# Real-embedding patterns DBs. rev5/rev7 are the only §A3 DBs that carry
# HYBRID fingerprints (rev4 and earlier are STRUCTURAL-only). Both files
# live under the gitignored `benchmarks/.runs/` — present on a dev machine,
# absent in CI, so the DB-backed tests skip there and the synthetic fixture
# remains the CI gate.
_REAL_PATTERNS_DBS = ("a3rev5-patterns.db", "a3rev7-patterns.db")

_EXPECTED_PROVIDER = "openai:text-embedding-3-small"
_EXPECTED_DIM = 1536

# Regime-robust gates (see module docstring). Comfortably below the
# measured values on both rev5 and rev7 so the test is not flaky — the
# corpus is a fixed file, the computation fully deterministic.
_MIN_RAW_COSINE_SEPARATION = 0.15  # measured: rev7 +0.245, rev5 +0.340
_MIN_INTER_LEG_DELTA = 0.05  # the literal §16.10 test-5 inter gate; rev7 +0.171, rev5 +0.283
_MIN_SEPARATION_IMPROVEMENT = 0.08  # measured: rev7 +0.106, rev5 +0.191
_V1_SATURATION_FLOOR = 0.70  # measured: rev7 0.813, rev5 0.784 (this is why intra cannot lift)
_MIN_LIVE_SEPARATION = 0.10  # fresh-API workload-prompt embed; measured +0.190


def _repo_root() -> Path:
    """Walk up from this file until the dir containing `benchmarks/.runs`."""
    for parent in Path(__file__).resolve().parents:
        if (parent / "benchmarks" / ".runs").is_dir():
            return parent
    raise RuntimeError("could not locate repo root (no benchmarks/.runs ancestor)")


def _db_path(db_name: str) -> Path:
    return _repo_root() / "benchmarks" / ".runs" / db_name


@dataclass(frozen=True)
class _Fingerprint:
    """One real fingerprint row: workload label + features + embedding."""

    workload_id: str
    features: StructuralFeatures  # workload_id-stripped (see _load_fingerprints)
    embedding: tuple[float, ...]


def _load_fingerprints(db_path: Path) -> list[_Fingerprint]:
    """Load HYBRID fingerprints from a real §A3 patterns DB.

    `workload_id` is stripped from the `StructuralFeatures` fed to the
    similarity functions and kept only as the ground-truth cluster label.
    This mirrors the synthetic fixture's deliberate `workload_id=None`
    choice: with `workload_id` set, `similarity.py`'s §5.3 near-keyed
    partition drives same-workload pairs to ~1.0 and different-workload
    pairs to ~0.0 under BOTH v1 and v2, masking the embedding's
    contribution entirely. Stripping it measures what the embedding adds
    when the workload tag is unavailable — the agent-loop regime v2 is
    built for (pattern-store.md §16.1 / §16.5.2).
    """
    con = sqlite3.connect(db_path)
    try:
        rows = con.execute(
            "SELECT structural_json, embedding_blob, embedding_dim FROM fingerprints"
        ).fetchall()
    finally:
        con.close()

    out: list[_Fingerprint] = []
    for structural_json, blob, dim in rows:
        if blob is None or dim is None:
            continue  # STRUCTURAL-only row — no embedding to compare
        raw = msgspec.json.decode(structural_json, type=StructuralFeatures)
        if raw.workload_id is None:
            continue  # cannot label a cluster without a workload tag
        out.append(
            _Fingerprint(
                workload_id=raw.workload_id,
                features=msgspec.structs.replace(raw, workload_id=None),
                embedding=_decode_embedding_blob(blob, int(dim)),
            )
        )
    return out


@dataclass(frozen=True)
class _ClusterStats:
    """Pairwise intra/inter cluster means under v1 and v2."""

    db_name: str
    n_fingerprints: int
    n_workloads: int
    n_intra_pairs: int
    n_inter_pairs: int
    intra_v1: float
    intra_v2: float
    inter_v1: float
    inter_v2: float
    intra_cosine: float
    inter_cosine: float

    @property
    def inter_leg_delta(self) -> float:
        """How much v2 lowers the inter-cluster mean (the §16.10 test-5 inter leg)."""
        return self.inter_v1 - self.inter_v2

    @property
    def intra_leg_delta(self) -> float:
        """How much v2 lifts the intra-cluster mean (the §16.10 test-5 intra leg).

        Negative on the benchmark corpus — see the module docstring.
        """
        return self.intra_v2 - self.intra_v1

    @property
    def v1_separation(self) -> float:
        return self.intra_v1 - self.inter_v1

    @property
    def v2_separation(self) -> float:
        return self.intra_v2 - self.inter_v2

    @property
    def separation_improvement(self) -> float:
        """Cluster separation (intra - inter) gained going v1 -> v2."""
        return self.v2_separation - self.v1_separation

    @property
    def raw_cosine_separation(self) -> float:
        """Mean same-workload cosine minus mean different-workload cosine."""
        return self.intra_cosine - self.inter_cosine

    def summary(self) -> str:
        return (
            f"{self.db_name}: {self.n_fingerprints} fingerprints / "
            f"{self.n_workloads} workloads | "
            f"intra v1={self.intra_v1:.4f} v2={self.intra_v2:.4f} | "
            f"inter v1={self.inter_v1:.4f} v2={self.inter_v2:.4f} | "
            f"raw cosine intra={self.intra_cosine:.4f} inter={self.inter_cosine:.4f} | "
            f"separation v1={self.v1_separation:+.4f} v2={self.v2_separation:+.4f}"
        )


@cache
def _corpus_stats(db_name: str) -> _ClusterStats:
    """Compute the v1/v2 pairwise cluster matrix for a real patterns DB.

    Cached so the parametrized tests below share one O(n^2) pass per DB.
    """
    fps = _load_fingerprints(_db_path(db_name))
    n = len(fps)
    intra_v1: list[float] = []
    intra_v2: list[float] = []
    inter_v1: list[float] = []
    inter_v2: list[float] = []
    intra_cos: list[float] = []
    inter_cos: list[float] = []
    for i in range(n):
        a = fps[i]
        for j in range(i + 1, n):
            b = fps[j]
            v1 = weighted_jaccard(a.features, b.features)
            v2 = blended_similarity(
                a.features,
                b.features,
                a_embedding=a.embedding,
                b_embedding=b.embedding,
                alpha=_ALPHA,
            )
            cos = cosine_similarity(a.embedding, b.embedding)
            if a.workload_id == b.workload_id:
                intra_v1.append(v1)
                intra_v2.append(v2)
                intra_cos.append(cos)
            else:
                inter_v1.append(v1)
                inter_v2.append(v2)
                inter_cos.append(cos)

    def mean(xs: list[float]) -> float:
        return sum(xs) / len(xs)

    return _ClusterStats(
        db_name=db_name,
        n_fingerprints=n,
        n_workloads=len({fp.workload_id for fp in fps}),
        n_intra_pairs=len(intra_v1),
        n_inter_pairs=len(inter_v1),
        intra_v1=mean(intra_v1),
        intra_v2=mean(intra_v2),
        inter_v1=mean(inter_v1),
        inter_v2=mean(inter_v2),
        intra_cosine=mean(intra_cos),
        inter_cosine=mean(inter_cos),
    )


def _require_db(db_name: str) -> None:
    path = _db_path(db_name)
    if not path.exists():
        pytest.skip(
            f"real patterns DB {db_name} absent (benchmarks/.runs/ is gitignored — "
            "expected in CI; the synthetic test_v2_cluster_tightening.py is the CI gate)"
        )


@pytest.mark.parametrize("db_name", _REAL_PATTERNS_DBS)
def test_real_patterns_db_is_a_genuine_openai_embedding_corpus(db_name: str) -> None:
    """Sanity-check the corpus: every row is a HYBRID fingerprint carrying a
    real `openai:text-embedding-3-small` 1536-dim vector, spanning >= 3
    workloads with >= 2 fingerprints each (so intra-cluster pairs exist).

    If this fails the cluster-tightening numbers below are measuring the
    wrong thing — guard it first.
    """
    _require_db(db_name)
    con = sqlite3.connect(_db_path(db_name))
    try:
        kinds = dict(
            con.execute("SELECT kind, COUNT(*) FROM fingerprints GROUP BY kind").fetchall()
        )
        providers = dict(
            con.execute(
                "SELECT embedding_provider, COUNT(*) FROM fingerprints GROUP BY embedding_provider"
            ).fetchall()
        )
        dims = [r[0] for r in con.execute("SELECT DISTINCT embedding_dim FROM fingerprints")]
    finally:
        con.close()

    assert set(kinds) == {"hybrid"}, f"{db_name} carries non-HYBRID rows: {kinds}"
    assert set(providers) == {_EXPECTED_PROVIDER}, (
        f"{db_name} embedded under an unexpected provider: {providers}"
    )
    assert dims == [_EXPECTED_DIM], f"{db_name} embedding dim is not 1536: {dims}"

    fps = _load_fingerprints(_db_path(db_name))
    assert len(fps) >= 40, f"{db_name} too small for a stable A/B: {len(fps)} fingerprints"
    per_workload: dict[str, int] = {}
    for fp in fps:
        per_workload[fp.workload_id] = per_workload.get(fp.workload_id, 0) + 1
    multi = {wl: c for wl, c in per_workload.items() if c >= 2}
    assert len(multi) >= 3, (
        f"{db_name} needs >= 3 workloads with >= 2 fingerprints for intra pairs: {per_workload}"
    )
    # Every vector L2-normalized by the provider (§16.2) -> cosine == dot.
    for fp in fps:
        norm_sq = sum(x * x for x in fp.embedding)
        assert abs(norm_sq - 1.0) < 1e-3, f"{db_name} vector not L2-normalized: |v|^2={norm_sq}"


@pytest.mark.parametrize("db_name", _REAL_PATTERNS_DBS)
def test_real_embedder_preserves_workload_cluster_signal(db_name: str) -> None:
    """The core deferral question: does a real embedder cluster by workload?

    Raw cosine over the stored `text-embedding-3-small` vectors must
    separate same-workload pairs from different-workload pairs. This is
    embedding-only — no structural blend — so it isolates the embedder's
    own contribution.

    Measured: rev7 intra 0.705 / inter 0.460 (sep +0.245); rev5 intra
    0.631 / inter 0.291 (sep +0.340).
    """
    _require_db(db_name)
    stats = _corpus_stats(db_name)
    assert stats.raw_cosine_separation >= _MIN_RAW_COSINE_SEPARATION, (
        f"real embedder does NOT preserve the cluster signal — {stats.summary()}; "
        f"raw cosine separation {stats.raw_cosine_separation:+.4f} "
        f"(need >= {_MIN_RAW_COSINE_SEPARATION}). The v2 hybrid fingerprint would "
        f"not pay for itself on this corpus."
    )


@pytest.mark.parametrize("db_name", _REAL_PATTERNS_DBS)
def test_real_embedder_passes_inter_leg_and_separation_gates(db_name: str) -> None:
    """The regime-robust restatement of §16.10 test 5 on real data.

    Two gates that DO transfer from the synthetic fixture to the benchmark
    corpus:

      * inter leg (the literal §16.10 test-5 inter gate): v2's blended
        inter-cluster mean is >= 0.05 lower than v1's. Measured: rev7
        +0.171, rev5 +0.283.
      * cluster separation: v2's (intra - inter) separation exceeds v1's
        by >= 0.08. Measured: rev7 +0.106, rev5 +0.191.

    The third leg — the literal "v2 intra >= 0.10 higher" gate — does NOT
    transfer; see test_v1_structural_jaccard_is_saturated_on_benchmark_corpus.
    """
    _require_db(db_name)
    stats = _corpus_stats(db_name)
    assert stats.inter_leg_delta >= _MIN_INTER_LEG_DELTA, (
        f"v2 inter-cluster separation insufficient — {stats.summary()}; "
        f"inter leg delta {stats.inter_leg_delta:+.4f} (need >= {_MIN_INTER_LEG_DELTA})"
    )
    assert stats.separation_improvement >= _MIN_SEPARATION_IMPROVEMENT, (
        f"v2 does not tighten clusters on real data — {stats.summary()}; "
        f"separation improvement {stats.separation_improvement:+.4f} "
        f"(need >= {_MIN_SEPARATION_IMPROVEMENT})"
    )


@pytest.mark.parametrize("db_name", _REAL_PATTERNS_DBS)
def test_v1_structural_jaccard_is_saturated_on_benchmark_corpus(db_name: str) -> None:
    """Document why the literal §16.10 test-5 *intra* gate does not transfer.

    On the §A3 benchmark corpus v1 weighted-Jaccard is already saturated
    intra-cluster (~0.78-0.81) even with `workload_id` stripped — turns
    within one benchmark workload touch the same files with the same tools,
    so structure alone clusters them. There is no headroom for v2 to lift
    intra by a further 0.10; the embedding instead pulls the *blended*
    intra mean toward the (lower) cosine value. The synthetic fixture in
    test_v2_cluster_tightening.py deliberately models the opposite regime
    (it asserts `v1 intra < 0.7`), which is why it clears the literal
    intra gate and this corpus does not.

    This test asserts the saturation (the cause) and records the negative
    intra-leg delta (the effect) so the finding is pinned, not silent.
    """
    _require_db(db_name)
    stats = _corpus_stats(db_name)
    assert stats.intra_v1 >= _V1_SATURATION_FLOOR, (
        f"unexpected: v1 intra is NOT saturated on {db_name} "
        f"(intra_v1={stats.intra_v1:.4f}); the literal §16.10 intra gate may "
        f"actually be applicable here — re-examine the finding."
    )
    # The literal §16.10 test-5 intra gate would require intra_leg_delta >= 0.10.
    # On a saturated v1 baseline the blend lowers the intra mean instead — the
    # delta is negative. This is the finding, not a regression.
    assert stats.intra_leg_delta < 0.10, (
        f"the literal §16.10 intra gate unexpectedly cleared on {db_name} "
        f"(intra_leg_delta={stats.intra_leg_delta:+.4f}) — update pattern-store.md "
        f"§16.10, the regime finding no longer holds."
    )


@pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY"),
    reason=(
        "live OpenAI embedding call — opt-in only. Run with OPENAI_API_KEY exported "
        "(pytest does not load .env, so the default `uv run pytest` skips this and "
        "spends no API budget — the @pytest.mark.live equivalent the brief asked for)."
    ),
)
def test_live_openai_embedder_separates_workload_clusters() -> None:
    """One genuinely-fresh API call: embed the checked-in benchmark workload
    prompts with `openai:text-embedding-3-small` *today* and confirm the
    real embedder still clusters them by workload.

    The stored-vector tests above prove the May-2026 §A3 run vectors
    cluster; this proves the production `OpenAIEmbeddingProvider` class —
    construction, truncation, the live wire call, L2-normalization — still
    produces clustering embeddings. Cost: ~25 short prompts, well under
    $0.01 at $0.02 / 1M tokens.

    Measured 2026-05-20: intra cosine 0.531 / inter 0.340, separation
    +0.190 across 11 workloads / 25 turn prompts.
    """
    import asyncio
    import itertools

    import yaml
    from metis.core.patterns.embeddings import OpenAIEmbeddingProvider

    workloads_dir = _repo_root() / "benchmarks" / "workloads"
    if not workloads_dir.is_dir():
        pytest.skip("benchmarks/workloads/ absent")

    labelled: list[tuple[str, str]] = []  # (workload_id, prompt text)
    for workload_yaml in sorted(workloads_dir.glob("*/workload.yaml")):
        workload_id = workload_yaml.parent.name
        spec = yaml.safe_load(workload_yaml.read_text())
        for turn in spec.get("turns", []):
            prompt = turn.get("prompt")
            if prompt:
                labelled.append((workload_id, " ".join(prompt.split())))

    assert len(labelled) >= 15, f"too few workload prompts for an A/B: {len(labelled)}"

    provider = OpenAIEmbeddingProvider()

    async def _embed_all() -> list[tuple[float, ...]]:
        try:
            return [await provider.embed(text) for _, text in labelled]
        finally:
            await provider.aclose()

    vectors = asyncio.run(_embed_all())

    intra: list[float] = []
    inter: list[float] = []
    for i, j in itertools.combinations(range(len(labelled)), 2):
        cos = cosine_similarity(vectors[i], vectors[j])
        if labelled[i][0] == labelled[j][0]:
            intra.append(cos)
        else:
            inter.append(cos)

    assert intra, "no intra-workload pairs — every workload had a single turn"
    intra_mean = sum(intra) / len(intra)
    inter_mean = sum(inter) / len(inter)
    separation = intra_mean - inter_mean
    assert separation >= _MIN_LIVE_SEPARATION, (
        f"fresh OpenAI embeddings do not cluster benchmark workloads: "
        f"intra={intra_mean:.4f} inter={inter_mean:.4f} separation={separation:+.4f} "
        f"(need >= {_MIN_LIVE_SEPARATION})"
    )
