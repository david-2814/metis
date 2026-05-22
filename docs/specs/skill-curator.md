# Skill Curator Specification

**Status:** Draft v1 (not yet implemented). The gate — agent-authored skills — **landed 2026-05-20** (`skill_save` tool + `skill.created` event, [`skill-format.md §8.3`](skill-format.md)); the curator is now unblocked as its own implementation task (Phase 2.5b).
**Last updated:** 2026-05-20

> Periodic auxiliary-model maintenance of **agent-authored** skills. The
> curator pins what is being used, archives what has gone stale, consolidates
> near-duplicates, and patches small body errors — bounded by a shared cost
> cap, **never auto-deleting**, and never touching user-authored skills.
>
> The pattern is lifted from Nous Research's hermes-agent (`agent/curator.py`,
> ~75KB; defaults `interval_hours=168`, `stale_after=30d`, `archive_after=90d`).
> Hermes treats this as the self-improving loop that makes a long-lived agent's
> skill library survive contact with reality: skills that get loaded a lot
> earn priority; skills that never get loaded shouldn't pay rent in the
> discovery index forever; near-duplicates merge themselves.
>
> Metis adopts the pattern verbatim where it is cheap and good, and tightens
> two spots: (a) the curator runs against the trace store's
> `skill.loaded` events as the activity signal (no separate activity table),
> and (b) the auxiliary-model spend lives under the same
> [`BudgetTracker`](../../packages/metis-core/src/metis_core/eval/budget.py)
> primitive the evaluator uses, so a runaway curator can't outspend a runaway
> evaluator silently.

---

## 1. Purpose

The skills loader ([`skill-format.md`](skill-format.md)) is read-only: it
parses SKILL.md files the user already put on disk. As Phase 2.5 lands
**agent-authored skills** (`skill.created` with `source="auto_generated"`
per [`event-bus-and-trace-catalog.md §6.6`](event-bus-and-trace-catalog.md)),
the library starts to grow without an operator pruning it. Three failure
modes show up empirically in agent-authored libraries:

1. **Stale skills.** A skill the agent created during one project keeps
   appearing in the discovery index of every future session, paying rent in
   the cached system prefix for nobody. Discovery-index cost grows
   monotonically with library size.
2. **Near-duplicates.** The agent creates `code-review-python` then later
   `python-code-review` then later `review-python-code`. Each occupies a
   distinct skill_id and a distinct discovery-index line, all describing
   substantially the same procedure. The agent stops knowing which to load.
3. **Decay.** A skill that worked when written stops working after a tool
   contract changes upstream. The body still references the old tool name;
   the agent loads it and fails silently. A small auxiliary-model patch
   fixes it; without curation, it rots.

The curator is the periodic auxiliary-model task that owns these three.
It runs **bounded** (per-run and per-day cost caps), **rarely** (default
weekly), and **safely** (never deletes; never touches user-authored
skills; every action emits an audit event).

This spec depends on:

- [`skill-format.md`](skill-format.md) for the `Skill` / `SkillStore` types,
  the two-root merge (global + workspace), and the agentskills.io frontmatter
  contract (which this spec **does not extend** — invariant 4 in §11).
- [`event-bus-and-trace-catalog.md §6.6`](event-bus-and-trace-catalog.md) for
  `skill.loaded` (the activity signal) and `skill.created` / `skill.modified`
  (the existing skill-domain events).
- [`evaluator.md §7`](evaluator.md) for the
  [`BudgetTracker`](../../packages/metis-core/src/metis_core/eval/budget.py)
  primitive (per-run + per-day USD caps with a single source of truth).
- [`canonical-message-format.md §6.4`](canonical-message-format.md) for the
  `pricing_version` + `cost_usd` (`Decimal` serialized as string) conventions.
- [`analytics-api.md`](analytics-api.md) for how curator spend rolls up into
  the existing `/analytics/cost` surface.
- [`memory-store.md`](memory-store.md) as the sister "soft-cap → event,
  hard-cap → reject" pattern this spec mirrors.

---

## 2. Goals and non-goals

### 2.1 Goals

1. **Owner-grade safety.** The curator never deletes a skill. The strongest
   destructive action is archive (move to a sibling directory under
   `skills-archive/`), which is reversible by `mv`.
2. **Read-only against user-authored skills.** The curator only touches
   skills whose `skill.created.source` was `"auto_generated"` or
   `"curator_generated"`. Skills installed manually by the user (no
   `skill.created` event, or `source="manual"`) are observed but never
   modified.
3. **Bounded spend.** Two caps (`curator.per_run_max_usd`,
   `curator.per_day_max_usd`), shared `BudgetTracker` with the evaluator
   ([§7](#7-budget-and-safety)). A capped-out run emits a
   `curator.throttled` reason on its summary event and exits without writes.
4. **Audit trail.** Every curator action emits exactly one `skill.curated`
   event with the model, cost, latency, before/after `skill_version`, and
   a free-form `rationale`. No silent state changes.
5. **Inactivity-triggered, not cron-daemon.** The curator runs at
   `session.ended` boundaries when an interval has elapsed; the operator
   can also run it explicitly via `metis curate <workspace>`. The runtime
   does not spawn a background thread.
6. **Pin survives.** A pinned skill bypasses every auto-transition (archive,
   consolidate, edit). Pinning is a user-controllable affordance — the
   curator never auto-pins.
7. **Conform to agentskills.io.** No new SKILL.md frontmatter fields. State
   (pin / archive / curator-origin / lineage) lives in a sidecar
   `curator/state.json` per root, not in the skill directory itself.

### 2.2 Non-goals

1. **User-authored skill maintenance.** If a user wrote a SKILL.md by hand,
   the curator can read its body and emit `skill.curated(action="suggest")`
   advisories (deferred to §13.5) but cannot mutate it.
2. **Background daemon mode.** No long-running thread, no cron schedule
   from inside the process. `metis serve` does not run a curator loop
   while idle. Schedule the CLI invocation externally if needed (cron,
   systemd timer).
3. **Cross-workspace consolidation.** The curator operates on one root at
   a time (global OR workspace, not both at once). A skill present in both
   roots after the §6 merge belongs to the workspace; the global copy is
   reachable only by removing the workspace pin.
4. **Embedding-based similarity.** Consolidation in v1 uses a
   substring-overlap heuristic + the auxiliary model's judgment on a
   small candidate set; embedding-based clustering is a v2 concern
   (see §13.3) and lands alongside [`pattern-store.md §16`](pattern-store.md)
   if/when an embedding provider is wired.
5. **Multi-tenant / team curation.** v1 is single-operator. Team-shared
   curation lands when [`multi-user.md`](multi-user.md) §7 grows an
   audit-export surface.
6. **Replacement for `skill_load`-time validation.** The curator is
   off-loop maintenance; it does not gate per-call skill loading. A bad
   skill still loads via `skill_load`; the curator notices afterward.
7. **Auto-generation of new skills from scratch.** Creating *new* skills
   from session traces is the `skill.created(source="auto_generated")`
   Phase 2.5 path, separate from the curator. The curator only acts on
   skills that already exist.

---

## 3. Prerequisites

The curator is **gated on agent-authored skills landing first** (Phase 2.5).
That gate is now satisfied: the `skill_save` tool and the `skill.created`
event landed 2026-05-20 ([`skill-format.md §8.3`](skill-format.md) /
[`§9.2`](skill-format.md)). The curator distinguishes "skills it may touch"
from "skills it must not touch" by reading `skill.created` events from the
trace store:

| `skill.created.source` | Curator may touch? |
|------------------------|--------------------|
| `"manual"`             | No (read-only)     |
| `"auto_generated"`     | Yes                |
| `"imported"`           | No (read-only — third-party trust boundary; see §11) |
| `"curator_generated"`  | Yes (new value; introduced by this spec — see §8.1)  |

A skill with no `skill.created` event in the trace (e.g. a SKILL.md the user
hand-rolled before this spec landed) is treated as `"manual"` — read-only.

**Implication for sequencing:** building the curator before agent
authoring lands would give it nothing to act on. The implementation order
is:

1. ✅ **Landed 2026-05-20.** `skill_save` tool + `skill.created` event with
   `source="auto_generated"` ([`skill-format.md §8.3`](skill-format.md);
   out of scope for this spec). The `skill.created.source` enum already
   carries `"curator_generated"` (§8.5), so the curator needs no follow-up
   catalog change.
2. Curator (this spec). Lands as Phase 2.5b — now unblocked.

---

## 4. What the curator does

### 4.1 Six actions

| Action          | Effect                                                                                                          | Auxiliary-model spend? |
|-----------------|------------------------------------------------------------------------------------------------------------------|------------------------|
| `pin`           | Mark the skill pinned (sidecar state). Bypasses all subsequent auto-transitions. **Never invoked by the curator itself — set via `metis curate pin <name>` or the explicit CLI.** | No |
| `unpin`         | Clear the pinned flag. Same surface as `pin`.                                                                    | No |
| `archive`       | Move the skill directory to the archive root (see §5.2). The skill no longer loads. Reversible.                  | Yes — one auxiliary call to confirm the archive decision against a sample of recent activity. |
| `restore`       | Move an archived skill back to the active root. Reversible.                                                      | No — manual only.       |
| `consolidate`   | Pick a primary skill from a near-duplicate cluster, archive the others. Sets `consolidated_from` lineage on the survivor. | Yes — one auxiliary call to confirm the cluster and pick the survivor. |
| `edit`          | Patch the SKILL.md body in place (frontmatter untouched). Bumps `skill_version`. The previous body is preserved under the archive root for restoration. | Yes — one auxiliary call to propose the diff. |

The curator does **not** invoke any other action. In particular it does
not:

- Delete a skill from disk.
- Rename a skill (would change `skill_id`, which is load-bearing for
  trace dedup).
- Edit SKILL.md frontmatter (would risk diverging from agentskills.io;
  see §11 invariant 4).
- Reach across roots (global / workspace are independently curated).

### 4.2 What triggers each action

| Action          | Trigger                                                                                                                                                                                                                                       |
|-----------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `archive`       | The skill has been **inactive** (no `skill.loaded` event with this `skill_id`) for `curator.archive_after_days` (default 90) AND is not pinned AND is curator-touchable per §3. Stale (≥ `stale_after_days`, default 30) is a soft state — see §4.3. |
| `consolidate`   | At least two curator-touchable, non-pinned skills cluster by name + description similarity above the §6 threshold AND have been individually loaded ≥ 1 time in the past `archive_after_days`. The cluster size is capped at 5 (§13.7).         |
| `edit`          | A curator-touchable, non-pinned skill has been loaded ≥ 3 times in the past `interval_hours` AND its most recent `tool.failed` events within the same session correlate with `skill.loaded` of that skill at rate ≥ `edit_failure_rate` (default 0.5; §13.4). |
| `pin` / `unpin` / `restore` | Manual only (CLI or future UI). Never invoked by the auxiliary model.                                                                                                                                                              |

### 4.3 The "stale" soft state

`stale_after_days` (default 30) is **observable but not enforced**: a stale
skill is annotated in `metis curate list` output and in the curator
summary log, but the only action it triggers is bumping its priority in
the §6 candidate selection (stale skills are evaluated for archive sooner
than fresh ones in the same run). Archive happens only at
`archive_after_days`. This matches Hermes's "archive is recoverable; the
warning runway is longer than the deadline."

---

## 5. State and storage

### 5.1 Sidecar state file

Per source root, one JSON file:

```
~/.metis/curator/state.json          # global root state
<workspace>/.metis/curator/state.json # workspace root state
```

Both are optional; a missing file means "no curator state yet" (an empty
`{}`). Each file has shape:

```json
{
  "schema_version": "1",
  "last_curation_at_iso": "2026-05-12T18:30:00Z",
  "skills": {
    "pdf-processing": {
      "pinned": true,
      "pinned_at_iso": "2026-05-08T10:00:00Z",
      "pinned_by": "user",
      "origin": "manual",
      "consolidated_from": null,
      "archive_path": null
    },
    "code-review-python": {
      "pinned": false,
      "pinned_at_iso": null,
      "pinned_by": null,
      "origin": "auto_generated",
      "consolidated_from": null,
      "archive_path": null
    },
    "review-python-code": {
      "pinned": false,
      "pinned_at_iso": null,
      "pinned_by": null,
      "origin": "curator_generated",
      "consolidated_from": ["python-code-review-v1", "code-review-python-old"],
      "archive_path": null
    },
    "old-thing": {
      "pinned": false,
      "pinned_at_iso": null,
      "pinned_by": null,
      "origin": "auto_generated",
      "consolidated_from": null,
      "archive_path": ".metis/skills-archive/old-thing-20260514T183000Z/"
    }
  }
}
```

Field semantics:

- `schema_version` — `"1"`. Bumps on a breaking change to this shape.
- `last_curation_at_iso` — ISO-8601 UTC timestamp of the last curator
  invocation against this root. Drives the §4.2 interval check.
- `skills[name].pinned` — `bool`. The single source of truth for pin
  state. Pinned skills bypass every auto-transition.
- `skills[name].origin` — derived from the trace store at curator-run
  time (the trace store is authoritative; the cache here is for
  performance). `"manual"` / `"auto_generated"` / `"imported"` /
  `"curator_generated"` matches `skill.created.source` plus the new
  `"curator_generated"` value introduced by `consolidate`.
- `skills[name].consolidated_from` — list of skill_ids that were
  consolidated into this skill. Only set when `origin="curator_generated"`.
- `skills[name].archive_path` — non-null iff the skill is currently
  archived; gives the on-disk path of the archived copy relative to the
  root. Used by `restore` to move the directory back.

The file is **rewritten atomically** (write to `.tmp`, `os.replace`) on
every mutation. A corrupt file is logged at WARNING and replaced with an
empty state — the underlying `~/.metis/skills/` and
`<workspace>/.metis/skills/` directories are authoritative for skill
existence; the sidecar carries only the curator's annotations.

### 5.2 Archive root

```
~/.metis/skills-archive/                       # global archive root
<workspace>/.metis/skills-archive/              # workspace archive root
```

Archived skills live as directories under the archive root, named
`<skill_name>-<ISO8601-no-colons>/`:

```
~/.metis/skills-archive/old-thing-20260514T183000Z/
├── SKILL.md
├── scripts/...
└── ...
```

The full original directory is preserved verbatim — the archive is just
a `mv`. Restoration is `mv` back to the active root (and clearing
`archive_path` in the sidecar). The archive root is **not** loaded by
[`load_skills()`](../../packages/metis-core/src/metis_core/skills/store.py)
because it is at `skills-archive/`, not `skills/`. No loader changes
required.

The archive root is **append-only by the curator**. Manual cleanup
(`rm -rf ~/.metis/skills-archive/very-old-thing-*/`) is the operator's
prerogative; the curator never reaps it. This composes with `restore`:
if the operator manually removed an archived directory, `restore` fails
with a deterministic error.

### 5.3 No SKILL.md frontmatter changes

Pin state, origin, and lineage live exclusively in the sidecar JSON.
The agentskills.io spec is not extended (per the AGENTS.md memory:
"conform; don't invent fields"). A Metis skill remains drop-in
loadable in Claude Code / Cursor / Goose; those tools simply won't
know about pin/archive state, which is what we want — the curator is
Metis-specific.

---

## 6. Selection: which skills get curated this run

A curator run iterates over **one root** (caller specifies `global` or
`workspace`). The selection pipeline:

1. **Load the live `SkillStore` for the root** via
   `load_skills(global_dir=..., workspace_dir=None)` or vice versa.
   This honors the loader's existing skip-on-malformed behavior; the
   curator never re-validates a skill the loader rejected.
2. **Filter to curator-touchable.** Drop pinned skills (per §5.1). Drop
   skills whose `skill.created.source` is `"manual"` or `"imported"`
   (or has no `skill.created` event at all — defaults to manual). Read
   the trace store via [`canonical-message-format.md §9.1`](canonical-message-format.md).
3. **Compute per-skill activity.** For each remaining skill, query the
   trace store for `skill.loaded` events with this `skill_id` since
   `last_curation_at` (full-history scan on first run). Aggregate
   `loads_in_window`, `last_loaded_at`.
4. **Classify.**
   - **Archive candidate:** `last_loaded_at` is null or older than
     `archive_after_days`.
   - **Consolidate candidates:** computed once, after the per-skill
     pass. See §6.1.
   - **Edit candidate:** `loads_in_window >= 3` AND failure correlation
     ≥ `edit_failure_rate` per §4.2.
5. **Order.** Archive candidates first (cheapest action), then
   consolidate clusters, then edit candidates. Within each class, sort
   by `last_loaded_at` ascending (stalest first). This biases the
   per-run budget toward the highest-value cleanup.
6. **Apply per-run cap.** Walk the ordered list. For each candidate,
   estimate the auxiliary-model cost ([§7.2](#72-cost-estimation)). If the
   running per-run total + estimate would exceed `curator.per_run_max_usd`,
   stop. The remaining candidates are not processed this run; they will
   show up again on the next eligible run.

### 6.1 Consolidate clustering

Run only after the per-skill archive pass. Operates on the **non-archived,
non-pinned, curator-touchable** survivors.

1. **Candidate pairs** (`O(n²)` over the survivor set; n is bounded by
   `max_skills_per_root` — default 200, hard cap 1000):
   - Substring overlap: case-insensitive, drop stop-words
     (`a`, `the`, `with`, `for`, ...), then `len(intersection) /
     len(shorter)` against the (name + description) bag-of-words.
     Threshold: `name_overlap >= 0.6` OR `description_overlap >= 0.7`.
2. **Cluster.** Single-link clustering over the pair graph. Cluster size
   ≥ 2 to be a candidate; ≥ 5 is capped (the largest 5 by
   `loads_in_window`; the rest split into smaller candidate clusters or
   drop out).
3. **Auxiliary-model confirmation.** One call per cluster: pass the
   names + descriptions + body excerpts (≤ 500 chars each), ask the
   model to decide:
   - **Confirm + pick survivor.** Returns `{action: "consolidate",
     survivor: "name", archive: ["name1", "name2"], new_description: "..."}`.
   - **Reject.** Returns `{action: "skip", reason: "..."}`. The cluster
     is dropped from this run; will be re-evaluated next run.
4. **Execute.** On confirmation: the survivor's SKILL.md body is
   **rewritten by the auxiliary model** (one call, included in the
   per-cluster spend) to incorporate any unique content from the
   archived siblings. The survivor's sidecar state gets
   `origin="curator_generated"` (if it wasn't already) and
   `consolidated_from = [archived skill_ids]`. The archived siblings
   move to the archive root.

Two `skill.curated` events fire per consolidate: one with `action="consolidate"`
on the survivor (carrying `consolidated_from` and `skill_version_after`),
plus one with `action="archive"` per archived sibling (no auxiliary spend
on those; the cost was rolled into the survivor's event).

### 6.2 Edit selection

The edit candidate set is small in practice. For each candidate:

1. **Gather failure signals.** Walk the trace for the last
   `interval_hours` window: each `skill.loaded(skill_id)` event,
   find the same session's subsequent `tool.failed` events in the
   next `edit_failure_window_minutes` (default 30). Compute the
   per-session failure rate.
2. **Aggregate.** If the cross-session failure rate ≥
   `edit_failure_rate` (default 0.5) AND `loads_in_window >= 3`, the
   skill is an edit candidate.
3. **Auxiliary-model proposal.** One call: pass the body, the
   correlated `tool.failed` payloads (signatures only, no payloads —
   PRIVATE), and the failure rate. The model returns either a unified
   diff or `{action: "skip", reason: "..."}`. A diff must be syntactically
   applicable to the current body (whitespace-tolerant, single hunk
   ≤ 200 lines) — the curator does not retry on a malformed diff.
4. **Apply.** On a valid diff, rewrite the body atomically (write to
   `.tmp` next to the SKILL.md, `os.replace`). Bump `skill_version`
   (the SHA-256 changes automatically — no curator action needed).
   **Save the pre-edit body** under the archive root as
   `<workspace>/.metis/skills-archive/<skill_name>-edit-<iso>/SKILL.md`
   so the edit is recoverable via `metis curate restore <skill_name>
   --before <iso>`. The skill directory in the active root is **not**
   moved.

---

## 7. Budget and safety

### 7.1 Caps

Two caps shared with the evaluator via the same `BudgetTracker`
primitive:

| Cap                           | Default            | Configurable | Effect                                                          |
|-------------------------------|--------------------|--------------|------------------------------------------------------------------|
| `curator.per_run_max_usd`     | `Decimal("0.50")`  | yes          | When the running spend + next estimate would exceed this, the run stops and emits `curator.throttled(reason="per_run_cap")`. |
| `curator.per_day_max_usd`     | `Decimal("1.00")`  | yes          | When today's curator spend has hit this, the next eligible run is skipped entirely (no auxiliary calls). Emits `curator.throttled(reason="per_day_cap")` and updates `last_curation_at` so the interval counter advances. |
| `curator.interval_hours`      | `168` (7d)         | yes          | Minimum gap between auto-runs against the same root.             |
| `curator.stale_after_days`    | `30`               | yes          | Soft state per §4.3.                                             |
| `curator.archive_after_days`  | `90`               | yes          | Inactivity threshold for the archive action.                     |
| `curator.edit_failure_rate`   | `0.5`              | yes          | Per-skill failure rate that qualifies it as an edit candidate.   |
| `curator.max_skills_per_root` | `200`              | yes (≤ 1000) | Hard upper bound on candidate-set size; selection truncates.     |

These compose with — they do **not** replace — the evaluator's caps
([`evaluator.md §7`](evaluator.md)). A workspace running both the
evaluator and the curator has 4 independent ceilings; the
`BudgetTracker` instance is per-(domain, scope), so an exhausted
evaluator budget does not throttle the curator and vice versa.

### 7.2 Cost estimation

Before each auxiliary call, the curator estimates the cost using the
existing `PriceTable` against the candidate body + prompt. Estimates
use the *output cap* (max tokens reserved for the call), not the
typical output, to avoid an "estimate fits but actual blows" surprise.
If the estimate exceeds the remaining per-run headroom, the candidate
is skipped (not retried with a smaller body in v1; deferred to §13.6).

### 7.3 Kill switch

`curator.enabled = false` in the workspace config (or
`~/.metis/curator/state.json` schema-bumped to include a top-level
`disabled: true`) skips all auto-runs. The explicit CLI
(`metis curate <workspace>`) still works for one-off operator use.

---

## 8. Events

One new bus catalog event. All payloads are `msgspec.Struct(frozen=True)`
defined in [`packages/metis-core/src/metis_core/events/payloads.py`](../../packages/metis-core/src/metis_core/events/payloads.py).

### 8.1 `skill.curated`

> **Sensitivity:** `user_controlled` (floor; downgrades to `pseudonymous`
>   per [`event-bus-and-trace-catalog.md §4.4.1`](event-bus-and-trace-catalog.md)
>   when `signals.rationale_redacted` is set)
> **Phase:** 2.5b
> **Actor:** SYSTEM
> **Parent:** none (curator runs are not turn-scoped) OR
>   `skill.created` (when emitted alongside a `consolidate` survivor)

```python
{
    "curator_run_id": str,                          # monotonic ULID, one per `curator.maybe_run()` invocation
    "skill_id": str,                                # the affected skill
    "action": Literal["pin", "unpin", "archive", "restore", "consolidate", "edit"],
    "source": Literal["global", "workspace"],
    "skill_version_before": str | None,             # SHA-256(body)[:16]; null when skill didn't exist before (consolidate survivor created fresh)
    "skill_version_after": str | None,              # null for archive (skill no longer active)
    "consolidated_from": list[str],                 # populated only when action="consolidate"; empty otherwise
    "archive_path": str | None,                     # populated only when action="archive" or "edit" (pre-edit snapshot); null otherwise
    "curator_kind": Literal["llm", "manual"],       # llm = auxiliary-model call confirmed; manual = explicit CLI without model call
    "curator_model": str | None,                    # null when curator_kind="manual"
    "curator_cost_usd": str,                        # Decimal serialized as string; "0" when curator_kind="manual"
    "curator_pricing_version": str | None,
    "curator_latency_ms": int,
    "rationale": str,                               # auxiliary-model rationale; "" for manual actions
    "signals": dict,                                # see §8.2
}
```

### 8.2 The `signals` dict

Free-form key/value pairs the curator attaches for analytics. Standard
keys:

| Key                            | Type           | Set when                                                      |
|--------------------------------|----------------|---------------------------------------------------------------|
| `last_loaded_at_iso`           | `str \| null`  | always; null if the skill was never loaded                    |
| `loads_in_window`              | `int`          | always                                                        |
| `inactive_days`                | `int \| null`  | always; null if the skill was never loaded                    |
| `rationale_redacted`           | `bool`         | when an operator-configured redactor scrubbed `rationale`     |
| `cluster_size`                 | `int`          | only when `action="consolidate"`                              |
| `failure_rate`                 | `float`        | only when `action="edit"`                                     |
| `throttled_reason`             | `str`          | only on the run-summary event (see §8.3)                      |

The free-form shape mirrors [`evaluator.md §4.4`](evaluator.md)'s
`signals` design; standard keys are stable, others are advisory.

### 8.3 Run-boundary observability

There is **no run-summary `skill.curated` event** (no sentinel
`skill_id`, no synthetic action). A consumer wanting "all actions for
run X" filters `event_type="skill.curated" AND payload.curator_run_id=X`.
A run that takes no action (throttled, no candidates, interval not
elapsed, disabled) emits zero `skill.curated` events; the
"ran-but-did-nothing" case is observable via the run-boundary events
below.

### 8.4 `curator.run_started` and `curator.run_finished`

Two additional bus events for run-boundary observability:

```python
# curator.run_started
{
    "curator_run_id": str,
    "source": Literal["global", "workspace"],
    "trigger": Literal["session_ended", "explicit_cli", "scheduled"],
    "candidates_considered": int,                  # post-filter, pre-budget
    "config_snapshot": dict,                       # the seven §7.1 caps, frozen for this run
}

# curator.run_finished
{
    "curator_run_id": str,
    "source": Literal["global", "workspace"],
    "actions_taken": int,
    "spend_usd": str,                              # Decimal as string
    "throttled_reason": Literal["per_run_cap", "per_day_cap", "interval_not_elapsed", "disabled", "no_candidates", "completed"],
    "duration_ms": int,
}
```

Both are `pseudonymous` (no skill body content; no user message text).

`throttled_reason="completed"` is the happy path. The four other
non-trivial values (`per_run_cap`, `per_day_cap`, `interval_not_elapsed`,
`disabled`) cover the "no work happened, and here's why" cases without
needing a `skill.curated` event with a sentinel payload.

### 8.5 No new `skill.created` value, but…

The existing `skill.created.source` enum in [`event-bus-and-trace-catalog.md §6.6`](event-bus-and-trace-catalog.md)
must accept `"curator_generated"` as a value. This is a **catalog
change** described in the `CHANGES.md` entry that lands with this spec.
Additive — existing consumers that pattern-match the enum need to
handle the new value or be tolerant; the codebase's existing usage
treats `source` as an opaque string in everything except the curator's
own write path, so the catalog change is non-breaking.

A consolidate that produces a brand-new survivor (no pre-existing
SKILL.md to rewrite) emits one `skill.created(source="curator_generated")`
followed by one `skill.curated(action="consolidate")`. A consolidate
that rewrites an existing survivor's body emits only the
`skill.curated(action="consolidate")` event (the survivor's
`skill.created` was emitted long ago).

---

## 9. CLI

One new CLI surface, exposed by [`apps/cli/src/metis_cli/main.py`](../../apps/cli/src/metis_cli/main.py):

```
metis curate <workspace>                          # run once against this workspace's root
metis curate <workspace> --global                 # run once against the global root (~/.metis/skills/)
metis curate <workspace> --dry-run                # plan only; no actions executed
metis curate <workspace> --list                   # print the active state.json contents (no actions)
metis curate <workspace> pin <skill_name>         # manual pin (no auxiliary call)
metis curate <workspace> unpin <skill_name>
metis curate <workspace> restore <skill_name>     # manual restore from the archive root
metis curate <workspace> restore <skill_name> --before <iso>   # restore a pre-edit snapshot
```

All commands honor `--db-path` and `--config` like the existing
`metis evaluate` surface. `--dry-run` and `--list` emit no
`curator.run_started` events; the manual subcommands (`pin` / `unpin` /
`restore`) emit `skill.curated(curator_kind="manual")` events.

A `metis curate` call with no subcommand against an unconfigured
workspace exits with a deterministic message ("no curator state yet;
run `metis curate <workspace> --dry-run` first").

---

## 10. Integration points

### 10.1 Session-boundary trigger

The `SessionManager` ([`packages/metis-core/src/metis_core/sessions/manager.py`](../../packages/metis-core/src/metis_core/sessions/manager.py))
gains a `curator: SkillCurator | None = None` field, defaulting to
`None` for backwards compatibility. When set and `curator.enabled = true`,
`SessionManager.end_session()` calls `curator.maybe_run(...)` after
emitting `session.ended` and before returning to the caller. The
curator's `maybe_run` is **synchronous within the session's event
loop**, bounded by the per-run cost cap.

`maybe_run` short-circuits cheaply when the interval has not elapsed:
read `last_curation_at_iso` from the sidecar JSON, compare to `now`,
return without emitting events if the gap is short. The short-circuit
path is ~ms; the only observable cost on a non-eligible
`session.ended` is two `stat` calls and a small JSON read.

The `ChatRuntime` ([`apps/cli/src/metis_cli/runtime.py`](../../apps/cli/src/metis_cli/runtime.py))
wires a `SkillCurator` instance per workspace by default. Disabling is
via `curator.enabled = false` in the workspace config (the same config
that drives evaluator caps).

### 10.2 Analytics

Curator spend rolls up into the existing `/analytics/cost` endpoint
via a new optional parameter:

```
GET /analytics/cost?include_curator=true        # default: false (preserves existing rollups)
```

When `include_curator=true`, the response includes a `curator_cost_usd`
field per group and at the top level, projected from
`skill.curated.curator_cost_usd`. The `group_by=user|team|gateway_key`
projection is **not** populated for curator spend (the curator is
workspace-scoped, not identity-scoped) — those buckets land under
`null`. This mirrors the pre-multi-user analytics treatment of
direct-API calls.

A new endpoint, `GET /analytics/curator`, projects the most useful
curator-specific aggregates:

```json
{
  "since": "2026-05-07T00:00:00Z",
  "until": "2026-05-14T00:00:00Z",
  "runs": 1,
  "actions_total": 7,
  "actions_by_type": {"archive": 4, "consolidate": 2, "edit": 1},
  "spend_usd": "0.184",
  "throttled_runs": 0,
  "skills_pinned": 3,
  "skills_archived": 12,
  "skills_active": 27
}
```

Window parameters and projection conventions match
[`analytics-api.md §3`](analytics-api.md).

### 10.3 Pattern store

The pattern store ([`pattern-store.md`](pattern-store.md)) is **not**
modified by this spec. Curator actions do not write pattern fingerprints
or outcomes; the curator and the pattern store are orthogonal feedback
loops over the same trace. A future cross-link (use pattern-store
success scores to inform edit-candidate selection) is out of scope for
v1; see §13.8.

### 10.4 Evaluator

The curator **reads** `eval.completed` events when scoring an edit
candidate's failure correlation (§6.2). The signal is advisory — the
curator's primary failure signal is `tool.failed`, not `eval.completed`.
A workspace with the evaluator disabled still has a working curator;
the edit-candidate-selection pipeline degrades gracefully (falls back
to `tool.failed` alone).

The curator does **not** write `eval.*` events. The evaluator does not
read `skill.curated` events in v1. The two share only the
`BudgetTracker` primitive.

---

## 11. Invariants

1. **Never auto-delete.** The strongest destructive action is `archive`,
   which is a `mv`. Recoverable for the lifetime of the archive root.
2. **Never auto-touch user-authored skills.** A skill with
   `skill.created.source ∈ {"manual", "imported"}` (or no
   `skill.created` event at all) is read-only. The curator may emit
   advisory output about it (§13.5, deferred) but never mutates it.
3. **Pinned skills bypass every auto-transition.** Pin is the user's
   override; the curator must respect it.
4. **No SKILL.md frontmatter changes.** Conform to agentskills.io.
   Sidecar state is the only place curator-specific metadata lives.
   Editing the body via `edit` is allowed; editing the frontmatter is
   not.
5. **No reach across roots.** Global and workspace roots are
   independently curated. A skill present in both, after the workspace-
   overrides-global merge, exists at both sources; the curator sees the
   active source's copy.
6. **Bounded spend.** Every auxiliary call goes through `BudgetTracker.check_and_record`.
   A capped-out run stops mid-iteration and emits
   `curator.run_finished(throttled_reason)`. No silent overage.
7. **One audit event per action.** Every successful curator action
   emits exactly one `skill.curated`; every run emits exactly one
   `curator.run_started` and one `curator.run_finished`. Throttled-
   before-anything runs emit the two run-boundary events but zero
   `skill.curated` events.
8. **Atomic on-disk mutations.** Sidecar JSON, SKILL.md edits, and
   archive moves all use write-temp-then-rename. A crash mid-run leaves
   the on-disk state consistent (either fully old or fully new); the
   sidecar JSON is the last thing updated, so a crashed run's actions
   that already landed on disk will be re-evaluated on the next run.
9. **Restore is deterministic.** An archived skill can be restored via
   `metis curate <workspace> restore <skill_name>` iff its
   `archive_path` is still present under the archive root. The
   operator manually `rm -rf`-ing the archive forfeits restoration;
   the curator does not maintain a second backup.
10. **Curator-touchable origin is sticky.** A skill that started as
    `auto_generated` and gets `consolidate`d by the curator becomes
    `curator_generated` — still touchable. A user-edited skill that
    ever had a `manual` `skill.created` event stays `manual` forever
    (until a future operator action explicitly retags it; out of scope
    in v1).

---

## 12. Testing strategy

### 12.1 Required tests

State + storage:

1. Sidecar JSON round-trips through `read → mutate → atomic_write → read`.
2. A missing sidecar JSON loads as empty state.
3. A corrupt sidecar JSON is logged at WARNING and replaced with empty
   state; subsequent writes succeed.
4. The archive root is created on first archive; existing archive
   directories with the same name disambiguate by ISO timestamp suffix.

Selection (`§6`):

5. A skill with no `skill.created` event is filtered out as
   `origin="manual"` (never touched).
6. A pinned skill is filtered out regardless of activity.
7. `last_loaded_at` reads correctly from the trace store
   (`skill.loaded` event projection).
8. A skill loaded ≥ 1 time in the archive window is **not** an archive
   candidate.
9. A skill never loaded is an archive candidate.
10. Two skills with name overlap above the consolidate threshold
    cluster together; below the threshold do not.

Actions:

11. `archive` moves the skill directory to the archive root, updates
    the sidecar `archive_path`, and emits one `skill.curated(action="archive")`.
12. `restore` moves it back and clears `archive_path`; emits one
    `skill.curated(action="restore", curator_kind="manual")`.
13. `consolidate` rewrites the survivor's body via an auxiliary call,
    archives the siblings, and emits one
    `skill.curated(action="consolidate")` on the survivor plus one
    `skill.curated(action="archive")` per sibling. The survivor's
    sidecar gains `consolidated_from`.
14. `edit` writes a `.tmp` body and atomically renames; the previous
    body lands under the archive root with an `-edit-<iso>` suffix.
15. `pin` / `unpin` update the sidecar and emit
    `skill.curated(curator_kind="manual")`. No auxiliary call.

Budget:

16. A run that estimates the next call would exceed
    `curator.per_run_max_usd` stops and emits
    `curator.run_finished(throttled_reason="per_run_cap")`.
17. A run whose daily total is already at `curator.per_day_max_usd`
    short-circuits, updates `last_curation_at`, and emits
    `curator.run_finished(throttled_reason="per_day_cap")` with zero
    actions.
18. `curator.enabled = false` short-circuits every entry point
    (`maybe_run`, `metis curate`).

Events:

19. Every `skill.curated` event passes the `PAYLOAD_REGISTRY` validation
    (event-bus-and-trace-catalog.md §3.2).
20. `curator.run_started` and `curator.run_finished` always come in
    pairs with the same `curator_run_id`.

Integration:

21. `SessionManager.end_session()` calls `curator.maybe_run()` exactly
    once when `interval_hours` has elapsed since `last_curation_at`.
22. `metis curate <workspace> --dry-run` runs the selection pipeline
    but emits no `skill.curated` events.

### 12.2 Property tests (not required for v1)

- **Idempotence.** Running the curator twice back-to-back (second
  invocation with `interval_hours=0`) produces zero new actions in the
  second run — every candidate was already handled by the first.
- **Restoration round-trip.** `archive(X)` → `restore(X)` yields a
  state byte-identical to the pre-archive state (sidecar, on-disk
  directory). Hashed comparison.

---

## 13. Open questions

1. **Auxiliary model default.** Should this be the workspace default
   model, the haiku/cheapest model, or a configured `curator.model`?
   The Hermes default is "the cheapest model the operator has
   configured"; Metis's bias is similar. Default to
   `haiku-4-5` in v1; revisit if quality complaints land.
2. **Run scope.** Curate global + workspace in one `metis curate`
   invocation, or always one at a time? Hermes curates one at a time.
   v1 follows; the user can chain (`metis curate ws && metis curate ws
   --global`).
3. **Embedding clustering.** §6.1 substring overlap is cheap and known
   to miss semantically-near pairs ("regex helpers" vs "pattern
   matching"). A `pattern-store.md §16` embedding provider would
   trivially upgrade clustering quality. Decision deferred — only
   matters once an embedding provider is wired.
4. **`edit_failure_rate` calibration.** 0.5 is a guess. Tune against
   the benchmark suite once we have ≥ 10 auto-generated skills and
   real failure rates.
5. **Advisory output for user-authored skills.** §2.2 non-goal forbids
   mutation; an *advisory* `skill.curated(action="suggest")` that
   surfaces in `/analytics/curator` would let the user act on
   curator-found patches without surrendering authority. Deferred to
   v2.
6. **Body-too-large retry.** If a single candidate's estimated cost
   exceeds the per-run cap alone, v1 skips it forever (it never gets
   processed in any run). Should the curator instead split the body
   and pass it in chunks? Deferred.
7. **Cluster size cap of 5.** Hermes uses 4. Pick one and stick. v1: 5.
8. **Pattern-store cross-link.** Curator → pattern store: a skill that
   correlates with low `success_score` in the pattern store could be
   an `edit` candidate even without `tool.failed` signals. Cross-link
   deferred to v2.
9. **Audit export.** For team / enterprise (per [`multi-user.md §7.3`](multi-user.md)),
   curator actions need to land in the audit export with full
   `rationale` (PII-redaction respected). Deferred to multi-user §7.
10. **CLI `--auto-approve` for explicit `metis curate`.** v1 always
    runs without confirmation prompts (it is the operator running the
    CLI). If we add a "confirm each action" mode later, the toggle
    follows the existing `--auto-allow` convention for
    `metis dev` / `metis tui`.

---

## 14. Decision log

| Date       | Decision                                                                        | Rationale                                                                                                                                                  |
|------------|---------------------------------------------------------------------------------|------------------------------------------------------------------------------------------------------------------------------------------------------------|
| 2026-05-14 | Adopt Hermes's six actions (pin / unpin / archive / restore / consolidate / edit) | Survey of `agent/curator.py` showed this set covers the empirical failure modes; no obvious omission in 8 weeks of real-world activity.                    |
| 2026-05-14 | Archive = `mv` to sibling directory, never delete                               | Reversibility is load-bearing for owner trust; storage is cheap.                                                                                            |
| 2026-05-14 | Sidecar JSON for state, not SKILL.md frontmatter                                | Preserves agentskills.io conformance; the AGENTS.md memory pin on "conform; don't invent fields" is explicit.                                              |
| 2026-05-14 | Inactivity-triggered at `session.ended`, not a daemon                            | Metis is local-first and often not running. A background thread that wakes only when `metis chat` is open and idle would never wake. Session boundary is the natural firing edge. |
| 2026-05-14 | Share `BudgetTracker` with the evaluator; separate caps                          | Code reuse without coupling the budgets; an exhausted evaluator does not throttle the curator.                                                              |
| 2026-05-14 | One `skill.curated` event per action; no sentinel run-summary event              | Run-summary fits naturally in `curator.run_started` + `curator.run_finished` envelope; sentinel events with bogus skill_ids are ugly.                       |
| 2026-05-14 | Curator-touchable scope: `auto_generated` and `curator_generated` only           | A user who put a SKILL.md on disk owns it. The curator's authority extends only to what the agent (or the curator itself) created.                          |
| 2026-05-14 | Default `interval_hours=168`, `archive_after_days=90` (match Hermes)             | These are the only values with empirical evidence behind them. Tighten if observed action rates are too low.                                                |
| 2026-05-14 | Defer embedding clustering to v2                                                 | Substring overlap is the simplest thing that could work; embedding clustering composes cleanly with `pattern-store.md §16` if/when it lands.               |

---

## 15. References

- [hermes-agent `agent/curator.py`](https://github.com/NousResearch/hermes-agent/blob/main/agent/curator.py) — the design lifted here.
- [`skill-format.md`](skill-format.md) — the `Skill` / `SkillStore` substrate.
- [`event-bus-and-trace-catalog.md §6.6`](event-bus-and-trace-catalog.md) — the `skill.*` event family this spec extends.
- [`evaluator.md §7`](evaluator.md) — the `BudgetTracker` primitive shared with the curator.
- [`canonical-message-format.md §6.4`](canonical-message-format.md) — `cost_usd` / `pricing_version` conventions.
- [`analytics-api.md`](analytics-api.md) — the surface `/analytics/curator` joins.
- [`memory-store.md`](memory-store.md) — sister "soft / hard cap → event / reject" pattern.
- [`multi-user.md §7.3`](multi-user.md) — future audit-export integration (deferred).
- [`pattern-store.md §16`](pattern-store.md) — future embedding-clustering integration (deferred).
- [agentskills.io specification](https://agentskills.io/specification) — invariant 4 forbids extending this.
