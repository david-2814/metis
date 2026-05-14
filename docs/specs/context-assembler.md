# Context Assembler Specification

**Status:** Draft v1
**Last updated:** 2026-05-13
**Scope:** v1 covers prompt-cache breakpoint placement only. The full
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

---

## 9. References

- [`canonical-message-format.md §7`](canonical-message-format.md) — adapter contract this spec parameterizes.
- [`analytics-api.md §4.2`](analytics-api.md) — `/analytics/cache_effectiveness` is the validation surface.
- [`STRATEGY.md §1`](../STRATEGY.md) — context > skills > model selection thesis.
- [`KNOWN_ISSUES.md`](../KNOWN_ISSUES.md) — the prompt-caching gap this spec closes.
