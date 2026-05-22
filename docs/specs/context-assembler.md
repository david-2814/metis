# Context Assembler Specification

**Status:** Implemented v3 (v1 + v2 + v3 §5.2 explicit activation + pre-activation)
**Last updated:** 2026-05-22
**Scope:** v1 covers prompt-cache breakpoint placement; v2 (§5.1) adds
a **minimum-cacheable-prefix** rule so the cached prefix tokenizes above
the per-model cache floor on *every* session, not just long ones; v3
(§5.2) adds **skill activation** — when and how a skill body moves from
the discovery index into context, the per-session activation budget, and
the bridge between explicit `skill_load` activation and the
body-as-padding pre-activation path introduced in v2 §5.1. History
compression and behavior near the context window remain a later spec —
see the project strategy (private).

> **v3 implementation status (2026-05-14).** Per-session
> [`SkillActivationRegistry`](../../packages/metis-core/src/metis_core/skills/activation.py)
> tracks pre-activations and explicit activations and enforces the
> §5.2.4 budget caps (`MAX_EXPLICIT_ACTIVATIONS_PER_SESSION = 3`,
> `WARN_CUMULATIVE_ACTIVATION_TOKENS = 10000`,
> `HARD_CAP_CUMULATIVE_ACTIVATION_TOKENS = 30000`).
> [`SessionManager.create_session`](../../packages/metis-core/src/metis_core/sessions/manager.py)
> pre-computes the stable system prompt once and emits one
> `skill.loaded(load_reason="always")` event per body inlined by v2
> §5.1 padding; the discovery index annotates those skills `[preloaded]`.
> [`SkillLoadTool`](../../packages/metis-core/src/metis_core/skills/tools.py)
> consults the registry to return a pointer (not the body) for
> pre-activated and re-loaded skills, and to raise `ToolExecutionError`
> on budget exhaustion. The cache breakpoint placement (§3) is
> unchanged — activated bodies live as `tool_result` blocks in message
> history, not in the system prompt (§5.2.3).

> **v1 motivation.** Per the project strategy (private), context
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
`tools → system → messages`. The adapter places **three** breakpoints
(all `{"type": "ephemeral"}`):

1. **`tools[-1].cache_control = ephemeral`** — caches the tool list.
   The tools section is the largest stable prefix in a typical agent
   loop and almost never changes within a session.
2. **`system[<last stable block>].cache_control = ephemeral`** — caches
   `tools + stable system`. The next block is the volatile system text
   (no cache_control), so it falls outside the cached prefix.
3. **`messages[-1].content[-1].cache_control = ephemeral`** — a *rolling*
   breakpoint on the final content block of the last message, extending
   the cached prefix to cover `tools + system + the whole transcript`.
   Added 2026-05-22 (v2).

### 3.1. Why the rolling history breakpoint (v2)

Breakpoints 1 and 2 cache only the *static* prefix — tools plus the
stable system prompt, on the order of `MIN_CACHEABLE_PREFIX_TOKENS`
(~5K tokens). Everything in `messages` falls outside the cached prefix
and is re-billed at full input rate **every turn**.

For a short session that is fine: the transcript is small. For a long
session — or any session whose early turns read large tool outputs into
history — the transcript becomes the dominant share of input tokens,
and caching only a fixed ~5K-token prefix saves a rounding error. A
multi-turn session can sit at full input-rate on 90%+ of its tokens
while every LLM call still *reports* a cache hit (the static prefix
does cache). Cache hit **rate** is not cache hit **coverage**.

Breakpoint 3 fixes this. Because the transcript is append-only, the
prefix `tools + system + messages-through-turn-N` is byte-identical
between turn N's request and turn N+1's request. Marking the last
message each turn writes that prefix to cache; the next turn reads it
back at cache-read rate (10% of input) and pays full price only on the
new delta — the previous assistant response plus the new user message.
The marker sits on a *different* (newer) block every request — it
"rolls" forward — but the cached prefix it defines is stable, which is
what Anthropic matches on.

**Interaction with the volatile system block.** The volatile system
text (breakpoint 2's trailing block) sits between the system breakpoint
and `messages`. When `MEMORY.md` / `USER.md` mutate, the prefix up to
breakpoint 3 changes and breakpoint 3 misses for that one turn;
breakpoints 1 and 2 still hit. This is acceptable — memory mutations
are infrequent — and is the deliberate cost of keeping memory writable
mid-session.

Constraints:

- Anthropic's API permits up to 4 cache breakpoints. The adapter uses 3.
- Adapters MUST NOT place a cache breakpoint on a volatile block — that
  defeats the purpose. Breakpoint 3 lands on message *content*, which is
  append-only and therefore stable; it is not a volatile block.
- When `system_prompt_volatile` is empty / None, the single `system` block
  still carries the breakpoint (caches `tools + system`).
- When there are no tools, only the system + history breakpoints apply.
- The history breakpoint is unconditional whenever `messages` is
  non-empty. When the transcript tokenizes below the model's cache
  floor, Anthropic silently drops the marker — harmless, since a
  sub-floor transcript has nothing worth caching.

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
the only knob. The rolling history breakpoint (breakpoint 3) is emitted
by the direct Anthropic adapter only; bringing the OpenRouter adapter to
parity is tracked as a follow-up.

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
  ~1500–2000-token case in the project strategy (private)'s pre-skill design), padding
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

### 5.2. Skill activation (v3)

Per the project strategy (private), every skill loaded that isn't
used is wasted tokens. The discovery index (~100 tokens per skill) pays
the always-on cost; activation moves a body (~1–5K tokens) into context
on a deliberate decision. v3 specifies what counts as "deliberate," what
the budget is, and how activation interacts with the v2 padding rule so
that loading a skill doesn't churn the cached prefix.

#### 5.2.1. Activation paths

Two paths exist, both observable on the bus via `skill.loaded`
([`event-bus-and-trace-catalog.md §6.6`](event-bus-and-trace-catalog.md);
[`skill-format.md §9.1`](skill-format.md)). The `load_reason` enum on
that payload partitions them:

| `load_reason`     | Path                                  | When emitted                                                    |
|-------------------|---------------------------------------|-----------------------------------------------------------------|
| `"always"`        | **Pre-activation** (v2 §5.1 padding)  | At session init, once per body that v2 §5.1 inlined as padding. |
| `"on_demand"`     | **Explicit activation** (`skill_load`) | When the agent calls `skill_load(name)`. Already implemented.   |
| `"auto_suggested"`| **Auto-activation** (description match) | **Not in v3.** Reserved for a later spec; see §5.2.6.          |

**There is no description-match-driven auto-activation in v3.** The
agent's own LM is the relevance classifier — it reads the discovery
index and either calls `skill_search` (substring filter) or `skill_load`
directly. Rationale:

1. **Preserves agentskills.io progressive disclosure semantics.** The
   open standard treats activation as an agent decision, not a runtime
   classifier output.
2. **Avoids token competition with explicit activation.** A regex /
   substring / embedding classifier that fires on `user_message_text`
   has its own false-positive rate; every false positive burns the
   activation budget.
3. **Avoids non-determinism that breaks caching.** A classifier that
   chooses differently for the same prompt across runs invalidates
   message-level caches that future spec work might place.
4. **No usage data yet.** Once v3 ships and traces accumulate, an
   evidence-based pick between substring / regex / embedding becomes
   tractable. Until then, "agent decides" is the conservative default.

#### 5.2.2. Pre-activation via v2 §5.1 padding

v2 §5.1 inlines skill bodies into the stable system prefix when the
prefix tokenizes below `MIN_CACHEABLE_PREFIX_TOKENS`. v3 formalizes
this as **pre-activation**:

1. At session init, after the stable system prompt is assembled per
   v2 §5.1, the `SessionManager` MUST emit one `skill.loaded` event per
   inlined body, with:
   - `load_reason = "always"`
   - `triggered_by_tool_use_id = None`
   - `source`, `skill_id`, `skill_version`, `load_size_tokens` set
     per [`skill-format.md §9.1`](skill-format.md).
   - Same `session_id` as the session.started event; no `turn_id`
     (emitted outside any turn).
2. The discovery index entry for each pre-activated skill MUST be
   annotated `[preloaded]`:

   ```
   - pdf-processing [preloaded]: Extract PDF text, fill forms, merge files. ...
   ```

   The annotation tells the agent "the body is already in this system
   prompt; don't call `skill_load` for it." This is a substantive
   change to the index format defined in
   [`skill-format.md §7.1`](skill-format.md) — flagged in §5.2.7
   below.
3. If the agent calls `skill_load(name)` for a pre-activated skill,
   the tool MUST return a short pointer text (not the body) of the
   form:

   ```
   # Skill: {name} (source: {source})

   This skill's body is already loaded in the system prompt (pre-activated
   at session start). Re-read the system prompt section "# Skill: {name}"
   for its operating instructions.
   ```

   No `skill.loaded` event fires on this call (the pre-activation event
   already covered it; firing again would double-count in the trace).
   The tool's return metadata MUST include
   `{"already_preloaded": true}` so the agent can disambiguate in case
   of future logic that branches on it.

This bridges v2's padding behavior (a caching mechanism) with v3's
activation contract (an observable trace event) without changing v2's
byte-level placement.

#### 5.2.3. Explicit activation via `skill_load`

The existing tool semantics in [`skill-format.md §8.2`](skill-format.md)
hold unchanged, **with one addition**: the tool consults the per-session
activation budget (§5.2.4) before returning the body. If the budget is
exhausted, the tool raises `ToolExecutionError` with a message of the
form:

```
activation budget exhausted: {N} skills already activated this session
(limit {MAX_EXPLICIT_ACTIVATIONS}). Already loaded: [a, b, c]. To free
budget, summarize and discard previously loaded skill bodies in your
next response.
```

The error surfaces as `tool.failed` per the existing tool dispatcher
contract — no new event type is introduced. The agent sees the error
in the next user turn (as a `tool_result` with `is_error=true`) and can
adjust.

Bodies returned by `skill_load` live as `tool_result` blocks in the
message history, **not in the system prompt**. This is the v2-existing
behavior and is load-bearing for §5.2.5 below.

#### 5.2.4. Activation budget

Three caps apply per session. All counts and sizes consider only
**explicit** activations (`load_reason="on_demand"`); pre-activated
skills don't count, since their bytes are already paid for by the v2
padding rule.

| Constant                              | Default | Counts what                                        |
|---------------------------------------|---------|----------------------------------------------------|
| `MAX_EXPLICIT_ACTIVATIONS_PER_SESSION`| 3       | Distinct skills explicitly activated. Re-loading the same skill is a no-op (already in history) and doesn't increment. |
| `WARN_CUMULATIVE_ACTIVATION_TOKENS`   | 10000   | Sum of `load_size_tokens` across explicit activations. Crossing the threshold logs a `WARNING` and emits no event (pure telemetry). |
| `HARD_CAP_CUMULATIVE_ACTIVATION_TOKENS` | 30000 | Sum of `load_size_tokens`. Reaching this raises `ToolExecutionError` from `skill_load` regardless of count. |

Both the count cap and the token caps fire as `ToolExecutionError`
(surfacing as `tool.failed`); the warn threshold is log-only.

The defaults are **deliberately conservative**: a 200K-context model
can fit far more, but the goal is to keep agents from running away with
skill bodies they don't need. A session with three 5K-token skill
bodies has already consumed ~15K tokens of input on every subsequent
turn. The owner can revise upward after benchmark data shows the cap
is too tight (see §5.2.7 open question 1).

**Configuration.** Per-workspace overrides MAY land via
`<workspace>/.metis/skills/config.yaml` in a future revision; v3 ships
the defaults as module-level constants in
`metis_core.sessions.manager`. Config-file support is deferred to keep
v3 minimal-additive.

#### 5.2.5. Eviction (deferred in v3)

A loaded skill body, once in the message history, stays in history for
the rest of the session. v3 does **not** specify mid-session eviction.
Reasons:

1. **Mutating the message history invalidates message-level caches.**
   The provider sees a different prefix on the next request; any
   message-cache placement (a future spec topic) would lose its hit.
   v3 prefers the cache hit on a bloated history over the cache miss
   on a trimmed one.
2. **Tool-call / tool-result pairs are structurally linked.** Removing
   a `tool_result` block for `skill_load` requires also removing the
   corresponding `tool_use` from the assistant message, or the message
   fails canonical validation (`canonical-message-format.md §5.1.4`).
   Implementing this safely is non-trivial — a turn that summarizes
   and rewrites several past message pairs is an entire feature.
3. **The budget already bounds growth.** With three explicit
   activations capped at 30K cumulative tokens, the worst case is
   bounded. Sessions that exceed the cap surface the failure to the
   agent (per §5.2.3), which can choose to ask the user for a fresh
   session.

Eviction will likely land alongside history compression (the next
context-assembler spec topic) — both share the "rewrite past messages
without breaking caching" problem.

#### 5.2.6. Trace surface

v3 reuses the existing `skill.loaded` event with no payload changes.
The `load_reason` field is the discriminator:

- `"always"` — pre-activation (v2 §5.1 padding); emitted at session
  init, before any `turn.started`.
- `"on_demand"` — explicit activation via `skill_load`; emitted from
  the tool's existing path.
- `"auto_suggested"` — not emitted in v3; reserved for the
  description-match path.

No new event types are introduced. Budget exhaustion surfaces via the
existing `tool.failed` event (with a descriptive `error_message` per
the tool-dispatcher contract). No `skill.unloaded` event exists,
consistent with §5.2.5 deferring eviction.

**Analytics consequence.** A future `/analytics/skills` rollup can
project `skill.loaded` by `load_reason` to answer questions like "what
fraction of pre-activated skill bodies were ever explicitly referenced
by the agent" — useful for tuning §5.1's padding source priority. The
endpoint itself is not specified in v3.

#### 5.2.7. Open questions (owner sign-off)

1. **Default budget numbers.** `MAX_EXPLICIT_ACTIVATIONS_PER_SESSION = 3`,
   `HARD_CAP_CUMULATIVE_ACTIVATION_TOKENS = 30000`,
   `WARN_CUMULATIVE_ACTIVATION_TOKENS = 10000`. These are picks, not
   measurements. Owner should confirm or pick different numbers before
   implementation. The benchmark suite has no current workload that
   loads a skill, so there's no empirical signal yet — Wave 6 should
   add a skill-using workload before tuning.
2. **Discovery-index annotation breaks
   [`skill-format.md §7.1`](skill-format.md).** That spec specifies
   the index format as `- {name}: {description}` (one line per skill).
   v3 adds an optional `[preloaded]` annotation on pre-activated
   skills, changing the format to
   `- {name} [preloaded]: {description}` or
   `- {name}: {description}` depending on session state. Owner should
   confirm this is the right surface (alternatives: a separate
   `## Preloaded skills` block; an annotation in the body header rather
   than the index). Cross-spec edit to `skill-format.md §7.1` lands
   with implementation.
3. **Auto-activation mechanism.** v3 defers `load_reason="auto_suggested"`,
   but leaves the enum value in place. The candidate mechanisms
   enumerated in §5.2.1 (substring / regex / embedding match against
   `user_message_text`) all need usage data before pickable. Open
   until the trace store has explicit-activation patterns to learn
   from. The pattern store ([`pattern-store.md`](pattern-store.md))
   is a candidate substrate — fingerprint inputs already include
   `user_message_text` features.
4. **Re-loading the same skill is a no-op.** v3 says re-calling
   `skill_load` for an already-explicitly-loaded skill doesn't
   re-inject the body and doesn't increment the budget. Should it
   instead return an error (cheaper signal to the agent that it's
   already loaded), or return the body again (no special-case
   handling)? Owner pick. v3 specifies no-op-with-pointer; same
   pattern as §5.2.2 for pre-activated skills.
5. **Pre-activation event ordering.** v3 emits `skill.loaded`
   events with `load_reason="always"` at session init. Should they
   be ordered before or after `session.started`? v3 specifies
   *after* (the session must exist for the event's `session_id`
   foreign key to be valid in the trace store), but ordering inside
   that window (before any `turn.started`) is the contract. Owner
   confirm.

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

- **Auto-activation via description match.** v3 specifies explicit
  (`load_reason="on_demand"`) and pre-padded (`load_reason="always"`)
  activation; the `auto_suggested` enum value is reserved but no
  mechanism is wired. See §5.2.7 open question 3.
- **Mid-session skill eviction.** Loaded bodies stay in the message
  history for session lifetime; see §5.2.5 for rationale. Will likely
  land with history compression.
- **History compression.** When history grows past the context window,
  some tail of `messages` is dropped or summarized. That mutates the
  message prefix and invalidates message-level caches. Out of scope until
  there's a measured signal that history caching matters more than the
  system/tools cache.
- **Behavior near the context window.** Hard limit handling, soft warning
  thresholds, eviction strategy — all deferred.
- **Multi-breakpoint placement on long messages.** We can use up to 4
  breakpoints; v1 uses 2 and leaves the headroom unused.
- **Per-workspace budget overrides.** `MAX_EXPLICIT_ACTIVATIONS_PER_SESSION`
  and the cumulative caps are module-level constants in v3;
  `<workspace>/.metis/skills/config.yaml` overrides are deferred.

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
| 2026-05-14 | v3 skill activation is agent-driven only; no auto-activation via description match | The agent's own LM is the relevance classifier — it reads the discovery index and decides. Description-match auto-activation (substring / regex / embedding against `user_message_text`) introduces non-determinism that breaks caches, competes with the explicit-activation budget, and has no usage data to tune against yet. `load_reason="auto_suggested"` stays reserved for a later spec once trace data accumulates. Preserves agentskills.io progressive disclosure semantics. |
| 2026-05-14 | v2 §5.1 body-as-padding is formalized as pre-activation with `load_reason="always"` | v2 already inlines skill bodies into the stable prefix for cache-floor padding; v3 makes that observable on the bus (one `skill.loaded` per inlined body at session init) and bridges to explicit `skill_load` (which returns a pointer rather than re-injecting the body for a pre-activated skill). This makes "loaded bytes" countable in traces without double-paying the bytes in input. |
| 2026-05-14 | Activation budget is a per-session count cap (default 3) + cumulative token caps (warn 10K / hard 30K), not a per-turn cap | Per-turn caps would prevent multi-skill workflows that legitimately need several skills loaded across early turns; per-session caps bound the long-tail cost without blocking the common case. The actual numbers are picks not measurements — owner sign-off pending; benchmark workload that exercises skills is a Wave 6 prereq. |
| 2026-05-14 | No mid-session skill eviction in v3 | Eviction would mutate message history, invalidating any message-level cache placement a future spec adds. It also requires removing structurally-linked tool_use/tool_result pairs without breaking canonical-format validation. Defer to history-compression spec where the same problem is solved once. |
| 2026-05-14 | Budget exhaustion surfaces as `tool.failed`, not a new event type | The tool-dispatcher contract already emits `tool.failed` with `error_message` for `ToolExecutionError`; reusing it keeps the event catalog closed-list. No new `skill.activation_rejected` event introduced. |

---

## 9. References

- [`canonical-message-format.md §7`](canonical-message-format.md) — adapter contract this spec parameterizes.
- [`analytics-api.md §4.2`](analytics-api.md) — `/analytics/cache_effectiveness` is the validation surface.
- [`skill-format.md §7`](skill-format.md), [§8.2](skill-format.md), [§9.1](skill-format.md) — discovery-index format, `skill_load` semantics, `skill.loaded` payload schema. v3 §5.2.2 implies an additive change to the §7.1 index format (the `[preloaded]` annotation); v3 §5.2.3 implies an additive contract on §8.2 (`skill_load` returns a pointer for pre-activated skills).
- [`event-bus-and-trace-catalog.md §6.6`](event-bus-and-trace-catalog.md) — `skill.loaded` payload; v3 reuses with no schema change but emits `load_reason="always"` from a new session-init path.
- the project strategy (private) — context > skills > model selection thesis; v3 specifies the second-largest lever inside the largest one.
- [`KNOWN_ISSUES.md`](../KNOWN_ISSUES.md) — the prompt-caching gap this spec closes.
