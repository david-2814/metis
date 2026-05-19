"""SQLite-backed PatternStore.

Per `pattern-store.md §7.1`. WAL + synchronous=NORMAL, integer micros for
costs, JSON column for structural features with a SHA-256 dedup key. The
store is per-workspace at `<workspace>/.metis/patterns.db` and lazily
created on first write.

Welford streaming update for `success_score_mean` per §7.1 keeps means
exact under repeated updates without retaining raw per-session rows.
"""

from __future__ import annotations

import array
import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import msgspec

from metis.core.patterns.aggregation import (
    AggregationResult,
    ScoredModel,
    _AggregateInputs,
    aggregate_recommendation,
    now_ms,
)
from metis.core.patterns.fingerprint import (
    Fingerprint,
    FingerprintKind,
    StructuralFeatures,
    structural_signature,
    text_sha256,
)
from metis.core.patterns.retention import PatternCaps
from metis.core.patterns.similarity import blended_similarity, weighted_jaccard

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = "2"
_EMBEDDING_CACHE_MAX_ROWS = 10_000
_EMBEDDING_CACHE_MAX_AGE_DAYS = 180

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fingerprints (
  id                 TEXT PRIMARY KEY,
  kind               TEXT NOT NULL,
  structural_json    TEXT NOT NULL,
  structural_sig     TEXT NOT NULL,
  embedding_blob     BLOB,
  embedding_provider TEXT,
  embedding_dim      INTEGER,
  created_at_us      INTEGER NOT NULL,
  UNIQUE (structural_sig, embedding_provider)
);

CREATE INDEX IF NOT EXISTS idx_fp_created    ON fingerprints(created_at_us);
CREATE INDEX IF NOT EXISTS idx_fp_struct_sig ON fingerprints(structural_sig);

CREATE TABLE IF NOT EXISTS outcomes (
  fingerprint_id        TEXT NOT NULL,
  primary_model         TEXT NOT NULL,
  sample_size           INTEGER NOT NULL,
  success_score_mean    REAL NOT NULL,
  success_score_count   INTEGER NOT NULL,
  sum_cost_usd_micros   INTEGER NOT NULL,
  sum_latency_ms        REAL NOT NULL,
  pricing_version_last  TEXT NOT NULL,
  last_updated_at_us    INTEGER NOT NULL,
  PRIMARY KEY (fingerprint_id, primary_model),
  FOREIGN KEY (fingerprint_id) REFERENCES fingerprints(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_outcomes_updated ON outcomes(last_updated_at_us);

CREATE TABLE IF NOT EXISTS outcome_score_history (
  turn_id             TEXT PRIMARY KEY,
  fingerprint_id      TEXT NOT NULL,
  primary_model       TEXT NOT NULL,
  eval_id_applied     TEXT NOT NULL,
  score               REAL NOT NULL,
  confidence          REAL NOT NULL,
  applied_at_us       INTEGER NOT NULL,
  FOREIGN KEY (fingerprint_id) REFERENCES fingerprints(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_score_history_outcome
  ON outcome_score_history(fingerprint_id, primary_model);

CREATE TABLE IF NOT EXISTS store_meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS embedding_cache (
  text_sha256       TEXT NOT NULL,
  provider_id       TEXT NOT NULL,
  embedding_blob    BLOB NOT NULL,
  embedding_dim     INTEGER NOT NULL,
  created_at_us     INTEGER NOT NULL,
  last_used_at_us   INTEGER NOT NULL,
  use_count         INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (text_sha256, provider_id)
);

CREATE INDEX IF NOT EXISTS idx_embcache_last_used ON embedding_cache(last_used_at_us);
CREATE INDEX IF NOT EXISTS idx_embcache_created   ON embedding_cache(created_at_us);
"""


def _to_micros(value: datetime) -> int:
    epoch = datetime(1970, 1, 1, tzinfo=value.tzinfo or UTC)
    delta = value - epoch
    return delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds


def _decimal_to_micros(value: Decimal) -> int:
    return int((value * Decimal(1_000_000)).to_integral_value())


def _micros_to_decimal(micros: int) -> Decimal:
    return Decimal(micros) / Decimal(1_000_000)


def _encode_embedding_blob(vector: tuple[float, ...]) -> bytes:
    return array.array("f", vector).tobytes()


def _decode_embedding_blob(blob: bytes, dim: int) -> tuple[float, ...]:
    arr = array.array("f")
    arr.frombytes(blob)
    if len(arr) != dim:
        raise ValueError(f"embedding blob length {len(arr)} does not match recorded dim {dim}")
    return tuple(arr)


@dataclass(frozen=True)
class ModelOption:
    """One entry in `PatternRecommendation.alternatives` per `pattern-store §4`.

    Maps 1:1 to `route.decided.chain[].pattern_alternatives` and is used for
    display/debugging when the routing engine surfaces the chain.
    """

    model: str
    score: float
    sample_size: int
    avg_cost_usd: Decimal
    success_score_mean: float


@dataclass(frozen=True)
class PatternRecommendation:
    """Return value of `PatternStore.recommend()` per §4."""

    chosen_model: str | None
    confidence: float
    alternatives: tuple[ModelOption, ...]
    sample_size: int
    elapsed_ms: float
    k_cluster_size: int = 0
    fingerprint_id: str | None = None
    fingerprint_kind: FingerprintKind = FingerprintKind.STRUCTURAL


@dataclass(frozen=True)
class NeighborMatch:
    """Lower-level: a single neighbor outcome with similarity score."""

    fingerprint_id: str
    primary_model: str
    similarity: float
    sample_size: int
    success_score_mean: float
    success_score_count: int
    avg_cost_usd: Decimal
    avg_latency_ms: float


@dataclass(frozen=True)
class RecordResult:
    """Returned by `record()`; carries hashes for `pattern.recorded` events."""

    fingerprint_id: str
    primary_model: str
    sample_size_before: int
    sample_size_after: int
    was_new_fingerprint: bool
    over_soft_cap: bool
    rows_auto_evicted: int


@dataclass(frozen=True)
class UpdateScoreResult:
    """Returned by `update_score()` after a late-arriving evaluator verdict."""

    fingerprint_id: str
    primary_model: str
    success_score_mean_before: float
    success_score_mean_after: float
    success_score_count_before: int
    success_score_count_after: int
    rolled_back_prior: bool
    applied: bool  # False if eval_id already applied, or unknown turn


@dataclass(frozen=True)
class StoreSize:
    """Snapshot of the store's row counts and oldest row age."""

    fingerprints: int
    outcomes: int
    oldest_outcome_age_days: float | None


@dataclass(frozen=True)
class EmbeddingCacheSize:
    """v2 embedding cache stats per `pattern-store.md §16.6.3`."""

    rows: int
    oldest_row_age_days: float | None
    total_bytes: int


@dataclass(frozen=True)
class _EvictionStats:
    fingerprints_before: int
    fingerprints_after: int
    outcomes_before: int
    outcomes_after: int
    entries_evicted: int
    oldest_evicted_age_days: float | None


class PatternStore:
    """Per-workspace SQLite-backed store of fingerprints + outcomes."""

    def __init__(
        self,
        workspace_path: str | Path,
        *,
        caps: PatternCaps | None = None,
        now: callable | None = None,
        fingerprint_version: str = "v1",
        embedding_alpha: float = 0.6,
        embedding_cache_max_rows: int = _EMBEDDING_CACHE_MAX_ROWS,
        embedding_cache_max_age_days: int = _EMBEDDING_CACHE_MAX_AGE_DAYS,
    ) -> None:
        self._workspace = Path(workspace_path).expanduser().resolve()
        self._db_path = self._workspace / ".metis" / "patterns.db"
        self._caps = caps or PatternCaps()
        self._now = now or (lambda: datetime.now(UTC))
        if fingerprint_version not in ("v1", "v2"):
            raise ValueError(f"unknown fingerprint_version: {fingerprint_version!r}")
        if not (0.0 <= embedding_alpha <= 1.0):
            raise ValueError(f"embedding_alpha must be in [0.0, 1.0] (got {embedding_alpha})")
        self._fingerprint_version = fingerprint_version
        self._embedding_alpha = float(embedding_alpha)
        self._embedding_cache_max_rows = int(embedding_cache_max_rows)
        self._embedding_cache_max_age_days = int(embedding_cache_max_age_days)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self._db_path), isolation_level=None, check_same_thread=False
        )
        # Single sqlite3.Connection across threads is not safe for cursor-result
        # interleaving (the `check_same_thread=False` flag only disables the
        # Python-side guard). The store's documented invariant is one writer
        # per workspace (pattern-store.md §11.9), but concurrent reads from
        # routing slot 4 and writes from the subscriber can interleave on the
        # same asyncio task switch — and a future caller may share the store
        # across threads. This RLock serializes every public method as
        # defense-in-depth; under the single-task asyncio architecture it is
        # uncontended.
        self._lock = threading.RLock()
        # v2 embedding-cache hit/miss counters (pattern-store.md §16.7.2).
        # Bus subscribers (observability/metrics.py) read these to surface
        # `metis_pattern_embedding_cache_hits_total` /
        # `metis_pattern_embedding_cache_misses_total`. The pure counters are
        # process-local; durable counts are recoverable from trace events.
        self._cache_hits = 0
        self._cache_misses = 0
        self._configure()
        self._conn.executescript(_SCHEMA)
        self._conn.execute(
            "INSERT INTO store_meta(key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value "
            "WHERE store_meta.value < excluded.value",
            (_SCHEMA_VERSION,),
        )

    @property
    def workspace_path(self) -> str:
        return str(self._workspace)

    @property
    def db_path(self) -> Path:
        return self._db_path

    @property
    def caps(self) -> PatternCaps:
        return self._caps

    @property
    def fingerprint_version(self) -> str:
        return self._fingerprint_version

    @property
    def embedding_alpha(self) -> float:
        return self._embedding_alpha

    def _configure(self) -> None:
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._conn.execute("PRAGMA foreign_keys = ON")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- Embedding-cache counters (v2; observability/metrics.py reads these) ----

    def cache_hit_count(self) -> int:
        """Process-local count of `lookup_embedding` calls that returned a row."""
        with self._lock:
            return self._cache_hits

    def cache_miss_count(self) -> int:
        """Process-local count of `lookup_embedding` calls that returned None."""
        with self._lock:
            return self._cache_misses

    def cache_hit_ratio(self) -> float | None:
        """Process-local `hits / (hits + misses)`. None if no lookups yet."""
        with self._lock:
            total = self._cache_hits + self._cache_misses
            if total == 0:
                return None
            return self._cache_hits / total

    def __enter__(self) -> PatternStore:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # ---- Recording -----------------------------------------------------

    def record(
        self,
        fingerprint: Fingerprint,
        primary_model: str,
        success_score: float | None,
        cost_usd: Decimal,
        latency_ms: float,
        pricing_version: str,
    ) -> RecordResult:
        """Upsert into the (fingerprint, primary_model) accumulator."""
        if not isinstance(cost_usd, Decimal):
            raise TypeError("cost_usd must be Decimal")
        if success_score is not None and not (0.0 <= success_score <= 1.0):
            raise ValueError("success_score must be in [0, 1] or None")
        with self._lock:
            return self._record_locked(
                fingerprint, primary_model, success_score, cost_usd, latency_ms, pricing_version
            )

    def _record_locked(
        self,
        fingerprint: Fingerprint,
        primary_model: str,
        success_score: float | None,
        cost_usd: Decimal,
        latency_ms: float,
        pricing_version: str,
    ) -> RecordResult:
        sig = structural_signature(fingerprint.structural)
        provider = fingerprint.embedding_provider
        existing_fp_id = self._lookup_fingerprint_by_sig(sig, provider)
        was_new = existing_fp_id is None
        fp_id = existing_fp_id or fingerprint.id

        now_us = _to_micros(self._now())
        if was_new:
            self._insert_fingerprint(fp_id, fingerprint, sig, now_us)

        before = self._lookup_outcome(fp_id, primary_model)
        cost_micros = _decimal_to_micros(cost_usd)

        if before is None:
            sample_size_after = 1
            score_count = 1 if success_score is not None else 0
            score_mean = float(success_score) if success_score is not None else 0.0
            self._conn.execute(
                """
                INSERT INTO outcomes(
                    fingerprint_id, primary_model, sample_size,
                    success_score_mean, success_score_count,
                    sum_cost_usd_micros, sum_latency_ms,
                    pricing_version_last, last_updated_at_us
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fp_id,
                    primary_model,
                    sample_size_after,
                    score_mean,
                    score_count,
                    cost_micros,
                    float(latency_ms),
                    pricing_version,
                    now_us,
                ),
            )
            sample_size_before = 0
        else:
            sample_size_before = before["sample_size"]
            sample_size_after = sample_size_before + 1
            new_sum_cost = before["sum_cost_usd_micros"] + cost_micros
            new_sum_latency = before["sum_latency_ms"] + float(latency_ms)
            if success_score is not None:
                # Welford increment on the score mean (count-based).
                old_count = before["success_score_count"]
                old_mean = before["success_score_mean"]
                new_count = old_count + 1
                new_mean = old_mean + (float(success_score) - old_mean) / new_count
            else:
                new_count = before["success_score_count"]
                new_mean = before["success_score_mean"]
            self._conn.execute(
                """
                UPDATE outcomes SET
                    sample_size = ?,
                    success_score_mean = ?,
                    success_score_count = ?,
                    sum_cost_usd_micros = ?,
                    sum_latency_ms = ?,
                    pricing_version_last = ?,
                    last_updated_at_us = ?
                WHERE fingerprint_id = ? AND primary_model = ?
                """,
                (
                    sample_size_after,
                    new_mean,
                    new_count,
                    new_sum_cost,
                    new_sum_latency,
                    pricing_version,
                    now_us,
                    fp_id,
                    primary_model,
                ),
            )

        outcomes_count = self._count_outcomes()
        rows_evicted = 0
        over_soft_cap = outcomes_count > self._caps.soft_cap_rows

        if outcomes_count > self._caps.hard_cap_rows:
            stats = self._evict_to_hard_cap(now_us)
            rows_evicted = stats.entries_evicted
            outcomes_count = stats.outcomes_after
            over_soft_cap = outcomes_count > self._caps.soft_cap_rows
        elif over_soft_cap:
            # Opportunistic age trim per §6.4.1. Bounded work.
            stats = self._evict_by_age(now_us)
            if stats.entries_evicted > 0:
                rows_evicted = stats.entries_evicted
                outcomes_count = stats.outcomes_after
                over_soft_cap = outcomes_count > self._caps.soft_cap_rows

        return RecordResult(
            fingerprint_id=fp_id,
            primary_model=primary_model,
            sample_size_before=sample_size_before,
            sample_size_after=sample_size_after,
            was_new_fingerprint=was_new,
            over_soft_cap=over_soft_cap,
            rows_auto_evicted=rows_evicted,
        )

    def update_score(
        self,
        *,
        turn_id: str,
        fingerprint_id: str,
        primary_model: str,
        score: float,
        confidence: float,
        eval_id: str,
        pricing_version: str | None = None,
    ) -> UpdateScoreResult:
        """Apply a late-arriving evaluator verdict to an outcome row.

        Idempotent by `eval_id`: re-applying the same eval is a no-op.
        Re-evaluation of the same `turn_id` (new `eval_id`) rolls back the
        prior contribution before applying the new score, so the latest
        verdict per turn wins.

        Confidence-gate filter (`pattern.min_eval_confidence`) is applied by
        the caller (the routing engine config knows the threshold); the
        store itself only enforces value-range invariants here.
        """
        if not (0.0 <= score <= 1.0):
            raise ValueError("score must be in [0, 1]")
        with self._lock:
            return self._update_score_locked(
                turn_id=turn_id,
                fingerprint_id=fingerprint_id,
                primary_model=primary_model,
                score=score,
                confidence=confidence,
                eval_id=eval_id,
                pricing_version=pricing_version,
            )

    def _update_score_locked(
        self,
        *,
        turn_id: str,
        fingerprint_id: str,
        primary_model: str,
        score: float,
        confidence: float,
        eval_id: str,
        pricing_version: str | None,
    ) -> UpdateScoreResult:
        before = self._lookup_outcome(fingerprint_id, primary_model)
        if before is None:
            logger.warning(
                "update_score: unknown outcome row for fingerprint=%s model=%s",
                fingerprint_id,
                primary_model,
            )
            return UpdateScoreResult(
                fingerprint_id=fingerprint_id,
                primary_model=primary_model,
                success_score_mean_before=0.0,
                success_score_mean_after=0.0,
                success_score_count_before=0,
                success_score_count_after=0,
                rolled_back_prior=False,
                applied=False,
            )

        prior = self._conn.execute(
            "SELECT eval_id_applied, score FROM outcome_score_history WHERE turn_id = ?",
            (turn_id,),
        ).fetchone()
        if prior is not None and prior[0] == eval_id:
            return UpdateScoreResult(
                fingerprint_id=fingerprint_id,
                primary_model=primary_model,
                success_score_mean_before=before["success_score_mean"],
                success_score_mean_after=before["success_score_mean"],
                success_score_count_before=before["success_score_count"],
                success_score_count_after=before["success_score_count"],
                rolled_back_prior=False,
                applied=False,
            )

        mean_before = before["success_score_mean"]
        count_before = before["success_score_count"]
        mean = mean_before
        count = count_before
        rolled_back = False

        if prior is not None and prior[0] != eval_id:
            # Re-evaluation: roll back the prior score before re-applying.
            old_score = prior[1]
            if count > 1:
                # Inverse of the count-based Welford increment.
                mean = (mean * count - old_score) / (count - 1)
                count -= 1
            else:
                mean = 0.0
                count = 0
            rolled_back = True

        # Apply the new score.
        new_count = count + 1
        new_mean = mean + (float(score) - mean) / new_count

        now_us = _to_micros(self._now())
        # Pricing version: latch the latest if provided.
        new_pricing_version = (
            pricing_version if pricing_version is not None else before["pricing_version_last"]
        )

        self._conn.execute(
            """
            UPDATE outcomes SET
                success_score_mean = ?,
                success_score_count = ?,
                pricing_version_last = ?,
                last_updated_at_us = ?
            WHERE fingerprint_id = ? AND primary_model = ?
            """,
            (
                new_mean,
                new_count,
                new_pricing_version,
                now_us,
                fingerprint_id,
                primary_model,
            ),
        )
        self._conn.execute(
            """
            INSERT INTO outcome_score_history(
                turn_id, fingerprint_id, primary_model,
                eval_id_applied, score, confidence, applied_at_us
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(turn_id) DO UPDATE SET
                eval_id_applied = excluded.eval_id_applied,
                score = excluded.score,
                confidence = excluded.confidence,
                applied_at_us = excluded.applied_at_us
            """,
            (
                turn_id,
                fingerprint_id,
                primary_model,
                eval_id,
                float(score),
                float(confidence),
                now_us,
            ),
        )

        return UpdateScoreResult(
            fingerprint_id=fingerprint_id,
            primary_model=primary_model,
            success_score_mean_before=mean_before,
            success_score_mean_after=new_mean,
            success_score_count_before=count_before,
            success_score_count_after=new_count,
            rolled_back_prior=rolled_back,
            applied=True,
        )

    # ---- Retrieval -----------------------------------------------------

    def recommend(
        self,
        fingerprint: Fingerprint,
        *,
        cost_weight: float,
        min_confidence: float,
        min_sample_size: int,
        k: int = 10,
    ) -> PatternRecommendation:
        """K-NN + scoring pipeline per `pattern-store.md §8`.

        Returns `chosen_model=None` when:
        - The store is empty.
        - The cluster scores zero across the board.
        - Confidence is below threshold.
        - The chosen model's sample size is below threshold.

        Always returns the full ranked alternatives so the routing engine
        can fall to the next-best capability-validated option.
        """
        # Single lock acquisition wraps the K-NN scan + aggregation so
        # readers don't observe a partially-written outcome row mid-update.
        with self._lock:
            return self._recommend_locked(
                fingerprint,
                cost_weight=cost_weight,
                min_confidence=min_confidence,
                min_sample_size=min_sample_size,
                k=k,
            )

    def _recommend_locked(
        self,
        fingerprint: Fingerprint,
        *,
        cost_weight: float,
        min_confidence: float,
        min_sample_size: int,
        k: int,
    ) -> PatternRecommendation:
        start = now_ms()
        neighbors = self._find_k_nearest_locked(fingerprint, k=k)
        if not neighbors:
            return PatternRecommendation(
                chosen_model=None,
                confidence=0.0,
                alternatives=(),
                sample_size=0,
                elapsed_ms=now_ms() - start,
                k_cluster_size=0,
                fingerprint_id=None,
                fingerprint_kind=fingerprint.kind,
            )

        aggregate_inputs = tuple(
            _AggregateInputs(
                primary_model=n.primary_model,
                success_score_mean=n.success_score_mean,
                success_score_count=n.success_score_count,
                sample_size=n.sample_size,
                avg_cost_usd=n.avg_cost_usd,
            )
            for n in neighbors
        )
        result: AggregationResult = aggregate_recommendation(
            aggregate_inputs, cost_weight=cost_weight
        )

        ranked = tuple(
            ModelOption(
                model=s.model,
                score=s.score,
                sample_size=s.sample_size,
                avg_cost_usd=s.avg_cost_usd,
                success_score_mean=s.success_score_mean,
            )
            for s in result.ranked
        )

        # Look up the structural signature of the input to attach (without
        # creating a row). The pattern.matched event uses the *neighbor*
        # fingerprint id for traceability; the query-time id of the current
        # turn isn't yet stored.
        fp_id_for_event = fingerprint.id

        chosen = result.chosen_model
        if chosen is None:
            return PatternRecommendation(
                chosen_model=None,
                confidence=result.confidence,
                alternatives=ranked,
                sample_size=result.chosen_sample_size,
                elapsed_ms=now_ms() - start,
                k_cluster_size=len(neighbors),
                fingerprint_id=fp_id_for_event,
                fingerprint_kind=fingerprint.kind,
            )

        # Apply confidence + sample-size gates per §8.3.
        chosen_sample_size = result.chosen_sample_size
        if result.confidence < min_confidence or chosen_sample_size < min_sample_size:
            return PatternRecommendation(
                chosen_model=None,
                confidence=result.confidence,
                alternatives=ranked,
                sample_size=chosen_sample_size,
                elapsed_ms=now_ms() - start,
                k_cluster_size=len(neighbors),
                fingerprint_id=fp_id_for_event,
                fingerprint_kind=fingerprint.kind,
            )

        return PatternRecommendation(
            chosen_model=chosen,
            confidence=result.confidence,
            alternatives=ranked,
            sample_size=chosen_sample_size,
            elapsed_ms=now_ms() - start,
            k_cluster_size=len(neighbors),
            fingerprint_id=fp_id_for_event,
            fingerprint_kind=fingerprint.kind,
        )

    def find_k_nearest(self, fingerprint: Fingerprint, k: int) -> tuple[NeighborMatch, ...]:
        """Scan all outcomes; score each by structural Jaccard; return top K.

        Returns outcomes (not fingerprints) — a single fingerprint with three
        primary_models contributes three neighbors.

        Under `fingerprint_version="v2"` and when the query fingerprint
        carries an embedding, the score is the blended similarity from
        `pattern-store.md §16.5`; otherwise (and on a v1 store, or on a v2
        store with a query fingerprint missing its embedding), it is the
        v1 weighted-Jaccard. Mixed-version stores fall back to the v1
        score on any (query, neighbor) pair where either side lacks an
        embedding — see §16.5.3.
        """
        with self._lock:
            return self._find_k_nearest_locked(fingerprint, k=k)

    def _find_k_nearest_locked(self, fingerprint: Fingerprint, k: int) -> tuple[NeighborMatch, ...]:
        if k <= 0:
            return ()
        rows = self._conn.execute(
            """
            SELECT
              o.fingerprint_id, o.primary_model, o.sample_size,
              o.success_score_mean, o.success_score_count,
              o.sum_cost_usd_micros, o.sum_latency_ms,
              f.structural_json, f.embedding_blob, f.embedding_dim
            FROM outcomes o
            JOIN fingerprints f ON f.id = o.fingerprint_id
            """
        ).fetchall()
        if not rows:
            return ()

        query_embedding = (
            fingerprint.embedding
            if self._fingerprint_version == "v2" and fingerprint.embedding is not None
            else None
        )

        scored: list[tuple[float, NeighborMatch]] = []
        for row in rows:
            features = msgspec.json.decode(row[7], type=StructuralFeatures)
            neighbor_embedding: tuple[float, ...] | None = None
            if query_embedding is not None and row[8] is not None and row[9] is not None:
                neighbor_embedding = _decode_embedding_blob(row[8], int(row[9]))
            if query_embedding is not None and neighbor_embedding is not None:
                similarity = blended_similarity(
                    fingerprint.structural,
                    features,
                    a_embedding=query_embedding,
                    b_embedding=neighbor_embedding,
                    alpha=self._embedding_alpha,
                )
            else:
                similarity = weighted_jaccard(fingerprint.structural, features)
            sample_size = int(row[2])
            avg_cost = (
                _micros_to_decimal(int(row[5])) / Decimal(sample_size)
                if sample_size
                else Decimal("0")
            )
            avg_latency = float(row[6]) / sample_size if sample_size else 0.0
            scored.append(
                (
                    similarity,
                    NeighborMatch(
                        fingerprint_id=row[0],
                        primary_model=row[1],
                        similarity=similarity,
                        sample_size=sample_size,
                        success_score_mean=float(row[3]),
                        success_score_count=int(row[4]),
                        avg_cost_usd=avg_cost,
                        avg_latency_ms=avg_latency,
                    ),
                )
            )
        # Stable sort: similarity desc, then fingerprint_id+model for ties.
        scored.sort(key=lambda pair: (-pair[0], pair[1].fingerprint_id, pair[1].primary_model))
        return tuple(match for _, match in scored[:k])

    # ---- Embedding cache (v2; pattern-store.md §16.4) ------------------

    def lookup_embedding(self, text: str, provider_id: str) -> tuple[float, ...] | None:
        """Cache-only read for a `(text, provider_id)` pair.

        Bumps `last_used_at_us` + `use_count` on a hit. Returns `None` on
        miss; callers must decide whether to embed-and-cache (recording
        path) or fall back to v1 jaccard (routing query path; §16.6).
        """
        sha = text_sha256(text)
        with self._lock:
            row = self._conn.execute(
                "SELECT embedding_blob, embedding_dim FROM embedding_cache "
                "WHERE text_sha256 = ? AND provider_id = ?",
                (sha, provider_id),
            ).fetchone()
            if row is None:
                self._cache_misses += 1
                return None
            now_us = _to_micros(self._now())
            self._conn.execute(
                "UPDATE embedding_cache SET last_used_at_us = ?, use_count = use_count + 1 "
                "WHERE text_sha256 = ? AND provider_id = ?",
                (now_us, sha, provider_id),
            )
            self._cache_hits += 1
            return _decode_embedding_blob(row[0], int(row[1]))

    def store_embedding(self, text: str, provider_id: str, vector: tuple[float, ...]) -> None:
        """Write a vector to the cache, then trim if past caps.

        Idempotent: `INSERT OR REPLACE` is used so re-embedding the same
        `(text, provider_id)` pair (e.g., provider drift) does not
        accumulate rows.
        """
        if not vector:
            raise ValueError("store_embedding: empty vector")
        sha = text_sha256(text)
        now_us = _to_micros(self._now())
        blob = _encode_embedding_blob(vector)
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO embedding_cache(text_sha256, provider_id, "
                "embedding_blob, embedding_dim, created_at_us, last_used_at_us, use_count) "
                "VALUES (?, ?, ?, ?, ?, ?, 1)",
                (sha, provider_id, blob, len(vector), now_us, now_us),
            )
            self._trim_embedding_cache(now_us)

    def cache_size(self) -> EmbeddingCacheSize:
        with self._lock:
            rows = int(self._conn.execute("SELECT COUNT(*) FROM embedding_cache").fetchone()[0])
            oldest = self._conn.execute(
                "SELECT MIN(created_at_us) FROM embedding_cache"
            ).fetchone()[0]
            total = int(
                self._conn.execute(
                    "SELECT COALESCE(SUM(LENGTH(embedding_blob)), 0) FROM embedding_cache"
                ).fetchone()[0]
            )
            oldest_age: float | None = None
            if oldest is not None:
                now_us = _to_micros(self._now())
                oldest_age = max(0.0, (now_us - int(oldest)) / 1_000_000 / 86_400)
            return EmbeddingCacheSize(rows=rows, oldest_row_age_days=oldest_age, total_bytes=total)

    def cache_clear(self) -> int:
        with self._lock:
            before = int(self._conn.execute("SELECT COUNT(*) FROM embedding_cache").fetchone()[0])
            self._conn.execute("DELETE FROM embedding_cache")
            return before

    def _trim_embedding_cache(self, now_us: int) -> int:
        """Age-first + LRU + use-count tie-break trim, per §16.4.3."""
        removed = 0
        cutoff_us = now_us - self._embedding_cache_max_age_days * 86_400 * 1_000_000
        cursor = self._conn.execute(
            "DELETE FROM embedding_cache WHERE created_at_us < ?", (cutoff_us,)
        )
        removed += cursor.rowcount or 0
        current = int(self._conn.execute("SELECT COUNT(*) FROM embedding_cache").fetchone()[0])
        excess = current - self._embedding_cache_max_rows
        if excess > 0:
            cursor = self._conn.execute(
                """
                DELETE FROM embedding_cache WHERE rowid IN (
                  SELECT rowid FROM embedding_cache
                  ORDER BY last_used_at_us ASC, use_count ASC, created_at_us ASC
                  LIMIT ?
                )
                """,
                (excess,),
            )
            removed += cursor.rowcount or 0
        return removed

    # ---- Maintenance ---------------------------------------------------

    def size(self) -> StoreSize:
        with self._lock:
            fps = int(self._conn.execute("SELECT COUNT(*) FROM fingerprints").fetchone()[0])
            outs = int(self._conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0])
            oldest = self._conn.execute("SELECT MIN(last_updated_at_us) FROM outcomes").fetchone()[
                0
            ]
            oldest_age: float | None = None
            if oldest is not None:
                now_us = _to_micros(self._now())
                oldest_age = max(0.0, (now_us - oldest) / 1_000_000 / 86_400)
            return StoreSize(fingerprints=fps, outcomes=outs, oldest_outcome_age_days=oldest_age)

    def evict(
        self,
        *,
        max_rows: int | None = None,
        older_than: timedelta | None = None,
    ) -> int:
        """Manual eviction. Returns rows removed."""
        with self._lock:
            now_us = _to_micros(self._now())
            evicted = 0
            if older_than is not None:
                cutoff_us = now_us - int(older_than.total_seconds() * 1_000_000)
                cursor = self._conn.execute(
                    "DELETE FROM outcomes WHERE last_updated_at_us < ?", (cutoff_us,)
                )
                evicted += cursor.rowcount or 0
            if max_rows is not None:
                current = int(self._conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0])
                excess = current - max_rows
                if excess > 0:
                    cursor = self._conn.execute(
                        """
                        DELETE FROM outcomes WHERE rowid IN (
                          SELECT rowid FROM outcomes
                          ORDER BY last_updated_at_us ASC, sample_size ASC
                          LIMIT ?
                        )
                        """,
                        (excess,),
                    )
                    evicted += cursor.rowcount or 0
            if evicted > 0:
                self._cleanup_orphan_fingerprints()
            return evicted

    def clear(self) -> int:
        """Delete all rows. Used by `/patterns clear`."""
        with self._lock:
            before = int(self._conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0])
            self._conn.execute("DELETE FROM outcome_score_history")
            self._conn.execute("DELETE FROM outcomes")
            self._conn.execute("DELETE FROM fingerprints")
            return before

    def reprice(self, _price_table: object) -> None:
        """v1 noop per `pattern-store.md §15.3 / Open Questions §13.8`.

        Stored `pricing_version_last` is preserved; future reprice can walk
        outcomes and recompute under a new PriceTable.
        """
        return

    # ---- Internal: SQL helpers -----------------------------------------

    def _lookup_fingerprint_by_sig(self, sig: str, provider: str | None) -> str | None:
        row = self._conn.execute(
            "SELECT id FROM fingerprints "
            "WHERE structural_sig = ? AND ((embedding_provider IS NULL AND ? IS NULL) "
            "                              OR embedding_provider = ?)",
            (sig, provider, provider),
        ).fetchone()
        return row[0] if row else None

    def _insert_fingerprint(
        self, fp_id: str, fingerprint: Fingerprint, sig: str, now_us: int
    ) -> None:
        structural_json = msgspec.json.encode(fingerprint.structural).decode("utf-8")
        embedding_blob = None
        if fingerprint.embedding is not None:
            embedding_blob = _encode_embedding_blob(fingerprint.embedding)
        self._conn.execute(
            """
            INSERT INTO fingerprints(
                id, kind, structural_json, structural_sig,
                embedding_blob, embedding_provider, embedding_dim, created_at_us
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fp_id,
                fingerprint.kind.value,
                structural_json,
                sig,
                embedding_blob,
                fingerprint.embedding_provider,
                fingerprint.embedding_dim,
                now_us,
            ),
        )

    def _lookup_outcome(self, fp_id: str, model: str) -> dict | None:
        row = self._conn.execute(
            """
            SELECT sample_size, success_score_mean, success_score_count,
                   sum_cost_usd_micros, sum_latency_ms, pricing_version_last,
                   last_updated_at_us
            FROM outcomes WHERE fingerprint_id = ? AND primary_model = ?
            """,
            (fp_id, model),
        ).fetchone()
        if row is None:
            return None
        return {
            "sample_size": int(row[0]),
            "success_score_mean": float(row[1]),
            "success_score_count": int(row[2]),
            "sum_cost_usd_micros": int(row[3]),
            "sum_latency_ms": float(row[4]),
            "pricing_version_last": row[5],
            "last_updated_at_us": int(row[6]),
        }

    def _count_outcomes(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0])

    def _count_fingerprints(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM fingerprints").fetchone()[0])

    def _evict_by_age(self, now_us: int) -> _EvictionStats:
        cutoff_us = now_us - self._caps.max_age_days * 86_400 * 1_000_000
        fps_before = self._count_fingerprints()
        outs_before = self._count_outcomes()
        oldest_evicted = self._conn.execute(
            "SELECT MIN(last_updated_at_us) FROM outcomes WHERE last_updated_at_us < ?",
            (cutoff_us,),
        ).fetchone()[0]
        cursor = self._conn.execute(
            "DELETE FROM outcomes WHERE last_updated_at_us < ?", (cutoff_us,)
        )
        evicted = cursor.rowcount or 0
        if evicted > 0:
            self._cleanup_orphan_fingerprints()
        outs_after = self._count_outcomes()
        fps_after = self._count_fingerprints()
        age_days = None
        if oldest_evicted is not None:
            age_days = max(0.0, (now_us - int(oldest_evicted)) / 1_000_000 / 86_400)
        return _EvictionStats(
            fingerprints_before=fps_before,
            fingerprints_after=fps_after,
            outcomes_before=outs_before,
            outcomes_after=outs_after,
            entries_evicted=evicted,
            oldest_evicted_age_days=age_days,
        )

    def _evict_to_hard_cap(self, now_us: int) -> _EvictionStats:
        fps_before = self._count_fingerprints()
        outs_before = self._count_outcomes()
        # 1. Age-first: drop anything past max_age_days.
        age_stats = self._evict_by_age(now_us)
        current = age_stats.outcomes_after
        # 2. LRU + sample-size tie-break: trim down to the soft cap so we
        #    have headroom (per §6.3: "evict the row with the oldest
        #    last_updated_at"; we trim past hard cap, with soft-cap as the
        #    target since hard-cap auto-evict implies the store has grown
        #    well past acceptable).
        oldest_evicted_us: int | None = None
        if current > self._caps.hard_cap_rows:
            excess = current - self._caps.soft_cap_rows
            row = self._conn.execute(
                """
                SELECT MIN(last_updated_at_us) FROM (
                  SELECT last_updated_at_us FROM outcomes
                  ORDER BY last_updated_at_us ASC, sample_size ASC
                  LIMIT ?
                )
                """,
                (excess,),
            ).fetchone()
            if row is not None:
                oldest_evicted_us = row[0]
            cursor = self._conn.execute(
                """
                DELETE FROM outcomes WHERE rowid IN (
                  SELECT rowid FROM outcomes
                  ORDER BY last_updated_at_us ASC, sample_size ASC
                  LIMIT ?
                )
                """,
                (excess,),
            )
            if (cursor.rowcount or 0) > 0:
                self._cleanup_orphan_fingerprints()
        outs_after = self._count_outcomes()
        fps_after = self._count_fingerprints()
        age_days = age_stats.oldest_evicted_age_days
        if oldest_evicted_us is not None:
            from_lru = max(0.0, (now_us - int(oldest_evicted_us)) / 1_000_000 / 86_400)
            age_days = max(age_days or 0.0, from_lru)
        return _EvictionStats(
            fingerprints_before=fps_before,
            fingerprints_after=fps_after,
            outcomes_before=outs_before,
            outcomes_after=outs_after,
            entries_evicted=outs_before - outs_after,
            oldest_evicted_age_days=age_days,
        )

    def _cleanup_orphan_fingerprints(self) -> None:
        self._conn.execute(
            "DELETE FROM fingerprints WHERE id NOT IN (SELECT fingerprint_id FROM outcomes)"
        )

    # ---- Aggregation accessors (for tests / inspection) ----------------

    def list_outcomes(self) -> tuple[ScoredModel, ...]:  # pragma: no cover - debug helper
        rows = self._conn.execute(
            "SELECT fingerprint_id, primary_model, sample_size, success_score_mean, "
            "       success_score_count, sum_cost_usd_micros FROM outcomes"
        ).fetchall()
        out: list[ScoredModel] = []
        for row in rows:
            sample_size = int(row[2])
            avg_cost = (
                _micros_to_decimal(int(row[5])) / Decimal(sample_size)
                if sample_size
                else Decimal("0")
            )
            out.append(
                ScoredModel(
                    model=row[1],
                    score=0.0,
                    sample_size=sample_size,
                    avg_cost_usd=avg_cost,
                    success_score_mean=float(row[3]),
                    success_score_count=int(row[4]),
                )
            )
        return tuple(out)
