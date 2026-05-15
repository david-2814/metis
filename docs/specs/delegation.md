# Delegation Specification

**Status:** v1 MVP shipped (Wave 10). Streaming worker output to the planner,
cancellation cascade across parent + workers, recursive (worker-spawns-worker)
delegation, structured-output schema validation, and per-tier worker timeout
are deferred to later waves.
**Last updated:** 2026-05-15

> **What landed (Wave 10):** The `delegate()` built-in tool is registered for
> planner sessions whose active model has `can_delegate: true` in the registry.
> The tool body hands a `DelegateRequest` to `SessionManager.spawn_worker`,
> which resolves the tier → model, creates a worker `Session`
> (`is_worker=True`, `parent_session_id` / `parent_tool_use_id` set), runs the
> worker's turn loop synchronously, and returns a `DelegateOutcome`. Routing
> slot 5 fires `chose: <tier model>` inside worker re-entry (§7);
> top-level sessions still see `not_applicable` per the original chain shape.
> Worker LLM events stamp `parent_session_id`; analytics rolls worker spend
> under the planner via `group_by=parent_session` or partitions via
> `group_by=is_worker` (§8). The three `delegate.*` events are live in the
> catalog ([`event-bus-and-trace-catalog.md §6.8`](event-bus-and-trace-catalog.md)).
>
> **v1 scope.** The Phase-4 worker-session design behind the `delegate()`
> tool and the routing chain's slot 5 (`DELEGATE_REQUEST`). Slot 5 had
> existed in the chain enumeration since Phase 1 with
> `verdict: "not_applicable"`; this spec defines the contract that fills
> in the stub.
>
> **Optional wedge.** Delegation is an opt-in capability — neither the
> gateway nor the agent path requires it. Buyers with multi-step workloads
> (planning + many small mechanical sub-tasks) adopt it for the
> planner-on-deep / workers-on-fast cost shape; the routing chain, the
> gateway, the canonical IR, and the savings story all work fine without it.

---

## 1. Purpose

A capable planner model is expensive. Most of the sub-tasks it dispatches
while solving a problem (rename a symbol; format JSON; run a small grep;
re-derive a regex; summarise a file) are mechanical and would run fine on a
cheap model. Today the planner does them itself, paying its own per-token
rate for every mechanical step.

Delegation lets the planner emit a `delegate(tier, task, context)` tool
call. The system spawns a **worker session** — a child session with its own
routing decision, its own (cheaper) model, its own tool dispatch — runs it
to completion, and returns a structured result to the planner. The planner
turn resumes on the planner's model with the worker's output integrated as
the tool result.

This is the third lever in the cost-optimisation thesis
([`STRATEGY.md §4`](../STRATEGY.md)): bounded memory + lossless canonical
IR + **planner→worker delegation**. Without delegation the cost shape is
"one model handles the whole turn." With it, the planner is free to spend
its tokens on judgement and farm out execution.

This spec depends on:

- [`canonical-message-format.md`](canonical-message-format.md) for
  `Message`, content blocks, `ToolDefinition`, `Usage`, and the
  `next_monotonic_ulid()` id convention.
- [`event-bus-and-trace-catalog.md §6.8`](event-bus-and-trace-catalog.md)
  for `delegate.started` / `delegate.completed` / `delegate.failed`
  payloads (Phase 4) and the `Actor.WORKER` enum (Phase 1).
- [`routing-engine.md §6`](routing-engine.md) for the `delegate()` tool
  signature, tier resolution, and slot 5 re-entry semantics. This spec
  consolidates that material and treats §6 as the canonical slot 5
  reference; the engine itself is unchanged.
- [`streaming-protocol.md §6.4 + §7`](streaming-protocol.md) for the
  cancellation-during-delegation seam and the
  `include_worker_sessions` subscribe filter.
- [`server-api.md §sessions`](server-api.md) for the `include_workers`
  query parameter on `GET /sessions` and the `is_worker` /
  `parent_session_id` fields on session records.
- [`pattern-store.md`](pattern-store.md) for the workspace-scoped store
  the worker writes its own fingerprint into (§11).
- [`evaluator.md`](evaluator.md) for the turn-subject judge that scores
  the worker's terminal turn and rolls up into the parent's session
  rubric (§12).
- [`tool-dispatcher.md`](tool-dispatcher.md) for the confirmation policy
  workers inherit from the planner's session (§13).

---

## 2. Goals and non-goals

### 2.1 Goals

1. **Cost shape: planner-on-deep, workers-on-fast.** The default tier map
   puts `delegate(tier="fast")` on a cheap model so mechanical sub-tasks
   stop paying the planner's per-token rate. Cost predictability is what
   makes the feature defensible.
2. **Worker = full session, not a special call.** A worker is a fresh
   session — same canonical IR, same context assembler, same tool
   dispatcher, same trace events. New code paths in v1 are limited to
   spawn / context handoff / result integration. The implementation
   surface is small because the abstractions are shared.
3. **Explainable cost attribution.** Worker tokens land on the worker
   session's `usage.cost_usd`. The parent's session rollup includes
   worker totals broken out by `(worker_session_id, model)`. A user
   asking "where did my dollars go?" gets one record per delegation, not
   a single planner number that hides the breakdown.
4. **Read-only against durable state.** Workers cannot mutate the
   planner's memory, skills, or routing config (§10). The planner has
   the broader context; sub-tasks shouldn't change the planner's
   durable view of the world.
5. **Honest failure modes.** `insufficient_context` is a structured
   request shape, not free text. The planner can programmatically retry
   with the missing references rather than re-prompting itself
   ([`routing-engine.md §6.6.1`](routing-engine.md)).
6. **Cancellation is atomic across parent + workers.** Parent cancel
   cascades into in-flight workers; worker output is discarded; the
   parent's `turn.cancelled` is the user-visible terminator
   ([`streaming-protocol.md §6.4`](streaming-protocol.md)).
7. **Optional.** A buyer that doesn't want delegation never sees it —
   `can_delegate: false` on every model in the registry, no tool
   registration, slot 5 stays `not_applicable`. The savings story
   ([`benchmark.md`](benchmark.md) Run 3) holds without delegation; the
   benefit of turning it on is workload-shaped.

### 2.2 Non-goals

1. **Worker-as-planner (recursive delegation).** v1 disallows. Workers
   do not have the `delegate` tool registered (§5.5). A future phase may
   allow bounded recursion behind a config flag; v1 ships the simpler
   contract.
2. **Streaming worker output to the planner mid-execution.** v1 is
   request/response: the planner's `delegate()` tool call blocks until
   the worker's `delegate.completed` fires. Streaming partial worker
   output is an open question (§14.4) and is deferred.
3. **Multi-worker fan-out from a single `delegate()` call.** One tool
   call = one worker. A planner that wants to fan out four sub-tasks
   emits four `delegate()` calls, which the tool dispatcher may run in
   parallel per its existing concurrency contract
   ([`tool-dispatcher.md`](tool-dispatcher.md)).
4. **Cross-workspace delegation.** Worker workspace = planner workspace.
   v1 does not support delegating into a sibling workspace.
5. **Router-decided delegation.** The routing engine does not look at a
   `TurnContext` and decide "this turn should be a worker" on its own.
   The planner LLM decides via the `delegate()` tool. See §14.6 for the
   open question on whether a router-decided lane would ever earn its
   complexity budget.
6. **Worker UI as a first-class history entry.** Worker sessions are
   reachable for analytics and debugging but hidden from `/history` by
   default ([`routing-engine.md §6.2.2`](routing-engine.md)).

---

## 3. Optionality and adoption

Delegation is gated three ways, in series:

1. **Registry config.** A model has `can_delegate: true` (§4.2). Default
   for `balanced` and `deep`; `fast`-tier models default to `false`.
2. **Active planner model.** The session's currently-active model is one
   of those `can_delegate: true` models. If the user runs `/model haiku`
   on a `can_delegate: false` model, the `delegate` tool is silently
   de-registered for that session until they swap back.
3. **Planner LLM choice.** The planner emits a `delegate()` tool call.
   No automatic delegation — the planner has to ask for it.

A buyer that wants the gateway-only / agent-only experience never trips
any of these. The Phase-4 implementation wave can ship with
`can_delegate: false` on every shipped registry entry and the only
user-visible change is that slot 5 starts producing real verdicts when
the user opts in via the registry config.

The dashboard's per-session cost breakdown shows `Workers: $0.00` when
delegation isn't in use — no separate "delegation-enabled" UI mode.

### 3.6 Explicit v1 MVP deferrals

The Wave-10 implementation lands the spec end-to-end with these features
**not** wired (intentionally; named here so reviewers don't search for them
in the code):

- **Async / concurrent workers.** v1 is synchronous: the planner's tool
  dispatcher blocks on each `delegate()` call until the worker session ends.
  Fan-out via parallel tool calls in a single assistant message is permitted
  by the tool dispatcher's existing contract but not exercised in the v1
  test surface; a per-turn concurrent-workers cap is deferred (§14.3).
- **Cancellation cascade.** A planner cancel does not propagate into an
  in-flight worker in v1. The worker runs to completion and the planner's
  next assistant turn integrates the result. Top-down atomic cancel
  (§6.4 / streaming-protocol.md §6.4) is a later wave.
- **Streaming worker output back to the planner.** v1 blocks until the
  worker emits `stop_reason: end_turn`. Streaming partial worker output
  through the planner's WebSocket subscription (the
  `include_worker_sessions` filter on the streaming protocol) stays
  accepted-but-unused; see §14.4.
- **Worker-spawns-worker (recursive delegation).** The `delegate` tool is
  never registered for worker sessions (`SessionManager._effective_tool_definitions`
  filters it out for any session with `is_worker=True`), and the tool's
  body refuses defensively (`ToolExecutionError`) if a misconfigured
  dispatcher kept it visible. Bounded recursion is a future-phase opt-in
  (§2.2.1).
- **`output_schema` validation.** v1 accepts the optional `output_schema`
  parameter but does **not** validate the worker's output against it; the
  worker's terminal text is returned to the planner unchanged. The
  `output_schema_validation_failed` failure mode is reserved in the
  catalog for the follow-up implementation.
- **Worker wall-clock timeout.** No `timeout_seconds` parameter is exposed;
  `max_tokens` caps spend but not wall time (§14.5).
- **Router-decided delegation.** Slot 5 still only fires inside a worker
  re-entry. The router does **not** wrap a top-level turn in a worker on
  its own (§14.6).
- **Pattern-store integration for workers.** Worker sessions do **not**
  write or read pattern fingerprints in v1 — their structural fingerprint
  would mix with planner fingerprints in a way the K-NN can't usefully
  disambiguate. Cross-link of worker outcomes is §11 future work.

What **does** land in v1: the full §4 / §5 / §6 / §7 / §8 / §9 / §10 / §13
surface — `delegate()` tool, worker `Session` record, routing slot 5
re-entry, full cost attribution, `delegate.*` events, isolation (memory /
skills / `delegate` tool / trust-persistence-suppression for worker
prompts).

---

## 4. The `delegate()` tool

### 4.1 Signature

This re-articulates [`routing-engine.md §6.1`](routing-engine.md); the
canonical source for tool-signature changes remains the routing-engine
spec.

```python
delegate(
    tier: Literal["fast", "balanced", "deep"],
    task: str,                              # focused instruction for the worker
    context: ContextSpec,                   # see §6
    output_schema: dict | None = None,      # optional JSON schema for return
    allowed_tools: list[str] | None = None, # default: same as planner's
    max_tokens: int | None = None,          # cap on worker output
) -> DelegateResult
```

`ContextSpec` is the union of `{"mode": "minimal"}` and
`{"mode": "explicit", "include": [...]}` per
[`routing-engine.md §6.3`](routing-engine.md).

### 4.2 Tool registration

```yaml
# part of the model registry (registry.yaml or equivalent)
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

`can_delegate: false` means the `delegate` tool is not registered when
this model is the active session model. Enforced at session start and
when the active model changes mid-session (after a `/model` swap or a
queued swap landing at a turn boundary).

The tool is **never** registered for a worker session, regardless of
`can_delegate` on the worker's model (v1 non-goal §2.2.1).

### 4.3 Return value

```python
class DelegateResult(msgspec.Struct, frozen=True, kw_only=True):
    success: bool
    output: str | dict                  # text by default; dict if output_schema set
    error: str | None
    usage_summary: DelegateUsageSummary # tokens, cost, turns, tool calls
    worker_session_id: str              # for trace lookup

class DelegateUsageSummary(msgspec.Struct, frozen=True, kw_only=True):
    model: str                          # the resolved worker model
    turn_count: int
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal                   # workforce of /analytics/cost rollup
    wall_time_seconds: float
    tool_call_count: int
```

The planner sees `output` as the tool result content; the rest is
recorded on `delegate.completed` ([`event-bus §6.8`](event-bus-and-trace-catalog.md))
and surfaced in analytics rather than passed back to the LLM. Keeping
the planner-visible portion narrow prevents the planner from being
flooded with metadata.

### 4.4 Failure modes

Identical to [`routing-engine.md §6.6`](routing-engine.md); reproduced
here for completeness:

| Failure                                     | success | error                                | output                  |
|---------------------------------------------|---------|--------------------------------------|-------------------------|
| Worker raised an unhandled error            | false   | `"worker_error: {message}"`          | partial output if any   |
| Worker hit `max_tokens`                     | false   | `"max_tokens_exceeded"`              | truncated output        |
| Worker requested missing context            | false   | `"insufficient_context"`             | `InsufficientContextRequest` |
| Worker output didn't match `output_schema`  | false   | `"output_schema_validation_failed"`  | raw output              |
| No model available for `tier`               | false   | `"no_model_available_for_tier"`      | empty                   |
| User cancelled the planner mid-delegation   | false   | `"cancelled_by_user"`                | partial if any          |

The `insufficient_context` shape lives in
[`routing-engine.md §6.6.1`](routing-engine.md) and is referenced by the
typed `delegate.failed.insufficient_context_request` field on the bus
event.

---

## 5. The worker session

### 5.1 Definition

A **worker session** is a regular `Session` record (per
[`canonical-message-format.md §9.1`](canonical-message-format.md)) with
two additional fields populated:

```python
# additive fields on the existing Session record
parent_session_id: str | None     # the planner's session_id; None for top-level sessions
parent_tool_use_id: str | None    # the planner's delegate() tool_use_id; uniquely
                                  # identifies which delegate() call this worker
                                  # belongs to
is_worker: bool                   # parent_session_id is not None, materialised for
                                  # quick filtering on /sessions
```

The fields are nullable for backward-compatibility with sessions written
before Phase 4. No schema migration is required beyond adding the columns
with `DEFAULT NULL`.

### 5.2 Routing inheritance

A worker session has its own routing decision, made fresh at worker
session start via the full 7-slot chain in
[`routing-engine.md §4.1`](routing-engine.md). The planner's
`active_model` does **not** propagate. The chain enters slot 5
(`DELEGATE_REQUEST`) with the resolved-tier model as the candidate; all
earlier slots typically return `not_applicable` for workers (no `@model`
prefix, no user `/model` command, no rule matches against the task brief
by default). See [`routing-engine.md §6.9 + §6.9.1`](routing-engine.md)
for why the full chain runs and what the worker's `route.decided` looks
like.

### 5.3 Workspace

`workspace_path` = parent's `workspace_path`. Same directory, same
`.metis/` config, same MEMORY.md / USER.md / `routing.yaml` /
`patterns.db` / `trust.yaml`. Workers are not run in a sandbox
subdirectory; the workspace is the unit of trust.

This means worker file-tool reads see the same workspace state the
planner saw at delegation time, including any files the planner created
or modified earlier in the session. There is no copy-on-write semantics
for the filesystem.

### 5.4 Memory

The worker's system prompt is assembled by the same context-assembler
path as the planner's (per
[`context-assembler.md §5`](context-assembler.md)) and includes the
workspace's MEMORY.md + USER.md. The worker reads them but **cannot
modify them**: the `memory_add` / `memory_replace` / `memory_consolidate`
tools are not registered for worker sessions, even if listed in
`allowed_tools`.

Rationale: the planner has the broader context to judge what's worth
remembering. A worker shouldn't change the planner's durable view of the
world from inside a sub-task — and a planner that wants a fact recorded
in MEMORY.md after a worker returns is free to call `memory_add` itself
with the worker's output as input.

### 5.5 Skills

The worker can read and load skills (`skill_load` is available; the
worker's session-start skill index is built the same way the planner's
is). The worker **cannot** create, modify, or delete skill files. Skill
auto-generation (Phase 2.5; not yet shipped) is not invoked from worker
sessions, regardless of policy.

### 5.6 Tools

Default `allowed_tools` = the set the planner had. Workers retain access
to the same file / shell / search / network surface unless the planner
narrowed `allowed_tools` in the `delegate()` call.

Three tools are **always** absent from worker dispatch, regardless of
`allowed_tools`:

- `delegate` — v1 forbids recursive delegation (§2.2.1).
- `memory_add` / `memory_replace` / `memory_consolidate` — read-only
  memory invariant (§5.4).
- Skill-mutation tools, if any exist in a future phase (§5.5).

If the planner names one of these in `allowed_tools`, the worker session
silently drops it from its registry. No error — the planner's request is
honoured to the extent the contract allows. The list of dropped tools is
recorded on `delegate.started` (additive payload field `dropped_tools`
proposed below; see §9).

### 5.7 System prompt

The worker's system prompt is composed from:

1. The standard context-assembler stable prefix (operating context, MEMORY.md,
   USER.md, allowed-tools description) — same as a planner.
2. A worker-specific instruction block teaching it to be terse, focused, and
   to return rather than ask for clarification. The exact wording lives in
   [`routing-engine.md §6.5`](routing-engine.md) and is unchanged here.
3. The `task` brief from the `delegate()` call.
4. The assembled `context` references (inline notes, message snippets, tool
   results; files appear as references the worker re-reads through the file
   tool — see [`routing-engine.md §6.3`](routing-engine.md)).

The worker has no prior message history. Its first turn is the system prompt
+ a synthetic user message that contains the task brief and any inline
context. The planner is not a participant in the worker's transcript.

---

## 6. Lifecycle

### 6.1 Creation

```
[planner turn in flight on session sess_A, model = anthropic:claude-opus-4-7]

planner emits tool_use(delegate, {tier: "fast", task: "...", context: {...}})
  ↓
tool dispatcher dispatches delegate as a builtin tool with elevated kernel
  privileges (it can spawn a session — no other builtin can).
  ↓
delegate-tool body:
  1. Resolve tier → model via tier-config (per workspace, falling back to
     global). If no model is configured for the tier, return DelegateResult
     with error=no_model_available_for_tier and emit delegate.failed.
  2. Create a fresh Session record with parent_session_id=sess_A,
     parent_tool_use_id=tu_xxx, is_worker=true, workspace_path=parent's.
     Emit session.created (existing event; the worker fields are populated).
  3. Emit delegate.started with worker_session_id, tier, resolved_model,
     context_mode, context_reference_count, task_size_tokens
     (per event-bus §6.8).
  4. Build the worker's system prompt (§5.7) and synthetic user message.
  5. Invoke the session manager's turn loop on the worker. The session
     manager runs routing (entering slot 5 with the resolved model as the
     candidate; capability validation per routing-engine §4.4), then the
     LLM call cycle, exactly like any other session.
  6. Block on the worker's session manager future until session.ended or
     a terminal delegate.failed condition is observed.
  7. Compose DelegateResult from the worker's session outcome and return
     it to the planner's tool dispatcher.
```

### 6.2 Inside the worker

A worker session runs the same turn-locked loop as any other session
([`routing-engine.md §3.2`](routing-engine.md)). The worker's model is
fixed at the worker's turn start and owns all of the worker's LLM
calls, including any tool cycles.

A worker can have multiple turns internally. The default contract is
"one synthetic user turn → completion," but if the worker's first turn
emits a non-`end_turn` stop_reason (e.g., a tool cycle that requires
multiple LLM calls), the worker continues until it produces an assistant
message with `stop_reason: end_turn`. The full transcript is persisted
under the worker's session record.

The worker's terminal LLM output is the candidate for `DelegateResult.output`.
If `output_schema` is set, the worker is expected to produce JSON
matching the schema; the delegate tool body validates the parsed output
before returning. Schema-validation failure is `output_schema_validation_failed`.

### 6.3 Completion

When the worker reaches `stop_reason: end_turn` (or terminates via one of
the failure modes in §4.4):

1. Worker emits `session.ended` with `disposition: completed` (or
   `failed`/`cancelled` as appropriate).
2. The delegate-tool body collects the worker's `Usage` totals from the
   session-manager future result.
3. Delegate-tool body emits `delegate.completed` (or `delegate.failed`)
   with the usage rollup
   ([`event-bus §6.8`](event-bus-and-trace-catalog.md)).
4. Delegate-tool body returns `DelegateResult` to the planner's tool
   dispatcher, which packages it as a `TOOL` content block on the
   planner's next message (standard tool-cycle wiring per
   [`tool-dispatcher.md`](tool-dispatcher.md)).
5. The planner's turn resumes on the planner's model with the worker
   output as the tool result. The planner's `turn.completed` is unchanged
   in shape; `usage.cost_usd` on that event still measures the
   *planner's* token cost only. Worker cost lives on `delegate.completed`
   and rolls up via the analytics projection (§8).

### 6.4 Cancellation

Cross-references [`streaming-protocol.md §6.4`](streaming-protocol.md);
not redefined here. Summary:

- Cancel originating on the planner → planner's delegate-tool body
  signals the worker's session manager → worker's in-flight LLM call /
  tool dispatch cancels per the standard three-case model
  ([`streaming-protocol.md §6.2`](streaming-protocol.md)) → worker
  emits `session.ended` with `disposition: cancelled` → delegate-tool
  body emits `delegate.failed` with `failure_mode: cancelled_by_user`
  → planner's `turn.cancelled` follows.
- Cancel originating *directly on a worker* (a client attached only to
  the worker session via §11): same worker-side cancellation, but the
  planner is **not** notified. The planner's delegate-tool call sees a
  `delegate.failed` result and decides what to do
  (retry / take over / surface). v1: the planner sees this as
  `cancelled_by_user` with the qualification that the user cancelled
  *the worker*, not the planner. The planner's turn continues.

The atomic cascade only fires for top-down cancellation. Worker-only
cancel is treated as the worker failing — a normal failure mode the
planner already needs to handle.

### 6.5 Worker timeout

v1 does not impose a wall-clock timeout on workers beyond `max_tokens`
(per `delegate()` arg) and the planner's user being able to Ctrl-C.
Adding a per-tier worker timeout is an open question (§14.5).

---

## 7. The slot 5 contract (cross-reference)

This section is non-normative; slot 5's canonical definition lives in
[`routing-engine.md §6.9`](routing-engine.md).

When the delegate-tool body spawns a worker session, the worker's
session-manager runs the standard policy chain. The chain shape is the
same as for any other session:

```
1. PER_MESSAGE_OVERRIDE   → not_applicable (no user @-prefix on the synthetic task)
2. MANUAL_STICKY          → not_applicable (workers have no /model state)
3. CONFIGURED_RULES       → typically not_applicable; will fire if a user
                            wrote a workspace rule whose predicate matches
                            the worker's task brief — rare
4. PATTERN_RECOMMENDATION → not_applicable in v1 unless the worker's
                            structural fingerprint resolves a high-confidence
                            recommendation in the per-workspace store
5. DELEGATE_REQUEST       → chose: <resolved tier model>  (typical)
6. WORKSPACE_DEFAULT      → would chose if 5 was rejected on capability
7. GLOBAL_DEFAULT         → final floor
```

Slot 5 fires only inside a worker session. The chain always includes the
slot for shape uniformity; outside a worker session slot 5 reports
`verdict: not_applicable, reason: "not in delegation re-entry"`.

Capability validation runs on slot 5's candidate exactly like any other
slot. If the worker's task has images and the resolved `fast` model is
text-only, slot 5 rejects with `no_vision_support` and the chain falls
through — `WORKSPACE_DEFAULT` or a tier-upgrade rule typically catches
the case ([`routing-engine.md §6.9`](routing-engine.md)).

---

## 8. Cost attribution

### 8.1 Where worker tokens land

Every `llm.call_completed` emitted from inside a worker session has:

- `session_id` = the worker's session id.
- `is_worker: true` (already in the catalog —
  [`event-bus §6.3`](event-bus-and-trace-catalog.md)).
- `usage.cost_usd` = the worker's call cost, in Decimal, against the
  worker's model and the active pricing overlay
  (`pricing_version` is recorded as today).

The planner's `llm.call_completed` events are unchanged — their
`usage.cost_usd` is the planner's own cost, regardless of what workers
spent inside the same turn.

### 8.2 Rollup via the analytics surface

The `/analytics/cost` projection (per
[`analytics-api.md §4.1`](analytics-api.md)) already groups by
`session_id`. For Phase 4 it gains an additive behaviour: when an
`include_workers=true` query parameter is passed, the projection rolls
worker cost into the parent's session row via the
`parent_session_id` field on the worker's session record. Two new
optional dimensions are added to `_COST_GROUP_BY_ALLOWED`:

- `group_by=parent_session` — rolls every event under its session's
  `parent_session_id` if set, else the session's own id. Worker cost
  collapses into the planner's row.
- `group_by=is_worker` — partitions the response into "planner" vs.
  "worker" buckets without identifying which planner each worker
  belonged to.

The dashboard rendering described in
[`routing-engine.md §6.7`](routing-engine.md) uses `group_by=session,
include_workers=true` to render the per-session breakdown.

### 8.3 `delegate.completed.worker_total_cost_usd`

The summary cost on the `delegate.completed` event is **derived** — a
sum of the worker's `llm.call_completed.usage.cost_usd` values. The
analytics projection does not read this field for the user-facing
rollup; it reads `llm.call_completed` rows directly via the worker's
`session_id`. The summary on the event exists for at-a-glance debug
("how expensive was this one delegation?") and for the dashboard's
delegate-tooltip render.

Rationale: a single source of truth (`llm.call_completed`) avoids
double-counting if the rollup and the summary ever drift. Re-pricing a
historical trace under a new `pricing_version` reaches every
`llm.call_completed` whether or not the `delegate.completed` summary
matches.

---

## 9. Trace events (cross-reference)

The three Phase-4 events live in
[`event-bus-and-trace-catalog.md §6.8`](event-bus-and-trace-catalog.md):
`delegate.started`, `delegate.completed`, `delegate.failed`.

This spec proposes two additive payload fields on `delegate.started`:

```python
# delegate.started payload, additions in italics:
{
    "tool_use_id": str,
    "worker_session_id": str,
    "tier": Literal["fast", "balanced", "deep"],
    "resolved_model": str,
    "context_mode": Literal["minimal", "explicit"],
    "context_reference_count": int,
    "task_size_tokens": int,
    # additive (v1):
    # *"allowed_tool_count": int,*       # tools the planner asked for
    # *"dropped_tools": list[str],*       # tools removed per §5.6 invariants
}
```

Sensitivity stays `pseudonymous` — tool names are structural. The new
fields are populated whenever a worker is spawned; rows written before
Phase 4 (none — the event type doesn't exist yet) need no migration.

`Actor.WORKER` (event-bus §4.1, already in the catalog) is the actor on
every event emitted inside a worker session.

---

## 10. Isolation summary

| Surface           | Worker read | Worker write | Enforcement                                   |
|-------------------|-------------|--------------|-----------------------------------------------|
| MEMORY.md         | ✓ (composed into system prompt) | ✗ | Memory tools de-registered (§5.4).            |
| USER.md           | ✓                                | ✗ | Same.                                         |
| Skills            | ✓ (load + read)                  | ✗ | Skill-mutation tools de-registered (§5.5).    |
| `routing.yaml`    | ✓ (loaded by routing engine)     | ✗ | No mutation tool exists in v1; absent if added. |
| `patterns.db`     | ✓ (slot 4 query)                 | ✓ (worker's own outcomes — §11) | Worker writes its own session's fingerprint. |
| `trust.yaml`      | ✓ (confirmation policy)          | ✗ in v1 — see §13                            | Workers inherit; "always" answers do not persist from worker prompts. |
| Workspace files   | ✓                                | ✓ (via file tools, same as planner) | Standard tool dispatch / confirmation.     |
| `metis.db` (trace)| ✓ (own events)                   | ✓ (own events) | Worker emits via the same bus as planner.     |
| Sessions / `delegate` tool | ✓ (worker can't see other sessions) | ✗ (no `delegate` tool registered) | Dispatch-registration invariant (§5.6).  |

The principle: workers are read-only against **durable system state the
planner is reasoning about**, and read/write against task-shaped state
(workspace files, the worker's own trace, the worker's own pattern row).

---

## 11. Pattern store integration

A worker session writes a pattern row exactly like a top-level session.
At `session.ended` the pattern-store subscriber projects the worker's
turn(s) into a structural fingerprint and stores it with
`primary_model` = the worker's model
([`pattern-store.md §5`](pattern-store.md)). The `parent_session_id` on
the worker's session record is *not* projected into the fingerprint —
the pattern store treats workers as first-class fingerprintable units.

This is deliberate: routing slot 4 for *future* worker sessions will
read these rows. If sonnet-on-tier=balanced consistently beats
haiku-on-tier=fast for `regex_with_edge_cases`-shaped sub-tasks, the
pattern store accumulates that signal and a future worker turn will
match into slot 4 *before* slot 5 fires — at which point the engine
either keeps the slot-5 tier (if validation passes for slot 4's higher
score) or follows the pattern's recommendation.

A naive read of the chain would have slot 4 (pattern) outrank slot 5
(delegate) by virtue of its position. That would let learned patterns
silently override the planner's explicit `tier=` choice — exactly the
failure mode the chain ordering ([`routing-engine.md §4.2`](routing-engine.md))
defends against for user-set policy.

**Resolution.** v1 forces slot 4 to defer to slot 5 inside worker
re-entry: if a `DELEGATE_REQUEST` is in flight, the pattern slot still
runs its evaluation for trace purposes (so the disagreement is observable
in `route.decided.chain`) but always returns `verdict: deferred` with
`reason: "delegate_request_in_flight"`. The planner's explicit `tier=`
is treated as an intentional cost/quality choice that learned patterns
should not silently override. The planner can adjust by choosing a
different tier on the next `delegate()` call; the dashboard surfaces
the disagreement.

This is the worker-mode analogue of
[`routing-engine.md §5.6`](routing-engine.md) (rule beats pattern
recommendation by default). The decision log records the rationale
(§15).

---

## 12. Evaluator integration

The evaluator subscribes to `turn.completed` and `session.ended`
([`evaluator.md §6.1`](evaluator.md)). Worker sessions emit those events
exactly like any other session — they are scored independently.

Two downstream behaviours:

1. **Worker's terminal turn is scored by the turn rubric.** The
   evaluator's heuristic/hybrid/LLM tiers run against the worker's last
   assistant turn. The `eval.completed` event records the worker's
   `session_id`. Score lands in the pattern store via the
   pattern-store's late-arriving-score flow
   ([`pattern-store.md §10.4`](pattern-store.md)).
2. **Parent's session rubric folds in delegation outcomes.** When the
   evaluator scores the parent session
   ([`evaluator.md §5.6`](evaluator.md), heuristic-only in v1), it
   incorporates each child worker's success signal via the
   `delegate.completed.success` boolean. A planner whose three of four
   delegations failed gets a lower session score than one whose
   delegations succeeded. The exact weighting is heuristic and lives
   in the evaluator's rubric; this spec does not pin it.

The evaluator does **not** re-score the parent's *turn* using the
worker's evaluator verdict. The parent's turn is evaluated on the
planner's own output (text + tool-use behaviour), and the workers are
evaluated independently. Otherwise the parent's score double-counts the
workers' scores transitively, which inflates the apparent gain from
delegation and distorts the savings story.

---

## 13. Confirmation handler scope

Worker tool calls go through the same `ToolDispatcher` and same
`ToolConfirmationHandler` instance as the planner's
([`tool-dispatcher.md`](tool-dispatcher.md)). The session-manager
constructs the dispatcher with the active handler (CLI / remote /
auto-allow per the CLI runtime), and the worker reuses it.

Consequence: a worker's WRITE/EXECUTE/NETWORK tool calls produce the
same prompts the planner's would. If the user said "always allow shell"
in the planner session, the worker inherits that (the `trust.yaml`
entry is workspace-scoped, not session-scoped).

What v1 does **not** allow:

- **"Always" answers from worker prompts persisting to `trust.yaml`.**
  A worker dispatching `shell` for the first time and the user
  answering "always" should be treated as a one-time approval for this
  worker only — v1 conservatively suppresses the persistence. Rationale:
  the user is approving the worker's specific sub-task; promoting that
  to a workspace-wide policy is too implicit when the user didn't
  initiate the action. The planner remains free to call shell directly
  with a normal prompt whose "always" answer does persist.

This persistence-suppression rule is a v1 conservative default. Whether
worker prompts should be allowed to persist trust answers is an open
question (§14.7).

---

## 14. Open questions

Tracked here; v1 does not resolve.

### 14.1 Cost-of-delegation overhead

Worker spawn isn't free: a fresh session, system-prompt assembly, an
LLM call with no warm cache, and a structured return. The per-call
fixed cost is on the order of input-token assembly for the worker's
system prompt — bounded but not zero.

For a small sub-task (e.g. "format this 200-token JSON blob"), the
worker's fixed cost may exceed the planner's cost of doing it inline.
The threshold below which delegation is net-negative depends on:

- Tier ratio (planner $/Mtok vs. worker $/Mtok).
- Sub-task token budget (input + output).
- Prompt-cache warm-rate ([`context-assembler.md §5.1`](context-assembler.md))
  — the planner's cache warms across the session; each worker session
  starts cold.

[`benchmark.md`](benchmark.md) Phase 4 should add a `delegation-vs-inline`
workload pair that runs the same task both ways and measures the cost
difference. The planner's system prompt should be tuned (via the
worker-decision guidance described in
[`routing-engine.md §6.4`](routing-engine.md)) to delegate only when
the sub-task is large enough for the ratio to win.

### 14.2 Cancellation cascade scope

§6.4 specifies the top-down cascade. Open: should a worker that is
**not** in-flight at cancel time (e.g., the worker already
`session.ended` but the planner hasn't yet integrated the result) be
"un-completed"? v1: no — once `delegate.completed` fires, the
worker's record is durable; cancelling the planner just suppresses the
planner's further LLM calls. This may surprise a user who expects "cancel
= rollback"; deferring to Phase 4 ergonomics.

### 14.3 Concurrent delegation (one planner, many workers)

The tool dispatcher's existing concurrency contract
([`tool-dispatcher.md`](tool-dispatcher.md)) allows multiple tool calls
to run in parallel within a single turn. A planner emitting four
`delegate()` calls in one assistant message could spawn four worker
sessions concurrently.

v1: allowed, no explicit cap. The cap that exists in practice is the
tool dispatcher's per-turn concurrent-tool limit. Worker sessions
contribute to that limit equally with other tools. Whether to add a
per-turn `max_concurrent_workers` knob is a Phase-4 polish question.

### 14.4 Streaming worker output to the planner

The planner currently waits for the worker to fully complete. A
streaming worker that emits partial output to the planner mid-execution
would change the loop in ways the canonical message format doesn't yet
model (a tool result that's a partial state). This is
[`streaming-protocol.md §12.2`](streaming-protocol.md)'s open question
and is mirrored here. v1: blocking.

### 14.5 Worker wall-clock timeout

Should `delegate()` accept a `timeout_seconds` argument that cancels the
worker if exceeded? The cost cap (`max_tokens`) bounds spend but not
wall time. v1: no, deferred. Add only if real workloads hit
wall-time-runaway scenarios.

### 14.6 Router-decided delegation

Should the routing engine, observing a `TurnContext` with
`tool_call_count_projected > N` or
`estimated_input_tokens > M`, decide on its own that the *whole turn*
should be a worker session? This would be a new slot 5 mode where slot
5 fires *outside* a delegate-tool call — i.e., the engine wraps the
turn in delegation without the LLM asking.

v1: no. The user prompt requested this be considered. The argument
against:

- The planner LLM is the only entity with enough context to decide what
  to delegate vs. do inline. A predicate-based router can't tell
  "refactor this auth flow" from "rename this variable in src/auth.ts."
- A router-decided slot 5 would silently change which model handles a
  user-facing turn — exactly the failure mode `routing-engine.md §4.2`
  defends against ("user intent prevails over system suggestions").

The argument for: it could rescue users whose planners don't reliably
emit `delegate()` calls. Phase 4 may revisit; v1 deliberately makes the
planner the only delegation-decider.

### 14.7 Worker-prompt "always" answers persisting to trust.yaml

§13 conservatively suppresses persistence from worker prompts. Whether
to lift the restriction (and how to surface to the user that a worker
asked for the policy change, not the planner) is an open ergonomics
question.

### 14.8 Tier name configurability

[`routing-engine.md §6.10`](routing-engine.md) hardcodes the tier names
`fast` / `balanced` / `deep`. Some buyers may want richer taxonomies
(`code` / `math` / `agent` tiers). v1: hardcoded; Phase 4+ may surface.

### 14.9 Worker history visibility

[`routing-engine.md §6.2.2`](routing-engine.md) hides workers from
`/history` by default with `--include-workers` opt-in. Whether the
dashboard's session-list view defaults to including or excluding
workers is a UX choice the spec doesn't pin; the API surface
(`GET /sessions?include_workers=true`) is in
[`server-api.md`](server-api.md).

---

## 15. Decision log

| Date       | Decision                                                                  | Rationale                                                                                                  |
|------------|---------------------------------------------------------------------------|------------------------------------------------------------------------------------------------------------|
| 2026-05-14 | Delegation is opt-in; default registry has `can_delegate: false` on fast | Buyers without multi-step workloads shouldn't see the surface; savings story holds without it.             |
| 2026-05-14 | Worker = full session, not a special LLM call                             | Re-uses canonical IR, context assembler, tool dispatcher, trace catalog. Smallest implementation surface.  |
| 2026-05-14 | Worker workspace = planner workspace; no sandbox subdirectory             | Trust unit is the workspace. Copy-on-write filesystem semantics are out of scope for v1.                   |
| 2026-05-14 | Workers read-only against MEMORY.md / USER.md / skills / routing config   | Planner has broader context; sub-tasks shouldn't mutate durable system state the planner is reasoning about. |
| 2026-05-14 | Workers cannot delegate (no `delegate` tool registered)                   | Prevents recursion and fan-out cost explosions. Bounded recursion deferred to a future phase.              |
| 2026-05-14 | Slot 4 (pattern) always defers inside worker re-entry                     | Planner's explicit `tier=` is an intentional cost/quality choice that learned patterns shouldn't override silently. |
| 2026-05-14 | Slot 5 always present in the chain; reports `not_applicable` outside delegation | Chain shape is fixed for trace uniformity; uniform predicates win over per-session chain shapes. |
| 2026-05-14 | Worker terminal turn scored by evaluator independently; parent session rubric folds in delegate success | Avoids double-counting transitive worker scores into the parent's turn score, which would distort the savings story. |
| 2026-05-14 | Worker cost lands on worker's `llm.call_completed`; `delegate.completed.worker_total_cost_usd` is derived | Single source of truth (`llm.call_completed`) avoids drift on re-pricing.                         |
| 2026-05-14 | Top-down cancellation cascades atomically (planner → in-flight workers)  | Matches `streaming-protocol.md §6.4`; user-visible terminator is the planner's `turn.cancelled`.           |
| 2026-05-14 | Worker confirmation handler inherits planner's; "always" answers from worker prompts do NOT persist to trust.yaml in v1 | Conservative default — user is approving a worker sub-task, not a workspace-wide policy. Open question (§14.7). |
| 2026-05-14 | One worker per `delegate()` call; planner fan-out via multiple tool calls | Existing tool-dispatcher concurrency model handles fan-out; no new contract needed.                        |
| 2026-05-14 | Worker streaming back to planner deferred (blocking only in v1)           | Partial tool-result state isn't modeled in the canonical IR; deferred per `streaming-protocol.md §12.2`.   |
| 2026-05-14 | Router-decided delegation (slot 5 firing outside `delegate()` call) deferred | Predicate-based routing can't distinguish "delegate-worthy" sub-tasks from non-delegatable ones; the LLM has the context. |
| 2026-05-14 | `delegate.started` gains additive `allowed_tool_count` and `dropped_tools` fields | Lets the dashboard explain why a worker behaved as if it had fewer tools than the planner asked for.       |

---

## 16. References

- [`routing-engine.md §6`](routing-engine.md) — canonical `delegate()`
  tool signature, tier resolution, slot 5 re-entry, `InsufficientContextRequest`
  schema, `can_delegate` flag.
- [`event-bus-and-trace-catalog.md §6.8`](event-bus-and-trace-catalog.md) — Phase-4
  `delegate.started` / `delegate.completed` / `delegate.failed` payload schemas;
  `Actor.WORKER`; `is_worker` on `llm.call_started`.
- [`streaming-protocol.md §6.4 + §7`](streaming-protocol.md) — cancellation
  cascade across parent + worker; `include_worker_sessions` subscribe filter;
  direct worker WebSocket attach.
- [`server-api.md`](server-api.md) — `is_worker` / `parent_session_id` fields
  on session records; `include_workers` query parameter on `GET /sessions`.
- [`canonical-message-format.md §9.1`](canonical-message-format.md) — Session
  schema; the additive `parent_session_id` / `parent_tool_use_id` / `is_worker`
  columns.
- [`pattern-store.md`](pattern-store.md) — worker writes its own fingerprint
  row; parent_session_id is not projected into the fingerprint.
- [`evaluator.md §5.6 + §6.1`](evaluator.md) — worker terminal turn scored
  independently; parent session rubric folds in `delegate.completed.success`.
- [`tool-dispatcher.md`](tool-dispatcher.md) — registration, confirmation
  policy inheritance, per-turn concurrent-tool contract.
- [`context-assembler.md §5`](context-assembler.md) — worker's system prompt
  assembled by the same path as planner's, including MEMORY.md / USER.md / skill
  index.
- [`STRATEGY.md §4`](../STRATEGY.md) — the third lever (planner→worker
  delegation) in the cost-optimisation thesis.
