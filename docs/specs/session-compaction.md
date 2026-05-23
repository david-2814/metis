# Session Compaction Specification

**Status:** Drafted 2026-05-22; implementation deferred to Wave 18 (substrate) + Wave 19 (end-to-end). See [`docs/design/token-reduction-strategy.md §5`](../design/token-reduction-strategy.md).
**Last updated:** 2026-05-22

> **One-line framing.** Long sessions grow input context linearly. The
> shipped prompt-cache work ([`context-assembler.md §5.1`](context-assembler.md))
> pins the stable prefix; the **mutating tail** of message history is
> not cached and grows unbounded. Compaction summarizes older turns
> into a single compressed block so the tail stays bounded, with a
> content-hash-keyed cache so re-runs don't pay summarization cost twice.

> **What this is not.** Not a replacement for
> [`memory-store.md`](memory-store.md): MEMORY.md captures *cross-session*
> distilled facts; compaction acts on *in-session* message history. Not
> a trace-store retention mechanism: compaction touches only the
> in-memory session and the per-workspace cache; the trace store
> ([`event-bus-and-trace-catalog.md §7`](event-bus-and-trace-catalog.md))
> is unchanged. Not a context-window-limit fallback: compaction is a
> cost lever first, a length lever second.

This spec depends on:

- [`canonical-message-format.md`](canonical-message-format.md) — the
  `Message` shape that compaction reads and writes.
- [`event-bus-and-trace-catalog.md`](event-bus-and-trace-catalog.md) —
  three new `session.compaction_*` events register here.
- [`context-assembler.md`](context-assembler.md) — the stable-prefix
  contract that compaction must NOT invalidate.
- [`provider-adapter-contract.md`](provider-adapter-contract.md) — the
  separate LLM call compaction makes uses the standard adapter path.

---

## 1. Purpose

A long `metis dev` session accumulates message history turn over turn.
By turn ~30 the message history has typically grown past the prefix
itself; the prefix is cached, the history is not, and each turn pays
the full input-token rate on the *growing* part of the request. This
is the largest unaddressed token-cost lever for live `metis dev`
([`token-reduction-strategy.md §4 Tier 1`](../design/token-reduction-strategy.md)).

Compaction is the standard remedy: when the message history crosses a
threshold, summarize the older turns into one compressed block, splice
that block back into the history in place of the summarized span, and
proceed. The technique itself is well-trodden — Claude Code's
compaction, LangChain's conversation-buffer-summary memory, Letta's
recall/archival memory — but Metis adds two specific constraints:

1. **The stable prefix must not change.** The provider's prompt cache
   reads from the prefix; if compaction altered the prefix, every
   subsequent turn would cache-miss. So compaction touches only the
   message history, and only the portion older than a sliding
   watermark.
2. **The compaction summary itself is cached.** A content-hash key
   over `(messages-to-compact, summarization_model, prompt_version)`
   makes the operation deterministic and re-run-free across restarts,
   benchmark reruns, and evaluator re-evaluations. This is what makes
   it a "cache" in the strict sense, not just a context-curation pass.

---

## 2. Goals and non-goals

### 2.1 Goals

- Reduce per-turn input tokens on sessions that exceed a configured
  threshold.
- Preserve the stable-prefix invariant so prompt caching remains 100%
  hit-rate.
- Deterministic across restarts: the same span hashed the same way
  reuses the same summary.
- Bounded in cost: a hard cap on cumulative compaction spend per
  session, layered on the existing evaluator
  [`BudgetTracker`](evaluator.md) primitive.
- Observable: emit `session.compaction_*` events for the trace store,
  analytics, and TUI surfacing.

### 2.2 Non-goals

- **Cross-session memory.** That is
  [`memory-store.md`](memory-store.md).
- **Trace deletion.** Compaction modifies the in-memory session and
  message store representation; the trace store retains the original
  events untouched (`event-bus-and-trace-catalog.md §7` invariants
  hold).
- **Semantic compaction across domain shift.** v1 uses straight LLM
  summarization against a fixed prompt; if a session changes topic
  three times, the summary smears it. Higher-fidelity compaction
  (topic-segmented, structured) is a v2 concern.
- **Multi-pass compaction** (compacting an already-compacted summary
  again). v1 watermarks the compacted span and never re-touches it.
- **Auto-recovery from a bad summary.** If summarization produces
  nonsense, the session-level outcome may degrade; v1 emits
  `session.compaction_failed` on hard failure and falls back to
  truncation. Quality regression on a *successful but bad* summary is
  a measurement problem (see §10).

---

## 3. Trigger and scope

### 3.1 When compaction fires

A `SessionManager` configuration knob:

```python
SessionManager(
    ...,
    compaction_threshold_tokens: int = 30_000,
    compaction_hard_cap_tokens: int = 80_000,
    compaction_preserve_recent_turns: int = 5,
    compaction_model: str = "anthropic:claude-haiku-4-5",
    compaction_max_cost_per_session_usd: Decimal = Decimal("0.20"),
)
```

At the start of every turn, **before** the LLM call:

1. Compute `pending_history_tokens` — the sum of tokens in messages
   between the stable-prefix end and the most recent
   `compaction_preserve_recent_turns` turns. Use the active adapter's
   token-count helper if available; otherwise a cheap estimator
   (`len(text) / 3` is the documented fallback for §3.1 calculations
   per [`context-assembler.md §5.1`](context-assembler.md)).
2. If `pending_history_tokens >= compaction_hard_cap_tokens`, **force**
   compaction. The compaction call counts toward
   `compaction_max_cost_per_session_usd` but is not gated by it — if
   the cap is already exhausted, the compaction LLM call runs anyway
   and `session.compaction_failed` is emitted with
   `failure_mode="budget_exhausted_forced"`. The session then degrades
   to truncation of the oldest non-preserved turns (see §3.4).
3. Else if `pending_history_tokens >= compaction_threshold_tokens`,
   attempt compaction. If the per-session budget is exhausted, skip
   silently — the turn proceeds with the un-compacted history.
4. Else, skip compaction.

### 3.2 What gets compacted

The **span** to compact is `messages[stable_prefix_end : compaction_watermark]`,
where `compaction_watermark` is the message index immediately before the
preserved tail (`message_count - compaction_preserve_recent_turns`).

The watermark is monotonic: once a span is compacted, it is replaced by
a single synthetic message and a new `compaction_watermark` is
recorded. Subsequent compactions only touch messages added *after* the
new watermark. The compacted synthetic message itself is never
re-compacted in v1.

### 3.3 Tool-call boundary preservation

Compaction MUST NOT split a `tool_use` block from its matching
`tool_result` block. If the span boundary at `compaction_watermark`
would fall inside a tool cycle, the watermark slides *earlier* (toward
the prefix) until it lands on a tool-cycle boundary. If no valid
boundary exists in the candidate range, compaction is skipped for this
turn and `session.compaction_failed` is emitted with
`failure_mode="no_valid_boundary"`.

### 3.4 Fallback to truncation

If the compaction LLM call fails (NETWORK / AUTH / capability error per
[`provider-adapter-contract.md §6`](provider-adapter-contract.md)) **and**
`pending_history_tokens >= compaction_hard_cap_tokens`, the session
falls back to **hard truncation**: drop the oldest non-preserved messages
(respecting tool-cycle boundaries per §3.3) until
`pending_history_tokens < compaction_hard_cap_tokens`. A
`session.compaction_failed` event is emitted with
`failure_mode="adapter_error"` and a `truncated_message_count` payload
field. Truncation is the explicit degradation path — it loses more than
summarization but keeps the session running.

If the failure happens below the hard cap, compaction is simply skipped
for this turn (no fallback truncation); the next turn will retry.

---

## 4. The compaction call

A separate LLM call against the model named by `compaction_model`. The
call goes through the standard adapter path
([`provider-adapter-contract.md`](provider-adapter-contract.md)) — same
retry, same error classification, same cost reporting — but is
**accounted separately** from the session's primary LLM spend.

### 4.1 Request shape

- `system_prompt`: a fixed compaction prompt, versioned via
  `compaction_summary_prompt_version` (see §4.3). NOT the session's
  system prompt — compaction is a stateless transformation, the
  session's instructions are irrelevant.
- `messages`: the span to compact, verbatim. The compaction model
  sees the same `tool_use` / `tool_result` / `text` blocks the session
  sees.
- `max_tokens`: bounded; default 4096. The summary is a single block
  of text.
- No tools. The compaction model emits a text response only.

### 4.2 Response handling

The text response becomes the body of a single synthetic message:

```python
Message(
    id=next_monotonic_ulid(),
    role=Role.USER,                       # see §4.4 for the role choice
    content=[
        TextContentBlock(text=f"<COMPACTION_SUMMARY model={compaction_model} watermark={watermark_before}>\n{summary_text}\n</COMPACTION_SUMMARY>"),
    ],
    metadata=MessageMetadata(
        synthetic=True,
        compaction_span=(watermark_before, watermark_after),
        compaction_cache_key=cache_key,
    ),
)
```

The original span is replaced in the in-memory session and the
persistent session store (see §6) by this single message.

### 4.3 Summarization prompt versioning

`compaction_summary_prompt_version` is a string version pinned in code.
Bumping it forces a cache miss for previously-cached summaries — old
cache rows remain readable for replay but a new compaction of the same
span re-summarizes with the new prompt. This is the standard rubric-
versioning pattern used by
[`evaluator.md §12 invariant 7`](evaluator.md). Default: `"1.0.0"`.

The compaction prompt itself is a small, fixed text — out of scope of
this spec. Quality of the default prompt is a measurement question
(§10), not a contract.

### 4.4 Role choice for the synthetic message

The synthetic message uses `role=Role.USER` to preserve the strict
alternation invariant
([`canonical-message-format.md §5.1`](canonical-message-format.md)).
The compacted span ended on either a user or assistant message (it must
have, by §3.3 — tool cycles are atomic), and the next non-compacted
message is whichever came after the watermark. The compactor inserts an
assistant-then-user pair if needed to preserve alternation:

- If the compacted span ended on an assistant message AND the next
  preserved message is also assistant, an empty `Role.USER` message is
  not needed — the synthetic compaction message IS the user message.
- If the compacted span ended on a user message AND the next preserved
  message is also user, the synthetic compaction message replaces the
  span as `Role.USER` and a synthetic empty
  `Role.ASSISTANT` ack message ("compaction acknowledged") is inserted
  to preserve alternation.

The exact alternation-preservation rule is a small implementation detail
— the contract is: **the compacted message history MUST satisfy
`validate_message()` invariants in `canonical-message-format.md §5`**.

---

## 5. Cache key and storage

### 5.1 Cache key

```python
cache_key = sha256_hex(
    msgpack.dumps({
        "messages": [_canonicalize(m) for m in messages_to_compact],
        "summarization_model": compaction_model,
        "summarization_prompt_version": compaction_summary_prompt_version,
        "summarization_max_tokens": max_tokens,
    })
)
```

`_canonicalize(m)` strips per-message metadata that should not affect the
cache (timestamps, message ids, cost stamps) and keeps the
content blocks in their `msgspec`-encoded form. The exact canonical
form is a stability contract: two compactions of byte-identical content
must produce the same key, and trivial metadata differences must not.

### 5.2 Storage

Cache rows live in a per-workspace SQLite database at
`<workspace>/.metis/compaction-cache.sqlite`. Schema:

```sql
CREATE TABLE compaction_cache (
    cache_key TEXT PRIMARY KEY,
    summary_text TEXT NOT NULL,
    summarization_model TEXT NOT NULL,
    summarization_prompt_version TEXT NOT NULL,
    span_message_count INTEGER NOT NULL,
    span_token_count_in INTEGER NOT NULL,
    span_token_count_out INTEGER NOT NULL,
    created_at_ms INTEGER NOT NULL,
    last_read_at_ms INTEGER NOT NULL,
    use_count INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX idx_compaction_cache_last_read ON compaction_cache(last_read_at_ms);
```

The cache is bounded by row count
(`compaction_cache_max_rows = 1000`, configurable) with LRU eviction on
`last_read_at_ms`. No age-based eviction in v1 — a summary that hasn't
been touched in a year and still fits is still valid.

### 5.3 Cache miss vs. hit semantics

- **Hit** (`cache_key` row exists): increment `use_count`, update
  `last_read_at_ms`, return `summary_text`. Skip the LLM call. Emit
  `session.compaction_completed` with `cache_hit=True` and zero
  `cost_usd` / `latency_ms` (the read latency itself).
- **Miss**: run the LLM call, write the row, return the summary. Emit
  `session.compaction_completed` with `cache_hit=False` and the actual
  `cost_usd` / `latency_ms`.

### 5.4 Concurrency

The compaction cache uses `threading.RLock()` per the same pattern as
the pattern store ([`pattern-store.md §17`](pattern-store.md)) to
prevent `sqlite3.InterfaceError` under hostile thread contention. In
the documented single-asyncio-task architecture, the lock is
uncontended.

---

## 6. Events

Three new events register in `PAYLOAD_REGISTRY`:

| Event type | Sensitivity | Payload |
|------------|-------------|---------|
| `session.compaction_started` | PSEUDONYMOUS | `{session_id, turn_id, watermark_before, span_message_count, span_token_count_in, threshold_or_hard_cap}` |
| `session.compaction_completed` | PSEUDONYMOUS | `{session_id, turn_id, watermark_before, watermark_after, span_message_count, span_token_count_in, span_token_count_out, cache_hit, cache_key, cost_usd, latency_ms}` |
| `session.compaction_failed` | PSEUDONYMOUS | `{session_id, turn_id, watermark_before, span_message_count, failure_mode, error_class?, error_message?, truncated_message_count?}` |

`failure_mode` is `Literal["adapter_error", "no_valid_boundary", "budget_exhausted_forced", "validation_failed"]`.

All three are PSEUDONYMOUS because they reference a `session_id` and
optional `error_message` (with the standard PII-stripping pass per
[`redaction.md`](redaction.md)). The summary text itself is **not** in
the payload — it lives in the message store, where it is subject to the
session store's existing persistence rules.

These events are NOT in `AUDIT_EVENT_TYPES`
([`audit-log.md`](audit-log.md)) — compaction is an operational
optimization, not a compliance event.

---

## 7. Interaction with prompt caching

The shipped prompt-cache work places **three** cache breakpoints
([`context-assembler.md §3`](context-assembler.md) v2,
[`provider-adapter-contract.md §4.5`](provider-adapter-contract.md)):

1. **Tools** — on the last `tools[]` entry.
2. **Stable system prompt** — on the last block of the stable segment
   ([`context-assembler.md §5.1`](context-assembler.md)).
3. **Rolling history** — on the last content block of the *last
   message* in the transcript; advances every turn so the entire
   transcript-up-to-the-current-turn enters the cached prefix.

Compaction acts on the **message history** between the stable prefix
and the preserved recent tail. Breakpoints 1 and 2 are upstream of
that region and are not touched by compaction — they remain
cache-warm across a compaction event.

**Breakpoint 3 (rolling history) IS invalidated by compaction.** When
compaction replaces messages `[stable_prefix_end : compaction_watermark]`
with a single synthetic message, the prefix portion ending at the
rolling breakpoint changes shape (a long span is replaced by a short
summary). The next LLM call after compaction sees a different prefix
than the cache holds and pays cache-write rate on the new compacted
prefix; the call after *that* sees the new prefix as cached and reads
back at cache-read rate.

This is a **one-time per-compaction re-cache cost**, not a recurring
penalty. The cost arithmetic for a session that crosses the threshold
once at turn ~30 with ~30k tokens of compactable history:

| Phase | Per-turn input billing |
|-------|------------------------|
| Pre-compaction (turn 30) | ~3k tokens stable prefix (cache-read) + ~30k tokens transcript (cache-read on the rolling-breakpoint cache) + ~1k tokens new turn (full input) |
| One-time at compaction (turn 31) | ~3k tokens stable prefix (cache-read) + ~5k tokens new compacted prefix (cache-**write**) + ~1k tokens new turn (full input) |
| Post-compaction steady state (turn 32+) | ~3k tokens stable prefix (cache-read) + ~5k tokens compacted prefix (cache-read) + ~1k tokens new turn (full input) |

The compaction lever still wins on a multi-turn horizon: the recurring
input-token cost drops from `~31k tokens billed per turn` (pre) to
`~6k tokens billed per turn` (post). The one-time re-cache adds
roughly one turn's worth of cache-write cost (~6k tokens at ~1.25×
input rate). Break-even is ~1-2 post-compaction turns on a haiku-rate
basis.

The §3.3 tool-cycle boundary guard also implicitly protects against
splitting a rolling-breakpoint span: the rolling breakpoint sits on
the *last* message, which by construction comes after
`compaction_watermark = message_count - compaction_preserve_recent_turns`,
so the breakpoint placement is never inside the compacted span.

§10 measurement validates the cost-curve empirically; if break-even
doesn't materialize within 2 post-compaction turns, the
`compaction_threshold_tokens` default needs raising.

---

## 8. Interaction with delegation

Worker sessions ([`delegation.md §5`](delegation.md)) are short-lived
and almost always finish before crossing the compaction threshold. The
contract:

- A worker session inherits its parent's
  `compaction_threshold_tokens` etc. configuration.
- A worker session DOES NOT share the parent's compaction cache row
  for its own history — the worker's prefix differs (different system
  prompt, different available tools per `_WORKER_FORBIDDEN_TOOLS`), so
  the cache keys naturally differ.
- A worker session that DOES hit the threshold (deep recursion or a
  long planner-then-many-mechanical-steps cycle) compacts its own
  history normally. The compaction cost rolls up under the parent via
  the standard analytics rollup (`group_by=parent_session`).

Worker compaction is not a primary use case; this section exists to
pin the contract, not to optimize for it.

---

## 9. Interaction with MEMORY.md

Compaction is in-context; MEMORY.md is cross-session. **Compaction
does NOT touch MEMORY.md.** If the agent learned a durable fact during
a span that gets compacted, the agent must have already `memory_add`'d
it during that span — otherwise the fact persists only in the summary,
which is in-session only.

The summarization prompt SHOULD instruct the model to note "facts the
agent learned that may warrant memory_add" in the summary itself, so a
post-compaction turn can see the recommendation. v1 does not act on
this automatically; the agent reads its own summary like any other
message.

---

## 10. Measurement

A new benchmark workload is required to validate compaction in CI.
Candidate shape:

- **Workload name:** `long-session-compaction` (or similar).
- **Profile:** ~40 turns, each turn adding ~1k tokens of message
  history. Total uncompacted: ~40k input tokens by turn 40, well
  above the default threshold.
- **Measurement:** compare per-turn input-token totals with compaction
  enabled vs. disabled. Expected reduction: 30–60% on turns past the
  threshold, per the `token-reduction-strategy.md §4` estimate.
- **Quality signal:** the existing per-turn heuristic + LLM-judge
  pipeline. Quality must not degrade by more than 0.10 mean score
  versus the no-compaction baseline.

Wave 19 §19a-5 owns the measurement. If the quality regression exceeds
0.10, the default compaction prompt needs tuning before the lever
ships.

---

## 11. Configuration summary

| Knob | Default | Purpose |
|------|---------|---------|
| `compaction_threshold_tokens` | `30_000` | Soft trigger — compact if exceeded and budget allows. |
| `compaction_hard_cap_tokens` | `80_000` | Forced trigger — compact regardless of budget; degrade to truncation on failure. |
| `compaction_preserve_recent_turns` | `5` | The N most recent turns are never compacted. |
| `compaction_model` | `"anthropic:claude-haiku-4-5"` | The summarization model. |
| `compaction_max_cost_per_session_usd` | `Decimal("0.20")` | Per-session compaction-spend cap. |
| `compaction_summary_prompt_version` | `"1.0.0"` | Bump to force cache miss on a prompt change. |
| `compaction_cache_max_rows` | `1000` | LRU cap on the per-workspace cache DB. |

All knobs are `SessionManager.__init__` keyword arguments with the
defaults above. Operators tune via `<workspace>/.metis/config.yaml`
(when that surface lands) or via the `ChatRuntime` builder in
[`metis.cli.runtime`](../../packages/metis/src/metis/cli/runtime.py).

---

## 12. Open questions

1. **Per-workspace vs per-session model.** Some workloads want sonnet
   compaction for higher-fidelity summaries; others want haiku for
   cost. v1 ships a single `compaction_model` knob. A per-skill or
   per-workspace override may be needed; defer until a workload
   surfaces.
2. **Quality regression measurement.** §10 sketches a benchmark
   workload but doesn't ship one. Wave 19 §19a-5 is the place to land
   it; this spec assumes that landing.
3. **TUI surfacing.** A user watching `metis dev` should know
   compaction fired (it's a cost / latency event). A single status
   line is sufficient in v1 — the bus event carries the data. Detail
   level is a [`metis-cli`](../../packages/metis/src/metis/cli/)
   concern, not a `metis.core` concern.
4. **Multi-pass compaction.** If a session is long enough to need
   compaction *twice*, the second pass should compact only the new
   span (which is the default by §3.2). But the synthetic message
   from the first compaction sits in the history; a future v2 may
   want to re-compact the previous summary alongside the new span.
   v1 explicitly does not.
5. **Adapter pricing for compaction calls.** The compaction LLM call
   is just another LLM call from the adapter's perspective — it goes
   through the standard pricing path
   ([`pricing.md`](pricing.md)). But the analytics surface should
   probably expose a `pricing_mode="compaction"` filter so operators
   can isolate compaction spend. Decide during Wave 19 implementation.

---

## 13. References

- [`docs/design/token-reduction-strategy.md`](../design/token-reduction-strategy.md) — the umbrella doc this spec lives under.
- [`context-assembler.md`](context-assembler.md) — the stable-prefix contract compaction must preserve.
- [`provider-adapter-contract.md §4.5`](provider-adapter-contract.md) — prompt-cache placement (informs §7).
- [`provider-adapter-contract.md §4.6`](provider-adapter-contract.md) — the batch API mode shipped alongside this spec; compaction calls do NOT use batch (interactive latency-bound).
- [`memory-store.md`](memory-store.md) — the cross-session memory primitive compaction is NOT.
- [`event-bus-and-trace-catalog.md`](event-bus-and-trace-catalog.md) — where the three new events register.
- [`delegation.md`](delegation.md) — worker-session interaction (§8).
- [`evaluator.md`](evaluator.md) — shared `BudgetTracker` pattern; rubric versioning analog.
- [`pattern-store.md §17`](pattern-store.md) — concurrency pattern (RLock).
