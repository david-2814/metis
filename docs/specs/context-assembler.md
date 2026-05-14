# Context Assembler Specification

**Status:** Draft v2 (additive on v1)
**Last updated:** 2026-05-14
**Scope:** v1 covers prompt-cache breakpoint placement; v2 (§5.1) adds
a **minimum-cacheable-prefix** rule so the cached prefix tokenizes above
the per-model cache floor on *every* session, not just long ones. The full
context-assembler design (skill activation, history compression, behavior
near the context window) is a later spec — see [`STRATEGY.md §6.5`](../STRATEGY.md).

> **v1 motivation.** Per [`STRATEGY.md §1`](../STRATEGY.md), context
> engineering is the single largest cost lever (the "5–10×" claim in
> [`KNOWN_ISSUES.md`](../KNOWN_ISSUES.md)). The cheapest way to realize
> that lever is honest cache-breakpoint placement so the prefix tokens
> the agent reuses turn-to-turn aren't billed at write rates each turn.
> Today the codebase composes a fresh system prompt every LLM call
> (mixing stable instructions with mutating `MEMORY.md` / `USER.md` content),
> so any cache the provider keeps is invalidated immediately. v1 fixes
> that without designing the rest of the assembler.

---

## 1. Purpose

Specify where adapters place provider-recognized cache breakpoints, and
where in the canonical request shape the boundary between *stable* and
*volatile* context lives, so prompt caching actually pays off across
turns.

This spec depends on:

- `canonical-message-format.md §7` — the adapter contract this spec
  parameterizes via `AdapterCapabilities.supports_prompt_caching` and the
  proposed `CanonicalRequest.system_prompt_volatile` field.
- `provider-adapter-contract.md` (planned) — wire-translation rules.

---

## 2. The two-segment system prompt

`CanonicalRequest` carries the system prompt in **two segments**:

| Field | Lifetime | Examples |
|-------|----------|----------|
| `system_prompt: str \| None` | Stable across many turns | Base agent persona, skill discovery index, project-level instructions |
| `system_prompt_volatile: str \| None` | Mutates per-turn | `USER.md`, `MEMORY.md`, anything the agent writes mid-session |

Both are optional. The wire concatenation order is **stable first, volatile
second**. On the wire, this puts the volatile content *after* the
prompt-cache breakpoint (see §3) so changes to it don't churn the cached
prefix.

A caller that doesn't care about caching (e.g. a one-off request from a
script) can put everything in `system_prompt` and leave
`system_prompt_volatile` unset. The adapters fall back gracefully.

---

## 3. Cache breakpoint placement

The cache prefix walked by Anthropic's API in canonical order is
`tools → system → messages`. v1 places **two** breakpoints (both
`{"type": "ephemeral"}`):

1. **`tools[-1].cache_control = ephemeral`** — caches the tool list.
   The tools section is the largest stable prefix in a typical agent
   loop and almost never changes within a session.
2. **`system[<last stable block>].cache_control = ephemeral`** — caches
   `tools + stable system`. The next block is the volatile system text
   (no cache_control), so it falls outside the cached prefix.

Constraints:

- Anthropic's API permits up to 4 cache breakpoints. v1 uses 2.
- Adapters MUST NOT place a cache breakpoint on a volatile block — that
  defeats the purpose.
- When `system_prompt_volatile` is empty / None, the single `system` block
  still carries the breakpoint (caches `tools + system`).
- When there are no tools, only the system breakpoint applies.

OpenAI does not expose explicit breakpoints. Its caching is automatic on
prefix matches of ≥1024 tokens. The adapter's responsibility is to keep
the request prefix **byte-stable**: `system → tools → messages` in that
order, identical text turn-to-turn for the stable portion, with volatile
content concatenated at the end of the system message rather than
inserted in the middle.

OpenRouter passes through to the upstream provider in OpenAI shape. For
Anthropic-backed routes on OpenRouter, the wrapped request still carries
`cache_control` markers in the messages/system blocks (they survive
wrapping). For other upstream providers, OpenAI-shape prefix stability is
the only knob.

---

## 4. Honest capability declaration

`AdapterCapabilities.supports_prompt_caching` is the routing-engine's
substitutability gate for this lever. Adapters MUST set it to `true`
only when:

1. The adapter writes the cache breakpoints described in §3 for that
   model, OR
2. The provider caches automatically on prefix matches (OpenAI), AND the
   adapter preserves prefix stability per §3 last paragraph.

OpenRouter declares `supports_prompt_caching=False` because cache
behavior depends on which upstream the request lands on, and the routing
result isn't reportable from the adapter at request time. This is the
honest answer until OpenRouter exposes per-route cache semantics.

---

## 5. Composition rule for the session manager

`SessionManager` MUST split the system prompt into the two segments:

- `system_prompt` = base persona + skill discovery index (the agent's
  durable instructions).
- `system_prompt_volatile` = `USER.md` + `MEMORY.md` (whatever the
  per-session memory store yields).

This puts the volatile content **after** the cache breakpoint on the
wire, so a turn that mutates `MEMORY.md` doesn't invalidate the cached
prefix for the next turn.

### 5.1. Minimum-cacheable-prefix rule (v2)

Anthropic's cache silently drops `cache_control` markers when the cached
prefix tokenizes below the per-model floor. The Anthropic docs cite:

| Model family   | Documented cache floor (tokens) |
|----------------|---------------------------------|
| haiku 4.5      | 2048                            |
| sonnet 4.x     | 1024                            |
| opus 4.x       | 1024                            |

A live probe against `anthropic:claude-haiku-4-5`
([`benchmarks/RESULTS.md` "Run 3"](../../benchmarks/RESULTS.md)) found
the **effective** floor is higher than the documented 2048 — a prefix
of ~3320 actual input tokens produced
`cache_creation_input_tokens = 0`, while a prefix of ~4957 actual tokens
produced a successful cache write. The implementation MUST target above
the *effective* floor, not the documented one.

The natural Metis stable prefix (the short `DEFAULT_SYSTEM_PROMPT` plus
the five built-in tools) tokenizes to ~265 heuristic tokens — far below
even the documented floor. On haiku that means **every short session
caches zero**: the prefix never clears the floor, the provider drops
the markers, and turn N pays full input-rate on tokens that should
have read at 10% input-rate. This was the root cause of Run 2's
"cache activity in `multi-turn-refactor` only" finding
([`benchmarks/RESULTS.md` §A2](../../benchmarks/RESULTS.md)).

**Rule.** `SessionManager` MUST ensure the stable prefix tokenizes above
the effective floor of any model the session might route to. Concretely:

1. Compute an estimate of `tokens(tools + stable_system_prompt)`.
2. If the estimate is below `MIN_CACHEABLE_PREFIX_TOKENS`, append
   bounded, deterministic padding to `stable_system_prompt` until the
   estimate clears the floor or the upper bound is reached.
3. Hard upper bound: `MAX_CACHEABLE_PREFIX_TOKENS`. Padding stops
   before this bound regardless of remaining headroom — we want a
   cached prefix, not a maximal one.

Default values (per the haiku-4-5 probe):

| Constant                       | Value (heuristic tokens) |
|--------------------------------|--------------------------|
| `MIN_CACHEABLE_PREFIX_TOKENS`  | 4500                     |
| `MAX_CACHEABLE_PREFIX_TOKENS`  | 5500                     |

These are heuristic (`~4 chars / token`) values. Real English-prose
tokenization typically yields *more* actual tokens than the heuristic
estimates (the operating-context block at ~9.7K chars estimated to
~2400 heuristic tokens tokenized to ~3045 actual tokens, a ratio of
~1.26×). Targeting 4500 heuristic tokens corresponds to ~5670 actual
tokens, comfortably above the observed effective floor.

**Padding sources, in priority order:**

1. **Loaded skill bodies.** If the per-session `SkillStore` has one or
   more skills, append each skill's `SKILL.md` body (heading + body
   text), in `name`-ascending order, until the bound is reached or
   bodies run out. Skill bodies are substantive content the agent
   might activate anyway via `skill_load`, so pre-loading them into
   the cached prefix is functional, not filler. (This deviates from
   the discovery-only stance of `skill-format.md` "progressive
   disclosure": v2 keeps `skill_load` for explicit activation, but
   accepts the extra bytes in the cached prefix when the prefix
   *needs* the bytes to clear the floor. The skill-format spec's
   progressive-disclosure norm still applies to the discovery index —
   bodies are only inlined when padding is required.)
2. **Operating-context guidelines.** A static, byte-stable block of
   Metis operating guidelines defined in `metis_core.sessions.manager`
   (constant: `_OPERATING_CONTEXT_PADDING`). This is the universal
   fallback for sessions with no skills loaded. The block must be:
   - **Deterministic across runs** — no timestamps, no run-specific
     ids, no machine fingerprint.
   - **Byte-stable across turns** — generated from a module-level
     constant, not assembled per call.
   - **Substantive** — real guidance about how Metis operates (tool
     etiquette, naming, style, error handling), not lorem-ipsum.
   - **Sized to clear the floor alone** — even with zero skills and
     no tools, the block must tokenize ≥ `MIN_CACHEABLE_PREFIX_TOKENS
     - MAX_BASE_PROMPT_TOKENS` heuristic tokens.

**Determinism constraints (load-bearing).** The padding text MUST be
byte-identical turn-to-turn within a session. Any per-call variation
(e.g. enumerating loaded files, including a session id, embedding the
current time) invalidates the cache on every turn and defeats the rule.
Implementations SHOULD source padding from module-level constants and
sorted, frozen collections.

**Sessions with custom `system_prompt`.** When a caller passes a custom
`system_prompt` to `SessionManager` (e.g. an integration test with its
own padded prompt), §5.1 still runs but typically becomes a no-op: the
caller's prompt is already above the floor, the estimate clears
`MIN_CACHEABLE_PREFIX_TOKENS`, and no padding is appended. The rule is
a floor, not a ceiling.

**Cost trade-off (informational).** Padding adds bytes to every cached
write and read. The break-even N (turns above which padding saves cost)
depends on the *natural* prefix size:

- When the natural prefix is *close to but below* the floor (the
  ~1500–2000-token case in `STRATEGY.md`'s pre-skill design), padding
  is a clear win after ~2 turns.
- When the natural prefix is *far below* the floor (the bare-bones
  case, ~265 tokens with no skills), padding writes more tokens than
  the natural prefix would ever cost uncached — but it lets the
  cache pipeline activate, which is the precondition for any future
  savings as skills, memory, and project instructions accumulate
  in the stable prefix.

The implementation pads in both cases for simplicity. A future
revision MAY introduce a "skip padding when natural prefix is below X"
escape hatch if the bare-bones case turns out to be cost-dominant in
production traffic.

**Observable effect.** After §5.1 lands, the smoke-test scenario
"natural Metis system prompt + no skills" against haiku must produce
`cached_input_tokens > 0` on turn 2 (the load-bearing assertion in
`scripts/smoke_cache.py`). The benchmark suite's
`actual_repriced_usd` should change visibly on the two short workloads
(`fix-a-bug-small`, `write-a-doc-from-notes`) that registered zero
cache tokens in Run 2 §A2 — the direction (savings or modest cost
increase) depends on the trade-off above.

---

## 6. Validation

The validation surface is `/analytics/cache_effectiveness`
([analytics-api.md §4.2](analytics-api.md)) — `hit_rate > 0` after a
multi-turn session against an Anthropic model is the load-bearing
signal. `cache_write_share` should be ≤ 1/N where N is the number of
turns: a higher value means the prefix is churning.

A live-API smoke test (`scripts/smoke_cache.py`) drives a 2-turn
conversation against Anthropic and asserts `cached_input_tokens > 0` on
turn 2. Cost ≤ $0.05 per run.

---

## 7. Out of scope (later iterations)

- **Skill activation.** Loading a skill body changes the stable prefix
  (the discovery index gains a body) — this is OK because the cache
  invalidates only when the prefix actually changes; activation costs one
  extra cache write, then steady state. A future iteration may move
  active skill bodies to a separate breakpoint.
- **History compression.** When history grows past the context window,
  some tail of `messages` is dropped or summarized. That mutates the
  message prefix and invalidates message-level caches. Out of scope until
  there's a measured signal that history caching matters more than the
  system/tools cache.
- **Behavior near the context window.** Hard limit handling, soft warning
  thresholds, eviction strategy — all deferred.
- **Multi-breakpoint placement on long messages.** We can use up to 4
  breakpoints; v1 uses 2 and leaves the headroom unused.

---

## 8. Decision log

| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-05-13 | Two breakpoints (tools + stable system); volatile system trails | Captures the dominant prefix savings without overthinking the design. The four-breakpoint budget is reserved for a future revision once the simple shape ships and is measured. |
| 2026-05-13 | Two-segment system prompt on `CanonicalRequest` rather than a callback or block-list | Lowest-friction shape for callers; adapters that don't care just concatenate the two strings. Block-list shape would force every caller to think about cache placement. |
| 2026-05-13 | OpenRouter declares `supports_prompt_caching=False` | The honest answer: cache behavior depends on which upstream the route lands on. Lying breaks routing's substitutability gate. |
| 2026-05-14 | Minimum-cacheable-prefix rule with bounded padding (§5.1, v2) | v1's breakpoint placement was honest but the natural Metis prefix tokenizes below the effective haiku-4-5 cache floor on short sessions. Without padding, every short session pays full input-rate on tokens that should cache. Padding is bounded so we don't pay for a maximal prefix; sourced first from loaded skill bodies (substantive content) and falling back to a static operating-context block (universal fallback for no-skill sessions). |
| 2026-05-14 | Targets clear the *effective* haiku-4-5 floor (~4000 actual tokens), not the documented 2048 | Live probe showed a 3320-actual-token prefix produced `cache_creation=0`; a 4957-actual-token prefix worked. Picking 4500 heuristic tokens (≈5670 actual at observed ~1.26× ratio) clears the effective floor with margin. |
| 2026-05-14 | Static `_OPERATING_CONTEXT_PADDING` lives in the session manager, not in a separate file | The padding is load-bearing for caching but is otherwise inert text. Keeping it next to the assembly code makes the byte-stability invariant easy to enforce (module-level constant; no I/O at call time). A future v3 may move it to a per-workspace override if users want to customize. |

---

## 9. References

- [`canonical-message-format.md §7`](canonical-message-format.md) — adapter contract this spec parameterizes.
- [`analytics-api.md §4.2`](analytics-api.md) — `/analytics/cache_effectiveness` is the validation surface.
- [`STRATEGY.md §1`](../STRATEGY.md) — context > skills > model selection thesis.
- [`KNOWN_ISSUES.md`](../KNOWN_ISSUES.md) — the prompt-caching gap this spec closes.
