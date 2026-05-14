# Pattern Store Specification

**Status:** Draft v1 (specs-only; no implementation yet)
**Last updated:** 2026-05-13

> **v1 scope.** Per-workspace, bounded SQLite-backed store of task fingerprints
> + outcomes that powers routing slot 4 (`PATTERN_RECOMMENDATION`) per
> `routing-engine.md §5.5`. Phase 2.5 wedge. Embedding-provider-agnostic;
> v1 default fingerprint is purely structural (no embeddings). Multi-user
> aggregation is Phase 3+ and explicitly out of scope.

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

**v2 hybrids** add an embedding vector over the user message text, with
the structural features still used as a hard pre-filter (an "auth bug"
embedding shouldn't match a "format JSON" turn even if texts are
superficially similar). Hybrid mode is **not** in v1; the schema admits
it (`Fingerprint.kind`, `embedding`, `embedding_provider`) so v2 lands
as a data-only addition, not a contract change.

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

For v2 hybrid fingerprints, similarity blends structural Jaccard with
cosine over the embedding vector (`α * jaccard_score + (1-α) * cosine`,
`α` configurable). v2 is out of scope here; the weights are listed in
Open Questions §13.

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
    cost_weight = workspace_config.pattern.cost_weight,    # default 0.3
    min_confidence = workspace_config.pattern.min_confidence,  # default 0.3
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
from `routing.yaml` (per `routing-engine.md §5.1`):

```yaml
pattern:
  cost_weight: 0.3
  min_confidence: 0.3
  min_sample_size: 5
```

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

1. **Embedding provider in v2.** When (if) v2 adds embeddings, which
   provider? OpenAI's `text-embedding-3-small` is cheapest; local
   sentence-transformers avoid external dependency but adds a heavy
   binary. The schema admits either; the choice has GTM consequences
   (a buyer who self-hosts everything dislikes external embedding
   API calls).

2. **Embedding cost amortization.** Embedding cost per turn is
   non-zero. Should pattern lookup at routing time embed the fresh
   user message synchronously (latency added to the 5ms routing
   budget) or asynchronously (route this turn without pattern; record
   the fingerprint for next time)? Sync is simpler; async is cheaper.
   Deferred to v2.

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
  cost_weight: 0.3
  min_confidence: 0.3
  min_sample_size: 5
  min_eval_confidence: 0.5      # NEW; default 0.5
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

## 16. References

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
- `STRATEGY.md §4`, §6.6 — pattern store named as the third
  differentiating leg + open question.
- `benchmark.md` — once the workload suite runs end-to-end, it is the
  validation surface for fingerprint feature weights (§13.3).
- `evaluator.md` (parallel draft) — the upstream source of
  `success_score`; reconcile in Wave 4 (§15).
- [Letta core blocks](https://docs.letta.com/concepts/memory) — the
  bounded-memory peer; pattern store extends the "eviction is a feature"
  stance into routing-decision history.
