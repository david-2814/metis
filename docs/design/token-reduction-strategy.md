# Token Reduction Strategy

**Status:** Design doc; sequencing plan pending owner ratification.
**Last updated:** 2026-05-22
**Scope:** Inventory of token-reduction levers beyond what the shipped
provider-prompt-cache work already gives us. Cross-refs existing specs
for already-captured pieces; new specs ([`session-compaction.md`](../specs/session-compaction.md),
[`provider-adapter-contract.md §4.6`](../specs/provider-adapter-contract.md))
cover the novel subsystems landing alongside this doc. Deferred /
research-shaped items get pointers, not new specs.

> **One-line framing.** Provider prompt caching is the largest token-cost
> lever in the agent loop. Metis has captured most of that lever
> ([`context-assembler.md §5.1`](../specs/context-assembler.md), 100% hit
> rate on the benchmark suite, 22.8% aggregate cost reduction Run 2 → Run 3).
> Remaining levers fall into five well-bounded buckets — this doc names
> them, ranks them by impact on Metis's actual workload mix, and sequences
> the work for a solo part-time owner.

> **What this doc is not.** This is not a spec. Contracts (canonical
> types, bus events, adapter protocol) live in `docs/specs/`. This doc is
> the *strategy artifact* — taxonomy, impact ranking, and sequencing.
> Each lever points at either an existing spec, a new spec drafted
> alongside this doc, or a deferral with its own spec when it lands.

---

## 1. The honest framing

**Prompt caching as offered by Anthropic / OpenAI / OpenRouter is a
server-side feature.** The provider caches the KV tensors (attention
keys/values) of a stable prefix on their inference GPUs. A cache hit
means they skip recomputing attention over those tokens — the client
pays ~10% of input rate and latency drops.

That state lives on the provider's hardware. The HTTP API is atomic:
a client cannot say "here's the same prefix as last time, only attend
over the new suffix." **The provider's prompt cache *is* that
mechanism, and only they can implement it.** So:

- For remote API calls, the *only* client-side lever on prompt caching
  is engineering the stable prefix so the provider's cache fires more
  often. Metis already does this end-to-end ([`context-assembler.md §5.1`](../specs/context-assembler.md)
  + [`provider-adapter-contract.md §4.5`](../specs/provider-adapter-contract.md)).
- For local inference, **the engine** owns the KV cache (vLLM's
  Automatic Prefix Caching, SGLang's RadixAttention, llama.cpp's
  `cache_prompt`, LMCache). This is meaningful only if Metis adopts
  a local-inference adapter; see §6.3.

Everything else that gets called a "cache" in this space is a *different
thing* — a response cache, a context-compression cache, a tool-result
cache, an embedding cache, or a routing cache. Each has a different
correctness contract and a different payoff curve. The taxonomy in §3
separates them.

---

## 2. Workload profile

Token spend across the Metis surface (rough magnitudes for sequencing
purposes; exact numbers depend on workload mix):

| Surface | Per-turn input shape | Spend driver | Caching status today |
|---------|---------------------|--------------|---------------------|
| `metis dev` interactive | Grows linearly with turn count; stable prefix + transcript + new turn | **Live spend** — primary driver | Cache fires on all three shipped breakpoints (tools / stable system / rolling history per [`context-assembler.md §3`](../specs/context-assembler.md) v2); the *new turn* still pays full input rate every turn |
| Gateway pass-through | Client-controlled; usually one HTTP request per turn | Variable; client-side conversation history | Provider prompt cache fires when client sends a stable prefix |
| Benchmark suite reruns | 6 workloads × N turns each | In-house, recurring | Cache fires 100% on Run 3 |
| Evaluator backfills | 1 LLM-judge call per turn over a window | Bounded by `BudgetTracker` ($0.10/session, $1/day defaults) | No batching today; sync calls |
| Worker delegations | Short-lived sub-sessions; haiku-on-fast | Mechanical sub-tasks | Provider prompt cache fires per worker prefix |

The remaining unaddressed lever on long `metis dev` sessions is the
**absolute size of the cached transcript**. The §3 v2 rolling
breakpoint keeps the transcript cache-warm at cache-read rate (~10% of
input rate), but a 30-turn transcript still bills ~3k cache-read
tokens per turn. Compaction shortens the cache-read portion, paying a
one-time cache-write to re-establish the now-smaller prefix; net win
on a multi-turn horizon. The new-turn delta still pays full input
rate by construction and is not addressable by any cache.

---

## 3. The taxonomy

Five buckets. Each lever names what it actually reduces, what it costs in
correctness, and where its spec lives.

### 3.1 Caches that bypass the LLM entirely

These replace an LLM call with a stored response.

**Exact-match response cache.** Key on `SHA-256(full canonical request)`
— model + params + system prompt + messages + tools. On hit, return the
stored `CanonicalResponse` and skip the API call. Saves *every* token
(input + output) for that call.

- **Where it pays off:** benchmark reruns, evaluator backfills,
  idempotent retries after transient NETWORK errors.
- **Where it doesn't:** the live agent loop. The suffix grows every
  turn, so the exact-match key never re-hits after turn 1.
- **Correctness:** lossless. Same request → same response.
- **Status:** unspecced; small enough to land alongside the benchmark
  harness without a standalone spec. See §6.1.

**Semantic / embedding-based response cache.** Embed the request,
return a stored response if a stored request is "close enough" by
cosine. (GPTCache, LiteLLM's `semantic` cache.)

- **Where it pays off:** narrowly-bounded FAQ workloads.
- **Correctness:** **changes semantics.** Answers a *different*
  question with an old answer. Wrong shape for an agent loop with
  stateful tool use.
- **Status:** **deferred** indefinitely; called out here so the option
  is not re-discovered later. The pattern store's embedding cache
  ([`embeddings.py`](../../packages/metis/src/metis/core/patterns/embeddings.py))
  is the substrate if this ever becomes useful — for routing, not for
  response caching.

### 3.2 Caches that reduce input tokens (context curation)

These keep the LLM call but make the input shorter. Usually the biggest
wins on long sessions.

**Rolling summary cache (session compaction).** When the message
history grows past a threshold, summarize older turns into a single
compressed block and replace the original span. Cache the summary keyed
by a content hash so re-runs (restarts, benchmark reruns, evaluator
re-evaluations) don't pay summarization cost twice.

- **Where it pays off:** any session past ~20 turns. Largest
  unaddressed lever for live `metis dev`.
- **Correctness:** lossy by design. Operator picks the summarization
  model and the trigger threshold; the agent must have already
  `memory_add`'d durable facts (compaction touches in-session context,
  not cross-session `MEMORY.md`).
- **Status:** drafted in [`session-compaction.md`](../specs/session-compaction.md)
  (alongside this doc). Implementation deferred to Wave 18 per §5.

**RAG over skills (context-assembler v3 skill activation).** When a
workspace accumulates many skills, retrieve the k relevant ones per turn
instead of inlining all of them as cache-floor padding. The embedding
index is the cache.

- **Where it pays off:** skill-heavy workspaces. Near-zero impact on
  workspaces with few skills.
- **Correctness:** lossy if retrieval is wrong. v3 mitigates with
  explicit activation paths.
- **Status:** spec exists at [`context-assembler.md §5.2`](../specs/context-assembler.md)
  with partial scaffolding shipped (`SkillActivationRegistry`,
  per-session budget caps). The retrieval-driven activation path is
  not yet fully wired end-to-end. Owned by context-assembler.md, not
  this doc.

**MEMORY.md / USER.md.** Bounded distilled facts the agent extracts
across sessions, so a follow-up session doesn't re-derive them. The
eviction is the feature.

- **Status:** shipped. Spec at [`memory-store.md`](../specs/memory-store.md).
  Mentioned here for completeness; no new work on this axis.

**Sliding window / hard truncation.** Drop old turns past a threshold
without summarization.

- **Status:** crude fallback if compaction (§3.2) is unavailable.
  Not specced as a primary lever — compaction is strictly better when
  the LLM-call budget allows it. Compaction's hard-cap path
  ([`session-compaction.md §3.1`](../specs/session-compaction.md))
  degrades to truncation when the summarization model itself is
  unavailable.

### 3.3 Caches that amplify the provider's prompt cache

Not caches you own — *tactics* that make the provider's cache fire more
often, which is economically equivalent.

**Two-segment system prompt + minimum-cacheable-prefix padding.**
Stable instructions in segment 1, mutating context in segment 2,
breakpoint between them. Pad segment 1 above the per-model cache floor.

- **Status:** shipped end-to-end. Spec at
  [`context-assembler.md §5.1`](../specs/context-assembler.md). 100% hit
  rate on the benchmark suite. No new work.

**Rolling history breakpoint.** A third breakpoint on the last content
block of the last message brings the entire transcript into the cached
prefix on a rolling basis. Shipped 2026-05-22 per
[`context-assembler.md §3`](../specs/context-assembler.md) v2.

- **Status:** shipped. No new work; informs the
  [`session-compaction.md §7`](../specs/session-compaction.md)
  cost arithmetic (compaction pays a one-time cache-write to
  re-establish the rolling breakpoint over the post-compaction
  prefix).

**Tool-schema stability.** Tool definitions sit in the prefix; if they
wobble across calls, the prefix changes and the cache misses.

- **Status:** unspecced *as a discipline* — it's a habit, not a
  subsystem. Flagged in [`KNOWN_ISSUES.md`](../KNOWN_ISSUES.md) only
  if it surfaces as a regression. No new spec needed.

**OpenRouter explicit-breakpoint emission.**

- **Status:** shipped Wave 17. Spec at
  [`provider-adapter-contract.md §4.5`](../specs/provider-adapter-contract.md).
  No new work.

### 3.4 Caches around tool I/O

**Deterministic tool-result memoization.** Cache the result of
idempotent tool calls — `read_file(path, mtime)`,
`web_fetch(url, ETag)`, idempotent shell commands.

- **Where it pays off:** *tool latency* and *external API cost* (web
  search, expensive subprocess invocation). **It does NOT reduce LLM
  input tokens by itself** — the cached result still gets fed back to
  the model as a `tool_result` block in the next LLM call. The token
  win only materializes if the round-trip is suppressed *before* the
  model sees it, which gets into prompt-rewriting territory.
- **Status:** **deferred.** Listed here so the option isn't
  re-discovered. Worth doing for latency on workloads with expensive
  deterministic tools, but not on the critical token-reduction path
  for the use cases that drive Metis spend today.

### 3.5 Adjacent levers that aren't caches but reduce token cost

Worth naming because they often deliver more than caching does.

**Model routing / delegation.** A haiku call instead of a sonnet call
is a ~5× per-token cost reduction with no cache machinery. Metis's
whole thesis.

- **Status:** shipped. Three independent datapoints from delegation
  (8.3% / 19.9% / 26.1% cost-per-quality improvement). §A3 task-domain
  wedge deferred post-GA per the project status.

**Anthropic Message Batches API (and OpenAI Batches).** 50% discount on
async submission with up-to-24h turnaround. Compatible with workloads
that are *already* async — evaluator backfills, benchmark re-runs.

- **Status:** drafted in
  [`provider-adapter-contract.md §4.6`](../specs/provider-adapter-contract.md)
  (alongside this doc). Implementation deferred to Wave 18 per §5.
  Highest ROI per implementation hour in this list.

**Output bounding.** `max_tokens`, extended-thinking budgets. Output
tokens cost more per token than input on every provider; capping is
sometimes the highest-ROI knob.

- **Status:** infrastructure exists in `CanonicalRequest`; no operator
  guidance / default policy. Mentioned for completeness; not in the
  Wave-18 plan.

---

## 4. Impact ranking

Three tiers. Magnitudes are *order-of-magnitude estimates* against
Metis's current workload mix, not measured numbers — they want
verification once the levers ship.

### Tier 1 — biggest unrealized levers

| Lever | Estimated impact | Workloads | Effort | Spec |
|-------|-----------------|-----------|--------|------|
| Rolling summary cache | 30–60% input-token reduction on sessions past ~20 turns | `metis dev` interactive | Medium (new spec + bus events + heuristic) | [`session-compaction.md`](../specs/session-compaction.md) (new, alongside this doc) |
| Routing improvements (post-GA §A3 task-domain wedge) | 10–25% cost-per-quality on the right workloads | All | Variable; research-shaped | Existing: [`pattern-store.md`](../specs/pattern-store.md), [`delegation.md`](../specs/delegation.md), [`evaluator.md`](../specs/evaluator.md) |

### Tier 2 — large % but narrower workload

| Lever | Estimated impact | Workloads | Effort | Spec |
|-------|-----------------|-----------|--------|------|
| RAG over skills (context-assembler v3) | 5k–10k tokens shaved per call on skill-heavy workspaces; near zero otherwise | Skill-heavy `metis dev` workspaces | Medium; substrate exists | Existing: [`context-assembler.md §5.2`](../specs/context-assembler.md) |
| Batch API for evaluator + benchmarks | Flat 50% on those paths | Evaluator backfills, `scripts/benchmark.py` | Low; trivial adapter additions | [`provider-adapter-contract.md §4.6`](../specs/provider-adapter-contract.md) (new amendment) |

### Tier 3 — already captured or niche

| Lever | Status | Notes |
|-------|--------|-------|
| Provider prompt-cache amplification | Shipped | 100% hit rate on benchmark; residual is discipline |
| Exact-match response cache for benchmarks | Optional | Mostly buys determinism, not dollars |
| Output bounding | Workload-dependent | Needs measurement before ranking |
| Semantic response cache | Deferred indefinitely | Wrong correctness shape for agent loop |
| Tool-result memoization | Deferred | Latency win, not token win without prompt rewriting |
| Local-inference cache | Deferred | Only meaningful with a local-inference adapter |

---

## 5. Sequencing plan

Designed for the solo part-time owner posture. Each wave is sized to be
land-able in one focused stretch without violating the post-GA "no
speculative scope" discipline.

### Wave 18 — batch API + compaction substrate

**Rationale:** Wave 18 is the smallest possible step that captures a
meaningful Tier-2 win (batch API: flat 50% on evaluator + benchmark
paths, near-zero correctness risk, no new bus events) AND lays the
substrate for the Tier-1 compaction work without committing to its full
shape yet.

- **18a-1** — Implement `AdapterCapabilities.supports_batch_api` flag +
  `submit_batch` / `fetch_batch` adapter protocol additions per
  [`provider-adapter-contract.md §4.6`](../specs/provider-adapter-contract.md).
  Anthropic adapter only in this wave; OpenAI in Wave 19.
- **18a-2** — Wire `metis evaluate` to opt into batch when
  `--batch-mode` is set. Default off; evaluator behavior unchanged
  unless flag is passed.
- **18a-3** — Wire `scripts/benchmark.py --batch-mode` for offline
  benchmark reruns. Document in [`benchmark.md`](../specs/benchmark.md).
- **18a-4** — `CompactionCache` SQLite store skeleton +
  `<workspace>/.metis/compaction-cache.sqlite` lifecycle (open / write /
  read / LRU eviction). Schema only; no caller yet.
- **18a-5** — `session.compaction_*` events in `PAYLOAD_REGISTRY`
  per [`session-compaction.md §6`](../specs/session-compaction.md);
  catalog wiring in
  [`event-bus-and-trace-catalog.md §6`](../specs/event-bus-and-trace-catalog.md).
  No emitter yet — wave 19 lights it up.

**Exit criteria:** evaluator batch path measurably 50% cheaper on a
backfill against a known cassette. Compaction substrate exists with no
callers.

#### Wave 18 execution detail

> **Status:** ratified 2026-05-22. The five sub-items below are sized
> for a solo part-time owner, ordered by ROI (capture the 50% batch
> discount on the active spend path first; ship Wave-19 substrate
> last). The alternative "warm-up" order is noted under §5.1.1.5.

##### 5.1.1.1 Suggested order

`18a-1 → 18a-2 → 18a-3 → 18a-4 → 18a-5`.

| # | Item | Size | Depends on | Unblocks |
|---|------|------|------------|----------|
| 1 | 18a-1 — Anthropic batch adapter | **L** | (none) | 18a-2, 18a-3 |
| 2 | 18a-2 — `metis evaluate --batch-mode` | M | 18a-1 | (immediate 50% on evaluator spend) |
| 3 | 18a-3 — `scripts/benchmark.py --batch-mode` | M | 18a-1 | (immediate 50% on benchmark spend) |
| 4 | 18a-4 — `CompactionCache` SQLite store (schema only) | S | (none) | Wave 19 §19a-2 |
| 5 | 18a-5 — `session.compaction_*` events in catalog | XS | (none) | Wave 19 §19a-2 |

**Alternative "warm-up" order:** `18a-5 → 18a-4 → 18a-1 → 18a-2 → 18a-3`.
Two quick wins (XS + S) before the L adapter work — fine if momentum
matters more than ROI timing.

##### 5.1.1.2 Per-item dossier

###### 18a-1 — Anthropic adapter `submit_batch` / `fetch_batch` / `poll_batch`

Touches:

- [`packages/metis/src/metis/core/canonical/`](../../packages/metis/src/metis/core/canonical/) — new
  `BatchHandle`, `BatchStatus`, `BatchResult`, `BatchError` msgspec
  types per [`provider-adapter-contract.md §4.6.2`](../specs/provider-adapter-contract.md).
- The `AdapterCapabilities` struct — add `supports_batch_api: bool = False`.
- The `Usage` struct — add `pricing_mode: Literal["sync", "batch"] | None = None`.
- [`packages/metis/src/metis/core/pricing/`](../../packages/metis/src/metis/core/pricing/) —
  add `ModelPricing.batch_rates: ModelPricing | None = None`.
- [`packages/metis/src/metis/core/adapters/protocol.py`](../../packages/metis/src/metis/core/adapters/protocol.py) —
  three new methods on the `ProviderAdapter` Protocol; default
  implementations raise `NotImplementedError`.
- [`packages/metis/src/metis/core/adapters/anthropic.py`](../../packages/metis/src/metis/core/adapters/anthropic.py) —
  implement against `/v1/messages/batches` per [`provider-adapter-contract.md §4.6.3`](../specs/provider-adapter-contract.md).
  Declare `supports_batch_api=True` for at least one Anthropic model row.
- `packages/metis/tests/core/adapters/test_anthropic_batch.py` (new) —
  cassette-driven round-trip.

Acceptance:

- Cassette test submits 3 requests, polls until `completed`, fetches
  results; result list is same length and order as the submitted
  `requests` list, with `custom_ids` preserved.
- `Usage.pricing_mode="batch"` stamped on every successful result.
- `Usage.cost_usd` matches `ModelPricing.batch_rates` (50% of sync) when
  the row is present; falls back to sync rates with a documented
  WARN log when absent.
- Existing adapter test suite passes unchanged.

Out of scope: OpenAI batch (Wave 19 §19a-4), OpenRouter (not supported
by upstream as of 2026-05-22), gateway pass-through (open question §7.3).

###### 18a-2 — `metis evaluate --batch-mode`

Touches:

- [`packages/metis/src/metis/cli/`](../../packages/metis/src/metis/cli/) —
  `metis evaluate` CLI gains `--batch-mode` + `--collect-batches`
  flags.
- [`packages/metis/src/metis/core/eval/`](../../packages/metis/src/metis/core/eval/) —
  evaluator entry point branches on the flag; submission path persists
  handle; collection path polls + ingests.
- New table `evaluator_batch_handles(custom_id TEXT PRIMARY KEY,
  batch_id TEXT, submitted_at_ms INTEGER, subject_kind TEXT,
  subject_id TEXT, status TEXT)` in the existing trace DB (additive;
  no schema-version bump per [`event-bus-and-trace-catalog.md §7`](../specs/event-bus-and-trace-catalog.md)).
- [`docs/specs/evaluator.md`](../specs/evaluator.md) — new subsection
  documenting `--batch-mode`.
- `packages/metis/tests/core/eval/test_batch_mode.py` (new) —
  submit → poll → fetch → ingest cycle against a fixture.

Acceptance:

- `metis evaluate --batch-mode --subject turn --since <iso>` submits a
  batch, persists handles, exits without waiting.
- `metis evaluate --collect-batches` polls pending handles, ingests
  completed results as `eval.completed` events with
  `pricing_mode="batch"` stamped.
- Verdicts on the test fixture match sync-mode output byte-for-byte.
- A second `--collect-batches` invocation is idempotent (no duplicate
  `eval.completed` events for the same custom_id).

###### 18a-3 — `scripts/benchmark.py --batch-mode`

Touches:

- [`scripts/benchmark.py`](../../scripts/benchmark.py) — `--batch-mode`
  + `--collect-batch <run_id>` flags.
- Persistence: `benchmarks/.runs/<run_id>/batch-handles.jsonl`
  (matches the existing per-run artifact pattern in the benchmark
  harness).
- [`docs/specs/benchmark.md`](../specs/benchmark.md) — new section
  documenting batch mode + the two-pass invocation.

Acceptance:

- `scripts/benchmark.py --batch-mode --workload fix-a-bug-small` submits
  the workload, persists handles, exits.
- `scripts/benchmark.py --collect-batch <run_id>` finalizes the run
  with results loaded from the upstream.
- A new entry in [`benchmarks/RESULTS.md`](../../benchmarks/RESULTS.md)
  documents the cost-per-quality reduction vs sync mode on the same
  workload (~50% expected).

###### 18a-4 — `CompactionCache` SQLite store (schema only)

Touches:

- `packages/metis/src/metis/core/sessions/compaction_cache.py` (new
  module) — class `CompactionCache`, `open(path) -> CompactionCache`,
  `read(cache_key) -> CompactionRow | None`,
  `write(cache_key, summary_text, ...) -> None`,
  `evict_lru(max_rows) -> int`. Schema per
  [`session-compaction.md §5.2`](../specs/session-compaction.md).
  Concurrency via `threading.RLock()` per [`pattern-store.md §17`](../specs/pattern-store.md).
- `packages/metis/tests/core/sessions/test_compaction_cache.py` (new)
  — schema creation, CRUD, LRU eviction, concurrency under hostile
  thread contention.

Acceptance:

- Module imports cleanly; `open()` creates the SQLite file at the
  passed path with the schema in `session-compaction.md §5.2`.
- Read / write / LRU eviction tests pass.
- `compaction_cache_max_rows` default 1000 honored.
- **No caller wiring in this sub-item** — the store exists but is not
  consumed. Wave 19 §19a-2 lights it up.

###### 18a-5 — `session.compaction_*` events in catalog

Touches:

- [`packages/metis/src/metis/core/events/payloads.py`](../../packages/metis/src/metis/core/events/payloads.py) —
  three new `msgspec.Struct(frozen=True)` payload types per
  [`session-compaction.md §6`](../specs/session-compaction.md); three
  new entries in `PAYLOAD_REGISTRY` with `Sensitivity.PSEUDONYMOUS`.
- [`docs/specs/event-bus-and-trace-catalog.md §6`](../specs/event-bus-and-trace-catalog.md) —
  add three new payload rows under the `session.*` family.
- `packages/metis/tests/core/events/test_payloads.py` — round-trip +
  catalog membership tests.

Acceptance:

- `PAYLOAD_REGISTRY["session.compaction_started"]` /
  `["session.compaction_completed"]` / `["session.compaction_failed"]`
  resolve to the new payload structs with `PSEUDONYMOUS` sensitivity.
- Catalog spec carries three new rows under §6 with payload schemas
  matching `session-compaction.md §6`.
- Round-trip tests pass for all three.
- **No emitter wiring in this sub-item** — the catalog entry exists
  but is not produced. Wave 19 §19a-2 lights it up.

##### 5.1.1.3 Test-count delta target

Approximate per-item additions:

| Item | Estimated new tests |
|------|---------------------|
| 18a-1 | ~20 (round-trip, cost mapping, capability flag, error surfaces, expired-batch fallback) |
| 18a-2 | ~6 (CLI flag, submit + collect cycle, idempotency) |
| 18a-3 | ~4 (CLI flag, handle persistence, two-pass invocation) |
| 18a-4 | ~8 (schema, CRUD, LRU, concurrency) |
| 18a-5 | ~6 (3 payload types × round-trip + 3 registry-membership) |

Total: ~44 new tests. Current suite is **1865**; target after Wave 18:
**~1909**. (Per [`AGENTS.md`](../../AGENTS.md), each wave bumps the test
count line in `AGENTS.md` + `README.md` at close.)

##### 5.1.1.4 Doc-sync checklist (at Wave 18 close)

- [ ] [`AGENTS.md`](../../AGENTS.md) (which `CLAUDE.md` symlinks to) —
  status sentence extends through "Wave 18 reaches the
  batch-API + compaction-substrate milestone."
- [ ] [`AGENTS.md`](../../AGENTS.md) "What works" — one new entry
  ("Async batch submission (Wave 18)" or similar) covering 18a-1
  through 18a-3 + a separate entry covering 18a-4 + 18a-5.
- [ ] [`README.md`](../../README.md) — test count line bumped
  `1865 → ~1909`.
- [ ] [`docs/specs/CHANGES.md`](../specs/CHANGES.md) — the two
  `pending review` entries for `session-compaction.md` v1 and
  `provider-adapter-contract.md §4.6` move to `verified`.
- [ ] [`docs/specs/event-bus-and-trace-catalog.md §6`](../specs/event-bus-and-trace-catalog.md) —
  three new `session.compaction_*` rows.
- [ ] [`docs/specs/evaluator.md`](../specs/evaluator.md) — new section
  documenting `--batch-mode`.
- [ ] [`docs/specs/benchmark.md`](../specs/benchmark.md) — new section
  documenting `--batch-mode`.
- [ ] [`benchmarks/RESULTS.md`](../../benchmarks/RESULTS.md) — new
  §Wave-18 entry with measured cost-per-quality reduction on at least
  one workload.

##### 5.1.1.5 Risks + mitigations

| Risk | Mitigation |
|------|------------|
| Anthropic Batches API SLA variability ("best-effort 24h" can exceed 24h). | 18a-2's CLI exits without waiting; handles persist; user collects on a later invocation. No process holds a handle in memory. |
| `Usage.pricing_mode` analytics queries against pre-§4.6 trace rows (`NULL` values). | Every `/analytics/cost?group_by=pricing_mode` consumer treats `NULL` as `"sync"` (documented pre-§4.6 default). Test explicitly in 18a-1. |
| 18a-4 / 18a-5 ship as dead code until Wave 19. | Both items carry test coverage that exercises the substrate independently. `session-compaction.md` references both, so the dead-code-discovery path is documented. Plan to land Wave 19 within one quarter of Wave 18; if Wave 19 slips beyond that, fold 18a-4 / 18a-5 into Wave 19 instead. |
| Cassette discipline drift (Anthropic Batches API responses captured at one point in time may not replay if upstream wire shape changes). | Treat the batch cassette like the existing sync-mode cassettes per [`provider-adapter-contract.md §10.3`](../specs/provider-adapter-contract.md) — re-record on every adapter-touching PR; flag wire-shape changes in the PR description. |
| Anthropic batch-rate pricing not in registry. | 18a-1 reads `ModelPricing.batch_rates`; if absent, logs WARN and falls back to sync rates (correctness preserved; savings missed). Pricing-table update lands with 18a-1 so the fallback is exercised by tests only, not production. |

##### 5.1.1.7 Parallel agent dispatch

Wave 18 parallelizes across **two phases**, each running multiple agents
concurrently. Verbatim agent prompts live in
[`docs/design/wave-18-agent-dispatch.md`](wave-18-agent-dispatch.md) —
copy them into separate Claude Code sessions, or pass them to the
`Agent` tool with `isolation: "worktree"`.

###### Phase structure

```
Phase 1  (3 agents in parallel; no inter-dependencies):
  ├── Agent A  →  18a-1 (L)   Anthropic batch adapter
  ├── Agent B  →  18a-4 (S)   CompactionCache SQLite store
  └── Agent C  →  18a-5 (XS)  session.compaction_* events in catalog

   [user reviews + merges A's PR — unblocks Phase 2]

Phase 2  (2 agents in parallel; both depend on Agent A's merge):
  ├── Agent D  →  18a-2 (M)   metis evaluate --batch-mode
  └── Agent E  →  18a-3 (M)   scripts/benchmark.py --batch-mode
```

The critical path is `18a-1 + max(18a-2, 18a-3) = L + M`. Compared to
the fully-serial `L + M + M + S + XS`, parallelism saves `M + S + XS`
of wall clock.

###### Conflict-free file partition (verified 2026-05-22)

The five sub-items touch **disjoint** file sets:

| Sub-item | Modifies | Creates |
|----------|----------|---------|
| **18a-1** | `core/canonical/capabilities.py`, `core/canonical/messages.py`, `core/pricing/table.py`, `core/adapters/protocol.py`, `core/adapters/anthropic.py` | `core/canonical/batch.py`, `tests/core/adapters/test_anthropic_batch.py` |
| **18a-2** *(Phase 2)* | `cli/main.py`, `core/eval/cli.py`, `core/eval/subscriber.py`, `docs/specs/evaluator.md` | `tests/core/eval/test_batch_mode.py` |
| **18a-3** *(Phase 2)* | `scripts/benchmark.py`, `docs/specs/benchmark.md` | — |
| **18a-4** | — | `core/sessions/compaction_cache.py`, `tests/core/sessions/test_compaction_cache.py` |
| **18a-5** | `core/events/payloads.py`, `tests/core/events/test_payloads.py`, `docs/specs/event-bus-and-trace-catalog.md` | — |

Phase 1 agents (A, B, C) touch disjoint files: A is in `canonical/` +
`adapters/` + `pricing/`, B is in `sessions/`, C is in `events/`. No
shared files across Phase 1.

Phase 2 agents (D, E) touch disjoint files: D is in `cli/` + `core/eval/`,
E is in `scripts/`. No shared files across Phase 2.

###### Branching discipline

Per the project's branch-off-`origin/main` convention, each agent MUST:

```bash
git fetch origin main
git checkout -b wave-18a-<N> origin/main
```

NOT `git checkout -b … HEAD`. Agents started after the umbrella PR
merges (which carries the spec drafts in `docs/specs/` +
`docs/design/`) will see those drafts in `origin/main` and can read
them locally.

###### Coordination workflow

1. **Spawn Phase 1**: Agents A, B, C in parallel (any order, separate
   worktrees / sessions).
2. **Each Phase-1 agent halts at PR.** Do not auto-merge.
3. **User reviews + merges all three PRs** (any order; no conflicts
   between them by §5.1.1.7's partition).
4. **Spawn Phase 2**: once A's PR is merged to `origin/main`, spawn
   Agents D and E in parallel. Both branch off the new
   `origin/main` (which now carries the 18a-1 adapter additions).
5. **Each Phase-2 agent halts at PR.** User reviews + merges.
6. **Wave 18 close**: user runs the §5.1.1.4 doc-sync checklist.

###### Agent tool vs separate Claude Code sessions

Either deployment works; the prompts are identical:

- **`Agent` tool with `isolation: "worktree"`** — orchestrator session
  spawns three Phase-1 agents in one response (parallel tool calls);
  the harness creates a temporary worktree per agent. Failure isolation
  is automatic; cleanup is automatic if no changes land. Best for an
  experienced orchestrator who wants to watch all three finish in one
  session.
- **Separate Claude Code sessions** — open three terminals, create
  three `git worktree`s manually, paste the prompt into each session.
  Best when the agents need different model selections or different
  permission modes, or when you want each session to be
  independently inspectable.

##### 5.1.1.6 Out of scope for Wave 18

Recorded here so the wave boundary is explicit:

- **OpenAI batch adapter.** Wave 19 §19a-4.
- **OpenRouter batch.** Not supported upstream as of 2026-05-22; no
  Metis-side work needed.
- **Gateway pass-through of batch endpoints.** Open question §7.3;
  needs its own gateway-spec amendment if added.
- **Compaction caller wiring.** Wave 19 §19a-1 + §19a-2.
- **TUI surfacing of compaction or batch events.** Wave 19 §19a-3
  (compaction); batch is a non-interactive surface so no TUI work.
- **§A3 task-domain wedge.** Wave 20+, post-GA, owner-discretionary.

### Wave 19 — compaction end-to-end + OpenAI batch

**Rationale:** Compaction is the biggest unrealized lever; ship it
end-to-end against `metis dev`. OpenAI batch is the obvious follow-on to
Wave 18 to capture the same 50% discount for non-Anthropic workloads.

- **19a-1** — `Compactor` class + summarization-prompt versioning
  ([`session-compaction.md §4`](../specs/session-compaction.md)).
- **19a-2** — `SessionManager.turn_start` trigger
  ([`session-compaction.md §3.1`](../specs/session-compaction.md)),
  including the cache-key hash, cache hit/miss read path, and the
  `session.compaction_*` event emission wired in 18a-5.
- **19a-3** — TUI surfacing: a single status line when compaction
  fires; cost + token deltas visible via `/cost`. Optional REPL.
- **19a-4** — OpenAI adapter `submit_batch` / `fetch_batch`
  ([`provider-adapter-contract.md §4.6`](../specs/provider-adapter-contract.md)).
- **19a-5** — Measure: pick one long-session benchmark (or add one),
  compare pre- vs post-compaction input-token totals.

**Exit criteria:** A `metis dev` session that exceeds the compaction
threshold runs compaction at least once, with cache hits demonstrable
across a restart. Measurement documented in
[`benchmarks/RESULTS.md`](../../benchmarks/RESULTS.md).

### Wave 20+ — discretionary

- **§A3 task-domain wedge** — post-GA, only if owner re-prioritizes.
  Existing specs unchanged. Cross-ref:
  [`pattern-store.md`](../specs/pattern-store.md),
  [`delegation.md`](../specs/delegation.md).
- **Context-assembler v3 skill retrieval** — wire the retrieval-driven
  activation path when a workspace shape arrives that justifies it.
  Existing spec at [`context-assembler.md §5.2`](../specs/context-assembler.md).
- **Output bounding policy** — measurement-driven. Not specced yet.
- **Local-inference adapter** — only if a buyer-side use case
  surfaces. Would slot in alongside Anthropic / OpenAI / OpenRouter
  per [`provider-adapter-contract.md`](../specs/provider-adapter-contract.md).
  Not on the roadmap.

---

## 6. Deferrals and rationale

### 6.1 Exact-match response cache (deferred to "alongside the consumer")

Worth building, but **not as its own subsystem**. The two consumers
(benchmark harness, evaluator) can each carry a small SQLite-keyed cache
inside their own runner without inventing a generic cross-cutting
abstraction. Land them when the corresponding wave (`scripts/benchmark.py`
restructuring or `metis evaluate` extension) needs determinism.

### 6.2 Semantic response cache (deferred indefinitely)

Changes correctness semantics; wrong shape for the agent loop. Re-open
only if a clearly-bounded FAQ-style consumer surfaces (unlikely for
Metis's positioning).

### 6.3 Local-inference adapter (deferred indefinitely)

Real prefix caching locally is a solved problem in the inference engine
ecosystem (vLLM, SGLang, llama.cpp/Ollama, TensorRT-LLM, TGI, LMDeploy,
MLC-LLM; dedicated KV layers like LMCache and NVIDIA Dynamo). All of
these expose OpenAI-compatible endpoints, so pointing the OpenAI adapter
at one via `OPENAI_BASE_URL` works today without a new adapter — but the
open-weights model substitution carries quality trade-offs that are
upstream of caching. Re-open only if a deployment substitutes
open-weights models for cost reasons; not on the roadmap.

### 6.4 Tool-result memoization (deferred)

Latency-shaped, not token-shaped. Worth doing once a workload arrives
where it materially shifts the cost-per-quality curve (large
deterministic web fetches, expensive subprocess invocations). Not on the
Wave-18 / Wave-19 path.

---

## 7. Open questions

1. **Compaction model choice.** Default is haiku, but a workload that
   needs higher-fidelity summarization may want sonnet. Should this be a
   per-workspace config or a per-session knob? See
   [`session-compaction.md §9 — open questions`](../specs/session-compaction.md).
2. **Compaction in the gateway.** The gateway is per-request stateless;
   it does not own a session. Compaction is therefore a `metis dev` /
   `metis serve` concern, not a gateway concern — confirm during Wave 19
   implementation that no gateway-side surface needs to be considered.
3. **Batch API in the gateway.** Same per-request statelessness applies.
   Wave 18 wires batch into the adapter; gateway callers would need to
   route their own batch requests through the upstream directly, not
   through Metis. Decide in Wave 18 whether to expose
   `POST /v1/messages/batches` and `POST /v1/batches` on the gateway as
   pass-through, or leave it out.
4. **Compaction interaction with delegation.** Worker sessions are
   short-lived and unlikely to hit the compaction threshold, but the
   contract should be explicit. See
   [`session-compaction.md §8 — interaction with delegation`](../specs/session-compaction.md).

---

## 8. References

- [`context-assembler.md`](../specs/context-assembler.md) — prompt-cache
  breakpoint placement (v1), minimum-cacheable-prefix padding (v2),
  skill activation (v3).
- [`provider-adapter-contract.md §4.5`](../specs/provider-adapter-contract.md)
  — OpenRouter prompt caching, the closest analog to §4.6 (batch API).
- [`pattern-store.md`](../specs/pattern-store.md),
  [`delegation.md`](../specs/delegation.md),
  [`evaluator.md`](../specs/evaluator.md) — routing levers.
- [`benchmarks/RESULTS.md`](../../benchmarks/RESULTS.md) — measured
  baseline for evaluating each lever.
- [`session-compaction.md`](../specs/session-compaction.md) — new spec
  alongside this doc.
- [`provider-adapter-contract.md §4.6`](../specs/provider-adapter-contract.md)
  — new amendment alongside this doc.

---

## 9. Decision log

- **2026-05-22.** Owner ratified umbrella + new-specs format. Tier-3
  items captured as pointers, not new specs. Wave 18 sized for batch
  API + compaction substrate; Wave 19 for compaction end-to-end + OpenAI
  batch.
