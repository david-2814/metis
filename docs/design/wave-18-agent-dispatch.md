# Wave 18 — Agent Dispatch Prompts

**Status:** Drafted 2026-05-22 alongside [`token-reduction-strategy.md §5.1.1.7`](token-reduction-strategy.md).
**Purpose:** Five drop-in agent prompts for parallel execution of Wave 18.
**Phase structure:** 3 agents in Phase 1, 2 agents in Phase 2 (see §5.1.1.7 of the umbrella).

> **How to use this file.** Each `## Agent X` section below is a
> verbatim prompt. Copy it whole, paste into a fresh Claude Code
> session (or pass to the `Agent` tool with `isolation: "worktree"`),
> and wait for the agent to halt at PR. Do not merge until you have
> reviewed.

---

## Common preamble (applies to all agents)

Every agent prompt below assumes the agent will:

1. **Branch off `origin/main`**, not local HEAD:
   ```bash
   git fetch origin main
   git checkout -b <branch> origin/main
   ```
2. **Run the full test suite + ruff** before opening the PR:
   ```bash
   uv run pytest
   uv run ruff check packages apps scripts
   uv run ruff format packages apps scripts
   ```
3. **Halt at PR** — do NOT auto-merge, do NOT start any other
   sub-item, do NOT modify files outside the assigned partition.
4. **Test count target**: each item has a target delta in
   §5.1.1.3 of the umbrella; aim within ±25% of target.
5. **PR body** should reference the umbrella anchor and the assigned
   sub-item id.

---

# Phase 1 (parallel — no inter-dependencies)

## Agent A — Wave 18a-1: Anthropic batch adapter

**Branch:** `wave-18a-1`
**Size:** L (~3–5 sittings of focused work)
**Phase:** 1
**Dependencies:** none
**Unblocks:** Phase 2 (Agents D, E)

### Read first

1. [`docs/specs/provider-adapter-contract.md §4.6`](../specs/provider-adapter-contract.md) — the contract you are implementing. §4.6.1 through §4.6.7 inclusive.
2. [`docs/specs/provider-adapter-contract.md §4.5`](../specs/provider-adapter-contract.md) — the OpenRouter prompt-caching section is the closest existing analog for §4.6 work; mimic its style.
3. [`docs/design/token-reduction-strategy.md §5.1.1.2 → 18a-1`](token-reduction-strategy.md) — acceptance criteria.
4. Skim [`packages/metis/src/metis/core/adapters/anthropic.py`](../../packages/metis/src/metis/core/adapters/anthropic.py) (current sync adapter — your batch additions live alongside it, NOT as a new file).

### Touches

**Modify:**

- [`packages/metis/src/metis/core/canonical/capabilities.py`](../../packages/metis/src/metis/core/canonical/capabilities.py) — `AdapterCapabilities`: add `supports_batch_api: bool = False`.
- [`packages/metis/src/metis/core/canonical/messages.py`](../../packages/metis/src/metis/core/canonical/messages.py) — `Usage`: add `pricing_mode: Literal["sync", "batch"] | None = None`. Update the disjoint-bucket docstring to mention the new field is orthogonal to the three input-token buckets.
- [`packages/metis/src/metis/core/pricing/table.py`](../../packages/metis/src/metis/core/pricing/table.py) — `ModelPricing`: add `batch_rates: ModelPricing | None = None`. When set, batch-mode calls cost off this row instead of the sync row.
- [`packages/metis/src/metis/core/adapters/protocol.py`](../../packages/metis/src/metis/core/adapters/protocol.py) — add three methods to `ProviderAdapter` Protocol: `submit_batch`, `fetch_batch`, `poll_batch`. Each has a default implementation in the base that raises `NotImplementedError` so existing OpenAI / OpenRouter adapters still satisfy the Protocol without code changes.
- [`packages/metis/src/metis/core/adapters/anthropic.py`](../../packages/metis/src/metis/core/adapters/anthropic.py) — implement the three methods against `POST /v1/messages/batches`, `GET /v1/messages/batches/{id}`, `GET /v1/messages/batches/{id}/results`. Declare `supports_batch_api=True` for at least the `anthropic:claude-haiku-4-5` row in the model registry.

**Create:**

- `packages/metis/src/metis/core/canonical/batch.py` (new file) — house the new types: `BatchHandle`, `BatchStatus` (literal), `BatchResult` (union), `BatchError`. Keep them in `msgspec.Struct(frozen=True)` style consistent with the rest of `canonical/`. Re-export from `canonical/__init__.py`.
- `packages/metis/tests/core/adapters/test_anthropic_batch.py` (new file) — cassette-driven round-trip. Cassette lives at `packages/metis/tests/cassettes/anthropic_batch_*.yaml` (or similar — match whatever cassette convention `test_anthropic_adapter.py` uses).

### Acceptance criteria

- [ ] A cassette test submits 3 `CanonicalRequest`s, polls `poll_batch` until `BatchStatus="completed"`, fetches via `fetch_batch`. The returned list is same length and same order as the input, with `custom_ids` preserved.
- [ ] At least one `anthropic:*` model row in the registry declares `supports_batch_api=True`.
- [ ] `Usage.pricing_mode="batch"` is stamped on every successful result.
- [ ] `Usage.cost_usd` matches `ModelPricing.batch_rates` (50% of sync) when present; when absent, the adapter logs a single WARN line and falls back to sync rates (correctness preserved, savings lost).
- [ ] Expired batches (24h elapsed) surface as `BatchError(error_class=ErrorClass.PROVIDER_TRANSIENT, retryable=True)` per `custom_id`.
- [ ] All existing adapter tests in `packages/metis/tests/core/adapters/` pass unchanged.
- [ ] `uv run mypy packages/metis/src/metis/core` clean.
- [ ] Test-count target: **+20 ± 5** new tests in `test_anthropic_batch.py`.

### Implementation gotchas

- **Do NOT use `BaseHTTPMiddleware`-style wrappers.** Batch adapter code goes through the standard `httpx.AsyncClient` path; no middleware involved.
- **Cassette recording.** Use a $0.50-budget real batch against `claude-haiku-4-5` once, then check the cassette in. Don't record live API in CI.
- **`Usage.pricing_mode` is Optional.** Pre-§4.6 trace rows have `NULL`; analytics consumers (out of scope for 18a-1) will treat `NULL` as `"sync"`. Your job is to stamp `"batch"` correctly; don't worry about back-compat queries.
- **Failed entries inside a successful batch surface as `BatchError`, not raises.** The list returned by `fetch_batch` is `list[BatchResult]` where `BatchResult = CanonicalResponse | BatchError`. Only batch-level failures (entire batch failed before any results) raise `AdapterError`.
- **Don't wire CLI yet.** `metis evaluate --batch-mode` (18a-2) and `scripts/benchmark.py --batch-mode` (18a-3) consume your adapter additions in Phase 2 — they are out of scope for this PR.
- **Re-export discipline.** Add `BatchHandle` etc. to `canonical/__init__.py` so callers can `from metis.core.canonical import BatchHandle` consistently with the rest of canonical/.

### Completion

```bash
uv run pytest                                                # full suite passes
uv run ruff check packages apps scripts                      # lint clean
uv run ruff format packages apps scripts                     # auto-format
git add -A
git commit -m "feat(adapters): Anthropic batch submission (Wave 18a-1)

Implements docs/specs/provider-adapter-contract.md §4.6 for the
Anthropic adapter. New canonical types (BatchHandle, BatchStatus,
BatchResult, BatchError) + Usage.pricing_mode + ModelPricing.batch_rates
+ three new ProviderAdapter Protocol methods (default
NotImplementedError) + Anthropic implementation against
/v1/messages/batches. Unblocks Wave 18a-2 / 18a-3.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
git push -u origin wave-18a-1
gh pr create --title "feat(adapters): Anthropic batch submission (Wave 18a-1)" \
             --base main \
             --body "Implements [provider-adapter-contract.md §4.6](docs/specs/provider-adapter-contract.md) per the Wave-18 dispatch plan in [token-reduction-strategy.md §5.1.1.2](docs/design/token-reduction-strategy.md). Unblocks Wave 18a-2 / 18a-3.

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
```

HALT — do not start any other sub-item.

---

## Agent B — Wave 18a-4: CompactionCache SQLite store (schema only)

**Branch:** `wave-18a-4`
**Size:** S (~1–2 sittings)
**Phase:** 1
**Dependencies:** none
**Unblocks:** Wave 19 §19a-2

### Read first

1. [`docs/specs/session-compaction.md §5`](../specs/session-compaction.md) — full storage section (§5.1 / §5.2 / §5.3 / §5.4) is the spec for this sub-item.
2. [`docs/specs/pattern-store.md §17`](../specs/pattern-store.md) — concurrency pattern (`threading.RLock()`) you are reusing.
3. Skim [`packages/metis/src/metis/core/sessions/sqlite_store.py`](../../packages/metis/src/metis/core/sessions/sqlite_store.py) — closest analog for the SQLite-store-in-sessions pattern.

### Touches

**Create:**

- `packages/metis/src/metis/core/sessions/compaction_cache.py` (new file) — class `CompactionCache` with the API per `session-compaction.md §5.2`:
  ```python
  class CompactionCache:
      def __init__(self, path: Path, *, max_rows: int = 1000) -> None: ...
      def read(self, cache_key: str) -> CompactionRow | None: ...
      def write(self, cache_key: str, summary_text: str, *,
                summarization_model: str,
                summarization_prompt_version: str,
                span_message_count: int,
                span_token_count_in: int,
                span_token_count_out: int) -> None: ...
      def evict_lru(self) -> int: ...
      def close(self) -> None: ...
  ```
- `packages/metis/tests/core/sessions/test_compaction_cache.py` (new file).

### Acceptance criteria

- [ ] Module imports cleanly: `from metis.core.sessions.compaction_cache import CompactionCache`.
- [ ] `CompactionCache(path)` creates the SQLite file at `path` with the schema in [`session-compaction.md §5.2`](../specs/session-compaction.md) (exact columns, exact PRIMARY KEY, exact index).
- [ ] `write()` then `read()` round-trips a row; second `read()` updates `last_read_at_ms` + increments `use_count`.
- [ ] `evict_lru()` keeps at most `max_rows` rows, evicting the one with the oldest `last_read_at_ms` first.
- [ ] `threading.RLock()` wraps every public method (mimic [`pattern-store.md §17`](../specs/pattern-store.md)).
- [ ] Concurrency test: 100 threads × 10 writes each, all succeed, no `sqlite3.InterfaceError`.
- [ ] **No caller wiring** — `SessionManager` does not call `CompactionCache` in this PR. The store exists but is unused. Wave 19 §19a-2 will wire it.
- [ ] Test-count target: **+8 ± 2** new tests in `test_compaction_cache.py`.

### Implementation gotchas

- **No `numpy` dependency.** `session-compaction.md §5.2` doesn't need vector storage; keep dependencies minimal.
- **WAL mode + NORMAL sync** (mirror `pattern-store.md §17` and `trace-store`'s `wal_autocheckpoint`). Don't roll your own pragma config — copy the pattern.
- **Path discipline.** The `path` argument is the *file* path, not a workspace path. The caller (Wave 19) will pass `<workspace>/.metis/compaction-cache.sqlite`; this sub-item is path-agnostic.
- **No event emission.** The cache is a passive store — it does not emit `session.compaction_*` events. Agent C (18a-5) defines those event payloads; the emitter is wired by Wave 19 §19a-2.

### Completion

```bash
uv run pytest packages/metis/tests/core/sessions/ -x
uv run pytest                                                # full suite
uv run ruff check packages apps scripts
uv run ruff format packages apps scripts
git add -A
git commit -m "feat(sessions): CompactionCache SQLite store (Wave 18a-4)

Schema-only SQLite store for the rolling-summary cache per
session-compaction.md §5. threading.RLock concurrency; LRU eviction;
1000-row default cap. No caller wiring — Wave 19 §19a-2 lights it up.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
git push -u origin wave-18a-4
gh pr create --title "feat(sessions): CompactionCache SQLite store (Wave 18a-4)" \
             --base main \
             --body "Implements [session-compaction.md §5](docs/specs/session-compaction.md) per the Wave-18 dispatch plan in [token-reduction-strategy.md §5.1.1.2](docs/design/token-reduction-strategy.md). Schema only; no caller wiring (Wave 19 §19a-2 will wire SessionManager).

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
```

HALT — do not start any other sub-item.

---

## Agent C — Wave 18a-5: `session.compaction_*` events in catalog

**Branch:** `wave-18a-5`
**Size:** XS (~1 sitting)
**Phase:** 1
**Dependencies:** none
**Unblocks:** Wave 19 §19a-2

### Read first

1. [`docs/specs/session-compaction.md §6`](../specs/session-compaction.md) — the payload schemas you are registering.
2. [`docs/specs/event-bus-and-trace-catalog.md §6`](../specs/event-bus-and-trace-catalog.md) — where the new rows land. Look at the `session.*` family entries for shape.
3. [`packages/metis/src/metis/core/events/payloads.py`](../../packages/metis/src/metis/core/events/payloads.py) — the `PAYLOAD_REGISTRY` (last ~80 lines) and existing `session.*` payload structs for style.

### Touches

**Modify:**

- [`packages/metis/src/metis/core/events/payloads.py`](../../packages/metis/src/metis/core/events/payloads.py) — three new `msgspec.Struct(frozen=True)` payload classes per [`session-compaction.md §6`](../specs/session-compaction.md):
  - `SessionCompactionStarted` — `{session_id, turn_id, watermark_before, span_message_count, span_token_count_in, threshold_or_hard_cap}`
  - `SessionCompactionCompleted` — `{session_id, turn_id, watermark_before, watermark_after, span_message_count, span_token_count_in, span_token_count_out, cache_hit: bool, cache_key, cost_usd: Decimal, latency_ms}`
  - `SessionCompactionFailed` — `{session_id, turn_id, watermark_before, span_message_count, failure_mode: Literal["adapter_error", "no_valid_boundary", "budget_exhausted_forced", "validation_failed"], error_class: ErrorClass | None, error_message: str | None, truncated_message_count: int | None}`
  - Add to `PAYLOAD_REGISTRY` with `Sensitivity.PSEUDONYMOUS` for all three.
- [`packages/metis/tests/core/events/test_payloads.py`](../../packages/metis/tests/core/events/test_payloads.py) — round-trip tests for each new payload + catalog-membership tests.
- [`docs/specs/event-bus-and-trace-catalog.md §6`](../specs/event-bus-and-trace-catalog.md) — three new rows under the `session.*` family. Mirror the style of the existing `session.created` / `session.resumed` / `session.ended` entries.

### Acceptance criteria

- [ ] `PAYLOAD_REGISTRY["session.compaction_started"]` resolves to `(SessionCompactionStarted, Sensitivity.PSEUDONYMOUS)`.
- [ ] Same for `session.compaction_completed` and `session.compaction_failed`.
- [ ] Round-trip tests pass: a `make_event(type=..., payload=struct)` followed by JSON encode + decode produces the same struct.
- [ ] `event-bus-and-trace-catalog.md §6` carries three new payload rows with full schemas.
- [ ] **No emitter wiring** — nothing in `SessionManager` produces these events in this PR. Wave 19 §19a-2 wires the emitter.
- [ ] `docs/specs/CHANGES.md` — the existing `2026-05-22 — session-compaction.md v1` entry's "References to verify" note about `event-bus-and-trace-catalog.md §6` should move from `pending review` toward `verified`; add a one-line update under the existing entry naming this sub-item's branch / PR.
- [ ] Test-count target: **+6 ± 2** new tests.

### Implementation gotchas

- **`PSEUDONYMOUS`, NOT `PRIVATE`.** Summary text lives in the message store, not the event payload, so PRIVATE-tier sensitivity is wrong. PSEUDONYMOUS is correct.
- **NOT in `AUDIT_EVENT_TYPES`.** Compaction is an operational optimization, not a compliance event. Do not add these to the audit frozenset.
- **`Decimal` for `cost_usd`**, not `float`. Match existing `LLMCallCompleted` style.
- **The `failure_mode` literal must include exactly the four values** in `session-compaction.md §6`: `"adapter_error" | "no_valid_boundary" | "budget_exhausted_forced" | "validation_failed"`. Don't add or remove.
- **No new event types beyond the three listed.** If the spec implies a fourth, flag it as a question in the PR body; don't add speculatively.

### Completion

```bash
uv run pytest packages/metis/tests/core/events/ -x
uv run pytest                                                # full suite
uv run ruff check packages apps scripts
uv run ruff format packages apps scripts
git add -A
git commit -m "feat(events): session.compaction_* in catalog (Wave 18a-5)

Three new PSEUDONYMOUS event payloads + PAYLOAD_REGISTRY entries per
session-compaction.md §6. Spec docs/specs/event-bus-and-trace-catalog.md
§6 carries the new rows. No emitter wiring — Wave 19 §19a-2 wires
SessionManager.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
git push -u origin wave-18a-5
gh pr create --title "feat(events): session.compaction_* in catalog (Wave 18a-5)" \
             --base main \
             --body "Implements [session-compaction.md §6](docs/specs/session-compaction.md) per the Wave-18 dispatch plan in [token-reduction-strategy.md §5.1.1.2](docs/design/token-reduction-strategy.md). Catalog wiring only; emitter lands Wave 19 §19a-2.

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
```

HALT — do not start any other sub-item.

---

# Phase 2 (parallel — both depend on 18a-1's merge to main)

> **Prerequisite check before spawning Phase 2:** verify
> `wave-18a-1` has merged to `origin/main`. If not, spawning these
> agents is wasted work — they will fail at the test step.
>
> ```bash
> git fetch origin main
> git log origin/main --oneline -5    # expect to see the 18a-1 PR merge commit
> ```

## Agent D — Wave 18a-2: `metis evaluate --batch-mode`

**Branch:** `wave-18a-2`
**Size:** M (~2 sittings)
**Phase:** 2
**Dependencies:** 18a-1 merged

### Read first

1. [`docs/specs/provider-adapter-contract.md §4.6`](../specs/provider-adapter-contract.md) — the adapter contract 18a-1 implemented.
2. [`docs/specs/evaluator.md`](../specs/evaluator.md) — the existing evaluator surface, especially §6.2 (the `metis evaluate` CLI).
3. [`docs/design/token-reduction-strategy.md §5.1.1.2 → 18a-2`](token-reduction-strategy.md).
4. [`packages/metis/src/metis/core/eval/cli.py`](../../packages/metis/src/metis/core/eval/cli.py) — the current evaluator CLI entry.
5. [`packages/metis/src/metis/cli/main.py`](../../packages/metis/src/metis/cli/main.py) — where the `evaluate` subparser registers (around line 76).

### Touches

**Modify:**

- [`packages/metis/src/metis/cli/main.py`](../../packages/metis/src/metis/cli/main.py) — add `--batch-mode` (submit-only) and `--collect-batches` (poll + ingest) flags to the existing `evaluate` subparser.
- [`packages/metis/src/metis/core/eval/cli.py`](../../packages/metis/src/metis/core/eval/cli.py) — branch on the new flags:
  - `--batch-mode`: collect all `eval` requests for the window, call `adapter.submit_batch`, persist the handle, exit without waiting.
  - `--collect-batches`: read pending handles, call `adapter.poll_batch`; for each completed batch, call `adapter.fetch_batch` and emit `eval.completed` events with `pricing_mode="batch"` stamped (via the adapter's `Usage.pricing_mode` already from 18a-1).
- [`packages/metis/src/metis/core/eval/subscriber.py`](../../packages/metis/src/metis/core/eval/subscriber.py) — if any of the `eval.completed` emission paths assume sync-mode behavior, surface them and add the batch path.
- [`docs/specs/evaluator.md`](../specs/evaluator.md) — new subsection under §6.2 documenting the two flags + the two-pass workflow.

**Create:**

- `packages/metis/tests/core/eval/test_batch_mode.py` (new file) — submit → poll → fetch → ingest cycle against a fixture; idempotency on a second `--collect-batches`.

**Touches the trace DB (additive):**

- New SQLite table `evaluator_batch_handles` in the existing trace DB. Schema (additive; do NOT bump `TRACE_SCHEMA_VERSION`):
  ```sql
  CREATE TABLE IF NOT EXISTS evaluator_batch_handles (
      custom_id TEXT PRIMARY KEY,
      batch_id TEXT NOT NULL,
      provider TEXT NOT NULL,
      submitted_at_ms INTEGER NOT NULL,
      subject_kind TEXT NOT NULL,
      subject_id TEXT NOT NULL,
      status TEXT NOT NULL,
      ingested_at_ms INTEGER
  );
  CREATE INDEX IF NOT EXISTS idx_evaluator_batch_handles_status
      ON evaluator_batch_handles(status);
  ```

### Acceptance criteria

- [ ] `metis evaluate --batch-mode --subject turn --since 2026-05-01T00:00:00Z` (against a known trace DB) submits a batch, persists handles in `evaluator_batch_handles`, exits without waiting. STDOUT prints the batch id(s) + an informational line.
- [ ] `metis evaluate --collect-batches` polls pending handles, ingests completed results as `eval.completed` events, marks `evaluator_batch_handles.status = "ingested"` and stamps `ingested_at_ms`.
- [ ] Verdicts on a known fixture (`tests/fixtures/eval_batch_fixture.db` or similar) match byte-for-byte against the sync `metis evaluate` output for the same window. The only difference is `eval.completed.payload.pricing_mode = "batch"`.
- [ ] A second `--collect-batches` invocation is idempotent — no duplicate `eval.completed` events.
- [ ] Test-count target: **+6 ± 2** new tests.

### Implementation gotchas

- **DO NOT bump `TRACE_SCHEMA_VERSION`** — the new table is additive. `CREATE TABLE IF NOT EXISTS` makes the migration zero-friction.
- **`subject_kind = "turn" | "session" | "workload"`** — the spec already supports re-evaluation at three subject kinds via §6.2; preserve all three.
- **One batch per `--subject` per invocation, NOT one batch per turn.** A single `evaluator.submit_batch` call should bundle all in-window items, up to the Anthropic 100k / 256MB cap. If the window exceeds the cap, chunk into multiple batches and persist multiple rows in one invocation.
- **`pricing_mode="batch"` propagation.** The adapter's `Usage` already carries it from 18a-1. Your job is to make sure the `eval.completed` event payload (or the `LLMCallCompleted` it triggers) preserves it.
- **No CLI added beyond `--batch-mode` + `--collect-batches`.** Don't add `--cancel-batch`, `--list-batches`, etc.; out of scope.

### Completion

```bash
uv run pytest                                                # full suite
uv run ruff check packages apps scripts
uv run ruff format packages apps scripts
git add -A
git commit -m "feat(eval): metis evaluate --batch-mode (Wave 18a-2)

Submits eval re-runs to the Anthropic Batches API at 50% discount;
persists handles in a new evaluator_batch_handles table;
--collect-batches polls + ingests. Verdicts match sync mode
byte-for-byte (only pricing_mode differs).

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
git push -u origin wave-18a-2
gh pr create --title "feat(eval): metis evaluate --batch-mode (Wave 18a-2)" \
             --base main \
             --body "Captures the 50% Anthropic Batches API discount on evaluator backfills per [token-reduction-strategy.md §4 Tier 2](docs/design/token-reduction-strategy.md) and [provider-adapter-contract.md §4.6](docs/specs/provider-adapter-contract.md). Depends on Wave 18a-1.

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
```

HALT — do not start any other sub-item.

---

## Agent E — Wave 18a-3: `scripts/benchmark.py --batch-mode`

**Branch:** `wave-18a-3`
**Size:** M (~2 sittings)
**Phase:** 2
**Dependencies:** 18a-1 merged

### Read first

1. [`docs/specs/provider-adapter-contract.md §4.6`](../specs/provider-adapter-contract.md) — the adapter contract 18a-1 implemented.
2. [`docs/specs/benchmark.md`](../specs/benchmark.md) — the existing benchmark spec; identify where batch fits.
3. [`docs/design/token-reduction-strategy.md §5.1.1.2 → 18a-3`](token-reduction-strategy.md).
4. [`scripts/benchmark.py`](../../scripts/benchmark.py) — the current harness.
5. [`benchmarks/RESULTS.md`](../../benchmarks/RESULTS.md) — the format for the new entry you'll add.

### Touches

**Modify:**

- [`scripts/benchmark.py`](../../scripts/benchmark.py) — add `--batch-mode` (submit only) and `--collect-batch <run_id>` (poll + ingest) flags. Sub-shape mirrors Agent D's evaluator pattern.
- [`docs/specs/benchmark.md`](../specs/benchmark.md) — new subsection documenting the two flags and the two-pass workflow.
- [`benchmarks/RESULTS.md`](../../benchmarks/RESULTS.md) — new §Wave-18 entry with measured cost-per-quality reduction on at least one workload.

**Persistence (new artifact directory pattern — no code-level new files):**

- `benchmarks/.runs/<run_id>/batch-handles.jsonl` (gitignored under `.runs/`) — one JSONL line per submitted batch: `{custom_id, batch_id, provider, submitted_at_ms, workload, model, status}`.

### Acceptance criteria

- [ ] `scripts/benchmark.py --batch-mode --workload fix-a-bug-small` submits the workload's turn requests as a batch, writes `benchmarks/.runs/<run_id>/batch-handles.jsonl`, exits without waiting.
- [ ] `scripts/benchmark.py --collect-batch <run_id>` polls pending handles, finalizes the run, produces the standard `benchmarks/.runs/<run_id>/results.json` (or whatever the existing harness emits) with `pricing_mode="batch"` on every `LLMCallCompleted`.
- [ ] A new `benchmarks/RESULTS.md` §Wave-18 entry documents:
  - One workload run twice: once in sync mode, once in batch mode.
  - Measured cost delta (expect ~50% reduction in batch mode).
  - Quality scores (should be identical between modes since the model is the same and `temperature` is unchanged).
  - Total spend for the comparison (target ≤ $0.50).
- [ ] Test-count target: **+4 ± 2** new tests (the benchmark harness lives under `scripts/` and may not have full pytest coverage — add tests where the existing harness has them; otherwise document smoke results inline).

### Implementation gotchas

- **The benchmark workflow is offline-and-async-friendly already.** Batch mode just changes the underlying adapter call; the per-workload turn loop is unchanged.
- **`--collect-batch` takes a `<run_id>`** — the harness identifies runs by id already, so this flag re-uses the existing run id from `--batch-mode`'s output.
- **`benchmarks/.runs/` is gitignored.** Don't check in the JSONL handles file.
- **The `RESULTS.md` entry is the load-bearing artifact** for this sub-item. Without a measurable cost-per-quality entry, the wave doesn't demonstrate the 50% reduction; spend the time to make the comparison clean.
- **No new workloads.** Use existing workloads; do not introduce new ones in this PR.

### Completion

```bash
uv run pytest                                                # full suite
uv run ruff check packages apps scripts
uv run ruff format packages apps scripts
# Live API spend on a small workload to populate RESULTS.md:
uv run python scripts/benchmark.py --batch-mode --workload fix-a-bug-small
# wait, then:
uv run python scripts/benchmark.py --collect-batch <run_id>
# document results in benchmarks/RESULTS.md
git add -A
git commit -m "feat(benchmark): scripts/benchmark.py --batch-mode (Wave 18a-3)

Submits benchmark runs to the Anthropic Batches API at 50% discount;
persists handles per-run; --collect-batch finalizes. RESULTS.md §Wave-18
documents cost-per-quality on fix-a-bug-small.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
git push -u origin wave-18a-3
gh pr create --title "feat(benchmark): scripts/benchmark.py --batch-mode (Wave 18a-3)" \
             --base main \
             --body "Captures the 50% Anthropic Batches API discount on benchmark re-runs per [token-reduction-strategy.md §4 Tier 2](docs/design/token-reduction-strategy.md) and [provider-adapter-contract.md §4.6](docs/specs/provider-adapter-contract.md). Depends on Wave 18a-1.

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
```

HALT — Wave 18 closes once this and 18a-2 are merged.

---

# Wave 18 close (operator checklist)

Once all five PRs (`wave-18a-1` through `wave-18a-5`) are merged to
`origin/main`, run the §5.1.1.4 doc-sync checklist from
[`token-reduction-strategy.md`](token-reduction-strategy.md):

```
[ ] AGENTS.md status sentence: "Wave 18 reaches the
    batch-API + compaction-substrate milestone."
[ ] AGENTS.md "What works" — one entry for async batch (18a-1/2/3),
    one for compaction substrate (18a-4/5).
[ ] README.md — test count bumped 1865 → ~1909.
[ ] docs/specs/CHANGES.md — two `pending review` entries
    (`session-compaction.md` + `provider-adapter-contract.md §4.6`)
    move to `verified`.
[ ] docs/specs/event-bus-and-trace-catalog.md §6 — three new
    `session.compaction_*` rows landed via 18a-5.
[ ] docs/specs/evaluator.md — `--batch-mode` documented via 18a-2.
[ ] docs/specs/benchmark.md — `--batch-mode` documented via 18a-3.
[ ] benchmarks/RESULTS.md — §Wave-18 batch-mode measurement landed
    via 18a-3.
```

Then Wave 19 planning starts; see [`token-reduction-strategy.md §5.2`](token-reduction-strategy.md).
