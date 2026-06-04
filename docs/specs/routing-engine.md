# Routing Engine Specification

**Status:** v3.4 — proposed; LLM_ROUTER slot added (Wave 19, opt-in)
**Last updated:** 2026-06-04
**Owner:** _your name_

> **v3.4 update (2026-06-04 — default router model + REPL visibility):**
> First field test against the originally-shipped `openrouter:qwen/qwen-plus`
> default returned `no_tool_call` — qwen-plus generated prose instead of
> calling `choose_model`. Default switched to `anthropic:claude-haiku-4-5`
> (reliable forced tool-use; ~$0.001 per meta-call vs qwen-plus's measured
> $0.004 at 150-token max output). The §11.10 open question ("genuinely
> best cost-effective router model") stays open — haiku is the safe-bet
> default, not the optimal one. `TurnResult` gains an additive
> `route_chain` field (defaulted empty tuple) so the `metis dev` result
> tag can echo the LLM_ROUTER slot's per-turn verdict + meta-cost
> without callers having to query the trace store. The echo is quiet when
> the slot was a no-op (disabled / worker re-entry / not pre-computed).
>
> **v3.4 update (2026-06-04 — router-prompt quality fix):** Same-day field
> test showed the router picking `claude-sonnet-4-6` for a one-word `test`
> prompt at $0.045 per turn, when `haiku-4-5` was the obvious right answer.
> Two prompt bugs identified per §5.6.2 history note: (1) the candidate
> catalog carried capability tags + `fast`/`balanced`/`deep` task-profile
> labels but no concrete per-MTok prices, so the LLM had nothing to
> anchor "cheapest" against; (2) the guidance closed with "when in doubt,
> pick a balanced mid-tier candidate" — the wrong default for short or
> ambiguous prompts, which are exactly the case where "doubt" applies.
> Fix: catalog lines now carry `in $X/MTok, out $Y/MTok` from the active
> `PriceTable`; the guidance flips to "default to the cheapest candidate
> for short, ambiguous, conversational, or low-stakes prompts" with
> narrow escalation criteria. §5.6.2 is rewritten with the new prompt
> shape + the dated history note explaining the failure.
>
> **v3.4 changes (2026-06-03 — Wave 19, LLM router):** New chain slot
> `LLM_ROUTER` inserted at position 5, between `PATTERN_RECOMMENDATION` and
> the renumbered `DELEGATE_REQUEST` (now position 6). When enabled, the slot
> asks a small auxiliary LLM ("router model"; default
> `anthropic:claude-haiku-4-5` as of 2026-06-04; was
> `openrouter:qwen/qwen-plus` on the v3.4 first cut — see header update
> above) to pick the model for the turn
> from the registered, capability-valid, available candidates. The router
> returns its pick via a single `choose_model(model_id, reason)` tool call;
> the pick passes through the standard §4.4 validation gate. Off by default
> (chain shape unchanged for existing deployments — slot 5 reports
> `not_applicable, reason="llm_router disabled"`). Enabled per workspace
> via the new `llm_router:` block in `routing.yaml` (§5.6) or interactively
> with `/router llm on|off|model <id>|status` in `metis dev`. The slot
> shares the evaluator's `BudgetTracker` primitive with independent
> per-session / per-day caps; over-budget → `not_applicable,
> reason="budget_exhausted"`. Failure modes (timeout, invalid model id,
> network error, no candidates) all degrade to `not_applicable` with the
> reason recorded on `route.decided.chain`. In worker re-entry the slot
> defers with `reason="delegate_request_in_flight"` so the planner's
> explicit `tier=` choice is never second-guessed (matches PATTERN slot 4
> behavior under delegation, see `delegation.md §11`). Catalog event
> `route.decided` is extended additively — `PolicyEvaluation` gains
> `meta_cost_usd` / `meta_tokens_input` / `meta_tokens_output` fields
> (`None` for every slot other than `llm_router`); the
> `RoutingPolicyName` literal gains `"llm_router"`. Slot 4 still uses the
> Wave-15 pattern store. Renumbering shifts `DELEGATE_REQUEST` 5→6,
> `WORKSPACE_DEFAULT` 6→7, `GLOBAL_DEFAULT` 7→8 — slot names (the keys the
> implementation uses) are unchanged, only the numeric ordinals shift.
>
> **v3.3 changes (2026-05-20 — implementation sync):** Spec re-synced to the
> shipped routing engine. Slots 4 (`PATTERN_RECOMMENDATION`) and 5
> (`DELEGATE_REQUEST`) are wired, not stubs — §4.1/§4.2 corrected (slot 5 is
> always present in the chain, reporting `not_applicable` outside a delegation
> re-entry; it is not "skipped"). `team_budget_remaining_lt` predicate added
> to the closed set (§5.3). The v1-stub status of `cost_today_exceeds_usd`,
> `skills_matching_message_includes`, and `file_extensions_in_context` is now
> stated explicitly (§5.3–§5.4) — these are accepted at load but never match
> until their backing infra lands. §6 trimmed to the routing-owned surface:
> the `delegate()` tool contract is now canonical in
> [`delegation.md`](delegation.md), and §6.1–§6.8 are cross-reference stubs
> (section numbers preserved so existing references resolve). Auxiliary-event
> implementation status annotated (§7.3). Stale `metis-core` source path and
> the `~/.yourtool/` config-dir placeholder corrected.
>
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
> require all three slots (§5.8). Mid-turn multiple swaps last-write-wins (§3.3).
> Various nits.
>
> *Throughout: the per-workspace routing config lives at
> `<workspace>/.metis/routing.yaml`. Older drafts of this spec used a
> `~/.yourtool/` placeholder; remaining `~/.metis/`-style paths denote the
> user-global config directory.*

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
5. LLM_ROUTER             — auxiliary LLM picks model from validated candidates; opt-in, see §4.6
6. DELEGATE_REQUEST       — resolved tier model on a delegation re-entry; see §6
7. WORKSPACE_DEFAULT      — workspace-scoped default
8. GLOBAL_DEFAULT         — hardcoded fallback
```

All eight slots are evaluated in this fixed order on every turn — the chain
shape does not change between turn types. Slot 5 (`LLM_ROUTER`) is opt-in
per workspace (§5.6); when disabled or absent it reports `not_applicable,
reason="llm_router disabled"` and the chain proceeds. Slot 6
(`DELEGATE_REQUEST`) only *proposes a candidate* inside a worker session's
re-entry into the chain (§6.9); on a normal top-level turn it reports
`not_applicable` (it is not skipped). Likewise slots 3, 4, and 5 propose a
candidate only when a rule matches / the pattern store returns a confident
recommendation / the router LLM returns a validated pick, and report
`not_applicable` otherwise. The `route.decided.chain` trace (§7) is the
prefix of slots evaluated up to and including the winner, so a turn won by
slot 3 records three entries and a turn won by slot 8 records all eight.

### 4.2 Why this order

User intent dominates. (1) is the most local user signal — "just this message." (2) is the session-level user signal. (3) is pre-declared user policy. (4) is system inference from accumulated empirical data, ranked below user-set things by design. (5) is system inference from an inference-time LLM, ranked below (4) because patterns are cheaper, faster, deterministic for a given fingerprint, and grounded in observed outcomes — the LLM router is the inference-time fallback when patterns are cold or below the confidence gate. (6) handles delegation. (7) and (8) are floors.

This order is deliberate and stable. Reordering it (e.g., putting pattern recommendations above rules, or putting the LLM router above patterns) would let probabilistic inference silently override user-set or empirically-validated choices — the failure mode that destroys trust.

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

> *This relies on `AdapterCapabilities` (defined in `canonical-message-format.md` §7.2) declaring `supports_tools`, `supports_system_prompt`, and `supports_structured_output`. Those fields have shipped on the canonical type. Entries that don't declare them default to `true` for `supports_tools` and `supports_system_prompt` (the common case for both Anthropic and OpenAI) and `false` for `supports_structured_output`. The engine's `_validate()` ([`routing/engine.py`](../../packages/metis/src/metis/core/routing/engine.py)) reads them via `ModelRegistry.capabilities_for()`.*

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

The thresholds (`_NETWORK_PROVIDER_ESCALATION_THRESHOLD`, `_NETWORK_PROVIDER_ESCALATION_WINDOW_SECONDS`) live in [`availability.py`](../../packages/metis/src/metis/core/routing/availability.py) as module constants; AUTH still escalates immediately because a misconfigured key cannot be a one-off.

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

### 4.6 The `LLM_ROUTER` slot

The `LLM_ROUTER` slot (chain position 5) asks a small auxiliary LLM to pick the model for the turn from the set of registered, capability-valid, available candidates. It is off by default, opt-in per workspace via the `llm_router:` block in `routing.yaml` (§5.6) or interactively via `/router llm on` in `metis dev`.

When disabled, the slot returns `not_applicable, reason="llm_router disabled"` and the chain proceeds to slot 6.

#### 4.6.1 Why this slot exists

Slot 4 (`PATTERN_RECOMMENDATION`) returns confident picks only after the pattern store has accumulated enough verdicts in the K-NN cluster around the current turn's fingerprint. In a fresh workspace, or on a workload the store hasn't seen, slot 4 returns `not_applicable` and routing falls through to the workspace / global default. The default is by construction a single fixed model — it ignores per-turn task character.

`LLM_ROUTER` is the inference-time complement to the pattern store: when accumulated data is absent, an LLM that has been trained on text describing many models' strengths can produce a per-turn pick that is at least correlated with task character. It does *not* replace patterns — once the pattern store has data, slot 4 wins first (cheaper, deterministic, grounded in observed outcomes). The LLM router fires on the cold-start tail.

#### 4.6.2 What the router LLM sees

The router LLM is called once per turn (when enabled) with:

1. **System prompt** (stable across turns in a workspace, so provider-side prompt cache applies): the closed instruction to call `choose_model` exactly once; a catalog of every candidate model with `model_id`, capability summary (vision / tools / system / structured output / context window), and per-MTok price tier (`fast` / `mid` / `deep`).
2. **User prompt** (the turn's new user message text, snapshot per §5.3.1). No prior turn history; no tool definitions of the *outer* session; no skills.
3. **One tool**: `choose_model(model_id: string, reason: string)`. Tool-use is required (forced via `tool_choice: choose_model` on providers that support it; JSON-mode fallback otherwise).

The router LLM does NOT see the workspace contents, `MEMORY.md`, prior turn assistant text, or any other context. This is by design — the meta-call must stay cheap and bounded, and we want to avoid leaking conversation content into the meta-decision.

The candidate set is built from the model registry filtered by §4.4 (capability + per-(provider, model) availability). A candidate that fails validation is omitted from the prompt entirely — the router can't choose a model that wouldn't pass validation anyway.

#### 4.6.3 Validation of the router's pick

The model id returned by the router LLM passes through the standard §4.4 validation gate. If validation rejects the pick (capability mismatch, provider Unavailable, model not registered), the slot reports `rejected` with the validation failure recorded, and the chain falls through to slot 6 — exactly like a rejected pattern recommendation or rule.

If the router returns a `model_id` that is not in the candidate set (e.g. hallucinated, or the model was removed between the prompt and the tool call), the slot reports `not_applicable, reason="invalid_model_id: <name>"` and the chain proceeds.

#### 4.6.4 Worker re-entry

When the engine is invoked for a worker session (the planner emitted `delegate(tier=...)`), the `LLM_ROUTER` slot defers with `verdict: not_applicable, reason: "delegate_request_in_flight"`. The planner's explicit `tier=` choice must not be second-guessed by an LLM. This matches the slot 4 (pattern) behavior under delegation — see `delegation.md §11`.

#### 4.6.5 Budget

The slot shares the `BudgetTracker` primitive from the evaluator (`evaluator.md §4.3`) with **independent caps**. Defaults:

- `per_session_budget_usd: 0.10`
- `per_day_budget_usd: 1.00`

Both are configurable in the `llm_router:` block (§5.6). When the meta-call would push the running total past either cap, the slot reports `not_applicable, reason="budget_exhausted"` and the chain proceeds without making the LLM call. The evaluator and router each see their own running totals — sharing the primitive means we don't ship two implementations of token-budget bookkeeping, not that the two budgets sum into one cap.

#### 4.6.6 Failure modes

All failure modes degrade to `not_applicable` with a documented `reason`, so the chain always reaches a definite winner from slots 6-8. Failures are recorded on `route.decided.chain` so dashboards can attribute them.

| Failure                                          | `reason` recorded                                  |
| ------------------------------------------------ | -------------------------------------------------- |
| LLM call timed out (default 8s)                  | `timeout`                                          |
| LLM call raised a network / 5xx error            | `network_error: <error_class>`                     |
| LLM returned a model id not in the candidate set | `invalid_model_id: <name>`                         |
| LLM returned no tool call                        | `no_tool_call`                                     |
| Provider rejected the request (e.g. AUTH)        | `provider_error: <error_class>`                    |
| Budget exhausted (per-session or per-day)        | `budget_exhausted`                                 |
| No candidate models passed §4.4 validation       | `no_candidates`                                    |
| `llm_router:` block missing or `enabled: false`  | `llm_router disabled`                              |
| Worker re-entry                                  | `delegate_request_in_flight`                       |

The slot **never** raises an exception that escapes the engine. The chain is allowed to fall through; routing's no-model-available hard failure (§4.8) still requires every slot to come up empty.

#### 4.6.7 Cost trace

The meta-call lands in the trace store as a normal `llm.call_completed` event stamped with a new `Actor.ROUTER` (`canonical/actors.py`), so dashboards can attribute meta-spend separately from the planner's spend. The cost is also surfaced on the `LLM_ROUTER` slot's `PolicyEvaluation` via three new fields (`meta_cost_usd`, `meta_tokens_input`, `meta_tokens_output`), `None` for every other slot. The slot's meta-cost does **not** count against the session's `turn.completed.usage.cost_usd` (which measures only planner-side LLM tokens, consistent with the worker convention in `delegation.md §8`).

The meta-call's `route.decided` invariant still holds: routing produces exactly one `route.decided` event per turn. The router's `llm.call_completed` is a separate event, ordered before `route.decided` because the router's response is read first.

#### 4.6.8 Caching

The router's system prompt is stable for the lifetime of a workspace's candidate set (changes only when the registry changes or `llm_router.model` changes). Provider-side prompt caching (Anthropic `cache_control`, OpenAI implicit, OpenRouter where supported) applies automatically via the existing context-assembler pathway. The router does NOT maintain its own decision cache in v1 — the same user prompt going through the router twice will make two meta-calls. Adding a per-workspace decision cache is a deferred follow-on (§11.x).

#### 4.6.9 What the router is not

- **Not a model committee.** Single LLM, single tool call, single pick. Multi-model voting / ensemble is out of scope.
- **Not a delegator.** The router cannot return `delegate()` — it picks a single model that handles the turn. Routing-level delegation remains the planner's job via the `delegate()` tool (`delegation.md`).
- **Not a self-trainer.** The router does not write back to the pattern store. Decisions made by slot 5 are recorded in the trace and contribute to evaluation, but the K-NN cluster the pattern store uses is built from turn outcomes, not from router picks.
- **Not above the user.** Slots 1-4 always win first. A user with `/model haiku` set sticks with haiku; a user typing `@sonnet` in a message wins; a YAML rule that matches wins. The router fires only when accumulated user intent and empirical data are both silent.

### 4.7 No per-rule fallback lists

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

### 4.8 Hard failure

If every policy in the chain returns `None` or fails validation, the engine raises a hard error to the session manager. The TUI surfaces:

```
No model available for this turn.
  Tried: anthropic:claude-opus-4-7 (unavailable), anthropic:claude-sonnet-4-6 (unavailable)
  Run /model <id> to choose explicitly, or /rules check.
```

The turn is not started. The user must intervene (set a sticky, fix config, wait for provider recovery). Silently using a model the user didn't authorize is never acceptable.

### 4.9 Hot reload

The configured policy file is read fresh at the start of every turn. Cost: a yaml parse and validation pass, ~1ms for a typical file. The router caches the parsed structure keyed by file mtime to avoid re-parsing when nothing changed.

If the file is invalid (yaml syntax error, unknown predicate, unknown model), the router uses the last-known-good version and surfaces this in the TUI. Users diagnose with `/rules check`.

---

## 5. The configured rule format

### 5.1 File location and shape

The per-workspace routing config lives at `<workspace>/.metis/routing.yaml`.
It is read fresh at the start of every turn (§4.9); a missing file is
equivalent to an empty policy (the chain falls straight through to the
defaults).

```yaml
# <workspace>/.metis/routing.yaml
schema_version: 1

global_default: anthropic:claude-sonnet-4-6

# Tier mapping for delegation (§6.10)
tiers:
  fast: anthropic:claude-haiku-4-5
  balanced: anthropic:claude-sonnet-4-6
  deep: anthropic:claude-opus-4-7

# Pattern store weighting (§5.5) + optional v2 hybrid-fingerprint opt-in
pattern:
  cost_weight: 0.05       # 0.0 = pure quality, 1.0 = pure cost (default 0.05)
  min_confidence: 0.05    # default 0.05 — scaled to match cost_weight=0.05 (see §5.5)
  min_sample_size: 5
  # fingerprint_version: v2                            # opt in to the hybrid embedding fingerprint
  # embedding_provider: openai:text-embedding-3-small  # required when v2
  # embedding_alpha: 0.6                               # cosine/jaccard blend (pattern-store.md §16)

# LLM-router slot (§4.6 + §5.6). Off by default. When enabled, slot 5
# asks the configured router model to pick the turn's model from the
# candidate set. Falls through on any failure mode.
llm_router:
  enabled: false                           # default false; chain shape unchanged when disabled
  model: anthropic:claude-haiku-4-5        # safe-bet default (reliable forced tool-use); cheaper models TBD per §5.6 research note
  per_session_budget_usd: 0.10             # shared BudgetTracker primitive with evaluator (independent caps)
  per_day_budget_usd: 1.00
  timeout_seconds: 8                       # meta-call wall-clock cap
  # tool_choice: required                  # always forced; not user-tunable in v1

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

**Workspace `tiers` must define all three slots (`fast`, `balanced`, `deep`) or be omitted entirely.** A partial workspace tier map is rejected at validation time. Rationale: an all-or-nothing tier map makes a misconfiguration a config-time error rather than a non-obvious runtime `no_model_available_for_tier`. If a workspace truly only wants to override one tier, it must restate the others (typically by copying the global mapping). **Note:** as of v1 the `routing.yaml` `tiers:` block is parsed and validated but not consumed by delegation — tier resolution reads the model registry instead. See §6.10.

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
| `team_budget_remaining_lt`         | The team's remaining monthly budget (cap minus month-to-date spend) at turn start, in USD. Set by the gateway harness on team-bound requests; `None` — and the predicate `False` — on the agent path or when no team-level cap is configured (multi-user.md §6.1). |

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
| `team_budget_remaining_lt`         | float         | Team's remaining monthly budget is below the threshold, in USD (multi-user.md §6.1). |
| `any_of`                           | [predicate]   | Logical OR.                                                       |
| `all_of`                           | [predicate]   | Logical AND.                                                      |
| `not`                              | predicate     | Logical NOT.                                                      |

A `when` block with multiple top-level keys is implicitly `all_of`.

> **v1 implementation status.** Three predicates are accepted at load time so
> policy can be written forward-compatibly, but their backing infrastructure
> is not yet wired — they evaluate to `False` on every turn, so a rule whose
> `when` block depends on one of them never matches:
> `skills_matching_message_includes` (no skill-description index is built at
> routing time), `file_extensions_in_context` (no tool-touched-file tracker),
> and `cost_today_exceeds_usd` (no daily-cost accumulator — see §5.4).
> `team_budget_remaining_lt` is *not* in this category: it matches whenever
> the gateway supplies the team-budget headroom, and is `False` only when
> there is genuinely no team binding (the agent path, pre-multi-user keys).

> *Note on `skills_matching_message_includes`: prior drafts of this spec called this predicate `skills_loaded_includes`. That name was misleading because skills are loaded by the context assembler, which runs **after** routing — at routing time no skills are "loaded" yet. The current name reflects what's actually checked: a fast match against the skill description index.*

### 5.4 The cost circuit breaker

`cost_today_exceeds_usd` is a first-class predicate. When it fires, the TUI surfaces:

```
Daily budget $5.00 exceeded ($5.42 today). Routing per "budget circuit breaker" rule.
```

Banner clears at UTC midnight reset.

> **v1 status.** The predicate is part of the closed set and is accepted by
> the policy loader, but the daily-cost accumulator that would feed it is not
> wired yet (§5.3.2 "v1 implementation status"). Until it lands the predicate
> evaluates to `False`, so a `cost_today_exceeds_usd` rule never fires and the
> banner above never appears. The behavior described here is the intended
> contract for when the accumulator ships.

### 5.5 Pattern recommendations and the cost/quality knob

> **Status.** Slot 4 (`PATTERN_RECOMMENDATION`) is wired end-to-end: the
> engine consults the per-workspace `PatternStore` when a `pattern_store_resolver`
> and `fingerprint_inputs_builder` are injected, and emits `pattern.matched`
> on a win. The store mechanics — fingerprinting, K-NN retrieval, the v2
> hybrid embedding path — are specified in
> [`pattern-store.md`](pattern-store.md); this section covers only how the
> routing chain *consumes* a recommendation.

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

### 5.6 The `llm_router:` config block

Workspace-scoped (top-level `llm_router:` applies globally; `workspaces.<path>.llm_router:` overrides per workspace, matching the §5.5 `pattern:` pattern). All fields optional; absent block ≡ `enabled: false`.

```yaml
llm_router:
  enabled: true
  model: anthropic:claude-haiku-4-5
  per_session_budget_usd: 0.10
  per_day_budget_usd: 1.00
  timeout_seconds: 8
```

| Field                    | Type    | Default                          | Notes                                                                                              |
| ------------------------ | ------- | -------------------------------- | -------------------------------------------------------------------------------------------------- |
| `enabled`                | bool    | `false`                          | Master switch. False → slot 5 returns `not_applicable, reason="llm_router disabled"`.              |
| `model`                  | str     | `anthropic:claude-haiku-4-5`     | Router model id (registered in `ModelRegistry`, capability-validated like any other model).        |
| `per_session_budget_usd` | float   | `0.10`                           | Per-session cap; shares `BudgetTracker` primitive with evaluator (independent caps).               |
| `per_day_budget_usd`     | float   | `1.00`                           | Per-day cap; ditto.                                                                                |
| `timeout_seconds`        | float   | `8.0`                            | Wall-clock cap on the meta-call. Timeout → `not_applicable, reason="timeout"`.                     |

#### 5.6.1 Default router model

`anthropic:claude-haiku-4-5` is the v1 default — chosen for reliable forced tool-use (the slot requires the router to call `choose_model` exactly once, and haiku honors that consistently). **This default is not load-bearing** — the cost-effective router model question is genuinely open and should be benchmarked against the standard suite once we have signal on router-pick quality across `haiku-4-5`, `gpt-4o-mini`, `qwen3-coder`, and other sub-$1/MTok candidates. The router model selection benchmark is a deferred follow-on (§11.10).

**2026-06-04 history.** First field test (one turn against the v3.4-first-cut `openrouter:qwen/qwen-plus` default) returned `no_tool_call`: qwen-plus emitted 150 output tokens of prose explaining its pick instead of calling the tool, the slot reported `not_applicable, reason="no_tool_call"` and the chain fell through to the workspace default ($0.0042 wasted per turn on the meta-call). The default switched to haiku-4-5 the same day. Cheaper candidates (qwen3 family, gpt-4o-mini) remain viable if tool-use is verified per-model first.

Workspaces with no `openrouter` API key configured should set `model:` explicitly to an `anthropic:` or `openai:` candidate that resolves under their existing credentials. The `metis auth doctor` output lists which providers have credentials.

#### 5.6.2 The router's tool prompt

The router is called with one tool:

```json
{
  "name": "choose_model",
  "description": "Pick the model best suited to handle the user task. Return one of the registered candidates.",
  "input_schema": {
    "type": "object",
    "required": ["model_id", "reason"],
    "properties": {
      "model_id": {"type": "string", "enum": ["<candidate_1>", "<candidate_2>", ...]},
      "reason":   {"type": "string", "maxLength": 200}
    }
  }
}
```

The `model_id` enum is constructed from the candidate set at call time (post §4.4 validation). The router cannot return a model not on the enum; if a provider's tool-use implementation doesn't enforce enums strictly, an off-enum response triggers `invalid_model_id` per §4.6.6.

The system prompt is built from a stable template + the candidate catalog and is constant across consecutive turns in a workspace, so provider-side prompt caching applies (Anthropic `cache_control`, OpenAI implicit, OpenRouter where the upstream supports it per `provider-adapter-contract.md §4.5`).

Each catalog line carries: `model_id`, capability summary (`tools` / `vision` / `thinking`), context-window size, and **per-MTok input + output rates** from the active `PriceTable`. The price is concrete dollar amounts, not a relative tier label — without absolute numbers the router LLM can't anchor "cheapest" against the alternatives. A candidate whose model is not in the `PriceTable` is rendered with a `price unknown` marker rather than being dropped.

```
- anthropic:claude-haiku-4-5    [fast; tools; 200k ctx; in $0.8/MTok, out $4/MTok]
- anthropic:claude-sonnet-4-6   [balanced; tools; vision; thinking; 200k ctx; in $3/MTok, out $15/MTok]
- anthropic:claude-opus-4-7     [deep; tools; vision; thinking; 200k ctx; in $15/MTok, out $75/MTok]
```

The guidance block at the bottom of the system prompt explicitly biases the router toward the cheapest viable candidate and enumerates the (narrow) escalation criteria. The phrasing is **load-bearing**: an earlier draft ended with "when in doubt, pick a balanced mid-tier candidate" which qwen-plus interpreted as license to pick `claude-sonnet-4-6` for a one-word prompt (`test`) on 2026-06-04, paying a ~$0.045 turn cost when haiku would have produced the same response for under $0.005. The shipped guidance instead reads:

> Bias hard toward the cheapest viable model.
> - **Default to the cheapest candidate** for short, ambiguous, conversational, or low-stakes prompts.
> - Escalate to a mid-tier model **only when** the task explicitly calls for multi-step reasoning, code synthesis across multiple files, careful refactoring, or non-trivial debugging.
> - Escalate to the deep tier **only for** architecture design, security review, multi-document synthesis, or tasks that explicitly request extended reasoning. "test" / "hi" / "continue" / single-sentence questions are NOT in this category.
> - A 4× more expensive model that gives a 5% better answer on a trivial task is the wrong pick.

The catalog-line price + biased-cheap guidance combination is the v3.4 fix for the 2026-06-04 sonnet-for-`test` failure; the §11.10 "genuinely best router model" open question is independent of this fix.

#### 5.6.3 Validation

The `llm_router:` block is validated at load time:

- `enabled` is a bool.
- `model` is a non-empty string. The model id is NOT required to be currently registered (the registry can change at runtime); resolution happens per turn, and an unregistered model results in `no_candidates` or a validation rejection.
- `per_session_budget_usd` and `per_day_budget_usd` are non-negative floats. `0.0` is allowed (disables the router via budget exhaustion on every turn).
- `timeout_seconds` is a positive float; values < 1.0 are clamped to 1.0 with a `routing.policy_invalid`-style WARN.

A malformed `llm_router:` block is treated as if absent (slot reports `not_applicable, reason="llm_router disabled"`), and a `routing.policy_invalid` event is emitted with the validation errors. The rest of the policy file continues to load (the rule slot stays functional).

### 5.7 Tie-breaking against configured rules

When both a rule and a pattern recommendation are available, the rule wins (per §4.1). The pattern recommendation is *not* discarded — it's recorded in the `route.decided` event as a deferred policy with its own evaluation.

**Pattern-disagreement surfacing — not yet implemented.** The opt-in feature
described in the rest of this section is specified but unbuilt as of Wave 16:
the `pattern_disagreement` config block is not parsed by the policy loader,
the `/route override` and `/route ignore` commands do not exist, and the
`route.overridden` / `pattern.override_dismissed` events are not in the
catalog. The design is retained here as the intended contract.

An opt-in feature surfaces high-confidence pattern disagreement to the user:

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

### 5.8 Validation

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
10. `pattern.fingerprint_version` is `v1` or `v2`; `pattern.embedding_alpha` is in `[0.0, 1.0]`; `fingerprint_version: v2` requires `pattern.embedding_provider` to be set (the v2 hybrid fingerprint — pattern-store.md §16).

A failure in any of these causes the file to be rejected as a whole — last-known-good is used. `/rules check` prints validation errors.

### 5.9 What rules cannot do

By design, rules cannot:

- Modify session state (set memory, load skills, etc.). Rules pick a model; nothing else.
- Reference message history beyond predicate evaluation.
- Chain (one rule firing causing another to evaluate differently).
- Define functions or variables. No DSL.

These constraints are how the engine stays predictable. Users wanting richer logic write a skill, not a rule.

---

## 6. The `delegate()` contract

> **Status (Wave 16).** The `delegate()` tool shipped in Wave 10 (delegation
> v1 MVP). [`delegation.md`](delegation.md) is the **canonical contract** for
> the tool signature, the worker session lifecycle, context handoff
> (`ContextSpec`), the worker system prompt, return values, failure modes,
> and cost attribution. This section is trimmed to the surface the **routing
> engine** owns: the `DELEGATE_REQUEST` chain slot, worker re-entry into the
> policy chain (§6.9), capability validation of the resolved tier model, and
> tier definitions (§6.10).
>
> §6.1–§6.8 are retained as cross-reference stubs. Their section numbers are
> cited from `delegation.md`, `tool-dispatcher.md`, and
> `event-bus-and-trace-catalog.md`, so the numbering is kept stable even
> though the canonical text now lives in `delegation.md`.
>
> The routing chain's `DELEGATE_REQUEST` slot (§4.1, position 6 as of v3.4;
> position 5 in v3.3 and earlier — see §4 changelog header for the
> renumbering rationale) existed from Phase 1 as a stub returning
> `not_applicable`; delegation v1 filled in the stub. The slot still
> reports `not_applicable` on every non-worker turn.

### 6.1 Tool signature

→ Canonical: [`delegation.md §4.1`](delegation.md). The
`delegate(tier, task, context, output_schema?, allowed_tools?, max_tokens?)`
tool is registered for a planner session whose active model has
`can_delegate: true` in the model registry (§6.8). It is never registered for
worker sessions — no recursive delegation in v1.

### 6.2 What the worker is

→ Canonical: [`delegation.md §5`](delegation.md). A worker is a fresh
`Session` (`is_worker=true`, with `parent_session_id` / `parent_tool_use_id`
set, workspace inherited) running the same turn-locked loop (§3.2) as any
other session.

### 6.2.1 Worker side-effects: what's read-only

→ Canonical: [`delegation.md §5.4–§5.6`](delegation.md) and the isolation
summary in delegation.md §10. Workers are read-only against memory (the
`memory_*` tools are not registered for worker sessions), skills (load only,
no authoring), and `routing.yaml`.

### 6.2.2 Worker visibility in the UI

→ Canonical: [`delegation.md §5`](delegation.md) and delegation.md §14.9.
Worker sessions are hidden from `/history` by default and surface as cost
rollups under the parent session.

### 6.3 Context handoff: two modes only

→ Canonical: [`delegation.md §4`](delegation.md). `ContextSpec` is `minimal`
(task brief only) or `explicit` (planner-specified `include` references).
There is no `auto` mode in v1 — the planner decides what the worker needs.

### 6.4 When to use which mode

→ Canonical: [`delegation.md §4`](delegation.md). `minimal` for
self-contained mechanical sub-tasks; `explicit` when the worker needs curated
prior context. When unsure, default to `explicit`.

### 6.5 The system prompt the worker sees

→ Canonical: [`delegation.md §5.7`](delegation.md). The worker runs under a
sub-agent system prompt instructing it to stay focused, be terse, fetch
missing information via tools, and return rather than ask the user for
clarification.

### 6.6 Return value and failure modes

→ Canonical: [`delegation.md §4.3–§4.4`](delegation.md). `DelegateResult`
carries `success` / `output` / `error` / a usage rollup / `worker_session_id`.
The one failure mode the **routing** path itself produces is
`no_model_available_for_tier` (§6.9); the rest (`worker_error`,
`max_tokens_exceeded`, `insufficient_context`, `output_schema_validation_failed`,
`cancelled_by_user`, `timeout`) originate inside the worker session or its
spawn wrapper.

#### 6.6.1 The `insufficient_context` schema

→ Canonical: [`delegation.md §4.4`](delegation.md). A worker that cannot
proceed without more information returns a structured
`InsufficientContextRequest` rather than free text, so the planner can
programmatically retry with targeted references.

### 6.7 Cost accounting

→ Canonical: [`delegation.md §8`](delegation.md). Worker token usage is
attributed to the worker session and rolls up under the planner via the
analytics projection (`/analytics/cost?group_by=parent_session` /
`group_by=is_worker`). The planner's own `turn.completed.usage.cost_usd`
measures planner tokens only; worker cost lives on `delegate.completed`.

### 6.8 The model registry's `can_delegate` flag

→ Canonical: [`delegation.md §4.2`](delegation.md). `ModelEntry.can_delegate`
([`routing/registry.py`](../../packages/metis/src/metis/core/routing/registry.py))
gates whether the `delegate` tool is registered when that model is the active
planner model. Default `false`; a worker session never sees the tool
regardless of the flag.

### 6.9 Re-entry into the routing pipeline

This is the canonical definition of the `DELEGATE_REQUEST` slot, cross-referenced
by [`delegation.md §7`](delegation.md).

When a planner emits a `delegate(tier=...)` call,
`SessionManager.spawn_worker`
([`sessions/manager.py`](../../packages/metis/src/metis/core/sessions/manager.py))
resolves `tier` to a concrete model via the model registry's `delegation_tier`
field (`ModelRegistry.model_for_tier`, §6.10). If no registered model carries
that tier, `spawn_worker` short-circuits with `no_model_available_for_tier`
and no worker session is created — this case never reaches the routing engine.

Otherwise a worker `Session` is created, and its first turn enters the policy
chain with the resolved model carried on `TurnContext.worker_tier_model`. The
chain runs end-to-end (§6.9.1); slots 1–5 typically report `not_applicable`
(slot 5 `LLM_ROUTER` defers with `reason="delegate_request_in_flight"` per
§4.6.4), and slot 6 (`DELEGATE_REQUEST`) proposes the resolved tier model as
its candidate.

The resolved tier model is **validated like any other candidate** (§4.4 —
capability and availability). If it passes, slot 6 wins. If it fails — e.g.
the worker's task carries images and the resolved `fast` model is text-only,
or the model's provider is Unavailable — slot 6 is `rejected` and the chain
**falls through** to `WORKSPACE_DEFAULT` / `GLOBAL_DEFAULT`, exactly as for
any other rejected candidate. There is **no** automatic `fast → balanced →
deep` upgrade inside the engine in v1 (earlier drafts of this spec described
one; it was never implemented). A user wanting capability-driven escalation
encodes it as a configured rule (§4.6) or relies on the workspace default.

The worker's turn is itself turn-locked (§3.2): the model chosen at the
worker's turn start owns all of the worker's LLM calls and tool cycles.

#### 6.9.1 Why the full chain runs for workers

The policy chain runs end-to-end for worker sessions, including policies that cannot apply to workers by construction (`PER_MESSAGE_OVERRIDE` — workers don't have user messages with `@` prefixes; `MANUAL_STICKY` — workers have no user `/model` command; `CONFIGURED_RULES` — *can* match against the worker's task brief and would fire if the user wrote rules targeting it, though this is rare).

The alternative — skipping inapplicable policies in worker mode — produces a cleaner trace but adds a special case to the routing pipeline. A reviewer might prefer either; this spec keeps the chain uniform for predictability. The `route.decided` event traces for workers will show `not_applicable` verdicts on the user-facing policies, which is the trade-off.

### 6.10 Tier definitions

`tier` is a coarse abstraction over concrete models — `fast` / `balanced` /
`deep`:

| Tier      | Intent                                                                   |
|-----------|--------------------------------------------------------------------------|
| `fast`    | Cheap, low-latency. Used for mechanical sub-tasks.                       |
| `balanced`| Capable enough for most coding work. Default for medium tasks.           |
| `deep`    | The most capable available. Used for planning and reasoning.             |

In the shipped implementation, tier resolution is a **model-registry**
lookup. Each `ModelEntry` may declare a `delegation_tier`
([`routing/registry.py`](../../packages/metis/src/metis/core/routing/registry.py)),
and `ModelRegistry.model_for_tier(tier)` returns the first registered model
whose `delegation_tier` matches. A tier with no registered model causes
`delegate(tier=X)` to return `no_model_available_for_tier` (§6.9).

> **Spec/impl note.** The `tiers:` block in `routing.yaml` (§5.1, §5.2) is
> parsed and validated into `RoutingPolicy.tiers` / `WorkspaceScope.tiers`
> (a `TierMap`) but is **not yet consumed** by delegation — `spawn_worker`
> reads the model registry, not the routing policy. Per-workspace tier
> overrides written in `routing.yaml` therefore do not take effect in v1;
> the block is retained as forward-compatible config. Reconciling the two
> tier sources — having `spawn_worker` consult the workspace-scoped
> `TierMap`, or dropping the `routing.yaml` block in favor of registry
> config — is tracked as an open question (§11).

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
    policy: str                       # per_message_override | manual_sticky | rule
                                      # | pattern | delegate_request
                                      # | workspace_default | global_default
                                      # (lowercase snake_case wire literals —
                                      #  RoutingPolicyName in events/payloads.py)
    verdict: Verdict                  # not_applicable | deferred | rejected | chose
    candidate_model: str | None       # what this policy proposed (None if not_applicable)
    reason: str                       # human-readable

    # Mode-specific extras
    rule_name: str | None             # for the rule slot
    confidence: float | None          # for the pattern slot
    pattern_alternatives: list[PatternAlternative] | None  # for the pattern slot

    # Validation outcome (if candidate_model was set)
    validation_failure: str | None    # "no_vision_support" | "exceeds_context_window"
                                      # | "no_tool_support" | "no_system_prompt_support"
                                      # | "no_structured_output_support"
                                      # | "provider_unavailable" | "not_configured"
                                      # | None if validation passed

class PatternAlternative:
    model: str
    score: float                      # the aggregate score from §5.5
    sample_size: int                  # how many neighbors used this model

class Verdict(StrEnum):
    NOT_APPLICABLE = "not_applicable"  # policy didn't have anything to say (e.g. no override token)
    DEFERRED       = "deferred"         # policy had a candidate but a higher-priority policy won
    REJECTED       = "rejected"         # candidate failed validation, chain continued
    CHOSE          = "chose"            # this policy won
```

> **`deferred` is currently never emitted.** The engine evaluates policies in
> order and **stops at the first winner** — lower-priority policies are not
> evaluated and do not appear in `chain`. So `chain` is always the prefix of
> slots up to and including the `chose` entry, and a policy outranked by an
> earlier winner simply never runs. The `deferred` verdict is reserved for
> the opt-in pattern-disagreement feature (§5.7), which would deliberately
> evaluate and record the pattern slot even when a rule wins; until that
> ships, every chain entry is `not_applicable`, `rejected`, or `chose`.

### 7.2 Why one event, not many

Earlier drafts had separate events for `routing.constraint_failure`, `routing.rule_skipped`, `routing.policy_invalid`. Folding them into a single `route.decided` event means:

- One database row to look up "why did this turn pick this model?"
- The dashboard renders one view, not a join across event types.
- The full chain trace is atomic with the decision; no risk of missing related events.

`routing.policy_invalid` (the rule file failed to load) remains a separate session-level event since it's not turn-scoped.

### 7.3 Auxiliary events

These remain separate events because they describe distinct user actions or worker lifecycle. The **Status** column reflects the Wave-16 implementation:

| Event type                       | When                                                          | Status |
|----------------------------------|---------------------------------------------------------------|--------|
| `route.overridden`               | User chose `/route override` (turn re-dispatched on pattern's choice). | **Not implemented** — depends on the §5.7 pattern-disagreement feature; no payload in the catalog. |
| `pattern.override_dismissed`     | User chose `/route ignore` (turn proceeds with original choice). | **Not implemented** — see §5.7. |
| `delegate.started`               | A `delegate` tool call began. Includes worker_session_id.     | Shipped (Wave 10). |
| `delegate.completed`             | The worker session ended.                                     | Shipped (Wave 10). |
| `delegate.failed`                | The worker session failed; includes failure mode (§6.6).      | Shipped (Wave 10). |
| `routing.policy_invalid`         | The rule file failed to load; last-known-good in use.         | Shipped; audit-flagged (`AUDIT_EVENT_TYPES`). |
| `routing.provider_unavailable`   | Provider state transitioned to Unavailable.                   | Payload defined in the catalog; **emission not wired** — the availability state machine ([`availability.py`](../../packages/metis/src/metis/core/routing/availability.py)) has no event-bus handle in v1. |
| `routing.provider_recovered`     | Provider state transitioned back to Healthy.                  | Payload defined in the catalog; **emission not wired** (as above). |

All events conform to the schema defined in `event-bus-and-trace-catalog.md`.

### 7.4 How the dashboard uses it

The "why this model?" view is a single record render:

```
Turn 01HZ_xyz · session sess_42 · 2026-05-08T14:23:11Z
Chose: anthropic:claude-sonnet-4-6                           (workspace default)

Chain:
  [1] per_message_override   not_applicable  no @model token in message
  [2] manual_sticky          not_applicable  no sticky model set
  [3] rule                   rejected        rule "deep for architecture" matched →
                                             anthropic:claude-opus-4-7 (provider_unavailable)
  [4] pattern                not_applicable  no high-confidence recommendation
  [5] llm_router             not_applicable  llm_router disabled
  [6] delegate_request       not_applicable  not a delegation re-entry
  [7] workspace_default      chose           anthropic:claude-sonnet-4-6
```

The chain stops at the first `chose` entry (here slot 7), so `global_default`
(slot 8) does not appear. The TUI's `/model show` command prints the same
trace inline.

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

### 8.4a LLM_ROUTER picks for a cold-start workspace

```
session.active_model = None
rules: []
pattern store: empty (cold start)
llm_router: enabled=true, model=anthropic:claude-haiku-4-5

User: "Refactor the get_user helper to accept a Session argument; preserve the call sites."

Router meta-call:
  → input: system prompt + candidate catalog + user message
  → choose_model(model_id="anthropic:claude-sonnet-4-6",
                 reason="multi-file refactor with contract preservation needs balanced tier")
  → meta_cost_usd=$0.00021, meta_tokens_input=1840, meta_tokens_output=72

Chain:
  PER_MESSAGE_OVERRIDE   not_applicable
  MANUAL_STICKY          not_applicable
  CONFIGURED_RULES       not_applicable  (no rules match)
  PATTERN_RECOMMENDATION not_applicable  (no high-confidence recommendation, sample size 0)
  LLM_ROUTER             chose → anthropic:claude-sonnet-4-6  (meta_cost $0.00021)
```

### 8.4b LLM_ROUTER timeout falls through

```
session.active_model = None
rules: []
pattern store: empty
llm_router: enabled=true, model=anthropic:claude-haiku-4-5, timeout_seconds=8
workspace_default: anthropic:claude-haiku-4-5
(router meta-call times out at 8.0s)

Chain:
  PER_MESSAGE_OVERRIDE   not_applicable
  MANUAL_STICKY          not_applicable
  CONFIGURED_RULES       not_applicable
  PATTERN_RECOMMENDATION not_applicable
  LLM_ROUTER             not_applicable  (timeout)
  DELEGATE_REQUEST       not_applicable  (not a delegation re-entry)
  WORKSPACE_DEFAULT      chose → anthropic:claude-haiku-4-5
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

> **Not yet implemented.** This example illustrates the opt-in
> pattern-disagreement feature (§5.7), which is specified but unbuilt as of
> Wave 16. With the feature off — the current behavior — the chain
> short-circuits when the rule wins (slot 3 `chose`), the pattern slot is
> never evaluated, and no `route.decided` entry, TUI prompt, or `deferred`
> verdict is produced. The example is retained as the intended contract.

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
| `/router llm on\|off`         | Enable/disable the LLM_ROUTER slot for the active workspace (persists to routing.yaml). |
| `/router llm model <id>`      | Set the router model id (persists to routing.yaml).                 |
| `/router llm status`          | Print the current `llm_router:` config + running budget totals.     |
| `/cost`                       | Print this session's cost broken down by model and role.            |

> **Implementation note.** `/route override` and `/route ignore` depend on
> the pattern-disagreement surfacing feature (§5.7) and are **not implemented**
> as of Wave 16. The shipped CLI surface is `/model`, `/cost`, `/models`,
> `/help` plus the per-message `@alias` override; see `AGENTS.md` for the
> authoritative list. `/router llm on|off|model|status` ships in v3.4
> (Wave 19) alongside the `LLM_ROUTER` slot itself.

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

> **Note.** Tests 12–14, 22, 24, and 26 exercise the `delegate()` contract,
> which is now owned by [`delegation.md`](delegation.md) — its test plan is
> authoritative for those. Tests 13 and 22 describe an automatic
> `fast → balanced → deep` capability upgrade that was never implemented
> (§6.9): in the shipped engine a worker tier model that fails validation
> falls through the chain rather than being upgraded.

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

1. **Nested workspace matching.** A workspace inside another; whose rules apply? v1: **exact path match only** — `RoutingPolicy.workspace_for()` matches the session's absolute workspace path exactly; substring / prefix / closest-ancestor matching is a deliberate non-feature. Nested-workspace and symlink resolution deferred.
2. **Cross-session pattern weighting.** Should very recent sessions weight more in K-nearest aggregation? v1: equal weighting. Phase 3 may add recency decay.
3. **Multi-tier delegation depth.** v1 disallows worker-as-planner. Phase 4 may allow bounded recursion.
4. **Streaming for delegation.** Planner currently waits for the worker to fully complete. Streaming worker output back is deferred.
5. **Scheduled rules.** Weekly/monthly windows. v1 has only daily.
6. **Pattern store influencing tier resolution.** `delegate(tier="fast")` could consult patterns to pick among fast-tier models. v1: configured `fast` model is used.
7. **Provider availability state machine.** v1 is binary (Healthy / Unavailable). The Degraded state is sketched but unused; refinement deferred.
8. **`/rules check` shadow detection.** v1 prints rules; v2 may detect when one rule strictly shadows another and warn.
9. **Tier config source.** The `routing.yaml` `tiers:` block (§5.1, §5.2) is parsed and validated but **not consumed** — delegation resolves tiers via the model registry's `delegation_tier` field (§6.10). v2 should either wire `SessionManager.spawn_worker` to consult the workspace-scoped `TierMap`, or drop the `routing.yaml` block in favor of registry config. Until then, per-workspace tier overrides written in `routing.yaml` have no effect.
10. **Cost-effective router model.** §5.6 defaults to `anthropic:claude-haiku-4-5` (safe-bet for reliable forced tool-use as of 2026-06-04); the genuinely best model for the LLM_ROUTER meta-call is open. Cheaper candidates (`qwen3-coder`, `gpt-4o-mini`, `deepseek-v3`, and others sub-$1/MTok) need tool-use reliability verified before they can replace haiku as default. Benchmark against the standard suite (cost per router decision, pick quality vs. workspace-default baseline, tool-call hit rate).
11. **Router decision cache.** v1 makes the meta-call on every turn when enabled; provider-side prompt caching helps the system prompt but the user message changes per turn. A per-workspace decision cache keyed by normalized prompt hash could skip the meta-call for repeated tasks. Deferred until v1 produces traffic and we can measure repeat-prompt rate.
12. **LLM_ROUTER for delegation.** v1 does not let the router invoke `delegate()` — it picks a single concrete model. Allowing the router to return `delegate(tier=…)` would let it split tasks across tiers but breaks the §4.6.4 "router does not second-guess explicit delegate calls" invariant on worker re-entry. Deferred.

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
| 2026-05-20 | Spec re-synced to the Wave-16 implementation (v3.3)                   | Draft v3.2 had drifted from shipped code: slots 4/5 are wired, `team_budget_remaining_lt` was added to the closed predicate set, and §6 was superseded by `delegation.md`. |
| 2026-05-20 | §6 trimmed; `delegation.md` is the canonical `delegate()` contract    | Delegation v1 shipped Wave 10 with its own spec; routing-engine.md §6 keeps only the routing-owned surface (slot 5, tier resolution), §6.1–§6.8 as cross-reference stubs. |
| 2026-05-20 | Automatic `fast → balanced → deep` tier upgrade dropped from the spec | Never implemented. The shipped engine falls a rejected worker-tier candidate through the chain like any other rejection (§6.9). Supersedes the 2026-05-08 "Tier upgrade exhausts at deep" row. |
| 2026-06-03 | LLM_ROUTER slot added at position 5; opt-in per workspace             | Pattern store covers the warm-cluster case; defaults are coarse. An auxiliary LLM that sees the user prompt + candidate catalog is the cold-start complement. Off by default preserves byte-identical behavior for existing deployments. |
| 2026-06-03 | LLM_ROUTER ranked below PATTERN (slot 5 below slot 4)                 | Patterns are cheaper, faster, deterministic per fingerprint, and grounded in observed outcomes. The router fires when patterns are silent. Reversing the order would let probabilistic inference override empirical data. |
| 2026-06-03 | LLM_ROUTER defers in worker re-entry                                  | Planner's explicit `tier=` choice must not be second-guessed; matches slot 4 pattern behavior under delegation (`delegation.md §11`). |
| 2026-06-03 | LLM_ROUTER all failures degrade to `not_applicable`                   | Routing must remain resilient; a flaky router model cannot fail turns. Hard failure still requires every slot to come up empty (§4.8). |
| 2026-06-03 | LLM_ROUTER shares BudgetTracker primitive with evaluator              | Avoids shipping two implementations of token-budget bookkeeping. Caps are independent — the two budgets don't sum into one shared bucket. |
| 2026-06-03 | LLM_ROUTER returns picks via `choose_model(model_id, reason)` tool    | Forces a typed response; model_id enum is built from the candidate set at call time so the router cannot hallucinate a non-existent candidate (provider tool-use enum enforcement permitting). |

---

## 13. References

- `canonical-message-format.md` — `Message`, `ToolDefinition`, `AdapterCapabilities`.
- `event-bus-and-trace-catalog.md` — payload shape for `route.decided` and routing auxiliaries.
- `delegation.md` — canonical `delegate()` tool contract, worker session lifecycle, slot-5 re-entry.
- `pattern-store.md` — task fingerprint design, K-NN aggregation, and the v2 hybrid embedding fingerprint that backs slot 4.
- `streaming-protocol.md` — turn lifecycle events; the three cancellation cases (§3.4).
- `multi-user.md` — data source for the `team_budget_remaining_lt` predicate (§5.3).
