# Pattern Store Specification

**Status:** v1 implemented (Phase 2.5; routing slot 4 wired); v2 contract drafted in §16 (Phase 4 implementation pending §A3-rev3)
**Last updated:** 2026-05-14

> **v1 scope.** Per-workspace, bounded SQLite-backed store of task fingerprints
> + outcomes that powers routing slot 4 (`PATTERN_RECOMMENDATION`) per
> `routing-engine.md §5.5`. Phase 2.5 wedge. Embedding-provider-agnostic;
> v1 default fingerprint is purely structural (no embeddings). Multi-user
> aggregation is Phase 3+ and explicitly out of scope.
>
> **v2 scope (§16).** Hybrid fingerprint with a pluggable `EmbeddingProvider`,
> a bounded embedding cache, and a blended cosine-plus-Jaccard similarity.
> Implementation contingent on §A3-rev3 (Wave 9 candidate) failing to
> invert slot 4 under v1's structural-only fingerprint after Wave 8a's
> three unblocks. If §A3-rev3 inverts, v2 stays specs-only.

---

## 1. Purpose

The pattern store is the per-workspace, bounded, agent-curated record of
"how a similar task went last time, and which model was used." It is the
storage substrate behind the third leg of Metis's differentiating trio
(per `STRATEGY.md §4`): bounded memory, lossless canonical IR, and
**task-fingerprint pattern learning**.

The store answers one question for the routing engine: *given the current
turn's fingerprint, which model has historically produced the highest
score on similar tasks, and how confident are we?* The routing engine
consumes this answer at slot 4 of the policy chain (`routing-engine.md §4.1`),
ranked below user-set policy and configured rules so learned behavior
never silently overrides user intent.

Without this spec, "learned routing" is interface-only — the routing
engine's pattern slot returns `not_applicable` by construction. The
pattern store is the substrate that lets slot 4 actually fire.

This spec depends on:

- `canonical-message-format.md` for `Message`, `Usage`, `RoutingDecisionRecord`,
  and the `next_monotonic_ulid()` id convention.
- `event-bus-and-trace-catalog.md` for `session.ended`, `turn.completed`,
  `tool.called`, `llm.call_completed`, `feedback.*`, and the pattern domain
  (§6.5b) which this spec extends with three new event types.
- `routing-engine.md §5.5` for the K-nearest aggregation math and the
  `cost_weight` / `min_confidence` / `min_sample_size` config knobs.
- `memory-store.md` as the reference shape for a Phase-2 bounded-storage
  spec; this document mirrors its goals/non-goals/caps/eviction structure.

---

## 2. Goals and non-goals

### 2.1 Goals

1. **Bounded by design.** Both the row count and the per-row age are
   capped. Unbounded vector slop is the dominant competitor pattern
   (`STRATEGY.md §4`); bounded is the wedge. The peer with the same
   stance is Letta (Series-A; per-block character caps with agent
   self-edit tools). Metis's pattern store extends that stance from
   episodic memory into routing-decision history.
2. **Local-first.** No external embedding service required for v1. The
   fingerprint is computable from session state alone. Embedding
   providers are pluggable for Phase 3+ but never required.
3. **Workspace-scoped.** One store per workspace, mirroring `MEMORY.md`.
   Per-user, per-team, and global rollups are out of scope (Phase 3+;
   `STRATEGY.md §6.6` defers).
4. **Honest cost arithmetic.** All costs are `Decimal`, all averages
   propagate `pricing_version` (per `event-bus §6.3` `llm.call_completed`)
   so historical fingerprints can be re-priced under a current price table
   without losing fidelity. Mirrors the analytics savings model
   (`analytics-api.md §4.7`).
5. **Observable eviction.** Soft-cap overflow emits `pattern.evicted` as
   a signal, mirroring `memory.eviction`. Hard-cap overflow drops the
   oldest rows automatically (this differs from `memory-store.md`; see
   §6.2 rationale).
6. **Cheap to query.** Routing-engine target is ≤5ms per turn
   (`routing-engine.md §2.1.8`). The pattern slot's share of that budget
   is ≤3ms at v1 scale (≤1000 fingerprints).
7. **Embedding-provider abstract.** Whether v1 uses structural-only
   fingerprints or v2 adds embeddings, the store's interface and the
   routing engine's call site don't change. Provider lock-in is a wedge
   we'd lose by accident.

### 2.2 Non-goals

1. **Cross-workspace pattern sharing.** v1 is workspace-scoped, period.
   Multi-user / team rollups are deferred to Phase 3+ (sync layer);
   `STRATEGY.md §2` and §6.6 are unresolved on whether team patterns are
   even a desirable surface.
2. **Real-time learning.** Patterns are written at session end, not
   mid-turn. A turn can't observe the pattern from earlier in the same
   session. Rationale: outcomes require post-turn signal (cost, latency,
   success score) that only stabilize after the turn completes; writing
   mid-session creates ordering hazards.
3. **Replacement for the trace store.** The pattern store derives from
   trace events but isn't a substitute for them. If the pattern store is
   lost, it can be rebuilt by re-running the projection over the trace.
4. **Free-text search over fingerprints.** No FTS5. Queries are K-NN by
   similarity, not "find me sessions about auth bugs."
5. **Vector database.** Even in Phase 3+ embedding mode, the storage is
   SQLite with a custom similarity function or a compact extension
   (`sqlite-vss` is a candidate). No standalone vector service.
6. **Auto-extracted intent labels.** The fingerprint is mechanical —
   computed from observable structural state. LLM-based topic
   classification is deferred; see Open Questions §13.4.

---

## 3. File layout

```
<workspace>/.metis/
├── MEMORY.md           # workspace facts (see memory-store.md)
├── USER.md             # user facts (see memory-store.md)
└── patterns.db         # this spec — pattern fingerprints + outcomes
```

The `patterns.db` file is created lazily on first write. Missing file =
empty store; reads return "no neighbors found." It is a separate SQLite
file from the trace database (`~/.metis/metis.db` by default — distinct
on-disk file) and the session/message store (also in the trace database
in v1 per `canonical-message-format.md §9.1`).

The separation is deliberate:

- The trace store is the system of record for events; the pattern store
  is a derived projection. Mixing them risks accidental coupling.
- `patterns.db` is workspace-scoped (lives under the workspace); the
  trace DB is process-scoped (lives in the user's home). Different
  retention semantics, different sync futures.
- A pattern-store wipe (the user runs `/patterns clear`) shouldn't
  affect trace history. A trace-store wipe shouldn't poison routing.

In Phase 3+ when sync ships, `patterns.db` is a candidate for
`git`-tracked workspace sync; the trace DB likely isn't.

---

## 4. Schema (Python interface)

All types are `msgspec.Struct(frozen=True)` per the canonical convention
(`AGENTS.md`, implementation conventions). Costs are `Decimal`. Ids use
`next_monotonic_ulid()` from `canonical/ids.py`.

```python
class FingerprintKind(StrEnum):
    """Closed enum. Adding a kind is a deliberate spec change."""
    STRUCTURAL = "structural"          # v1: no embeddings
    HYBRID     = "hybrid"              # v2+: structural + embedding vector


class StructuralFeatures(Struct, frozen=True):
    """The deterministic, observable shape of a turn or short turn cluster.

    Computed from session state at routing time without any LLM call.
    Used as the v1 fingerprint and as the structural half of v2 hybrids.
    """
    # File/extension surface
    file_extensions: tuple[str, ...]   # sorted, lowercase, ".py", ".sql", etc.
    file_path_buckets: tuple[str, ...] # workspace-relative top-level dirs touched

    # Tool surface
    tool_names: tuple[str, ...]        # sorted; what tools the agent has used in this session
    side_effect_classes: tuple[str, ...]  # "read" | "write" | "execute" | "network"

    # Input/output shape
    has_images: bool
    has_tool_calls_in_history: bool
    estimated_input_tokens_bucket: int  # log10 bucket: 0=<1k, 1=1k-10k, 2=10k-100k, 3=100k+

    # Intent hints (heuristic; computed by mechanical regex over the new user message)
    intent_tags: tuple[str, ...]       # e.g. ("commit",), ("refactor",), ("architecture",),
                                       # ("debug",), ("doc",), ("test",); empty if no match

    # Provenance (not used in similarity; recorded for analytics)
    workspace_hash: str                # SHA-256 of workspace path (per event-bus §6.1)


class Fingerprint(Struct, frozen=True):
    """The full fingerprint stored in the pattern store. Kind determines
    whether the embedding vector is populated."""
    id: str                            # ULID; primary key
    kind: FingerprintKind
    structural: StructuralFeatures
    embedding: tuple[float, ...] | None  # None for STRUCTURAL; populated for HYBRID
    embedding_provider: str | None     # "openai:text-embedding-3-small", etc.; None for STRUCTURAL
    embedding_dim: int | None          # parallel to embedding; for sanity checks
    created_at: datetime               # UTC, microsecond


class Outcome(Struct, frozen=True):
    """The per-fingerprint outcome record. One per (fingerprint, primary_model)
    accumulator; updated on session.ended."""
    fingerprint_id: str
    primary_model: str                 # canonical "provider:name"
    sample_size: int                   # number of sessions that contributed
    success_score_mean: float          # 0..1; mean across contributing sessions
    success_score_count: int           # how many sessions provided a non-null score
    avg_cost_usd: Decimal              # mean total session cost across contributing sessions
    avg_latency_ms: float              # mean wall_time across contributing turns in the cluster
    pricing_version_last: str          # the most recent pricing_version seen on contributing rows
    last_updated_at: datetime


class PatternRecommendation(Struct, frozen=True):
    """What PatternStore.recommend() returns to the routing engine.
    Maps 1:1 to the payload shape that routing-engine.md §5.5 consumes."""
    chosen_model: str | None           # None if no recommendation (insufficient signal)
    confidence: float                  # 0..1; (top_score - runner_up) / top_score
    alternatives: tuple[ModelOption, ...]  # full ranked list from the K-cluster
    sample_size: int                   # total samples backing chosen_model
    elapsed_ms: float                  # how long the lookup + scoring took


class ModelOption(Struct, frozen=True):
    """One entry in PatternRecommendation.alternatives. Same shape as
    route.decided.chain[].pattern_alternatives in event-bus-and-trace-catalog
    §6.5 (route.decided)."""
    model: str
    score: float                       # aggregate score per routing §5.5 formula
    sample_size: int                   # neighbors with primary_model == model
    avg_cost_usd: Decimal              # for display / debugging
    success_score_mean: float          # for display / debugging


class PatternStore:
    """The per-workspace store. Inject via SessionManager's pattern_factory,
    same pattern as memory_factory (see memory-store.md §9.5)."""

    def __init__(self, workspace_path: str | Path,
                 *, embedder: Embedder | None = None,
                 caps: PatternCaps | None = None) -> None: ...

    @property
    def workspace_path(self) -> str: ...

    # --- Recording (called by SessionEndedSubscriber; never by tools directly) ---
    def record(
        self,
        fingerprint: Fingerprint,
        primary_model: str,
        success_score: float | None,
        cost_usd: Decimal,
        latency_ms: float,
        pricing_version: str,
    ) -> RecordResult:
        """Upsert into the outcome accumulator for (fingerprint, primary_model).
        Soft-cap overflow returns over_soft_cap=True (caller emits
        pattern.evicted). Hard-cap overflow auto-evicts oldest rows (see §6.2)."""

    # --- Retrieval (called by the routing engine at slot 4) ---
    def recommend(
        self,
        fingerprint: Fingerprint,
        cost_weight: float,
        min_confidence: float,
        min_sample_size: int,
        k: int = 10,
    ) -> PatternRecommendation: ...

    def find_k_nearest(
        self, fingerprint: Fingerprint, k: int
    ) -> tuple[NeighborMatch, ...]:
        """Lower-level: just neighbors, no scoring. Exposed for /patterns inspect."""

    # --- Maintenance ---
    def size(self) -> StoreSize:
        """Row counts + oldest-row age. Used by /patterns status."""

    def evict(self, *, max_rows: int | None = None, older_than: timedelta | None = None) -> int:
        """Manual eviction. Returns rows removed."""

    def clear(self) -> int:
        """Delete all rows. Used by /patterns clear; emits pattern.evicted with
        trigger='manual'. Returns rows removed."""

    # --- Reprice (Phase 3+; mirrors AnalyticsStore.savings re-pricing) ---
    def reprice(self, price_table: PriceTable) -> None:
        """Walk Outcomes, recompute avg_cost_usd under the new PriceTable
        using each row's pricing_version_last. Used after a price-table update
        so K-cluster scoring reflects current economics. v1 may noop and let
        rows age out under the existing pricing_version_last."""


class Embedder(Protocol):
    """Abstract over embedding providers. v1 ships no implementation —
    the structural-only fingerprint path needs no embedder. Phase 3+ may
    register a default."""
    def embed(self, text: str) -> tuple[float, ...]: ...

    @property
    def provider_id(self) -> str: ...   # "openai:text-embedding-3-small", etc.

    @property
    def dim(self) -> int: ...


@dataclass(frozen=True)
class RecordResult:
    """Returned by record(); carries hashes for pattern.recorded events."""
    fingerprint_id: str
    primary_model: str
    sample_size_before: int
    sample_size_after: int
    was_new_fingerprint: bool          # True if first sighting; False if accumulator update
    over_soft_cap: bool                # >= soft cap after this write
    rows_auto_evicted: int             # > 0 if hard cap fired


@dataclass(frozen=True)
class PatternCaps:
    """Per-workspace caps. Defaults below; pinned at construction time so
    tests can construct narrower stores. v1 caps target single-user laptop
    scale; Phase 3+ may raise."""
    soft_cap_rows: int = 5_000
    hard_cap_rows: int = 10_000
    max_age_days: int = 180            # rows older than this are eligible for eviction first
```

---

## 5. The fingerprint

### 5.1 What gets fingerprinted

**Unit of fingerprinting: the turn.**

A turn is `routing-engine.md §3.1`'s unit — one user message, one routing
decision, one or more LLM calls + tool cycles until `stop_reason: end_turn`.
This matches what the routing engine actually queries against (per
`routing-engine.md §5.5`: "the K nearest fingerprints to the current
turn's fingerprint").

Per-tool-cycle is too granular (routing is turn-locked; no decision is
made per tool). Per-message is identical to per-turn for the user
message that starts the turn. Per-session is too coarse — one session
can have ten unrelated turns; the routing engine needs per-turn signal.

### 5.2 Fingerprint shape

**v1 is structural-only.** No embeddings, no LLM calls, no external
service. Fingerprints are computed mechanically from session state at
routing time.

The structural feature set is enumerated in `StructuralFeatures` (§4).
The rationale for each field:

| Field                            | Why included                                                       |
|----------------------------------|--------------------------------------------------------------------|
| `file_extensions`                | A SQL-heavy turn looks different from a TypeScript-heavy turn; extension is the cheapest classifier. |
| `file_path_buckets`              | Top-level dirs (`src/auth`, `tests`, `docs`) distinguish task domains within a workspace. |
| `tool_names`                     | "Has called read_file" vs "has run shell" is a strong shape signal. |
| `side_effect_classes`            | A read-only exploration turn vs a write-heavy refactor turn route to different models. |
| `has_images`                     | Capability gate (routing-engine §4.4); also a strong shape signal. |
| `has_tool_calls_in_history`      | A first turn (no prior tool calls) differs from a tenth turn. Distinguishes cold starts. |
| `estimated_input_tokens_bucket`  | Log-bucketed so a 50k-token turn matches a 70k-token turn (same regime), not a 1k-token turn. |
| `intent_tags`                    | Mechanical regex over the user message: `commit`, `refactor`, `architecture`, `debug`, `doc`, `test`. Captures the strongest user-stated intent. |
| `workspace_hash`                 | Provenance only; never participates in similarity (would force exact match). |
| `workload_id`                    | Optional caller-supplied tag (`str \| None`, default `None`). When the caller is the benchmark harness (one workload per run) it's the workload name; agent-loop sessions leave it `None`. A near-keyed partition for K-NN — see §5.3. |

**v2 hybrids** add an embedding vector over the user message text, with
the structural features still used as a regularizer in the similarity
blend (an "auth bug" embedding shouldn't match a "format JSON" turn
even if texts are superficially similar). Hybrid mode is **not** in
v1; the schema admits it (`Fingerprint.kind`, `embedding`,
`embedding_provider`) so v2 lands as a data-only addition, not a
contract change. The full v2 implementation contract — the
`EmbeddingProvider` Protocol, three concrete providers, the embedding
cache, the blended-similarity formula, the `fingerprint_version`
config flag, and the trade-off surface — is in §16.

### 5.3 Similarity

For v1 structural-only fingerprints, similarity is a **weighted Jaccard
overlap** over the structural features:

```
sim(A, B) =  0.30 * jaccard(A.intent_tags,         B.intent_tags)
           + 0.20 * jaccard(A.file_extensions,     B.file_extensions)
           + 0.15 * jaccard(A.tool_names,          B.tool_names)
           + 0.10 * jaccard(A.file_path_buckets,   B.file_path_buckets)
           + 0.10 * jaccard(A.side_effect_classes, B.side_effect_classes)
           + 0.10 * (A.estimated_input_tokens_bucket == B.estimated_input_tokens_bucket)
           + 0.05 * (A.has_images == B.has_images)
```

Empty sets on both sides match (Jaccard convention: `0/0 → 1`); empty
on one side and not the other contributes 0. The weights sum to 1.

`workload_id` (added 2026-05-14) acts as a **near-keyed partition** rather
than as one tag among many. When both fingerprints carry a workload_id,
the structural score above is blended with a strong cluster signal so
same-workload neighbors land near 1.0 and different-workload neighbors
collapse toward 0.0 even when their structural features happen to
overlap:

```
sim_blended(A, B) = 0.85 * (1 if A.workload_id == B.workload_id else 0)
                  + 0.15 * sim_structural(A, B)
```

When either side has `workload_id == None` the blend is skipped and the
result is exactly the v1 weighted-Jaccard — non-benchmark callers (which
never set it) see identical behavior. This closes the §A3-rev finding
that K-NN was mixing workloads because `intent_tags` is empty on most
turns, washing out the cluster signal.

For v2 hybrid fingerprints, similarity blends structural Jaccard with
cosine over the embedding vector. The full formula and the `α = 0.6`
default are specified in §16.5; this section's v1 weighted-Jaccard
remains the structural half of the v2 blend.

`workspace_hash` is **not** included — within a workspace it would
always match; across workspaces queries are forbidden in v1.

### 5.4 What's recorded alongside

Per `Outcome` (§4):

- **`primary_model`** — the model the turn was decided to use. This is
  the `route.decided.chosen_model` for the turn.
- **`success_score`** — `Optional[float]` in `[0, 1]`. The evaluator
  feeds this in (see "Coordinates with evaluator.md" below). When the
  evaluator hasn't run or has no signal, the outcome is recorded with
  `success_score = None` and `success_score_count` does not increment;
  the K-NN aggregation in `routing-engine.md §5.5` ignores those rows
  for the success-score average (but they still contribute to
  `sample_size` for the confidence check).
- **`cost_usd`** — `Decimal` total cost over the turn's `llm.call_completed`
  rows. Sums planner + worker (per `delegate.completed` rollup) when
  delegation is in play (Phase 4+).
- **`latency_ms`** — wall time per `turn.completed.wall_time_seconds * 1000`.
- **`pricing_version`** — the pricing version stamped on the
  `llm.call_completed` event(s). Necessary for re-pricing under a future
  `PriceTable` so the cost-efficiency math in `routing-engine.md §5.5`
  uses comparable units.

The outcome is **per (fingerprint, primary_model)**, not per session.
Multiple sessions hitting the same fingerprint accumulate into a single
row — `sample_size += 1`, means recomputed via Welford-style streaming
update so old samples aren't re-fetched. This keeps the row count
roughly bounded by `|unique_fingerprints| * |models|` rather than
`|sessions|`.

---

## 6. Caps and eviction

| Cap                | Default       | Trigger                                          |
|--------------------|---------------|--------------------------------------------------|
| `soft_cap_rows`    | 5,000 outcomes | Emit `pattern.evicted` (signal only; no removal) |
| `hard_cap_rows`    | 10,000 outcomes | Auto-evict oldest rows until under hard cap     |
| `max_age_days`     | 180 days       | Eviction candidates ranked oldest-first         |

### 6.1 Why bounded

Same wedge as `memory-store.md §5.2`: unbounded vector slop is what
competitors ship (per `STRATEGY.md §4`). Bounded patterns force
relevance — old, rarely-touched fingerprints age out and don't dilute
the K-nearest cluster with stale signal. The model registry changes
quarterly (new Anthropic / OpenAI releases); patterns from 6 months ago
about a deprecated model aren't useful evidence.

### 6.2 Why hard-cap auto-evicts (and `memory-store.md` doesn't)

`memory-store.md` rejects hard-cap writes so the agent must
`memory_consolidate`. The pattern store **auto-evicts** instead. The
asymmetry is deliberate:

- **Memory writes are agent-curated, individually meaningful.** Each
  `memory_add` is a deliberate choice; rejecting forces a curate step.
- **Pattern writes are mechanical projections of session activity.** A
  session ends; an outcome is recorded; the agent has no judgment role
  here. There's no "consolidation" the agent could perform — the
  outcome rows are by construction independent.
- **Blocking pattern writes would silently degrade routing.** A user
  who hits the hard cap and isn't watching would have new sessions
  fail to contribute to the K-nearest cluster, and the pattern slot
  would slowly become biased toward old data. Auto-evicting old data
  to make room for new data keeps the slot useful.

The eviction is observable via `pattern.evicted` (§7.3). Rows evicted
are reported in the event payload so it's clear when the cap was hit.

### 6.3 Eviction policy

When eviction fires (either via auto-evict on hard-cap overflow or
explicit `PatternStore.evict()`):

1. **Age-first.** Rows with `last_updated_at` older than `max_age_days`
   are evicted before any others, regardless of cap pressure. This is a
   continuous trim, not just a cap response.
2. **LRU among remaining.** Among rows under the age threshold, evict
   the rows with the oldest `last_updated_at`.
3. **Sample-size tie-break.** Among rows with equal `last_updated_at`,
   evict the row with the lowest `sample_size` first (keep
   battle-tested signal over single-shot rows).

The eviction is atomic at the SQLite transaction level — either all
condemned rows are removed or none are. `RecordResult.rows_auto_evicted`
reports the count.

### 6.4 Soft-cap behavior

When a write would bring the store between `soft_cap_rows` and
`hard_cap_rows`:

- The write **succeeds**.
- `RecordResult.over_soft_cap = True`.
- The caller (session-ended subscriber; never the agent) emits
  `pattern.evicted` with `entries_evicted: 0` and a warning trigger.
- The next batch eviction sweep (Phase 3+: scheduled background; v1:
  on every record that hits soft cap, run a continuous trim per §6.3.1)
  is a candidate to clean up old rows.

This mirrors `memory.eviction` semantics: soft cap is the signal that
the store is getting heavy; hard cap is the enforcement.

#### 6.4.1 Continuous trim on soft-cap hit

When a `record()` lands the store between soft and hard cap, v1
opportunistically calls the age-first eviction pass (§6.3.1 only —
remove rows older than `max_age_days`). This is bounded work
(`O(rows_older_than_threshold)`), runs inside the session-end batch
subscriber, and keeps the store from accumulating runs of stale rows
without an explicit operator call.

If no rows are old enough to evict and the store is still under hard
cap, no eviction happens — the row simply sits over soft cap until
either age accrues or hard cap forces removal.

---

## 7. Storage

### 7.1 SQLite schema

```sql
-- Fingerprints: one row per unique structural shape.
CREATE TABLE fingerprints (
  id                  TEXT PRIMARY KEY,                       -- ULID
  kind                TEXT NOT NULL,                          -- "structural" | "hybrid"
  structural_json     TEXT NOT NULL,                          -- StructuralFeatures as JSON
  structural_sig      TEXT NOT NULL,                          -- SHA-256 of canonical-form structural_json
  embedding_blob      BLOB,                                   -- packed float32, nullable
  embedding_provider  TEXT,
  embedding_dim       INTEGER,
  created_at_us       INTEGER NOT NULL,                       -- unix microseconds
  UNIQUE (structural_sig, embedding_provider)
);

CREATE INDEX idx_fp_created     ON fingerprints(created_at_us);
CREATE INDEX idx_fp_struct_sig  ON fingerprints(structural_sig);

-- Outcomes: one row per (fingerprint, primary_model) accumulator.
CREATE TABLE outcomes (
  fingerprint_id          TEXT NOT NULL,
  primary_model           TEXT NOT NULL,
  sample_size             INTEGER NOT NULL,
  success_score_mean      REAL NOT NULL,                      -- Welford-updated
  success_score_count     INTEGER NOT NULL,                   -- rows with non-null score
  sum_cost_usd_micros     INTEGER NOT NULL,                   -- Decimal stored as integer micros
  sum_latency_ms          REAL NOT NULL,
  pricing_version_last    TEXT NOT NULL,
  last_updated_at_us      INTEGER NOT NULL,
  PRIMARY KEY (fingerprint_id, primary_model),
  FOREIGN KEY (fingerprint_id) REFERENCES fingerprints(id) ON DELETE CASCADE
);

CREATE INDEX idx_outcomes_updated ON outcomes(last_updated_at_us);

-- Counter snapshots: emitted occasionally for /patterns status without scanning.
CREATE TABLE store_meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
-- Reserved keys:
--   schema_version       -> "1"
--   total_fingerprints   -> stringified int (best-effort; recomputed if stale)
--   total_outcomes       -> stringified int
--   last_eviction_at_us  -> stringified int
```

**Notes on storage choices:**

- **Cost stored as integer micros**, not REAL or TEXT. Avoids float
  drift in repeated Welford updates; converts back to `Decimal` on
  read using `Decimal(micros) / Decimal(1_000_000)`. Consistent with
  the analytics convention (`analytics-api.md §4.7` re-pricing math).
- **Welford streaming update** for `success_score_mean` and
  `avg_cost_usd` derived from `sum_cost_usd_micros / sample_size`. No
  raw per-session history retained — those rows are in the trace store
  if needed.
- **`structural_json` as TEXT, not packed columns.** Schema evolves
  with the spec; JSON gives forward compat. The signature column
  (`structural_sig`) is the join key for dedup.
- **WAL + `synchronous=NORMAL`** like the trace store (per
  `event-bus-and-trace-catalog.md §7.2`). Pattern writes are not on the
  fast event path (they run after `session.ended` in the batch
  subscriber), so durability vs throughput is less critical, but the
  config keeps inserts under 1ms when a batch lands.
- **No virtual columns in v1.** If specific queries get slow (e.g., the
  Jaccard scan), Phase 3+ adds extracted columns (`json_extract` over
  `structural_json` into virtual columns for `intent_tags`,
  `file_extensions`).

### 7.2 K-nearest implementation note

The Jaccard scan over `outcomes JOIN fingerprints` is `O(n)` in the
fingerprint count. At v1 cap (≤10,000 outcomes; ≤~3,000 unique
fingerprints in practice) this is sub-millisecond on a modern SSD.

Phase 3+ may add an `intent_tags` inverted index or a packed structural
signature to prune the scan; v1 doesn't need it.

For hybrid (v2+) fingerprints with embeddings, the structural pre-filter
narrows the candidate set before the cosine pass. v1 omits cosine
entirely.

### 7.3 No raw session retention

The pattern store records **aggregated outcomes**, not raw per-session
rows. This is intentional:

- The trace store is the system of record for per-session detail.
- Aggregating in the pattern store keeps row count bounded by
  `|unique_fingerprints| * |models|` rather than `|sessions|`.
- Welford updates make accumulation lossless for mean/variance without
  per-row storage.
- "Show me the sessions backing this recommendation" is answerable by
  joining trace events on `route.decided.chosen_model = M` + fingerprint
  match; not the pattern store's job.

If a session needs to be re-applied (e.g., the evaluator's score
arrives late), the contributing event in the trace store has all the
fields needed to call `record()` again, and `record()` is idempotent
under deterministic input (Welford-correct re-application is out of
scope; v1 accepts at-most-once per session at the call site).

---

## 8. Retrieval / matching

This section maps the pattern store's `recommend()` to the routing
engine's slot 4 (`routing-engine.md §4.1` and §5.5). The math is fully
specified in routing-engine.md; this spec implements it.

### 8.1 The call site

The routing engine's pattern policy, evaluating slot 4, calls:

```python
recommendation = pattern_store.recommend(
    fingerprint = compute_fingerprint(turn_context),
    cost_weight = workspace_config.pattern.cost_weight,    # default 0.05
    min_confidence = workspace_config.pattern.min_confidence,  # default 0.05
    min_sample_size = workspace_config.pattern.min_sample_size,  # default 5
    k = 10,
)

if recommendation.chosen_model is None:
    return None    # chain continues to next policy

return RoutingDecision(
    chosen_model = recommendation.chosen_model,
    confidence = recommendation.confidence,
    alternatives = recommendation.alternatives,
)
```

### 8.2 K-nearest

`PatternStore.find_k_nearest(fingerprint, k)` returns the K outcomes
(across all `primary_model` values) whose **parent fingerprints** are
most similar to the input. K is over outcomes, not fingerprints — a
single fingerprint with three primary_models contributes three rows.

If fewer than K outcomes exist in the store, all are returned.

### 8.3 Scoring

Implements `routing-engine.md §5.5` verbatim:

```
For each model M present in the K-cluster:
  normalized_success_M = mean(success_score_mean) over neighbors with primary_model = M
                          (weighted by sample_size? — see §8.4)

  avg_cost_M = mean(avg_cost_usd) over neighbors with primary_model = M

  if max_avg_cost == min_avg_cost:
      normalized_cost_efficiency_M = 0
  else:
      normalized_cost_efficiency_M = (max_avg_cost - avg_cost_M) /
                                     (max_avg_cost - min_avg_cost)

  score_M = (1 - cost_weight) * normalized_success_M
          + cost_weight       * normalized_cost_efficiency_M

chosen = argmax(score)
runner_up = second_argmax(score)
confidence = (score_chosen - score_runner_up) / score_chosen   if score_chosen > 0 else 0

if confidence < min_confidence: return None
if sample_size(chosen) < min_sample_size: return None
```

### 8.4 Sample-size weighting in the mean

The routing-engine spec is silent on whether `normalized_success_M`
weights each contributing neighbor by its own `sample_size` or treats
all neighbors equally. **v1 weights by sample_size**, i.e. computes
`Σ(success_mean_i * sample_size_i) / Σ(sample_size_i)`. Rationale: an
outcome row with 50 contributing sessions is stronger evidence than a
single-shot row; equal weighting would let a noisy one-off dominate a
well-evidenced row.

This is an interpretation, not a contradiction of routing-engine.md.
Flagged in the routing-engine cross-reference checklist below to land
the clarification in routing-engine.md §5.5 in the next sweep.

### 8.5 Failure modes

| Condition                                          | `recommend()` returns                              |
|----------------------------------------------------|----------------------------------------------------|
| Store is empty                                     | `PatternRecommendation(chosen_model=None, confidence=0.0, alternatives=(), sample_size=0, elapsed_ms=...)` |
| K-cluster found but every primary_model is unavailable per routing capability gates | Same as empty — `chosen_model=None`. Capability validation happens in routing-engine; the store doesn't know what's available. Returns the full ranked list so routing can pick the next-best validated option from `alternatives`. |
| `confidence < min_confidence`                      | `chosen_model=None` per §8.3                       |
| `sample_size < min_sample_size`                    | `chosen_model=None` per §8.3                       |
| K-cluster all on one model                         | `chosen_model=that_model`, `confidence=1.0` (degenerate case; no runner-up) |
| All candidate models have identical `avg_cost`     | Cost-efficiency term zeros per `routing-engine.md §5.5`; score falls to pure quality |
| Store file is corrupted (SQLite read error)        | Log at WARN; return empty recommendation; do not crash |

The chain-fallthrough invariant from `routing-engine.md §4.6` means a
`None` return is always safe — the next policy in the chain runs.

---

## 9. Routing integration

This section is the contract between the pattern store and the routing
engine. It is descriptive — the authoritative ordering is in
`routing-engine.md §4.1`.

### 9.1 Where slot 4 fires

Per `routing-engine.md §4.1`, slot 4 is `PATTERN_RECOMMENDATION` and
ranks below per-message override, manual sticky, and configured rules.
The pattern store **never overrides user intent** by construction.

When a pattern recommendation is available but a higher-priority policy
won, the recommendation is **deferred** (recorded in
`route.decided.chain[]` with `verdict: "deferred"` per
`routing-engine.md §7.1`). The pattern store does not need to know
this; it just returns the recommendation.

### 9.2 Disagreement surfacing

`routing-engine.md §5.6` describes an opt-in Phase 3 feature where a
high-confidence pattern recommendation that disagrees with the chosen
rule is surfaced to the user. The pattern store itself doesn't drive
this — the routing engine and the TUI do. The store is queried the same
way regardless; the surfacing decision is downstream.

When the user accepts via `/route override`, the existing
`route.overridden` event fires (`event-bus §6.5b`). When the user
dismisses via `/route ignore`, the existing `pattern.override_dismissed`
event fires.

### 9.3 Failure mode if store is empty / unavailable

If the pattern store hasn't been initialized, `patterns.db` is missing,
or the file is unreadable, the routing engine treats the pattern slot
as if it returned `None`. The chain continues. No turn ever fails
because the pattern store is down.

The store's `recommend()` is responsible for catching its own errors
(SQLite open failures, schema-version mismatches) and returning an
empty `PatternRecommendation`. Logged at WARN; not emitted as a bus
event (consistent with `event-bus §3.5` on bus diagnostics in logs).

### 9.4 Cost-weight resolution

The `cost_weight`, `min_confidence`, and `min_sample_size` knobs come
from `routing.yaml` (per `routing-engine.md §5.1`). Current defaults
(see `routing/policy.py:PatternConfig`):

```yaml
pattern:
  cost_weight: 0.1        # was 0.3 before 2026-05-14 (§A3-rev)
  min_confidence: 0.05    # was 0.3 before 2026-05-14 (§A3-rev2)
  min_sample_size: 5
```

The `min_confidence` default scales with `cost_weight`. The confidence
formula is `(top_score - runner_up_score) / top_score`. Under the prior
`cost_weight=0.3` regime, the cost-efficiency term alone produced
~0.35 confidence on tied-quality clusters, so `min_confidence=0.3`
acted as a noise gate. Under `cost_weight=0.1` the same near-tied
clusters produce ~0.10 confidence, so the legacy gate suppresses
genuine cluster inversions: §A3-rev2 Pass C turn 2 on
`write-a-doc-from-notes` aggregated `sonnet=0.900` vs `haiku=0.842`
(confidence `0.064`) and was gated off by the legacy `0.3`. At `0.05`
slot 4 fires; cluster-empty / zero-score / fewer-than-K cases still
gate off in `aggregation.py`. See `benchmarks/RESULTS.md §A3-rev2
finding` for the data trail.

Resolution is per-workspace first, then global (per `routing-engine.md
§5.1` "Workspace `pattern` config replaces the corresponding global
section for that workspace"). The pattern store doesn't read the yaml
itself — the routing engine resolves the effective knobs and passes
them to `recommend()`.

### 9.5 Interaction with the existing `route.decided.chain[].pattern_*` fields

`event-bus-and-trace-catalog.md §6.5` and `routing-engine.md §7.1`
already define:

- `chain[].confidence: float | None` — populated when the policy is `pattern`.
- `chain[].pattern_alternatives: list[ModelOption] | None` — the
  ranked alternatives.

The pattern store's `PatternRecommendation` maps 1:1 to these. The
routing engine fills them in when assembling the `route.decided` event.
No catalog change is required to support v1; the fields already exist.

---

## 10. Events

This spec adds **three new event types** to `event-bus-and-trace-catalog.md
§6.5b` (the Pattern domain). All are additive; existing pattern events
(`route.overridden`, `pattern.override_dismissed`) are unchanged.

The additions are flagged in `docs/specs/CHANGES.md` per AGENTS.md
("Adding a new event type" → step 4: log to CHANGES.md).

### 10.1 `pattern.recorded`

> **Sensitivity:** `pseudonymous`
> **Phase:** 2.5
> **Actor:** SYSTEM
> **Parent:** `session.ended`

Emitted by the `session.ended` batch subscriber after computing the
session's contributing fingerprints + outcomes and calling
`PatternStore.record()` for each. One event per fingerprint/model pair
written, not one per session.

```python
{
    "fingerprint_id": str,                  # ULID
    "fingerprint_kind": Literal["structural", "hybrid"],
    "primary_model": str,
    "sample_size_before": int,
    "sample_size_after": int,
    "was_new_fingerprint": bool,
    "success_score": float | None,          # this session's score (None if evaluator didn't run)
    "cost_usd": float,                      # this session's contribution; for cross-checking
    "pricing_version": str,
    "over_soft_cap": bool,                  # store state after this write
}
```

Sensitivity rationale: structural features and model ids are
`pseudonymous` (no raw user content). The `fingerprint_id` is a ULID,
not a content hash, so it doesn't leak information about the source
text.

### 10.2 `pattern.matched`

> **Sensitivity:** `pseudonymous`
> **Phase:** 2.5
> **Actor:** SYSTEM
> **Parent:** `route.decided`

Emitted when the routing engine's slot 4 wins (i.e., the pattern policy
chose the model used for the turn). Distinct from `route.decided`
(which describes the full chain) — this event is queryable for
"how often does pattern routing fire?" without a JSON scan over
`route.decided.chain`.

```python
{
    "fingerprint_id": str,                  # fingerprint computed for this turn
    "fingerprint_kind": Literal["structural", "hybrid"],
    "chosen_model": str,                    # mirrors route.decided.chosen_model
    "confidence": float,
    "sample_size": int,                     # neighbors backing chosen_model
    "k_cluster_size": int,                  # total neighbors found (≤ K)
    "alternatives_count": int,              # how many distinct models scored
}
```

Not emitted when the pattern policy *deferred* (a rule won). The
deferred recommendation is already captured in
`route.decided.chain[].verdict = "deferred"` per `routing-engine.md §7.1`.

### 10.3 `pattern.evicted`

> **Sensitivity:** `pseudonymous`
> **Phase:** 2.5
> **Actor:** SYSTEM
> **Parent:** `pattern.recorded` (when triggered by a write hitting a cap)
> **Parent:** none (when triggered manually via `/patterns clear` or scheduled trim)

Mirrors `memory.eviction` per `event-bus §6.7`. Fired when:

1. A write lands the store over `soft_cap_rows` (signal only;
   `entries_evicted` may be 0).
2. A write lands the store over `hard_cap_rows` and auto-evict
   removed rows.
3. The continuous trim (§6.4.1) removed age-stale rows.
4. The operator ran `/patterns clear` (full wipe).

```python
{
    "trigger": Literal["soft_cap_signal", "hard_cap_evict",
                       "age_trim", "manual_clear"],
    "fingerprints_before": int,
    "fingerprints_after": int,
    "outcomes_before": int,
    "outcomes_after": int,
    "entries_evicted": int,                 # outcomes removed; 0 for soft_cap_signal
    "oldest_evicted_age_days": float | None,  # for age_trim and hard_cap_evict
}
```

Sensitivity rationale: counts and ages, no content.

### 10.4 Late-arriving scores from the evaluator (`update_score()` flow)

The evaluator's verdicts arrive **asynchronously** — typically seconds
to minutes after `session.ended` for the heuristic judge, possibly
minutes to days for hybrid escalation or manual re-evaluation (per
[`evaluator.md §4.6`](evaluator.md), §6.1). The pattern store
accommodates this with a two-phase write:

**Phase 1 (`session.ended` subscriber).** Each turn's outcome is
recorded immediately with `success_score=None`. `pattern.recorded`
fires with `success_score: null` and `sample_size_after = sample_size_before + 1`.
The accumulator's `success_score_count` is **not** incremented (no score
yet); `success_score_mean` is unchanged. Cost/latency/pricing_version
are written in full — those signals are known at session end.

**Phase 2 (`eval.completed` subscriber).** When a verdict lands for a
`turn_id`, the subscriber:

1. Looks up the turn's `(fingerprint_id, primary_model)` via the
   `route.decided` event (parent of the turn).
2. Calls `PatternStore.update_score(turn_id, score, confidence,
   eval_id, pricing_version)`.
3. The store applies a single Welford increment for this sample:
   `success_score_count += 1`; `success_score_mean` updated with the
   new score; `pricing_version_last` set if the eval's pricing version
   is more recent.
4. A second `pattern.recorded` event fires with the now-known
   `success_score` set (sample_size_before == sample_size_after,
   signaling a score-only update).

**Idempotence.** `update_score()` is keyed by `eval_id` — applying the
same `eval_id` twice is a no-op. The store maintains a small
`(turn_id → applied_eval_id)` table (or equivalent) so re-delivery
doesn't double-count.

**Latest-verdict rule (MAX(eval_id)).** Re-evaluation produces a new
`eval.completed` with a fresh `eval_id` for the same `(subject_kind,
subject_id)` (per [`evaluator.md §4.6`](evaluator.md), §11.1). When
multiple verdicts exist for a `turn_id`, pattern-store callers join on
`MAX(eval_id) PER subject` to obtain the latest verdict. The pattern
store's K-cluster aggregation reflects only the latest verdict per
contributing turn:

- If the latest eval_id replaces an older one (same `turn_id`),
  `update_score()` rolls back the prior contribution (subtract the
  old score weighted by its sample_size_at_apply_time) before
  applying the new score. The pricing version stamp updates to the
  latest `eval.completed.judge_pricing_version` when present.
- If no prior verdict exists for the `turn_id`, `update_score()`
  applies the new score as a first-time write.

The rollback step is exact when implemented with retained
per-(turn_id, primary_model) score history, which v1 maintains in a
narrow `outcome_score_history(turn_id, eval_id_applied, score, applied_at_us)`
table. The table is bounded by the same caps as outcomes (one row per
unique turn_id contributing to an outcome; evicted with its parent
outcome row).

**Confidence-gate filter.** Verdicts with `confidence <
pattern.min_eval_confidence` (default `0.5`, per §15.4) are recorded
in the trace store and may invoke `update_score()` — but the score is
**not** folded into `success_score_mean`. The pattern store treats
low-confidence verdicts as "verdict observed but not actionable for
routing." This keeps the agreement-rate view in
`/analytics/quality` complete while preventing noisy verdicts from
biasing K-cluster aggregation.

**Failure mode: verdict for an unknown turn_id.** If `update_score()`
is called for a `turn_id` whose outcome row was never recorded (e.g.,
the session was abandoned before `session.ended` fired), the call
returns a `RecordResult` with `was_new_fingerprint=False` and
`sample_size_after=0`; no row is created. Logged at WARN.

### 10.5 Catalog additions summary

Per AGENTS.md "Adding a new event type":

1. ✅ `PatternRecorded`, `PatternMatched`, `PatternEvicted` to be added
   to `events/payloads.py` (Phase 2.5 implementation; not in this spec).
2. ✅ `PAYLOAD_REGISTRY` entries for `"pattern.recorded"`,
   `"pattern.matched"`, `"pattern.evicted"`.
3. ✅ Catalog §6.5b to be extended with the three payloads (cross-spec
   change tracked in `CHANGES.md` entry below).
4. ✅ `CHANGES.md` entry (this change).
5. ⏳ Tests for round-trip + registry membership (Phase 2.5 implementation).

---

## 11. Invariants

1. **Per-workspace storage.** One `patterns.db` per workspace, at
   `<workspace>/.metis/patterns.db`. No global v1 pattern store.
2. **Workspace isolation.** A query against workspace A never reads
   rows from workspace B. v1 has no cross-workspace API at all.
3. **`PatternStore.record()` is at-most-once per (session, fingerprint, primary_model).**
   Re-runs of the session-ended subscriber against the same session id
   are idempotent at the call site (the subscriber dedupes by
   session id before calling `record()`). Welford updates make
   double-counting strictly incorrect, so the dedup is load-bearing.
4. **Costs are `Decimal` everywhere.** Stored as integer micros on disk;
   never `float` in arithmetic.
5. **`pricing_version` is preserved per outcome.** Used for re-pricing
   under a future `PriceTable` (Phase 3+); v1 may noop on reprice but
   stores the field.
6. **No silent capability filtering at the store layer.** `recommend()`
   returns ranked alternatives without checking model availability.
   The routing engine applies capability/availability validation per
   `routing-engine.md §4.4`.
7. **Hard-cap auto-evicts; never blocks writes.** Unlike memory, the
   pattern store can't refuse to record an outcome — the outcome would
   be permanently lost (no agent to retry; this is a batch projection).
8. **Reads on missing file return empty.** No "file not found" errors
   propagate to callers.
9. **One process writer per workspace.** Same single-writer assumption
   as `memory-store.md §9.6`. Phase 3+ sync is when concurrent writes
   become a concern.

---

## 12. Testing strategy

### 12.1 Required tests

1. **Empty store recommend** returns `PatternRecommendation(chosen_model=None, ...)`.
2. **Missing `.metis/patterns.db`** is treated as empty store (no exception).
3. **First record creates a fingerprint and outcome row.**
4. **Second record with same fingerprint + same model** increments
   `sample_size`, recomputes mean via Welford.
5. **Second record with same fingerprint + different model** creates a
   second outcome row, fingerprint count unchanged.
6. **K-NN ranking** — fixture: 3 fingerprints with controlled feature
   overlap; verify the closest one ranks first.
7. **Jaccard weights sum to 1.0** in `StructuralFeatures` similarity.
8. **Cost-weight=0** ⇒ chosen model is the one with highest
   `success_score_mean` regardless of cost.
9. **Cost-weight=1** ⇒ chosen model is the cheapest in the cluster
   regardless of success.
10. **Cost-weight=0.5** ⇒ scoring blends both per `routing-engine.md §5.5`.
11. **Degenerate cluster** (all candidate models in K have identical
    `avg_cost_usd`) — cost-efficiency term zeros; recommendation falls
    to pure quality. Mirrors `routing-engine.md §10.1.25`.
12. **Confidence below threshold** ⇒ `recommend()` returns
    `chosen_model=None`.
13. **Sample size below threshold** ⇒ `recommend()` returns
    `chosen_model=None`.
14. **Soft-cap write** sets `RecordResult.over_soft_cap=True` and emits
    `pattern.evicted` with `trigger="soft_cap_signal"`,
    `entries_evicted=0`.
15. **Hard-cap auto-evict** trims oldest rows; `RecordResult.rows_auto_evicted > 0`;
    emits `pattern.evicted` with `trigger="hard_cap_evict"`.
16. **Age-stale rows trim first** under combined eviction pressure.
17. **Eviction is atomic** — partial-row removal isn't possible
    (transactional).
18. **`/patterns clear` empties the store** and emits `pattern.evicted`
    with `trigger="manual_clear"`.
19. **`success_score=None` recording** updates `sample_size` and
    `avg_cost_usd` but not `success_score_mean`.
20. **Sample-size-weighted mean** — neighbor with sample_size=50 weights
    50× a neighbor with sample_size=1 in the cluster mean. (§8.4)
21. **`pricing_version_last` is the most-recent contributing version.**
22. **Workspace isolation** — two `PatternStore` instances in different
    workspace dirs do not see each other's rows.
23. **Concurrent reads** are safe (SQLite handles this).
24. **`pattern.recorded` event payload** validates against the catalog
    schema once added.
25. **`pattern.matched` event payload** validates against the catalog
    schema once added.
26. **Empty fingerprint sets** (no tools used, no files touched —
    happens on the first turn of a session) produce a valid
    `StructuralFeatures` with empty tuples; recommend returns
    empty/None gracefully.

### 12.2 Property tests

Worth investing in:

- **Idempotence of `record()` modulo session dedup** — recording the
  same (fingerprint, primary_model, cost, latency, score) with
  monotonic-increasing call id produces deterministic outcome state.
- **Welford correctness** — random sequence of `record()` calls; final
  `success_score_mean` matches a naive mean recomputed over all
  contributions.
- **Sort stability of K-NN ranking** — given equal similarities, the
  ordering is deterministic and reproducible across runs.

---

## 13. Open questions

These are **live**; AI agents shouldn't unilaterally close them.
`STRATEGY.md §6.6` calls out the pattern store as a deferred owner
decision. The questions below are the ones that surfaced during this
draft.

1. **Embedding provider in v2.** ~~When (if) v2 adds embeddings, which
   provider?~~ **Resolved 2026-05-14 in §16.3:** three providers
   ship (OpenAI `text-embedding-3-small`, Cohere `embed-multilingual-v3.0`,
   local `sentence-transformers/all-MiniLM-L6-v2`); selection is per
   workspace via `PatternConfig.embedding_provider`; unset means
   structural-only.

2. **Embedding cost amortization.** ~~Embedding cost per turn is
   non-zero...~~ **Resolved 2026-05-14 in §16.7:** `embedding_strategy`
   knob exposes sync (default; OK at human latency) and async (gateway
   path) modes. The cache (§16.4) brings the steady-state per-turn
   cost to ~$0 and latency to ~1ms; cache miss costs are documented
   in §16.7.1.

3. **Fingerprint feature weights.** The Jaccard weights in §5.3 are an
   educated guess. They should be tuned against the benchmark suite
   (`benchmark.md`) once enough data exists. Open to refinement after
   first deployment.

4. **Auto-generated intent tags.** `intent_tags` are mechanical regex.
   A small classifier (or even an LLM call at session end) could
   generate richer tags ("auth-flow-debug" vs just "debug"). Tradeoff:
   cost + non-determinism vs richer cluster shape. Deferred — let v1
   regex run; revisit if cluster quality is poor.

5. **Cross-workspace patterns.** Should patterns from `~/code/proj-a`
   inform routing in `~/code/proj-b` (same user, different workspace)?
   v1: no, hard isolation. Argument for: a Python developer's "deep
   for architecture" pattern probably transfers. Argument against:
   workspace isolation is the privacy floor; mixing leaks information
   across project boundaries. Tied to `STRATEGY.md §6.2` buyer profile
   resolution. **Deferred to owner.**

6. **Multi-user / team patterns.** Should multiple users in the same
   buyer org's deployment share patterns? Tied directly to
   `STRATEGY.md §2` (buyer ≠ user) and §6.6. **Deferred to owner;
   v1 is per-workspace-per-process.**

7. **Sample-size weighting in K-NN mean.** §8.4 picks
   sample-size-weighted means as the v1 interpretation. The routing
   engine spec doesn't say which. Reconcile in next routing-engine
   sweep — either pin the weighted interpretation in routing-engine.md
   §5.5 or back out of it here. **Tagged for routing-engine spec
   update.**

8. **Reprice timing.** When `PriceTable` updates (new model, price
   change), should the pattern store immediately walk all outcomes
   and recompute `avg_cost_usd`? Or let rows age out under stale
   pricing? `analytics-api.md §4.7` re-prices on read; the pattern
   store accumulates means and can't re-price at read time without
   losing Welford state. v1 records `pricing_version_last` but does
   not auto-reprice. **Deferred.**

9. **Hard-cap auto-evict vs reject.** §6.2 picks auto-evict; an
   alternative is reject (mirror memory.md). Auto-evict was chosen
   because the agent has no role in pattern curation, but a reviewer
   might prefer the symmetry. **Open for review.**

10. **Pattern store influence on `delegate()` tier resolution.**
    `routing-engine.md §11.6` flags this as deferred — whether a
    `delegate(tier="fast")` call should consult the pattern store to
    pick *which* fast-tier model. v1: no; configured fast model wins.
    Phase 4+ may revisit.

---

## 14. Decision log

| Date       | Decision                                                                 | Rationale                                                                                  |
|------------|--------------------------------------------------------------------------|--------------------------------------------------------------------------------------------|
| 2026-05-13 | Per-workspace SQLite at `<workspace>/.metis/patterns.db`                 | Mirrors `MEMORY.md` placement; STRATEGY.md §2 defers multi-user.                            |
| 2026-05-13 | Bounded store with soft + hard caps, default 5k / 10k rows               | "Eviction is a feature" per AGENTS.md; mirrors `memory-store.md` stance.                    |
| 2026-05-13 | Hard cap auto-evicts (asymmetric with memory)                            | Pattern writes are mechanical projections; no agent curation step possible.                 |
| 2026-05-13 | Unit of fingerprinting is the turn                                       | Matches the routing engine's query unit; per-tool-cycle is too granular.                    |
| 2026-05-13 | v1 fingerprint is structural-only (no embeddings)                        | Local-first, no external dependency, sufficient signal for v1; v2 hybrid lands data-only.   |
| 2026-05-13 | Costs stored as integer micros; `Decimal` at API boundary                | Float drift would compound under Welford updates over 50+ contributing sessions.            |
| 2026-05-13 | Welford streaming update; no raw per-session retention                   | Bounds row count by `unique_fingerprints * models`, not sessions; trace store has raw rows. |
| 2026-05-13 | Sample-size-weighted mean for K-NN cluster aggregation                   | Single-shot rows shouldn't dominate well-evidenced rows; routing-engine §5.5 to align.      |
| 2026-05-13 | Pattern store never overrides user intent (routing slot 4 below rules)   | Routing-engine §4.1 invariant; spec depends on it.                                          |
| 2026-05-13 | Three new event types (`pattern.recorded`, `.matched`, `.evicted`)       | Pattern domain queryability without JSON scans over `route.decided.chain[]`.                |
| 2026-05-13 | Recording happens at session end, not mid-turn                           | Outcomes (cost, latency, score) stabilize after the turn completes; ordering hazards.       |
| 2026-05-13 | `PatternStore.recommend()` returns full ranked alternatives even when chosen=None | Routing engine can apply capability/availability filter and try next-best.          |
| 2026-05-13 | Pricing_version_last preserved per outcome                               | Future reprice under a new `PriceTable` preserves comparable economics.                     |
| 2026-05-13 | Embedding provider abstract; no v1 default                               | Avoid lock-in; structural-only path needs no embedder; v2 lands additively.                 |

---

## 15. Coordinates with `evaluator.md`

> *Authored 2026-05-13 in parallel with Agent 3B's draft of
> `evaluator.md`. Reconciled 2026-05-14 — see CHANGES.md
> "Pattern-store ↔ evaluator reconciliation sweep." The four
> coordination items called out in the original draft are pinned
> below; the body of this section reflects the reconciled contract.*

The pattern store and the evaluator are two halves of the learning
loop:

- The **evaluator** answers "how did that turn go?" per
  [`evaluator.md §3`](evaluator.md). Output is an `EvalVerdict`
  carrying `score` in `[0, 1]` and `confidence` in `[0, 1]`, emitted
  as the payload of `eval.completed`.
- The **pattern store** records `(fingerprint → primary_model → outcome)`
  with that score as the success signal, and surfaces aggregated
  evidence to the routing engine.

### 15.1 Verdict shape — evaluator owns it

The canonical verdict shape is `EvalVerdict` as defined in
[`evaluator.md §4.1`](evaluator.md). The pattern store **does not
re-specify** the verdict; it consumes the fields it needs (`subject_id`
as the join key, `score`, `confidence`, `eval_id`) and treats the rest
(`signals`, `judge_kind`, `rubric_id`, etc.) as opaque pass-through.

The session-ended batch subscriber and the eval-completed subscriber
together orchestrate the flow (see §10.4 below). The pattern store
exposes two entry points the orchestrator calls:

- `PatternStore.record(...)` — called from the `session.ended`
  subscriber. Writes the outcome accumulator with `success_score=None`
  if no verdict has landed yet.
- `PatternStore.update_score(turn_id, score, confidence, eval_id, pricing_version)`
  — called from the `eval.completed` subscriber when a verdict lands
  for a turn whose outcome row was already written. The score join
  key is `turn_id` ([`evaluator.md §3`](evaluator.md)'s `subject_id`
  for `subject_kind=turn`).

The pattern store does **not** call the evaluator; the subscribers
orchestrate both.

### 15.2 Evaluator scope is pattern-store-agnostic

The pattern store is agnostic to *how* the evaluator scores. It
consumes `eval.completed.score: float` and `eval.completed.confidence:
float`. The evaluator's judge tier (heuristic / LLM / hybrid per
[`evaluator.md §5`](evaluator.md)) is invisible at the pattern-store
layer.

The pattern store's `success_score_mean` blends whatever signal lands;
the store doesn't need to know which judge produced it.

### 15.3 Score timing — async; late-arriving verdicts use `update_score()`

**Reconciled 2026-05-14.** Evaluator scoring is **asynchronous**
(per [`evaluator.md §6.1`](evaluator.md): non-fast-path subscriber on
`turn.completed`, with a lookahead window that may delay the verdict
several user messages or up to 24h). The session-ended batch
subscriber does **not** block waiting for verdicts.

Flow:

1. `session.ended` fires.
2. The batch subscriber computes each turn's fingerprint and calls
   `PatternStore.record(fingerprint, primary_model, success_score=None,
   cost_usd, latency_ms, pricing_version)`. The outcome accumulator's
   `success_score_count` is unchanged (since the score is `None`);
   `sample_size` increments. `pattern.recorded` fires with
   `success_score=null`.
3. Later, the evaluator's `eval.completed` event for the turn fires
   (heuristic verdict typically within seconds; hybrid escalation
   within minutes; manual re-evaluation possibly days later).
4. A dedicated subscriber on `eval.completed` looks up the outcome row
   by `(fingerprint, primary_model)` from the turn's `route.decided`
   event and calls
   `PatternStore.update_score(turn_id, score, confidence, eval_id, pricing_version)`.
5. `update_score()` applies a single-shot Welford increment to
   `success_score_mean` and bumps `success_score_count`, then re-emits
   `pattern.recorded` with the now-known `success_score`. Idempotence
   is by `eval_id`: a given `eval_id` updates the score at most once.

Re-evaluation produces a new `eval.completed` with a fresh `eval_id`
(per [`evaluator.md §4.6`](evaluator.md)). Pattern-store consumers
that read `eval.completed` events directly take `MAX(eval_id)` per
`(subject_kind, subject_id)` as the latest verdict; see §10.4.

### 15.4 Confidence-gate filter — lives in pattern-store config

**Reconciled 2026-05-14.** The `pattern.min_eval_confidence` knob
lives in the **pattern-store config block** of `routing.yaml` (sits
next to `pattern.cost_weight`, `pattern.min_confidence`,
`pattern.min_sample_size`). The evaluator does not own this gate; it
is a consumer-side filter applied when the pattern store aggregates
verdicts into K-cluster scores.

**Default: `0.5`** — matches the default declared in
[`evaluator.md §4.3`](evaluator.md).

Resolution path: workspace `routing.yaml` overrides global per the
existing `routing-engine.md §5.1` precedence. Verdicts with
`confidence < pattern.min_eval_confidence` still record (for the
agreement-rate view); they are excluded from the K-cluster success
aggregation only.

Example workspace config:

```yaml
pattern:
  cost_weight: 0.05             # default 0.05 since 2026-05-15 (§A3-rev5; was 0.1 from 2026-05-14, was 0.3 prior)
  min_confidence: 0.05          # default 0.05 since 2026-05-14 (§A3-rev2)
  min_sample_size: 5
  min_eval_confidence: 0.5      # default 0.5
```

### 15.5 Feedback loop

When the user dismisses a pattern recommendation via `/route ignore`
(`pattern.override_dismissed`), that's a weak negative signal on the
recommendation. The evaluator may consume `pattern.override_dismissed`
events as part of its scoring. The pattern store doesn't change shape
to support this; the evaluator queries the trace store.

Similarly, `route.overridden` (user accepts a deferred pattern
recommendation) is a weak positive signal — the user's manual choice
agreed with the pattern store's prediction.

The pattern store **doesn't directly observe** override/dismiss events;
those flow through the evaluator's scoring and arrive as part of the
`score` on the next `eval.completed` event for the affected turn.

### 15.6 Pattern recommendations as evaluator input

The reverse direction: the evaluator may want to know "was this turn
routed by a pattern?" to weight its scoring. This is recoverable from
the trace (`pattern.matched` event from §10.2, parent of the turn) —
no direct surface needed between the specs.

### 15.7 Reconciliation status (2026-05-14)

| Item                                                                         | Outcome                                                                  |
|------------------------------------------------------------------------------|--------------------------------------------------------------------------|
| Verdict-shape compatibility                                                  | Evaluator owns `EvalVerdict` ([`evaluator.md §4.1`](evaluator.md)); pattern store consumes verbatim. ✓ |
| Sync vs async score timing                                                   | Async. `record()` writes immediately with `success_score=None`; `update_score()` patches later. Join key: `turn_id`. ✓ |
| Confidence-gate filter home                                                  | Pattern-store config (`routing.yaml::pattern.min_eval_confidence`); default `0.5`. ✓ |
| Sample-size weighting in K-cluster aggregation                               | Pinned in [`routing-engine.md §5.5`](routing-engine.md) (one-line clarification 2026-05-14). ✓ |
| `MAX(eval_id)` as latest-verdict rule                                        | Documented in §10.4 below; aligned with [`evaluator.md §4.6`](evaluator.md), §11.1. ✓ |
| Three new pattern events in event-bus catalog §6.5b                          | Catalog edit lands with Phase 2.5 implementation (tracked in CHANGES.md). |

---

## 16. v2 hybrid fingerprint: implementation contract

**Status:** Implemented 2026-05-14 (Wave 10). Code lands under
`packages/metis-core/src/metis_core/patterns/embeddings.py` (Protocol +
three providers + a deterministic provider for tests), the
`embedding_cache` table in `PatternStore` (`patterns/store.py`,
schema_version bumped `"1" → "2"`), `cosine_similarity` +
`blended_similarity` in `patterns/similarity.py`, `embedding` +
`embedding_provider` fields on `FingerprintInputs`, `compute_fingerprint`
producing `HYBRID` fingerprints when the embedding is set, the
`attach_embedding_for_recording` async helper (recording path; embeds on
cache miss), the engine-side `_attach_cached_embedding` sync helper
(routing query path; v1 fallback on cache miss per §16.6), and the v2
fields on `PatternConfig` (`routing/policy.py`). v1 default behavior is
unchanged. 52 new tests cover the Protocol contract, blend math, cache
hit/miss/eviction (TTL + LRU), mixed-version K-NN, and the routing
slot-4 v2 code path end-to-end. **v2 stays opt-in** — §A3-rev3 inverted
slot 4 under v1, so v2 is the implementation-ready alternative for
workspaces whose structural-Jaccard washes out (the explicit motivation
in §16.1). The headline cluster-tightening fixture (§16.10 test 5
against the 6-workload + 4-agent-loop corpus) is deferred to a follow-up
A/B benchmark wave.

This section converts the v2 sketch in §5.2, §13.1, and §13.2 into a
concrete implementation contract: an `EmbeddingProvider` Protocol, three
concrete provider impls, an on-disk embedding cache, a blended-similarity
formula, a `fingerprint_version` flag with a forward-only migration path,
and the routing-time cost/latency surface that v2 introduces.

The v1 fingerprint (§5) does not change. v2 is **additive**: a v2
workspace uses hybrid fingerprints; a v1 workspace continues to use
structural-only fingerprints; the two stores interoperate (the K-NN
falls back to the structural-only score when either side lacks an
embedding — see §16.5.3).

### 16.1 Why v2 (the wedge §A3-rev2 didn't unblock)

[`benchmarks/RESULTS.md §A3-rev2`](../../benchmarks/RESULTS.md) shipped
three structural-only unblocks (Wave 8a-1 workload-tagged fingerprints,
8a-2 `cost_weight=0.3 → 0.1`, 8a-3 grounding-check rubric primitive) and
the differentiator still didn't invert: slot 4 emitted `not_applicable`
on all 18 routed turns of Pass C. The K-NN itself was reading correctly
(on `write-a-doc-from-notes` Pass C turn 2 the K-NN aggregated
sonnet=0.900 ahead of haiku=0.842 — the first time in any A3 series),
but the confidence gap formula `(top - runner_up) / top` produced values
0.030–0.231 under `cost_weight=0.1`, all below the unchanged
`min_confidence=0.3` gate.

§A3-rev3 (Agent 9a-7) is the Wave 9 candidate that lowers
`PatternConfig.min_confidence` to ~0.05 (or reshapes the confidence
formula) and lets the v1 structural-only K-NN actually fire slot 4.
**If §A3-rev3 inverts, v2 is unnecessary.** This section exists so v2
is implementation-ready if it doesn't — specifically, if §A3-rev3
reveals that the K-NN's same-workload partition is too brittle to
generalize off the benchmark suite (real user turns rarely carry a
`workload_id`, so the v1 `_structural_jaccard` path runs with mostly
empty `intent_tags` and washes out the cluster signal).

The v2 hybrid fingerprint addresses the wash-out directly: an
embedding over the user message text captures semantic shape even when
`intent_tags` is empty, so K-NN aggregation gets a real signal on
non-benchmark traffic.

### 16.2 `EmbeddingProvider` Protocol

The v2 entry point is a Protocol so the choice of provider is a
per-workspace config decision, not a code change. Implementations must
be **deterministic** (same input → same vector — or near enough that
two calls don't push the cache hit rate below the §16.7.2 target) and
**stable** (`provider_id` persists across provider versions so cached
vectors survive process restarts).

```python
class EmbeddingProvider(Protocol):
    """v2 fingerprint embedding provider.

    Pluggable per-workspace via `PatternConfig.embedding_provider`.
    The provider_id forms part of the cache key (§16.4) and the
    `fingerprints.embedding_provider` column; changing the id
    invalidates cached vectors and forces re-embedding on next use.
    """

    @property
    def provider_id(self) -> str:
        """Stable identifier, e.g. "openai:text-embedding-3-small"
        or "local:sentence-transformers:all-MiniLM-L6-v2".

        Format: "<vendor>:<model>[:<variant>]". The id is opaque to
        the pattern store; it just round-trips through the cache key
        and the fingerprint row."""

    @property
    def dim(self) -> int:
        """Output vector dimension. Constant across calls for a given
        provider_id. The pattern store stores this on the fingerprint
        row (`embedding_dim`) and rejects writes whose vector length
        disagrees."""

    @property
    def max_input_tokens(self) -> int:
        """Maximum input length the provider accepts. The pattern
        store's caller MUST truncate longer inputs before calling
        `embed()` — the provider is allowed to reject (or silently
        truncate) over-length inputs and the result wouldn't be
        deterministic. v2 truncates `user_message_text` to the first
        `max_input_tokens` tokens (approximated as `max_input_tokens *
        4` UTF-8 bytes since v2 doesn't carry a tokenizer per
        provider; see §16.11 open question 4)."""

    async def embed(self, text: str) -> tuple[float, ...]:
        """Compute the L2-normalized embedding for `text`.

        Returns a tuple of length `dim`. L2-normalized so cosine
        similarity reduces to a dot product (§16.5.1). Pure
        inference; no side effects on the provider. Async because
        the API-backed impls are network-bound; local providers
        offload to a thread pool internally."""

    async def aclose(self) -> None:
        """Release provider resources (HTTP client connections,
        local model handles). The pattern store calls this on
        `PatternStore.close()`."""
```

Implementations live under
`packages/metis-core/src/metis_core/patterns/embedders/` (new
subpackage; out of scope here, Phase 4). The Protocol is a `typing.Protocol`
with `@runtime_checkable` so duck-typed test stubs work without
inheritance.

### 16.3 Concrete provider implementations

Three providers ship in v2. Selection is `PatternConfig.embedding_provider:
str | None`; the string is interpreted as the `provider_id` and resolved
via a registry built into `metis_core.patterns.embedders.__init__`. None
means structural-only (§16.6).

| `provider_id`                                      | Vendor                       | Dim   | Cost / 1M tokens | Latency (cache miss)    | Notes                                                                  |
|----------------------------------------------------|------------------------------|-------|------------------|-------------------------|------------------------------------------------------------------------|
| `openai:text-embedding-3-small`                    | OpenAI API                   | 1536  | $0.02            | 50–150ms (US-region)    | Cheapest hosted. Supports `dimensions` param down to 512 (Phase 4 opt-in). |
| `cohere:embed-multilingual-v3.0`                   | Cohere API                   | 1024  | $0.10            | 80–200ms                | Multilingual surface; better for non-English user messages.            |
| `local:sentence-transformers:all-MiniLM-L6-v2`     | sentence-transformers local  | 384   | $0 (compute only)| 30–80ms CPU; 5–10ms GPU | No API; adds ~80MB binary download + Torch dependency. Lower dim but adequate K-NN selectivity per published benchmarks. |

**Construction.** Each impl takes its configuration via a typed
`EmbedderConfig` dataclass (API key env var name, base URL override,
local model checkpoint path) read from the same `routing.yaml`
namespace as the rest of the pattern config (§16.6). API-backed
providers reuse the existing `httpx` async client pool; the local
provider wraps `sentence_transformers.SentenceTransformer` with an
`asyncio.to_thread` shim. A misconfigured provider (missing API key,
unreadable checkpoint) raises at `PatternStore.__init__` time so the
workspace fails fast on first session, not at first turn.

**Determinism caveat.** Hosted providers are *nominally* deterministic
(both vendors document this in their API docs) but minor non-determinism
under load is occasionally observed. The cache (§16.4) absorbs this:
once a (text, provider_id) pair is cached, all subsequent same-input
turns get the exact same vector regardless of upstream drift.

**Provider lock-in stance.** No provider is a "default"; an unset
`embedding_provider` keeps the workspace on v1 structural-only.
Selecting `local:sentence-transformers:*` is the recommended choice
for buyers who self-host everything (per `STRATEGY.md §6.2`); the
hosted options exist for users who already pay for those APIs and
don't want a 200MB Torch install.

### 16.4 Embedding cache

Embedding calls dominate v2's per-turn cost and latency. The cache
brings the steady-state per-turn embedding cost to ~$0 and latency to
~$1ms after the first ~K turns of a workload converge (§16.7.2).

#### 16.4.1 Cache key

The cache key is `(provider_id, SHA-256(user_message_text))`. The same
SHA-256 pre-image is used as the v1 fingerprint's structural dedup
basis — see §5.2 — so re-running an identical user message in the same
workspace under v2 hits both caches (structural signature + embedding)
without recomputation.

#### 16.4.2 SQLite schema (new table; additive to §7.1)

A single new table is added to `<workspace>/.metis/patterns.db`. **No
migration on the existing `fingerprints` or `outcomes` tables** —
those continue under the v1 schema. The new table is created on the
first v2 write; v1 workspaces never see it.

```sql
CREATE TABLE IF NOT EXISTS embedding_cache (
  text_sha256       TEXT NOT NULL,                  -- SHA-256(user_message_text), hex
  provider_id       TEXT NOT NULL,
  embedding_blob    BLOB NOT NULL,                  -- packed float32 (4 * dim bytes)
  embedding_dim     INTEGER NOT NULL,
  created_at_us     INTEGER NOT NULL,               -- unix micros
  last_used_at_us   INTEGER NOT NULL,               -- bumped on read; drives LRU
  use_count         INTEGER NOT NULL DEFAULT 1,     -- read counter; debugging
  PRIMARY KEY (text_sha256, provider_id)
);

CREATE INDEX IF NOT EXISTS idx_embcache_last_used  ON embedding_cache(last_used_at_us);
CREATE INDEX IF NOT EXISTS idx_embcache_created    ON embedding_cache(created_at_us);
```

The `embedding_blob` uses packed float32 (not JSON) because vectors
are dense (every entry is non-zero post-L2-normalization) and JSON
encoding doubles storage for no benefit. Reading is `np.frombuffer(blob,
dtype=np.float32, count=embedding_dim)` (or the pure-Python `struct.unpack`
equivalent if v2 chooses not to add NumPy as a hard dependency — see
§16.11 open question 3).

`schema_version` in `store_meta` is bumped from `"1"` to `"2"` when
the embedding cache table is created. v1 readers tolerating
`schema_version="2"` is the back-compat path — v1 code reads
`schema_version`, accepts `"2"`, and ignores the unknown table.

#### 16.4.3 Eviction

The cache is bounded the same way the outcomes table is bounded
(§6 — eviction is a feature):

| Cap                | Default          | Trigger                                   |
|--------------------|------------------|-------------------------------------------|
| `cache_max_rows`   | 10,000 vectors   | Hard cap; auto-evict on overflow          |
| `cache_max_age`    | 180 days         | Continuous trim; rows past this age go first |

Eviction policy mirrors §6.3:

1. **Age-first.** Rows with `created_at_us` older than `cache_max_age`
   evict before any others.
2. **LRU among remaining.** Evict by oldest `last_used_at_us` (not
   `created_at_us`) — a vector that's still hot stays cached even if
   it was written months ago.
3. **Use-count tie-break.** Among rows with equal `last_used_at_us`,
   evict the row with the lowest `use_count`.

The cache caps are independent of the outcomes caps (§6) — a workspace
might have 5,000 unique fingerprints (each from a unique user message)
but only 8,000 cached vectors after eviction. There is **no foreign
key** between `embedding_cache` and `fingerprints`; a cache miss after
fingerprint eviction is fine, and a cache hit on a never-recorded
fingerprint is fine (the cache is upstream of the fingerprint write).

A new event `pattern.embedding_cache_evicted` is **not** added in v2;
cache eviction is a routine maintenance signal and would flood the bus
on workloads with high churn. Cache size is observable via
`PatternStore.size()` (extended with `embedding_cache_rows: int` in
v2 — see §16.6.3).

#### 16.4.4 Cache-miss flow

On a v2 K-NN query:

1. Compute `text_sha256 = SHA-256(user_message_text)`.
2. `SELECT embedding_blob, embedding_dim FROM embedding_cache
   WHERE text_sha256 = ? AND provider_id = ?`.
3. **Hit:** decode the vector, bump `last_used_at_us` and `use_count`,
   proceed to §16.5 blended similarity.
4. **Miss:** invoke `await provider.embed(text)`, insert into the
   cache (`INSERT OR REPLACE`), proceed to §16.5.

The miss path is the only place an `EmbeddingProvider.embed()` call
fires. Recording-time writes (§10.4 phase 1 / phase 2) **do not**
embed — the embedding only matters for K-NN retrieval, and the
recording-side cost is already paid by the routing-time embed of the
same `user_message_text`.

### 16.5 Blended similarity

For v2 hybrid fingerprints, similarity blends cosine over the embedding
vector with the v1 weighted-Jaccard score:

```
similarity(A, B) = α * cosine(A.embedding, B.embedding)
                 + (1 - α) * weighted_jaccard(A.structural, B.structural)
```

with `α = PatternConfig.embedding_blend_alpha`, default **0.6**.

#### 16.5.1 Cosine similarity

Vectors are L2-normalized by the provider (§16.2), so cosine reduces
to dot product:

```python
def cosine(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    if len(a) != len(b):
        raise ValueError("dim mismatch")
    return sum(x * y for x, y in zip(a, b))
```

Range is `[-1.0, 1.0]` in principle; in practice modern sentence
embeddings sit in `[0.0, 1.0]` for non-adversarial inputs. The blend
formula doesn't rescale — a negative cosine pulls `similarity` down,
which is the correct behavior (semantically opposite messages should
score worse than disjoint ones).

#### 16.5.2 Why α = 0.6 (embedding-dominant)

The default tilts toward embeddings for three reasons:

1. **Structural Jaccard is sparse on non-benchmark turns.** The §A3-rev2
   experience shows `intent_tags`, `file_extensions`, and `tool_names`
   are often empty on the first turn of a session — the v1
   `_structural_jaccard` score collapses to 0.15 (the `has_images` +
   `estimated_input_tokens_bucket` floor) for two unrelated turns and
   to 0.20–0.30 for two superficially related ones. Embeddings discriminate
   better in this regime.

2. **Embeddings carry the user's stated intent in compressed form.**
   "Refactor the auth middleware to drop session token storage" and
   "Move the session-token writes out of the auth middleware" embed
   very close even though they share zero tokens with the v1 regex
   `intent_tags = ("refactor",)` intersection.

3. **Structural is still load-bearing as a regularizer.** When the
   embedding is noisy (a user message that happens to share vocabulary
   with a different domain — "test the auth flow" vs "test the JSON
   parser"), the structural `file_extensions` and `file_path_buckets`
   columns pull the score apart. 40% weight is enough to break
   superficial vocabulary ties.

The 0.6 default is a starting point — §16.11 open question 1 calls
out that the value should be tuned against the benchmark suite once
v2 lands. **`α = 0` reduces v2 to v1 exactly** (structural-only;
useful for A/B comparison). **`α = 1` is embedding-only** — flagged
as a footgun in §16.11 q2 because it removes the regularizer.

#### 16.5.3 Mixed-version stores (v1 rows + v2 rows)

A v2 workspace will have a mix of pre-flag-flip outcomes (recorded
under v1, no embedding column populated) and post-flag-flip outcomes
(v2 hybrid, embedding set). The similarity function handles this:

```python
def similarity(a: Fingerprint, b: Fingerprint, alpha: float) -> float:
    structural_score = weighted_jaccard(a.structural, b.structural)
    if a.embedding is None or b.embedding is None:
        # Either side missing an embedding — fall back to v1.
        return structural_score
    if a.embedding_dim != b.embedding_dim:
        # Provider changed mid-workspace; treat as fallback.
        return structural_score
    return alpha * cosine(a.embedding, b.embedding) + (1 - alpha) * structural_score
```

This makes the migration path forward-only and lossless: old rows stay
queryable, new rows get the blended score, and the K-NN naturally
weighs newer hybrid neighbors above older structural-only ones when
their embeddings agree with the query.

#### 16.5.4 Workload-id partition (v1 §5.3) interaction

The v1 workload-id near-keyed partition still wins when both sides
carry a `workload_id` (§5.3). The full v2 score is:

```python
def similarity_v2(a, b, alpha):
    base = blended_similarity(a, b, alpha)   # the §16.5.0 formula
    if a.workload_id is None or b.workload_id is None:
        return base
    cluster = 1.0 if a.workload_id == b.workload_id else 0.0
    return _WORKLOAD_BLEND_WEIGHT * cluster + (1 - _WORKLOAD_BLEND_WEIGHT) * base
```

`_WORKLOAD_BLEND_WEIGHT` is the existing `0.85` constant from
`similarity.py`. Benchmark runs (which always set `workload_id`)
continue to land same-workload neighbors near 1.0; agent-loop traffic
(which leaves `workload_id=None`) gets the pure v2 blended score.

### 16.6 `PatternConfig.fingerprint_version` and migration

The v2 toggle is one field on a new `PatternConfig` struct that
collects the routing-time knobs that were previously scattered across
`routing.yaml::pattern.*`. The struct is shipped with v2 but the v1
knobs (`cost_weight`, `min_confidence`, `min_sample_size`,
`min_eval_confidence`) move into it without changing semantics.

```python
class PatternConfig(msgspec.Struct, frozen=True):
    """Per-workspace pattern-routing config. Resolved by the routing
    engine from `<workspace>/.metis/routing.yaml::pattern.*` with
    global-then-workspace precedence per routing-engine.md §5.1."""

    # --- v1 (existing) ---
    cost_weight: float = 0.1                   # was 0.3 pre-Wave 8a; see A3-rev2
    min_confidence: float = 0.3                # Wave 9 candidate: 0.05; see §16.6.4
    min_sample_size: int = 5
    min_eval_confidence: float = 0.5           # see §15.4

    # --- v2 (new) ---
    fingerprint_version: Literal["v1", "v2"] = "v1"
    embedding_provider: str | None = None      # provider_id; e.g. "openai:text-embedding-3-small"
    embedding_blend_alpha: float = 0.6         # §16.5
    embedding_strategy: Literal["sync", "async"] = "sync"   # §16.7.3
    embedding_cache_max_rows: int = 10_000
    embedding_cache_max_age_days: int = 180
```

#### 16.6.1 Forward-only migration

A workspace upgrades from v1 to v2 by:

1. Editing `routing.yaml` to set `pattern.fingerprint_version: v2` and
   `pattern.embedding_provider: <provider_id>`.
2. Restarting the agent / gateway / server process (the
   `PatternStore.__init__` reads the config once; no live reload in v2).
3. New turns recorded under v2 get hybrid fingerprints; old outcomes
   continue to be queried under the §16.5.3 fallback rule.

There is **no migration script**. The pattern store doesn't backfill
embeddings for existing rows — re-embedding old user messages would
re-charge the embedding API and may not be worth it for cold rows.
v1 rows age out under §6.3 over the 180-day window; the store
converges to pure-v2 naturally.

#### 16.6.2 Downgrade

Setting `fingerprint_version: v1` after running v2 leaves the v2 rows
in place. The K-NN reads them but the similarity falls back to
structural-only (§16.5.3 — `embedding` is set on the row but the
*query* fingerprint has `embedding=None`, so the fallback fires). No
data loss; no surprise behavior. The `embedding_cache` table is left
on disk; it is dead weight under v1 but bounded so it can't grow.

#### 16.6.3 `PatternStore` API additions

The Protocol surface adds:

```python
class PatternStore:
    # ... existing v1 surface ...

    def __init__(
        self,
        workspace_path: str | Path,
        *,
        caps: PatternCaps | None = None,
        config: PatternConfig | None = None,
        embedder: EmbeddingProvider | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None: ...

    # find_k_nearest, recommend become async to accommodate the embed path.
    # The sync wrappers (`find_k_nearest_sync`, `recommend_sync`) call
    # `asyncio.run(...)` under the hood for code paths that need sync;
    # the routing engine consumes the async surface directly (see §16.7.3).

    async def find_k_nearest(self, fingerprint: Fingerprint, k: int) -> tuple[NeighborMatch, ...]: ...
    async def recommend(self, fingerprint: Fingerprint, *, cost_weight: float,
                        min_confidence: float, min_sample_size: int,
                        k: int = 10) -> PatternRecommendation: ...

    # Cache stats (v2 only):
    def cache_size(self) -> EmbeddingCacheSize:
        """Cached vector count + oldest-row age. Used by /patterns status."""

    def cache_clear(self) -> int:
        """Drop all cached vectors (e.g., after a provider change). Used by /patterns cache clear."""
```

`StoreSize` (§4) gains a parallel struct:

```python
@dataclass(frozen=True)
class EmbeddingCacheSize:
    rows: int
    oldest_row_age_days: float | None
    total_bytes: int                  # sum of len(embedding_blob) — disk footprint
```

#### 16.6.4 Slot-4 confidence gate interaction

§A3-rev2 identified `min_confidence=0.3` as the load-bearing gate that
slot 4 currently doesn't clear under `cost_weight=0.1`. v2's
embedding-dominated similarity is expected to produce sharper
clusters (higher per-cluster intra-similarity, lower inter-cluster
similarity), which should raise the per-model score spread and pull
the confidence gap above the gate without needing the Wave 9
`min_confidence: 0.05` flip.

**This is a hypothesis, not a load-bearing promise.** v2's headline
benchmark (§16.10 test 5) is "does v2 produce a measurably tighter
cluster-score distribution than v1 on the same workload set?" If the
hypothesis fails, Wave 9 (`min_confidence`) and v2 (embedding) are
independent fixes that can both ship; if it holds, v2 alone may
inverts slot 4 on agent-loop traffic without touching the gate.

### 16.7 Live API cost in the routing critical path

v2's biggest semantic shift from v1: the routing engine's slot 4 now
makes a (cached) live API call. v1's slot 4 was pure CPU on a SQLite
JOIN. The trade-off is in §16.9.

#### 16.7.1 Per-turn cost

`openai:text-embedding-3-small` priced 2026-05-14 at $0.02 / 1M input
tokens. A user message embedded under v2 is typically 50–500 tokens
(the first user turn of a session in the benchmark suite averages
~120 tokens). At 200 tokens / turn the per-turn embedding cost is
`200 / 1_000_000 * 0.02 = $0.000004` ≈ **$4 per 1M turns**. The
embedding cost is dominated by the LLM call cost itself by ~5 orders
of magnitude.

Cohere is 5× the OpenAI cost (~$20 / 1M turns). The local provider is
free at the API boundary but adds CPU/GPU pressure on the host.

#### 16.7.2 Cache hit rate target

The miss path's cost and latency only matter on the first sighting of
each unique `user_message_text`. For typical agent workloads, repeated
prompts are common (the user iterates on the same task, the agent's
own composed prompts converge to similar shapes), so the cache hit
rate climbs fast.

**Target: ≥80% cache hit rate within 100 turns of a workload.**

The rationale is empirical-from-v1: §A3-rev2 patterns DBs show ~3,000
unique structural fingerprints accumulated across 100s of benchmark
turns, and `structural_sig` collisions on identical user messages are
common (re-runs of the same workload, multi-turn refactors that
reissue similar prompts). Embeddings are keyed by a tighter cache key
(exact text, not the structural projection), so the hit rate is
*lower* than v1's structural dedup rate, but the empirical ratio of
total turns to unique text in benchmark suite v1 is ~3:1, which
implies a steady-state ~67% hit rate — close to the 80% target after
a workload's "vocabulary" stabilizes.

The target is **non-load-bearing**. If real workloads converge to a
worse hit rate (e.g., agents that never repeat themselves), the cache
still amortizes the cost across repeated workloads run from the same
workspace; the steady-state cost is bounded by §16.7.1 anyway.

A v2 health-check projection (deferred to Phase 4) over the trace
store would surface `cache_hit_rate_per_workspace` on
`/analytics/patterns` once §16.10 test 4 lands.

#### 16.7.3 Sync vs async embed strategy

The routing engine's slot evaluation is synchronous in v1 — slot 4
returns within ~1ms by reading SQLite directly. v2's embed call on a
cache miss is 50–200ms. Two strategies, configurable via
`embedding_strategy`:

- **`sync` (default).** Slot 4 awaits the embed call. On cache miss,
  the routing decision is delayed by the embed latency. The 5ms
  budget from `routing-engine.md §2.1.8` is intentionally violated
  on cache miss; the budget reverts to 5ms on cache hit. Acceptable
  for human-driven agent loops where 200ms is invisible against
  multi-second LLM latency; *not* acceptable for the gateway
  surface which may serve high-QPS clients.
- **`async`.** Slot 4 short-circuits to `not_applicable` when the
  embedding cache misses, and the embed call fires asynchronously
  in the background (writing into the cache for the next turn).
  The first turn for each unique user message gets v1 routing; the
  second and subsequent turns get full v2 routing. Trades immediate
  accuracy for tail-latency predictability. Recommended for the
  gateway path (`gateway.md §2`).

The routing engine consumes the `recommend()` future regardless;
`embedding_strategy` lives inside the `PatternStore` and is
transparent to the routing engine itself (the store returns
`PatternRecommendation(chosen_model=None, ...)` on async miss the
same way it returns it on empty store).

### 16.8 Events (no new types in v2)

v2 reuses the three v1 pattern events (§10). The `fingerprint_kind`
field on `pattern.recorded` / `pattern.matched` already discriminates
`"structural"` vs `"hybrid"` per §10.1 / §10.2 — v1's catalog
addition is forward-compatible with v2 by construction.

No new event type for embedding cache miss/hit is added in v2: routing
critical-path events should not flood the bus at v2 traffic rates. A
debug log line at DEBUG level is enough; aggregate hit-rate analytics
live in the §16.7.2 projection (Phase 4 deferred surface).

### 16.9 Trade-off: v2 is not strictly cheaper than v1

v2 buys cluster-quality and slot-4 inversion at the cost of a routing-time
API call. **It is qualitatively different from v1, not strictly better.**
The trade-offs:

| Dimension                         | v1 (structural)                                | v2 (hybrid)                                                          |
|-----------------------------------|------------------------------------------------|----------------------------------------------------------------------|
| Routing budget (cache hit)        | ~1ms SQLite scan                               | ~1ms SQLite scan + cache lookup                                      |
| Routing budget (cache miss)       | n/a (always hit)                               | 50–200ms (sync) or short-circuit + bg embed (async)                  |
| Per-turn cost                     | $0                                             | ~$0.000004 (OpenAI) to $0 (local)                                    |
| External dependency               | None                                           | Embedding API / 80MB local model binary                              |
| Cluster quality on benchmark suite| §A3-rev2: K-NN reads correctly, doesn't invert | Hypothesis: tighter clusters; pending §16.10 test 5 to confirm        |
| Cluster quality on agent traffic  | Empty `intent_tags` → washes out               | Embedding captures intent → discriminates                            |
| Privacy                           | Workspace-local                                | API providers see hashed-but-recoverable user messages on cache miss |
| Sync-mode tail latency            | Predictable ~1ms                               | Bimodal (~1ms on hit, ~100ms on miss)                                |
| Maintenance burden                | One table; one similarity function             | Two tables; provider registry; cache eviction sweep; vendor SDK pins |

The honest framing is "v2 is the move once v1 is provably stuck."
§A3-rev3 (Agent 9a-7) is the load-bearing experiment that answers
"is v1 stuck?" before v2 spends complexity budget.

### 16.10 Test plan

The tests below are **specified**, not implemented. Implementation
lands with Phase 4; the test count is what the implementation owes.

1. **`EmbeddingProvider` Protocol contract test.** Given a
   `runtime_checkable` Protocol, any class implementing
   `provider_id` / `dim` / `max_input_tokens` / `embed` /
   `aclose` `isinstance(x, EmbeddingProvider) is True`. Negative
   case: missing `dim` raises.

2. **`EmbeddingProvider` determinism test.** For each shipped impl
   (with the API impls behind an env-gated marker), `embed(text)` ==
   `embed(text)` byte-for-byte. The local provider is fully
   deterministic under a fixed PYTHONHASHSEED; the API providers are
   tested via a `responses=`-style HTTP record/replay fixture so the
   determinism check verifies the cache key construction, not the
   provider's wire output.

3. **Cosine similarity unit test.** `cosine([1, 0, 0], [1, 0, 0]) ==
   1.0`; `cosine([1, 0, 0], [0, 1, 0]) == 0.0`; `cosine([1, 0, 0],
   [-1, 0, 0]) == -1.0`. Dim mismatch raises `ValueError`. Length
   verified against an L2-normalized input pair to confirm cosine =
   dot product under §16.2's normalization invariant.

4. **Blend math unit test.** `α = 0` ⇒ output == structural Jaccard;
   `α = 1` ⇒ output == cosine; `α = 0.6, cosine = 0.8, jaccard = 0.4`
   ⇒ output == 0.64. Workload-id partition (§16.5.4) interaction:
   same-workload pair with `α = 0.6, cosine = 0.0, jaccard = 0.0`
   still scores ≥ `_WORKLOAD_BLEND_WEIGHT` (=0.85). Mixed-version
   pair (one side `embedding=None`) falls back to pure Jaccard
   regardless of `α`.

5. **v2 fingerprint cluster differs from v1 on the same input set
   (the headline test of why v2 is worth implementing).** Fixture: a
   curated 60-turn set drawn from the §A3-rev2 patterns DBs spanning
   all 6 benchmark workloads + 4 "off-benchmark" agent-loop traces.
   For each turn, compute the v1 fingerprint and a v2 fingerprint with
   the local provider. Compute the pairwise similarity matrix under
   v1 and under v2 (`α=0.6`). Assertion: the average intra-cluster
   similarity (same-workload pairs) under v2 is **at least 0.10
   higher** than under v1, AND the average inter-cluster similarity
   (different-workload pairs) is **at least 0.05 lower** under v2
   than under v1. Both deltas measure cluster tightening; failing
   either delta is a "v2 doesn't pay for itself" signal that justifies
   pulling v2 from Phase 4.

6. **Cache hit on identical text.** Two `find_k_nearest` calls with
   the same `user_message_text` issue one `provider.embed` call.

7. **Cache miss on different text.** Two calls with different text
   issue two `embed` calls.

8. **Cache miss on different provider_id.** Two calls with same text
   but different configured providers issue two embed calls (cache
   key includes `provider_id`).

9. **Cache TTL eviction.** A row with `created_at_us` older than
   `cache_max_age_days` is evicted on the next eviction pass; a
   `find_k_nearest` that would have hit it now misses.

10. **Cache size cap auto-evicts LRU first.** Filling the cache to
    `cache_max_rows + 1` evicts exactly one row; the evicted row is
    the one with the oldest `last_used_at_us`.

11. **Mixed-version store K-NN.** Outcomes recorded under v1 (no
    embedding) coexist with v2 outcomes; `find_k_nearest` over a v2
    query returns both, scored under §16.5.3's fallback rule for v1
    neighbors and the blended rule for v2 neighbors.

12. **Provider-mismatch fallback.** A v2 fingerprint embedded under
    provider X compared against a v2 fingerprint embedded under
    provider Y falls back to structural-only per §16.5.3 (the
    `embedding_dim` mismatch path).

13. **`fingerprint_version` flag default is v1.** A `PatternConfig()`
    constructed without args returns `fingerprint_version="v1"`; a
    `PatternStore` initialized without `embedder=` runs v1 even if
    `fingerprint_version="v2"` is set (the embedder is required at
    construction; an unset embedder under v2 raises at init).

14. **Async-mode short-circuit.** Under `embedding_strategy="async"`,
    a first call with a cache-miss `user_message_text` returns
    `chosen_model=None` immediately and schedules a background embed;
    a second call with the same text after `await asyncio.sleep(0.5)`
    hits the now-populated cache and returns a real recommendation.

15. **Schema-version bump on first v2 write.** A v1 patterns DB
    (`schema_version="1"`, no `embedding_cache` table) opened by a v2
    `PatternStore` is upgraded in-place: `schema_version` becomes
    `"2"` and `embedding_cache` is created. No existing rows are
    touched. Reopening the same DB under a v1 process succeeds
    (`schema_version="2"` is tolerated; unknown table ignored).

### 16.11 Open questions (v2-specific)

These are live; the spec doesn't unilaterally close them.

1. **Default `embedding_blend_alpha`.** 0.6 is an educated guess
   informed by the §16.5.2 sparsity argument. Should be tuned
   against the §16.10 test-5 fixture once v2 lands. Candidate range:
   `[0.4, 0.8]`.

2. **`α = 1` (embedding-only) — disallowed?** Removing the structural
   regularizer is a footgun on workspaces with vocabulary-overlap
   noise; `α = 0` is fine (it reduces to v1). The spec currently
   allows both; consider gating `α ≥ 1.0` behind a warning.

3. **NumPy as a hard dep.** Packed float32 ops are cleaner with NumPy
   (`np.frombuffer`, `np.dot`). The current `metis-core` has no NumPy
   dependency. v2 either adds NumPy or implements cosine via
   `struct.unpack` + a manual loop. The local sentence-transformers
   provider transitively requires NumPy anyway, so a v2 install that
   uses the local provider already pulls it in — the question is
   whether the API providers should also force the dependency.
   **Tentative call:** add NumPy as a hard `metis-core` dep when v2
   lands; the savings benchmark suite already pulls it via
   sentence-transformers in some test runs.

4. **Tokenizer per provider.** §16.2's `max_input_tokens` is
   provider-specific; v2 truncates by byte length (`max_input_tokens
   * 4`) as a coarse approximation. A provider with a non-Latin
   user-message workload (Cohere multilingual case) under-truncates
   under this rule. The proper fix is per-provider tokenizers; v2's
   coarse rule is a documented limitation.

5. **Persist embedding on `fingerprints` row vs cache-only.**
   §16.4.2's cache is keyed by `text_sha256`. The `fingerprints`
   table's `embedding_blob` column (existing in v1 schema, currently
   always NULL) could *also* hold the vector. The v2 spec proposes
   **populating both** — the `fingerprints.embedding_blob` is the
   "long-term" copy that survives cache eviction, and the
   `embedding_cache` is the "fast lookup" copy for K-NN queries.
   This doubles storage but the vectors are tiny (1536 × 4 = 6KB per
   v3-small embedding; 10k rows = ~60MB max). The alternative
   (cache-only) would force re-embedding when the cache evicts a
   vector that the K-NN still needs. **Tentative call:** populate
   both; revisit if disk footprint complaints land.

6. **Re-embedding after a provider change.** If a workspace changes
   `embedding_provider` mid-life (e.g., switching from OpenAI to
   local), all existing fingerprint rows have embeddings under the
   old `provider_id`. The K-NN's §16.5.3 fallback handles this
   gracefully (provider-mismatch → structural-only), but the
   workspace effectively loses v2 quality on legacy turns. Should
   there be a `metis patterns reembed` CLI that re-runs the new
   provider over historical user messages? Deferred — manual eviction
   + natural age-out works for v2.

7. **Cross-workspace embeddings.** A single user with multiple
   workspaces has duplicate cached embeddings if their prompts
   overlap. Phase 3+ sync could dedup the cache cross-workspace
   (the cache key is provider+SHA-256, both workspace-independent).
   v2: no, keep workspace-local. Consistent with the §13.5 isolation
   stance.

8. **Async-mode background embed lifecycle.** Under `embedding_strategy="async"`,
   the background embed is `asyncio.create_task(...)` with no explicit
   cancellation. If the process exits before the embed completes, the
   in-flight call leaks. v2 implementation owes a `PatternStore.aclose()`
   that awaits outstanding background tasks; the contract is in
   §16.6.3 (`aclose` added to the close path) but the spec doesn't
   pin the timeout. Tentative: 5s graceful, then cancel.

### 16.12 Decision log additions

| Date       | Decision                                                                  | Rationale                                                                                  |
|------------|---------------------------------------------------------------------------|--------------------------------------------------------------------------------------------|
| 2026-05-14 | v2 spec firmed up; implementation contingent on §A3-rev3 outcome           | Wave 9 needs an implementation-ready v2 if `min_confidence` flip doesn't invert slot 4.    |
| 2026-05-14 | Three concrete providers: OpenAI / Cohere / local sentence-transformers    | Cost + latency + dependency-weight spectrum; buyer with self-host preference picks local.  |
| 2026-05-14 | `embedding_blend_alpha = 0.6` default (embedding-dominant)                 | Structural Jaccard is sparse on non-benchmark turns; embeddings discriminate better there. |
| 2026-05-14 | Cache keyed by `(provider_id, SHA-256(user_message_text))`                 | Same pre-image as v1 structural dedup; survives process restarts; per-provider isolation.  |
| 2026-05-14 | Cache TTL = 180 days; size cap = 10k rows                                  | Mirrors outcomes table caps (§6) so v2's cache aging matches the rest of the store.        |
| 2026-05-14 | `fingerprint_version: v1` is the default; v2 is opt-in per workspace       | Forward-only migration; v1 workspaces never see the v2 code path.                          |
| 2026-05-14 | Schema bumps to `"2"`; new `embedding_cache` table only — no v1 row edits  | v1 readers tolerate `schema_version="2"`; no destructive migration; clean downgrade path.  |
| 2026-05-14 | No new event types; v1's `pattern.recorded.fingerprint_kind` discriminates | Catalog stability; debug-level logging for cache miss/hit, not bus events.                 |
| 2026-05-14 | Mixed-version K-NN falls back to structural-only on missing embedding      | Migration is forward-only and lossless; the K-NN converges to v2 as v1 rows age out.       |
| 2026-05-14 | `embedding_strategy` knob: sync default for agent loop, async for gateway  | Sync is fine at human latency; async is required at gateway-QPS for the 5ms budget.        |

### 16.13 Implementation notes (Wave 10)

The shipped implementation deviates from the spec in a few documented
ways, intentionally:

1. **`PatternConfig.embedding_alpha`** (spec name: `embedding_blend_alpha`).
   Renamed for brevity; field semantics unchanged. The `routing.yaml`
   surface accepts both names if a v3 of the loader re-adds support; v1
   loader only reads the rename. Default `0.6`.

2. **No `embedding_strategy` field.** The sync/async distinction in
   §16.7.3 is collapsed at the routing layer rather than configured on
   the store: `PatternStore.lookup_embedding` is a cache-only sync read,
   and the routing engine's `_attach_cached_embedding` calls it directly
   from slot 4. The recording path is async
   (`attach_embedding_for_recording`) and does call the embedder on
   cache miss. The net effect is the same as `embedding_strategy="async"`
   universally — the K-NN's query-time embedding is only consulted on
   cache hit, never blocking on a network call. Gateway path inherits
   this for free.

3. **`recommend()` stays sync.** Spec §16.6.3 described an async
   `recommend()` future. Because the engine pulls cached embeddings
   itself (point 2), the store's K-NN signature is unchanged and the
   routing engine remains sync.

4. **No NumPy hard dependency.** Vectors round-trip through
   `array.array('f', ...)` — packed float32 with `struct`-level cost.
   §16.11 open question 3 was decided "no NumPy" because the routing
   critical path doesn't compute large matrix ops; the local
   sentence-transformers provider transitively pulls NumPy when
   actually installed (extra `metis-patterns-local`), so users of that
   provider still get NumPy in their environment.

5. **`DeterministicEmbeddingProvider`** ships in `embeddings.py`
   alongside the three production providers. It's the §A3-rev3 caveat
   workaround for tests + fixtures that need byte-deterministic vectors
   without an API key — the SHA-256-derived vectors satisfy the
   Protocol's `runtime_checkable` contract and the cache's
   round-trip invariants.

### 16.14 Migration: upgrading a v1 workspace to v2

Concrete steps for a workspace already on v1:

1. **Pick a provider.** `openai:text-embedding-3-small` is the cheapest
   ($0.02 / 1M tokens); `local:sentence-transformers:all-MiniLM-L6-v2`
   is the self-host option (requires the `metis-patterns-local` extra
   to install `sentence-transformers`).

2. **Edit `<workspace>/.metis/routing.yaml`** to set the v2 fields:

   ```yaml
   pattern:
     fingerprint_version: v2
     embedding_provider: openai:text-embedding-3-small
     # embedding_alpha: 0.6   # default; tune in [0.4, 0.8] per §16.11 q1
   ```

3. **Set the API key** (`OPENAI_API_KEY` or `COHERE_API_KEY`) in the
   environment the agent / gateway / server reads. The local provider
   needs no key but pulls Torch on first use.

4. **Restart the process.** `PatternStore.__init__` reads the config
   once.

5. **No backfill.** The pattern store does not re-embed historical user
   messages — they age out under the 180-day TTL (§6.3). New turns
   write hybrid fingerprints with embedding rows; old structural-only
   rows continue to be queried under the §16.5.3 fallback rule
   (mixed-version K-NN). Embedding cache fills naturally over the
   first ~K turns of each workload (§16.7.2 target: ≥80% hit rate
   within 100 turns).

6. **Downgrade is graceful.** Setting `fingerprint_version: v1` after
   running v2 leaves the v2 rows in place. The K-NN falls back to
   structural-only on every query (§16.6.2). The `embedding_cache`
   table is left on disk and is bounded so it can't grow.

The schema bump `"1" → "2"` is in-place and only updates `store_meta`;
no rows in `fingerprints` / `outcomes` are touched. v1 processes
opening a v2 db tolerate `schema_version="2"` (v1 readers ignore the
unknown `embedding_cache` table; the bump path uses `WHERE value <
excluded.value` so a v1 process never downgrades the version).

---

## 17. Production tuning

**Status:** Drafted 2026-05-15 (Wave 13a-4, sustained-load audit). Findings
from a production-readiness audit of the v1 + v2 pattern store under
multi-tenant pressure. None of the findings invalidate the v1 / v2
semantics; they document where the implementation has measurable
operating bounds.

The sibling Wave 13a-5 audit covers the trace store's analogous bounds
(`docs/operations/trace-performance.md`). Operators wiring `/metrics`
into alerting should follow both.

### 17.1 K-NN query latency curve

The slot 4 scan in `find_k_nearest` is **O(N) in the outcomes table**:
every call walks every row, decodes its `structural_json`, computes
either weighted-Jaccard (v1) or the blended cosine+Jaccard score (v2),
and partial-sorts the top K. v2's per-row cosine adds a dim-sized dot
product in pure Python.

Measured p50 / p95 on a quiet MacBook (SQLite WAL, default
`PRAGMA synchronous=NORMAL`, k=10):

| Outcomes  | v1 p50  | v1 p95  | v2 (384-dim) p50 | v2 (384-dim) p95 |
|-----------|---------|---------|------------------|------------------|
| 200       | 2.0ms   | 3.1ms   | 16.9ms           | 19.6ms           |
| 1,000     | 6.1ms   | 7.1ms   | 47.5ms           | 48.8ms           |
| 5,000     | 6.1ms   | 6.6ms   | 47.5ms           | 50.6ms           |
| 20,000    | 6.4ms   | 7.3ms   | 47.8ms           | 50.4ms           |

**Breakpoint vs the spec's slot-4 latency budget (§2.1.6, ≤3ms at
≤1000 fingerprints):**

- **v1** clears the 3ms budget at 200 outcomes; at 1,000 outcomes the
  scan already takes ~6ms. The slot-4 budget is **exceeded by the
  pattern slot alone at v1's documented scale**; the larger routing
  budget (≤5ms total per `routing-engine.md §2.1.8`) is also exceeded.
- **v2** runs ~7-8× slower than v1 at every size (the cosine dominates).
  A 384-dim embedding (the `local:sentence-transformers` provider) lands
  K-NN p50 at ~47ms; a 1536-dim embedding (the `openai:text-embedding-3-small`
  provider) would scale linearly to ~190ms p50.

**Operator guidance:**

- The K-NN scan is **CPU-bound on Python**, not SQLite. The fix is not
  an index; it is rewriting the inner loop. Phase 4 candidates: emit a
  `__pyx`-compiled cosine helper, fold the `weighted_jaccard` weights
  into the SQL `CASE` projection, or store an extracted-column index
  per structural field. v1 ships without any of these — the documented
  scale targets a single-user laptop, not multi-tenant gateway QPS.
- Workspaces that breach the budget should **lower `hard_cap_rows`**.
  Cutting `hard_cap_rows: 10_000 → 2_500` keeps the steady-state scan
  under 10ms (v1) / 25ms (v2 at 384-dim) at the cost of more aggressive
  eviction. The age-first eviction policy (§6.3) preserves recent
  signal so the tighter cap doesn't lose the differentiator.
- v2 stores can opt out of slot-4 entirely via
  `min_confidence = 1.0` — slot 4 then always emits `not_applicable`
  and slot 7 wins. Useful as a circuit breaker if the K-NN scan is
  observed to dominate request latency at scrape time.

The test `test_knn_latency_smoke_under_500_fingerprints` in
`tests/patterns/test_production_readiness.py` pins a generous bound
(p95 < 100ms at 400 fingerprints) so CI catches a 10× regression. The
tighter bound is operational, not architectural.

### 17.2 Embedding-cache throughput collapse at cap

The v2 embedding cache (§16.4) is keyed by `(provider_id,
SHA-256(user_message_text))` and bounded by `embedding_cache_max_rows`
(default 10,000). The cap is enforced by `_trim_embedding_cache`, which
runs on **every `store_embedding` call**:

1. Age-first delete (`WHERE created_at_us < cutoff`).
2. `SELECT COUNT(*)` over the cache.
3. If over cap: `DELETE FROM embedding_cache WHERE rowid IN (SELECT
   rowid FROM embedding_cache ORDER BY last_used_at_us ASC, use_count
   ASC, created_at_us ASC LIMIT excess)`.

The third query is **O(N log N)** in the cap on every saturated write
— SQLite has to sort the full table to find the LRU row. Measured
write throughput on a quiet MacBook:

| Cap     | Writes before cap | Writes after cap |
|---------|-------------------|------------------|
| 1,000   | 4,800/s           | 1,200/s          |
| 10,000  | 7,000/s           | **~150/s**       |

**Operational consequence.** At 100 active gateways with one
embedding-per-turn each, the per-pod cache saturates inside the first
10k turns (~hours of moderate load). Once saturated, sustained writes
hold throughput at ~150/s, which is a regime where **slot-4 routing
latency on cache miss can spike to seconds** while the writer waits
its turn. The v1 architectural mitigation (cache lookup at routing
time is sync cache-only; the embed-on-miss path is async, §16.13) is
load-bearing — without it, slot 4 would block on the cache write
during steady-state pressure.

**Operator guidance:**

- Each `PatternStore` runs an **independent cache**. A cluster with
  N workspaces gets N × `embedding_cache_max_rows` aggregate vectors.
  Sizing the per-workspace cap to `expected unique turns × 1.2` over
  the workload "vocabulary stabilization window" (≥100 turns per
  §16.7.2) keeps the cache out of the saturated regime.
- The miss-path embed cost dominates the LRU trim cost by ~50× even
  on the hosted-cheapest provider (`openai:text-embedding-3-small` at
  50-150ms / call vs ~6ms LRU trim at 10k cap). So a *bigger* cache
  is always strictly cheaper than the embed it replaces — until disk
  footprint matters (10k × 1536 × 4 = 60 MB per workspace; 100
  workspaces = 6 GB).
- For multi-tenant deployments, **disable v2 at the workspace level**
  on workspaces that haven't asked for it. `PatternConfig.fingerprint_version
  = "v1"` is the default; the gateway path doesn't write patterns at
  all (per `gateway.md §2`) so the gateway never pays this cost.

The test `test_cache_eviction_holds_at_cap_under_sustained_writes`
pins that the cap is enforced; the throughput-curve numbers above are
a measurement, not a CI gate.

### 17.3 Concurrent recording — defense in depth

§11.9 invariant pins "one process writer per workspace." The current
agent-loop architecture honors this by construction: `PatternEventSubscriber`
runs on a single asyncio task on a single event loop, and routing
slot 4's read path runs on the same loop. So in production, `record()`
and `recommend()` calls are naturally serialized.

The previous build of `PatternStore` opened the SQLite connection with
`check_same_thread=False` but **did not guard against concurrent use
of the shared cursor**. A multi-threaded caller (a future worker pool,
a test fixture that shares one store, a misconfigured gateway that
runs the subscriber on a thread pool) would see two failure modes:

- `sqlite3.InterfaceError: bad parameter or other API misuse` when two
  threads interleave statements on the same connection.
- `TypeError: 'NoneType' object is not subscriptable` when one thread's
  `fetchone()` reads `None` because another thread's cursor advanced
  past its result.

Measured failure rate without the lock: ~36% of `record()` calls fail
with 100 concurrent threads × 10 calls each. With the lock added
(Wave 13a-4), the same workload lands 1000/1000 writes with zero errors
in <0.5s.

The fix is a `threading.RLock` wrapping every public method of
`PatternStore`. Under the single-task asyncio architecture the lock
is uncontended; it only serializes when a caller actually crosses
threads. The lock is **defense in depth** — the architectural single-
writer invariant remains the contract.

The test `test_concurrent_record_lands_all_writes` pins this
(100 threads × 10 records, 1000 writes, zero errors).

### 17.4 Retention coordination with `trace-retention.md`

The pattern store and the trace store have **independent retention
policies**:

| Store      | Default retention                    | Mechanism                                  |
|------------|--------------------------------------|--------------------------------------------|
| Trace DB   | 90 days (cutoff, audit-exempt)       | `metis trace prune` (Wave 12a-2)            |
| Pattern DB | 180 days age cap + 5k/10k row caps   | `record()` continuous trim + `evict()`     |

The two stores live in different files (trace at `~/.metis/metis.db`,
patterns at `<workspace>/.metis/patterns.db`) and the sweeps run
against different SQLite handles. **There is no transactional
coordination between them.**

The audit identified three coordination concerns; all resolve as
"working as intended" but operators should know the shape:

1. **Patterns outlive their trace events.** A 180-day-old outcome row
   can reference a `turn_id` whose `route.decided` / `turn.completed`
   events were pruned 90 days ago. The outcome row stays valid for
   K-NN purposes (Welford means + sample counts are self-contained);
   the audit *trail* of how the row got there is gone. If forensics
   requires "show me the sessions backing this recommendation," it
   has to be answered from the pattern store alone (`outcome_score_history`
   carries the `turn_id` → `eval_id` chain) — the trace cross-reference
   is lost.

2. **Late `eval.completed` for a pruned turn is a no-op.** The
   `PatternEventSubscriber._on_eval_completed` handler maintains an
   in-memory `_turn_outcomes: dict[turn_id, (fp_id, model)]` for the
   lifetime of the process. A trace-store prune doesn't affect the
   map. If `metis evaluate` re-runs over a window predating both
   stores' retention and emits a late `eval.completed`, the handler
   logs "unknown turn" and skips. Not a corruption — just a missed
   late-update. This is consistent with the §10.4 idempotence
   contract (a verdict for an unknown turn returns
   `RecordResult(applied=False)`).

3. **GDPR `metis user forget` does not cascade.** The redaction layer
   (`redaction.md` v1) pseudonymizes user-identifying fields in the
   trace DB. The pattern store records `workspace_hash` (per-workspace
   SHA) and `text_sha256(user_message_text)` on the embedding cache
   row — neither field is `user_id` material, so the spec does not
   require the pattern store to participate. In multi-user-per-workspace
   deployments (Phase 3+; out of scope for v1) this would need to be
   revisited.

**Operator guidance:**

- If `metis trace prune --days N` is run with `N < 180`, expect a
  permanent asymmetry: pattern outcomes will be queryable in K-NN
  without a corresponding trace entry. This is correct.
- If a stricter coordination is wanted, set `PatternCaps.max_age_days
  = retention_days` in the pattern-store constructor so the two ages
  match. The runtime accepts a `caps=` kwarg; the helm chart does
  not yet surface it. Phase 4 candidate.

### 17.5 Audit-flag posture (confirmed correct)

| Event             | Audit-flagged? | Survives `trace prune` | Rationale                                                                              |
|-------------------|----------------|------------------------|----------------------------------------------------------------------------------------|
| `pattern.evicted` | **Yes**        | Yes                    | Bounded-store enforcement record. Same shape as `memory.eviction`. Cap pressure is operationally meaningful long after the immediate cause (audit-log.md §4). |
| `pattern.recorded`| No             | Pruned at retention    | Operational telemetry. Recovering "did this row get recorded?" forensically reads `outcomes.last_updated_at_us` + `outcome_score_history` directly.           |
| `pattern.matched` | No             | Pruned at retention    | Operational telemetry. Per-match audit is already in `route.decided.chain[]`, which is *also* pruned — both ages together preserve the audit invariant.       |

The Wave 13a-4 audit confirmed the existing posture is correct. A
spec change would be required to flip `pattern.recorded` or
`pattern.matched`; the test
`test_pattern_recorded_and_matched_are_not_audit_flagged` pins the
current state so a drift causes CI failure.

### 17.6 Observability: `metis_pattern_embedding_cache_hit_ratio`

Wave 13a-4 adds three Prometheus gauges, polled at `/metrics` scrape
time via the new `pattern_cache_getter` on `MetricsCollector`:

| Metric                                          | Type  | Labels         | Source                                                                |
|-------------------------------------------------|-------|----------------|-----------------------------------------------------------------------|
| `metis_pattern_embedding_cache_hit_ratio`       | Gauge | `workspace_id` | `PatternStore.cache_hit_ratio()` — process-local hits/(hits+misses).  |
| `metis_pattern_embedding_cache_hits_total`      | Gauge | `workspace_id` | `PatternStore.cache_hit_count()` — process-local cumulative.          |
| `metis_pattern_embedding_cache_misses_total`    | Gauge | `workspace_id` | `PatternStore.cache_miss_count()` — process-local cumulative.         |

Hits and misses are **process-local** counters reset on `PatternStore`
construction. They are not durable across process restarts; the v2
cache itself is durable (SQLite) but the counters are not. This is
intentional — durable counts are recoverable from the bus event
stream (count `pattern.matched.fingerprint_kind="hybrid"` over a
window) if forensics requires it.

**Alert recipe.** The §16.7.2 target is ≥80% hit ratio within 100 turns
of a workload. Operators should alert when:

- `metis_pattern_embedding_cache_hit_ratio{workspace_id} < 0.5` for
  >5 minutes after the first 100 turns of the workload **and**
- `rate(metis_pattern_embedding_cache_misses_total[5m]) > 0.1` (i.e.
  the workspace is actually using v2).

A sustained low ratio means the cache is undersized (raise
`embedding_cache_max_rows`) or thrashed (lower workspace traffic, or
opt out of v2 with `fingerprint_version=v1`).

---

## 18. References

- `routing-engine.md §4.1`, §5.5, §11.6 — slot 4 ordering, K-NN math,
  open question on pattern-driven tier resolution.
- `event-bus-and-trace-catalog.md §6.5b`, §6.7 — pattern domain
  (`route.overridden`, `pattern.override_dismissed`), memory eviction
  precedent.
- `memory-store.md` — reference shape for a Phase-2 bounded-storage
  spec; this doc mirrors its goals/non-goals/caps/eviction structure.
- `analytics-api.md §4.7` — re-pricing math (`actual_repriced_usd` /
  `baseline_repriced_usd`) is the precedent for preserving
  `pricing_version` on stored cost.
- `canonical-message-format.md §9.1` — SQLite session/message store
  pattern; pattern store mirrors the SQLite-WAL approach.
- `STRATEGY.md §4`, §6.2, §6.6 — pattern store named as the third
  differentiating leg + open question; §6.2 self-hosting buyer profile
  motivates the local-sentence-transformers option in §16.3.
- `benchmark.md` — once the workload suite runs end-to-end, it is the
  validation surface for fingerprint feature weights (§13.3) and the
  §16.10 test 5 cluster-tightening fixture.
- `benchmarks/RESULTS.md §A3-rev2` — the failure case (slot 4 emitting
  `not_applicable` after the Wave 8a unblocks) that motivates the v2
  contract in §16.
- `evaluator.md` (parallel draft) — the upstream source of
  `success_score`; reconcile in Wave 4 (§15).
- [Letta core blocks](https://docs.letta.com/concepts/memory) — the
  bounded-memory peer; pattern store extends the "eviction is a feature"
  stance into routing-decision history.
