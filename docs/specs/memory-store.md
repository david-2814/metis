# Memory Store Specification

**Status:** Draft v1 (retrospective; documents the existing implementation in [src/metis/memory/](../../src/metis/memory/))
**Last updated:** 2026-05-12

---

## 1. Purpose

The memory store is the per-workspace, agent-curated, byte-budgeted markdown layer that gives the agent cross-session continuity. Two files per workspace:

- **`MEMORY.md`** — workspace facts the agent should remember (~2 KB soft cap, 4 KB hard cap).
- **`USER.md`** — facts about the user (~1.5 KB soft cap, 3 KB hard cap).

Both live at `<workspace>/.metis/`. They are written by the agent through three tools (`memory_add`, `memory_replace`, `memory_consolidate`) and read by the session manager at every LLM call (composed into the system prompt fresh per turn).

This spec depends on:

- `canonical-message-format.md` for `ToolDefinition`, `ToolUseBlock`, `SideEffects`.
- `event-bus-and-trace-catalog.md` for `memory.updated` and `memory.eviction` payload schemas.
- `tool-dispatcher.md` for how the memory tools register and dispatch.

---

## 2. Goals and non-goals

### 2.1 Goals

1. **Bounded by design.** Both files have hard byte caps. Unbounded markdown ("everything CLAUDE.md") is the dominant competitor pattern; bounded memory is the wedge.
2. **Agent-curated.** Adds, replaces, consolidates are tool calls the LLM makes — not human-edited config (though humans CAN edit the files directly; they're just markdown).
3. **File-on-disk and portable.** No database, no JSON envelope. Plain markdown, `git add`-able, human-readable.
4. **Cross-session continuity.** Memory persists across `metis chat` invocations. The agent doesn't relearn the workspace every session.
5. **Eviction is observable.** Soft-cap overflow emits `memory.eviction` as a signal the agent must consolidate; hard-cap overflow rejects the write so the agent can't keep growing.
6. **No silent garbage collection.** The agent decides what to evict via `memory_consolidate`. Auto-pruning would lose curation.

### 2.2 Non-goals

1. **Vector storage or semantic search.** Both files are short enough that the LLM reads them in full every turn. No retrieval; no RAG.
2. **Versioning beyond `git`.** If the user wants history, they `git init` the workspace.
3. **Per-user memory in shared workspaces.** v1 has one `USER.md` per workspace. Multi-user is a Phase 3+ concern (sync layer).
4. **Auto-extracted facts.** The agent decides what's worth remembering via explicit tool calls. Background extraction is out of scope.
5. **Cross-workspace memory.** `USER.md` is workspace-scoped, not user-scoped, in v1. A future "global user memory" would be a separate file or a sync feature.

---

## 3. File layout

```
<workspace>/.metis/
├── MEMORY.md      # workspace facts
└── USER.md        # user facts
```

The `.metis/` directory is created lazily on first write. Empty files = missing files = `""`; the read API smooths this over.

The directory may also contain other Metis state (the trace SQLite, session SQLite) when a workspace is treated as the trace target. v1 does not enforce strict separation.

---

## 4. Schema (Python interface)

```python
class MemoryFile(StrEnum):
    """Closed enum matching event-bus memory.updated.file."""
    MEMORY = "MEMORY.md"
    USER   = "USER.md"


@dataclass(frozen=True)
class WriteResult:
    """Returned by writes; carries hashes for memory.updated events."""
    file: MemoryFile
    before_hash: str             # SHA-256 of pre-write content (utf-8 bytes)
    after_hash: str              # SHA-256 of post-write content
    before_size_bytes: int
    after_size_bytes: int
    over_soft_cap: bool          # write succeeded but exceeded soft cap
    over_hard_cap: bool          # always False on success; raises otherwise


class MemoryStore:
    def __init__(self, workspace_path: str | Path) -> None: ...

    @property
    def workspace_path(self) -> str: ...

    # Reads
    def read(self, file: MemoryFile | str) -> str: ...
    def exists(self, file: MemoryFile | str) -> bool: ...
    def size_bytes(self, file: MemoryFile | str) -> int: ...

    # Writes — all return WriteResult; all raise MemoryHardCapExceeded on overflow.
    def add_entry(self, file, entry: str) -> WriteResult: ...
    def replace(self, file, old_text: str, new_text: str) -> WriteResult: ...
    def consolidate(self, file, new_content: str) -> WriteResult: ...

    # Caps (static)
    @staticmethod
    def soft_cap(file: MemoryFile) -> int: ...   # 2048 for MEMORY, 1536 for USER
    @staticmethod
    def hard_cap(file: MemoryFile) -> int: ...   # 4096 for MEMORY, 3072 for USER

    # Composition
    def assemble_system_prompt(self, base: str) -> str:
        """Compose: base + USER.md section + MEMORY.md section."""
```

---

## 5. Caps

| File        | Soft cap | Hard cap | Rationale                                                    |
|-------------|----------|----------|--------------------------------------------------------------|
| `MEMORY.md` | 2048 B   | 4096 B   | Workspace facts; ~500 tokens at soft cap. Fits in context cheaply. |
| `USER.md`   | 1536 B   | 3072 B   | User facts; smaller because typically less to remember.       |

**Soft cap** — write succeeds; `WriteResult.over_soft_cap = True`; the bus emits a `memory.eviction` event (or, more accurately, the tool layer translates the over-soft-cap signal into a hint to `memory_consolidate`). The agent sees the hint in the tool result text.

**Hard cap** — write is rejected by raising `MemoryHardCapExceeded`. The tool layer translates this into a `ToolExecutionError` with `is_user_visible: true`. The agent receives the error and must `memory_consolidate` before adding more.

Sizes are measured in **utf-8 bytes**, not characters. Multi-byte content (rare for code workflows) counts at byte cost.

### 5.1 Why two thresholds?

Soft cap as a signal: the agent learns that consolidation is needed but isn't blocked. Hard cap as an enforcement: the agent literally cannot grow the file past the limit.

In practice the agent typically consolidates between soft and hard cap. Hard cap acts as a runaway-loop safety net: even if the agent ignores the soft-cap signal, the file can't bloat indefinitely.

### 5.2 Why bounded?

Because *unbounded memory destroys context quality*. The dominant alternative — `CLAUDE.md` or `.cursorrules` files that grow indefinitely — turns memory into a noise floor on every turn. The agent reads everything and treats nothing as load-bearing.

By forcing eviction, the agent stays sharp on what matters. The peer with the same stance is Letta (Series-A funded; bounded character-limited core memory blocks with agent self-edit tools). Metis's hard byte budgets are tighter than Letta's character caps; this is a deliberate position.

---

## 6. Tools

Three tools, all `SideEffects.WRITE`, all `requires_workspace: true`. Registered via `metis.memory.tools.register_memory_tools(dispatcher)`.

### 6.1 `memory_add`

Append a single entry to `MEMORY.md` or `USER.md`.

**Input schema:**

```json
{
  "type": "object",
  "properties": {
    "file":  {"type": "string", "enum": ["MEMORY.md", "USER.md"]},
    "entry": {"type": "string", "minLength": 1}
  },
  "required": ["file", "entry"],
  "additionalProperties": false
}
```

**Semantics:**
- Trims `entry`; rejects empty / whitespace-only.
- Appends as a new line. Ensures a trailing newline on the prior content first.
- Returns a human-readable confirmation including the new size.
- If the post-write size exceeds the soft cap, the confirmation includes `"— over soft cap; consider memory_consolidate"`.

### 6.2 `memory_replace`

Replace a unique substring in `MEMORY.md` or `USER.md`.

**Input schema:**

```json
{
  "type": "object",
  "properties": {
    "file": {"type": "string", "enum": ["MEMORY.md", "USER.md"]},
    "old":  {"type": "string"},
    "new":  {"type": "string"}
  },
  "required": ["file", "old", "new"],
  "additionalProperties": false
}
```

**Semantics:**
- `old` must appear exactly once in the file. Zero or many occurrences raise `ToolExecutionError`.
- Useful for surgical edits without rewriting the whole file.

### 6.3 `memory_consolidate`

Replace the entire content of `MEMORY.md` or `USER.md`.

**Input schema:**

```json
{
  "type": "object",
  "properties": {
    "file":    {"type": "string", "enum": ["MEMORY.md", "USER.md"]},
    "content": {"type": "string"}
  },
  "required": ["file", "content"],
  "additionalProperties": false
}
```

**Semantics:**
- The agent uses this when soft-cap pressure builds and the file would benefit from rewriting.
- Hard-cap check still applies. The agent can't `consolidate` to a value larger than the hard cap either.

---

## 7. Events

Two event types in the bus catalog:

### 7.1 `memory.updated`

Fired on every successful write.

```python
{
    "file": Literal["MEMORY.md", "USER.md"],
    "operation": Literal["add", "replace", "consolidate"],
    "before_hash": str,           # SHA-256 hex of utf-8 bytes
    "after_hash": str,
    "before_size_bytes": int,
    "after_size_bytes": int,
}
```

### 7.2 `memory.eviction`

Fired on soft-cap overflow during a write. The session manager (or whatever layer wraps the tool) is responsible for emission; the `MemoryStore` itself doesn't touch the bus.

```python
{
    "file": Literal["MEMORY.md", "USER.md"],
    "trigger": Literal["size_cap_exceeded", "manual"],
    "entries_evicted": int,        # 0 for soft-cap-warning-only
    "size_before_bytes": int,
    "size_after_bytes": int,
}
```

In v1, `entries_evicted` is informational; nothing is auto-evicted. The event is the agent's cue to call `memory_consolidate`.

Sensitivity for both: `private` (the content is verbatim agent memory, potentially including user prompts or workspace facts).

---

## 8. System prompt composition

`MemoryStore.assemble_system_prompt(base: str) -> str` produces:

```
{base}

## User context (USER.md)
{USER.md content}

## Workspace memory (MEMORY.md)
{MEMORY.md content}
```

Empty files are omitted. The `SessionManager` calls this fresh on every LLM call (not cached) so that the same-turn memory writes are visible to the *next* LLM call within the turn.

Implications:
- A memory edit in turn N is visible in the *system prompt* of turn N+1, not in mid-turn LLM calls of turn N (mostly — the next LLM call after the tool dispatch will see the new memory because the system prompt is re-assembled before each call).
- The agent always sees fresh memory; no staleness.

---

## 9. Invariants

1. **`<workspace>/.metis/` is the only on-disk location.** No global memory in v1.
2. **Both files default to empty.** Missing file = empty string. The agent should not see "file not found" errors.
3. **Writes are atomic at the path level.** `Path.write_text` is a single syscall; either the new content lands or it doesn't. No half-written state.
4. **Hashes are over utf-8 bytes, not Python `str`.** Used for `memory.updated` events.
5. **One `MemoryStore` per session.** Injected via `SessionManager`'s `memory_factory`. Multiple sessions in the same workspace share the on-disk files but each session re-reads on every operation; there is no in-process cache.
6. **No locking across processes.** v1 assumes single-writer (one `metis serve` per workspace). Concurrent writes are a Phase 3+ concern when sync ships.

---

## 10. Testing strategy

### 10.1 Required tests

1. **Empty file reads** return `""`, not raise.
2. **`.metis/` is created lazily** on first write.
3. **`add_entry` appends with newline discipline** — joining to non-newline-terminated content adds the newline.
4. **`replace` requires unique `old`** — zero or many matches raise.
5. **Soft-cap overflow allowed** — `WriteResult.over_soft_cap = True`; write succeeds.
6. **Hard-cap overflow rejected** — `MemoryHardCapExceeded` raised; file unchanged.
7. **Hashes round-trip** — `before_hash` on write N+1 equals `after_hash` on write N.
8. **`memory_consolidate` to empty string works** (rewriting to clear).
9. **`assemble_system_prompt` omits empty sections.**
10. **`assemble_system_prompt` includes both sections when both files are populated.**
11. **Tools surface soft-cap hint** in their text output.
12. **Tools translate `MemoryHardCapExceeded` to `ToolExecutionError`** with `is_user_visible: true`.

### 10.2 Property tests

Worth investing in:

- **Monotonicity of size by `add`** — `add_entry` never decreases `after_size_bytes`.
- **Idempotence of `consolidate(content)`** — calling twice with the same `content` produces the same after_hash.

---

## 11. Open questions

1. **Global vs workspace `USER.md`.** v1 is workspace-scoped, which means the agent learns about the user separately per workspace. A future global `~/.metis/USER.md` would compose ahead of the workspace version. Deferred — wait for evidence the duplication hurts.
2. **Per-session memory.** A short-term scratchpad above the bounded files? Probably belongs in canonical message history, not memory. Deferred.
3. **Auto-summarization at session end.** When a session ends, run a brief reflection on what should land in `MEMORY.md` for next time? Decoupled from the file format; would be a Phase 2.5 evaluator concern.
4. **Concurrent-write semantics under sync.** When `git pull` merges someone else's `MEMORY.md` edits, what happens to in-flight writes? Deferred to sync spec (Phase 3+).
5. **Eviction-event semantics.** v1 fires on soft-cap overflow as a signal; should hard-cap rejection also fire an event? Currently no. Argument for: visibility into how often the agent hits the wall. Argument against: failed writes shouldn't pollute the trace.

---

## 12. Decision log

| Date       | Decision                                                  | Rationale                                                                            |
|------------|-----------------------------------------------------------|--------------------------------------------------------------------------------------|
| 2026-05-08 | Two files: `MEMORY.md` and `USER.md`                      | Workspace facts vs. user facts are different concerns; separate caps; separate sync semantics later. |
| 2026-05-08 | Bounded with soft + hard caps                             | "Eviction is a feature" — the dominant alternative (unbounded markdown) destroys context quality. |
| 2026-05-08 | Files-on-disk plain markdown                              | Human-readable, `git add`-able, portable, no DB. The user can edit directly.         |
| 2026-05-08 | Agent-curated via three tools (add/replace/consolidate)   | The agent decides what's worth remembering. Auto-extraction loses signal.            |
| 2026-05-08 | System prompt re-assembled per LLM call                   | Mid-session memory edits visible to the next call without explicit refresh.          |
| 2026-05-12 | Spec drafted retrospectively from existing implementation | Memory is built and tested but was unspec'd; this doc closes the gap.                |

---

## 13. References

- `canonical-message-format.md` — `ToolDefinition`, `ToolUseBlock`, `SideEffects.WRITE`, tool input schema subset.
- `event-bus-and-trace-catalog.md §6.7` — `memory.updated`, `memory.eviction` payloads and sensitivity tags.
- `tool-dispatcher.md` — how memory tools register, dispatch, and emit `tool.called`/`tool.completed`.
- [Letta core blocks](https://docs.letta.com/concepts/memory) — the prior art; per-Letta blocks are character-bounded with agent self-edit tools, same stance.
- agentskills.io spec — separate but related portable-markdown standard for *procedural* knowledge (skills) as opposed to *episodic* knowledge (memory).
