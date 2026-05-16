# Routing Engine Specification

**Status:** Draft v3.2
**Last updated:** 2026-05-08
**Owner:** _your name_

> **v3.2 changes:** Cancellation event sequence cross-reference updated to
> point at streaming-protocol's three-case model (§3.4).
>
> **v3.1 changes:** Auxiliary event `pattern.override_accepted` renamed to
> `route.overridden` to align with event-bus-and-trace-catalog.md §6.5b
> (preserves the one-`route.decided`-per-turn invariant). Delegation phase
> asymmetry documented (§6 preamble): the chain slot exists from Phase 1
> as a stub; the `delegate()` tool itself ships in Phase 4.
>
> **v3 changes:** Provider availability state machine made consistent (§4.5).
> Capability validation extended to tools, system prompt, structured output (§4.4).
> Predicate snapshot points pinned (§5.3). `skills_loaded_includes` renamed to
> `skills_matching_message_includes` (§5.3). Cost-efficiency divide-by-zero defined
> (§5.5). `insufficient_context` schema specified (§6.6). Worker memory/skill/visibility
> rules added (§6.2.1). Tier upgrade exhaustion behavior stated (§6.9). Workspace tiers
> require all three slots (§5.7). Mid-turn multiple swaps last-write-wins (§3.3).
> Various nits.
>
> *Throughout: paths shown use `~/.yourtool/` as a placeholder for the final config directory.*

---

## 1. Purpose

This document specifies the routing engine: the component that decides, for every turn in a session, which model handles it. The engine composes three modes — manual selection by the user, configured rules from a yaml policy, and agent-decided delegation — into a single ordered policy chain. It also defines the contract for `delegate()`, the tool by which a planner model invokes a sub-agent on a different tier.

Routing is the user-visible feature most likely to feel either magical or untrustworthy. A wrong choice wastes money or produces bad output; a silent override of user intent destroys trust. This spec aims to make every decision explainable, every override visible, and every failure mode predictable.

This spec depends on `canonical-message-format.md` for `Message`, `ToolDefinition`, and `AdapterCapabilities`.

---

## 2. Goals and non-goals

### 2.1 Goals

1. **Predictable.** For a given session state, message, and policy, the chosen model is deterministic.
2. **Turn-locked.** A turn's model is decided once, at turn start, and owns the entire turn including all tool cycles.
3. **Explainable.** Every routing decision is recorded with the full policy-chain trace. Users can ask "why this model?" and get an answer that fits on one screen.
4. **User intent prevails over system suggestions.** Pattern-store recommendations never silently override user-set rules. Disagreement is surfaced, not hidden.
5. **Resilient under provider failure.** A configured model becoming unavailable causes graceful chain fallthrough, not a turn failure.
6. **Safe under capability mismatch.** Routing rejects models that can't process the input and falls through.
7. **Hot-reloadable policy.** Editing the rule file takes effect on the next turn without restarting the server.
8. **Cheap.** Routing decisions add ≤5ms per turn at v1 scale (≤100 rules, ≤1000 fingerprints).

### 2.2 Non-goals

1. **Be a general-purpose policy engine.** No DSL, no Turing-complete rules. Closed predicate set.
2. **Be a model recommender.** The engine picks among configured models; it doesn't suggest models the user hasn't installed.
3. **Optimize cost globally.** Routing is per-turn. The only concession to global optimization is the daily-cost circuit breaker (§4.4).
4. **Replace user judgment.** Even agent-decided routing always defers to user-set policy.

---

## 3. Turn lifecycle and the lock

### 3.1 What a turn is

A turn begins when a USER message is added to the session and ends when the session manager observes an assistant message with `stop_reason: end_turn` (no further tool calls pending), or the user cancels.

Within a turn there can be many LLM calls and tool invocations:

```
USER message ─┐
              ├─ LLM call #1 → ASSISTANT (text + tool_use)
              │   └─ tool dispatch → TOOL message
              ├─ LLM call #2 → ASSISTANT (text + tool_use)
              │   └─ tool dispatch → TOOL message
              ├─ LLM call #3 → ASSISTANT (text, end_turn)  ◄── turn ends
              │
            (next USER message starts the next turn)
```

All LLM calls within one turn are part of the same `turn_id`.

### 3.2 The lock

**The model chosen at turn start owns the entire turn.** All LLM calls within the turn use that model, including tool-loop continuations. Re-routing happens only at turn boundaries.

Rationale:

- **Cost predictability.** A rule that resolves to Haiku at turn start can't silently re-route to Opus mid-tool-loop because `estimated_input_tokens` grew with tool results.
- **Behavioral predictability.** A configured rule that fired on the user's first message keeps applying for the duration of that message's turn, even as state changes.
- **Reasoning continuity.** The model accumulates context within a turn; switching mid-turn discards that and produces worse outputs.

The single exception is **delegation**: when a model calls `delegate()`, the worker runs in a *separate session* with its own routing decision (per §6). The parent turn's lock is unaffected; it resumes on the parent model when `delegate()` returns.

### 3.3 Mid-turn `/model` swaps

If the user runs `/model <id>` while a turn is in flight, the swap is queued and applies to the *next* turn. The TUI surfaces this:

```
Model swap pending: anthropic:claude-opus-4-7. Applies to next turn.
```

This is a deliberate UX choice — the alternative (cancel the in-flight turn and restart on the new model) is more disruptive than waiting one turn boundary.

If the user runs multiple `/model` commands during a single turn, last-write-wins: only the most recent pending swap takes effect at the next turn boundary. Earlier pending swaps are silently superseded; the TUI banner updates to reflect the latest target.

### 3.4 Cancellation and re-routing

User-cancellation (Ctrl-C) ends the turn early with `status: cancelled`. The next USER message starts a new turn and goes through fresh routing. Any queued model swap takes effect at that boundary.

The exact event sequence emitted on cancellation depends on where in the turn lifecycle the cancel arrives — see `streaming-protocol.md` §6.2 for the three cases (cancel during LLM streaming, cancel during tool dispatch, cancel at the seam). The routing engine itself does not emit cancellation events; it simply stops dispatching new LLM calls or tool calls for the cancelled turn and lets the session manager and adapters emit the canonical sequence.

---

## 4. The policy chain

### 4.1 Order

For each turn, at turn start, the engine runs policies in fixed order. The first policy returning a non-`None`, validated `RoutingDecision` wins:

```
1. PER_MESSAGE_OVERRIDE   — explicit @model syntax in the user's message
2. MANUAL_STICKY          — session.active_model set explicitly via /model
3. CONFIGURED_RULES       — first-match-wins over the rules list
4. PATTERN_RECOMMENDATION — pattern store result with confidence ≥ threshold
5. DELEGATE_REQUEST       — only present in the chain during a delegation re-entry; see §6
6. WORKSPACE_DEFAULT      — workspace-scoped default
7. GLOBAL_DEFAULT         — hardcoded fallback
```

(5) is conditional — it's in the chain only during a delegation. During normal turns it's skipped.

### 4.2 Why this order

User intent dominates. (1) is the most local user signal — "just this message." (2) is the session-level user signal. (3) is pre-declared user policy. (4) is system inference, ranked below user-set things by design. (5) handles delegation. (6) and (7) are floors.

This order is deliberate and stable. Reordering it (e.g., putting pattern recommendations above rules) would let learned behavior silently override user choices — the failure mode that destroys trust.

### 4.3 `MANUAL_STICKY` is opt-in

`MANUAL_STICKY` returns `None` unless the user has *explicitly* set a sticky model via `/model <id>` in the current session. A fresh session has no sticky and falls through to rules.

Setting a sticky is opt-out from rule-based routing for that session. Rationale: a user typing `/model haiku` is signaling "I want Haiku, ignore my rules." That signal should be honored.

To return to rule-based routing within a session, the user runs `/model -` (clears sticky).

### 4.4 Validation: capability *and* availability

Every policy that returns a candidate model passes through validation before becoming the winner:

```python
def validate(model: str, context: TurnContext) -> ValidationResult:
    caps = adapter_registry.capabilities(model)

    # Availability
    if not adapter_registry.is_configured(model):
        REJECT(reason="not_configured")          # missing API key, etc.
    if not adapter_registry.is_available(model):
        REJECT(reason="provider_unavailable")    # see §4.5

    # Capability — gated on whether the turn actually needs the capability
    if context.has_images and not caps.supports_images:
        REJECT(reason="no_vision_support")
    if context.estimated_input_tokens > caps.max_context_tokens:
        REJECT(reason="exceeds_context_window")
    if context.has_tool_definitions and not caps.supports_tools:
        REJECT(reason="no_tool_support")
    if context.has_system_prompt and not caps.supports_system_prompt:
        REJECT(reason="no_system_prompt_support")
    if context.requires_structured_output and not caps.supports_structured_output:
        REJECT(reason="no_structured_output_support")

    return OK
```

A rejected candidate causes the policy to be treated as if it returned `None`; the chain continues. Each rejection is recorded in the canonical `route.decided` event (§7) as part of the policy's evaluation.

The capability gates follow the "only require what we'll use" principle: a turn with no images doesn't need `supports_images`; a worker with no `output_schema` doesn't need `supports_structured_output`. This avoids spurious rejections that would force fallthrough when the model would actually have worked.

`supports_thinking` is *not* validated. Models that don't support thinking simply have thinking blocks dropped at adapter serialization time (per §7.3 of the canonical format spec).

> *This requires `AdapterCapabilities` (defined in `canonical-message-format.md` §7.2) to declare `supports_tools`, `supports_system_prompt`, and `supports_structured_output`. The canonical format spec must be updated to add these fields. Existing entries that don't yet declare them default to `true` for `supports_tools` and `supports_system_prompt` (the common case for both Anthropic and OpenAI) and `false` for `supports_structured_output`.*

### 4.5 Availability state machine

Availability is tracked at two granularities, both maintained by the adapter registry:

1. **Per-(provider, model)**, the default. Most outages affect a single model (a hot model rate-limited; a deprecated checkpoint returning errors).
2. **Per-provider**, escalated when failures suggest a provider-wide problem.

Each scope has the same three states:

- **Healthy** (default): treated as available.
- **Degraded**: failures observed but not enough to reject. *Phase 2 refinement; v1 treats Degraded as Healthy for routing purposes.*
- **Unavailable**: routing rejects with `provider_unavailable`. Auto-clears as defined below.

#### 4.5.1 Triggers

| Failure pattern                                                              | Scope marked Unavailable | Rationale                                |
|------------------------------------------------------------------------------|---------------------------|------------------------------------------|
| ≥5 consecutive failures on one `(provider, model)` within 2 minutes          | That `(provider, model)`  | Single-model issue (rate limit, deprecation). |
| ≥3 distinct models from one provider hit Unavailable within 2 minutes        | The whole provider        | Pattern points to a provider-wide issue. |
| Any auth error (401, 403) on any model from a provider                       | The whole provider        | Misconfigured key affects everything.    |
| ≥2 DNS / network errors reaching a provider's host within 30 seconds         | The whole provider        | Sustained connectivity loss, not model-specific. |
| A single transient DNS / network error reaching a provider's host            | None (counts toward the per-`(provider, model)` 5-strike threshold below) | One-off SSL renegotiation or TCP RST is not an outage. |
| Bounded exponential backoff exhausted inside a single adapter call           | No state change           | Per-call transient handling; not a signal of sustained outage. |

A successful call against a `(provider, model)` clears that scope's Unavailable state immediately. A successful call against any model from a provider clears the provider-wide Unavailable state *and* the sliding NETWORK-failure window.

**Why NETWORK is not immediate (refined 2026-05-16):** an earlier revision blacked the whole provider out on a single NETWORK error, on the theory that DNS / connectivity issues affect every model identically. In practice transient SSL handshake errors (`ssl.SSLError: SSLV3_ALERT_BAD_RECORD_MAC`, `httpx.ConnectError` mid-TLS-renegotiation, one-off TCP RST) reach the adapter as `ErrorClass.NETWORK` but represent a single failed connection rather than a sustained provider-side outage. The 5-minute auto-clear made one transient hiccup look like a 5-minute provider blackout. The 2-within-30-seconds threshold filters the one-off from the real outage: a genuine DNS / regional-network failure will produce a second NETWORK error well inside 30 seconds, while a one-off TLS glitch resolves on the next call.

The thresholds (`_NETWORK_PROVIDER_ESCALATION_THRESHOLD`, `_NETWORK_PROVIDER_ESCALATION_WINDOW_SECONDS`) live in [`availability.py`](../../packages/metis-core/src/metis_core/routing/availability.py) as module constants; AUTH still escalates immediately because a misconfigured key cannot be a one-off.

#### 4.5.2 Auto-clear

Without successful calls, Unavailable states auto-clear after 5 minutes of no attempts. This prevents the system from being permanently locked out of a provider that recovered while idle.

#### 4.5.3 Validation behavior

When a policy's chosen model is `m` from provider `p`:

- If `p` is provider-wide Unavailable → reject with `provider_unavailable` (provider-wide).
- Else if `(p, m)` is Unavailable → reject with `provider_unavailable` (model-specific).
- Else → pass.

Both rejection cases use the same `validation_failure` value (`provider_unavailable`); the `route.decided` event's reason field disambiguates ("anthropic:claude-opus-4-7 model-specific outage" vs. "all anthropic models temporarily unavailable").

#### 4.5.4 Banners

When a model-specific Unavailable causes fallthrough:
```
anthropic:claude-opus-4-7 currently unavailable. Routing fell through to anthropic:claude-sonnet-4-6.
```

When a provider-wide Unavailable causes fallthrough:
```
anthropic provider currently unavailable. Routing fell through to openai:gpt-5 (workspace default).
```

Banners clear when the corresponding state returns to Healthy.

### 4.6 No per-rule fallback lists

Rules do *not* carry fallback lists (`fallback: [model_a, model_b]`). All fallback is handled by chain fallthrough.

Rationale: per-rule fallback breaks the predictability invariant. If `rule_A` has fallbacks `[opus, sonnet, haiku]` and `rule_B` has different fallbacks, debugging "why this model?" becomes a search through multiple lists. Chain fallthrough keeps the explanation linear: each policy gets one shot, and the chain is short.

If a user wants a specific fallback order for a workspace, they encode it as additional rules:

```yaml
rules:
  - name: "deep for architecture"
    when: {message_matches: "architecture"}
    use: anthropic:claude-opus-4-7
  - name: "deep for architecture (sonnet fallback)"
    when: {message_matches: "architecture"}
    use: anthropic:claude-sonnet-4-6
```

The second rule fires only if the first's model is unavailable (validation rejects it, chain continues, second rule's predicate matches).

### 4.7 Hard failure

If every policy in the chain returns `None` or fails validation, the engine raises a hard error to the session manager. The TUI surfaces:

```
No model available for this turn.
  Tried: anthropic:claude-opus-4-7 (unavailable), anthropic:claude-sonnet-4-6 (unavailable)
  Run /model <id> to choose explicitly, or /rules check.
```

The turn is not started. The user must intervene (set a sticky, fix config, wait for provider recovery). Silently using a model the user didn't authorize is never acceptable.

### 4.8 Hot reload

The configured policy file is read fresh at the start of every turn. Cost: a yaml parse and validation pass, ~1ms for a typical file. The router caches the parsed structure keyed by file mtime to avoid re-parsing when nothing changed.

If the file is invalid (yaml syntax error, unknown predicate, unknown model), the router uses the last-known-good version and surfaces this in the TUI. Users diagnose with `/rules check`.

---

## 5. The configured rule format

### 5.1 File location and shape

```yaml
# ~/.yourtool/routing.yaml
schema_version: 1

global_default: anthropic:claude-sonnet-4-6

# Tier mapping for delegation (§6.10)
tiers:
  fast: anthropic:claude-haiku-4-5
  balanced: anthropic:claude-sonnet-4-6
  deep: anthropic:claude-opus-4-7

# Pattern store weighting (§5.5)
pattern:
  cost_weight: 0.05       # 0.0 = pure quality, 1.0 = pure cost (default 0.05)
  min_confidence: 0.05    # default 0.05 — scaled to match cost_weight=0.05 (see §5.5)
  min_sample_size: 5

rules:
  - name: "fast for commits"
    when:
      message_matches: "^/commit|write.*commit message"
    use: anthropic:claude-haiku-4-5

  - name: "deep for architecture"
    when:
      any_of:
        - message_matches: "(architecture|design review|security review)"
        - skills_matching_message_includes: "system_design"
    use: anthropic:claude-opus-4-7

  - name: "long context"
    when:
      estimated_input_tokens_gt: 80000
    use: anthropic:claude-opus-4-7

  - name: "budget circuit breaker"
    when:
      cost_today_exceeds_usd: 5.00
    use: anthropic:claude-haiku-4-5

workspaces:
  ~/code/myproject:
    default: openai:gpt-5
    pattern:
      cost_weight: 0.7    # this is the "ship reliable" workspace
    tiers:
      fast: openai:gpt-5-mini
      balanced: openai:gpt-5
      deep: openai:gpt-5     # no deeper option configured; deep == balanced here
    rules:
      - name: "this project uses gpt for SQL"
        when:
          file_extensions_in_context: [".sql"]
        use: openai:gpt-5
```

### 5.2 Rule evaluation

Within `rules`, top-to-bottom, first match wins. Each rule has a unique `name` (used for tracing and the `/rules` UI). Rules without `name` get a synthetic name `rule_<index>`.

Within `workspaces.{path}.rules`, same semantics, but workspace rules run *before* global rules. Workspace `default` and `pattern` config replace the corresponding global section for that workspace (full replacement, not merge — v1 simplification).

**Workspace `tiers` must define all three slots (`fast`, `balanced`, `deep`) or be omitted entirely.** A partial workspace tier map is rejected at validation time. Rationale: silent gaps in the tier map cause `delegate(tier="balanced")` calls inside the workspace to fail with `no_model_available_for_tier` for non-obvious reasons. Forcing all-or-nothing makes the failure mode at config time, not at runtime. If a workspace truly only wants to override one tier, it must restate the others (typically by copying the global mapping).

### 5.3 Predicate set

Closed set. Adding a predicate is a deliberate spec change.

#### 5.3.1 Snapshot points

Predicates evaluate against state captured at routing time, which is the start of a turn. Each predicate has a defined input source:

| Predicate                          | Reads from                                                                |
|------------------------------------|---------------------------------------------------------------------------|
| `message_matches`, `message_contains_any` | The new USER message of the current turn.                          |
| `estimated_input_tokens_*`         | The active adapter's `estimate_input_tokens()` against the messages that *would* be sent if the candidate model were chosen, including the assembled system prompt and tool definitions. |
| `has_images`                       | The new USER message of the current turn (image blocks).                  |
| `has_tool_calls_in_history`        | The session's canonical message store (any prior ASSISTANT message with `tool_use` blocks). |
| `skills_matching_message_includes` | The skill-description index, matched against the new USER message. The index is built at session start from frontmatter descriptions only — no skill bodies are loaded for this. |
| `file_extensions_in_context`       | File extensions appearing in any *tool input* or *tool result* of the current session (the file paths the agent has actually touched). User-message text is not scanned — too noisy. At the first turn this is always empty. |
| `workspace_path_matches`           | The session's workspace path, set at session creation.                    |
| `time_of_day_between`              | Wall clock at routing time, in the user's local timezone.                 |
| `cost_today_exceeds_usd`           | The accumulated cost across the user's sessions since UTC midnight.       |

#### 5.3.2 Predicate reference

| Predicate                          | Type          | Description                                                       |
|------------------------------------|---------------|-------------------------------------------------------------------|
| `message_matches`                  | regex         | Matches the user's turn message (the new USER message).           |
| `message_contains_any`             | [string]      | Any substring (case-insensitive) appears in the message.          |
| `estimated_input_tokens_gt`        | int           | `estimate_input_tokens()` exceeds threshold.                      |
| `estimated_input_tokens_lt`        | int           | `estimate_input_tokens()` is under threshold.                     |
| `has_images`                       | bool          | Any image block in this turn's user message.                      |
| `has_tool_calls_in_history`        | bool          | Any prior assistant message has tool_use blocks.                  |
| `skills_matching_message_includes` | [string]      | Skill name(s) whose description match the user message above threshold. |
| `file_extensions_in_context`       | [string]      | File types touched by tools in this session (case-insensitive).   |
| `workspace_path_matches`           | regex         | Workspace's absolute path matches.                                |
| `time_of_day_between`              | [HH:MM,HH:MM] | Local time falls in window. Wraps midnight: `[22:00, 06:00]`.     |
| `cost_today_exceeds_usd`           | float         | Sum of today's session costs (UTC midnight).                      |
| `any_of`                           | [predicate]   | Logical OR.                                                       |
| `all_of`                           | [predicate]   | Logical AND.                                                      |
| `not`                              | predicate     | Logical NOT.                                                      |

A `when` block with multiple top-level keys is implicitly `all_of`.

> *Note on `skills_matching_message_includes`: prior drafts of this spec called this predicate `skills_loaded_includes`. That name was misleading because skills are loaded by the context assembler, which runs **after** routing — at routing time no skills are "loaded" yet. The current name reflects what's actually checked: a fast match against the skill description index.*

### 5.4 The cost circuit breaker

`cost_today_exceeds_usd` is a first-class predicate. When it fires, the TUI surfaces:

```
Daily budget $5.00 exceeded ($5.42 today). Routing per "budget circuit breaker" rule.
```

Banner clears at UTC midnight reset.

### 5.5 Pattern recommendations and the cost/quality knob

The pattern policy queries the pattern store for the K nearest fingerprints (default K=10) to the current turn's fingerprint. Among the K neighbors, it groups by `outcome.primary_model`. For each model M in the cluster, it computes:

```
normalized_success_M       = sample-size-weighted mean(success_score) for neighbors
                              with primary_model = M, computed as
                                Σ(success_score_i × sample_size_i) / Σ(sample_size_i)
                              (already in 0..1; a neighbor row with 50 contributing
                              sessions weights 50× a single-shot row, so well-evidenced
                              outcomes dominate noisy one-offs)

if max_avg_cost_in_cluster == min_avg_cost_in_cluster:
    normalized_cost_efficiency_M = 0  for all models in the cluster
else:
    normalized_cost_efficiency_M = (max_avg_cost_in_cluster - avg_cost_M)
                                  / (max_avg_cost_in_cluster - min_avg_cost_in_cluster)
                                  (0..1; cheapest gets 1.0, most expensive gets 0.0)

score_M = (1 - cost_weight) × normalized_success_M
        + cost_weight × normalized_cost_efficiency_M
```

The degenerate case (all candidate models in the cluster have identical average cost) zeroes out the cost-efficiency term entirely, making the score reduce to `(1 - cost_weight) × normalized_success_M` — i.e., the decision falls to pure quality. This is the right behavior: there is no cost differentiation to weight.

`cost_weight` is configurable per workspace (default `0.05`, lowered from `0.1` on 2026-05-15 per the §A3-rev5 benchmark finding, which itself succeeded the `0.3 → 0.1` migration on 2026-05-14 — see "Default rationale" below). `cost_weight = 0` means "pure quality, ignore cost"; `cost_weight = 1` means "pure cost, ignore quality"; values in between blend.

The model with the highest aggregate score is the recommendation. The runner-up appears in `alternatives`.

```
confidence = (top_score - runner_up_score) / top_score
```

If `top_score == 0`, confidence is 0. The pattern policy returns `None` if `confidence < pattern.min_confidence` or `sample_size < pattern.min_sample_size`. Both are configurable.

**Default rationale.** The `cost_weight` default of 0.05 (was 0.1 from 2026-05-14, was 0.3 prior to 2026-05-14) is itself a value judgment — but a documented one. A user prototyping wants higher; a user shipping production code may want lower (closer to pure quality). The point is that the tradeoff is *visible*, not hidden, and the user can see and override it.

The default was lowered from 0.3 → 0.1 after the §A3-rev benchmark run (see `benchmarks/RESULTS.md`). At 0.3 the cost-efficiency term required a success delta of ~0.43 to flip the chooser when the cheapest model also scored 1.0 on cost_efficiency — larger than the 0.15–0.30 cluster-level quality deltas the LLM judge produced in real data. The result was slot 4 picking the cheaper model on every routed turn regardless of evidence. At 0.1 a quality delta of ~0.143 is enough to invert the ranking, which the observed deltas do clear. The scoring formula is unchanged — only the default of the blend constant moved. Workspaces that depended on the prior cost-bias must restate `cost_weight: 0.3` in their `routing.yaml`.

The default was lowered again from 0.1 → 0.05 on 2026-05-15 after the §A3-rev5 benchmark run. §A3-rev5 reproduced a separate failure mode: with the v2 HYBRID fingerprint wiring landed (Wave 11) and the §A3-rev3 defaults in place, slot 4 still picked haiku on all 17 routed Pass C turns — including `regex-with-edge-cases` where haiku rubric-fails on the hard "16 edge case tests" turn (q=0.19) and sonnet rubric-passes (q=1.00). Diagnosis: `cost_efficiency` normalizes per cluster to `[0.0, 1.0]`, so at `cost_weight=0.1` whichever model is cheapest gets a *flat* `+0.10` floor on its score regardless of cluster geometry. On the §A3-rev5 regex cluster (haiku q=0.91 vs sonnet q=1.00, cost_haiku ≪ cost_sonnet) this floor swamped the 0.09 quality delta and slot 4 picked haiku at conf=0.011 — gated off, falling to slot 7. Direct simulation against the `a3rev5-patterns.db` snapshot under cw=0.05 showed 6 sonnet picks pass the `min_confidence=0.05` gate where cw=0.10 produced 0; haiku-correct decisions on workloads with genuine quality dominance (multi-file-refactor q=0.79 vs 0.67; multi-turn-refactor q=1.00 vs 0.95) still pick haiku at high confidence. The scoring formula is unchanged — only the default of the blend constant moved. Workspaces that depended on the `0.1` cost bias restate `cost_weight: 0.1` in their `routing.yaml`. (Per-prompt sub-cluster partitioning was considered as an alternative wedge but found unnecessary: the K-NN already pulls 9 of 10 same-workload neighbors per cluster on §A3-rev5 data.)

The `min_confidence` default was lowered from 0.3 → 0.05 in the 2026-05-14 wave after the §A3-rev2 benchmark run. The two knobs are coupled: confidence is `(top_score - runner_up_score) / top_score`, and `score` itself is `(1 - cost_weight) * success + cost_weight * cost_efficiency`. Under the legacy `cost_weight=0.3`, the cost-efficiency term alone — independent of any quality delta — produced ~0.35 confidence on tied-quality clusters where the two models had different costs, so `min_confidence=0.3` acted as a noise gate without suppressing genuine signal. Under `cost_weight=0.1` the same tied-quality clusters produce ~0.10 confidence, and the legacy `0.3` gate suppresses real cluster inversions: §A3-rev2 Pass C turn 2 on `write-a-doc-from-notes` aggregated `sonnet=0.900` ahead of `haiku=0.842` (the first cluster-level inversion in any A3 series) with confidence `0.064`, and slot 4 emitted `not_applicable`. At `0.05` the gate scales down with the cost-weight reduction so real inversions can fire; cluster-empty / zero-score / fewer-than-K-cluster cases still gate off in `aggregation.py`. Workspaces that depended on the prior tighter gate restate `min_confidence: 0.3` in their `routing.yaml`. The 2026-05-15 `cost_weight 0.1 → 0.05` migration leaves `min_confidence=0.05` unchanged: under the new `cost_weight=0.05` the cost-floor effect on confidence drops further (~0.05 max contribution from cost_efficiency saturation alone), and the §A3-rev2 inversion-friendly ratio still clears the `0.05` gate.

### 5.6 Tie-breaking against configured rules

When both a rule and a pattern recommendation are available, the rule wins (per §4.1). The pattern recommendation is *not* discarded — it's recorded in the `route.decided` event as a deferred policy with its own evaluation.

In Phase 3, an opt-in feature surfaces high-confidence pattern disagreement to the user:

```yaml
pattern_disagreement:
  surface: true                    # default false
  min_confidence: 0.85
  min_sample_size: 20
```

When enabled and a pattern recommendation disagrees with the chosen rule above the thresholds, the TUI shows:

```
→ Routing to anthropic:claude-haiku-4-5 per rule "fast for commits"
  Pattern store suggests: anthropic:claude-sonnet-4-6 (confidence 0.87, 23 similar tasks)
  /route override   to use Sonnet for this turn
  /route ignore     to dismiss
```

The user's choice is itself an event (`route.overridden` for accept, `pattern.override_dismissed` for ignore), feeding back into pattern learning. See `event-bus-and-trace-catalog.md` §6.5b for payloads.

### 5.7 Validation

At load time, the router validates:

1. yaml is well-formed.
2. `schema_version` matches a supported version.
3. Every `use` and `default` references a model in the adapter registry.
4. Every tier (global and per-workspace) maps to a model in the registry.
5. Workspace `tiers` blocks define all three slots (`fast`, `balanced`, `deep`) or are absent. Partial maps are rejected.
6. Every predicate is in the closed set; every value is the right type.
7. Every regex compiles.
8. No `name` is duplicated (synthetic names excepted).
9. `pattern.cost_weight` is in `[0.0, 1.0]`; `min_confidence` in `[0.0, 1.0]`; `min_sample_size ≥ 1`.

A failure in any of these causes the file to be rejected as a whole — last-known-good is used. `/rules check` prints validation errors.

### 5.8 What rules cannot do

By design, rules cannot:

- Modify session state (set memory, load skills, etc.). Rules pick a model; nothing else.
- Reference message history beyond predicate evaluation.
- Chain (one rule firing causing another to evaluate differently).
- Define functions or variables. No DSL.

These constraints are how the engine stays predictable. Users wanting richer logic write a skill, not a rule.

---

## 6. The `delegate()` contract

> **Phase note.** The `delegate()` tool ships in **Phase 4**. The routing chain's `DELEGATE_REQUEST` policy slot (§4.1, position 5), however, exists from **Phase 1** as a stub that always returns `not_applicable`. This asymmetry is deliberate: the routing pipeline's shape is fixed across phases, and adding the slot's behavior later is filling in a stub rather than refactoring the chain. The catalog's `delegate.*` events likewise ship in Phase 4 (per `event-bus-and-trace-catalog.md` §6.8). Phase 1 implementations should:
>
> - Include `DELEGATE_REQUEST` in the policy chain enumeration so trace events have a consistent shape.
> - Not register the `delegate` tool with any model, regardless of `can_delegate` configuration.
> - Skip §6.1 through §6.10 of this document for implementation purposes; they describe the Phase 4 contract.

### 6.1 Tool signature

The `delegate` tool is registered automatically when a session's active model is on a tier marked as `can_delegate: true` (typically only `balanced` and `deep`). Its schema:

```python
delegate(
    tier: Literal["fast", "balanced", "deep"],
    task: str,                            # focused instruction for the worker
    context: ContextSpec,                 # see §6.3
    output_schema: dict | None = None,    # optional JSON schema for return
    allowed_tools: list[str] | None = None,  # default: same as planner's
    max_tokens: int | None = None,        # cap on worker output
) -> DelegateResult
```

Workers cannot delegate. The tool is not registered for sessions invoked as workers. This prevents fan-out and recursion in v1.

### 6.2 What the worker is

A worker is a fresh session — same architecture, same context assembler, same tool dispatcher — instantiated with curated context and run to completion. It has:

- Its own session id (tagged `parent_session_id` and `parent_tool_use_id`).
- The workspace's MEMORY.md and USER.md loaded.
- Skills loaded per the same logic as the planner's session (descriptions always; bodies on demand).
- Tools per `allowed_tools` (default: same set as the planner).
- A worker-specific system prompt instructing it to be terse, focused, and to return rather than ask for clarification.

### 6.2.1 Worker side-effects: what's read-only

To prevent workers from mutating state the planner is reasoning about, workers operate read-only against the following:

- **Memory.** Workers can read MEMORY.md and USER.md (these are loaded into the worker's system prompt) but cannot modify them. Memory-mutating tools (`memory_add`, `memory_replace`, `memory_consolidate`) are not registered for worker sessions, regardless of `allowed_tools`. Rationale: the planner has the broader context to judge what's worth remembering. A worker shouldn't change the planner's durable view of the world from inside a sub-task.
- **Skills.** Workers can read and load skills (`load_skill` is available). Workers cannot create, modify, or delete skill files. Skill auto-generation (Phase 2.5) does not run from worker sessions.
- **Routing config.** Workers cannot edit `routing.yaml` (no rule-management tool exists in v1, but if such a tool exists in a future phase, it is not registered for workers).

What workers *can* do (via their normal tool access): read files, write files, run shell commands, call any other tool the planner had — these are task-shaped operations, not durable system state.

### 6.2.2 Worker visibility in the UI

By default, worker sessions are not listed in `/history` or the dashboard's session list. They are visible:

- As cost rollups under the parent session (per §6.7).
- By drilling into a parent session's `delegate.completed` event, which carries `worker_session_id` and links to a full worker session view.
- Via `/history --include-workers`, which surfaces them as nested entries under their parents.

Rationale: at scale a single planner session can spawn many workers; flattening them into history clutters the user's view of their own work. They remain reachable for analytics and debugging.

Worker sessions are persisted with the same retention as parent sessions and follow the same trace-event rules (with `is_worker: true` flagged on `llm.call_started` events per the event catalog).

### 6.3 Context handoff: two modes only

`ContextSpec` is one of:

```python
# Mode 1: minimal — task brief only.
{"mode": "minimal"}

# Mode 2: explicit — references the planner specifies.
{"mode": "explicit", "include": [...]}
```

There is no "auto" mode in v1. The planner must decide what context the worker needs. This forces the planner to think about handoff, which produces more reliable results than letting the system guess.

`include` items in mode 2 can be:

```python
{"type": "file",         "path": "src/auth.ts"}                   # worker re-reads via file tool
{"type": "file_range",   "path": "src/auth.ts", "lines": [40,80]} # only those lines
{"type": "tool_result",  "tool_use_id": "tu_01HZ100"}             # past tool call result
{"type": "message",      "message_id": "01HZ_..."}                # specific past turn
{"type": "inline",       "label": "decision", "text": "..."}      # planner-authored note
```

Files referenced by path are *not* inlined at delegation time — they appear as references the worker re-reads through the file tool. This keeps planner→worker handoff cheap and ensures the worker reads current file content.

Tool results, message content, and inline notes *are* inlined (the planner has already curated them).

### 6.4 When to use which mode

The planner's system prompt teaches:

- **`minimal`** — for self-contained mechanical tasks. "Format this JSON." "Generate a UUID." "Convert these dates from ISO to RFC 2822." The worker doesn't need session context.
- **`explicit`** — for tasks needing prior context. "Refactor the function defined in `src/auth.ts:authenticate`. Use the JWT approach we decided on (see message `01HZ_...`)."

If the planner is unsure which to use, default to `explicit` with the relevant references. A worker can fail and report `insufficient_context` (§6.6) — the planner can then retry with more references or take over.

### 6.5 The system prompt the worker sees

```
You are a sub-agent invoked by another agent (the "planner") to handle
a specific sub-task. You are running on model {worker_model}.

Your task:
{task}

{context_summary}

Guidelines:
- Focus only on this task. Do not pursue tangents.
- If you need information not provided, use the available tools to fetch it.
- Do not ask the user for clarification. If clarification is needed, return
  a structured response indicating what's missing.
- Be terse. The planner will see your final output and integrate it.
- {if output_schema:} Your final response must conform to this schema:
  {output_schema}
- Return when complete. Do not produce a summary unless asked.

Available tools: {allowed_tools}
```

`{context_summary}` lists included files and references with brief descriptions.

### 6.6 Return value and failure modes

```python
class DelegateResult:
    success: bool
    output: str | dict           # text by default; dict if output_schema specified
    error: str | None
    usage_summary: dict          # tokens, cost, turn count, tool calls made
    worker_session_id: str       # for trace lookup
```

Failure modes:

| Failure                                     | success | error                                | output                  |
|---------------------------------------------|---------|--------------------------------------|-------------------------|
| Worker raised an unhandled error            | false   | `"worker_error: {message}"`          | partial output if any   |
| Worker hit `max_tokens`                     | false   | `"max_tokens_exceeded"`              | truncated output        |
| Worker requested missing context            | false   | `"insufficient_context"`             | InsufficientContextRequest (see below) |
| Worker output didn't match `output_schema`  | false   | `"output_schema_validation_failed"`  | raw output              |
| No model available for `tier`               | false   | `"no_model_available_for_tier"`      | empty                   |
| User cancelled the planner mid-delegation   | false   | `"cancelled_by_user"`                | partial if any          |

The planner decides what to do: retry with `tier: "deep"`, take over directly, or surface to the user.

#### 6.6.1 The `insufficient_context` schema

When a worker determines it cannot complete the task without more information, it returns a structured request rather than free text. This lets the planner programmatically retry with additional context rather than re-prompting itself.

```python
class InsufficientContextRequest:
    missing: list[MissingItem]
    summary: str                  # one-sentence human-readable description

class MissingItem:
    type: Literal["file", "file_range", "message", "tool_result", "decision", "other"]
    ref: str                      # path, message_id, tool_use_id, or human-readable label
    hint: str                     # what the worker would do with this; helps planner judge

# Example:
{
    "missing": [
        {"type": "file", "ref": "src/auth/jwt.ts",
         "hint": "need to see the current JWT signing logic to refactor it"},
        {"type": "decision", "ref": "token expiration policy",
         "hint": "need to know the agreed-upon expiration window"}
    ],
    "summary": "Need the current JWT implementation and the team's expiration policy."
}
```

The worker's system prompt teaches it to return this shape via a special `_request_context` tool that it calls to terminate the worker session with this payload. The planner sees the structured request as the `output` field of the `DelegateResult` when `error == "insufficient_context"`.

Planner behavior on `insufficient_context`:

1. Inspect `missing`. If references can be resolved (files exist, decisions are recorded in MEMORY.md, etc.), retry `delegate()` with those references added to `context.include`.
2. If references cannot be resolved (the worker needs information neither side has), the planner either takes over the task directly or surfaces to the user.

### 6.7 Cost accounting

Worker token usage is attributed to the worker session, but rolled up into the parent session's totals for user-facing displays. Trace events include both `session_id` (the worker's own) and `parent_session_id`. The dashboard shows planner cost vs. worker cost broken out:

```
Session sess_42 — total $0.34
├─ planner (anthropic:claude-opus-4-7): $0.21, 12 turns
└─ workers: $0.13, 4 delegations
   ├─ tu_01HZ_a → anthropic:claude-haiku-4-5: $0.02, 1 turn
   ├─ tu_01HZ_b → anthropic:claude-haiku-4-5: $0.04, 2 turns
   ├─ tu_01HZ_c → openai:gpt-5-mini: $0.03, 1 turn
   └─ tu_01HZ_d → anthropic:claude-haiku-4-5: $0.04, 2 turns
```

### 6.8 The model registry's `can_delegate` flag

```yaml
# part of the model registry config
models:
  anthropic:claude-opus-4-7:
    tier: deep
    can_delegate: true
  anthropic:claude-sonnet-4-6:
    tier: balanced
    can_delegate: true
  anthropic:claude-haiku-4-5:
    tier: fast
    can_delegate: false
```

`can_delegate: false` means the `delegate` tool is not registered when this model is the active session model. Enforced at session start; updated when the active model changes.

### 6.9 Re-entry into the routing pipeline

When `delegate(tier=...)` is called, the routing engine resolves `tier` to a concrete model by consulting the `tiers` config. That resolved model is the worker's candidate. It enters the policy chain at the `DELEGATE_REQUEST` slot.

Capability validation still applies. If the resolved tier model fails validation (e.g., the task involves images and the fast model is text-only), the engine upgrades along `fast → balanced → deep` and re-validates at each step. If the requested tier was already `deep` and `deep` fails, or if `deep` itself fails after upgrades, `delegate` returns `no_model_available_for_tier` — there is no tier above `deep` to escalate to.

The worker's turn itself is still turn-locked (§3.2). The worker session can have multiple internal LLM calls and tool cycles, all on the same model.

#### 6.9.1 Why the full chain runs for workers

The policy chain runs end-to-end for worker sessions, including policies that cannot apply to workers by construction (`PER_MESSAGE_OVERRIDE` — workers don't have user messages with `@` prefixes; `MANUAL_STICKY` — workers have no user `/model` command; `CONFIGURED_RULES` — *can* match against the worker's task brief and would fire if the user wrote rules targeting it, though this is rare).

The alternative — skipping inapplicable policies in worker mode — produces a cleaner trace but adds a special case to the routing pipeline. A reviewer might prefer either; this spec keeps the chain uniform for predictability. The `route.decided` event traces for workers will show `not_applicable` verdicts on the user-facing policies, which is the trade-off.

### 6.10 Tier definitions

`tier` is an abstraction over concrete models, mapped per-workspace via `tiers` config (§5.1).

| Tier      | Intent                                                                   |
|-----------|--------------------------------------------------------------------------|
| `fast`    | Cheap, low-latency. Used for mechanical sub-tasks.                       |
| `balanced`| Capable enough for most coding work. Default for medium tasks.           |
| `deep`    | The most capable available. Used for planning and reasoning.             |

Tier resolution falls back through workspace → global → registry default. A misconfigured tier (no model assigned) causes `delegate(tier=X)` to return `no_model_available_for_tier`.

---

## 7. The canonical `route.decided` event

Every turn produces exactly one `route.decided` event, emitted at turn start after the chain runs. This is the source of truth for "why this model?"

### 7.1 Shape

```python
class RouteDecidedEvent:
    # Standard event envelope (see event-bus spec)
    type: Literal["route.decided"]
    timestamp: datetime
    session_id: str
    turn_id: str

    # Routing-specific payload
    chain: list[PolicyEvaluation]    # one entry per policy in chain order
    winner_index: int                 # index into chain
    chosen_model: str
    elapsed_ms: float

class PolicyEvaluation:
    policy: str                       # PER_MESSAGE_OVERRIDE | MANUAL_STICKY | RULE
                                      # | PATTERN | DELEGATE_REQUEST
                                      # | WORKSPACE_DEFAULT | GLOBAL_DEFAULT
    verdict: Verdict                  # NOT_APPLICABLE | DEFERRED | REJECTED | CHOSE
    candidate_model: str | None       # what this policy proposed (None if NOT_APPLICABLE)
    reason: str                       # human-readable

    # Mode-specific extras
    rule_name: str | None             # for RULE
    confidence: float | None          # for PATTERN
    pattern_alternatives: list[ModelOption] | None  # for PATTERN

    # Validation outcome (if candidate_model was set)
    validation_failure: str | None    # "no_vision_support" | "exceeds_context_window"
                                      # | "no_tool_support" | "no_system_prompt_support"
                                      # | "no_structured_output_support"
                                      # | "provider_unavailable" | "not_configured"
                                      # | None if validation passed

class ModelOption:
    model: str
    score: float                      # the aggregate score from §5.5
    sample_size: int                  # how many neighbors used this model

class Verdict(StrEnum):
    NOT_APPLICABLE = "not_applicable"  # policy didn't have anything to say (e.g. no override token)
    DEFERRED       = "deferred"         # policy had a candidate but a higher-priority policy won
    REJECTED       = "rejected"         # candidate failed validation, chain continued
    CHOSE          = "chose"            # this policy won
```

### 7.2 Why one event, not many

Earlier drafts had separate events for `routing.constraint_failure`, `routing.rule_skipped`, `routing.policy_invalid`. Folding them into a single `route.decided` event means:

- One database row to look up "why did this turn pick this model?"
- The dashboard renders one view, not a join across event types.
- The full chain trace is atomic with the decision; no risk of missing related events.

`routing.policy_invalid` (the rule file failed to load) remains a separate session-level event since it's not turn-scoped.

### 7.3 Auxiliary events

These remain separate events because they describe distinct user actions or worker lifecycle:

| Event type                       | When                                                          |
|----------------------------------|---------------------------------------------------------------|
| `route.overridden`               | User chose `/route override` (turn re-dispatched on pattern's choice). |
| `pattern.override_dismissed`     | User chose `/route ignore` (turn proceeds with original choice). |
| `delegate.started`               | A `delegate` tool call began. Includes worker_session_id.     |
| `delegate.completed`             | The worker session ended.                                     |
| `delegate.failed`                | The worker session failed; includes failure mode (§6.6).      |
| `routing.policy_invalid`         | The rule file failed to load; last-known-good in use.         |
| `routing.provider_unavailable`   | Provider state transitioned to Unavailable.                   |
| `routing.provider_recovered`     | Provider state transitioned back to Healthy.                  |

All events conform to the schema defined in `event-bus-and-trace-catalog.md`.

### 7.4 How the dashboard uses it

The "why this model?" view is a single record render:

```
Turn 01HZ_xyz · session sess_42 · 2026-05-08T14:23:11Z
Chose: anthropic:claude-sonnet-4-6                           (workspace default)

Chain:
  [1] PER_MESSAGE_OVERRIDE   not_applicable  no @model token in message
  [2] MANUAL_STICKY          not_applicable  no sticky model set
  [3] CONFIGURED_RULES       rejected        rule "deep for architecture" matched →
                                             anthropic:claude-opus-4-7 (provider_unavailable)
  [4] PATTERN_RECOMMENDATION deferred        suggested anthropic:claude-sonnet-4-6
                                             (confidence 0.71, 14 samples) — outranked by rule
  [5] WORKSPACE_DEFAULT      chose           anthropic:claude-sonnet-4-6
```

The TUI's `/model show` command prints the same trace inline.

---

## 8. Worked examples

### 8.1 Manual sticky

```
session.active_model = "anthropic:claude-sonnet-4-6"  (user ran /model sonnet)
user: "Refactor this function."

Chain:
  PER_MESSAGE_OVERRIDE   not_applicable
  MANUAL_STICKY          chose → anthropic:claude-sonnet-4-6 (validates)
```

### 8.2 Rule match

```
session.active_model = None  (no /model run)
rules: [{name: "fast for commits", when: {message_matches: "^/commit"}, use: haiku}]
user: "/commit fix the auth bug"

Chain:
  PER_MESSAGE_OVERRIDE   not_applicable
  MANUAL_STICKY          not_applicable
  CONFIGURED_RULES       chose → anthropic:claude-haiku-4-5 (rule "fast for commits")
```

### 8.3 Per-message override beats sticky

```
session.active_model = "anthropic:claude-sonnet-4-6"
user: "@haiku what's a quick name for this variable?"

Chain:
  PER_MESSAGE_OVERRIDE   chose → anthropic:claude-haiku-4-5 (override "@haiku")
```

`session.active_model` is unchanged after this turn.

### 8.4 Pattern recommendation, no rule

```
session.active_model = None
rules: []
pattern store: returns sonnet, confidence 0.78, sample 12

Chain:
  PER_MESSAGE_OVERRIDE   not_applicable
  MANUAL_STICKY          not_applicable
  CONFIGURED_RULES       not_applicable  (no rules match)
  PATTERN_RECOMMENDATION chose → anthropic:claude-sonnet-4-6 (confidence 0.78, 12 samples)
```

### 8.5 Model-specific outage causes chain fallthrough

```
rules: [{name: "deep for architecture", when: {message_matches: "architecture"}, use: opus}]
workspace_default: anthropic:claude-sonnet-4-6
provider state: (anthropic, claude-opus-4-7) — Unavailable
                anthropic provider-wide — Healthy
                (anthropic, claude-sonnet-4-6) — Healthy
user: "Walk me through the architecture of this codebase"

Chain:
  PER_MESSAGE_OVERRIDE   not_applicable
  MANUAL_STICKY          not_applicable
  CONFIGURED_RULES       rejected → opus (provider_unavailable, model-specific)
  PATTERN_RECOMMENDATION not_applicable  (insufficient samples)
  WORKSPACE_DEFAULT      chose → anthropic:claude-sonnet-4-6 (validates)

TUI banner:
  anthropic:claude-opus-4-7 currently unavailable. Routing fell through to anthropic:claude-sonnet-4-6.
```

### 8.6 Provider-wide outage hits hard failure

```
rules: [{name: "default override", when: {}, use: opus}]
workspace_default: anthropic:claude-sonnet-4-6
global_default: anthropic:claude-haiku-4-5
provider state: anthropic provider-wide — Unavailable (auth error triggered escalation)
no other providers configured

Chain:
  PER_MESSAGE_OVERRIDE   not_applicable
  MANUAL_STICKY          not_applicable
  CONFIGURED_RULES       rejected → opus (provider_unavailable, provider-wide)
  PATTERN_RECOMMENDATION not_applicable
  WORKSPACE_DEFAULT      rejected → sonnet (provider_unavailable, provider-wide)
  GLOBAL_DEFAULT         rejected → haiku (provider_unavailable, provider-wide)

Hard failure. Turn does not start.

TUI:
  No model available for this turn.
    anthropic provider currently unavailable.
    Tried: opus, sonnet, haiku — all on anthropic.
    Run /model <id> to choose a model from a configured provider, or wait for recovery.
```

(The TUI message dynamically lists alternative *configured* providers; if none exist, it omits the suggestion.)

### 8.7 Capability rejection

```
turn: estimated_input_tokens = 90000, has_images = true
rules: [{name: "long context", when: {estimated_input_tokens_gt: 80000}, use: haiku}]
                                  (haiku has 200k context but no vision support)
workspace_default: opus

Chain:
  CONFIGURED_RULES       rejected → haiku (no_vision_support)
                         (rule matched but candidate failed validation)
  WORKSPACE_DEFAULT      chose → opus (supports images, 200k context)
```

### 8.8 Rule wins, pattern recommendation deferred (and surfaced)

```
session.active_model = None
rules: [{name: "fast for commits", when: {message_matches: "^/commit"}, use: haiku}]
pattern store: sonnet at confidence 0.87, sample 23
pattern_disagreement.surface = true
user: "/commit fix the auth bug"

Chain:
  CONFIGURED_RULES       chose → anthropic:claude-haiku-4-5 (rule "fast for commits")
  PATTERN_RECOMMENDATION deferred → anthropic:claude-sonnet-4-6
                          (confidence 0.87, 23 samples — outranked by rule)

TUI:
  → Routing to anthropic:claude-haiku-4-5 per rule "fast for commits"
    Pattern store suggests anthropic:claude-sonnet-4-6 (confidence 0.87, 23 tasks)
    /route override   to use Sonnet for this turn
    /route ignore     to dismiss
```

### 8.9 Turn-locked model through tool cycles

```
Turn start at T=0:
  user: "Read README.md and summarize"
  Chain: CONFIGURED_RULES → sonnet (rule "balanced for reads")
  Lock: sonnet for entire turn.

T=1.2s: sonnet emits tool_use (read_file)
T=1.5s: tool dispatcher returns TOOL message with file content
T=1.6s: LLM call #2 (still sonnet, lock holds): emits tool_use (read_file for table of contents)
T=2.0s: TOOL message
T=2.1s: LLM call #3 (still sonnet): emits final summary, stop_reason=end_turn
        Turn ends.

route.decided event has chain trace from T=0.
No re-routing happens between LLM calls #1, #2, #3.
```

### 8.10 Mid-turn `/model` swap is queued

```
Turn N in flight. User runs `/model opus` while sonnet is mid-tool-loop.

Server response (TUI banner): "Model swap pending: anthropic:claude-opus-4-7. Applies to next turn."

Turn N completes on sonnet (lock holds).
Turn N+1 starts:
  Chain: MANUAL_STICKY → opus (newly set)
```

### 8.11 Delegation re-entry

```
Active planner model: anthropic:claude-opus-4-7
Planner emits: delegate(tier="fast", task="rename `foo` to `bar` in src/", context={"mode": "minimal"})

Worker session creation triggers routing:
  Chain (in worker context):
    PER_MESSAGE_OVERRIDE   not_applicable
    MANUAL_STICKY          not_applicable
    CONFIGURED_RULES       not_applicable  (rules evaluated against worker's "user message" = task brief)
    PATTERN_RECOMMENDATION not_applicable  (insufficient context)
    DELEGATE_REQUEST       chose → anthropic:claude-haiku-4-5 (tier=fast resolves)

Worker runs to completion on haiku. Returns to planner via delegate tool result.
Planner's lock on opus is preserved through the delegation.
```

### 8.12 Daily budget circuit breaker (rule order matters)

```
rules:
  - name: "deep for architecture"
    when: {message_matches: "architecture"}
    use: opus
  - name: "budget cap"
    when: {cost_today_exceeds_usd: 5.00}
    use: haiku

cost_today = $5.42
user: "Walk me through the architecture..."

Chain:
  CONFIGURED_RULES       chose → opus (rule "deep for architecture" matched first)

This is wrong if the user wants the budget cap to win. Reorder:

rules:
  - name: "budget cap"
    when: {cost_today_exceeds_usd: 5.00}
    use: haiku
  - name: "deep for architecture"
    when: {message_matches: "architecture"}
    use: opus

Now:
  CONFIGURED_RULES       chose → haiku (rule "budget cap" matched first)
```

`/rules check` can detect obvious shadowing (an earlier rule's predicates strictly subsume a later rule's), but rule ordering remains the user's contract.

---

## 9. CLI / TUI surface

### 9.1 Slash commands

| Command                       | Effect                                                              |
|-------------------------------|---------------------------------------------------------------------|
| `/model <id>`                 | Set session sticky model (opt-out from rules).                      |
| `/model -`                    | Clear sticky; next turn uses default policy chain.                  |
| `/model show`                 | Print active model and the last turn's full chain trace.            |
| `/route override`             | (When pattern-disagreement is surfaced) use the pattern's choice.   |
| `/route ignore`               | (When pattern-disagreement is surfaced) dismiss the suggestion.     |
| `/rules check`                | Validate the routing.yaml file; print errors or "ok".               |
| `/rules show`                 | Print the active rule list (post-validation, with synthetic names). |
| `/rules reload`               | Force re-read of routing.yaml (normally automatic).                 |
| `/cost`                       | Print this session's cost broken down by model and role.            |

### 9.2 Per-message override syntax

A user message starting with `@<alias>` (e.g., `@haiku`, `@opus`, `@sonnet`) is parsed as a per-message override. The token is stripped before sending to the model. Aliases are resolved against the alias table; unknown aliases produce an inline error and the turn does not start.

Aliases live in the model registry config (alongside `tier` and `can_delegate` per §6.8). Each model entry can declare any number of aliases:

```yaml
models:
  anthropic:claude-haiku-4-5:
    tier: fast
    can_delegate: false
    aliases: [haiku, fast]
  anthropic:claude-sonnet-4-6:
    tier: balanced
    can_delegate: true
    aliases: [sonnet, balanced]
  anthropic:claude-opus-4-7:
    tier: deep
    can_delegate: true
    aliases: [opus, deep]
```

Aliases must be unique across the registry; duplicates are rejected at validation.

The override syntax must be at the start of the message and followed by whitespace. `Email me @haiku tomorrow` is not an override.

Edge case: a user wanting to ask the agent about the literal string `@haiku` at message start can prefix with a backslash (`\@haiku`). The backslash is stripped; no override is applied.

---

## 10. Testing strategy

### 10.1 Required tests

1. **Pipeline order.** For every pair of policies, construct a state where both can fire; verify the higher-priority one wins.
2. **First-match-wins within rules.** Rules with overlapping predicates; verify only the first matches.
3. **Turn lock.** A turn with multiple LLM calls and tool cycles; verify all use the same model. Mid-turn `/model` swap is queued, not applied.
4. **Hot reload.** Edit the rule file mid-session; verify the next turn uses new rules.
5. **Validation rejection.** Load files with each kind of error; verify each is rejected with a clear message.
6. **Capability fallthrough.** Construct a turn where a rule's chosen model fails capability validation; verify the chain continues and `route.decided` records the rejection.
7. **Provider unavailability fallthrough.** Stub a provider as Unavailable; verify the chain falls through and the event records `provider_unavailable`.
8. **Hard failure.** Stub all configured providers as Unavailable; verify the turn does not start and the user is told why.
9. **Pattern below threshold.** Stub the pattern store to return low confidence; verify the policy returns None.
10. **Pattern disagreement surfacing.** Enable opt-in; verify TUI message and the events emitted by `/route override` and `/route ignore`.
11. **Cost weight effect.** With cost_weight=0 (pure quality) vs. cost_weight=1 (pure cost), verify pattern recommendations differ as expected on a fixture cluster.
12. **Delegation tier resolution.** Configure a tier; call `delegate(tier=...)`; verify the worker uses the resolved model.
13. **Delegation capability upgrade.** Worker task has images, fast tier model is text-only; verify automatic upgrade.
14. **Workers cannot delegate.** A worker session does not have the `delegate` tool registered.
15. **Budget circuit breaker.** Set today's cost above the threshold; verify the rule fires (when ordered first).
16. **Cost rollup across delegations.** Run a session with several delegations; verify dashboard cost matches the sum of `usage.cost_usd`.
17. **`route.decided` completeness.** Every turn emits exactly one `route.decided` event with chain length equal to the number of policies that actually ran.
18. **Tool-capability gate.** Turn has tool definitions, candidate model has `supports_tools=false`. Verify rejection with `no_tool_support` and chain continues.
19. **Per-(provider, model) availability.** Mark Anthropic Opus as Unavailable; Sonnet remains Healthy. Verify a rule pointing at Opus falls through; a rule pointing at Sonnet succeeds.
20. **Provider-wide escalation on auth.** Inject an auth error on one Anthropic call; verify the entire `anthropic` provider is marked Unavailable on the next routing decision.
21. **Provider-wide escalation on multi-model failures.** Mark three distinct Anthropic models Unavailable within 2 minutes; verify the provider transitions to provider-wide Unavailable.
22. **Tier exhaustion at deep.** Worker's task triggers an upgrade chain that exhausts at deep (deep also rejected). Verify `delegate` returns `no_model_available_for_tier`.
23. **Workspace partial tiers rejected at validation.** A workspace defining only `fast` is rejected by `/rules check`.
24. **Worker memory write rejected.** A worker attempts `memory_add`; the tool is not registered; the call fails as "unknown tool" or equivalent.
25. **Cost-efficiency degenerate cluster.** Construct a pattern cluster where all candidate models have identical avg cost; verify scoring reduces to pure quality and does not raise.
26. **`insufficient_context` shape.** Worker returns a structured `InsufficientContextRequest`; the planner's `delegate.failed` event payload validates against the schema.
27. **Multiple mid-turn `/model` swaps.** User runs `/model A` then `/model B` within one turn; verify only B is applied at the next turn boundary, and the banner reflects B.

### 10.2 Property tests

Worth investing in for two specific properties:

- **Determinism:** Same state + same policy + same message → same decision.
- **Predicate evaluation closure:** Random combinations of `any_of`/`all_of`/`not` over the predicate set always evaluate to a bool, never raise.

---

## 11. Open questions

Tracked here, deferred to later revisions:

1. **Nested workspace matching.** A workspace inside another; whose rules apply? v1: closest path match wins; symlink ambiguity deferred.
2. **Cross-session pattern weighting.** Should very recent sessions weight more in K-nearest aggregation? v1: equal weighting. Phase 3 may add recency decay.
3. **Multi-tier delegation depth.** v1 disallows worker-as-planner. Phase 4 may allow bounded recursion.
4. **Streaming for delegation.** Planner currently waits for the worker to fully complete. Streaming worker output back is deferred.
5. **Scheduled rules.** Weekly/monthly windows. v1 has only daily.
6. **Pattern store influencing tier resolution.** `delegate(tier="fast")` could consult patterns to pick among fast-tier models. v1: configured `fast` model is used.
7. **Provider availability state machine.** v1 is binary (Healthy / Unavailable). The Degraded state is sketched but unused; refinement deferred.
8. **`/rules check` shadow detection.** v1 prints rules; v2 may detect when one rule strictly shadows another and warn.

---

## 12. Decision log

| Date       | Decision                                                              | Rationale                                                                                  |
|------------|-----------------------------------------------------------------------|--------------------------------------------------------------------------------------------|
| 2026-05-08 | Turn-locked model; re-routing only at turn boundaries                 | Cost predictability; behavioral predictability; reasoning continuity within a turn.        |
| 2026-05-08 | Mid-turn `/model` swap is queued, not applied                         | Less disruptive than cancelling the in-flight turn.                                        |
| 2026-05-08 | First-match-wins for rules; user orders the list                      | Predictable. Specificity ordering is a debugging nightmare.                                |
| 2026-05-08 | User-set policy beats pattern recommendations by default              | Trust. Silent override of user rules destroys confidence in the engine.                    |
| 2026-05-08 | Pattern disagreement surfaced as suggestion (Phase 3 opt-in)          | Lets the system improve on user rules without overriding them.                             |
| 2026-05-08 | Capability validation per-policy, fall through on failure             | Routing is resilient: a bad rule choice doesn't kill the turn.                             |
| 2026-05-08 | Provider availability tracked at adapter; routing rejects Unavailable | Outages produce graceful fallthrough, not turn failures.                                   |
| 2026-05-08 | No per-rule fallback lists; chain fallthrough is the answer           | Per-rule fallback breaks linearity of "why this model?" debugging.                         |
| 2026-05-08 | Hard failure when no policy succeeds; surface to user                 | Never silently use a model the user didn't authorize.                                      |
| 2026-05-08 | Closed predicate set, no DSL                                          | Predicates are contracts; users wanting more write skills, not rules.                      |
| 2026-05-08 | `cost_today_exceeds_usd` first-class predicate                        | Budget enforcement is the most common reason users add rules.                              |
| 2026-05-08 | Delegation context: `minimal` and `explicit` only; no `auto`          | Forces planners to think about handoff; produces more reliable results than guessing.      |
| 2026-05-08 | Workers cannot delegate                                               | Prevents fan-out cost explosions and recursion.                                            |
| 2026-05-08 | Tiers as abstraction over concrete models                             | Decouples planner reasoning from provider choices.                                         |
| 2026-05-08 | Hot reload on every turn                                              | Cheap; eliminates "I edited the file but nothing changed" frustration.                     |
| 2026-05-08 | Pattern recommendations require min sample size and min confidence    | Cold start safety; prevents routing on noise.                                              |
| 2026-05-08 | `cost_weight` configurable per workspace                              | Cost/quality tradeoff differs by workflow; defaulting to a hidden tradeoff is wrong.       |
| 2026-05-08 | One canonical `route.decided` event per turn                          | Single record answers "why this model?" without joins; full chain trace is atomic.         |
| 2026-05-08 | Availability tracked at `(provider, model)` with provider-wide promotion | Single-model outages don't blackout a provider; auth/DNS errors do.                     |
| 2026-05-08 | Capability validation extended to tools, system prompt, structured output | Required for Ollama and other limited models; "only require what we'll use" prevents spurious rejections. |
| 2026-05-08 | Predicate `skills_loaded_includes` renamed to `skills_matching_message_includes` | Routing runs before context assembly; "loaded" was a lie.                          |
| 2026-05-08 | Workspace `tiers` must define all three slots or be absent            | Partial tier maps cause runtime `no_model_available_for_tier` for non-obvious reasons; fail at config time. |
| 2026-05-08 | Workers are read-only against memory and skill state                  | Planner has the broader context; sub-tasks shouldn't mutate durable state.                 |
| 2026-05-08 | Worker sessions hidden from `/history` by default                     | A planner can spawn many workers; flat listing clutters the user's view.                   |
| 2026-05-08 | `insufficient_context` returns a structured `InsufficientContextRequest` | Planner can programmatically retry with targeted references rather than re-prompting itself. |
| 2026-05-08 | Cost-efficiency degenerate case (all costs equal) zeros the term      | Decision falls cleanly to pure quality when there's no cost differentiation.               |
| 2026-05-14 | `cost_weight` default lowered from 0.3 → 0.1                          | §A3-rev showed 0.3 required a ~0.43 quality delta to flip the chooser, swamping the 0.15–0.30 cluster-level deltas the LLM judge actually produces. 0.1 needs ~0.143, which observed deltas clear. |
| 2026-05-15 | `cost_weight` default lowered from 0.1 → 0.05                         | §A3-rev5 showed `cost_efficiency` normalizes per cluster to `[0.0, 1.0]`, so cw=0.1 added a flat +0.10 floor to whichever model was cheapest, swamping the 0.05–0.09 quality deltas observed on `regex-with-edge-cases` (haiku 0.91 / sonnet 1.00) and `fix-a-bug-small`. cw=0.05 halves the floor; direct simulation showed 6 sonnet picks pass the gate where cw=0.1 produced 0; haiku-correct decisions on workloads with q-delta ≥0.1 still pick haiku. |
| 2026-05-08 | Tier upgrade exhausts at `deep`; no escape above it                   | Explicit failure mode rather than implicit infinite loop.                                  |
| 2026-05-08 | Pattern override emits `route.overridden`, not `pattern.override_accepted` | Aligns with event-bus catalog; preserves one-`route.decided`-per-turn invariant.       |
| 2026-05-08 | Delegation slot in Phase 1; `delegate()` tool in Phase 4              | Chain shape is fixed; fills stub later rather than refactoring the pipeline.               |

---

## 13. References

- `canonical-message-format.md` — `Message`, `ToolDefinition`, `AdapterCapabilities`.
- `event-bus-and-trace-catalog.md` — payload shape for `route.decided` and routing auxiliaries.
- `streaming-protocol.md` (planned) — turn lifecycle events; how delegation result streaming works (or doesn't, in v1).
- Architecture overview — pattern fingerprint design and pattern store schema.
