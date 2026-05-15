# Skill Format Specification

**Status:** Draft v1 (retrospective; documents the existing implementation in [packages/metis-core/src/metis_core/skills/](../../packages/metis-core/src/metis_core/skills/))
**Last updated:** 2026-05-13

---

## 1. Purpose

Skills are bundled, portable units of **procedural knowledge** the agent loads on
demand. Each skill is a directory containing a `SKILL.md` file with YAML
frontmatter (`name`, `description`, …) and a Markdown body of operating
instructions. Optional sibling directories (`scripts/`, `references/`,
`assets/`) carry executable code, deeper references, and static resources the
skill body refers to.

Two on-disk roots are merged at startup:

- `~/.metis/skills/` — the global user library, shared across workspaces.
- `<workspace>/.metis/skills/` — workspace-pinned skills, override globals on
  name collision.

The runtime injects only a **discovery index** (name + description per skill)
into the system prompt. The agent activates a skill by calling `skill_load`,
which returns the full body and emits a `skill.loaded` event. This implements
the agentskills.io three-stage **progressive disclosure** model (discovery →
activation → execution).

This spec depends on:

- `canonical-message-format.md` for `ToolDefinition`, `ToolUseBlock`,
  `SideEffects`, tool input-schema subset.
- `event-bus-and-trace-catalog.md` §6.6 for the `skill.loaded` payload schema
  (which includes the `source` field added 2026-05-12).
- `tool-dispatcher.md` for how the two skill tools register and dispatch,
  and for the `ToolContext` extension that carries the per-session
  `SkillStore`.

This spec **conforms to** the [agentskills.io specification](https://agentskills.io/specification).
Where Metis adds runtime behavior the standard does not define (workspace merge
order, on-disk root paths, `skill.loaded` event), this spec documents that.
Metis does **not** add new SKILL.md frontmatter fields, per the AGENTS.md
guideline "conform to it; don't invent fields."

---

## 2. Goals and non-goals

### 2.1 Goals

1. **Conform to agentskills.io.** Frontmatter fields, validation rules, and
   directory layout match the open standard so a Metis skill drops into Claude
   Code / Cursor / Goose / etc. unmodified.
2. **Progressive disclosure is the cost lever.** Only ~100 tokens per skill
   pays rent in the system prompt; full bodies (recommended ≤ 5000 tokens)
   load only when the agent decides the skill is relevant. Many idle skills
   is cheap; activating one is metered.
3. **Workspace beats global on collision.** A user can pin a workspace-specific
   version of `code-review` without forking the global one.
4. **Provenance is recorded.** Every `skill.loaded` event records which
   directory (`global` or `workspace`) served the skill after the merge.
5. **One bad skill should not break the runtime.** Validation errors on a
   single SKILL.md are logged and the skill is skipped; the rest load.
6. **Cheap discovery.** Substring match on name + description is sufficient
   for v1; a semantic index would land alongside the pattern store in Phase 2.5.

### 2.2 Non-goals

1. **Auto-generated skills.** `skill.created` (auto-generation,
   import) is a Phase 2.5 concern (`event-bus-and-trace-catalog.md §6.6`).
   v1 ships only loading.
2. **Reload-on-change.** A `SkillStore` is built once at session creation; mid-
   session edits to SKILL.md are not reflected until the next session.
3. **`allowed-tools` enforcement.** The field is parsed and exposed on the
   `Skill` dataclass per the agentskills.io spec, but the dispatcher does not
   gate execution against it in v1. Marked experimental upstream.
4. **Security scanning.** Phase 2.5 introduces the import/auto-generation path
   where untrusted SKILL.md bodies need a scanner; v1 only loads files the user
   put on disk themselves.
5. **Marketplace, signing, transport.** Out of scope for v1.
6. **Discovery via semantic embedding.** v1 is case-insensitive substring;
   real ranking is a Phase 2.5 concern.

---

## 3. On-disk layout

### 3.1 Roots

```
~/.metis/skills/                  # global library
└── pdf-processing/
    ├── SKILL.md
    └── ...

<workspace>/.metis/skills/         # workspace-pinned
└── code-review/
    └── SKILL.md
```

The global root is resolved by `metis_cli.runtime.default_skills_dir()` as
`Path.home() / ".metis" / "skills"`. The workspace root is
`<workspace>/.metis/skills/`. Either root is allowed to be missing; the
loader treats a missing root as an empty set of skills (no error).

### 3.2 Per-skill structure

A skill is a **directory** whose name equals the skill's `name` frontmatter
field. The directory must contain a file literally named `SKILL.md`
(uppercase). The directory may contain any other files; standard sibling
directories per the agentskills.io spec are `scripts/`, `references/`, and
`assets/`. v1 does not interpret these — the body can reference them by
relative path, and the agent uses general-purpose file tools to read scripts
or reference files when the body instructs it to.

```
skill-name/
├── SKILL.md          # required: frontmatter + Markdown body
├── scripts/          # optional: executable code (uninterpreted by Metis)
├── references/       # optional: deeper docs the body refers to
├── assets/           # optional: templates, lookup tables, etc.
└── ...               # any additional files
```

Entries in either root that are not directories, or directories missing
`SKILL.md`, are silently skipped (not an error). A directory whose `SKILL.md`
fails validation is logged at `WARNING` and skipped — the rest of the library
still loads.

### 3.3 SKILL.md format

```
---
<YAML frontmatter>
---
<Markdown body>
```

The file must begin with `---\n`. The next `\n---` line closes the
frontmatter. Content after that, with leading newlines stripped, is the
Markdown body. Missing opening or closing delimiters is a validation error.

Frontmatter is parsed with `yaml.safe_load` and must produce a YAML mapping at
the top level. Non-mapping documents (a list, a scalar) are rejected.

---

## 4. Frontmatter schema

The six-field schema below conforms to
[agentskills.io/specification](https://agentskills.io/specification). Fields
not listed are accepted by the YAML parser (`yaml.safe_load` is permissive)
but are ignored by the loader.

| Field           | Required | Type           | Constraints                                                                                                                         |
| --------------- | -------- | -------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| `name`          | Yes      | string         | 1-64 chars; regex `^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$`; no consecutive `--`; **must equal parent directory name**.                   |
| `description`   | Yes      | string         | 1-1024 chars; non-empty after `strip()`.                                                                                            |
| `license`       | No       | string         | License name or pointer to a bundled license file. No length cap.                                                                   |
| `compatibility` | No       | string         | ≤ 500 chars. Free-form note on environment requirements (target product, system packages, network needs).                           |
| `metadata`      | No       | mapping        | String-keyed mapping. Values must be strings; scalars (`int`, `float`, `bool`) are silently coerced to `str` (see §10 gap note).    |
| `allowed-tools` | No       | string         | Space-separated tool names. Parsed into a `tuple[str, ...]`. **Not enforced** by the dispatcher in v1; marked experimental upstream. |

A SKILL.md that violates any constraint above is rejected by
`SkillValidationError`. The loader catches the exception, logs each error
message, and skips the offending skill — it does not abort the rest of the
load.

### 4.1 `name`

Validated by `_NAME_RE = ^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$` plus an explicit
"no consecutive hyphens" check (the regex permits inner consecutive hyphens, so
the explicit check is load-bearing) plus an explicit length check (the regex
has no length cap) plus an equality check against `skill_dir.name`.

Rationale for `name == parent dir name`: when a user types `skill_load("pdf-processing")`
the agent doesn't need to consult the frontmatter to find the directory — the
identifier is the path component.

### 4.2 `description`

Used for two things: discovery-index injection into the system prompt, and
substring matching in `skill_search`. Should describe *what the skill does and
when to apply it*; agents pick relevance from this string alone in stage 1.

### 4.3 `metadata`

Per agentskills.io, conventionally holds `author`, `version`, and similar
out-of-spec keys. Metis does not interpret any specific metadata key in v1.

### 4.4 `allowed-tools`

Parsed-but-not-enforced. The agentskills.io spec marks this experimental;
Metis stores the parsed tuple on the `Skill` dataclass for future use without
gating tool dispatch on it. A Phase 2.5+ change can enforce it at the
dispatcher level; the spec change would land alongside that work.

---

## 5. The `Skill` and `SkillStore` types

```python
SkillSource = Literal["global", "workspace"]

@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str                       # Markdown after frontmatter; leading newlines stripped
    source: SkillSource             # which root served this skill after merge
    skill_dir: Path                 # absolute path to the skill directory
    license: str | None = None
    compatibility: str | None = None
    metadata: dict[str, str] | None = None
    allowed_tools: tuple[str, ...] = ()

    @property
    def version(self) -> str:
        """SHA-256 of body (utf-8); first 16 hex chars. Used as
        `skill_version` in the skill.loaded event payload."""

    @property
    def estimated_body_tokens(self) -> int:
        """`max(1, len(body) // 4)` — rough char-to-token heuristic."""

    @property
    def over_body_token_warn(self) -> bool:
        """True if `estimated_body_tokens > 5000`. Emits a WARNING at load."""


class SkillStore:
    def list_skills(self) -> list[Skill]: ...        # sorted by name
    def get(self, name: str) -> Skill | None: ...
    def search(self, query: str, *, limit: int = 10) -> list[Skill]: ...
    def discovery_index(self) -> list[tuple[str, str]]: ...
    def __len__(self) -> int: ...
    def __contains__(self, name: object) -> bool: ...

    @classmethod
    def empty(cls) -> SkillStore: ...


def load_skills(
    *,
    global_dir: Path | str | None,
    workspace_dir: Path | str | None,
) -> SkillStore: ...
```

`Skill.version` is intentionally derived from the body alone (frontmatter
changes don't bump version). This keeps trace records stable across cosmetic
edits to the description field; if the operating instructions change, the
hash changes.

---

## 6. Merge rules

`load_skills(global_dir=..., workspace_dir=...)` enumerates each root
independently with `_load_dir(root, source=...)`. The merge is then:

```python
merged = dict(global_skills)          # start with globals
for name, skill in workspace_skills.items():
    if name in merged:
        logger.info("skill %r in workspace overrides global definition", name)
    merged[name] = skill              # workspace wins
```

Rules:

1. **Workspace overrides global on name collision.** The workspace value
   replaces the global value entirely; there is no field-level merge.
2. **The override is logged at `INFO`** with the skill name so users can see
   which workspace pin shadowed which global.
3. **Both roots are optional.** Passing `None` for either is allowed and
   means "no skills from that source." A nonexistent directory is treated
   the same as `None`.
4. **Discovery ordering** within each root is `sorted(root.iterdir())`, so
   load order is deterministic across runs. The final `SkillStore.list_skills()`
   is also sorted by name.
5. **`Skill.source` is set at load time**, not at merge time — a skill from
   `~/.metis/skills/pdf/SKILL.md` has `source="global"` even if it survives
   the merge unopposed.

---

## 7. Progressive disclosure (the three stages)

This is the cost lever: only stage-1 metadata is "always paid"; stage-2 and
stage-3 are metered per activation.

### 7.1 Stage 1 — discovery (always loaded)

At session start, the `SessionManager` calls
`SkillStore.discovery_index()` and appends a single block to the **stable**
half of the system prompt (the part before the prompt-cache breakpoint, see
`context-assembler.md §2-§5`):

```
## Available skills
Use `skill_search(query)` to filter and `skill_load(name)` to read a body.

- pdf-processing: Extract PDF text, fill forms, merge files. Use when handling PDFs.
- code-review: Run a structured review on the current branch. Use when the user asks for review.
- ...
```

One line per skill, format `- {name}: {description}`. Bodies are NOT injected
in this stage. The discovery index is omitted entirely if the store is empty.

**`[preloaded]` annotation (context-assembler.md v3 §5.2.2).** When the
v2 §5.1 padding rule inlines a skill body into the stable prefix as
pre-activation, that skill's discovery line gains a `[preloaded]`
annotation:

```
- pdf-processing [preloaded]: Extract PDF text, fill forms, merge files. ...
```

The annotation tells the agent "the body is already in this system
prompt; calling `skill_load(name)` returns a pointer rather than the
body." Pre-activation is observable on the bus via
`skill.loaded(load_reason="always")` emitted once per inlined body at
session init.

**Cache impact:** the index lives in the stable system prompt segment, so it
becomes part of the cached prefix on Anthropic and OpenAI. Adding or removing
a skill invalidates the cache; editing a skill's description invalidates the
cache; editing a skill's body does **not**.

### 7.2 Stage 2 — activation (on demand)

The agent calls `skill_load(name)`. The tool returns the full SKILL.md body
prefixed with a `# Skill: {name} (source: {source})` header, and emits
`skill.loaded` with `load_reason="on_demand"`.

The body lives in the message history (as a `tool_result` block), not in the
system prompt. It is therefore subject to history compression / truncation
policies the assembler eventually implements — but in v1, history is not
truncated, so once loaded, it stays loaded for the rest of the session.

The agent decides when to activate. Two paths:

1. **Direct.** The agent already knows the skill's name (from the discovery
   index) and calls `skill_load("pdf-processing")` directly.
2. **Search-then-load.** The agent calls `skill_search("PDF")` to filter
   the index, then calls `skill_load` on the chosen name.

Both paths are first-class. The discovery index nudges the agent toward
direct loading by listing every available skill with its description.

### 7.3 Stage 3 — execution

The skill body may reference scripts under `scripts/`, deeper docs under
`references/`, or assets under `assets/`. v1 does not interpret these — the
body tells the agent which files to read, and the agent uses general-purpose
file tools (`read_file`) or shell tools (`run_command`) to follow through.
Per the agentskills.io spec, the body should keep references one level deep
and supply self-contained scripts.

---

## 8. Tools

Two tools, both `SideEffects.READ`, both `requires_workspace=False`.
Registered via `metis_core.skills.tools.register_skill_tools(dispatcher)`.

The dispatcher binds a per-session `SkillStore` onto each `ToolContext` via
the `skills` field (`tool-dispatcher.md`; `ToolContext.skills` is `Any` to
avoid the import cycle from tools back to skills). The `SessionManager`
constructs the store from a `skill_store_factory` callback at session
creation time, mirroring the `memory_factory` injection pattern.

### 8.1 `skill_search`

Filter the discovery index by substring on `name` + `description`.

**Input schema:**

```json
{
  "type": "object",
  "properties": {
    "query": {"type": "string"},
    "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10}
  },
  "required": ["query"],
  "additionalProperties": false
}
```

**Semantics:**

- Case-insensitive substring match. `query` is lowercased + `strip()`ed.
- Scoring: `+2` if the query is in the name, `+1` if in the description.
  Skills with score 0 are excluded.
- Results are ordered by score desc, then by name asc.
- Empty / whitespace-only `query` returns the first `limit` skills sorted
  by name (acts like a paginated list).
- A skill store that wasn't configured for this session raises
  `ToolExecutionError("skills are not configured for this session")`.

**Output:** a single `TextBlock` formatted as:

```
Skills matching 'pdf' (2):
- pdf-processing [global] — Extract PDF text, fill forms, merge files. ...
- pdf-redact [workspace] — Redact regions of PDFs by bbox.
```

plus a metadata dict carrying `query`, `result_count`, and `result_names`.

**Side effects:** none. No event emitted.

### 8.2 `skill_load`

Return the full SKILL.md body for a named skill.

**Input schema:**

```json
{
  "type": "object",
  "properties": {
    "name": {"type": "string", "minLength": 1, "maxLength": 64}
  },
  "required": ["name"],
  "additionalProperties": false
}
```

**Semantics:**

- Lookup is exact-match against `Skill.name`. Unknown names raise
  `ToolExecutionError(f"no skill registered with name {name!r}")`.
- A skill store that wasn't configured for this session raises
  `ToolExecutionError("skills are not configured for this session")`.
- Output is the body prefixed with `# Skill: {name} (source: {source})\n\n`.
- **Pre-activated path** (context-assembler.md v3 §5.2.2): if the
  skill's body was already inlined into the stable system prefix as
  v2 §5.1 padding, the tool returns a short pointer instead of the
  body. No `skill.loaded` event fires (the pre-activation event at
  session init already covered it). Output metadata carries
  `already_preloaded: True`.
- **Already-activated path** (context-assembler.md v3 §5.2.7 q4): if
  the agent already called `skill_load(name)` earlier in the session,
  the tool returns a pointer rather than re-injecting the body. No new
  `skill.loaded` event fires, the activation budget is not incremented.
  Output metadata carries `already_loaded: True`.
- **Budget exhaustion** (context-assembler.md v3 §5.2.4): if returning
  the body would push the session past
  `MAX_EXPLICIT_ACTIVATIONS_PER_SESSION` or
  `HARD_CAP_CUMULATIVE_ACTIVATION_TOKENS`, the tool raises
  `ToolExecutionError` with a descriptive message. Surfaces as
  `tool.failed` per §5.2.6.

**Side effects:** emits exactly one `skill.loaded` event per
*body-returning* call (the pre-activated and re-load paths emit no
event; budget-exhaustion paths fail before emitting). No file is
touched, no memory is mutated, no bus events beyond `skill.loaded`.

**Output metadata:** `skill_id`, `skill_version`, `source`,
`load_size_tokens`. Plus `already_preloaded: True` on the pre-activated
pointer path, or `already_loaded: True` on the re-load pointer path.

---

## 9. Events

### 9.1 `skill.loaded`

Per `event-bus-and-trace-catalog.md §6.6`:

```python
{
    "skill_id": str,                            # = Skill.name
    "skill_version": str,                       # SHA-256(body)[:16]
    "load_reason": Literal["always", "on_demand", "auto_suggested"],
    "load_size_tokens": int,                    # estimated_body_tokens
    "source": Literal["global", "workspace"],   # additive 2026-05-12
    "triggered_by_tool_use_id": str | None,     # the skill_load call's tool_use_id
}
```

Sensitivity: `pseudonymous` (skill names + sizes; bodies are not in the
payload).

The implementation in `skills/tools.py::SkillLoadTool` emits with
`load_reason="on_demand"` and `triggered_by_tool_use_id=context.tool_use_id`.

`load_reason="always"` is the pre-activation path
(context-assembler.md v3 §5.2.2): `SessionManager.create_session`
emits one such event per body inlined into the stable prefix as v2
§5.1 padding, with `triggered_by_tool_use_id=None` and no `turn_id`.
Pre-activation fires before any `turn.started` in the session.

`load_reason="auto_suggested"` remains reserved for a later
description-match-driven activation mechanism
(context-assembler.md v3 §5.2.7 q3). Not wired in v3.

### 9.2 No `skill.created` in v1

`skill.created` is a Phase 2.5 event for auto-generation / import flows
(`event-bus-and-trace-catalog.md §6.6`). v1 only loads skills the user
authored manually, so no creation event is emitted.

---

## 10. Invariants

1. **Frontmatter `name` equals parent directory name.** Enforced at load time.
2. **A skill is identified by its frontmatter `name`**, not its directory
   path. Two skills with the same `name` across roots collide and merge per
   §6; two skills with the same `name` within a single root would also
   collide, but since the directory name must equal the frontmatter name,
   this is structurally impossible on a normal filesystem.
3. **One bad skill should not break loading.** `SkillValidationError`
   inside `_load_dir` is caught, logged at `WARNING`, and the skill is
   skipped.
4. **`SkillStore` is per-session, snapshot at creation.** Mutating the
   on-disk files mid-session has no effect on the live store. Reload-on-
   change is deferred.
5. **`Skill` instances are frozen.** No in-place mutation.
6. **`skill.loaded` carries `source`.** All current emitters set the field;
   it is not optional in the payload struct.
7. **The discovery index is injected into the stable system prompt segment**,
   ahead of the prompt-cache breakpoint (`context-assembler.md §2-§5`).
   Editing a skill body does NOT invalidate cache; editing
   name/description / adding/removing a skill DOES.
8. **`SideEffects.READ` for both tools.** No write classification; no
   confirmation prompt under any handler.

---

## 11. Implementation gaps (surface for triage; NOT bug-fixed in this spec)

These are observations from reading the existing implementation. Each is a
candidate for a follow-up issue, not part of v1 of this spec.

1. **`name` validation error messages are slightly inaccurate.** The first
   `elif` says the regex enforces "no leading/trailing/consecutive hyphens",
   but the regex `^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$` does not forbid inner
   consecutive hyphens — the explicit `elif "--" in name` check is what
   catches them, with a different message. Cosmetic only; validation is
   correct.

2. **`metadata` silently coerces scalars to strings.** The agentskills.io
   spec says metadata is a string→string map; the loader accepts `int`,
   `float`, `bool` values and calls `str()` on them. This is a deliberate
   ergonomic divergence ("version: 1.0" without YAML quoting works), but
   it's worth flagging — strictly conforming clients would reject the same
   input.

3. **No upper bound on the discovery index.** 100 skills × ~1024-char
   descriptions could push the index past 100 KB, all in the stable system
   prompt. There is no max-skill-count or max-index-size cap. In practice
   small libraries (≤30 skills with terse descriptions) sit well under a KB.

4. **No reload-on-change.** A long-running `metis serve` will not pick up
   edits to either skills root until restart. The docstring on `SkillStore`
   acknowledges this; the gap is the lack of an inotify/poll mechanism.

5. **Hidden directories are not excluded.** `_load_dir` accepts any directory
   under the root, including dot-prefixed ones. A `.history/` directory under
   `~/.metis/skills/` would be enumerated; if it accidentally contained a
   well-formed `SKILL.md` with `name: history`, it would load. Low risk
   given the directory-name=skill-name invariant, but the loader doesn't
   actively defend against it.

6. **Symlinks are followed.** `Path.is_dir()` follows symlinks; both the
   skill directory itself and `SKILL.md` may be symlinks. Unlike the
   `WorkspaceFileAPI` (which rejects out-of-root symlinks), the skill loader
   has no equivalent guard. Acceptable for a user-curated library, but
   surfaces a difference from the workspace tool security model.

7. **`allowed-tools` is parsed but not enforced.** Documented in §4 as
   intentional (matches the agentskills.io "experimental" stance), but
   worth surfacing here so future readers don't assume it gates anything.

---

## 12. Testing strategy

### 12.1 Required tests (all passing as of 2026-05-13)

Frontmatter validation:

1. Valid single skill loads and roundtrips all six fields.
2. Multiple skills load and are sorted by name.
3. `name` mismatched with parent dir is rejected.
4. `name` violating the regex (uppercase, leading/trailing hyphen) is rejected.
5. `name` with consecutive hyphens is rejected.
6. `name` longer than 64 chars is rejected.
7. `description` missing / empty / over 1024 chars is rejected.
8. `compatibility` over 500 chars is rejected.
9. `metadata` non-mapping or non-string values (other than coerced scalars)
   is rejected.
10. Missing opening / closing frontmatter delimiter is rejected.
11. Malformed YAML is rejected.

Loader behavior:

12. Non-directory entries under a root are skipped silently.
13. Directories missing `SKILL.md` are skipped silently.
14. Lowercase `skill.md` is **not** accepted (uppercase `SKILL.md` required).
15. One invalid skill is logged + skipped; the rest load.
16. Missing root returns an empty store.

Merge:

17. Workspace overrides global on name collision; the override is logged.
18. Disjoint global + workspace skills both load.

Discovery & search:

19. `search` returns substring matches.
20. `search` ranks name-match above description-match.
21. `search` with empty query returns the full sorted list (up to `limit`).
22. `search` `limit` is respected.
23. `discovery_index()` returns `(name, description)` pairs.

`Skill` properties:

24. `version` is stable across runs for unchanged body.
25. `over_body_token_warn` triggers a `WARNING` log on load when
    `estimated_body_tokens > 5000`.

Tools:

26. Both tools register on the dispatcher.
27. `skill_search` returns matches and metadata.
28. `skill_search` with no matches returns the no-match text.
29. `skill_search` raises `ToolExecutionError` when skills aren't configured.
30. `skill_load` returns the body wrapped in the `# Skill: ...` header.
31. `skill_load` raises `ToolExecutionError` for unknown names.
32. `skill_load` emits exactly one `skill.loaded` event with
    `load_reason="on_demand"` and `triggered_by_tool_use_id` set.
33. `skill_load` for a workspace skill records `source="workspace"`; same
    for global / `"global"`.
34. `skill_load` raises `ToolExecutionError` when skills aren't configured.

### 12.2 Property tests worth investing in (not yet written)

- **Idempotence of load.** Calling `load_skills` twice over the same roots
  produces equal stores (same names, same versions, same fields).
- **Round-trip with `Skill.version`.** Mutating the body changes the
  version; mutating only the description does not.

---

## 13. Open questions

1. **Index injection vs `skill_search`-only.** v1 ships both: the index is
   in the system prompt AND the tool is registered. If most agents go
   straight to `skill_load` from the index, `skill_search` is unused weight.
   If the agent never reads the index (large libraries), the tool is
   load-bearing. Open until usage data lands; consider toggling injection
   when libraries grow.
2. **`load_reason="always"`.** Should certain skills always be loaded into
   context (Letta-style "core memory" for procedural knowledge)? Not in v1;
   would need a per-skill `always: true` field, which would diverge from
   agentskills.io. Defer.
3. **Per-skill model hints.** A skill with very long instructions might
   benefit from a `preferred_model: sonnet-4-6` hint. Crosses into routing-
   engine territory; defer to Phase 2.5 alongside `auto_suggested` activation.
4. **Versioning / signing.** Once skills can be imported from third parties
   (Phase 2.5+), `skill_version` becomes a security primitive. v1 uses
   the SHA-256-prefix for trace dedup only.
5. **Reload-on-change.** Worth investing in when iteration on skill bodies
   becomes a friction point (i.e., once anyone is authoring multiple skills
   in a single session). Until then, restart `metis serve` to pick up edits.

---

## 14. Decision log

| Date       | Decision                                                                          | Rationale                                                                                                                  |
| ---------- | --------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| 2026-05-08 | Conform to agentskills.io 6-field frontmatter                                     | Open standard, ~35+ implementers; cross-tool portability is a feature; "conform; don't invent fields" per AGENTS.md memory. |
| 2026-05-08 | Two roots, workspace overrides global                                             | Lets a user pin a workspace-specific variant without forking the shared global one.                                        |
| 2026-05-08 | Discovery index in **stable** system prompt segment                               | Keeps cache prefix stable across volatile MEMORY.md edits (`context-assembler.md §2-§5`).                                  |
| 2026-05-08 | `skill_search` tool ships alongside index injection                               | Index is the cheap path; `skill_search` is for libraries large enough that scanning a long index burns tokens.            |
| 2026-05-08 | `Skill.version = SHA256(body)[:16]`, not a frontmatter `version` field            | Body is the operative content; trace records should track operative-content changes only.                                  |
| 2026-05-12 | Add `source: Literal["global", "workspace"]` to `skill.loaded` payload            | Provenance after merge; resolves "which definition served this skill" for traces.                                          |
| 2026-05-13 | Spec drafted retrospectively from existing implementation                         | Skills are built and tested but were unspec'd; this doc closes the gap. Follows the `memory-store.md` pattern.             |

---

## 15. References

- [agentskills.io specification](https://agentskills.io/specification) — the
  open standard this spec conforms to. Six-field frontmatter, SKILL.md
  filename, progressive disclosure stages, recommended body cap.
- `canonical-message-format.md` — `ToolDefinition`, `ToolUseBlock`,
  `SideEffects.READ`, tool input-schema subset.
- `event-bus-and-trace-catalog.md §6.6` — `skill.loaded` payload schema and
  sensitivity. The `source` field landed 2026-05-12 (CHANGES.md entry).
- `tool-dispatcher.md` — how tools register and dispatch; the `ToolContext`
  carries the per-session `SkillStore` via the `skills` field.
- `context-assembler.md §2-§5` — placement of the discovery index in the
  stable system prompt segment, and the cache implications.
- `memory-store.md` — sister retro-spec for the bounded markdown layer; same
  pattern: code shipped, spec drafted afterward.
